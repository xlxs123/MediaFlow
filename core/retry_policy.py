"""有界任务重试的错误分类和可取消退避；不在 HTTP 层叠加隐藏重试。"""
from __future__ import annotations

import errno
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead, RemoteDisconnected
import math
import socket
import ssl
import time
from urllib.error import HTTPError, URLError

from core.errors import MediaCheckCancelled


_HTTP_RETRYABLE = frozenset((429, 500, 502, 503, 504))
_FATAL = frozenset(('AUTH_REQUIRED', 'HTTP_403', 'EXPIRED_URL', 'POSSIBLE_URL_EXPIRED',
                    'ROU_SIGNED_URL_EXPIRED', 'PROTECTED_MEDIA', 'DRM_PROTECTED', 'DISK_FULL',
                    'DISK_SPACE', 'PERMISSION_DENIED', 'FILE_EXISTS', 'OUTPUT_EXISTS',
                    'CANCELLED', 'TLS_ERROR', 'RESUME_MISMATCH', 'INCOMPLETE_MEDIA_RESPONSE',
                    'OUTPUT_NOT_VIDEO', 'OUTPUT_VALIDATION_ERROR', 'MEDIA_PROTOCOL_MISMATCH',
                    'ROU_SEGMENT_INVALID', 'ROU_SEGMENT_TOO_LARGE', 'ROU_SEGMENT_DEFLATE_FAILED'))
_NETWORK_KINDS = frozenset(('ConnectTimeout', 'ReadTimeout', 'TimeoutError', 'ConnectionResetError',
                            'ConnectionAbortedError', 'ConnectionRefusedError', 'RemoteDisconnected',
                            'IncompleteRead', 'DNS_ERROR', 'gaierror', 'BrokenPipeError', 'SSLEOFError'))


def is_temporary_tls_error(error):
    # An unexpected transport EOF can be retried with normal TLS verification.
    # Certificate failures and other unknown TLS errors remain permanent.
    return (isinstance(error, ssl.SSLError) and not isinstance(error, ssl.SSLCertVerificationError)
            and (isinstance(error, ssl.SSLEOFError)
                 or getattr(error, 'reason', '') == 'UNEXPECTED_EOF_WHILE_READING'))


def is_retryable(error):
    """只有有临时故障证据时重试；未知永久错误交给用户处理。"""
    if isinstance(error, MediaCheckCancelled):
        return False
    diagnostic = getattr(error, 'diagnostic', {}) or {}
    code = str(getattr(error, 'code', ''))
    if code in _FATAL or diagnostic.get('requires_context_refresh') or diagnostic.get('drm') is True:
        return False
    status = diagnostic.get('http_status')
    if isinstance(error, HTTPError):
        status = error.code
    if status is not None:
        try:
            status = int(status)
        except (TypeError, ValueError):
            return False
        if status >= 400:
            return status in _HTTP_RETRYABLE
    if code in {f'HTTP_{value}' for value in _HTTP_RETRYABLE}:
        return True
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLError):
        return is_temporary_tls_error(reason)
    if isinstance(reason, (PermissionError, FileNotFoundError)):
        return False
    if isinstance(reason, socket.gaierror):
        # EAI_NONAME 是永久 NXDOMAIN；EAI_AGAIN、Windows WSATRY_AGAIN 等才是临时故障。
        return reason.errno in (None, socket.EAI_AGAIN, 11002)
    if isinstance(reason, (TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected)):
        return True
    if isinstance(reason, OSError) and reason.errno in (errno.ENOSPC, errno.EACCES, errno.EPERM, errno.EROFS):
        return False
    kind = diagnostic.get('exception_type') or diagnostic.get('exception_class')
    if code == 'NETWORK_ERROR':
        if kind in ('DNS_ERROR', 'gaierror'):
            return diagnostic.get('dns_temporary', True) is True
        return kind in _NETWORK_KINDS
    return False


def _retry_after(error):
    diagnostic = getattr(error, 'diagnostic', {}) or {}
    value = diagnostic.get('retry_after')
    if value is None:
        headers = diagnostic.get('response_headers') or getattr(error, 'headers', {}) or {}
        value = next((item for key, item in headers.items() if key.lower() == 'retry-after'), None)
    if value is None:
        return None
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            seconds = float(value)
            return seconds if math.isfinite(seconds) else None
        except ValueError:
            return None
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return max(0.0, stamp.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(error, attempt, base=2.0):
    """attempt=1 为第一次重试，默认 2、4、8 秒；尊重429/503有效 Retry-After。"""
    if type(attempt) is not int or attempt < 1 or type(base) not in (float, int) or not math.isfinite(base) or base < 0:
        raise ValueError('重试次数和退避时间必须是有效非负数。')
    diagnostic = getattr(error, 'diagnostic', {}) or {}
    status = diagnostic.get('http_status')
    if isinstance(error, HTTPError):
        status = error.code
    if status is None:
        code = str(getattr(error, 'code', ''))
        if code.startswith('HTTP_') and code[5:].isdigit():
            status = int(code[5:])
    try:
        status = int(status) if status is not None else None
    except (ValueError, TypeError):
        status = None
    if status in (429, 503):
        requested = _retry_after(error)
        if requested is not None:
            return requested
    return min(60.0, float(base) * (2 ** min(attempt - 1, 16)))


def wait_for_retry(cancel_event, seconds, on_tick=None):
    """倒计时每秒回调，可立即取消；等待结束才返回 True。"""
    if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('重试等待时间必须是有限非负数。')
    if cancel_event is not None and cancel_event.is_set():
        return False
    deadline = time.monotonic() + seconds
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        remaining = max(0.0, deadline - time.monotonic())
        if on_tick is not None:
            on_tick(remaining)
        if remaining <= 0:
            return True
        interval = min(1.0, remaining)
        if cancel_event is not None:
            if cancel_event.wait(interval):
                return False
        else:
            time.sleep(interval)
