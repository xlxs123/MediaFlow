"""原子保存任务快照；请求上下文只允许由当前 Windows 用户的 DPAPI 加密。"""
from __future__ import annotations

import base64
import ctypes
from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import uuid


_STATES = frozenset(('queued', 'running', 'stopping', 'paused', 'waiting_context',
                     'completed', 'failed', 'cancelled', 'retrying', 'waiting_network', 'recovering'))
_PROGRESS_NUMBERS = frozenset(('percent', 'downloaded_bytes', 'total_bytes', 'total_bytes_estimate',
                               'speed', 'eta', 'fragment_count', 'fragment_index', 'retry_attempt',
                               'retry_count', 'retry_countdown', 'completed_fragments', 'total_fragments',
                               'cache_hits', 'verified_segments'))
_RECOVERY_STATES = frozenset(('waiting_network', 'retrying', 'resuming', 'restarting', 'recovering',
                              'merging', 'verifying', 'downloading', 'paused', 'waiting_context',
                              'resumed', 'restart', 'completed', ''))
_SAFE_REQUEST_FIELDS = frozenset(('yt_dlp_path', 'ffmpeg_path', 'ffprobe_path', 'output_directory', 'filename',
                                  'output_path', 'temporary_directory', 'output_extension', 'final_output_directory',
                                  'workspace_output_directory',
                                  'download_protocol', 'concurrent_fragments', 'segment_max_attempts',
                                  'recovery_max_attempts', 'retry_base_delay'))
_DPAPI_ENTROPY = b'MediaFlow.TaskStore.v1.current-user'


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class TaskStoreSecurityError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise TaskStoreSecurityError('当前环境没有 Windows DPAPI，任务需要重新分析媒体地址。')
    try:
        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        buffer = ctypes.create_string_buffer(data)
        entropy_buffer = ctypes.create_string_buffer(_DPAPI_ENTROPY)
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy = _DataBlob(len(_DPAPI_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = _DataBlob()
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DataBlob)]
        function.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        # 不使用 CRYPTPROTECT_LOCAL_MACHINE，确保仅当前用户可以解密；禁止凭据 UI。
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1,
                        ctypes.byref(output)):
            raise TaskStoreSecurityError('Windows DPAPI 无法保护或还原请求上下文，请重新分析媒体地址。')
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)
    except TaskStoreSecurityError:
        raise
    except Exception:
        raise TaskStoreSecurityError('Windows DPAPI 不可用，请重新分析媒体地址。') from None


