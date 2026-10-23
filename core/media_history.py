"""与恢复队列隔离的媒体历史；仅保存白名单元数据，不保存下载访问上下文。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from utils.security import Redactor


_APPLICATION_ID = 0x4D464831  # MFH1
_SCHEMA_VERSION = 1
_TASK_STATES = frozenset(('completed', 'failed', 'cancelled'))
_VALIDATION_STATES = frozenset(('valid', 'unverified', 'failed'))
_FILE_STATES = frozenset(('exists', 'missing', 'inaccessible', 'unverified', 'invalid'))
_MEDIA_TYPES = frozenset(('video', 'audio', 'unknown'))
_NAME = re.compile(r'^[a-zA-Z0-9_,]{1,160}$')
_KEY = re.compile(r'^[a-zA-Z0-9_.:-]{1,128}$')
_SITE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}$')
_URL = re.compile(r'(?:https?|ftp)://[^\s<>"\']+|(?<![\w:])//[^\s<>"\']+', re.I)
_SENSITIVE = re.compile(r'cookie|auth|token|sign|secret|password|credential|session|expire|policy|key.?pair|x-amz|x-goog', re.I)
_MEDIA_PATH = re.compile(r'\.(?:m3u8|mpd|mp4|m4v|mov|webm|mkv|flv|ts|m4s|mp3|m4a|aac|ogg|opus|wav|flac)(?:/|$)', re.I)
_PAGE_IDS = frozenset(('modal_id', 'p', 'bvid', 'aid', 'cid', 'id', 'v', 'video_id', 'aweme_id'))
_PROGRESS_NUMBERS = frozenset(('percent', 'downloaded_bytes', 'total_bytes', 'total_bytes_estimate',
                               'fragment_count', 'fragment_index', 'completed_fragments',
                               'total_fragments', 'retry_count', 'retry_attempt'))
_FILE_COLUMNS = ('path', 'thumbnail_path', 'media_type', 'size', 'duration', 'width', 'height',
                 'container', 'video_codec', 'audio_codec', 'validation_state',
                 'validation_reason', 'validation', 'file_state')
_METADATA_COLUMNS = ('media_type', 'size', 'duration', 'width', 'height', 'container',
                     'video_codec', 'audio_codec')
_TASK_COLUMNS = ('queue_id', 'title', 'source_site', 'source_url', 'task_state', 'error', 'progress',
                 'started_at', 'finished_at', 'cancelled_at')
_PUBLIC_COLUMNS = ('id', 'task_key', *_TASK_COLUMNS, *_FILE_COLUMNS)
_SELECT = 'e.id, t.task_key, ' + ', '.join('t.' + key for key in _TASK_COLUMNS) + ', ' + ', '.join('e.' + key for key in _FILE_COLUMNS)


class HistoryStoreError(RuntimeError):
    """可直接显示给用户的存储错误，不包含 SQLite 原始内容或敏感数据。"""


def _number(value, *, integer=False, maximum=2 ** 63 - 1):
    if type(value) not in (int, float) or value < 0 or value > maximum or not math.isfinite(value):
        return 0 if integer else 0.0
    return int(value) if integer else float(value)


def _text(value, length=512, redactor=None):
    if not isinstance(value, str):
        return ''
    value = value[:32768]
    value = (redactor or Redactor()).redact(value)
    value = _URL.sub('[链接已隐藏]', value)
    value = re.sub(r'(?i)\b(password|passwd|token|secret|signature|api[-_]?key)(\s*[:=]\s*)[^\s;,]+',
                   r'\1\2********', value)
    value = ''.join(character for character in value if ord(character) >= 32 or character in '\n\t')
    return value.strip()[:length]


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        return ''
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return ''
    return parsed.isoformat(timespec='milliseconds')


def _path(value):
    try:
        value = os.fspath(value)
    except TypeError:
        return ''
    if (not isinstance(value, str) or len(value) > 32767 or not value
            or any(ord(character) < 32 for character in value)
            or re.match(r'^\w+://', value) or value.startswith(('\\\\?\\', '\\\\.\\'))):
        return ''
    # 不做 resolve/stat；纯映射和数据库查询不得在 UI 线程隐式访问磁盘。
    if ntpath.isabs(value):
        return ntpath.normpath(value) if '\\' in value or re.match(r'^[a-zA-Z]:', value) else os.path.normpath(value)
    if os.path.isabs(value):
        return os.path.normpath(value)
    return ''


def _site(value):
    if isinstance(value, str) and _SITE.fullmatch(value):
        return value.lower().removeprefix('www.')
    return ''


def _hostname(value):
    try:
        parsed = urlsplit(value if isinstance(value, str) else '')
        return _site(parsed.hostname or '')
    except ValueError:
        return ''


def _source_url(value):
    """仅保留无鉴权的网页地址和有限的公开作品 ID，不保留直接媒体链接。"""
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return ''
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        port = parsed.port
        if parsed.scheme not in ('http', 'https') or not _site(host) or parsed.username or parsed.password:
            return ''
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=64)
        path = unquote(parsed.path)
    except (ValueError, UnicodeError):
        return ''
    if (any(_SENSITIVE.search(key) for key, _ in query) or _MEDIA_PATH.search(path)
            or _SENSITIVE.search(path) or re.search(r'(?i)/(?:api|cdn|stream|videoplayback)(?:/|$)', path)
            or any(ord(c) < 32 for c in path)):
        return ''
    host = host.lower()
    # 公共网页的长作品 ID 与 CDN 路径中的不透明签名分开处理。
    known_page = ((host == 'douyin.com' or host.endswith('.douyin.com'))
                  and (path == '/user/self' or re.fullmatch(r'/video/\d{10,30}/?', path)))
    known_page = known_page or ((host == 'bilibili.com' or host.endswith('.bilibili.com'))
                                and re.fullmatch(r'/video/(?:BV[a-zA-Z0-9]{10,20}|av\d{1,20})/?', path))
    known_page = known_page or ((host == 'rou.video' or host.endswith('.rou.video'))
                                and re.fullmatch(r'/v/[a-zA-Z0-9_-]{5,80}/?', path))
    if not known_page:
        if (any(len(segment) > 80 or re.fullmatch(r'[a-zA-Z0-9_-]{32,}', segment)
                for segment in path.split('/'))
                or re.search(r'(?:^|\.)(?:bilivideo|douyinvod|bytecdn|akamaized)\.', host)
                or re.search(r'(?i)\.(?:video|audio|media)-?cdn\.', host)):
            return ''
    safe_query = [(key, item) for key, item in query
                  if key in _PAGE_IDS and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', item)]
    netloc = host + (':' + str(port) if port is not None else '')
    return urlunsplit((parsed.scheme, netloc, parsed.path or '/', urlencode(safe_query), ''))


def _validation(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    name = value.get('format_name')
    if isinstance(name, str) and _NAME.fullmatch(name):
        result['format_name'] = name.lower()
    for key in ('size', 'duration', 'mtime_ns'):
        if key in value:
            result[key] = _number(value[key], integer=key != 'duration')
    for role in ('video', 'audio', 'subtitle'):
        streams = value.get(role + '_streams')
        if not isinstance(streams, list):
            continue
        result[role + '_streams'] = []
        for stream in streams[:128]:
            if not isinstance(stream, dict) or stream.get('codec_type') not in (None, role):
                continue
            item = {'codec_type': role}
            name = stream.get('codec_name')
            if isinstance(name, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,80}', name):
                item['codec_name'] = name.lower()
            for key in ('index', 'width', 'height', 'channels', 'sample_rate', 'bit_rate'):
                if key in stream:
                    item[key] = _number(stream[key], integer=True, maximum=2 ** 53)
            from core.media_tracks import language_code
            if language_code(stream.get('language')):
                item['language'] = language_code(stream['language'])
            for key in ('title', 'channel_layout'):
                if isinstance(stream.get(key), str):
                    item[key] = _text(stream[key], 160 if key == 'title' else 80)
            for key in ('default', 'forced'):
                if type(stream.get(key)) is bool:
                    item[key] = stream[key]
            if item.get('codec_name') or item.get('width') or item.get('height'):
                result[role + '_streams'].append(item)
    if type(value.get('full_decode')) is bool:
        result['full_decode'] = value['full_decode']
    elif isinstance(value.get('full_decode'), dict):
        decode = value['full_decode']
        safe = {}
        if decode.get('status') in ('passed', 'failed', 'cancelled', 'timeout', 'unavailable', 'skipped'):
            safe['status'] = decode['status']
        for key in ('progress_end', 'streams_read'):
            if type(decode.get(key)) is bool:
                safe[key] = decode[key]
        if isinstance(decode.get('streams_read'), list):
            safe['streams_read'] = [role for role in decode['streams_read'][:256] if role in ('video', 'audio')]
        elif type(decode.get('streams_read')) is int:
            safe['streams_read'] = _number(decode['streams_read'], integer=True, maximum=256)
        for key in ('decoded_frames', 'expected_frames'):
            if key in decode:
                safe[key] = _number(decode[key], integer=True)
        if type(decode.get('return_code')) is int and -65535 <= decode['return_code'] <= 65535:
            safe['return_code'] = decode['return_code']
        if safe:
            result['full_decode'] = safe
    checked_at = _time(value.get('checked_at'))
    if checked_at:
        result['checked_at'] = checked_at
    sidecars = value.get('subtitle_files')
    if isinstance(sidecars, list):
        result['subtitle_files'] = []
        for item in sidecars[:32]:
            if not isinstance(item, dict) or not _path(item.get('path')):
                continue
            from core.media_tracks import language_code
            entry = {'path': _path(item['path']),
                'language': language_code(item.get('language')), 'format': _text(item.get('format'), 16),
                'status': item.get('status') if item.get('status') in ('verified', 'missing', 'inaccessible', 'changed') else 'unverified'}
            if isinstance(item.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
                entry['sha256'] = item['sha256']
                entry['size'] = _number(item.get('size'), integer=True)
            result['subtitle_files'].append(entry)
    checks = value.get('subtitle_check')
    if isinstance(checks, dict) and checks.get('status') in ('passed', 'failed', 'not_checked', 'none'):
        result['subtitle_check'] = {'status': checks['status'], 'count': _number(checks.get('count', 0), integer=True, maximum=256)}
        result['subtitle_check']['reason'] = _text(checks.get('reason'), 500)
        if isinstance(checks.get('streams'), list):
            result['subtitle_check']['streams'] = [{
                'index': _number(item.get('index'), integer=True, maximum=65535),
                'status': item['status'], 'method': item['method']}
                for item in checks['streams'][:128] if isinstance(item, dict)
                and item.get('status') in ('passed', 'failed', 'not_checked')
                and item.get('method') in ('text_decode', 'packet_read', 'none')]
    # 与读取端的 256 KiB 上限保持一致，不能写出自己无法读取的大型记录。
    while len(json.dumps(result, ensure_ascii=False)) > 240000:
        if result.get('subtitle_files'):
            result['subtitle_files'].pop()
        else:
            largest = max(('video_streams', 'audio_streams', 'subtitle_streams'), key=lambda key: len(result.get(key, [])))
            if not result.get(largest):
                break
            result[largest].pop()
    return result


def _validation_metadata(validation):
    videos = validation.get('video_streams') or []
    audios = validation.get('audio_streams') or []
    main_video = max(videos, key=lambda stream: stream.get('width', 0) * stream.get('height', 0), default={})
    return {'media_type': 'video' if videos else 'audio' if audios else 'unknown',
            'size': validation.get('size', 0), 'duration': validation.get('duration', 0.0),
            'width': main_video.get('width', 0), 'height': main_video.get('height', 0),
            'container': validation.get('format_name', ''),
            'video_codec': main_video.get('codec_name', ''),
            'audio_codec': audios[0].get('codec_name', '') if audios else ''}


def _validated(validation):
    return bool(validation.get('format_name') and validation.get('size', 0) > 0
                and (validation.get('video_streams') or validation.get('audio_streams')))


def _progress(value):
    if not isinstance(value, dict):
        return {}
    result = {key: _number(item) for key, item in value.items()
              if key in _PROGRESS_NUMBERS and type(item) in (int, float)
              and 0 <= item <= 2 ** 63 - 1 and math.isfinite(item)}
    if 'percent' in result:
        result['percent'] = min(100.0, result['percent'])
    return result


def _redactor(request):
    headers = {str(key): str(value) for key, value in (getattr(request, 'headers', None) or {}).items()}
    body = getattr(request, 'body', None)
    if isinstance(body, bytes):
        body = body[:65536].decode('utf-8', errors='replace')
    if isinstance(body, str):
        values = [body]
        try:
            values.extend(item for _, item in parse_qsl(body, max_num_fields=256) if item)
        except ValueError:
            pass
        try:
            payload = json.loads(body)
        except (ValueError, TypeError, RecursionError):
            payload = None

        def collect(item, depth=0):
            if depth > 12 or len(values) > 512:
                return
            if isinstance(item, dict):
                for child in item.values():
                    collect(child, depth + 1)
            elif isinstance(item, list):
                for child in item:
                    collect(child, depth + 1)
            elif item is not None:
                values.append(str(item))

        collect(payload)
        headers.update({'x-body-secret-' + str(index): item for index, item in enumerate(values) if item})
    metadata = getattr(request, 'media_metadata', None) or {}
    urls = [getattr(request, key, '') for key in ('url', 'transport_url', 'manifest_url')]
    if isinstance(metadata, dict):
        urls.append(metadata.get('page_url', ''))
    return Redactor(headers, [value for value in urls if isinstance(value, str)])


def snapshot_job(job, started_at=''):
    """纯映射：不探测文件、不访问网络，暂停和恢复上下文占位不会冒充终态。"""
    state = getattr(job, 'state', '')
    result = getattr(job, 'result', None) or {}
    if not isinstance(result, dict):
        result = {}
    if state == 'waiting_context':
        if (getattr(job, 'context_error', '') or result.get('success') or result.get('cancelled')
                or result.get('error_code') == 'CONTEXT_UNAVAILABLE'
                or not (result.get('error') or result.get('error_code'))):
            return {}
        state = 'failed'
    if state not in _TASK_STATES:
        return {}
    request = getattr(job, 'request', None)
    metadata = getattr(request, 'media_metadata', None)
    metadata = metadata if isinstance(metadata, dict) else {}
    redactor = _redactor(request)
    temporary = getattr(request, 'temporary_directory', '') or ''
    basename = ntpath.basename(str(temporary).replace('/', '\\')).lower()
    if re.fullmatch(r'[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', basename):
        identity = 'workspace:' + basename.replace('-', '')
    else:
        identity = json.dumps([str(temporary).replace('/', '\\').casefold(),
                               getattr(job, 'created_at', ''), getattr(job, 'id', 0)], ensure_ascii=False)
    page_url = metadata.get('page_url', '')
    finished_at = _time(getattr(job, 'updated_at', '')) or _time(getattr(job, 'created_at', ''))
    validations = result.get('output_validation')
    validations = validations if isinstance(validations, list) else []
    files = result.get('files')
    files = files if isinstance(files, (list, tuple)) else []
    safe_files = []
    for index, value in enumerate(files[:1024]):
        path = _path(value)
        if path:
            safe_files.append({'path': path, 'validation': _validation(validations[index]) if index < len(validations) else {}})
    organized = metadata.get('organization_values')
    original_title = (organized.get('title', '').strip() if isinstance(organized, dict)
                      and isinstance(organized.get('title'), str) else '')
    return {'task_key': hashlib.sha256(identity.encode('utf-8')).hexdigest(),
            'queue_id': _number(getattr(job, 'id', 0), integer=True),
            'title': _text(original_title or getattr(job, 'name', '') or metadata.get('title', ''), redactor=redactor),
            'source_site': _hostname(page_url) or _hostname(getattr(request, 'url', '')),
            'source_url': _source_url(page_url), 'task_state': state,
            'error': '' if state == 'completed' else _text(result.get('error') or getattr(job, 'status', ''), 3000, redactor),
            'progress': _progress(getattr(job, 'progress', None)),
            'started_at': _time(started_at) or _time(getattr(job, 'created_at', '')),
            'finished_at': finished_at, 'cancelled_at': finished_at if state == 'cancelled' else '',
            'files': safe_files}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _database_message(error):
    if isinstance(error, sqlite3.OperationalError) and any(word in str(error).lower() for word in ('locked', 'busy')):
        return '媒体历史数据库正被占用，请稍后重试；下载队列和已有文件不受影响。'
    if any(word in str(error).lower() for word in ('malformed', 'not a database', 'corrupt')):
        return '媒体历史数据库已损坏，原文件已保留；请备份后恢复数据库。下载恢复数据不受影响。'
    return '无法读写媒体历史数据库，请检查目录权限、可用空间和文件状态；已有数据未被重建或覆盖。'


class HistoryStore:
    """每个后台工作线程独占一个连接；分页 SQL 不扫描媒体或载入缩略图。"""

    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        self._connection = None
        try:
            exists = self.path.exists()
            if exists and (not self.path.is_file() or self.path.stat().st_size == 0):
                raise HistoryStoreError('媒体历史数据库为空或路径无效，原文件已保留；请备份后检查存储目录。')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=1.0, isolation_level=None)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            if connection.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
                raise HistoryStoreError('媒体历史数据库校验失败，原文件已保留；请备份后恢复数据库。')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            application = connection.execute('PRAGMA application_id').fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if version == 0 and application == 0 and not tables:
                self._create_schema()
            elif version != _SCHEMA_VERSION or application != _APPLICATION_ID:
                raise HistoryStoreError('媒体历史数据库版本不兼容，原文件已保留；请使用匹配版本或恢复备份。')
            else:
                self._check_schema(tables)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=NORMAL')
            # 增量附属表不改变原有任务/历史结构；记录与本地整理日志的绑定和原子提交点。
            connection.execute('''CREATE TABLE IF NOT EXISTS organization_transactions (
                transaction_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                committed INTEGER NOT NULL DEFAULT 0)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS storage_cleanup_transactions (
                transaction_id TEXT PRIMARY KEY, digest TEXT NOT NULL)''')
            # Independent import selections share the original tables without exposing
            # history-only imports in the media-library view (old rows remain visible).
            connection.execute('''CREATE TABLE IF NOT EXISTS migration_visibility (
                entry_id INTEGER PRIMARY KEY, library INTEGER NOT NULL, history INTEGER NOT NULL)''')
        except (sqlite3.Error, OSError, ValueError) as error:
            self.close()
            raise HistoryStoreError(_database_message(error)) from None
        except HistoryStoreError:
            self.close()
            raise

    def _create_schema(self):
        with self._operation(write=True) as database:
            database.execute('''CREATE TABLE tasks (
                task_key TEXT PRIMARY KEY, queue_id INTEGER NOT NULL, title TEXT NOT NULL,
                source_site TEXT NOT NULL, source_url TEXT NOT NULL, task_state TEXT NOT NULL,
                error TEXT NOT NULL, progress TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL, cancelled_at TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0)''')
            database.execute('''CREATE TABLE entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL REFERENCES tasks(task_key),
                slot INTEGER NOT NULL, path TEXT NOT NULL, file_name TEXT NOT NULL, thumbnail_path TEXT NOT NULL,
                media_type TEXT NOT NULL, size INTEGER NOT NULL, duration REAL NOT NULL,
                width INTEGER NOT NULL, height INTEGER NOT NULL, container TEXT NOT NULL,
                video_codec TEXT NOT NULL, audio_codec TEXT NOT NULL, validation_state TEXT NOT NULL,
                validation_reason TEXT NOT NULL, validation TEXT NOT NULL, file_state TEXT NOT NULL,
                custom_fields TEXT NOT NULL DEFAULT '[]', UNIQUE(task_key, slot))''')
            database.execute('''CREATE TABLE tombstones (
                task_key TEXT NOT NULL, slot INTEGER NOT NULL, PRIMARY KEY(task_key, slot))''')
            for name, fields in (('date', 'finished_at'), ('state', 'task_state'), ('source', 'source_site')):
                database.execute('CREATE INDEX tasks_' + name + ' ON tasks(' + fields + ')')
            for name, fields in (('size', 'size'), ('duration', 'duration'), ('resolution', '(width * height)'),
                                 ('type', 'media_type'), ('name', 'file_name COLLATE NOCASE')):
                database.execute('CREATE INDEX entries_' + name + ' ON entries(' + fields + ')')
            database.execute('PRAGMA application_id=' + str(_APPLICATION_ID))
            database.execute('PRAGMA user_version=' + str(_SCHEMA_VERSION))

    def _check_schema(self, tables):
        expected = {'tasks': {'task_key', *_TASK_COLUMNS, 'removed'},
                    'entries': {'id', 'task_key', 'slot', 'file_name', *_FILE_COLUMNS, 'custom_fields'},
                    'tombstones': {'task_key', 'slot'}}
        for table, columns in expected.items():
            if table not in tables or not columns <= {row[1] for row in self._connection.execute('PRAGMA table_info(' + table + ')')}:
                raise HistoryStoreError('媒体历史数据库结构不完整，原文件已保留；请备份后恢复数据库。')

    @contextmanager
    def _operation(self, write=False):
        if self._connection is None:
            raise HistoryStoreError('媒体历史数据库已关闭。')
        try:
            if write:
                self._connection.execute('BEGIN IMMEDIATE')
            yield self._connection
            if write:
                self._connection.commit()
        except sqlite3.Error as error:
            if write and self._connection.in_transaction:
                self._connection.rollback()
            raise HistoryStoreError(_database_message(error)) from None
        except Exception:
            if write and self._connection.in_transaction:
                self._connection.rollback()
            raise

    def close(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _task(snapshot):
        return {'queue_id': _number(snapshot.get('queue_id'), integer=True),
                'title': _text(snapshot.get('title')), 'source_site': _site(snapshot.get('source_site')),
                'source_url': _source_url(snapshot.get('source_url')), 'task_state': snapshot['task_state'],
                'error': _text(snapshot.get('error'), 3000), 'progress': _json(_progress(snapshot.get('progress'))),
                'started_at': _time(snapshot.get('started_at')), 'finished_at': _time(snapshot.get('finished_at')),
                'cancelled_at': _time(snapshot.get('cancelled_at'))}

    @staticmethod
    def _file(item):
        validation = _validation(item.get('validation'))
        valid = _validated(validation)
        state = item.get('validation_state')
        state = state if state in _VALIDATION_STATES else 'valid' if valid else 'unverified'
        if state == 'valid' and not valid:
            state = 'unverified'
        values = {'path': _path(item.get('path')), 'thumbnail_path': _path(item.get('thumbnail_path')),
                  **_validation_metadata(validation), 'validation_state': state,
                  'validation_reason': _text(item.get('validation_reason'), 2000), 'validation': _json(validation),
                  'file_state': 'invalid' if state == 'failed' else 'exists' if state == 'valid' else 'unverified'}
        return values

    def record(self, snapshot, only_if_absent=False):
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('task_key'), str)
                or not _KEY.fullmatch(snapshot['task_key']) or snapshot.get('task_state') not in _TASK_STATES):
            return False
        key = snapshot['task_key']
        task = self._task(snapshot)
        files = snapshot.get('files')
        files = files if isinstance(files, (list, tuple)) else []
        if len(files) > 1024:
            raise HistoryStoreError('单个任务的媒体文件数量超过历史存储限制，请分批保存。')
        files = [self._file(item) for item in files if isinstance(item, dict)] or [self._file({})]
        with self._operation(write=True) as database:
            previous = database.execute('SELECT removed FROM tasks WHERE task_key=?', (key,)).fetchone()
            if previous is not None and (only_if_absent or previous['removed']):
                return False
            if previous is None:
                database.execute('INSERT INTO tasks (task_key, ' + ','.join(_TASK_COLUMNS) + ') VALUES ('
                                 + ','.join('?' for _ in range(len(_TASK_COLUMNS) + 1)) + ')',
                                 (key, *(task[column] for column in _TASK_COLUMNS)))
            else:
                database.execute('UPDATE tasks SET ' + ','.join(column + '=?' for column in _TASK_COLUMNS) + ' WHERE task_key=?',
                                 (*(task[column] for column in _TASK_COLUMNS), key))
            for slot, values in enumerate(files):
                if database.execute('SELECT 1 FROM tombstones WHERE task_key=? AND slot=?', (key, slot)).fetchone():
                    continue
                current = database.execute('SELECT * FROM entries WHERE task_key=? AND slot=?', (key, slot)).fetchone()
                if current is None:
                    database.execute('INSERT INTO entries (task_key, slot, file_name,' + ','.join(_FILE_COLUMNS) + ') VALUES ('
                                     + ','.join('?' for _ in range(len(_FILE_COLUMNS) + 3)) + ')',
                                     (key, slot, ntpath.basename(values['path']), *(values[column] for column in _FILE_COLUMNS)))
                else:
                    custom = self._decode(current['custom_fields'], list)
                    columns = [column for column in _FILE_COLUMNS if column not in custom]
                    if not values['thumbnail_path'] and current['thumbnail_path']:
                        columns = [column for column in columns if column != 'thumbnail_path']
                    if values['validation'] == '{}' and current['validation'] != '{}':
                        preserve = {*_METADATA_COLUMNS, 'validation', 'validation_state', 'validation_reason'}
                        columns = [column for column in columns if column not in preserve]
                    if columns:
                        database.execute('UPDATE entries SET ' + ','.join(column + '=?' for column in columns) + ' WHERE id=?',
                                         (*(values[column] for column in columns), current['id']))
                        if 'path' in columns:
                            database.execute('UPDATE entries SET file_name=? WHERE id=?', (ntpath.basename(values['path']), current['id']))
            return True

    @staticmethod
    def _decode(value, expected):
        try:
            if len(value) > 262144:
                raise ValueError
            decoded = json.loads(value)
            if not isinstance(decoded, expected):
                raise ValueError
            if expected is list and any(not isinstance(item, str) or item not in _FILE_COLUMNS for item in decoded):
                raise ValueError
            return decoded
        except (TypeError, ValueError, RecursionError):
            raise HistoryStoreError('媒体历史记录内容损坏，原数据库已保留；请备份后恢复数据库。') from None

    def _public(self, row):
        result = {column: row[column] for column in _PUBLIC_COLUMNS}
        result['validation'] = _validation(self._decode(result['validation'], dict))
        result['progress'] = _progress(self._decode(result['progress'], dict))
        return result

    def query(self, view='library', search='', media_type='', source_site='', sort='date', offset=0, limit=50):
        view_where = "t.removed=0" + (" AND t.task_state='completed'" if view == 'library' else '')
        flag = 'library' if view == 'library' else 'history'
        view_where += ' AND NOT EXISTS (SELECT 1 FROM migration_visibility mv WHERE mv.entry_id=e.id AND mv.' + flag + '=0)'
        conditions = [view_where]
        params = []
        search = _text(search, 512)
        if search:
            search = search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.append("(e.file_name LIKE ? ESCAPE '\\' OR t.title LIKE ? ESCAPE '\\')")
            params.extend(('%' + search + '%', '%' + search + '%'))
        if media_type in _MEDIA_TYPES:
            conditions.append('e.media_type=?')
            params.append(media_type)
        if source_site:
            conditions.append('t.source_site=?')
            params.append(_site(source_site))
        where = ' AND '.join(conditions)
        order = {'date': 't.finished_at', 'size': 'e.size', 'duration': 'e.duration',
                 'resolution': '(e.width * e.height)'}.get(sort, 't.finished_at')
        offset = _number(offset, integer=True, maximum=2 ** 31 - 1)
        limit = max(1, min(100, _number(limit, integer=True, maximum=2 ** 31 - 1)))
        with self._operation() as database:
            sources = [row[0] for row in database.execute('SELECT DISTINCT t.source_site FROM entries e JOIN tasks t ON t.task_key=e.task_key WHERE '
                                                         + view_where + " AND t.source_site<>'' ORDER BY t.source_site COLLATE NOCASE")]
            total = database.execute('SELECT COUNT(*) FROM entries e JOIN tasks t ON t.task_key=e.task_key WHERE ' + where, params).fetchone()[0]
            rows = database.execute('SELECT ' + _SELECT + ' FROM entries e JOIN tasks t ON t.task_key=e.task_key WHERE '
                                    + where + ' ORDER BY ' + order + ' DESC, t.finished_at DESC, e.id DESC LIMIT ? OFFSET ?',
                                    (*params, limit, offset)).fetchall()
            return {'items': [self._public(row) for row in rows], 'total': total, 'sources': sources}

    def get(self, entry_id):
        with self._operation() as database:
            row = database.execute('SELECT ' + _SELECT + ' FROM entries e JOIN tasks t ON t.task_key=e.task_key WHERE e.id=? AND t.removed=0',
                                   (_number(entry_id, integer=True),)).fetchone()
            return self._public(row) if row is not None else None

    def update(self, entry_id, fields):
        if not isinstance(fields, dict):
            return False
        with self._operation(write=True) as database:
            current = database.execute('SELECT * FROM entries WHERE id=?', (_number(entry_id, integer=True),)).fetchone()
            if current is None:
                return False
            values = {}
            if 'validation' in fields:
                validation = _validation(fields['validation'])
                values.update(_validation_metadata(validation))
                values['validation'] = _json(validation)
            for column in _FILE_COLUMNS:
                if column not in fields or column == 'validation':
                    continue
                value = fields[column]
                if column in ('path', 'thumbnail_path'):
                    value = _path(value)
                elif column in ('size', 'width', 'height'):
                    value = _number(value, integer=True, maximum=65535 if column != 'size' else 2 ** 63 - 1)
                elif column == 'duration':
                    value = _number(value)
                elif column in ('container', 'video_codec', 'audio_codec'):
                    value = value.lower() if isinstance(value, str) and _NAME.fullmatch(value) else ''
                elif column == 'validation_state':
                    if value not in _VALIDATION_STATES:
                        continue
                elif column == 'file_state':
                    if value not in _FILE_STATES:
                        continue
                elif column == 'media_type':
                    if value not in _MEDIA_TYPES:
                        continue
                else:
                    value = _text(value, 2000)
                values[column] = value
            if not values:
                return False
            if values.get('validation_state') == 'valid':
                validation = self._decode(values.get('validation', current['validation']), dict)
                if not _validated(validation):
                    values['validation_state'] = 'unverified'
            custom = set(self._decode(current['custom_fields'], list))
            custom.update(values)
            # 重新定位后不能被上一位置的下载摘要和迟到缩略图回调覆盖。
            if 'path' in values:
                custom.update(_FILE_COLUMNS)
            columns = list(values)
            database.execute('UPDATE entries SET ' + ','.join(column + '=?' for column in columns)
                             + ',custom_fields=? WHERE id=?',
                             (*(values[column] for column in columns), _json(sorted(custom)), current['id']))
            if 'path' in values:
                database.execute('UPDATE entries SET file_name=? WHERE id=?', (ntpath.basename(values['path']), current['id']))
            return True

    def storage_entries(self):
        """后台取得所有现存历史引用；容量始终由扫描器重新读取磁盘。"""
        with self._operation() as database:
            rows = database.execute('SELECT ' + _SELECT + ''' FROM entries e
                JOIN tasks t ON t.task_key=e.task_key WHERE t.removed=0 ORDER BY e.id''')
            return [self._public(row) for row in rows]

    def register_storage_cleanup(self, transaction_id, digest):
        if not re.fullmatch(r'[a-f0-9]{32}', transaction_id or '') or not re.fullmatch(r'[a-f0-9]{64}', digest or ''):
            raise HistoryStoreError('清理事务标记无效，请重新预览。')
        with self._operation(write=True) as database:
            old = database.execute('SELECT digest FROM storage_cleanup_transactions WHERE transaction_id=?',
                                   (transaction_id,)).fetchone()
            if old and old[0] != digest:
                raise HistoryStoreError('清理事务不匹配，已保留全部文件和记录。')
            database.execute('INSERT OR IGNORE INTO storage_cleanup_transactions VALUES (?,?)', (transaction_id, digest))

    def storage_cleanup_transaction(self, transaction_id):
        with self._operation() as database:
            row = database.execute('SELECT digest FROM storage_cleanup_transactions WHERE transaction_id=?',
                                   (transaction_id,)).fetchone()
            return row[0] if row else ''

    def mark_storage_removed(self, paths, reason):
        """仅更新指定路径的所有引用；保留任务和其余副本的下载历史。"""
        keys = {os.path.normcase(_path(path)).casefold() for path in paths if _path(path)}
        changed = 0
        with self._operation(write=True) as database:
            rows = list(database.execute('''SELECT e.* FROM entries e JOIN tasks t
                ON t.task_key=e.task_key WHERE t.removed=0'''))
            for row in rows:
                fields = {}
                validation = self._decode(row['validation'], dict)
                if os.path.normcase(_path(row['path'])).casefold() in keys:
                    fields.update(file_state='missing', validation_state='unverified', validation_reason=_text(reason, 2000))
                for item in validation.get('subtitle_files', []):
                    if isinstance(item, dict) and os.path.normcase(_path(item.get('path'))).casefold() in keys:
                        item['status'] = 'missing'
                        fields.update(validation=_json(_validation(validation)),
                                      validation_reason=_text(reason + ' 独立字幕缺失，可重新定位或重新分析。', 2000))
                if not fields:
                    continue
                custom = set(self._decode(row['custom_fields'], list)) | set(fields)
                database.execute('UPDATE entries SET ' + ','.join(key + '=?' for key in fields)
                                 + ',custom_fields=? WHERE id=?', (*fields.values(), _json(sorted(custom)), row['id']))
                changed += 1
        return changed

    def organization_references(self, paths):
        """后台流式检查引用，不扫描媒体、不把全部历史载入内存。"""
        keys = {os.path.normcase(_path(path)).casefold(): path for path in paths if _path(path)}
        result = {path: set() for path in paths}
        with self._operation() as database:
            rows = database.execute('''SELECT e.id,e.path,e.thumbnail_path,e.validation
                FROM entries e JOIN tasks t ON t.task_key=e.task_key WHERE t.removed=0''')
            for row in rows:
                values = [row['path'], row['thumbnail_path']]
                validation = self._decode(row['validation'], dict)
                values.extend(item.get('path', '') for item in validation.get('subtitle_files', [])
                              if isinstance(item, dict))
                for path in values:
                    key = os.path.normcase(_path(path)).casefold()
                    if key in keys:
                        result[keys[key]].add(row['id'])
        return result

    def register_organization(self, transaction_id, digest):
        if not re.fullmatch(r'[a-f0-9]{32}', transaction_id or '') or not re.fullmatch(r'[a-f0-9]{64}', digest or ''):
            raise HistoryStoreError('整理事务标记无效，请重新预览。')
        with self._operation(write=True) as database:
            database.execute('INSERT INTO organization_transactions(transaction_id,digest) VALUES (?,?)',
                             (transaction_id, digest))

    def organization_transaction(self, transaction_id):
        with self._operation() as database:
            row = database.execute('SELECT digest,committed FROM organization_transactions WHERE transaction_id=?',
                                   (transaction_id,)).fetchone()
            return dict(row) if row is not None else None

    def commit_organization(self, transaction_id, digest, updates, references=None):
        """文件均已独占发布后，用一个 CAS 事务更新整组路径及提交标记。"""
        with self._operation(write=True) as database:
            transaction = database.execute('SELECT digest,committed FROM organization_transactions WHERE transaction_id=?',
                                           (transaction_id,)).fetchone()
            if transaction is None or transaction['digest'] != digest or transaction['committed']:
                raise HistoryStoreError('整理事务状态已变化，请刷新后重新预览。')
            if references is not None:
                actual = self.organization_references(list(references))
                if {path: sorted(ids) for path, ids in actual.items()} != references:
                    raise HistoryStoreError('整理提交前出现其他文件引用，原文件已保留，请重新预览。')
            for update in updates:
                row = database.execute('SELECT ' + _SELECT + ''' FROM entries e JOIN tasks t ON t.task_key=e.task_key
                    WHERE e.id=? AND t.removed=0''', (update['id'],)).fetchone()
                current = self._public(row) if row is not None else None
                if (not current or current['task_state'] != 'completed' or any(
                        current.get(key) != value for key, value in update['expected'].items())):
                    raise HistoryStoreError('历史记录在预览后发生变化，原文件已保留，请重新预览。')
            for update in updates:
                row = database.execute('SELECT custom_fields FROM entries WHERE id=?', (update['id'],)).fetchone()
                custom = set(self._decode(row['custom_fields'], list)) | set(_FILE_COLUMNS)
                database.execute('''UPDATE entries SET path=?,file_name=?,thumbnail_path=?,validation=?,
                    custom_fields=? WHERE id=?''', (_path(update['path']), ntpath.basename(update['path']),
                    _path(update['thumbnail_path']), _json(_validation(update['validation'])),
                    _json(sorted(custom)), update['id']))
            database.execute('UPDATE organization_transactions SET committed=1 WHERE transaction_id=?', (transaction_id,))
        return True

    def remove(self, entry_id):
        with self._operation(write=True) as database:
            row = database.execute('SELECT task_key, slot FROM entries WHERE id=?', (_number(entry_id, integer=True),)).fetchone()
            if row is None:
                return False
            database.execute('INSERT OR IGNORE INTO tombstones(task_key, slot) VALUES (?,?)', (row['task_key'], row['slot']))
            database.execute('DELETE FROM entries WHERE id=?', (_number(entry_id, integer=True),))
            if database.execute('SELECT 1 FROM entries WHERE task_key=? LIMIT 1', (row['task_key'],)).fetchone() is None:
                database.execute("UPDATE tasks SET removed=1,queue_id=0,title='',source_site='',source_url='',error='',progress='{}',"
                                 "started_at='',finished_at='',cancelled_at='' WHERE task_key=?", (row['task_key'],))
            return True

    def update_thumbnails(self, task_key, items):
        if not isinstance(task_key, str) or not _KEY.fullmatch(task_key) or not isinstance(items, list):
            return False
        changed = False
        with self._operation(write=True) as database:
            for slot, item in enumerate(items[:1024]):
                if not isinstance(item, dict) or item.get('status') not in ('generated', 'existing'):
                    continue
                path = _path(item.get('path'))
                if not path:
                    continue
                row = database.execute('SELECT id, thumbnail_path, custom_fields FROM entries WHERE task_key=? AND slot=?', (task_key, slot)).fetchone()
                if (row is None or 'thumbnail_path' in self._decode(row['custom_fields'], list)
                        or row['thumbnail_path'] == path):
                    continue
                database.execute('UPDATE entries SET thumbnail_path=? WHERE id=?', (path, row['id']))
                changed = True
        return changed
