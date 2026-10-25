"""Synthetic SARIF boundaries; real public-data review evidence is separate."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import security_gate as gate


class PreciseCodeQLReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mediaflow-codeql-review-unit-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'sarif'
        self.directory.mkdir()
        for name in (*gate.PUBLIC_METADATA_GUARDS, 'main.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('public fixture bytes\n', 'utf-8')
        physical = lambda name: {'artifactLocation': {'uri': name, 'uriBaseId': '%SRCROOT%'},
            'region': {'startLine': 1, 'startColumn': 1, 'endColumn': 20}}
        self.entry = {'ruleId': 'py/clear-text-logging-sensitive-data',
            'locations': [{'physicalLocation': physical('main.py')}],
            'partialFingerprints': {'primaryLocationLineHash': 'unit-known-line:1'},
            'codeFlows': [{'threadFlows': [{'locations': [
                {'location': {'physicalLocation': physical('utils/build_info.py'),
                    'message': {'text': 'public fingerprint fixture'}}},
                {'location': {'physicalLocation': physical('main.py')}}]}]}]}
        # One flow/thread has no table indexes; this independently encodes the
        # documented canonical form rather than using the implementation helper.
        canonical = json.dumps(self.entry['codeFlows'], ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')
        self.review = {'rule_id': self.entry['ruleId'], 'classification': 'reviewed_public_metadata',
            'primary_location': {'path': 'main.py', 'region': deepcopy(self.entry['locations'][0]['physicalLocation']['region'])},
            'partial_fingerprints': deepcopy(self.entry['partialFingerprints']),
            'codeflows_sha256': hashlib.sha256(canonical).hexdigest(), 'codeflow_count': 1,
            'dependency_sha256': {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in (*gate.PUBLIC_METADATA_GUARDS, 'main.py')},
            'reason': 'Only this exact synthetic public fingerprint flow is reviewed in the unit fixture.',
            'evidence': {'source_sarif_sha256': 'a' * 64, 'source_commit': 'b' * 40, 'codeql_run': '1'},
            'reviewed_by': 'Unit fixture reviewer'}

    def scan(self, *, entry=None, review=None):
        path = self.root / 'security/codeql-reviews.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'canonicalization': 'codeql-complete-flow-v1',
            'reviews': [self.review if review is None else review]}), 'utf-8')
        (self.directory / 'python.sarif').write_text(json.dumps({'version': '2.1.0', 'runs': [
            {'results': [self.entry if entry is None else entry]}]}), 'utf-8')
        return gate.scan_sarif(self.directory, root=self.root)

    def test_exact_public_flow_is_reviewed_and_still_counted(self):
        report = self.scan()
        self.assertTrue(report['passed'])
        self.assertEqual(report['total_findings'], 1)
        self.assertEqual(report['reviewed_public_metadata'], 1)
        self.assertEqual(report['unresolved_findings'], 0)
        self.assertEqual(len(report['reviewed_findings']), 1)

    def test_additional_source_is_not_hidden_by_same_primary_fingerprint(self):
        entry = deepcopy(self.entry)
        entry['codeFlows'][0]['threadFlows'][0]['locations'].insert(0,
            {'location': {'physicalLocation': {'artifactLocation': {'uri': 'unreviewed_source.py'},
                'region': {'startLine': 2}}}})
        self.assertEqual(self.scan(entry=entry)['unresolved_findings'], 1)

    def test_any_guard_or_sink_source_change_invalidates_review(self):
        for name in (*gate.PUBLIC_METADATA_GUARDS, 'main.py'):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(original + b'changed source contract\n')
                self.assertFalse(self.scan()['passed'])
                path.write_bytes(original)

    def test_changed_rule_location_or_fingerprint_still_blocks(self):
        for change in ('rule', 'line', 'fingerprint'):
            entry = deepcopy(self.entry)
            if change == 'rule':
                entry['ruleId'] = 'py/clear-text-storage-sensitive-data'
            elif change == 'line':
                entry['locations'][0]['physicalLocation']['region']['startLine'] = 2
            else:
                entry['partialFingerprints']['primaryLocationLineHash'] = 'different-line:1'
            with self.subTest(change=change):
                self.assertFalse(self.scan(entry=entry)['passed'])

    def test_arbitrary_rule_or_empty_fingerprint_review_is_rejected(self):
        for change in ('rule', 'fingerprint', 'guard'):
            review = deepcopy(self.review)
            if change == 'rule':
                review['rule_id'] = 'py/path-injection'
            elif change == 'fingerprint':
                review['partial_fingerprints'] = {}
            else:
                review['dependency_sha256'].pop('core/update_manifest.py')
            with self.subTest(change=change), self.assertRaises(gate.SecurityGateError):
                self.scan(review=review)

    def test_untrusted_review_path_is_rejected(self):
        review = deepcopy(self.review)
        review['dependency_sha256']['../unread-private-source.py'] = 'a' * 64
        with self.assertRaises(gate.SecurityGateError):
            self.scan(review=review)

    def test_artifact_table_index_changes_only_with_explicit_same_uri_are_safe(self):
        entry = deepcopy(self.entry)
        artifact = entry['codeFlows'][0]['threadFlows'][0]['locations'][0]['location']['physicalLocation']['artifactLocation']
        artifact['index'] = 999
        self.assertTrue(self.scan(entry=entry)['passed'])
        artifact.pop('uri')
        self.assertFalse(self.scan(entry=entry)['passed'])

    def test_complete_thread_and_source_order_is_bound(self):
        entry = deepcopy(self.entry)
        entry['codeFlows'][0]['threadFlows'][0]['locations'].reverse()
        self.assertFalse(self.scan(entry=entry)['passed'])
