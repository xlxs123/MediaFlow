"""Bounded parent driver for the actual packaged public-update acceptance.

The EXE creates report-dir/result.json itself. Its original report is never
rewritten; parent metadata is saved separately. This does not sign, publish,
inject TLS trust, or turn unit process fixtures into public online acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))

from core.process_tree import NO_WINDOW, ProcessTree
from core.update_manifest import MAX_PACKAGE_BYTES, parse_version
from core.update_transport import https_url
from scripts.package_update import _plain, _read_stamp
from scripts.publish_release import _file_hash, _read_document, _save_new


_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SAFE_ENV = {name.casefold() for name in ('SystemRoot', 'SystemDrive', 'WINDIR', 'ComSpec',
    'PATH', 'PATHEXT', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'ProgramData',
    'PUBLIC', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'NUMBER_OF_PROCESSORS',
    'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER', 'PROCESSOR_LEVEL', 'PROCESSOR_REVISION')}
_REQUIRED_CHECKS = {
    'Tampered remote manifest is rejected',
    'Public ZIP actual size and full SHA-256 match signed manifest',
    'Tampered real downloaded ZIP refused before extraction or deployment',
    'Deployed EXE actually runs and reports expected build',
    'Preserved old release is byte-for-byte unchanged',
    'Old database/history/tasks/media/profile fixture bytes unchanged',
    'Actual new GUI launch returned a real successful result',
    'Actual old GUI rollback returned a real successful result',
}


class AcceptanceError(RuntimeError):
    pass


def _overlap(left, right):
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _environment(workspace):
    # A whitelist also strips custom signing-password variable names, PATs,
    # proxy credentials, SSL_CERT_FILE/DIR and all alternate CA-bundle settings.
    environment = {key: value for key, value in os.environ.items() if key.casefold() in _SAFE_ENV}
    for name, relative in (('APPDATA', 'private/Roaming'), ('LOCALAPPDATA', 'private/Local'),
                           ('TEMP', 'private/Temp'), ('TMP', 'private/Temp')):
        path = workspace / relative
        path.mkdir(parents=True, exist_ok=True)
        environment[name] = str(path)
    environment.update(QT_QPA_PLATFORM='offscreen', PYINSTALLER_RESET_ENVIRONMENT='1',
        PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    return environment


def _run_process(command, *, cwd, environment, workspace, timeout):
    started = time.monotonic()
    process = tree = None
    value = {'execution_kind': 'actual_packaged_exe', 'exit_code': None,
             'timed_out': False, 'cancelled': False, 'process_tree_cleaned': False}
    with (workspace / 'stdout.log').open('xb') as stdout, (workspace / 'stderr.log').open('xb') as stderr:
        try:
            process = subprocess.Popen(command, cwd=str(cwd), env=environment,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                shell=False, creationflags=NO_WINDOW, start_new_session=os.name != 'nt')
            tree = ProcessTree(process)
            value['pid'] = process.pid
            while process.poll() is None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    value['timed_out'] = True
                    break
                try:
                    process.wait(timeout=min(.25, remaining))
                except subprocess.TimeoutExpired:
                    pass
        except KeyboardInterrupt:
            value['cancelled'] = True
        finally:
            if tree is not None:
                try:
                    # Job-object termination also closes surviving descendants
                    # after the direct child has exited. Only our tree is owned.
                    tree.terminate()
                    value['process_tree_cleaned'] = True
                finally:
                    tree.close()
            elif process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process is not None:
                value['exit_code'] = process.returncode
            value['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return value


def _public_identity(value):
    return {key: item for key, item in value.items() if key not in ('executable', 'runtime_root')}


def _verify_native(value, *, release, stamp, old, old_stamp, directory, exe_sha256, old_exe_sha256):
    if (value.get('passed') is not True or value.get('public_online_verified') is not True
            or value.get('scope') != 'real_public_https_packaged_client'):
        raise AcceptanceError('实际 EXE 的原始公网验收报告未通过，不能仅凭退出码宣布成功。')
    identity = value.get('runtime_identity')
    if (type(identity) is not dict or _public_identity(identity) != stamp
            or Path(identity.get('executable', '')).resolve() != release / 'MediaFlow.exe'):
        raise AcceptanceError('验收报告的实际运行 EXE 路径、版本、build 或运行源码摘要不匹配。')
    context = value.get('installation_context', {})
    if (context.get('version') != old_stamp['app_version'] or context.get('build_id') != old_stamp['build_id']):
        raise AcceptanceError('原始报告的旧安装上下文与明确指定的旧版本不一致。')
    checks = value.get('checks')
    if (type(checks) is not list or not checks or any(type(item) is not dict
            or item.get('passed') is not True or not isinstance(item.get('description'), str) for item in checks)):
        raise AcceptanceError('原始报告存在失败、缺失或无效的逐项检查。')
    descriptions = {item['description'] for item in checks}
    if not _REQUIRED_CHECKS <= descriptions:
        raise AcceptanceError('原始报告缺少实际下载、篡改拒绝、健康检查、GUI 回退或旧数据保留证据。')
    for version in (stamp['app_version'], old_stamp['app_version']):
        for description in ('Actual GUI stays running and initializes private media library',
                            'Launched GUI media library is readable'):
            if not any(item['description'] == description and item.get('version') == version for item in checks):
                raise AcceptanceError('新旧两个实际 GUI 的运行及媒体库检查证据不完整。')
    public = value.get('public_manifest', {})
    if public.get('version') != stamp['app_version'] or public.get('build_id') != stamp['build_id']:
        raise AcceptanceError('实际公网清单与待验收版本或 build 不一致。')
    source = public.get('source')
    channel = stamp.get('update_channel', {})
    if (not isinstance(source, str) or https_url(source) != source
            or channel.get('key_state') != 'initialized'
            or public.get('key_id') not in channel.get('key_fingerprints', {})
            or hashlib.sha256(source.encode('utf-8')).hexdigest() != channel.get('source_sha256')):
        raise AcceptanceError('实际公网来源与已验收 EXE 的内置更新渠道不一致。')
    package = public.get('package')
    if (type(package) is not dict or set(package) != {'format', 'url', 'size', 'sha256'}
            or package.get('format') != 'zip' or type(package.get('size')) is not int
            or not 0 < package['size'] <= MAX_PACKAGE_BYTES
            or not isinstance(package.get('sha256'), str) or not _HASH.fullmatch(package['sha256'])
            or not isinstance(package.get('url'), str) or https_url(package['url']) != package['url']):
        raise AcceptanceError('实际公网发布包的格式、HTTPS 地址、大小或摘要无效。')
    operation = value.get('operation', {})
    if (operation.get('state') != 'healthy' or operation.get('authenticated') is not True
            or operation.get('version') != stamp['app_version'] or operation.get('build_id') != stamp['build_id']
            or operation.get('package_size') != package['size'] or operation.get('package_sha256') != package['sha256']
            or operation.get('origin_version') != old_stamp['app_version']
            or operation.get('origin_sha256') != old_exe_sha256
            or Path(operation.get('current_path', '')).resolve() != old):
        raise AcceptanceError('实际部署结果与公网包、旧版本 EXE 或安装上下文不一致。')
    target = _plain(operation.get('target_path', '')).resolve()
    if not target.is_relative_to(directory) or _overlap(target, release) or _overlap(target, old):
        raise AcceptanceError('实际新版本必须部署到本次验收私有目录，不能覆盖新旧输入目录。')
    health = operation.get('health', {})
    if (health.get('passed') is not True or _public_identity(health.get('identity', {})) != stamp
            or Path(health.get('identity', {}).get('executable', '')).resolve() != target / 'MediaFlow.exe'):
        raise AcceptanceError('实际部署 EXE 的健康检查运行身份与输入 build 不一致。')
    launched = value.get('launched_processes')
    if type(launched) is not list:
        raise AcceptanceError('原始报告缺少实际新旧 GUI 启动记录。')
    for version, executable in ((stamp['app_version'], target / 'MediaFlow.exe'),
                                (old_stamp['app_version'], old / 'MediaFlow.exe')):
        if not any(type(item) is dict and item.get('version') == version
                and item.get('library_readable') is True and type(item.get('pid')) is int and item['pid'] > 0
                and Path(item.get('path', '')).resolve() == executable for item in launched):
            raise AcceptanceError('原始报告未证明指定的新旧 GUI 分别实际启动且媒体库可读取。')
    if _file_hash(target / 'MediaFlow.exe') != exe_sha256:
        raise AcceptanceError('实际下载、部署及启动的新 EXE 字节与待验收 EXE 不一致。')
    return {'checks': len(checks), 'new_gui_verified': True, 'old_gui_verified': True,
            'public_package': dict(package)}


def run_acceptance(release_dir, old_release_dir, expected_version, report_dir, *, timeout=2400):
    """Run one real EXE; preserve its original result and every owned log."""
    started = time.monotonic()
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 2400:
        raise AcceptanceError('父进程总超时必须为 0–2400 秒之间的有限正数。')
    release, old, directory = (_plain(path).resolve() for path in (release_dir, old_release_dir, report_dir))
    if (not release.is_dir() or not old.is_dir() or directory.exists() or not directory.parent.is_dir()
            or _overlap(release, old) or _overlap(directory, release) or _overlap(directory, old)):
        raise AcceptanceError('需要独立的新旧完整发布目录，以及发布目录外尚不存在的验收目录。')
    stamp, old_stamp = _read_stamp(release), _read_stamp(old)
    if stamp['app_version'] != expected_version or parse_version(old_stamp['app_version']) >= parse_version(expected_version):
        raise AcceptanceError('实际输入版本必须匹配 expected-version，且旧安装上下文版本严格更低。')
    executable, old_executable = release / 'MediaFlow.exe', old / 'MediaFlow.exe'
    exe_sha256, old_exe_sha256 = _file_hash(executable), _file_hash(old_executable)
    workspace = directory.parent / (directory.name + '-driver-' + uuid.uuid4().hex)
    workspace.mkdir()
    environment = _environment(workspace)
    runner = {}
    summary = {'schema_version': 1, 'kind': 'MediaFlow.packaged-public-update-driver',
        'passed': False, 'public_online_verified': False, 'scope': 'actual_packaged_public_https_acceptance',
        'started_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'expected_version': expected_version, 'version': stamp['app_version'],
        'build_id': stamp['build_id'], 'source_sha256': stamp['source_sha256'],
        'exe_sha256': exe_sha256, 'old_version': old_stamp['app_version'],
        'old_exe_sha256': old_exe_sha256, 'runtime_identity_bound': False,
        'driver_logs': workspace.name}
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise AcceptanceError('启动前已达到父进程总超时，未运行 EXE。')
        # report-dir deliberately remains absent until the native harness creates it.
        runner = _run_process([str(executable), '--update-public-check', str(directory),
            '--update-old-release', str(old)], cwd=release, environment=environment,
            workspace=workspace, timeout=remaining)
        kind = runner.get('execution_kind')
        if kind not in ('actual_packaged_exe', 'unit_process_fixture'):
            raise AcceptanceError('父驱动未记录明确的真实进程执行范围。')
        if kind == 'unit_process_fixture':
            summary['scope'] = 'unit_process_fixture'
        if (runner.get('exit_code') != 0 or runner.get('timed_out') is not False
                or runner.get('cancelled') is not False or runner.get('process_tree_cleaned') is not True):
            raise AcceptanceError('实际验收进程失败、取消、超时或无法完成本进程树清理。')
        if (_file_hash(executable) != exe_sha256 or _file_hash(old_executable) != old_exe_sha256
                or _read_stamp(release) != stamp or _read_stamp(old) != old_stamp):
            raise AcceptanceError('验收期间新旧输入 EXE 或构建身份发生变化，不能绑定该结果。')
        value, result_sha256, result_size = _read_document(directory / 'result.json', 32 * 1024 * 1024)
        verified = _verify_native(value, release=release, stamp=stamp, old=old, old_stamp=old_stamp,
            directory=directory, exe_sha256=exe_sha256, old_exe_sha256=old_exe_sha256)
        summary.update(verified, passed=True, runtime_identity_bound=True,
            public_online_verified=kind == 'actual_packaged_exe',
            scope='unit_process_fixture' if kind == 'unit_process_fixture' else summary['scope'],
            native_result={'sha256': result_sha256, 'size': result_size})
    except Exception as error:
        summary.update(error_type=type(error).__name__,
            message=str(error) if isinstance(error, AcceptanceError)
                else '原生验收报告、进程或文件绑定无法验证；保留原始结果和日志，未宣布成功。')
    finally:
        summary['elapsed_seconds'] = round(time.monotonic() - started, 3)
        summary['runner'] = runner
        data = (json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        _save_new(workspace / 'summary.json', data)
        # On an early child failure, create only the parent summary folder after
        # the process has stopped. Do not invent a native result.json.
        _plain(directory)
        if not directory.exists():
            directory.mkdir()
        _save_new(directory / 'summary.json', data)
        _save_new(directory / 'runner.json', (json.dumps(runner, indent=2) + '\n').encode('utf-8'))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('release-dir', 'old-release-dir', 'expected-version', 'report-dir'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=float, default=2400)
    options = vars(parser.parse_args(argv))
    try:
        result = run_acceptance(**options)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['passed'] else 1
    except Exception as error:
        print(json.dumps({'passed': False, 'public_online_verified': False,
            'error_type': type(error).__name__, 'message': str(error) if isinstance(error, AcceptanceError)
                else '验收驱动无法启动或保存结果；检查新目录和输入发布文件。'}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
