"""有限的启动自检；既有数据只读，所有 Store 初始化都在私有临时目录。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

from utils.build_info import runtime_identity
from utils.config import ConfigStore


class HealthCheckError(RuntimeError):
    pass


def _plain_path(path):
    """Reject redirects before opening or creating data/report files."""
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise HealthCheckError('路径包含符号链接或目录联接，未执行健康检查。')
    return path


@contextmanager
def _data_lease(directory):
    """Match the application's byte lock without writing an existing lock file."""
    path = directory / '.mediaflow-data.lock'
    _plain_path(path)
    try:
        handle = path.open('x+b')
        handle.write(b'\0')
        handle.flush()
    except FileExistsError:
        handle = path.open('rb')
    try:
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise HealthCheckError('用户数据正被其他 MediaFlow 实例或迁移占用，请关闭该实例后重试。') from None
        yield
    finally:
        handle.close()


def _snapshot_files(paths, target, limit):
    from core.data_migration import MigrationError, frozen_file
    with ExitStack() as stack:
        opened = []
        for path in paths:
            _plain_path(path)
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            if not path.is_file() or info.st_size > limit:
                raise HealthCheckError('数据文件不是普通文件或超过健康检查读取上限；原文件已保留。')
            try:
                source = stack.enter_context(frozen_file(path))
            except MigrationError:
                raise HealthCheckError('数据文件正在使用或无法读取，请关闭其他 MediaFlow 实例并检查权限后重试；原文件未修改。') from None
            opened.append((path, source))
        for path, source in opened:
            with (target / path.name).open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)


def _database(path, private, kind):
    from core.media_history import _TASK_COLUMNS, _FILE_COLUMNS
    application, required = ((0x4D464831, {
        'tasks': ('task_key', *_TASK_COLUMNS, 'removed'),
        'entries': ('id', 'task_key', 'slot', 'file_name', *_FILE_COLUMNS, 'custom_fields'),
        'tombstones': ('task_key', 'slot'),
    }) if kind == 'history' else (0x4D464254, {
        'batches': ('id', 'metadata'),
        'items': ('id', 'batch_id', 'ordinal', 'url_hash', 'url_cipher', 'metadata'),
    }))
    wal = path.with_name(path.name + '-wal')
    journal = path.with_name(path.name + '-journal')
    if journal.exists():
        raise HealthCheckError('数据库存在待恢复事务日志；请先使用原版本完成恢复，未修改原数据库。')
    if not path.exists():
        if wal.exists():
            raise HealthCheckError('数据库主文件缺失但存在 WAL，未尝试恢复或删除。')
        return {'present': False, 'records': 0}
    target = private / kind
    target.mkdir()
    _snapshot_files((path, wal), target, 1024 ** 3)
    snapshot = target / path.name
    with closing(sqlite3.connect(snapshot, timeout=1.0)) as database:
        # A copied WAL may be recovered here. The real database is never opened by SQLite.
        database.execute('PRAGMA trusted_schema=OFF')
        if (database.execute('PRAGMA application_id').fetchone()[0] != application
                or database.execute('PRAGMA user_version').fetchone()[0] != 1):
            raise HealthCheckError('数据库产品标识或结构版本不兼容；请使用数据迁移预览处理。')
        if database.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise HealthCheckError('数据库完整性检查失败；原数据库和备份均未修改。')
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, columns in required.items():
            if table not in tables or not set(columns) <= {
                    row[1] for row in database.execute('PRAGMA table_info(' + table + ')')}:
                raise HealthCheckError('数据库缺少必要表或字段，不能正常打开；原数据库未修改。')
        if database.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise HealthCheckError('数据库记录关联不完整；原数据库未修改。')
        counts = {table: database.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in required}
        if kind == 'history':
            optional = {'organization_transactions': ('transaction_id', 'digest', 'committed'),
                        'storage_cleanup_transactions': ('transaction_id', 'digest'),
                        'migration_visibility': ('entry_id', 'library', 'history')}
            for table, columns in optional.items():
                if table in tables and not set(columns) <= {
                        row[1] for row in database.execute('PRAGMA table_info(' + table + ')')}:
                    raise HealthCheckError('媒体库附属记录结构损坏，原数据库未修改。')
        return {'present': True, 'records': counts.get('entries', counts.get('items', 0)),
                'counts': counts}


