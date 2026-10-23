"""独立的批次 SQLite 存储；来源网址仅使用现有当前用户 DPAPI 加密。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from core.batch_input import _validate_source_url
from core.task_store import TaskStore
from utils.security import Redactor


_VERSION = 1
_APPLICATION_ID = 0x4D464254
_STATES = frozenset(('waiting_discovery', 'analyzing', 'waiting_selection', 'waiting_download',
                     'downloading', 'merging', 'verifying', 'completed', 'paused', 'cancelled',
                     'failed', 'waiting_context'))
_PROGRESS = frozenset(('percent', 'downloaded_bytes', 'total_bytes', 'total_bytes_estimate'))
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)
_SECRET = re.compile(r'(?i)\b(token|password|passwd|secret|api[-_]?key|session(?:id)?|'
                     r'signature|authorization|cookie|set-cookie|x-auth-token|x-api-key)'
                     r'(\s*[:=]\s*)[^\s,;]+')


class BatchStoreError(RuntimeError):
    pass


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise BatchStoreError('批次或任务标识无效，未写入批次存储。') from None


def _integer(value, default=0, maximum=2 ** 63 - 1):
    return min(maximum, max(0, value)) if type(value) is int else default


def _safe_text(value, maximum=1000, url=''):
    if not isinstance(value, str):
        return ''
    text = Redactor(urls=[url] if url else []).redact(value[:maximum * 4])
    text = _URL.sub('[来源地址已隐藏]', text)
    text = _SECRET.sub(r'\1\2********', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer ********', text)
    return ''.join(character for character in text if ord(character) >= 32 or character in '\n\t')[:maximum]


def _local_path(value):
    if not isinstance(value, str) or len(value) > 32767 or '\x00' in value:
        return ''
    if re.match(r'(?i)^\s*[a-z][a-z0-9+.-]*://', value):
        return ''
    return value


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        return ''
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return value
    except ValueError:
        return ''


def _options(value):
    value = value if isinstance(value, dict) else {}
    quality = value.get('quality')
    naming = value.get('naming')
    concurrent = value.get('max_concurrent')
    retry = value.get('retry_limit')
    seconds = value.get('discovery_seconds')
    from core.media_tracks import normalize_preferences
    result = {'output_directory': _local_path(value.get('output_directory', '')),
            'quality': quality if quality in ('auto', 'highest', 'balanced', 'smallest') else 'auto',
            'naming': naming if naming in ('title', 'index_title', 'date_title') else 'title',
            'max_concurrent': min(10, max(1, concurrent)) if type(concurrent) is int else 3,
            'retry_limit': min(3, max(0, retry)) if type(retry) is int else 3,
            'discovery_seconds': min(120, max(5, seconds)) if type(seconds) is int else 60}
    if value.get('track_preferences'):
        result['track_preferences'] = normalize_preferences(value['track_preferences'])
    if value.get('organization'):
        from core.output_organization import normalize_options
        result['organization'] = normalize_options(value['organization'])
    if value.get('collection_name'):
        result['collection_name'] = _safe_text(value['collection_name'], 100)
        result['collection_directory'] = _local_path(value.get('collection_directory', ''))
    return result


def _batch(value):
    if not isinstance(value, dict):
        raise BatchStoreError('批次数据格式无效，未写入批次存储。')
    return {'id': _uuid(value.get('id')), 'title': _safe_text(value.get('title'), 500),
            'created_at': _time(value.get('created_at')), 'updated_at': _time(value.get('updated_at')),
            'paused': value.get('paused') is True, 'options': _options(value.get('options'))}


def _item(value, url=''):
    if not isinstance(value, dict):
        raise BatchStoreError('批次任务数据格式无效，未写入批次存储。')
    progress = value.get('progress')
    safe_progress = {}
    if isinstance(progress, dict):
        for key in _PROGRESS:
            number = progress.get(key)
            if type(number) in (int, float) and 0 <= number <= 2 ** 63 - 1 and math.isfinite(number):
                safe_progress[key] = min(100.0, number) if key == 'percent' else number
    site = value.get('source_site')
    if not isinstance(site, str) or not re.fullmatch(r'[A-Za-z0-9.\-:\[\]]{1,253}', site):
        site = ''
    queue_key = value.get('queue_key')
    if not isinstance(queue_key, str) or not re.fullmatch(r'[a-fA-F0-9]{32,128}', queue_key):
        queue_key = ''
    state = value.get('state')
    return {'id': _uuid(value.get('id')), 'batch_id': _uuid(value.get('batch_id')),
            'ordinal': _integer(value.get('ordinal'), maximum=1000000),
            'title': _safe_text(value.get('title'), 1000, url), 'source_site': site,
            'state': state if state in _STATES else 'waiting_context',
            'candidate_count': _integer(value.get('candidate_count'), maximum=1000000),
            'progress': safe_progress, 'queue_key': queue_key,
            'job_id': _integer(value.get('job_id')), 'error': _safe_text(value.get('error'), 4000, url),
            'output_path': _local_path(value.get('output_path')),
            'created_at': _time(value.get('created_at')), 'updated_at': _time(value.get('updated_at')),
            'attempts': _integer(value.get('attempts'), maximum=1000000),
            'context_error': _safe_text(value.get('context_error'), 1000, url),
            'pause_scope': value.get('pause_scope') if value.get('pause_scope') in ('item', 'batch') else '',
            'previous_state': value.get('previous_state') if value.get('previous_state') in _STATES else ''}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class BatchStore:
    """一个后台线程拥有一个连接；事务增量更新，不触碰活动队列快照。"""

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self._connection = None
        self._url_cache = {}
        existed = self.path.exists()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if existed and self.path.stat().st_size == 0:
                raise BatchStoreError('批次数据库为空或已损坏；原文件已保留，请先备份后再处理。')
            connection = sqlite3.connect(str(self.path), timeout=1.5)
            self._connection = connection
            connection.execute('PRAGMA busy_timeout=1500')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            application = connection.execute('PRAGMA application_id').fetchone()[0]
            if existed:
                if version != _VERSION or application != _APPLICATION_ID:
                    raise BatchStoreError('批次数据库版本无法识别；原文件已保留，请使用匹配版本的软件。')
                if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise BatchStoreError('批次数据库校验失败；原文件已保留，请先备份后再处理。')
                # 在任何写操作前确认表结构，禁止以 CREATE IF NOT EXISTS 隐藏损坏。
                connection.execute('SELECT id,metadata FROM batches LIMIT 0')
                connection.execute('SELECT id,batch_id,ordinal,url_hash,url_cipher,metadata FROM items LIMIT 0')
            else:
                with connection:
                    connection.execute('CREATE TABLE batches (id TEXT PRIMARY KEY, metadata TEXT NOT NULL)')
                    connection.execute('CREATE TABLE items (id TEXT PRIMARY KEY, batch_id TEXT NOT NULL '
                                       'REFERENCES batches(id), ordinal INTEGER NOT NULL, url_hash TEXT NOT NULL, '
                                       'url_cipher BLOB, metadata TEXT NOT NULL)')
                    connection.execute('CREATE INDEX items_batch ON items(batch_id,ordinal)')
                    connection.execute('CREATE INDEX items_url ON items(url_hash)')
                    connection.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                    connection.execute(f'PRAGMA user_version={_VERSION}')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
        except BatchStoreError:
            self.close()
            raise
        except (OSError, sqlite3.Error):
            self.close()
            raise BatchStoreError('无法打开批次存储，可能已损坏、被占用或没有访问权限；原有数据未被重建覆盖。') from None

    def close(self):
        if self._connection is not None:
            connection, self._connection = self._connection, None
            connection.close()
        self._url_cache.clear()

    def _db(self):
        if self._connection is None:
            raise BatchStoreError('批次存储已经关闭，请重新打开后操作。')
        return self._connection

    def save_batch(self, value):
        record = _batch(value)
        try:
            with self._db() as connection:
                connection.execute('INSERT INTO batches(id,metadata) VALUES(?,?) '
                                   'ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata',
                                   (record['id'], _json(record)))
        except sqlite3.Error:
            raise BatchStoreError('无法保存批次，存储被占用、磁盘空间不足或没有访问权限，请稍后重试。') from None
        return True

    def save_item(self, value):
        return self.save_items([value])

    def create_batch(self, batch, items):
        """先完整提交批次和初始任务，再允许控制器开始网络分析。"""
        record = _batch(batch)
        try:
            with self._db() as connection:
                if connection.execute('SELECT 1 FROM batches WHERE id=?', (record['id'],)).fetchone():
                    raise BatchStoreError('批次已经存在，请恢复已有批次，不能重复创建。')
                connection.execute('INSERT INTO batches(id,metadata) VALUES(?,?)',
                                   (record['id'], _json(record)))
                changes = self._write_items(connection, items, required_batch=record['id'])
            self._url_cache.update(changes)
        except sqlite3.Error:
            raise BatchStoreError('无法保存新批次，存储被占用、磁盘空间不足或没有访问权限，请稍后重试。') from None
        return True

    def save_items(self, values):
        """整组原子提交；任何一条加密/写入失败都回滚，绝不明文降级。"""
        try:
            with self._db() as connection:
                changes = self._write_items(connection, values)
            self._url_cache.update(changes)
        except sqlite3.Error:
            raise BatchStoreError('无法保存批次任务，存储被占用、磁盘空间不足或没有访问权限，请稍后重试。') from None
        return True

    def _write_items(self, connection, values, required_batch=''):
        changes = {}
        for value in values:
            if not isinstance(value, dict):
                raise BatchStoreError('批次任务数据格式无效，未写入批次存储。')
            url = value.get('url', '')
            if not isinstance(url, str):
                raise BatchStoreError('来源网址格式无效，未写入批次存储。')
            url = url.strip()
            if url:
                try:
                    if len(url.encode('utf-8')) > 2 * 1024 * 1024:
                        raise ValueError()
                    _validate_source_url(url)
                except (ValueError, RuntimeError):
                    raise BatchStoreError('来源网址格式无效，未写入批次存储。') from None
            record = _item(value, url)
            if required_batch and record['batch_id'] != required_batch:
                raise BatchStoreError('新任务不属于正在创建的批次，未保存本批次。')
            previous = connection.execute('SELECT batch_id,url_hash,url_cipher FROM items WHERE id=?',
                                          (record['id'],)).fetchone()
            if previous and previous[0] != record['batch_id']:
                raise BatchStoreError('任务已经属于其他批次，未更改已有记录。')
            if url:
                url_hash = hashlib.sha256(url.encode('utf-8')).hexdigest()
                cached = self._url_cache.get(record['id'])
                if previous and cached == (url_hash, previous[2]) and previous[1] == url_hash:
                    cipher = previous[2]
                else:
                    try:
                        cipher = TaskStore._protect(url.encode('utf-8'))
                        if not isinstance(cipher, bytes) or not cipher:
                            raise ValueError()
                    except Exception:
                        raise BatchStoreError('Windows 当前用户加密不可用，来源网址未保存；请恢复加密服务后重试。') from None
                changes[record['id']] = (url_hash, cipher)
            elif previous:
                # 解密失败后的空 URL 或只更新进度不能擦除恢复所需密文。
                url_hash, cipher = previous[1], previous[2]
            else:
                url_hash, cipher = '', None
                record['context_error'] = record['context_error'] or '来源网址不可用，需重新提供来源后分析。'
            connection.execute('INSERT INTO items(id,batch_id,ordinal,url_hash,url_cipher,metadata) '
                               'VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                               'ordinal=excluded.ordinal,url_hash=excluded.url_hash,'
                               'url_cipher=excluded.url_cipher,metadata=excluded.metadata',
                               (record['id'], record['batch_id'], record['ordinal'], url_hash,
                                sqlite3.Binary(cipher) if cipher is not None else None, _json(record)))
        return changes

    def load(self):
        connection = self._db()
        try:
            batches = [_batch(json.loads(raw)) for _, raw in
                       connection.execute('SELECT id,metadata FROM batches ORDER BY rowid')]
            items = []
            for item_id, batch_id, ordinal, url_hash, cipher, raw in connection.execute(
                    'SELECT id,batch_id,ordinal,url_hash,url_cipher,metadata FROM items ORDER BY batch_id,ordinal,rowid'):
                value = json.loads(raw)
                value.update(id=item_id, batch_id=batch_id, ordinal=ordinal)
                url = ''
                context_error = ''
                try:
                    if not cipher:
                        raise ValueError()
                    url = TaskStore._unprotect(bytes(cipher)).decode('utf-8')
                    _validate_source_url(url)
                    if hashlib.sha256(url.encode('utf-8')).hexdigest() != url_hash:
                        raise ValueError()
                    self._url_cache[item_id] = (url_hash, cipher)
                except Exception:
                    self._url_cache.pop(item_id, None)
                    url = ''
                    context_error = '无法还原当前用户的来源网址，需重新提供来源后分析；原始加密数据已保留。'
                record = _item(value, url)
                record.update(url=url, url_hash=url_hash)
                if context_error:
                    record['context_error'] = context_error
                items.append(record)
            return {'batches': batches, 'items': items}
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            raise BatchStoreError('批次存储内容无法读取，原有数据已保留；请备份后检查存储文件。') from None
