"""有限重定向、完整请求上下文和独立 HTTP 诊断；不使用站点规则。"""
from __future__ import annotations

from dataclasses import dataclass, replace
import ipaddress
from http.client import RemoteDisconnected, IncompleteRead
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from core.errors import MediaCheckError, MediaCheckCancelled
from core.request_context import RequestContext
from core.retry_policy import is_temporary_tls_error


def cancelled(event):
    if event and event.is_set():
        raise MediaCheckCancelled('下载已停止。')


def is_loopback(url):
    host = urlsplit(url).hostname
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == 'localhost'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def http_failure(context: RequestContext, status: int, response_headers=None, redirects=None):
    headers = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
    code = 'AUTH_REQUIRED' if status == 401 or headers.get('www-authenticate') else 'HTTP_403' if status == 403 else 'PROXY_ERROR' if status == 407 else 'HTTP_ERROR'
    missing = [name for name in ('origin', 'referer', 'user-agent', 'cookie', 'authorization')
               if not context.headers.get(name)]
    signature = any(k.lower() in ('sign', 'signature', 'sig', 'token', 't', 'auth', 'secure', 'expires', 'exp')
                    for k, _ in context.query_parameters)
    for name, value in context.query_parameters:
        if name.lower() in ('expires', 'expire', 'expiry', 'exp'):
            try:
                stamp = float(value)
                if stamp > 10**12:
                    stamp /= 1000
                if status in (401, 403, 404, 410) and 946684800 < stamp < time.time():
                    code = 'EXPIRED_URL'
            except ValueError:
                pass
    if status in (401, 403):
        message = (f'HTTP {status}：服务器拒绝当前请求，这不代表 DRM。请检查 Origin / Referer / User-Agent、'
                   'Cookie / Authorization 和签名是否过期，必要时重新 Copy as cURL。')
        if code == 'EXPIRED_URL':
            message = f'HTTP {status}：URL 的到期时间已经过去，请重新复制最新 cURL。'
        elif code == 'AUTH_REQUIRED':
            message = f'HTTP {status}：服务器要求认证，请检查 Cookie / Authorization，重新复制浏览器中的成功请求。'
    else:
        message = f'媒体请求失败：HTTP Error {status}。'
    if status == 403 and 'range' in context.dropped_headers:
        message += ' 原 Range 已按隔离模式过滤；如果该资源也要求 Range，可在高级设置开启继承。'
    diagnostic = context.summary()
    causes = []
    if not context.origin or not context.referer:
        causes.append('ANTI_HOTLINK')
    if signature:
        causes.append('POSSIBLE_URL_EXPIRED')
    needs_refresh = code == 'EXPIRED_URL' or status in (403, 404, 410) and signature
    if needs_refresh and status in (404, 410):
        message += ' 该地址包含临时签名，可能已过期；请在原播放页重新分析媒体地址。'
    diagnostic.update(http_status=status, drm=None, missing_context=missing, signed_url=signature,
                      redirects=redirects or [], possible_causes=causes, stage='HTTP 请求',
                      exception_type='HTTPError', requires_context_refresh=needs_refresh,
                      retry_after=headers.get('retry-after'), response_headers={k: v for k, v in headers.items()
                          if k in ('content-type', 'content-range', 'accept-ranges', 'content-length', 'etag', 'last-modified', 'retry-after')})
    return MediaCheckError(message, code, diagnostic)


def network_failure(context: RequestContext, error: Exception, phase='connect', redirects=None):
    reason = error.reason if isinstance(error, URLError) and isinstance(error.reason, BaseException) else error
    name = type(reason).__name__
    code, kind = 'NETWORK_ERROR', name
    if is_temporary_tls_error(reason):
        kind = 'SSLEOFError'
    elif isinstance(reason, ssl.SSLError):
        code, kind = 'TLS_ERROR', 'TLS_ERROR'
    elif isinstance(reason, socket.gaierror):
        kind = 'DNS_ERROR'
    elif isinstance(reason, TimeoutError):
        kind = 'ReadTimeout' if phase == 'read' else 'ConnectTimeout'
    elif isinstance(reason, RemoteDisconnected):
        kind = 'RemoteDisconnected'
    elif isinstance(reason, ConnectionResetError):
        kind = 'ConnectionResetError'
    elif isinstance(reason, IncompleteRead):
        kind = 'IncompleteRead'
    elif 'proxy' in str(reason).lower() or 'tunnel connection failed' in str(reason).lower():
        code, kind = 'PROXY_ERROR', 'ProxyError'
    diagnostic = context.summary()
    diagnostic.update(http_status=None, drm=None, redirects=redirects or [],
                      stage='读取媒体响应' if phase == 'read' else '建立媒体连接',
                      exception_type=kind, exception_class=name, exception_message=str(reason))
    if isinstance(reason, socket.gaierror):
        diagnostic['dns_temporary'] = reason.errno in (None, socket.EAI_AGAIN, 11002)
    message = f'{kind}：{reason}。'
    if code == 'TLS_ERROR':
        message += ' 请检查证书、系统时间和代理配置。'
    elif code == 'PROXY_ERROR':
        message += ' 请检查系统代理是否可用。'
    elif kind in ('RemoteDisconnected', 'ConnectionResetError'):
        message += ' 服务器关闭连接，请检查当前浏览器请求上下文，必要时重新 Copy as cURL。'
    return MediaCheckError(message, code, diagnostic)