def _tasks(directory, private):
    from core.task_store import TaskStore
    target = private / 'tasks'
    target.mkdir()
    paths = tuple(directory / name for name in ('tasks.json', 'tasks.json.bak'))
    _snapshot_files(paths, target, 64 * 1024 * 1024)
    statuses = {}
    for path in paths:
        copy = target / path.name
        if not copy.exists():
            statuses[path.name] = 'missing'
        else:
            try:
                TaskStore._read_document(copy)
                statuses[path.name] = 'valid'
            except (ValueError, UnicodeError, OSError):
                statuses[path.name] = 'invalid'
    store = TaskStore(target / 'tasks.json')
    records = store.load()
    if store.last_error:
        raise HealthCheckError('任务主快照及备份均不可读取或校验失败；未覆盖或恢复原文件。')
    return {'present': any(value != 'missing' for value in statuses.values()),
            'records': len(records),
            'needs_reanalysis': sum(bool(item.get('context_error')) for item in records),
            'snapshots': statuses, 'warning': store.last_warning}


def _initialize(private):
    from core.media_history import HistoryStore
    from core.task_store import TaskStore
    from core.batch_store import BatchStore
    target = private / 'initialization'
    target.mkdir()
    with HistoryStore(target / 'history.sqlite3') as history:
        history.query(limit=1)
    with closing(BatchStore(target / 'batches.sqlite3')):
        pass
    store = TaskStore(target / 'tasks.json')
    if not store.save([]) or store.load() or store.last_error:
        raise HealthCheckError('任务恢复模块无法在私有临时目录中初始化。')
    return {'history': True, 'tasks': True, 'batches': True}


