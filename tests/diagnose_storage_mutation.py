"""Replay two immutable unittest methods on a candidate scanner and retain each observation."""

import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
import ctypes
from functools import wraps
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
from time import perf_counter_ns
import traceback
from types import ModuleType
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import media_storage_scan
from core.storage_identity import identity_for as read_path_identity

ORIGINAL_SHA = 'b48455a28f87f4ae198debe42df996d34ddee9d9'
DIAGNOSTIC_REF = 'refs/heads/diag/storage-content-integrity-20261111'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
PATH_OPEN = Path.open


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=30, creationflags=NO_WINDOW)


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def original_specs():
    initial_large = sha256(b'a' * 3_000_000)
    mutated_large = sha256(b'a' * 2_000_000 + b'X' + b'a' * 999_999)
    initial_small = sha256(b'real duplicated content')
    mutated_small = sha256(b'real duplicated contenX')
    return (
        dict(module='test_storage_scan', klass='StorageScanTests',
             method='test_hash_phase_change_is_excluded', expected_scans=1,
             files={'a.mp4': (initial_large, mutated_large), 'b.mp4': (initial_large, initial_large)}),
        dict(module='test_storage_scan_batches', klass='StorageScanBatchTests',
             method='test_file_changed_while_buffered_is_not_a_cache_hit', expected_scans=3,
             files={f'sample-{index}.mp4': (initial_small, mutated_small if index == 0 else initial_small)
                    for index in range(7)}),
    )


def load_original(spec):
    module = ModuleType('tests.' + spec['module'])
    source_path = 'tests/' + spec['module'] + '.py'
    module.__file__ = ORIGINAL_SHA + ':' + source_path
    source = git('show', module.__file__)
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    klass = getattr(module, spec['klass'])
    return klass, dict(path=source_path, sha256=sha256(source), fixture=spec)


def filesystem(root):
    if os.name != 'nt':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    name = ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumeInformationW(ctypes.c_wchar_p(root.anchor), None, 0,
                                        None, None, None, name, len(name)):
        raise ctypes.WinError(ctypes.get_last_error())
    return name.value


