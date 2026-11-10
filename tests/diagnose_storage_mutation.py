"""Collect assertions and metadata around two owned, synthetic scan mutations."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
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
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import core
from core import media_storage_scan
from tests import test_storage_scan, test_storage_scan_batches

BASELINE = '47e2d62f005c323d1e3c1b070ffdff1bae29bdc8'
CASES = (
    (test_storage_scan.StorageScanTests, 'test_hash_phase_change_is_excluded'),
    (test_storage_scan_batches.StorageScanBatchTests, 'test_file_changed_while_buffered_is_not_a_cache_hit'),
)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=30, creationflags=NO_WINDOW)


def filesystem(root):
    if os.name != 'nt':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    name = ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumeInformationW(ctypes.c_wchar_p(root.anchor), None, 0,
                                        None, None, None, name, len(name)):
        raise ctypes.WinError(ctypes.get_last_error())
    return name.value


def ffmpeg_version():
    executable = ROOT / 'tools' / 'ffmpeg.exe'
    if not executable.is_file():
        return None
    result = subprocess.run([str(executable), '-version'], capture_output=True, timeout=30,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    lines = result.stdout.decode('utf-8', errors='replace').splitlines()
    return {'exit_code': result.returncode, 'version': lines[0] if lines else ''}


@contextmanager
def source_variant(baseline):
    if not baseline:
        yield media_storage_scan
        return
    with ExitStack() as stack:
        for stem in ('storage_cache', 'media_storage_scan'):
            name = 'core.' + stem
            filename = 'core/' + stem + '.py'
            module = ModuleType(name)
            module.__file__ = BASELINE + ':' + filename
            stack.enter_context(patch.dict(sys.modules, {name: module}))
            stack.enter_context(patch.object(core, stem, module))
            exec(compile(git('show', BASELINE + ':' + filename), module.__file__, 'exec'), module.__dict__)
        stack.enter_context(patch.object(test_storage_scan, 'StorageScanner', module.StorageScanner))
        yield module


@contextmanager
def trace_scan(module, rows):
    scan, identity = module.StorageScanner.scan, module.identity_for
    opened, utime, handle_identity = Path.open, os.utime, module.file_identity

    def log(kind, **fields):
        rows.append(dict(event=kind, ns=perf_counter_ns(), thread=threading.get_native_id(), **fields))

    def identity_event(path, info=None):
        value = identity(path, info)
        log('path_identity', file=Path(path).name, identity=value)
        return value

    def file_identity_event(info):
        value = handle_identity(info)
        log('open_handle_stat', identity=value)
        return value

    def file_open(path, mode='r', *args, **kwargs):
        if path.suffix == '.mp4':
            log('file_open', file=path.name, mode=mode)
        return opened(path, mode, *args, **kwargs)

    def set_times(path, *args, **kwargs):
        if Path(path).suffix == '.mp4':
            log('utime_start', file=Path(path).name, times_ns=kwargs.get('ns'))
        result = utime(path, *args, **kwargs)
        if Path(path).suffix == '.mp4':
            log('utime_end', file=Path(path).name)
        return result

    def scan_event(scanner, entries, *args, progress=None, **kwargs):
        log('scan_start')

        def notify(value):
            fields = dict(phase=value['phase'], files_done=value['files_done'],
                          file=Path(value['path']).name, bytes_done=value['bytes_done'])
            log('progress_start', **fields)
            if progress is not None:
                progress(value)
            log('progress_end', **fields)

        result = scan(scanner, entries, *args, progress=notify, **kwargs)
        log('scan_end', cache_hits=result['cache_hits'], hashed_files=result['hashed_files'],
            groups=[[Path(item['path']).name for item in group['files']] for group in result['groups']],
            files=[dict(file=Path(item['path']).name, status=item['status'], sha256=item['sha256'])
                   for item in result['files']])
        return result

    with patch.object(module, 'identity_for', identity_event), \
            patch.object(module, 'file_identity', file_identity_event), \
            patch.object(module.StorageScanner, 'scan', scan_event), \
            patch.object(Path, 'open', file_open), patch.object(os, 'utime', set_times):
        yield


def run_case(case, module, traced):
    test = case[0](case[1])
    rows = []
    stream = io.StringIO()
    with ExitStack() as stack:
        if traced:
            stack.enter_context(trace_scan(module, rows))
        started = perf_counter_ns()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(test)
        elapsed = perf_counter_ns() - started
    return dict(test=test.id(), traced=traced, elapsed_ns=elapsed, passed=result.wasSuccessful(),
                failures=[text for _, text in result.failures], errors=[text for _, text in result.errors],
                thread=threading.get_native_id(), events=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--alternate-temp', required=True, type=Path)
    parser.add_argument('--rounds', type=int, default=12)
    parser.add_argument('--traced-rounds', type=int, default=6)
    args = parser.parse_args()
    if args.output.exists() or not 1 <= args.rounds <= 32 or not 1 <= args.traced_rounds <= 16:
        parser.error('Use a new evidence file and bounded, nonzero round counts.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = dict(
        checkout=git('rev-parse', 'HEAD').decode().strip(), baseline=BASELINE,
        python=sys.version, sqlite=sqlite3.sqlite_version, windows=platform.platform(),
        runner_image=os.environ.get('ImageVersion'), ffmpeg=ffmpeg_version(),
        roots=[], cases=[])
    for label, root in (('default-canonical', Path(tempfile.gettempdir()).resolve(strict=True)),
                        ('alternate-canonical', args.alternate_temp.resolve(strict=True))):
        evidence['roots'].append(dict(label=label, path=str(root), filesystem=filesystem(root)))
        with tempfile.TemporaryDirectory(dir=root, prefix='mediaflow-mutation-probe-') as scratch, \
                patch.object(tempfile, 'tempdir', scratch):
            for baseline in (False, True):
                with source_variant(baseline) as module:
                    for case in CASES[:1] if baseline else CASES:
                        for traced, repeat in ((False, args.rounds), (True, args.traced_rounds)):
                            for round_index in range(repeat):
                                row = run_case(case, module, traced)
                                evidence['cases'].append(dict(root=label, variant='baseline' if baseline else 'optimized',
                                                              round=round_index+1, **row))
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(evidence, output, ensure_ascii=False, indent=2)
        output.write('\n')
    print(json.dumps({'cases': len(evidence['cases']),
                      'assertion_failures': sum(not row['passed'] for row in evidence['cases'])}))


if __name__ == '__main__':
    main()