def health_report(release_dir, data_dir=None, expected_version=None, expected_build=None):
    """Return real checks only; no network, media download, migration or GUI startup."""
    report = {'schema_version': 1, 'kind': 'MediaFlow.update-health', 'passed': False,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'checks': [], 'errors': [],
              'warnings': [], 'identity': {}, 'data_directory': '', 'message': ''}

    def check(name, action, success):
        try:
            details = action()
            report['checks'].append({'name': name, 'passed': True, 'message': success,
                                     'details': details if isinstance(details, dict) else {}})
            return True
        except Exception as error:
            if isinstance(error, HealthCheckError):
                message = str(error)
            elif isinstance(error, PermissionError):
                message = '文件或目录权限不足，未修改现有数据。'
            elif isinstance(error, (OSError, sqlite3.Error)):
                message = '文件读取或本地数据库检查失败（' + type(error).__name__ + '），原数据已保留。'
            else:
                message = '健康检查未通过（' + type(error).__name__ + '），原数据已保留。'
            report['checks'].append({'name': name, 'passed': False, 'message': message})
            report['errors'].append(message)
            return False

    release = Path(release_dir).expanduser().absolute()
    directory = Path(data_dir) if data_dir is not None else ConfigStore().path.parent
    directory = directory.expanduser().absolute()
    report['data_directory'] = str(directory)

    def identity_check():
        actual = runtime_identity()
        report['identity'] = actual
        if expected_version is not None and actual['app_version'] != expected_version:
            raise HealthCheckError('新进程实际版本与待启动版本不一致。')
        if expected_build is not None and actual['build_id'] != expected_build:
            raise HealthCheckError('新进程实际构建标识与待启动构建不一致。')
        if actual['packaged'] and Path(actual['executable']).resolve() != (release / 'MediaFlow.exe').resolve():
            raise HealthCheckError('健康检查进程不是已部署目录中的 MediaFlow.exe。')
        for name in ('dash', 'track', 'organization', 'storage', 'migration'):
            if actual.get(name + '_module_loaded') is not True:
                raise HealthCheckError('必要功能模块未成功加载：' + name)
        return {'packaged': actual['packaged']}

    check('runtime_identity', identity_check, '实际运行版本、构建及必要模块身份一致。')

    def resources():
        from core.update_manifest import REQUIRED_FILES
        _plain_path(release)
        for name in REQUIRED_FILES:
            path = _plain_path(release / name)
            if not path.is_file() or path.stat().st_size == 0:
                raise HealthCheckError('发布目录缺少或无法读取必需文件：' + name)
            with path.open('rb') as stream:
                if not stream.read(1):
                    raise HealthCheckError('必需文件为空：' + name)
        actual = report['identity']
        stamp_path = release / 'build-info.json'
        if stamp_path.stat().st_size > 1024 * 1024:
            raise HealthCheckError('发布构建标识文件过大。')
        stamp = json.loads(stamp_path.read_text(encoding='utf-8'))
        if any(stamp.get(key) != actual.get(key) for key in ('app_version', 'build_id', 'source_sha256')):
            raise HealthCheckError('发布目录的构建标识与实际运行进程不一致。')
        asset = _plain_path(Path(actual['runtime_root']) / 'assets/app.ico')
        if not asset.is_file() or asset.stat().st_size == 0:
            raise HealthCheckError('应用内嵌图标资源缺失。')
        with asset.open('rb') as stream:
            stream.read(1)
        return {'required_files': len(REQUIRED_FILES), 'tools_execution_tested': False}

    check('release_resources', resources, '必需发布文件、tools 结构和内嵌资源可以读取；未执行下载回归。')

    def data_checks():
        _plain_path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with _data_lease(directory), tempfile.TemporaryDirectory(prefix='.mediaflow-health-', dir=directory) as temp:
            private = Path(temp)
            probe = private / 'permission-check'
            with probe.open('xb') as stream:
                stream.write(b'MediaFlow health permission probe')
                stream.flush()
                os.fsync(stream.fileno())
            if probe.read_bytes() != b'MediaFlow health permission probe':
                raise HealthCheckError('用户数据目录读写校验失败。')
            report['checks'].append({'name': 'data_permissions', 'passed': True,
                'message': '用户数据目录可以访问，私有临时文件读写通过。'})
            check('history_database', lambda: _database(directory / 'history.sqlite3', private, 'history'),
                  '既有媒体库只读结构及完整性检查通过；没有媒体库时由独立初始化检查覆盖。')
            check('batch_database', lambda: _database(directory / 'batches.sqlite3', private, 'batches'),
                  '既有批次记录只读结构及完整性检查通过。')

            def task_check():
                result = _tasks(directory, private)
                if result['warning']:
                    report['warnings'].append(result['warning'])
                if result['needs_reanalysis']:
                    report['warnings'].append(str(result['needs_reanalysis']) + ' 个任务的加密上下文无法还原，需要重新分析；原记录及进度保留。')
                return result

            check('task_snapshots', task_check, '任务快照已在只读副本中检查，原快照及备份未恢复或覆盖。')
            check('store_initialization', lambda: _initialize(private), '媒体库、批次与任务恢复模块可在私有临时目录初始化。')

    check('user_data_access', data_checks, '用户数据访问检查已完成，未执行迁移。')
    report['passed'] = not report['errors']
    report['message'] = ('启动健康检查通过；媒体下载及网站登录未在本检查中验证。' if report['passed']
                         else '启动健康检查失败；已保留版本及用户数据，可打开旧版本或查看具体失败项。')
    return report


def _write_report(path, report):
    path = _plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size > 4 * 1024 * 1024:
            raise HealthCheckError('报告目标已存在且不是健康检查报告。')
        previous = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(previous, dict) or previous.get('kind') != 'MediaFlow.update-health':
            raise HealthCheckError('报告目标已存在且不是健康检查报告，未覆盖。')
    descriptor, temporary = tempfile.mkstemp(prefix='.mediaflow-health-report-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_health_cli(argv):
    parser = argparse.ArgumentParser(description='MediaFlow 启动健康检查', allow_abbrev=False)
    parser.add_argument('--update-health', required=True, metavar='REPORT')
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--expected-build', required=True)
    parser.add_argument('--health-data-directory')
    arguments = list(argv)
    if arguments and not arguments[0].startswith('--'):
        arguments = arguments[1:]
    try:
        args = parser.parse_args(arguments)
    except SystemExit as error:
        return int(error.code)
    release = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    report = health_report(release, args.health_data_directory, args.expected_version, args.expected_build)
    try:
        _write_report(Path(args.update_health), report)
    except (OSError, ValueError, HealthCheckError):
        return 2
    return 0 if report['passed'] else 1
