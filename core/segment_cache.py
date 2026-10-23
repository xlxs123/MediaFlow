"""Verified, credential-free disk cache in front of the existing media bridge."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import threading
import uuid
from email.utils import parsedate_to_datetime

from core.errors import MediaCheckError
from core.http_client import HttpResponse, cancelled
from core.http_ranges import validate_direct_response, validate_resume_response
from core.recovery_workspace import atomic_json, file_hash
from core.resume_identity import _asset_url


class _FileStream:
    def __init__(self, path, status, headers):
        self._file = path.open('rb')
        self.status = status
        from email.message import Message
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name] = str(value)

    def read(self, size=-1):
        return self._file.read(size)

    def close(self):
        self._file.close()


class SegmentCache:
    def __init__(self, directory, identity, on_progress=None, max_resource_bytes=32 * 1024 * 1024):
        if type(max_resource_bytes) is not int or max_resource_bytes < 1:
            raise ValueError('分片缓存上限必须是正整数。')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.identity = identity
        self.on_progress = on_progress or (lambda values: None)
        self.hits = 0
        self.completed = 0
        self._locks = {}
        self._lock = threading.Lock()
        self.max_resource_bytes = max_resource_bytes

    def _key(self, context, overrides, role):
        value = [self.identity, role, _asset_url(context.url), context.method,
                 hashlib.sha256(context.body or b'').hexdigest(),
                 overrides.get('range', context.headers.get('range', '')) or '']
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _resource_key(self, context):
        # 与分片键一样不保存原始 URL、签名或正文；资源验证器不能按 Range 分裂。
        value = [self.identity, _asset_url(context.url), context.method,
                 hashlib.sha256(context.body or b'').hexdigest()]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _validator(headers):
        etag = headers.get('etag', '')
        if re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', etag):
            return etag
        modified = headers.get('last-modified', '')
        if modified:
            try:
                if parsedate_to_datetime(modified).tzinfo is not None:
                    return modified
            except (ValueError, TypeError, OverflowError):
                pass
        return ''

    def _load_validators(self, path):
        try:
            if path.stat().st_size > 16 * 1024:
                return {}
            record = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(record, dict):
                return {}
            return {key: value for key, value in record.items()
                    if key in ('etag', 'last-modified', 'content-length', 'content-range')
                    and isinstance(value, str)}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _headers(response):
        return {name: response.stream.headers.get(name) for name in
                ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'ETag', 'Last-Modified')
                if response.stream.headers.get(name) is not None}

    @staticmethod
    def _verify_response(status, headers, interval):
        if not isinstance(headers, dict):
            raise ValueError('缓存响应头格式损坏。')
        if status == 206:
            # Content-Range 的长度也是实际正文长度；不能只在 Content-Length 存在时核对。
            return validate_direct_response(status, headers, interval)
        if status != 200:
            raise MediaCheckError('分片响应不是可验证的 HTTP 200/206。', 'INCOMPLETE_MEDIA_RESPONSE')
        if interval and interval.lower() != 'bytes=0-':
            raise MediaCheckError('分片服务器忽略了指定 Range，不能安全拼接完整资源。',
                                  'INCOMPLETE_MEDIA_RESPONSE')
        return validate_direct_response(status, headers, '')

    def open(self, client, context, overrides, role):
        resource_key = self._resource_key(context)
        with self._lock:
            # 同一资源的不同范围共享验证器，在发出请求前读取旧记录。
            lock = self._locks.setdefault(resource_key, threading.Lock())
        with lock:
            cancelled(client.cancel_event)
            actual_overrides = dict(overrides or {})
            interval = actual_overrides.get('range', context.headers.get('range', '')) or ''
            validators_path = self.directory / (resource_key + '.validators.json')
            previous = self._load_validators(validators_path)
            validator = self._validator(previous)
            resume_match = re.fullmatch(r'bytes=(\d{1,20})-', interval, re.I)
            resume = resume_match is not None and int(resume_match[1]) > 0
            restarted = False
            if resume:
                if validator:
                    actual_overrides['if-range'] = validator
                else:
                    # 新响应的 ETag 不能充当旧 .part 的身份；先强制完整 200 和 wb。
                    actual_overrides.update({'range': None, 'if-range': None})
                    interval = ''
                    restarted = True
                    self.on_progress({'recovery_state': 'restarting',
                        'recovery_reason': '该轨道没有旧可靠验证器，安全从头下载'})
            key = self._key(context, actual_overrides, role)
            body = self.directory / (key + '.blob')
            metadata = self.directory / (key + '.json')
            if body.is_file() and metadata.is_file():
                try:
                    record = json.loads(metadata.read_text(encoding='utf-8'))
                    if validator and resume:
                        expected = validate_resume_response(record['status'], record['headers'], interval,
                                                            previous)['expected_length']
                    else:
                        expected = self._verify_response(record['status'], record['headers'], interval)
                        if validator:
                            validate_resume_response(record['status'], record['headers'], interval, previous)
                    if (body.stat().st_size == record['size'] and file_hash(body) == record['sha256']
                            and (expected is None or record['size'] == expected)
                            and record['status'] in (200, 206)):
                        self.hits += 1
                        self.on_progress({'cache_hits': self.hits, 'verified_segments': self.completed})
                        cached = HttpResponse(context, _FileStream(body, record['status'], record['headers']), [])
                        cached.expected_length = expected
                        return cached
                except (ValueError, OSError, KeyError, TypeError):
                    pass
                # Invalid cache is never appended or counted as a completed fragment.
                body.rename(body.with_name(key + '.corrupt-' + uuid.uuid4().hex))
                metadata.unlink(missing_ok=True)
            temporary = self.directory / (key + '.' + uuid.uuid4().hex + '.incoming')
            response = None
            try:
                try:
                    response = client.open(context, actual_overrides)
                except MediaCheckError as error:
                    if resume and validator and error.diagnostic.get('http_status') == 416:
                        validate_resume_response(416, error.diagnostic.get('response_headers', {}), interval, previous)
                        error.diagnostic['resume_complete'] = True
                    raise
                headers = self._headers(response)
                if restarted and response.status != 200:
                    raise MediaCheckError('无法获得完整 HTTP 200 响应，不能安全重新下载该轨道。',
                                          'INCOMPLETE_MEDIA_RESPONSE')
                if validator and resume:
                    check = validate_resume_response(response.status, headers, interval, previous)
                    expected = check['expected_length']
                    if check['mode'] in ('resume', 'restart'):
                        self.on_progress({'recovery_state': 'resuming' if check['mode'] == 'resume' else 'restarting',
                            'recovery_reason': '轨道资源验证器与实际响应已核对'})
                else:
                    expected = self._verify_response(response.status, headers, interval)
                    if validator:
                        validate_resume_response(response.status, headers, interval, previous)
                # 首次完整请求/初次 MPD 范围探测建立验证器。强制从头请求不将新验证器
                # 写为旧部分文件的身份，以覆盖收到200之后、yt-dlp截断旧文件之前的崩溃窗口。
                if not validator and not restarted:
                    record = {name.lower(): value for name, value in headers.items()
                              if name.lower() in ('etag', 'last-modified', 'content-length', 'content-range')}
                    atomic_json(validators_path, record)
                response.expected_length = expected
                if ((expected is not None and expected > self.max_resource_bytes)
                        or (role == 'prefix' and expected is None)
                        or (role == 'prefix' and resume)):
                    # 大轨道保留成熟 HttpFD 的流式写 .part/Range 能力，不在前端再预读一遍。
                    streaming, response = response, None
                    return streaming
                with response:
                    digest, size = hashlib.sha256(), 0
                    with temporary.open('xb') as stream:
                        while True:
                            cancelled(client.cancel_event)
                            block = response.read(65536)
                            if not block:
                                break
                            stream.write(block)
                            digest.update(block)
                            size += len(block)
                            if expected is not None and size > expected:
                                raise MediaCheckError('分片正文超过声明的长度。', 'INCOMPLETE_MEDIA_RESPONSE')
                        stream.flush()
                        import os
                        os.fsync(stream.fileno())
                    if expected is not None and size != expected:
                        raise MediaCheckError('分片响应提前中断，尚未缓存为有效分片。', 'NETWORK_ERROR',
                                              {'exception_type': 'IncompleteRead', 'http_status': response.status})
                    if size == 0:
                        raise MediaCheckError('媒体分片为空，不能作为已完成进度。', 'INCOMPLETE_MEDIA_RESPONSE')
                    record = {'version': 1, 'sha256': digest.hexdigest(), 'size': size,
                              'status': response.status, 'headers': headers, 'role': role}
                    temporary.replace(body)
                    atomic_json(metadata, record)
                    self.completed += 1
                    self.on_progress({'cache_hits': self.hits, 'verified_segments': self.completed})
                    cached = HttpResponse(response.context, _FileStream(body, response.status, headers), response.redirects)
                    cached.expected_length = expected
                    return cached
            finally:
                if response is not None:
                    response.close()
                temporary.unlink(missing_ok=True)
