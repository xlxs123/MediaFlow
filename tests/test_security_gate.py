"""Policy fixtures are unit tests, never proof of live cryptographic verification."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import security_gate as gate

COMMIT = 'a' * 40
REPOSITORY = 'policy-fixture/project'
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)


class SecurityWorkflowTests(unittest.TestCase):
    def client(self, *, runs=None, jobs=None):
        current = dict(id=17, run_attempt=2, head_sha=COMMIT, head_branch='main',
            head_repository={'full_name': REPOSITORY}, event='push', path=gate.WORKFLOW,
            status='completed', conclusion='success', html_url='https://github.com/' + REPOSITORY + '/actions/runs/17',
            updated_at=NOW.isoformat())
        class Client:
            calls = []
            def api(self, path):
                self.calls.append(path)
                if '/attempts/2/jobs' in path:
                    return {'jobs': jobs if jobs is not None else [dict(name=name, conclusion='success') for name in gate.CHECKS]}
                return {'workflow_runs': runs if runs is not None else [current]}
        return Client(), current

    def verify(self, client):
        return gate.verify_security_run(client, REPOSITORY, COMMIT, wait_seconds=0, now=NOW)

    def test_exact_commit_and_current_attempt_required_jobs_pass(self):
        client, _ = self.client()
        result = self.verify(client)
        self.assertTrue(result['passed'])
        self.assertEqual(result['commit'], COMMIT)
        self.assertEqual(result['run_attempt'], 2)
        self.assertTrue(any('/17/attempts/2/jobs' in path for path in client.calls))

    def test_latest_failed_run_cannot_be_replaced_with_an_old_green_run(self):
        _, current = self.client()
        old = dict(current, id=16)
        current['conclusion'] = 'failure'
        client, _ = self.client(runs=[old, current])
        with self.assertRaisesRegex(gate.SecurityGateError, 'latest exact-commit'):
            self.verify(client)

    def test_untrusted_event_commit_branch_repository_and_workflow_fail(self):
        for change in ({'event': 'pull_request'}, {'head_sha': 'b' * 40}, {'head_branch': 'feature'},
            {'head_repository': {'full_name': 'other/repository'}}, {'path': '.github/workflows/other.yml'}):
            with self.subTest(change=change):
                _, current = self.client()
                client, _ = self.client(runs=[dict(current, **change)])
                with self.assertRaisesRegex(gate.SecurityGateError, 'No real security workflow'):
                    self.verify(client)

    def test_pending_run_has_a_bounded_wait(self):
        _, current = self.client()
        client, _ = self.client(runs=[dict(current, status='in_progress', conclusion=None)])
        with self.assertRaisesRegex(gate.SecurityGateError, 'bounded wait'):
            self.verify(client)

    def test_missing_skipped_failed_or_duplicate_security_job_blocks(self):
        complete = [dict(name=name, conclusion='success') for name in gate.CHECKS]
        for jobs in (complete[:-1], [*complete, complete[0]],
            [dict(job, conclusion='skipped') for job in complete],
            [dict(job, conclusion='failure') for job in complete]):
            with self.subTest(jobs=jobs):
                client, _ = self.client(jobs=jobs)
                with self.assertRaisesRegex(gate.SecurityGateError, 'required security job'):
                    self.verify(client)

    def test_stale_future_or_invalid_timestamp_blocks(self):
        for timestamp in ((NOW - timedelta(days=9)).isoformat(), (NOW + timedelta(hours=1)).isoformat(), 'invalid'):
            with self.subTest(timestamp=timestamp):
                _, current = self.client()
                client, _ = self.client(runs=[dict(current, updated_at=timestamp)])
                with self.assertRaisesRegex(gate.SecurityGateError, 'stale'):
                    self.verify(client)

    def test_repository_and_commit_inputs_cannot_be_api_paths(self):
        client, _ = self.client()
        for repo, commit in (('../other/repo', COMMIT), (REPOSITORY, 'main'), (REPOSITORY, 'a' * 39)):
            with self.subTest(repo=repo, commit=commit), self.assertRaises(gate.SecurityGateError):
                gate.verify_security_run(client, repo, commit, wait_seconds=0, now=NOW)


class ProvenancePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mediaflow-provenance-policy-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve() / 'build-attestation.json'
        self.path.write_bytes(b'unit byte-bound receipt, not an attestation')
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.statement = dict(predicateType=gate.PROVENANCE_TYPE,
            subject=[dict(name=self.path.name, digest={'sha256': self.sha256})],
            predicate={'runDetails': {'metadata': {'invocationId':
                'https://github.com/' + REPOSITORY + '/actions/runs/17/attempts/1'}}})

    def client(self, *, statement=None, signature=True, timestamps=True, code=0, raw=None):
        entry = {'verificationResult': {'statement': statement or deepcopy(self.statement),
            'signature': {'certificate': {'unit': 'non-cryptographic fixture'}} if signature else {},
            'verifiedTimestamps': [{'unit': True}] if timestamps else []}}
        output = raw if raw is not None else json.dumps([entry]).encode()
        class Runner:
            calls = []
            def call(self, arguments, stage, **kwargs):
                self.calls.append((arguments, stage, kwargs))
                return code, output, b'unit diagnostic; never logged'
        class Client:
            gh = 'unit-cli-not-executed'
            runner = Runner()
            env = {'AUDIT_FIXTURE': '1'}
        return Client()

    def verify(self, client):
        return gate.verify_ci_provenance(client, REPOSITORY, COMMIT, '17', self.path)

    def test_cli_receives_pinned_identity_and_never_logs_raw_output(self):
        client = self.client()
        result = self.verify(client)
        self.assertTrue(result['passed'])
        arguments, _, kwargs = client.runner.calls[0]
        for flag, expected in (('--repo', REPOSITORY), ('--source-digest', COMMIT),
            ('--source-ref', 'refs/heads/main'), ('--signer-workflow', REPOSITORY + '/.github/workflows/ci.yml')):
            self.assertEqual(arguments[arguments.index(flag) + 1], expected)
        self.assertIn('--deny-self-hosted-runners', arguments)
        self.assertFalse(kwargs['logs'])
        self.assertEqual(result['sha256'], self.sha256)

    def test_nonzero_official_verifier_cannot_be_confirmed_away(self):
        with self.assertRaisesRegex(gate.SecurityGateError, 'cryptographic'):
            self.verify(self.client(code=1))

    def test_wrong_digest_run_subject_or_predicate_fails_after_cli(self):
        for field in ('digest', 'run', 'name', 'predicate'):
            with self.subTest(field=field):
                statement = deepcopy(self.statement)
                if field == 'digest':
                    statement['subject'][0]['digest']['sha256'] = 'b' * 64
                elif field == 'run':
                    statement['predicate']['runDetails']['metadata']['invocationId'] += '-different'
                elif field == 'name':
                    statement['subject'][0]['name'] = 'different-receipt.json'
                else:
                    statement['predicateType'] = 'https://unit-fixture.invalid/other-predicate'
                with self.assertRaisesRegex(gate.SecurityGateError, 'does not bind'):
                    self.verify(self.client(statement=statement))

    def test_missing_certificate_or_transparency_timestamp_fails(self):
        for client in (self.client(signature=False), self.client(timestamps=False)):
            with self.subTest(client=client), self.assertRaises(gate.SecurityGateError):
                self.verify(client)

    def test_malformed_or_excessive_verifier_output_fails(self):
        for raw in (b'not-json', b'x' * (4 * 1024 * 1024 + 1), b'{}'):
            with self.subTest(length=len(raw)), self.assertRaises(gate.SecurityGateError):
                self.verify(self.client(raw=raw))


class SarifGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mediaflow-sarif-policy-unit-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write(self, run):
        (self.directory / 'python.sarif').write_text(json.dumps({'version': '2.1.0', 'runs': [run]}), 'utf-8')

    def test_no_sarif_cannot_pass_as_zero_findings(self):
        with self.assertRaisesRegex(gate.SecurityGateError, 'no SARIF'):
            gate.scan_sarif(self.directory)

    def test_successful_empty_analysis_passes(self):
        self.write({'invocations': [{'executionSuccessful': True}], 'results': []})
        self.assertTrue(gate.scan_sarif(self.directory)['passed'])

    def test_failed_analysis_blocks_even_without_results(self):
        self.write({'invocations': [{'executionSuccessful': False}], 'results': []})
        with self.assertRaisesRegex(gate.SecurityGateError, 'unsuccessful'):
            gate.scan_sarif(self.directory)

    def test_findings_block_and_messages_host_paths_are_not_exported(self):
        self.write({'results': [{'ruleId': 'py/unit-rule', 'level': 'error',
            'message': {'text': 'This unit message must not appear in the public summary'},
            'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'file:///private/host/path'},
                'region': {'startLine': 5}}}]}]})
        result = gate.scan_sarif(self.directory)
        self.assertFalse(result['passed'])
        self.assertEqual(result['unresolved_findings'], 1)
        self.assertNotIn('private/host', json.dumps(result))
        self.assertNotIn('unit message', json.dumps(result))
