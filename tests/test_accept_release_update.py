"""Owned-process and binding unit tests; no executable or public network runs.

Synthetic child reports exercise the parent contract only. Their parent summary
must be marked unit_process_fixture and cannot claim public online acceptance.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import accept_release_update as driver


class AcceptanceDriverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-acceptance-driver-unit-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.release, self.old = self.root / 'release', self.root / 'old-release'
        self.release.mkdir(); self.old.mkdir()
        self.source = 'https://github.com/unit-owner/MediaFlow-Releases/releases/latest/download/manifest.json'
        self.stamp = {'app_version': '2.1.0', 'build_id': 'unit-driver-current', 'packaged': True,
            'source_sha256': 'a' * 64, 'update_channel': {'key_state': 'initialized',
                'source_sha256': hashlib.sha256(self.source.encode()).hexdigest(),
                'key_fingerprints': {'unit-not-production': 'b' * 64}}}
        self.old_stamp = dict(self.stamp, app_version='2.0.1', build_id='unit-driver-old', source_sha256='b' * 64)
        self.current_bytes = b'SYNTHETIC EXE; NEVER RUN BY UNIT TESTS; CURRENT'
        self.old_bytes = b'SYNTHETIC EXE; NEVER RUN BY UNIT TESTS; OLD'
        (self.release / 'MediaFlow.exe').write_bytes(self.current_bytes)
        (self.old / 'MediaFlow.exe').write_bytes(self.old_bytes)
        self.save(self.release / 'build-info.json', self.stamp)
        self.save(self.old / 'build-info.json', self.old_stamp)
        self.report = self.root / 'native-acceptance'

    @staticmethod
    def save(path, value):
        data = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
        path.write_bytes(data)
        return data

    def native_report(self, directory, target):
        checks = [{'passed': True, 'description': text} for text in (
            'Tampered remote manifest is rejected',
            'Public ZIP actual size and full SHA-256 match signed manifest',
            'Tampered real downloaded ZIP refused before extraction or deployment',
            'Deployed EXE actually runs and reports expected build',
            'Preserved old release is byte-for-byte unchanged',
            'Old database/history/tasks/media/profile fixture bytes unchanged',
            'Actual new GUI launch returned a real successful result',
            'Actual old GUI rollback returned a real successful result')]
        for version in ('2.1.0', '2.0.1'):
            checks += [{'passed': True, 'version': version, 'description': text} for text in (
                'Actual GUI stays running and initializes private media library',
                'Launched GUI media library is readable')]
        identity = dict(self.stamp, executable=str(self.release / 'MediaFlow.exe'), runtime_root='unit-cache')
        new_identity = dict(self.stamp, executable=str(target / 'MediaFlow.exe'), runtime_root='unit-new-cache')
        package = {'format': 'zip', 'size': 256, 'sha256': 'c' * 64,
            'url': 'https://github.com/unit-owner/MediaFlow-Releases/releases/download/v2.1.0/unit.zip'}
        return {'passed': True, 'scope': 'real_public_https_packaged_client', 'public_online_verified': True,
            'runtime_identity': identity, 'checks': checks,
            'installation_context': {'version': '2.0.1', 'build_id': self.old_stamp['build_id']},
            'public_manifest': {'source': self.source, 'version': '2.1.0',
                'build_id': self.stamp['build_id'], 'key_id': 'unit-not-production', 'package': package},
            'operation': {'state': 'healthy', 'authenticated': True, 'version': '2.1.0',
                'build_id': self.stamp['build_id'], 'package_size': 256, 'package_sha256': 'c' * 64,
                'origin_version': '2.0.1', 'origin_sha256': hashlib.sha256(self.old_bytes).hexdigest(),
                'current_path': str(self.old), 'target_path': str(target),
                'health': {'passed': True, 'identity': new_identity}},
            'launched_processes': [
                {'version': '2.1.0', 'pid': 4242, 'path': str(target / 'MediaFlow.exe'), 'library_readable': True},
                {'version': '2.0.1', 'pid': 4243, 'path': str(self.old / 'MediaFlow.exe'), 'library_readable': True}]}

    def fake_process(self, *, mutate=None, exit_code=0, omit_report=False, mutate_exe=False):
        def run(command, **options):
            self.assertFalse(self.report.exists(), 'Only the native child may create its report directory.')
            self.assertEqual(command, [str(self.release / 'MediaFlow.exe'), '--update-public-check', str(self.report),
                '--update-old-release', str(self.old)])
            self.assertEqual(options['cwd'], self.release)
            self.assertEqual(options['environment']['QT_QPA_PLATFORM'], 'offscreen')
            self.assertGreater(options['timeout'], 0)
            if not omit_report:
                self.report.mkdir()
                target = self.report / 'updates/versions/MediaFlow-unit-current'
                target.mkdir(parents=True)
                (target / 'MediaFlow.exe').write_bytes(self.current_bytes)
                value = self.native_report(self.report, target)
                if mutate:
                    mutate(value)
                self.native_bytes = self.save(self.report / 'result.json', value)
            if mutate_exe:
                (self.release / 'MediaFlow.exe').write_bytes(self.current_bytes + b'changed')
            return {'execution_kind': 'unit_process_fixture', 'exit_code': exit_code,
                    'timed_out': False, 'cancelled': False, 'process_tree_cleaned': True, 'elapsed_seconds': .01}
        return run

    def invoke(self, fake=None, **changes):
        values = dict(release_dir=self.release, old_release_dir=self.old,
            expected_version='2.1.0', report_dir=self.report, timeout=30)
        values.update(changes)
        with patch.object(driver, '_run_process', side_effect=fake or self.fake_process()):
            return driver.run_acceptance(**values)

    def test_valid_bound_unit_process_is_never_reported_as_public_acceptance(self):
        result = self.invoke()
        self.assertTrue(result['passed'])
        self.assertTrue(result['runtime_identity_bound'])
        self.assertTrue(result['new_gui_verified'])
        self.assertTrue(result['old_gui_verified'])
        self.assertFalse(result['public_online_verified'])
        self.assertEqual(result['scope'], 'unit_process_fixture')
        self.assertEqual(result['exe_sha256'], hashlib.sha256(self.current_bytes).hexdigest())
        self.assertEqual((self.report / 'result.json').read_bytes(), self.native_bytes)
        self.assertEqual(json.loads((self.report / 'summary.json').read_text('utf-8')), result)
        self.assertEqual((self.old / 'MediaFlow.exe').read_bytes(), self.old_bytes)

    def test_exit_zero_with_missing_failed_or_incomplete_native_evidence_is_rejected(self):
        mutations = [lambda r: r.update(passed=False), lambda r: r.update(public_online_verified=False),
            lambda r: r['checks'][0].update(passed=False), lambda r: r['checks'].pop(0),
            lambda r: r['launched_processes'].pop(),
            lambda r: r['checks'].__setitem__(-1, dict(r['checks'][-1], version='wrong-version'))]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.report = self.root / ('bad-native-' + str(index))
                result = self.invoke(self.fake_process(mutate=mutate))
                self.assertFalse(result['passed'])
                self.assertFalse(result['public_online_verified'])
                self.assertEqual(result['scope'], 'unit_process_fixture')
                self.assertEqual((self.report / 'result.json').read_bytes(), self.native_bytes)
        self.report = self.root / 'missing-native'
        result = self.invoke(self.fake_process(omit_report=True))
        self.assertFalse(result['passed'])
        self.assertFalse((self.report / 'result.json').exists())
        self.assertTrue((self.report / 'summary.json').is_file())

    def test_nonzero_process_exit_overrules_a_passed_native_report(self):
        result = self.invoke(self.fake_process(exit_code=1))
        self.assertFalse(result['passed'])
        self.assertTrue(json.loads((self.report / 'result.json').read_text('utf-8'))['passed'])
        self.assertEqual(result['runner']['exit_code'], 1)

    def test_exe_runtime_source_context_and_deployed_exe_bindings_are_enforced(self):
        changes = [lambda r: r['runtime_identity'].update(source_sha256='d' * 64),
            lambda r: r['runtime_identity'].update(executable=str(self.old / 'MediaFlow.exe')),
            lambda r: r['installation_context'].update(version='2.1.0'),
            lambda r: r['operation'].update(origin_sha256='d' * 64),
            lambda r: r['public_manifest'].update(build_id='wrong-build'),
            lambda r: r['public_manifest'].update(key_id='untrusted-unit-key'),
            lambda r: Path(r['operation']['target_path'], 'MediaFlow.exe').write_bytes(b'wrong deployed EXE')]
        for index, mutate in enumerate(changes):
            with self.subTest(case=index):
                self.report = self.root / ('bad-binding-' + str(index))
                self.assertFalse(self.invoke(self.fake_process(mutate=mutate))['passed'])
        self.report = self.root / 'changed-input-exe'
        result = self.invoke(self.fake_process(mutate_exe=True))
        self.assertFalse(result['passed'])
        self.assertIn('输入 EXE', result['message'])

    def test_existing_overlapping_or_wrong_version_paths_never_start_a_process(self):
        existing = self.root / 'existing-report'
        existing.mkdir()
        marker = existing / 'result.json'
        marker.write_bytes(b'Existing evidence remains intact.')
        choices = [{'report_dir': existing}, {'report_dir': self.release / 'new-report'},
            {'report_dir': self.old / 'new-report'}, {'old_release_dir': self.release},
            {'expected_version': '2.1.1'}, {'release_dir': self.old, 'old_release_dir': self.release}]
        for change in choices:
            values = dict(release_dir=self.release, old_release_dir=self.old,
                expected_version='2.1.0', report_dir=self.report)
            values.update(change)
            with self.subTest(change=list(change)), patch.object(driver, '_run_process') as process, \
                    self.assertRaises(driver.AcceptanceError):
                driver.run_acceptance(**values)
            process.assert_not_called()
        self.assertEqual(marker.read_bytes(), b'Existing evidence remains intact.')
        self.assertFalse(self.report.exists())

    def test_secret_and_tls_override_environment_is_not_inherited(self):
        secrets = {'GH_TOKEN': 'unit-gh-token', 'GITHUB_TOKEN': 'unit-github-token',
            'MEDIAFLOW_CUSTOM_SIGNING_PASSWORD': 'unit-signing-password',
            'SSL_CERT_FILE': 'unit-custom-ca', 'SSL_CERT_DIR': 'unit-ca-directory',
            'REQUESTS_CA_BUNDLE': 'unit-custom-ca', 'CURL_CA_BUNDLE': 'unit-custom-ca',
            'SSLKEYLOGFILE': 'unit-key-log', 'PYTHONPATH': 'unit-python-path'}
        with patch.dict(os.environ, secrets):
            environment = driver._environment(self.root / 'isolated-environment')
        self.assertTrue(set(environment).isdisjoint(secrets))
        self.assertEqual(environment['QT_QPA_PLATFORM'], 'offscreen')
        self.assertEqual(environment['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        for name in ('APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
            self.assertTrue(Path(environment[name]).is_relative_to(self.root))

    def test_timeout_is_finite_and_owned_tree_is_stopped_and_closed(self):
        class Process:
            pid, returncode = 4242, None
            def poll(self): return self.returncode
            def wait(self, timeout):
                if self.returncode is None: raise subprocess.TimeoutExpired('unit-owned-child', timeout)
                return self.returncode
        class Tree:
            stopped, closed = False, False
            def terminate(self):
                self.stopped = True
                process.returncode = -9
            def close(self): self.closed = True
        process, tree = Process(), Tree()
        workspace = self.root / 'timeout-runner'
        workspace.mkdir()
        with patch.object(driver.subprocess, 'Popen', return_value=process) as launch, \
                patch.object(driver, 'ProcessTree', return_value=tree), \
                patch.object(driver.time, 'monotonic', side_effect=[0, .02, .03]):
            result = driver._run_process(['unit-child'], cwd=self.release,
                environment={}, workspace=workspace, timeout=.01)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['process_tree_cleaned'])
        self.assertTrue(tree.stopped and tree.closed)
        self.assertEqual(result['exit_code'], -9)
        self.assertFalse(launch.call_args.kwargs['shell'])

    def test_invalid_timeouts_and_cli_failures_cannot_claim_success(self):
        for timeout in (0, -1, 2401, float('inf'), float('nan'), True):
            with self.subTest(timeout=str(timeout)), patch.object(driver, '_run_process') as process, \
                    self.assertRaises(driver.AcceptanceError):
                driver.run_acceptance(self.release, self.old, '2.1.0', self.report, timeout=timeout)
            process.assert_not_called()
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(driver, '_run_process', side_effect=self.fake_process(exit_code=1)), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(driver.main(['--release-dir', str(self.release), '--old-release-dir', str(self.old),
                '--expected-version', '2.1.0', '--report-dir', str(self.report), '--timeout', '30']), 1)
        self.assertFalse(json.loads(output.getvalue())['passed'])


if __name__ == '__main__':
    unittest.main()
