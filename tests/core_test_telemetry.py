"""Scope-bound test timings and disclosure-safe child process observations."""
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from functools import wraps
import hashlib
import ntpath
from pathlib import Path, PurePath
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from weakref import WeakKeyDictionary

from tests.regression_progress import count_delta, progress_events, unittest_counts


_PROGRAMS = {
    'python', 'pythonw', 'git', 'gitleaks', 'pwsh', 'powershell', 'ffmpeg', 'ffprobe',
    'dotnet', 'csc', 'cmd', 'node', 'curl', 'signtool', 'taskkill', 'yt-dlp',
}
_VERBS = {
    'git': {'version', '--version', 'rev-parse', 'status', 'log', 'diff', 'ls-tree', 'rev-list',
            'ls-files', 'cat-file', 'init', 'config', 'add', 'commit', 'checkout', 'switch',
            'tag', 'merge', 'show', 'update-index'},
    'gitleaks': {'git', 'dir', 'version'},
}
_POWERSHELL_SCRIPTS = {
    'authenticode.ps1', 'build.ps1', 'build-recycle.ps1', 'build-webview2.ps1',
    'publisher_windows_vault.ps1', 'release_mediaflow.ps1',
}


def _basename(value):
    return ntpath.basename(str(value)).casefold() if isinstance(value, (str, PurePath)) else ''


def command_label(command):
    if not isinstance(command, (tuple, list)) or not command:
        return 'command-string'
    name = _basename(command[0]).removesuffix('.exe')
    if name not in _PROGRAMS:
        return 'other'
    if name in _VERBS and len(command) > 1 and isinstance(command[1], str) and command[1] in _VERBS[name]:
        return f'{name}:{command[1]}'
    if name in {'pwsh', 'powershell'}:
        for flag, value in zip(command[1:], command[2:]):
            if isinstance(flag, str) and flag.casefold() == '-file':
                script = _basename(value)
                return f'{name}:{script}' if script in _POWERSHELL_SCRIPTS else name
    return name


def seconds(ns):
    return round(ns / 1_000_000_000, 7)


class CoreTelemetry:
    """Count selected outermost operations on the suite's calling thread."""
    def __init__(self, progress):
        self.progress = progress
        self.thread = threading.get_ident()
        self.active = None
        self.depth = 0

    def start_case(self, label, test=None):
        self.active = (label, time.process_time_ns(), time.thread_time_ns(),
                       defaultdict(lambda: [0, 0]))
        self.case_fixtures = ExitStack()
        if test is not None:
            for method, name in (('_callSetUp', 'setUp'), ('_callTearDown', 'tearDown'),
                                 ('doCleanups', 'cleanups')):
                original = getattr(test, method)
                self.case_fixtures.enter_context(patch.object(
                    test, method, self.timed(original, 'fixture.' + name)))

    def finish_case(self):
        _, cpu, thread_cpu, operations = self.active
        self.case_fixtures.close()
        self.active = None
        return {
            'process_cpu_seconds': seconds(time.process_time_ns() - cpu),
            'main_thread_cpu_seconds': seconds(time.thread_time_ns() - thread_cpu),
            'observed_operations': {
                name: {'calls': calls, 'wall_seconds': seconds(wall)}
                for name, (calls, wall) in sorted(operations.items())
            },
        }

    @contextmanager
    def phase(self, kind, label, result=None):
        if self.active is not None:
            # A module transition can also invoke the previous module's teardown.
            yield
            return
        prior = unittest_counts(result) if result is not None else None
        started = time.perf_counter_ns()
        self.start_case(label)
        self.progress.current = label
        self.progress.event(f'{kind}_start', label=label)
        try:
            yield
        finally:
            metrics = self.finish_case()
            if result is not None:
                metrics['unittest_count_delta'] = count_delta(prior, result)
            self.progress.event(f'{kind}_end', label=label,
                                wall_seconds=seconds(time.perf_counter_ns() - started), **metrics)
            self.progress.current = None

    def observing(self):
        return self.active is not None and not self.depth and threading.get_ident() == self.thread

    def operation(self, label, method, *args, **kwargs):
        if not self.observing():
            return method(*args, **kwargs)
        self.depth += 1
        started = time.perf_counter_ns()
        try:
            return method(*args, **kwargs)
        finally:
            values = self.active[3][label]
            values[0] += 1
            values[1] += time.perf_counter_ns() - started
            self.depth -= 1

    def timed(self, method, label):
        @wraps(method)
        def observe(*args, **kwargs):
            return self.operation(label, method, *args, **kwargs)
        return observe

    @contextmanager
    def observe_operations(self):
        # These patches exist only in the isolated Core test worker while it runs the suite.
        processes = WeakKeyDictionary()
        popen = subprocess.Popen
        launch = popen.__init__

        @wraps(launch)
        def start(process, args, *rest, **kwargs):
            if not self.observing():
                return launch(process, args, *rest, **kwargs)
            label = 'shell' if kwargs.get('shell') else command_label(args)
            self.operation('child.launch:' + label, launch, process, args, *rest, **kwargs)
            processes[process] = label
            self.progress.event('child_launch', label=self.active[0], command=label, pid=process.pid)

        def child_wait(method):
            @wraps(method)
            def wait(process, *args, **kwargs):
                if not self.observing():
                    return method(process, *args, **kwargs)
                label = processes.get(process, 'other')
                try:
                    return self.operation('child.wait:' + label, method, process, *args, **kwargs)
                finally:
                    if process.returncode is not None and process in processes:
                        self.progress.event('child_exit', label=self.active[0], command=label,
                                            pid=process.pid, exit_code=process.returncode)
                        del processes[process]
            return wait

        observed = (
            (Path, ('stat', 'open', 'mkdir', 'read_bytes', 'write_bytes', 'read_text', 'write_text'), 'path'),
            (shutil, ('copyfile', 'copytree', 'rmtree'), 'shutil'),
            (hashlib, ('file_digest',), 'hashlib'),
            (time, ('sleep',), 'thread'),
            (threading.Thread, ('join',), 'thread'),
            (threading.Event, ('wait',), 'event'),
            (threading.Condition, ('wait',), 'condition'),
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(popen, '__init__', start))
            for name in ('wait', 'communicate'):
                stack.enter_context(patch.object(popen, name, child_wait(getattr(popen, name))))
            for owner, names, category in observed:
                for name in names:
                    stack.enter_context(patch.object(owner, name,
                                                    self.timed(getattr(owner, name), f'{category}.{name}')))
            yield


