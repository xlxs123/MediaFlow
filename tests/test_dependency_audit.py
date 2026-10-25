"""Fail-closed dependency gate tests; synthetic advisories are not live scans."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import audit_dependencies as audit


class DependencyAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-dependency-unit-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requirements = self.root / 'requirements.txt'
        self.requirements.write_text('synthetic-app==1.0\n', encoding='utf-8')
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps(audit.POLICY), encoding='utf-8')
        self.packages = {'synthetic-app': '1.0', 'synthetic-child': '2.0'}
        self.target = {'prefix': str(self.root / 'build'), 'python': '3.14.0',
            'implementation': 'CPython', 'system': 'Windows', 'machine': 'AMD64',
            'distributions': [{'name': name, 'version': resolved}
                              for name, resolved in self.packages.items()]}
        self.tool = dict(self.target, prefix=str(self.root / 'scanner'))
        self.tool_packages = {'pip-audit': '2.10.1'}
        self.document = {'dependencies': [dict(name=name, version=resolved, vulns=[])
            for name, resolved in self.packages.items()], 'fixes': []}
        self.args = SimpleNamespace(requirements=self.requirements, policy=self.policy,
            environment_python='synthetic-build-python', audit_python='synthetic-audit-python',
            output=self.root / 'report')

    def run_audit(self, *, document=None, exit_code=0, after=None, failure=None, check_code=0):
        document = self.document if document is None else document
        calls = []

        def process(arguments, *, timeout):
            calls.append((arguments, timeout))
            if arguments[-1] == 'check':
                return subprocess.CompletedProcess(arguments, check_code, b'', b'')
            if failure:
                raise failure
            return subprocess.CompletedProcess(arguments, exit_code, json.dumps(document).encode(),
                                               b'Synthetic scanner status; never serialized verbatim.')

        snapshots = [(self.target, self.packages), (self.tool, self.tool_packages),
                     after if after is not None else (self.target, self.packages)]
        with patch.object(audit, 'inspect_environment', side_effect=snapshots), \
             patch.object(audit, 'run_process', side_effect=process):
            result = audit.audit(self.args)
        return result, calls

    def test_complete_installed_environment_includes_transitive_packages(self):
        result, calls = self.run_audit()
        self.assertTrue(result['passed'])
        self.assertEqual(result['coverage'], {'installed': 2, 'audited': 2,
                                              'missing': [], 'complete': True})
        self.assertTrue(result['environment_unchanged'])
        self.assertTrue(result['inputs_unchanged'])
        roles = {item['name']: item['role'] for item in result['dependencies']}
        self.assertEqual(roles['synthetic-app'], 'direct_requirement')
        self.assertEqual(roles['synthetic-child'], 'transitive_or_build_environment')
        pins = (self.args.output / 'resolved-requirements.txt').read_text('utf-8')
        self.assertEqual(pins, 'synthetic-app==1.0\nsynthetic-child==2.0\n')
        arguments = calls[1][0]
        self.assertEqual(arguments[0], self.args.audit_python)
        self.assertIn('--disable-pip', arguments)
        self.assertIn('--no-deps', arguments)
        self.assertNotIn('--fix', arguments)
        self.assertNotIn('--ignore-vuln', arguments)
        encoded = json.dumps(result)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn('Synthetic scanner status', encoded)

    def test_transitive_unknown_severity_advisory_blocks_and_retains_identifiers(self):
        document = deepcopy(self.document)
        document['dependencies'][1]['vulns'] = [{'id': 'PYSEC-2099-1',
            'aliases': ['CVE-2099-1000'], 'fix_versions': ['2.1'],
            'description': 'Synthetic private URL https://unit.invalid/?token=do-not-persist'}]
        result, _ = self.run_audit(document=document, exit_code=1)
        self.assertFalse(result['passed'])
        self.assertEqual(result['vulnerability_count'], 1)
        finding = result['dependencies'][1]['vulnerabilities'][0]
        self.assertEqual(finding, {'id': 'PYSEC-2099-1', 'aliases': ['CVE-2099-1000'],
                                  'fix_versions': ['2.1'], 'severity': 'UNKNOWN', 'blocking': True})
        self.assertIn('reported_vulnerabilities_blocked', result['failure_reasons'])
        self.assertNotIn('do-not-persist', json.dumps(result))

    def test_high_and_critical_findings_cannot_be_treated_as_warnings(self):
        document = deepcopy(self.document)
        document['dependencies'][0]['vulns'] = [
            {'id': 'CVE-2099-1001', 'severity': 'HIGH'},
            {'id': 'CVE-2099-1002', 'severity': 'CRITICAL'}]
        result, _ = self.run_audit(document=document, exit_code=1)
        self.assertFalse(result['passed'])
        self.assertTrue(all(item['blocking'] for item in result['dependencies'][0]['vulnerabilities']))

    def test_lookup_skipped_without_version_or_vulns_is_not_clean_coverage(self):
        document = deepcopy(self.document)
        document['dependencies'][1] = {'name': 'synthetic-child',
            'skip_reason': 'Synthetic https://private.invalid/?token=do-not-persist'}
        result, _ = self.run_audit(document=document, exit_code=1)
        self.assertFalse(result['passed'])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['skipped'], [{'name': 'synthetic-child', 'version': '2.0',
                                             'reason': 'vulnerability_lookup_skipped'}])
        self.assertNotIn('do-not-persist', json.dumps(result))

    def test_missing_transitive_package_is_blocked(self):
        document = deepcopy(self.document)
        document['dependencies'].pop()
        result, _ = self.run_audit(document=document)
        self.assertFalse(result['passed'])
        self.assertEqual(result['coverage']['missing'], ['synthetic-child'])

    def test_mismatched_audited_version_is_blocked(self):
        document = deepcopy(self.document)
        document['dependencies'][1]['version'] = '3.0'
        result, _ = self.run_audit(document=document)
        self.assertFalse(result['passed'])
        self.assertIn('audit_package_version_mismatch', result['failure_reasons'])

    def test_unexpected_or_duplicate_audited_packages_are_blocked(self):
        document = deepcopy(self.document)
        document['dependencies'].append(dict(document['dependencies'][0]))
        result, _ = self.run_audit(document=document)
        self.assertFalse(result['passed'])
        self.assertIn('audit_duplicate_or_unexpected_dependency', result['failure_reasons'])

    def test_same_tool_and_build_environment_is_blocked_before_scan(self):
        self.tool['prefix'] = self.target['prefix']
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('audit_tool_and_build_environments_must_be_distinct', result['failure_reasons'])
        self.assertEqual(calls, [])

    def test_audit_tool_in_build_environment_is_blocked(self):
        self.packages['pip-audit'] = '2.10.1'
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('audit_tool_must_not_be_installed_in_build_environment', result['failure_reasons'])
        self.assertEqual(calls, [])

    def test_unreviewed_scanner_version_is_blocked_before_scan(self):
        self.tool_packages['pip-audit'] = '0.0'
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('audit_tool_version_not_reviewed_pin', result['failure_reasons'])
        self.assertEqual(calls, [])

    def test_installed_direct_dependency_must_match_requirements_pin(self):
        self.packages['synthetic-app'] = '1.1'
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('installed_direct_requirements_do_not_match_pins', result['failure_reasons'])
        self.assertEqual(calls, [])

    def test_build_pins_are_checked_fingerprinted_and_not_labeled_runtime(self):
        self.args.build_requirements = self.root / 'requirements-build.txt'
        self.args.build_requirements.write_text('synthetic-child==2.0\n', encoding='utf-8')
        result, _ = self.run_audit()
        self.assertTrue(result['passed'])
        self.assertEqual(result['build_requirements'], [{'name': 'synthetic-child', 'version': '2.0'}])
        self.assertEqual(result['dependencies'][1]['role'], 'build_requirement')
        self.assertEqual(len(result['build_requirements_sha256']), 64)
        self.assertTrue(result['inputs_unchanged'])

    def test_installed_build_bootstrap_must_match_reviewed_pin(self):
        self.args.build_requirements = self.root / 'requirements-build.txt'
        self.args.build_requirements.write_text('synthetic-child==2.1\n', encoding='utf-8')
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('installed_build_requirements_do_not_match_pins', result['failure_reasons'])
        self.assertEqual(calls, [])

    def test_build_and_application_pin_conflicts_are_rejected(self):
        self.args.build_requirements = self.root / 'requirements-build.txt'
        self.args.build_requirements.write_text('synthetic-app==2.0\n', encoding='utf-8')
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('application_and_build_pins_conflict', result['failure_reasons'])
        self.assertEqual(calls, [])

    def test_inconsistent_installed_dependencies_are_blocked_before_lookup(self):
        result, calls = self.run_audit(check_code=1)
        self.assertFalse(result['passed'])
        self.assertIn('installed_environment_dependency_inconsistency', result['failure_reasons'])
        self.assertEqual(len(calls), 1)

    def test_environment_change_during_audit_invalidates_receipt(self):
        after = dict(self.packages, **{'synthetic-child': '3.0'})
        result, _ = self.run_audit(after=(self.target, after))
        self.assertFalse(result['passed'])
        self.assertIn('audited_environment_or_inputs_changed_during_scan', result['failure_reasons'])

    def test_scanner_timeout_is_a_blocking_report_not_a_pass(self):
        result, _ = self.run_audit(failure=audit.DependencyAuditError('process_timeout'))
        self.assertFalse(result['passed'])
        self.assertEqual(result['last_stage'], 'advisory_lookup')
        self.assertIn('process_timeout', result['failure_reasons'])
        self.assertTrue((self.args.output / 'dependency-audit.json').is_file())

    def test_failed_service_with_empty_findings_cannot_pass(self):
        result, _ = self.run_audit(exit_code=2)
        self.assertFalse(result['passed'])
        self.assertIn('audit_process_or_vulnerability_service_failed', result['failure_reasons'])

    def test_network_or_tool_error_without_json_is_blocked_and_redacted(self):
        snapshots = [(self.target, self.packages), (self.tool, self.tool_packages)]
        raw = b'Synthetic network error https://unit.invalid/?token=do-not-persist'

        def process(arguments, *, timeout):
            if arguments[-1] == 'check':
                return subprocess.CompletedProcess(arguments, 0, b'', b'')
            return subprocess.CompletedProcess(arguments, 1, raw, raw)

        with patch.object(audit, 'inspect_environment', side_effect=snapshots), \
             patch.object(audit, 'run_process', side_effect=process):
            result = audit.audit(self.args)
        self.assertFalse(result['passed'])
        self.assertIn('audit_service_or_tool_failed_without_valid_json', result['failure_reasons'])
        self.assertNotIn('do-not-persist', (self.args.output / 'dependency-audit.json').read_text('utf-8'))

    def test_weakened_policy_or_advisory_ignore_is_rejected(self):
        value = deepcopy(audit.POLICY)
        value['ignored_vulnerabilities'] = ['CVE-2099-1001']
        self.policy.write_text(json.dumps(value), encoding='utf-8')
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('policy_does_not_match_reviewed_fail_closed_policy', result['failure_reasons'])
        self.assertEqual(calls, [])

    def test_requirement_urls_or_command_options_are_not_accepted_or_logged(self):
        self.requirements.write_text('synthetic-app @ https://unit.invalid/?token=do-not-persist\n',
                                     encoding='utf-8')
        result, calls = self.run_audit()
        self.assertFalse(result['passed'])
        self.assertIn('requirements_must_use_plain_exact_pins', result['failure_reasons'])
        self.assertNotIn('do-not-persist', json.dumps(result))
        self.assertEqual(calls, [])

    def test_previous_report_directory_is_never_overwritten(self):
        self.args.output.mkdir()
        evidence = self.args.output / 'evidence.json'
        evidence.write_bytes(b'original-evidence')
        with self.assertRaises(FileExistsError):
            audit.audit(self.args)
        self.assertEqual(evidence.read_bytes(), b'original-evidence')

    def test_inventory_rejects_ambiguous_distribution_names(self):
        value = deepcopy(self.target)
        value['distributions'].append({'name': 'Synthetic_App', 'version': '1.0'})
        completed = subprocess.CompletedProcess([], 0, json.dumps(value).encode(), b'')
        with patch.object(audit, 'run_process', return_value=completed):
            with self.assertRaisesRegex(audit.DependencyAuditError, 'duplicate_environment_distribution'):
                audit.inspect_environment('synthetic-python', timeout=30)

    def test_ambient_audit_options_cannot_suppress_or_redirect_the_gate(self):
        with patch.dict(audit.os.environ, {'PIP_AUDIT_IGNORE_VULN': 'CVE-2099-1001',
                                         'PIP_AUDIT_OUTPUT': 'private-destination'}):
            cleaned = audit.safe_environment()
        self.assertNotIn('PIP_AUDIT_IGNORE_VULN', cleaned)
        self.assertNotIn('PIP_AUDIT_OUTPUT', cleaned)


if __name__ == '__main__':
    unittest.main()
