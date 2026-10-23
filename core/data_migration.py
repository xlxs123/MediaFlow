"""Explicit, offline migration with validated readers and a durable per-change journal.

No source store constructor is used. Public reports contain counts and fixed reasons;
encrypted request blobs stay in protected local snapshots/transaction storage only.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid

from core.media_history import HistoryStore, _text, _source_url, _validation, _progress, _site, _time
from core.task_store import TaskStore, _canonical, utc_now
from core.migration_tasks import inspect_tasks, migrated_record, safe_tree, source_lock
from utils.config import ConfigStore, DEFAULTS
from utils.branding import APP_VERSION

SELECTIONS = ('settings', 'library', 'history', 'organization', 'batches', 'tasks', 'browser')
DATA_FILES = ('config.json', 'tasks.json', 'tasks.json.bak', 'history.sqlite3', 'history.sqlite3-wal',
              'batches.sqlite3', 'batches.sqlite3-wal', 'data-manifest.json')
HISTORY_ID = 0x4D464831
BATCH_ID = 0x4D464254


@contextmanager
def _connect(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class MigrationError(RuntimeError):
    pass


class MigrationCancelled(MigrationError):
    pass


def _cancel(event):
    if event is not None and event.is_set():
        raise MigrationCancelled('迁移已安全停止；旧数据保持不变。')


def _json(path, limit=64 * 1024 * 1024):
    if path.is_symlink() or path.is_junction() or path.stat().st_size > limit:
        raise MigrationError('数据文件包含链接或超过安全读取上限。')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise MigrationError('数据文件结构无效。')
    return value


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _binary(value):
    if isinstance(value, bytes):
        return {'__cipher_bytes__': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {k: _binary(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_binary(v) for v in value]
    return value


def _unbinary(value):
    if isinstance(value, dict):
        if set(value) == {'__cipher_bytes__'}:
            return base64.b64decode(value['__cipher_bytes__'], validate=True)
        return {k: _unbinary(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unbinary(v) for v in value]
    return value


class DataDirectoryLease:
    """Cooperating 1.8 instances never open stores while migration is writing them."""
    def __init__(self, directory):
        self.path = Path(directory).resolve() / '.mediaflow-data.lock'
        self.handle = None

    def acquire(self):
        if self.handle:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise MigrationError('数据目录锁路径无效。')
        handle = self.path.open('a+b')
        try:
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise MigrationError('目标数据正在被另一个 MediaFlow 实例或迁移使用，请先关闭其他实例。') from None
        self.handle = handle
        return self

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()


@contextmanager
def frozen_file(path):
    """Read handle denying modification/deletion for the duration of a snapshot."""
    path = Path(path)
    if path.is_symlink() or path.is_junction():
        raise MigrationError('来源文件包含链接，已拒绝迁移。')
    if os.name != 'nt':
        with path.open('rb') as stream:
            yield stream
        return
    import msvcrt
    from core.recovery_workspace import _windows_file_api
    _, create, close = _windows_file_api()
    handle = create(str(path.resolve()), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise MigrationError('来源文件正在使用或无法访问，请关闭其他 MediaFlow 实例后重试。')
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close(handle)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        yield stream


def known_sources():
    roaming = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
    locations = [roaming / 'MediaFlow', roaming / '流媒体下载器', roaming / 'M3U8 Downloader']
    locations += [roaming / 'MediaFlow' / version for version in
                  ('1.2.0', '1.2.1', '1.2.2', '1.3.0', '1.4.0', '1.5.0', '1.6.0', '1.7.0', '1.8.0', '1.9.0', '2.0.0', '2.0.1')]
    return locations


def _profile(directory):
    roaming = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')).resolve()
    local = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
    if directory.parent == roaming / 'MediaFlow':
        return local / 'MediaFlow' / directory.name / 'BrowserProfile'
    if directory == roaming / 'MediaFlow':
        return local / 'MediaFlow/BrowserProfile'
    return directory / 'BrowserProfile'


def _db_read(path, application, required):
    """Freeze DB + WAL, copy to a private directory, let SQLite recover only the copy."""
    from contextlib import ExitStack
    with tempfile.TemporaryDirectory(prefix='MediaFlow-migration-read-') as temporary, ExitStack() as stack:
        target = Path(temporary) / path.name
        for source in (path, path.with_name(path.name + '-wal')):
            if source.is_file():
                stream = stack.enter_context(frozen_file(source))
                if source.stat().st_size > 1024 * 1024 * 1024:
                    raise MigrationError('单个迁移数据库超过 1 GiB，请先分批整理或保留旧版手动处理。')
                with (target.parent / source.name).open('xb') as output:
                    shutil.copyfileobj(stream, output, 1024 * 1024)
        with _connect(target) as db:
            db.row_factory = sqlite3.Row
            if (db.execute('PRAGMA application_id').fetchone()[0] != application
                    or db.execute('PRAGMA user_version').fetchone()[0] != 1
                    or db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'):
                raise MigrationError('数据库标识、版本或完整性不兼容；来源未修改。')
            for table, columns in required.items():
                actual = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
                if not set(columns) <= actual:
                    raise MigrationError('数据库缺少必要字段，不能直接迁移。')
            return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table)] for table in required}


def _history(path):
    from core.media_history import _TASK_COLUMNS, _FILE_COLUMNS
    rows = _db_read(path, HISTORY_ID, {'tasks': ('task_key', *_TASK_COLUMNS, 'removed'),
        'entries': ('id', 'task_key', 'slot', 'file_name', *_FILE_COLUMNS, 'custom_fields'),
        'tombstones': ('task_key', 'slot')})
    tasks = {}
    for row in rows['tasks']:
        key = row['task_key']
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', key):
            raise MigrationError('历史记录标识无效。')
        if row['task_state'] not in ('completed', 'failed', 'cancelled'):
            raise MigrationError('历史记录状态不兼容。')
        row.update(title=_text(row['title'], 512), source_site=_site(row['source_site']),
                   source_url=_source_url(row['source_url']), error=_text(row['error'], 3000),
                   progress=json.dumps(_progress(json.loads(row['progress'])), ensure_ascii=False))
        for name in ('started_at', 'finished_at', 'cancelled_at'):
            row[name] = _time(row[name])
        row['queue_id'] = int(row['queue_id']) if type(row['queue_id']) is int and row['queue_id'] >= 0 else 0
        row['removed'] = int(bool(row['removed']))
        tasks[key] = {k: row[k] for k in ('task_key', *_TASK_COLUMNS, 'removed')}
    tombs = {(row['task_key'], row['slot']) for row in rows['tombstones']}
    entries = []
    from core.media_history import _path, _number
    for row in rows['entries']:
        if row['task_key'] not in tasks:
            raise MigrationError('历史记录存在无主文件索引。')
        if tasks[row['task_key']]['removed'] or (row['task_key'], row['slot']) in tombs:
            continue
        row['path'] = _path(row['path'])
        row['thumbnail_path'] = _path(row['thumbnail_path'])
        row['file_name'] = Path(row['path']).name or _text(row['file_name'], 512)
        if type(row['slot']) is not int or row['slot'] < 0:
            raise MigrationError('历史文件编号无效。')
        for field in ('size', 'width', 'height'):
            row[field] = _number(row[field], integer=True)
        row['duration'] = _number(row['duration'])
        for field in ('container', 'video_codec', 'audio_codec'):
            row[field] = _text(row[field], 160)
        row['media_type'] = row['media_type'] if row['media_type'] in ('video', 'audio', 'unknown') else 'unknown'
        row['validation'] = json.dumps(_validation(json.loads(row['validation'])), ensure_ascii=False)
        row['validation_reason'] = _text(row['validation_reason'], 1500)
        row['validation_state'] = row['validation_state'] if row['validation_state'] in ('valid', 'failed', 'unverified') else 'unverified'
        row['custom_fields'] = '[]'  # Old relocation/cleanup transaction ownership must not replay.
        entries.append({k: row[k] for k in ('id','task_key','slot','file_name', *_FILE_COLUMNS, 'custom_fields')})
    return {'tasks': tasks, 'entries': entries}


def _batches(path):
    from core.batch_store import _batch, _item
    rows = _db_read(path, BATCH_ID, {'batches': ('id', 'metadata'),
        'items': ('id', 'batch_id', 'ordinal', 'url_hash', 'url_cipher', 'metadata')})
    batches = []
    ids = set()
    for row in rows['batches']:
        data = json.loads(row['metadata'])
        if data.get('id') != row['id']:
            raise MigrationError('批次标识和内容不一致。')
        record = _batch(data)
        record['paused'] = True
        batches.append({'id': record['id'], 'metadata': json.dumps(record, ensure_ascii=False)})
        ids.add(record['id'])
    items = []
    for row in rows['items']:
        if row['batch_id'] not in ids:
            raise MigrationError('批次任务缺少所属批次。')
        data = json.loads(row['metadata'])
        data.update(id=row['id'], batch_id=row['batch_id'], ordinal=row['ordinal'])
        context = ''
        try:
            address = TaskStore._unprotect(row['url_cipher']).decode('utf-8')
            if hashlib.sha256(address.encode()).hexdigest() != row['url_hash']:
                raise ValueError()
            from core.batch_input import _validate_source_url
            _validate_source_url(address)
        except Exception:
            context = '来源加密上下文不可用，请手动重新分析。'
        if data.get('state') not in ('completed', 'cancelled', 'failed'):
            data.update(state='waiting_context' if context else 'paused', resume_state='waiting_discovery',
                        error='升级后需要重新分析来源，原下载进度保留。')
        data['job_id'] = 0
        if context:
            data['context_error'] = context
        record = _item(data)
        row['metadata'] = json.dumps(record, ensure_ascii=False)
        items.append(row)
    return {'batches': batches, 'items': items}


def _legacy_task_history(path):
    """Older releases may have only TaskStore, including terminal task metadata."""
    from types import SimpleNamespace
    from core.downloader import DownloadRequest
    from core.media_history import snapshot_job
    store = TaskStore(path)
    rows = store.load()
    if store.last_error:
        return {'tasks': {}, 'entries': []}
    with tempfile.TemporaryDirectory(prefix='MediaFlow-migration-history-') as temporary:
        database_path = Path(temporary) / 'history.sqlite3'
        with HistoryStore(database_path) as history:
            for item in rows:
                meta = item['metadata']
                if meta.get('state') not in ('completed', 'failed', 'cancelled'):
                    continue
                request = item['request'] or DownloadRequest('', '', str(Path.home() / 'Downloads'))
                job = SimpleNamespace(id=meta['id'], request=request, name=_text(meta.get('name'), 512),
                    state=meta['state'], progress=meta.get('progress', {}), context_error='', status='',
                    created_at=meta.get('created_at', ''), updated_at=meta.get('updated_at', ''),
                    result={'files': meta.get('completed_files', []), 'error': item.get('last_error', '')})
                history.record(snapshot_job(job))
        return _history(database_path)


class MigrationEngine:
    def __init__(self, target_dir, target_profile=None):
        self.target = Path(target_dir).expanduser().resolve()
        self.target_profile = Path(target_profile).expanduser().resolve() if target_profile else _profile(self.target)
        self.root = self.target / 'migration'
        self._plans = {}
        self._mutex = threading.Lock()

    def _ledger(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / 'transactions.sqlite3', timeout=1.5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL, before_data TEXT, after_data TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS imports (source TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, digest TEXT NOT NULL, target TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(source,kind,item,digest))')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            db.close()
            raise MigrationError('迁移事务数据库校验失败，备份已保留，请先检查迁移报告。')
        db.commit()
        return db

    def _fingerprint(self, path):
        data = {}
        for name in DATA_FILES:
            file = path / name
            if file.is_file():
                with frozen_file(file) as stream:
                    data[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        return _hash(data)

    def _read(self, directory):
        directory = Path(directory).expanduser().resolve()
        if directory == self.target or directory.is_relative_to(self.target) or self.target.is_relative_to(directory) and directory != self.target.parent:
            raise MigrationError('迁移来源必须与目标数据目录分离。')
        if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
            raise MigrationError('请选择有效、无链接的旧版数据目录。')
        payload = {'settings': {}, 'history': {'tasks': {}, 'entries': []}, 'batches': {'batches': [], 'items': []}, 'tasks': []}
        issues = []
        types = []
        for name, key, reader in (('config.json', 'settings', lambda p: _json(p, 65536)),
                ('history.sqlite3', 'history', _history), ('batches.sqlite3', 'batches', _batches),
                ('tasks.json', 'tasks', inspect_tasks)):
            file = directory / name
            if file.is_file() or name == 'tasks.json' and file.with_name(name + '.bak').is_file():
                try:
                    payload[key] = reader(file)
                    if key == 'settings' and not set(payload[key]) & set(DEFAULTS):
                        raise MigrationError('配置不含已知 MediaFlow 设置。')
                    types.append(key)
                except (MigrationError, ValueError, OSError, sqlite3.Error, TypeError, KeyError):
                    payload[key] = {'tasks': {}, 'entries': []} if key == 'history' else {'batches': [], 'items': []} if key == 'batches' else [] if key == 'tasks' else {}
                    issues.append(name + '：内容不兼容、损坏或正在使用，已跳过；来源保持不变。')
        if 'tasks' in types:
            legacy = _legacy_task_history(directory / 'tasks.json')
            existing = payload['history']['tasks']
            for key, task in legacy['tasks'].items():
                if key not in existing:
                    existing[key] = task
                    payload['history']['entries'].extend(r for r in legacy['entries'] if r['task_key'] == key)
            if legacy['entries'] and 'history' not in types:
                types.append('history')
        profile = _profile(directory)
        if profile.is_dir():
            types.append('browser')
        if not types:
            raise MigrationError('所选目录没有可识别的 MediaFlow 数据。')
        version = '确切版本未知（按结构检查）'
        manifest = directory / 'data-manifest.json'
        if manifest.is_file():
            data = _json(manifest, 65536)
            if data.get('application') == 'MediaFlow' and re.fullmatch(r'1\.[2-8]\.\d+', str(data.get('version', ''))):
                version = data['version'] + '（标识与数据结构已检查）'
        counts = {'settings': len(set(payload['settings']) & set(DEFAULTS)),
                  'library': sum(payload['history']['tasks'][r['task_key']]['task_state'] == 'completed' for r in payload['history']['entries']),
                  'history': len(payload['history']['entries']), 'batches': len(payload['batches']['batches']),
                  'collections': sum(bool(json.loads(r['metadata']).get('options', {}).get('collection_name')) for r in payload['batches']['batches']),
                  'tasks_recoverable': sum(t['recoverable'] for t in payload['tasks']),
                  'tasks_incompatible': sum(not t['recoverable'] for t in payload['tasks'])}
        source = {'path': str(directory), 'version': version, 'profile_path': str(profile), 'counts': counts,
                  'types': types, 'issues': issues, 'fingerprint': self._fingerprint(directory), 'complete': not issues}
        source['id'] = _hash(str(directory).casefold())
        return source, payload

    def discover(self, paths=None):
        sources = []
        seen = set()
        for path in paths if paths is not None else known_sources():
            directory = Path(path).expanduser().resolve()
            if directory in seen or directory == self.target or not directory.is_dir():
                continue
            seen.add(directory)
            try:
                source, _ = self._read(directory)
                sources.append(source)
            except (MigrationError, OSError, ValueError):
                if paths is not None:
                    sources.append({'path': str(directory), 'version': '无法识别', 'types': [], 'counts': {}, 'issues': ['目录不兼容、已损坏或正在使用。'], 'complete': False})
        return sources

    def preview(self, source_path, selections=None):
        source, payload = self._read(source_path)
        choice = {key: key in ('settings', 'library', 'history') for key in SELECTIONS}
        if selections:
            choice.update({k: bool(v) for k, v in selections.items() if k in SELECTIONS})
        policy = (selections or {}).get('settings_policy', 'keep')
        choice['settings_policy'] = 'source' if policy == 'source' else 'keep'
        issues = list(source['issues'])
        if choice['tasks']:
            issues += ['旧任务 ' + str(t['source_id']) + '：' + t['reason']
                       for t in payload['tasks'][:100] if not t['recoverable']]
        config, warnings = self._settings(payload['settings'], choice)
        issues += warnings
        estimated = sum((Path(source['path']) / name).stat().st_size for name in DATA_FILES if (Path(source['path']) / name).is_file())
        estimated += sum(t['workspace']['bytes'] for t in payload['tasks'] if choice['tasks'] and t['recoverable'] and t['workspace'])
        browser = {}
        if choice['browser']:
            from core.migration_browser import inspect_profile
            browser = inspect_profile(source['profile_path'])
            estimated += browser.get('bytes', 0)
            if not browser.get('available', False):
                issues.append(browser.get('reason') or '浏览器配置暂不可迁移。')
            if self.target_profile.exists() and any(self.target_profile.iterdir()):
                browser.update(available=False, reason='新版 BrowserProfile 已有数据，保留现有配置，不能覆盖。')
                issues.append(browser['reason'])
        plan_id = uuid.uuid4().hex
        plan = {'plan_id': plan_id, 'source': source['path'], 'target': str(self.target), 'version': source['version'],
            'target_version': APP_VERSION, 'counts': source['counts'], 'items': choice, 'issues': issues,
            'estimated_bytes': estimated, 'files_to_check': sum(bool(r['path']) for r in payload['history']['entries']), 'browser': browser,
            'settings_preview': config, 'settings_policy': choice['settings_policy'], 'fingerprint': source['fingerprint']}
        self._plans[plan_id] = {'public': deepcopy(plan), 'payload': payload, 'config': config, 'source': source}
        return plan

    def _settings(self, values, choice):
        safe = ConfigStore(self.target / 'config.json')._safe(values)
        warnings = []
        result = {key: safe[key] for key in values if key in DEFAULTS and key != 'organization'} if choice['settings'] else {}
        # Naming rules are a distinct selection, including batch naming preferences.
        if choice['organization']:
            from core.output_organization import normalize_options, classification_parts, render_filename
            from string import Formatter
            raw = values.get('organization', {})
            options = normalize_options(raw)
            allowed = {'title','site','date','time','resolution','quality','id','ext','media_type'}
            try:
                for key in ('filename_template', 'directory_template'):
                    fields = [field for _, field, _, _ in Formatter().parse(options[key]) if field]
                    if any(field not in allowed for field in fields):
                        raise ValueError()
                classification_parts(options, {})
                render_filename(options['filename_template'], {}, 'mp4')
            except (ValueError, KeyError):
                options = dict(DEFAULTS['organization'])
                warnings.append('命名或分类规则包含不支持的字段/路径，预览已恢复安全默认值，不会重命名现有媒体。')
            result['organization'] = options
        elif 'batch_import' in result:
            result['batch_import'].pop('organization', None)
        for name in ('yt_dlp', 'ffmpeg', 'ffprobe'):
            path = result.get(name + '_path', '')
            if path and not Path(path).is_file():
                result.update({name + '_path': '', name + '_mode': 'auto'})
                warnings.append(name + ' 的旧绝对路径已失效，将使用工具自动定位。')
        if 'output_directory' in result:
            path = Path(result['output_directory']).expanduser()
            if not path.is_absolute() or path.is_file():
                result.pop('output_directory')
                warnings.append('旧默认下载目录无效，保留新版目录；不会移动旧媒体。')
        current = _json(self.target / 'config.json', 65536) if (self.target / 'config.json').exists() else {}
        if choice['settings_policy'] == 'keep':
            conflicts = set(current) & set(result)
            result = {k: v for k, v in result.items() if k not in current}
            if conflicts:
                warnings.append('配置冲突：保留目标中已有的 ' + str(len(conflicts)) + ' 个设置。')
        return result, warnings

    def _current_change(self, kind, target):
        if kind == 'config':
            path = self.target / 'config.json'
            data = _json(path, 65536) if path.exists() else {}
            return data.get(target, {'__absent__': True})
        if kind == 'queue':
            path = self.target / 'tasks.json'
            data = TaskStore._read_document(path)[1]['tasks'] if path.exists() else []
            return next((r for r in data if r['metadata']['id'] == int(target)), None)
        if kind in ('history', 'batches'):
            table, key = target.split(':', 1)
            columns = {'tasks': 'task_key', 'entries': 'id', 'migration_visibility': 'entry_id'} if kind == 'history' else {'batches': 'id', 'items': 'id'}
            if table not in columns:
                raise MigrationError('迁移事务字段无效。')
            path = self.target / ('history.sqlite3' if kind == 'history' else 'batches.sqlite3')
            if not path.exists():
                return None
            with _connect(path) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(f'SELECT * FROM {table} WHERE {columns[table]}=?', (key,)).fetchone()
                return dict(row) if row else None
        raise MigrationError('迁移事务类型无效。')

    def list_runs(self):
        if not (self.root / 'transactions.sqlite3').exists():
            return []
        with closing(self._ledger()) as db:
            return [json.loads(row['report']) for row in db.execute('SELECT report FROM runs ORDER BY rowid DESC')]

    def _target_totals(self):
        totals = {'history_entries': 0, 'queue_tasks': 0, 'batch_groups': 0}
        for name, table, key in (('history.sqlite3','entries','history_entries'), ('batches.sqlite3','batches','batch_groups')):
            path = self.target / name
            if path.exists():
                with _connect(path.as_uri() + '?mode=ro', uri=True) as db:
                    totals[key] = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        path = self.target / 'tasks.json'
        if not path.exists() and path.with_name(path.name + '.bak').exists():
            raise MigrationError('目标任务主文件缺失但恢复备份仍存在，请先关闭助手并由主窗口恢复备份，再重新迁移。')
        if path.exists():
            try:
                totals['queue_tasks'] = len(TaskStore._read_document(path)[1]['tasks'])
            except (ValueError, OSError, UnicodeError):
                raise MigrationError('目标任务快照无法通过校验，请先由主窗口恢复备份，再重新迁移；已有任务未覆盖。') from None
        return totals

    def _report(self, db, report):
        path = self.root / report['id'] / 'report.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        report['report_path'] = str(path)
        TaskStore._atomic_write(path, _canonical(report))
        db.execute('UPDATE runs SET status=?,report=? WHERE id=?', (report['status'], json.dumps(report, ensure_ascii=False), report['id']))
        db.commit()
        return report

    def _prepared(self, db, run, kind, target, before, after):
        seq = db.execute('INSERT INTO changes(run_id,kind,target,before_data,after_data) VALUES(?,?,?,?,?)',
             (run, kind, target, json.dumps(_binary(before), ensure_ascii=False), json.dumps(_binary(after), ensure_ascii=False))).lastrowid
        db.commit()  # Durable undo is written before a target mutation.
        return seq

    def _change(self, db, run, kind, target, before, after):
        seq = self._prepared(db, run, kind, target, before, after)
        self._write_change(kind, target, before, after)
        db.execute('UPDATE changes SET applied=1 WHERE seq=?', (seq,))
        db.commit()

    def _write_change(self, kind, target, before, after):
        if kind == 'config':
            path = self.target / 'config.json'
            data = _json(path, 65536) if path.exists() else {}
            if data.get(target, {'__absent__': True}) != before:
                raise MigrationError('目标设置在迁移期间发生变化，已停止以避免覆盖。')
            if after == {'__absent__': True}:
                data.pop(target, None)
            else:
                data[target] = after
            TaskStore._atomic_write(path, _canonical(data))
        elif kind == 'queue':
            path = self.target / 'tasks.json'
            document = TaskStore._read_document(path)[1] if path.exists() else {'version': 1, 'tasks': []}
            rows = document['tasks']
            current = next((r for r in rows if r['metadata']['id'] == int(target)), None)
            if current != before:
                raise MigrationError('目标任务在迁移期间变化，已停止以避免覆盖。')
            rows = [r for r in rows if r['metadata']['id'] != int(target)]
            if after is not None:
                rows.append(after)
            document = {'version': 1, 'saved_at': utc_now(), 'tasks': rows}
            document['checksum'] = _hash(document)
            TaskStore._atomic_write(path, _canonical(document))
        elif kind in ('history', 'batches'):
            table, key = target.split(':', 1)
            allowed = {'history': {'tasks': 'task_key', 'entries': 'id', 'migration_visibility': 'entry_id'},
                       'batches': {'batches': 'id', 'items': 'id'}}
            column = allowed[kind].get(table)
            if column is None:
                raise MigrationError('迁移事务字段无效。')
            with _connect(self.target / ('history.sqlite3' if kind == 'history' else 'batches.sqlite3')) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute('BEGIN IMMEDIATE')
                row = connection.execute(f'SELECT * FROM {table} WHERE {column}=?', (key,)).fetchone()
                if (dict(row) if row else None) != before:
                    raise MigrationError('目标数据库记录在迁移期间变化，已停止以避免覆盖。')
                if after is None:
                    if table == 'tasks' and connection.execute('SELECT 1 FROM entries WHERE task_key=?', (key,)).fetchone():
                        raise MigrationError('历史任务仍被后续记录引用，不能回滚删除。')
                    if table == 'batches' and connection.execute('SELECT 1 FROM items WHERE batch_id=?', (key,)).fetchone():
                        raise MigrationError('批次仍有后续任务，不能回滚删除。')
                    connection.execute(f'DELETE FROM {table} WHERE {column}=?', (key,))
                elif before is None:
                    columns = list(after)
                    connection.execute(f'INSERT INTO {table} (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', tuple(after[c] for c in columns))
                else:
                    columns = [c for c in after if c != column]
                    connection.execute(f'UPDATE {table} SET ' + ','.join(c + '=?' for c in columns) + f' WHERE {column}=?', (*[after[c] for c in columns], key))
        else:
            raise MigrationError('未知迁移事务类型。')

    def _backup(self, run, cancel_event, progress):
        destination = self.root / run / 'backup'
        destination.mkdir(parents=True, exist_ok=False)
        proof = {}
        for i, name in enumerate(DATA_FILES):
            _cancel(cancel_event)
            path = self.target / name
            if path.is_file():
                with frozen_file(path) as stream, (destination / name).open('xb') as output:
                    shutil.copyfileobj(stream, output)
                    output.flush()
                    os.fsync(output.fileno())
                proof[name] = hashlib.sha256((destination / name).read_bytes()).hexdigest()
            if progress:
                progress({'stage': 'backup', 'current': i + 1, 'total': len(DATA_FILES), 'message': '备份目标数据'})
        TaskStore._atomic_write(destination / 'snapshot.json', _canonical(proof))

    def _init_stores(self, choice):
        if choice['library'] or choice['history']:
            with HistoryStore(self.target / 'history.sqlite3'):
                pass
        if choice['batches']:
            from core.batch_store import BatchStore
            store = BatchStore(self.target / 'batches.sqlite3')
            store.close()

    def _imported(self, db, source, kind, item, digest):
        parameters = (source, kind, item, digest)
        row = db.execute('SELECT target,run_id FROM imports WHERE source=? AND kind=? AND item=? AND digest=?', parameters).fetchone()
        if row is None:
            return None
        exists = False
        if kind == 'queue':
            current = self._current_change('queue', row['target'])
            previous = db.execute("SELECT after_data FROM changes WHERE run_id=? AND kind='queue' AND target=? ORDER BY seq DESC LIMIT 1",
                                  (row['run_id'], row['target'])).fetchone()
            if current and previous:
                original = json.loads(previous[0])['metadata']
                actual = current['metadata']
                temporary = original.get('request', {}).get('temporary_directory', '')
                current_temporary = actual.get('request', {}).get('temporary_directory', '')
                exists = (original.get('created_at') == actual.get('created_at') and
                          (not temporary or Path(temporary).name == Path(current_temporary).name))
        elif kind == 'history':
            with _connect(self.target / 'history.sqlite3') as target:
                exists = bool(target.execute('SELECT 1 FROM tasks t JOIN entries e ON e.task_key=t.task_key WHERE t.task_key=? LIMIT 1', (row['target'],)).fetchone())
        elif kind == 'batch':
            with _connect(self.target / 'batches.sqlite3') as target:
                exists = bool(target.execute('SELECT 1 FROM batches WHERE id=?', (row['target'],)).fetchone())
                if exists:
                    children = [json.loads(change[0]) for change in db.execute(
                        "SELECT after_data FROM changes WHERE run_id=? AND kind='batches' AND target LIKE 'items:%'",
                        (row['run_id'],))]
                    exists = all(target.execute('SELECT 1 FROM items WHERE id=? AND batch_id=?',
                        (child['id'], row['target'])).fetchone() for child in children if child['batch_id'] == row['target'])
        if exists:
            return row
        # A partial rollback can remove some imported records while keeping later
        # user edits. Stale provenance must not suppress those records on retry.
        db.execute('DELETE FROM imports WHERE source=? AND kind=? AND item=? AND digest=?', parameters)
        db.commit()
        return None

    def _remember(self, db, run, source, kind, item, digest, target):
        db.execute('INSERT OR IGNORE INTO imports VALUES(?,?,?,?,?,?)', (source, kind, item, digest, str(target), run))
        db.commit()

    def _merge_history(self, db, run, source, payload, choice, counts, cancel_event):
        if not (choice['library'] or choice['history']):
            return
        target_path = self.target / 'history.sqlite3'
        with _connect(target_path) as target:
            target.row_factory = sqlite3.Row
            next_id = target.execute('SELECT COALESCE(MAX(id),0)+1 FROM entries').fetchone()[0]
        groups = {}
        for row in payload['entries']:
            groups.setdefault(row['task_key'], []).append(row)
        for task_key, entries in groups.items():
            _cancel(cancel_event)
            task = dict(payload['tasks'][task_key])
            if task['task_state'] != 'completed' and not choice['history']:
                continue
            digest = _hash([task, entries])
            if self._imported(db, source, 'history', task_key, digest):
                counts['skipped'] += len(entries)
                continue
            target_key = task_key
            with _connect(target_path) as target:
                target.row_factory = sqlite3.Row
                existing = target.execute('SELECT * FROM tasks WHERE task_key=?', (target_key,)).fetchone()
                if existing:
                    actual = [dict(r) for r in target.execute('SELECT * FROM entries WHERE task_key=? ORDER BY slot', (target_key,))]
                    comparable = lambda rows: [{k: v for k, v in r.items() if k not in ('id', 'file_state', 'validation_reason')} for r in rows]
                    if dict(existing) == task and comparable(actual) == comparable(entries):
                        counts['skipped'] += len(entries)
                        self._remember(db, run, source, 'history', task_key, digest, target_key)
                        continue
                    target_key = 'migrated:' + _hash([source, task_key, digest])
                    if target.execute('SELECT 1 FROM tasks WHERE task_key=?', (target_key,)).fetchone():
                        counts['skipped'] += len(entries)
                        self._remember(db, run, source, 'history', task_key, digest, target_key)
                        continue
                    counts['conflicts'] += 1
            task['task_key'] = target_key
            self._change(db, run, 'history', 'tasks:' + target_key, None, task)
            for entry in entries:
                row = dict(entry, id=next_id, task_key=target_key)
                from core.library_validation import file_status
                state = file_status(row['path']) if row['path'] else {'file_state': 'unverified'}
                row['file_state'] = state['file_state']
                if row['file_state'] != 'exists':
                    counts['missing_files'] += int(bool(row['path']))
                else:
                    stat = Path(row['path']).stat()
                    validation = json.loads(row['validation'])
                    if row['validation_state'] == 'valid' and (validation.get('size') != stat.st_size or validation.get('mtime_ns') != stat.st_mtime_ns):
                        row.update(validation_state='unverified', validation_reason='文件存在，但旧验证与当前大小/修改时间不一致，请重新验证。')
                self._change(db, run, 'history', 'entries:' + str(next_id), None, row)
                visibility = {'entry_id': next_id, 'library': int(choice['library']), 'history': int(choice['history'])}
                self._change(db, run, 'history', 'migration_visibility:' + str(next_id), None, visibility)
                next_id += 1
                counts['history'] += int(choice['history'])
                counts['library'] += int(choice['library'] and task['task_state'] == 'completed')
            self._remember(db, run, source, 'history', task_key, digest, target_key)

    def _merge_batches(self, db, run, source, payload, counts, cancel_event):
        path = self.target / 'batches.sqlite3'
        for batch in payload['batches']:
            _cancel(cancel_event)
            items = [i for i in payload['items'] if i['batch_id'] == batch['id']]
            digest = _hash(_binary([batch, items]))
            if self._imported(db, source, 'batch', batch['id'], digest):
                counts['skipped'] += 1
                continue
            record = dict(batch)
            old_id = record['id']
            with _connect(path) as connection:
                exists = connection.execute('SELECT 1 FROM batches WHERE id=?', (old_id,)).fetchone()
                if exists:
                    record['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, source + old_id + digest))
                    if connection.execute('SELECT 1 FROM batches WHERE id=?', (record['id'],)).fetchone():
                        raise MigrationError('目标中已有该批次的迁移副本，且来源映射无法完整对应；请先检查批次，未重复合并或覆盖。')
                    counts['conflicts'] += 1
            metadata = json.loads(record['metadata'])
            metadata['id'] = record['id']
            record['metadata'] = json.dumps(metadata, ensure_ascii=False)
            self._change(db, run, 'batches', 'batches:' + record['id'], None, record)
            for original in items:
                row = dict(original, batch_id=record['id'])
                with _connect(path) as connection:
                    if connection.execute('SELECT 1 FROM items WHERE id=?', (row['id'],)).fetchone():
                        row['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, source + original['id'] + digest))
                value = json.loads(row['metadata'])
                value.update(id=row['id'], batch_id=record['id'])
                row['metadata'] = json.dumps(value, ensure_ascii=False)
                self._change(db, run, 'batches', 'items:' + row['id'], None, row)
            counts['batches'] += 1
            self._remember(db, run, source, 'batch', old_id, digest, record['id'])

    def _merge_tasks(self, db, run, source, payload, counts, cancel_event, progress):
        path = self.target / 'tasks.json'
        rows = TaskStore._read_document(path)[1]['tasks'] if path.exists() else []
        next_id = max((r['metadata']['id'] for r in rows), default=0) + 1
        for value in payload:
            _cancel(cancel_event)
            request = value['request']
            from core.resume_identity import _asset_url
            identity = (request.resume_identity or {'protocol': request.download_protocol,
                'source': _asset_url(request.url)}) if request is not None else None
            digest = _hash([value['metadata'], identity,
                            value['workspace'].get('checkpoint') if value['workspace'] else None])
            if self._imported(db, source, 'queue', value['source_id'], digest):
                counts['skipped'] += 1
                continue
            workspace = ''
            if value['recoverable'] and value['workspace']:
                # Same UUID, independent directory: stable queue/history/collection
                # relationships survive without sharing a writable old workspace.
                root = self.target / 'Recovered' / '.stream-downloads' / value['workspace']['root'].name
                if root.exists():
                    raise MigrationError('目标中已有相同工作区标识，未覆盖既有进度；请检查重复来源或重新分析。')
                proof = {}
                # Durable ownership written before copying; a stopped copy is never resumable.
                seq = self._prepared(db, run, 'workspace', str(root), None, {})
                root.mkdir(parents=True, exist_ok=False)
                from core.migration_tasks import inspect_workspace
                current = inspect_workspace(value['request'])
                with source_lock(value['workspace']['root']):
                    for number, file in enumerate(current['files']):
                        _cancel(cancel_event)
                        target = root / file.relative_to(current['root'])
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with frozen_file(file) as stream, target.open('xb') as output:
                            digest_file = hashlib.sha256()
                            while data := stream.read(1024 * 1024):
                                _cancel(cancel_event)
                                output.write(data)
                                digest_file.update(data)
                            output.flush()
                            os.fsync(output.fileno())
                        proof[target.relative_to(root).as_posix()] = digest_file.hexdigest()
                        if progress:
                            progress({'stage': 'tasks', 'current': number + 1, 'total': len(current['files']), 'message': '复制已验证的暂停进度'})
                db.execute('UPDATE changes SET after_data=?,applied=1 WHERE seq=?', (json.dumps(proof), seq))
                db.commit()
                workspace = str(root)
                cloned = deepcopy(request)
                cloned.temporary_directory = workspace
                cloned.workspace_output_directory = str(self.target / 'Recovered')
                # Validate the copy against its original recovery proof, rather than
                # merely trusting the bytes seen during an earlier source inspection.
                inspect_workspace(cloned)
            record = migrated_record(value, next_id, self.target, workspace)
            self._change(db, run, 'queue', str(next_id), None, record)
            self._remember(db, run, source, 'queue', value['source_id'], digest, str(next_id))
            next_id += 1
            counts['tasks_recoverable' if value['recoverable'] else 'tasks_incompatible'] += 1

    def apply(self, plan, confirmed=False, browser_confirmed=False, cancel_event=None, progress=None):
        if not confirmed:
            raise MigrationError('迁移尚未确认，请先检查预览。')
        cached = self._plans.get(plan.get('plan_id'))
        if not cached or cached['public'] != plan:
            raise MigrationError('迁移预览已失效，请重新扫描并确认。')
        choice = plan['items']
        if choice['browser'] and not browser_confirmed:
            raise MigrationError('浏览器登录配置需要单独明确确认。')
        if choice['browser'] and not plan.get('browser', {}).get('available'):
            raise MigrationError('浏览器配置当前不能安全迁移，请检查预览原因后重新选择。')
        if not any(choice[k] for k in SELECTIONS):
            raise MigrationError('请至少选择一项迁移内容。')
        with self._mutex, DataDirectoryLease(self.target), closing(self._ledger()) as db:
            _cancel(cancel_event)
            if self._fingerprint(Path(plan['source'])) != plan['fingerprint']:
                raise MigrationError('来源数据自预览后已变化，请重新生成预览。')
            source, payload = self._read(plan['source'])
            required = plan['estimated_bytes'] * 3 + 16 * 1024 * 1024
            for file in DATA_FILES:
                if (self.target / file).is_file():
                    required += (self.target / file).stat().st_size * 2
            if shutil.disk_usage(self.target).free < required:
                raise MigrationError('目标磁盘空间不足，迁移尚未开始；请腾出备份和复制所需空间。')
            if choice['browser']:
                volume = self.target_profile.parent
                while not volume.exists() and volume != volume.parent:
                    volume = volume.parent
                if shutil.disk_usage(volume).free < plan['browser'].get('bytes', 0) * 3 + 16 * 1024 * 1024:
                    raise MigrationError('浏览器配置目标磁盘空间不足，尚未开始复制；原配置保持不变。')
            run = uuid.uuid4().hex
            report = {'id': run, 'status': 'preparing', 'source': plan['source'], 'target': str(self.target),
                      'version': plan['version'], 'target_version': APP_VERSION, 'items': choice,
                      'started_at': utc_now(), 'counts_before': plan['counts'], 'counts': dict.fromkeys(
                          ('settings','library','history','batches','tasks_recoverable','tasks_incompatible','skipped','failed','conflicts','missing_files'), 0),
                      'issues': list(plan['issues']), 'browser': {'status': 'not_selected', 'website_login': '未验证：请实际打开网站确认。'}}
            report['target_counts_before'] = self._target_totals()
            db.execute('INSERT INTO runs VALUES(?,?,?,?)', (run, source['id'], report['status'], json.dumps(report, ensure_ascii=False)))
            db.commit()
            self._report(db, report)
            try:
                self._backup(run, cancel_event, progress)
                self._init_stores(choice)
                report['status'] = 'running'
                self._report(db, report)
                config, _ = self._settings(payload['settings'], choice)
                current = _json(self.target / 'config.json', 65536) if (self.target / 'config.json').exists() else {}
                for key, value in config.items():
                    _cancel(cancel_event)
                    self._change(db, run, 'config', key, current.get(key, {'__absent__': True}), value)
                    report['counts']['settings'] += 1
                if progress:
                    progress({'stage': 'history', 'current': 0, 'total': 0, 'message': '逐项导入历史并检查实际文件状态'})
                self._merge_history(db, run, source['id'], payload['history'], choice, report['counts'], cancel_event)
                if choice['batches']:
                    if progress:
                        progress({'stage': 'batches', 'current': 0, 'total': 0, 'message': '迁移批次与合集，保留独立暂停状态'})
                    self._merge_batches(db, run, source['id'], payload['batches'], report['counts'], cancel_event)
                if choice['tasks']:
                    self._merge_tasks(db, run, source['id'], payload['tasks'], report['counts'], cancel_event, progress)
                if choice['browser']:
                    from core.migration_browser import copy_profile
                    if self.target_profile.exists() and any(self.target_profile.iterdir()):
                        raise MigrationError('新版 BrowserProfile 已有数据，保留现有配置，不能覆盖；请先选择空的独立目标。')
                    # Keep partial copies away from the runtime location, including
                    # cancellation and power loss. Staging is on the profile's volume
                    # so publication is one directory rename, even with redirected AppData.
                    stage = self.target_profile.parent / ('.mediaflow-migration-' + run) / 'BrowserProfile'
                    seq = self._prepared(db, run, 'browser_staging', str(stage), None, {})
                    profile = copy_profile(source['profile_path'], stage, cancel_event=cancel_event, progress=progress)
                    db.execute('UPDATE changes SET after_data=?,applied=1 WHERE seq=?', (json.dumps(profile), seq))
                    db.commit()
                    _cancel(cancel_event)
                    publication = self._prepared(db, run, 'browser', str(self.target_profile), None, profile)
                    if self.target_profile.exists():
                        if any(self.target_profile.iterdir()):
                            raise MigrationError('浏览器目标在迁移期间出现新数据，未覆盖；完整副本保留在暂存目录。')
                        self.target_profile.rmdir()  # Empty directory only; never deletes profile data.
                    stage.rename(self.target_profile)
                    db.execute('UPDATE changes SET applied=1 WHERE seq=?', (publication,))
                    db.commit()
                    report['browser'] = {'status': 'copied', 'files': profile.get('files_count', 0),
                        'bytes': profile.get('bytes', 0), 'website_login': '未验证：请实际打开网站确认。',
                        'usability': '离线结构和完整复制已验证；首次浏览器打开时检查 WebView2 可用性。'}
                _cancel(cancel_event)
                if progress:
                    progress({'stage': 'verify', 'current': 0, 'total': 0, 'message': '核对目标数据库与来源数据完整性'})
                self._verify_targets(choice)
                if self._fingerprint(Path(plan['source'])) != plan['fingerprint']:
                    raise MigrationError('执行期间来源数据发生变化，迁移已停止，需重新扫描。')
                report.update(status='completed', finished_at=utc_now())
                report['target_counts_after'] = self._target_totals()
                return self._report(db, report)
            except Exception as error:
                report['counts']['failed'] += 1
                report['issues'].append(str(error) if isinstance(error, MigrationError) else '迁移未完成（' + type(error).__name__ + '），请查看回滚摘要；原版数据保留。')
                outcome = self._rollback(db, run)
                report.update(status='cancelled' if isinstance(error, MigrationCancelled) else 'failed', rollback=outcome, finished_at=utc_now())
                return self._report(db, report)

    def _verify_targets(self, choice):
        for name in ('history.sqlite3', 'batches.sqlite3'):
            path = self.target / name
            if path.is_file():
                with _connect(path) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                        raise MigrationError('迁移后数据库完整性检查未通过。')
        path = self.target / 'tasks.json'
        if path.exists():
            TaskStore._read_document(path)

    def _rollback(self, db, run):
        if not re.fullmatch(r'[0-9a-f]{32}', run):
            raise MigrationError('迁移事务标识无效，未操作任何数据。')
        counts = {'reverted': 0, 'preserved': 0, 'failed': 0}
        changes = db.execute('SELECT * FROM changes WHERE run_id=? ORDER BY seq DESC', (run,)).fetchall()
        for row in changes:
            if row['applied'] == 2:
                continue
            before, after = _unbinary(json.loads(row['before_data'])), _unbinary(json.loads(row['after_data']))
            try:
                if row['kind'] in ('workspace', 'browser', 'browser_staging'):
                    # Never delete media or a profile during rollback. Move an unchanged,
                    # exclusively owned copy into this transaction's retained directory.
                    path = Path(row['target'])
                    if row['kind'] == 'browser':
                        if path.resolve() != self.target_profile.resolve():
                            raise MigrationError('浏览器回滚路径不属于本次目标，已保留文件。')
                    elif row['kind'] == 'browser_staging':
                        expected_stage = self.target_profile.parent / ('.mediaflow-migration-' + run) / 'BrowserProfile'
                        if path.resolve() != expected_stage.resolve():
                            raise MigrationError('浏览器暂存路径不属于本次迁移，已保留文件。')
                    else:
                        if path.resolve().parent != self.target / 'Recovered' / '.stream-downloads':
                            raise MigrationError('工作区回滚路径不属于本版恢复目录，已保留文件。')
                        try:
                            uuid.UUID(path.name)
                        except ValueError:
                            raise MigrationError('工作区回滚标识无效，已保留文件。') from None
                    if path.exists():
                        expected = after.get('hashes', {}) if row['kind'] in ('browser', 'browser_staging') else after
                        actual = {}
                        for file in safe_tree(path):
                            with file.open('rb') as stream:
                                actual[file.relative_to(path).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
                        if not expected or actual != expected:
                            counts['preserved'] += 1
                            continue
                        destination = self.root / run / 'retained' / (row['kind'] + '-' + path.name)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        path.rename(destination)
                else:
                    # A crash can occur before the prepared write, or after its fsync
                    # but before the applied bit. Both cases are resolved by value.
                    if self._current_change(row['kind'], row['target']) != before:
                        self._write_change(row['kind'], row['target'], after, before)
                db.execute('UPDATE changes SET applied=2 WHERE seq=?', (row['seq'],))
                db.commit()
                counts['reverted'] += 1
            except MigrationError:
                counts['preserved'] += 1
            except (OSError, ValueError, sqlite3.Error):
                counts['failed'] += 1
        if not counts['preserved'] and not counts['failed']:
            db.execute('DELETE FROM imports WHERE run_id=?', (run,))
            db.commit()
        return counts

    def rollback(self, run_id, confirmed=False, cancel_event=None, progress=None):
        if not confirmed:
            raise MigrationError('回滚尚未确认。')
        with self._mutex, DataDirectoryLease(self.target), closing(self._ledger()) as db:
            row = db.execute('SELECT report FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise MigrationError('未找到本次迁移记录。')
            report = json.loads(row[0])
            outcome = self._rollback(db, run_id)
            report.update(rollback=outcome, status='rollback_partial' if outcome['preserved'] or outcome['failed'] else 'rolled_back')
            self._verify_targets({})
            return self._report(db, report)

    def recover_interrupted(self):
        results = []
        for report in self.list_runs():
            if report['status'] in ('preparing', 'running'):
                results.append(self.rollback(report['id'], confirmed=True))
        return results