def _encode(value):
    if isinstance(value, bytes):
        return {'__mediaflow_bytes__': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    # RequestContext 是 frozen dataclass；不遍历 DownloadRequest.concurrency 的锁对象。
    if hasattr(value, '__dataclass_fields__'):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    raise ValueError('请求上下文包含不支持的恢复数据类型。')


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {'__mediaflow_bytes__'}:
            return base64.b64decode(value['__mediaflow_bytes__'], validate=True)
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _safe_progress(progress):
    result = {key: value for key, value in (progress or {}).items()
              if key in _PROGRESS_NUMBERS and _number(value)}
    state = (progress or {}).get('recovery_state')
    if state in _RECOVERY_STATES:
        result['recovery_state'] = state
    tracks = (progress or {}).get('tracks')
    if isinstance(tracks, dict):
        result['tracks'] = {role: {key: value for key, value in values.items()
                                  if key in ('downloaded_bytes', 'total_bytes') and _number(value)}
                            for role, values in tracks.items()
                            if role in ('video', 'audio') and isinstance(values, dict)}
        for role, values in result['tracks'].items():
            state = tracks[role].get('state')
            if state in ('downloading', 'completed', 'waiting'):
                values['state'] = state
    return result


def _error_category(job):
    from core.errors import ErrorCode
    code = (getattr(job, 'result', None) or {}).get('error_code', '')
    allowed = {value.value for value in ErrorCode}
    allowed.update(('DISK_FULL', 'DISK_SPACE', 'PERMISSION_DENIED', 'CANCELLED', 'FILE_EXISTS',
                    'HTTP_404', 'HTTP_429', 'HTTP_500', 'HTTP_502', 'HTTP_503', 'HTTP_504',
                    'RETRY_EXHAUSTED', 'RESOURCE_CHANGED', 'INVALID_RANGE', 'CONTEXT_UNAVAILABLE'))
    return code if code in allowed else ('DOWNLOAD_FAILED' if getattr(job, 'state', '') == 'failed' else '')


class TaskStore:
    """JSON 中仅保存白名单元信息，完整 DownloadRequest 保存在 DPAPI 密文中。

    save/load 不抛出磁盘或解密异常；通过 last_error/last_warning 显式报告。
    损坏主文件回退 .bak，之后保存前将损坏原文保留为 .corrupt-*。
    """

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.backup_path = self.path.with_name(self.path.name + '.bak')
        self.last_error = ''
        self.last_warning = ''
        self._encrypted_cache = {}
        self._opaque_context = {}
        self._unreadable = False

    @staticmethod
    def _protect(data):
        return _dpapi(data)

    @staticmethod
    def _unprotect(data):
        return _dpapi(data, decrypt=True)

    @staticmethod
    def _read_document(path):
        raw = path.read_bytes()
        document = json.loads(raw.decode('utf-8'))
        if (not isinstance(document, dict) or document.get('version') != 1
                or not isinstance(document.get('tasks'), list)):
            raise ValueError('未知的任务文件格式。')
        checksum = document.get('checksum')
        payload = {key: value for key, value in document.items() if key != 'checksum'}
        if checksum != hashlib.sha256(_canonical(payload)).hexdigest():
            raise ValueError('任务快照校验失败。')
        ids = set()
        for record in document['tasks']:
            if not isinstance(record, dict) or not isinstance(record.get('metadata'), dict):
                raise ValueError('任务快照记录无效。')
            job_id = record['metadata'].get('id')
            if type(job_id) is not int or job_id < 1 or job_id in ids:
                raise ValueError('任务 ID 无效或重复。')
            ids.add(job_id)
        return raw, document

    @staticmethod
    def _atomic_write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            from core.recovery_workspace import replace_with_retry
            replace_with_retry(temporary, path)
            # Windows 不支持 fsync 目录；文件内容在原子提交前已经刷新。
            if os.name != 'nt':
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _metadata(self, job):
        request = job.request
        safe_request = {name: getattr(request, name) for name in _SAFE_REQUEST_FIELDS
                        if hasattr(request, name)}
        track_count = 0
        media = getattr(request, 'media_metadata', {}) or {}
        if isinstance(media.get('tracks'), (list, tuple)):
            track_count = len(media['tracks'])
        completed_files = []
        if job.state == 'completed':
            for value in (getattr(job, 'result', None) or {}).get('files', []):
                path = Path(value).resolve()
                from core.output_organization import final_directory
                if path.parent == final_directory(request):
                    completed_files.append(str(path))
        return {'id': job.id, 'name': job.name, 'state': job.state if job.state in _STATES else 'paused',
                'progress': _safe_progress(job.progress), 'request': safe_request,
                'created_at': getattr(job, 'created_at', '') or utc_now(),
                'updated_at': getattr(job, 'updated_at', '') or utc_now(),
                'retry_count': job.retry_count, 'track_count': track_count,
                'completed_files': completed_files,
                'last_error_category': _error_category(job)}

    def save(self, jobs):
        self.last_error = ''
        self.last_warning = ''
        security_failed = False
        try:
            records = []
            active_ids = set()
            for job in jobs:
                active_ids.add(job.id)
                metadata = self._metadata(job)
                if getattr(job, 'context_error', ''):
                    record = {'metadata': metadata, 'context_error': 'CONTEXT_UNAVAILABLE'}
                    if job.id in self._opaque_context:
                        record['request_dpapi'] = self._opaque_context[job.id]
                    records.append(record)
                    security_failed = True
                    continue
                request = {field.name: _encode(getattr(job.request, field.name))
                           for field in fields(job.request) if field.name != 'concurrency'}
                request['__last_error'] = str((getattr(job, 'result', None) or {}).get('error', ''))[-1800:]
                plaintext = _canonical(request)
                digest = hashlib.sha256(plaintext).hexdigest()
                cached = self._encrypted_cache.get(job.id)
                record = {'metadata': metadata}
                try:
                    if cached and cached[0] == digest:
                        encrypted = cached[1]
                    else:
                        encrypted = base64.b64encode(self._protect(plaintext)).decode('ascii')
                        self._encrypted_cache[job.id] = (digest, encrypted)
                    record['request_dpapi'] = encrypted
                except Exception:
                    security_failed = True
                    self._encrypted_cache.pop(job.id, None)
                    record['context_error'] = 'DPAPI_UNAVAILABLE'
                records.append(record)
            self._encrypted_cache = {key: value for key, value in self._encrypted_cache.items() if key in active_ids}
            self._opaque_context = {key: value for key, value in self._opaque_context.items() if key in active_ids}
            document = {'version': 1, 'saved_at': utc_now(), 'tasks': records}
            document['checksum'] = hashlib.sha256(_canonical(document)).hexdigest()
            data = _canonical(document)
            if self._unreadable:
                raise OSError('先前无法读取任务文件，禁止覆盖。')
            if self.path.exists():
                try:
                    raw, _ = self._read_document(self.path)
                except (ValueError, UnicodeError):
                    # 保留损坏原文，绝不将损坏数据旋转进最近有效的 .bak。
                    corrupt = self.path.with_name(self.path.name + '.corrupt-' + uuid.uuid4().hex)
                    self._atomic_write(corrupt, self.path.read_bytes())
                    self.last_warning = '损坏任务文件已保留为 ' + corrupt.name
                else:
                    self._atomic_write(self.backup_path, raw)
            if not self.backup_path.exists():
                self._atomic_write(self.backup_path, data)
            self._atomic_write(self.path, data)
            if security_failed:
                self.last_error = '任务元信息已保存，但 Windows DPAPI 不可用；重启后需要重新分析媒体地址。'
            return not security_failed
        except Exception as error:
            # 文件系统异常可能包含原始URL；公开错误只给出类型与固定说明。
            self.last_error = '任务状态保存失败（' + type(error).__name__ + '）；已有快照及下载部分仍保留。'
            return False

    def load(self):
        self.last_error = ''
        self.last_warning = ''
        self._unreadable = False
        if not self.path.exists() and not self.backup_path.exists():
            return []
        document = None
        for path in (self.path, self.backup_path):
            try:
                _, document = self._read_document(path)
                if path == self.backup_path:
                    self.last_warning = '任务主快照损坏或缺失，已从最近有效备份恢复；损坏原文将保留。'
                break
            except FileNotFoundError:
                continue
            except (ValueError, UnicodeError):
                continue
            except OSError:
                self._unreadable = True
                self.last_error = '无法读取任务快照，请检查文件权限；已有文件不会被覆盖。'
        if document is None:
            self._unreadable = True
            self.last_error = self.last_error or '任务文件及备份均无法读取或已损坏；原文件保留，未覆盖。'
            return []
        restored = []
        for record in document['tasks']:
            item = {'metadata': record['metadata'], 'request': None, 'context_error': ''}
            if record.get('request_dpapi'):
                self._opaque_context[record['metadata']['id']] = record['request_dpapi']
            try:
                encrypted = record.get('request_dpapi')
                if not encrypted:
                    raise TaskStoreSecurityError('任务没有可恢复的加密请求上下文。')
                plaintext = self._unprotect(base64.b64decode(encrypted, validate=True))
                request = _decode(json.loads(plaintext.decode('utf-8')))
                if not isinstance(request, dict):
                    raise ValueError('任务请求上下文格式无效。')
                item['last_error'] = str(request.pop('__last_error', ''))[-1800:]
                from core.downloader import DownloadRequest
                from core.request_context import RequestContext
                allowed = {field.name for field in fields(DownloadRequest)} - {'concurrency'}
                request = {key: value for key, value in request.items() if key in allowed}
                context = request.get('context_snapshot')
                if context is not None:
                    context['dropped_headers'] = tuple(context.get('dropped_headers', ()))
                    request['context_snapshot'] = RequestContext(**context)
                item['request'] = DownloadRequest(**request)
                self._encrypted_cache[record['metadata']['id']] = (
                    hashlib.sha256(plaintext).hexdigest(), encrypted)
            except Exception:
                item['context_error'] = '无法解密或还原该任务的请求上下文，请在原播放页重新分析媒体地址。'
                self.last_warning = '部分任务需要重新分析媒体地址；原任务和已下载部分已保留。'
            restored.append(item)
        return restored
