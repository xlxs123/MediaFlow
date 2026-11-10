"""Exercise the ordinary runner with small, controlled test fixtures."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import run_unit_tests as entry
from tests.core_test_telemetry import command_label


class CoreTestTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-core-telemetry-')
        self.addCleanup(self.temporary.cleanup)
        # Runner Temp may spell the same directory with an 8.3 account alias.
        self.root = Path(self.temporary.name).resolve()

    def test_command_label_only_exposes_known_program_and_operation(self):
        private = 'synthetic_argument_diagnostic_canary'
        self.assertEqual(command_label(['G:/opaque/path/pwsh.exe', '-File',
                                        'C:/script/authenticode.ps1', private]), 'pwsh:authenticode.ps1')
        self.assertEqual(command_label(['git.exe', 'log', private]), 'git:log')
        self.assertEqual(command_label(['gitleaks.exe', private]), 'gitleaks')
        self.assertEqual(command_label([private + '.exe', private]), 'other')
        self.assertEqual(command_label(private), 'command-string')

    def test_core_entry_keeps_class_skip_failures_and_nonoverlapping_resource_evidence(self):
        evidence = self.root / 'report'
        input_file = self.root / 'synthetic-input.txt'
        input_file.write_bytes(b'fixture')
        canary = 'synthetic_argument_diagnostic_canary'

        class FixtureSkip(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest('synthetic fixture skip')

            def test_not_started(self):
                self.fail('skipped fixture unexpectedly ran')

        class Cases(unittest.TestCase):
            def test_resources(self):
                self.assertEqual(input_file.read_bytes(), b'fixture')
                child = subprocess.run([sys.executable, '-B', '-c', 'print(123)', canary],
                                       capture_output=True, timeout=10)
                self.assertEqual(child.returncode, 0)
                self.assertEqual(child.stdout.strip(), b'123')

            def test_skip(self):
                self.skipTest('synthetic per-test skip')

            def test_failure(self):
                self.fail('synthetic case failure')

        suite = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in (FixtureSkip, Cases))
        with patch.object(entry, 'report_directory', return_value=evidence), \
                patch.object(entry, 'gui_modules', return_value=[]), \
                patch.object(unittest.defaultTestLoader, 'discover', return_value=suite), \
                patch('utils.build_info.source_digest', return_value='fixture-digest'), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(entry.run_group('core'), 1)
        summary = json.loads((evidence / 'unit-core-results.json').read_text('utf-8'))
        self.assertEqual((summary['tests'], summary['failures'], summary['errors'], summary['skipped']),
                         (3, 1, 0, 2))
        timing = summary['timing']
        self.assertEqual(timing['planned_cases'], 4)
        self.assertEqual((timing['tests_started'], timing['tests_finished']), (3, 3))
        self.assertEqual(timing['observed_unittest_counts']['skipped'], 2)
        resources = next(row['observed_operations'] for row in timing['slowest_tests']
                         if row['test'].endswith('.test_resources'))
        self.assertEqual(resources['path.read_bytes']['calls'], 1)
        self.assertNotIn('path.open', resources)
        for fixture in ('setUp', 'tearDown', 'cleanups'):
            self.assertEqual(resources['fixture.' + fixture]['calls'], 1)
        tag = command_label([sys.executable])
        self.assertEqual(resources['child.launch:' + tag]['calls'], 1)
        self.assertIn('child.wait:' + tag, resources)
        progress = entry.progress_path(evidence, 'core').read_text('utf-8')
        self.assertNotIn(canary, progress)
        rows = [json.loads(line) for line in progress.splitlines()]
        started = [row for row in rows if row['phase'] == 'child_launch']
        ended = [row for row in rows if row['phase'] == 'child_exit']
        self.assertEqual(len(started), 1)
        self.assertEqual(len(ended), 1)
        self.assertEqual(started[0]['pid'], ended[0]['pid'])
        self.assertEqual(ended[0]['exit_code'], 0)

    def test_full_timeout_retains_completed_observations_and_unstarted_groups(self):
        evidence = self.root / 'fresh'
        arguments = ['tests/run_unit_tests.py', '--report-dir', str(evidence),
                     '--security-python', 'synthetic-security-python.exe']

        def child(command, **options):
            group = command[4]
            self.assertEqual(command[6], str(evidence))
            if group == 'core':
                self.assertEqual(options['timeout'], 1200)
                (evidence / 'unit-core-results.json').write_text(json.dumps({
                    'group': 'core', 'passed': True, 'tests': 3, 'failures': 0,
                    'errors': 0, 'skipped': 1,
                }), encoding='utf-8')
                return {'timeout_seconds': 1200, 'timed_out': False, 'exit_code': 0,
                        'launcher_pid': 100, 'elapsed_seconds': 5}
            self.assertEqual(group, 'security-tools')
            self.assertEqual(command[0], 'synthetic-security-python.exe')
            self.assertEqual(options['timeout'], 900)
            rows = [
                {'phase': 'test_start', 'test': 'fixture.first'},
                {'phase': 'test_end', 'test': 'fixture.first', 'case_seconds': 1,
                 'unittest_count_delta': {'failures': 1, 'errors': 0, 'skipped': 0}},
                {'phase': 'test_start', 'test': 'fixture.pending'},
            ]
            entry.progress_path(evidence, group).write_text(
                '\n'.join(map(json.dumps, rows)) + '\n{"phase":', encoding='utf-8')
            return {'timeout_seconds': 900, 'timed_out': True, 'exit_code': 1,
                    'launcher_pid': 101, 'elapsed_seconds': 900.1}

        with patch.object(sys, 'argv', arguments), \
                patch.object(entry, 'gui_modules', return_value=['fixture_gui']), \
                patch.object(entry, 'run_child', side_effect=child) as run, \
                patch('utils.build_info.source_digest', return_value='fixture-digest'), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(entry.main(), 1)
        self.assertEqual(run.call_count, 2)
        full = json.loads((evidence / 'unit-results.json').read_text('utf-8'))
        self.assertFalse(full['passed'])
        self.assertEqual(full['unstarted_groups'], ['fixture_gui'])
        self.assertGreaterEqual(full['elapsed_seconds'], 0)
        partial = full['groups'][1]
        self.assertFalse(partial['report_complete'])
        self.assertEqual((partial['tests'], partial['failures'], partial['runner_errors']), (1, 1, 1))
        self.assertEqual(partial['timing']['last_test'], 'fixture.pending')
        self.assertEqual(partial['timing']['last_position']['phase'], 'test_start')

    def test_real_owned_child_reports_timeout_and_exit_status(self):
        with (self.root / 'stdout.log').open('wb') as stdout, (self.root / 'stderr.log').open('wb') as stderr:
            child = entry.run_child([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                                    stdout=stdout, stderr=stderr, timeout=0.4)
        self.assertTrue(child['timed_out'])
        self.assertIsInstance(child['launcher_pid'], int)
        self.assertIsInstance(child['exit_code'], int)
        self.assertNotEqual(child['exit_code'], 0)