def require_ci(checkout, roots, rounds):
    expected = {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'Windows', 'GITHUB_REPOSITORY': 'xlxs123/MediaFlow',
                'GITHUB_REF': DIAGNOSTIC_REF, 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': checkout}
    for key, value in expected.items():
        if os.environ.get(key) != value:
            raise RuntimeError(f'The diagnostic requires {key}={value}.')
    if (os.name != 'nt' or sys.getwindowsversion().build != 20348 or rounds < 54
            or [root['drive'].upper() for root in roots] != ['C:', 'D:']
            or any(root['filesystem'] != 'NTFS' for root in roots)
            or Path(roots[1]['path']) != Path(os.environ['RUNNER_TEMP']).resolve(strict=True)):
        raise RuntimeError('Expected Windows Server 2022, distinct canonical C/D NTFS roots and 54 traced rounds.')


def scan_summary(result):
    return dict(complete=result['complete'], cancelled=result['cancelled'],
                cache_hits=result['cache_hits'], hashed_files=result['hashed_files'],
                groups=[dict(sha256=group['sha256'], members=[Path(row['path']).name for row in group['files']])
                        for group in result['groups']],
                files=[dict(file=Path(row['path']).name, status=row['status'], sha256=row['sha256'],
                            identity=row['identity']) for row in result['files']])


@contextmanager
def capture_scans(scans, events, traced):
    scanner = media_storage_scan.StorageScanner
    scan = scanner.scan

    def log(event, **fields):
        events.append(dict(event=event, ns=perf_counter_ns(), thread=threading.get_native_id(), **fields))

    def scan_event(instance, entries, *args, progress=None, **kwargs):
        if traced:
            log('scan_start')

        def notify(value):
            fields = dict(phase=value['phase'], files_done=value['files_done'],
                          file=Path(value['path']).name, bytes_done=value['bytes_done'])
            log('progress_start', **fields)
            if progress is not None:
                progress(value)
            log('progress_end', **fields)

        result = scan(instance, entries, *args, progress=notify if traced else progress, **kwargs)
        scans.append(scan_summary(result))
        if traced:
            log('scan_end', scan_index=len(scans) - 1)
        return result

    def path_identity_event(original):
        @wraps(original)
        def observe(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            log('path_identity', file=Path(path).name, identity=value)
            return value
        return observe

    def handle_identity_event(original):
        @wraps(original)
        def observe(*args, **kwargs):
            value = original(*args, **kwargs)
            log('open_handle_stat', identity=value)
            return value
        return observe

    def file_open(path, mode='r', *args, **kwargs):
        if path.suffix.casefold() == '.mp4':
            log('file_open', file=path.name, mode=mode)
        return PATH_OPEN(path, mode, *args, **kwargs)

    path_stat = Path.stat

    def stat_event(path, *args, **kwargs):
        info = path_stat(path, *args, **kwargs)
        if path.suffix.casefold() == '.mp4':
            log('path_stat', file=path.name, device=info.st_dev, index=info.st_ino,
                bytes=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)
        return info

    utime = os.utime

    def set_times(path, *args, **kwargs):
        observe = Path(path).suffix.casefold() == '.mp4'
        if observe:
            log('utime_start', file=Path(path).name, times_ns=kwargs.get('ns'))
        result = utime(path, *args, **kwargs)
        if observe:
            log('utime_end', file=Path(path).name)
        return result

    with ExitStack() as stack:
        stack.enter_context(patch.object(scanner, 'scan', scan_event))
        if traced:
            for name, wrapper in (('identity_for', path_identity_event), ('file_identity', handle_identity_event)):
                original = getattr(media_storage_scan, name, None)
                if original is not None:
                    stack.enter_context(patch.object(media_storage_scan, name, wrapper(original)))
            stack.enter_context(patch.object(Path, 'open', file_open))
            stack.enter_context(patch.object(Path, 'stat', stat_event))
            stack.enter_context(patch.object(os, 'utime', set_times))
        yield


def read_owned_fixture(test, spec, scratch):
    folder = test.root if spec['module'] == 'test_storage_scan' else test.paths[0].parent
    folder = folder.resolve(strict=True)
    if folder.parent != scratch:
        raise RuntimeError('The test did not create its fixture inside the owned round directory.')
    state = []
    for name, (initial_sha, final_sha) in spec['files'].items():
        path = folder / name
        if path.resolve(strict=True) != path or path.is_symlink() or not path.is_file():
            raise RuntimeError(f'Expected the owned regular fixture {name}.')
        with PATH_OPEN(path, 'rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        state.append(dict(file=name, original_fixture_sha256=initial_sha, expected_final_sha256=final_sha,
                          actual_final_sha256=actual, final_identity=read_path_identity(path),
                          matches_expected=actual == final_sha))
    return state


def run_case(klass, spec, traced, scratch):
    method = spec['method']
    test = klass(method)
    original = getattr(test, method)
    scans, events, readback, capture_errors = [], [], [], []
    called = False

    @wraps(original)
    def execute_test():
        nonlocal called
        called = True
        try:
            with capture_scans(scans, events, traced):
                return original()
        finally:
            # The original assertions have finished. Read actual bytes before the original cleanup.
            try:
                readback.extend(read_owned_fixture(test, spec, scratch))
            except Exception:
                capture_errors.append(traceback.format_exc())

    stream = io.StringIO()
    with patch.object(test, method, execute_test):
        start = perf_counter_ns()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(test)
        elapsed = perf_counter_ns() - start
    exercised = called and result.testsRun == 1 and not (result.skipped or result.expectedFailures)
    bytes_verified = len(readback) == len(spec['files']) and all(item['matches_expected'] for item in readback)
    passed = (exercised and result.wasSuccessful() and bytes_verified and not capture_errors
              and len(scans) == spec['expected_scans'])
    return dict(test=test.id(), mode='traced' if traced else 'result_only', elapsed_ns=elapsed,
                tests_run=result.testsRun, executed=exercised, passed=passed,
                assertions_passed=exercised and result.wasSuccessful(), fixture_write_verified=bytes_verified,
                failures=[detail for _, detail in result.failures], errors=[detail for _, detail in result.errors],
                skipped=[reason for _, reason in result.skipped],
                unexpected_successes=[item.id() for item in result.unexpectedSuccesses],
                expected_failures=[detail for _, detail in result.expectedFailures],
                capture_errors=capture_errors, thread=threading.get_native_id(),
                events=events, scans=scans, final_files=readback, unittest_output=stream.getvalue())


def summarise(evidence, requested):
    expected = {(root['label'], test, mode): count for root in evidence['roots'] for test in evidence['test_ids']
                for mode, count in requested.items()}
    totals = Counter((case['root'], case['test'], case['mode']) for case in evidence['cases'])
    rows = []
    for key, count in expected.items():
        root, test, mode = key
        cases = [case for case in evidence['cases'] if (case['root'], case['test'], case['mode']) == key]
        rows.append(dict(root=root, test=test, mode=mode, requested=count, recorded=len(cases),
                         executed=sum(case['executed'] for case in cases), passed=sum(case['passed'] for case in cases),
                         assertion_failures=sum(bool(case['failures']) for case in cases),
                         errors=sum(bool(case['errors'] or case['capture_errors']) for case in cases),
                         incomplete_fixture_writes=sum(not case['fixture_write_verified'] for case in cases)))
    complete = (len(expected) == 8 and totals == expected and not evidence['driver_errors']
                and all(case['executed'] for case in evidence['cases']))
    passed = complete and all(case['passed'] for case in evidence['cases'])
    return dict(complete=complete, passed=passed, planned_cases=4 * sum(requested.values()),
                recorded_cases=len(evidence['cases']), groups=rows)


def collect(args, evidence, journal):
    evidence['checkout'] = git('rev-parse', 'HEAD').decode().strip()
    evidence['scanner_source_sha256'] = sha256(Path(media_storage_scan.__file__).read_bytes())
    for label, root in (('default-canonical', Path(tempfile.gettempdir()).resolve(strict=True)),
                        ('alternate-canonical', args.alternate_temp.resolve(strict=True))):
        evidence['roots'].append(dict(label=label, path=str(root), drive=root.drive, filesystem=filesystem(root)))
    if args.require_windows_ci:
        require_ci(evidence['checkout'], evidence['roots'], args.rounds)
    loaded = []
    for spec in original_specs():
        klass, metadata = load_original(spec)
        evidence['original_test_sources'].append(metadata)
        evidence['test_ids'].append(klass(spec['method']).id())
        loaded.append((spec, klass))
    for root in evidence['roots']:
        with tempfile.TemporaryDirectory(dir=root['path'], prefix='mediaflow-mutation-probe-') as temporary:
            scratch = Path(temporary).resolve(strict=True)
            with patch.object(tempfile, 'tempdir', str(scratch)):
                for spec, klass in loaded:
                    for traced, count in ((False, args.result_only_rounds), (True, args.rounds)):
                        for index in range(count):
                            case = run_case(klass, spec, traced, scratch)
                            case.update(root=root['label'], round=index + 1)
                            evidence['cases'].append(case)
                            journal.write(json.dumps(case, ensure_ascii=False) + '\n')
                            journal.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--alternate-temp', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=54)
    parser.add_argument('--result-only-rounds', type=int, default=6)
    parser.add_argument('--require-windows-ci', action='store_true')
    args = parser.parse_args()
    journal_path = args.output.with_name('rounds.jsonl')
    if (args.output.exists() or journal_path.exists() or args.output == journal_path
            or not 1 <= args.rounds <= 96 or not 1 <= args.result_only_rounds <= 24):
        parser.error('Use fresh evidence files and bounded, nonzero round counts.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    requested = dict(traced=args.rounds, result_only=args.result_only_rounds)
    evidence = dict(schema_version=2, original_tests_revision=ORIGINAL_SHA, requested=requested,
                    fixture_sha256_source='Pinned original input bytes. Final SHA-256 is read independently.',
                    python=sys.version, sqlite=sqlite3.sqlite_version, windows=platform.platform(),
                    runner_image=os.environ.get('ImageVersion'), runner_run_id=os.environ.get('GITHUB_RUN_ID'),
                    roots=[], test_ids=[], original_test_sources=[], cases=[], driver_errors=[])
    with args.output.open('x', encoding='utf-8') as output, journal_path.open('x', encoding='utf-8') as journal:
        try:
            collect(args, evidence, journal)
        except Exception:
            evidence['driver_errors'].append(traceback.format_exc())
        finally:
            evidence['summary'] = summarise(evidence, requested)
            json.dump(evidence, output, ensure_ascii=False, indent=2)
            output.write('\n')
    print(json.dumps(evidence['summary'], ensure_ascii=False))
    return 0 if evidence['summary']['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