class TimedSuite(unittest.TestSuite):
    def __init__(self, tests, telemetry):
        super().__init__(tests)
        self.telemetry = telemetry

    @staticmethod
    def _class_name(cls):
        return f'{cls.__module__}.{cls.__qualname__}'

    def _handleClassSetUp(self, test, result):
        cls = type(test)
        if getattr(result, '_previousTestClass', None) is cls:
            return super()._handleClassSetUp(test, result)
        with self.telemetry.phase('fixture', self._class_name(cls) + '::setUpClass', result):
            return super()._handleClassSetUp(test, result)

    def _tearDownPreviousClass(self, test, result):
        cls = getattr(result, '_previousTestClass', None)
        if cls is None or (test is not None and type(test) is cls):
            return super()._tearDownPreviousClass(test, result)
        with self.telemetry.phase('fixture', self._class_name(cls) + '::tearDownClass', result):
            return super()._tearDownPreviousClass(test, result)

    def _handleModuleFixture(self, test, result):
        previous = getattr(result, '_previousTestClass', None)
        module = type(test).__module__
        if previous is not None and previous.__module__ == module:
            return super()._handleModuleFixture(test, result)
        with self.telemetry.phase('fixture', module + '::module_transition', result):
            return super()._handleModuleFixture(test, result)

    def _handleModuleTearDown(self, result):
        previous = getattr(result, '_previousTestClass', None)
        if previous is None:
            return super()._handleModuleTearDown(result)
        with self.telemetry.phase('fixture', previous.__module__ + '::tearDownModule', result):
            return super()._handleModuleTearDown(result)


def timing_summary(path):
    """Summarize completed events without inferring a successful suite."""
    started, completed, fixtures, observations = [], [], [], Counter()
    operations = defaultdict(lambda: {'calls': 0, 'wall_seconds': 0.0})
    phase_seconds = Counter()
    last_position = suite_end = worker_pid = planned_cases = None
    for row in progress_events(path):
        phase = row.get('phase')
        if phase == 'worker_start':
            worker_pid = row['pid']
        elif phase == 'suite_start':
            planned_cases = row['planned_cases']
        elif phase == 'test_start':
            started.append(row)
        elif phase == 'test_end':
            completed.append(row)
        elif phase == 'fixture_end':
            fixtures.append(row)
        elif phase == 'suite_end':
            suite_end = row
        if phase in {'test_start', 'test_end', 'fixture_start', 'fixture_end', 'setup_start', 'setup_end'}:
            last_position = row
        if phase in {'test_end', 'fixture_end', 'setup_end'}:
            phase_seconds[phase.removesuffix('_end')] += row.get('case_seconds', row.get('wall_seconds', 0.0))
            observations.update(row.get('unittest_count_delta', {}))
            for label, timing in row.get('observed_operations', {}).items():
                operations[label]['calls'] += timing['calls']
                operations[label]['wall_seconds'] += timing['wall_seconds']
    return {
        'worker_pid': worker_pid, 'planned_cases': planned_cases,
        'tests_started': len(started), 'tests_finished': len(completed),
        'last_test': started[-1]['test'] if started else None,
        'last_position': last_position,
        'observed_unittest_counts': dict(observations),
        'suite_finished': suite_end is not None,
        'suite_seconds': suite_end.get('wall_seconds') if suite_end else None,
        'phase_seconds': {key: round(value, 6) for key, value in phase_seconds.items()},
        'slowest_tests': sorted(completed, key=lambda row: row['case_seconds'], reverse=True)[:20],
        'slowest_fixtures': sorted(fixtures, key=lambda row: row['wall_seconds'], reverse=True)[:10],
        'observed_operations': {label: dict(calls=row['calls'], wall_seconds=round(row['wall_seconds'], 6))
                                for label, row in sorted(operations.items())},
    }
