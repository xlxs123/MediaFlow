"""Live progress survives interruption without exposing test locals."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from core.process_tree import ProcessTree, NO_WINDOW
from tests.regression_progress import ProgressLog, descendants, wait_for_descendants_exit, progress_summary, result_type


class RegressionProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-live-test-progress-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'progress.jsonl'

    def test_start_is_readable_before_completion_and_result_codes_are_explicit(self):
        outer = self
        progress = ProgressLog(self.path, Path(__file__).resolve().parents[1])
        class Cases(unittest.TestCase):
            def test_case(self):
                rows = [json.loads(line) for line in outer.path.read_text('utf-8').splitlines()]
                outer.assertEqual(rows[-1]['phase'], 'test_start')
                outer.assertEqual(progress_summary(outer.path)['tests_finished'], 0)
        try:
            result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=result_type(progress)).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
            self.assertTrue(result.wasSuccessful())
        finally:
            progress.close()
        summary = progress_summary(self.path)
        self.assertEqual((summary['tests_started'], summary['tests_finished']), (1, 1))
        last = json.loads(self.path.read_text('utf-8').splitlines()[-1])
        self.assertEqual(last['outcome'], 'passed'); self.assertEqual(last['result_code'], 0)
        self.assertFalse(progress.thread.is_alive())

    def test_failure_subtest_and_skip_do_not_become_passed_progress(self):
        progress = ProgressLog(self.path, self.root)
        class Cases(unittest.TestCase):
            def test_failure(self):
                with self.subTest(): self.fail('synthetic negative assertion')
            def test_skip(self):
                self.skipTest('synthetic diagnostic fixture')
        try:
            result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=result_type(progress)).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
            self.assertFalse(result.wasSuccessful())
        finally:
            progress.close()
        rows = [json.loads(line) for line in self.path.read_text('utf-8').splitlines() if json.loads(line)['phase'] == 'test_end']
        self.assertEqual([(r['outcome'], r['result_code']) for r in rows], [('failure', 1), ('skipped', None)])

    def test_later_skipped_subtest_cannot_hide_an_earlier_failure(self):
        progress = ProgressLog(self.path, self.root)
        class Cases(unittest.TestCase):
            def test_mixed(self):
                with self.subTest(phase='failure'): self.fail('synthetic negative assertion')
                with self.subTest(phase='skip'): self.skipTest('synthetic diagnostic fixture')
        try:
            result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=result_type(progress)).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
            self.assertFalse(result.wasSuccessful())
        finally:
            progress.close()
        last = json.loads(self.path.read_text('utf-8').splitlines()[-1])
        self.assertEqual((last['outcome'], last['result_code']), ('failure', 1))

    def test_heartbeat_records_stack_without_values_or_absolute_host_paths(self):
        progress = ProgressLog(self.path, Path(__file__).resolve().parents[1], interval=.01)
        local_value = 'SYNTHETIC_DIAGNOSTIC_PRIVACY_CANARY'
        try:
            progress.current = self.id()
            time.sleep(.07)
        finally:
            progress.close()
        raw = self.path.read_text('utf-8')
        self.assertNotIn(local_value, raw)
        self.assertNotIn(str(self.root), raw)
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertTrue(any(r['phase'] == 'heartbeat' and r['stack'] for r in rows))
        self.assertFalse(progress.thread.is_alive())

    def test_partial_final_line_preserves_the_last_started_unfinished_case(self):
        self.path.write_text('{"phase":"test_start","test":"synthetic.pending"}\n{"phase":', encoding='utf-8')
        self.assertEqual(progress_summary(self.path), dict(tests_started=1, tests_finished=0, last_test='synthetic.pending'))

    def test_old_progress_cannot_be_overwritten(self):
        self.path.write_text('retained previous evidence', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            ProgressLog(self.path, self.root)
        self.assertEqual(self.path.read_text('utf-8'), 'retained previous evidence')

    def test_owned_process_tree_is_terminated_and_unrelated_processes_not_reported(self):
        if os.name != 'nt':
            self.assertIsNone(descendants(os.getpid())); return
        marker = self.root / 'child.pid'
        program = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);open(sys.argv[1],"w").write(str(p.pid));time.sleep(60)'
        process = subprocess.Popen([sys.executable, '-c', program, str(marker)], creationflags=NO_WINDOW)
        tree = ProcessTree(process)
        try:
            deadline = time.monotonic()+10
            while not marker.exists() and time.monotonic()<deadline: time.sleep(.02)
            self.assertTrue(marker.exists())
            child_pid = int(marker.read_text('ascii'))
            rows = descendants(process.pid)
            self.assertTrue(any(r['pid'] == child_pid for r in rows))
            self.assertFalse(any(r['pid'] == os.getpid() for r in rows))
            tree.terminate(); process.wait(timeout=10)
        finally:
            tree.close()
            if process.poll() is None: process.wait(timeout=10)
        self.assertEqual(wait_for_descendants_exit(process.pid), [])

    def test_process_teardown_wait_is_bounded_and_keeps_unknown_or_live_results(self):
        child = [dict(pid=987654, parent_pid=123456, executable='synthetic.exe')]
        with patch('tests.regression_progress.descendants', side_effect=[child, []]):
            self.assertEqual(wait_for_descendants_exit(123456, timeout=1), [])
        with patch('tests.regression_progress.descendants', return_value=child):
            self.assertEqual(wait_for_descendants_exit(123456, timeout=0), child)
        with patch('tests.regression_progress.descendants', return_value=None):
            self.assertIsNone(wait_for_descendants_exit(123456))

    def test_windows_process_snapshot_errors_never_mean_an_empty_process_tree(self):
        if os.name != 'nt':
            self.assertIsNone(descendants(os.getpid())); return
        import ctypes
        for stage in ('snapshot', 'first', 'next'):
            kernel = Mock()
            kernel.CreateToolhelp32Snapshot.return_value = ctypes.c_void_p(-1).value if stage == 'snapshot' else 123
            def first(*args):
                if stage == 'first':
                    ctypes.set_last_error(5); return 0
                return 1
            def following(*args):
                ctypes.set_last_error(5); return 0
            kernel.Process32FirstW.side_effect = first
            kernel.Process32NextW.side_effect = following
            with self.subTest(stage=stage), patch('ctypes.WinDLL', return_value=kernel):
                ctypes.set_last_error(5)
                with self.assertRaises(OSError):
                    descendants(os.getpid())
            if stage != 'snapshot':
                kernel.CloseHandle.assert_called_once_with(123)

    def test_group_cannot_pass_when_windows_process_enumeration_is_unavailable(self):
        if os.name != 'nt':
            self.assertIsNone(descendants(os.getpid())); return
        from tests.run_update_tests import run
        from utils.build_info import source_digest
        root = Path(__file__).resolve().parents[1]
        (self.root / 'probe.json').write_text(json.dumps(dict(passed=True, tests=1, failures=0,
            errors=0, skipped=0, source_sha256=source_digest(root))), encoding='utf-8')
        for kind, effect in (('api_error', OSError(5, 'synthetic access denied')), ('unknown', None)):
            process = Mock(pid=987654, returncode=0)
            process.poll.return_value = 0; process.wait.return_value = 0
            with self.subTest(kind=kind), patch('tests.run_update_tests.GROUPS', {'probe': []}), \
                    patch('tests.run_update_tests.subprocess.Popen', return_value=process), \
                    patch('tests.run_update_tests.ProcessTree'), \
                    patch('tests.run_update_tests.wait_for_descendants_exit', side_effect=effect if isinstance(effect, OSError) else None,
                          return_value=None):
                self.assertEqual(run(self.root), 1)
            result = json.loads((self.root / 'results.json').read_text('utf-8'))
            self.assertFalse(result['passed'])
            self.assertIsNone(result['groups'][0]['remaining_owned_children'])
            if isinstance(effect, OSError):
                self.assertEqual(result['groups'][0]['process_enumeration_error']['type'], 'OSError')