@dataclass
class HttpResponse:
    context: RequestContext
    stream: object
    redirects: list[dict]
    lease: object = None

    @property
    def status(self):
        return self.stream.status

    @property
    def content_type(self):
        return self.stream.headers.get('Content-Type', '').lower()

    def read(self, size=-1):
        try:
            return self.stream.read(size)
        except (URLError, OSError, RemoteDisconnected, IncompleteRead) as error:
            raise network_failure(self.context, error, 'read', self.redirects) from None

    @property
    def response_headers(self):
        return {name.lower(): self.stream.headers.get(name) for name in
                ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'ETag', 'Last-Modified', 'Retry-After')
                if self.stream.headers.get(name) is not None}

    def close(self):
        try:
            self.stream.close()
        finally:
            if self.lease is not None:
                self.lease.release()
                self.lease = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ContextHttpClient:
    def __init__(self, cancel_event: threading.Event | None = None, timeout=8, concurrency=None):
        self.cancel_event = cancel_event
        self.timeout = timeout
        self.concurrency = concurrency

    def open(self, context: RequestContext, overrides: dict | None = None) -> HttpResponse:
        lease = self.concurrency.acquire(self.cancel_event) if self.concurrency else None
        try:
            response = self._open(context, overrides)
            response.lease = lease
            return response
        except BaseException:
            if lease is not None:
                lease.release()
            raise

    def _open(self, context: RequestContext, overrides: dict | None = None) -> HttpResponse:
        current = context
        redirects = list(context.redirect_chain)
        for _ in range(11):
            cancelled(self.cancel_event)
            headers = dict(current.headers)
            for name in ('host', 'content-length', 'connection', 'transfer-encoding', 'accept-encoding'):
                headers.pop(name, None)
            headers['accept-encoding'] = 'identity'
            for name, value in (overrides or {}).items():
                if value is None:
                    headers.pop(name.lower(), None)
                else:
                    headers[name.lower()] = value
            # 响应/错误上下文记录实际发送的头；范围覆盖不能仍显示父请求的 Range。
            current = replace(current, headers=headers)
            wire_url = quote(current.url, safe=":/?&=;%#@+!$'()*,-._~[]")
            request = Request(wire_url, data=current.body, headers=headers, method=current.method)
            handlers = [_NoRedirect()]
            if is_loopback(current.url):
                handlers.append(ProxyHandler({}))
            try:
                stream = build_opener(*handlers).open(request, timeout=self.timeout)
                try:
                    cancelled(self.cancel_event)
                except MediaCheckCancelled:
                    stream.close()
                    raise
                return HttpResponse(current, stream, redirects)
            except HTTPError as error:
                if error.code in (301, 302, 303, 307, 308) and error.headers.get('Location'):
                    target = urljoin(current.url, error.headers['Location'])
                    redirects.append({'status': error.code, 'from': current.url, 'to': target})
                    error.close()
                    current = current.redirected(target, error.code)
                    continue
                failure = http_failure(current, error.code, error.headers, redirects)
                error.close()
                raise failure from None
            except (URLError, OSError, RemoteDisconnected, IncompleteRead) as error:
                cancelled(self.cancel_event)
                raise network_failure(current, error, redirects=redirects) from None
        raise MediaCheckError('媒体请求重定向次数超过 10 次，已停止。', 'NETWORK_ERROR',
                              {**current.summary(), 'redirects': redirects, 'http_status': None, 'drm': None,
                               'exception_type': 'RedirectLimit'})
