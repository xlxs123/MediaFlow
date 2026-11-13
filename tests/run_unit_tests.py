"""Run every test, isolating the new Qt suite's QApplication process."""
from pathlib import Path
from contextlib import nullcontext
from datetime import datetime, timezone
import io
import json
import os
import re
import subprocess
import sys
from tempfile import mkdtemp
from time import perf_counter
import unittest
import faulthandler
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.process_tree import NO_WINDOW, ProcessTree
from tests.core_test_telemetry import CoreTelemetry, TimedSuite, command_label, timing_summary
from tests.regression_progress import ProgressLog, result_type, unittest_counts

SECURITY_MODULES = ('test_sbom_validation', 'test_workflow_policy')
CORE_TIMEOUT_SECONDS = 1200
OTHER_TIMEOUT_SECONDS = 900


def report_directory():
    if '--report-dir' in sys.argv:
        return Path(sys.argv[sys.argv.index('--report-dir') + 1]).resolve()
    runs = ROOT / 'verification' / 'unit-runs'
    if '--group' in sys.argv:
        runs.mkdir(parents=True, exist_ok=True)
        return Path(mkdtemp(prefix='group-', dir=runs))
    full = runs / 'full'
    full.mkdir(parents=True, exist_ok=True)
    # Sequence directories in launch order, so packaging cannot pick an older
    # success while a newer run is incomplete. mkdir also reserves the number.
    number = max((int(path.name) for path in full.iterdir()
                  if path.name.isascii() and path.name.isdecimal()), default=0)
    while True:
        number += 1
        directory = full / str(number)
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return directory


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def gui_modules():
    return sorted(path.stem for path in (ROOT / 'tests').glob('test_*.py')
        if re.search(r'\bQ(?:Core|Gui)?Application\b', path.read_text('utf-8')))


def progress_path(directory, group):
    return directory / f'unit-{group}-progress.jsonl'


def run_group(group):
    from utils.build_info import source_digest
    started = perf_counter()
    directory = report_directory()
    directory.mkdir(parents=True, exist_ok=True)
    evidence = progress_path(directory, group)
    # Open the exclusive progress file first so an accidental rerun keeps prior evidence.
    with ProgressLog(evidence, ROOT) as progress, \
            (directory / f'unit-{group}-live.txt').open('w', encoding='utf-8') as live, \
            (directory / f'unit-{group}-trace.txt').open('w', encoding='utf-8'):
        progress.event('worker_start', group=group, pid=os.getpid(), parent_pid=os.getppid())
        telemetry = CoreTelemetry(progress)

        class EvidenceStream(io.StringIO):
            def write(self, value):
                live.write(value)
                live.flush()
                return super().write(value)

        stream = EvidenceStream()
        with (telemetry.observe_operations() if group == 'core' else nullcontext()):
            with telemetry.phase('setup', 'source_fingerprint_before'):
                before = source_digest(ROOT)
            with telemetry.phase('setup', 'suite_discovery'):
                if group == 'core':
                    found = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py', top_level_dir=str(ROOT))
                elif group == 'security-tools':
                    found = unittest.defaultTestLoader.loadTestsFromNames(['tests.' + name for name in SECURITY_MODULES])
                else:
                    found = unittest.defaultTestLoader.loadTestsFromName('tests.' + group)
                gui_prefixes = tuple('tests.' + name + '.' for name in gui_modules())
                security_prefixes = tuple('tests.' + name + '.' for name in SECURITY_MODULES)
                suite = TimedSuite((item for item in cases(found)
                    if group != 'core' or not item.id().startswith(gui_prefixes + security_prefixes)), telemetry)
            # Preserve the same isolated update-channel fixture as the focused runner.
            empty = {'schema_version': 1, 'product': 'MediaFlow', 'channel': 'stable',
                     'source_url': '', 'trusted_keys': {}, 'key_state': 'pending'}
            progress.event('suite_start', planned_cases=suite.countTestCases())
            suite_started = perf_counter()
            with (nullcontext() if group == 'security-tools' else
                  patch('core.update_manager.load_channel', return_value=empty)):
                result = unittest.TextTestRunner(stream=stream, verbosity=2,
                    resultclass=result_type(progress, telemetry=telemetry)).run(suite)
            progress.event('suite_end', tests=result.testsRun, passed=result.wasSuccessful(),
                           wall_seconds=round(perf_counter()-suite_started, 6), **unittest_counts(result))
            with telemetry.phase('setup', 'source_fingerprint_after'):
                unchanged = before == source_digest(ROOT)
        # CPython 3.14.0 gh-140815: keep the existing external process deadline
        # and ordinary faulthandler, without scheduling a native traceback watchdog.
        faulthandler.cancel_dump_traceback_later()
    (directory / f'unit-{group}-results.txt').write_text(stream.getvalue(), encoding='utf-8')
    summary = {'passed': result.wasSuccessful(), 'tests': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'skipped': len(result.skipped), 'group': group}
    summary.update(source_sha256=before, source_unchanged=unchanged,
                   elapsed_seconds=round(perf_counter()-started, 6), timing=timing_summary(evidence))
    (directory / f'unit-{group}-results.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    return 0 if result.wasSuccessful() else 1


def run_child(command, *, stdout, stderr, timeout):
    started = perf_counter()
    state = {'timeout_seconds': timeout, 'timed_out': False, 'exit_code': None,
             'launcher_pid': None, 'command': command_label(command)}
    try:
        child = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr,
            env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONFAULTHANDLER='1'),
            creationflags=NO_WINDOW, start_new_session=os.name != 'nt')
    except OSError as error:
        state['launch_error'] = type(error).__name__
    else:
        state['launcher_pid'] = child.pid
        tree = ProcessTree(child)
        state['windows_job_attached'] = bool(tree._job) if os.name == 'nt' else None
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            state['timed_out'] = True
        except OSError as error:
            state['wait_error'] = type(error).__name__
        finally:
            try:
                if child.poll() is None:
                    tree.terminate()
            except (OSError, subprocess.TimeoutExpired) as error:
                state['termination_error'] = type(error).__name__
            finally:
                tree.close()
        state['exit_code'] = child.poll()
    state['elapsed_seconds'] = round(perf_counter()-started, 6)
    return state


def group_summary(directory, group, child):
    path = directory / f'unit-{group}-results.json'
    result = None
    if path.is_file() and not child['timed_out']:
        try:
            result = json.loads(path.read_text('utf-8'))
        except (OSError, ValueError):
            pass  # A worker may end during its final JSON write. Keep the live evidence.
    complete = isinstance(result, dict) and all(name in result for name in (
        'passed', 'tests', 'failures', 'errors', 'skipped'))
    if complete:
        result['passed'] = result['passed'] and child['exit_code'] == 0
    else:
        timing = timing_summary(progress_path(directory, group))
        observed = timing['observed_unittest_counts']
        result = {'group': group, 'passed': False, 'tests': timing['tests_finished'],
                  'failures': observed.get('failures', 0), 'errors': observed.get('errors', 0) + 1,
                  'skipped': observed.get('skipped', 0), 'timing': timing, 'runner_errors': 1}
    result.update(child, report_complete=complete)
    return result


def run_full():
    from utils.build_info import source_digest
    started = perf_counter()
    before = source_digest(ROOT)
    directory = report_directory()
    if '--report-dir' in sys.argv and directory.exists():
        raise RuntimeError('Use a fresh full-regression directory; previous evidence is preserved.')
    directory.mkdir(parents=True, exist_ok=True)
    groups = []
    # Every case still runs. Separate QApplication processes avoid native
    # objects and deferred deletions leaking across independent GUI fixtures.
    expected_groups = ['core', 'security-tools', *gui_modules()]
    with (directory / 'unit-entry-progress.jsonl').open('x', encoding='utf-8', buffering=1) as progress:
        def event(phase, **fields):
            progress.write(json.dumps(dict(phase=phase, utc=datetime.now(timezone.utc).isoformat(),
                                           elapsed_seconds=round(perf_counter()-started, 6),
                                           **fields)) + '\n')
            progress.flush()

        event('entry_start', planned_groups=expected_groups, pid=os.getpid())
        for group in expected_groups:
            timeout = CORE_TIMEOUT_SECONDS if group == 'core' else OTHER_TIMEOUT_SECONDS
            event('group_start', group=group, timeout_seconds=timeout)
            with (directory / f'unit-{group}.stdout.log').open('w', encoding='utf-8') as output, \
                    (directory / f'unit-{group}.stderr.log').open('w', encoding='utf-8') as errors:
                interpreter = (sys.argv[sys.argv.index('--security-python') + 1]
                    if group == 'security-tools' and '--security-python' in sys.argv else sys.executable)
                child = run_child([interpreter, '-B', str(Path(__file__).resolve()), '--group', group,
                                   '--report-dir', str(directory)], stdout=output, stderr=errors, timeout=timeout)
            result = group_summary(directory, group, child)
            groups.append(result)
            event('group_end', group=group, passed=result['passed'], group_seconds=child['elapsed_seconds'],
                  timed_out=child['timed_out'], exit_code=child['exit_code'], launcher_pid=child['launcher_pid'])
            if not result['passed']:
                break
        event('entry_end', started_groups=[group['group'] for group in groups])
    summary = {'passed':len(groups) == len(expected_groups) and all(item['passed'] for item in groups),
        **{name:sum(item[name] for item in groups) for name in ('tests','failures','errors','skipped')},
        'groups':groups,'source_sha256':before,'source_unchanged':before == source_digest(ROOT),
        'elapsed_seconds':round(perf_counter()-started, 6), 'planned_groups':expected_groups,
        'unstarted_groups':expected_groups[len(groups):]}
    (directory / 'unit-results.json').write_text(json.dumps(summary, indent=2), 'utf-8')
    (directory / 'unit-results.txt').write_text('\n'.join(
        (directory / f'unit-{item["group"]}-results.txt').read_text('utf-8') for item in groups
        if (directory / f'unit-{item["group"]}-results.txt').exists()), 'utf-8')
    print(json.dumps(summary))
    return 0 if summary['passed'] else 1


def main():
    if '--group' in sys.argv:
        group = sys.argv[sys.argv.index('--group') + 1]
        return run_group(group) if group in ('core', 'security-tools') or group in gui_modules() else 2
    return run_full()


if __name__ == '__main__':
    raise SystemExit(main())
