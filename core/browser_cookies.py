"""用户主动导入的 Cookie 后端；不记录凭据，不访问网页。

Netscape 文件没有 SameSite 信息，因此不能据此恢复原浏览器的 SameSite。
yt-dlp 的浏览器导出可能缺少 HttpOnly 标记；系统导出的条目保守按 HttpOnly 导入。
域 Cookie 使用现有 Qt 的公共后缀校验；host-only Cookie 不扩大到子域。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unicodedata
from urllib.parse import urlsplit

from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QNetworkCookie, QNetworkCookieJar

from core.process_tree import NO_WINDOW, ProcessTree


MAX_COOKIE_FILE_BYTES = 8 * 1024 * 1024
MAX_COOKIE_COUNT = 10000
MAX_COOKIE_LINE_BYTES = 32768
_BROWSERS = frozenset(('chrome', 'edge', 'firefox'))
_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_ERRORS = {
    'INVALID_PAGE': '请先打开需要导入登录状态的有效 http(s) 页面。',
    'INVALID_FILE': 'Cookie 文件不是有效的 Netscape 格式，或无法读取。',
    'FILE_TOO_LARGE': 'Cookie 文件过大，已停止导入。',
    'TOO_MANY_COOKIES': 'Cookie 文件条目过多，已停止导入。',
    'SCOPE_ERROR': '无法安全校验 Cookie 的域范围，已停止导入。',
    'INVALID_BROWSER': '仅支持从 Chrome、Edge 或 Firefox 导入 Cookie。',
    'TOOL_UNAVAILABLE': '无法启动已配置的 yt-dlp，请检查工具路径。',
    'INVALID_TIMEOUT': 'Cookie 导入超时设置无效。',
    'CANCELLED': 'Cookie 导入已取消。',
    'TIMEOUT': 'Cookie 导入超时，请使用内嵌网页登录或 Netscape Cookie 文件。',
    'BROWSER_EXPORT_FAILED': '浏览器 Cookie 导出失败，可能受 Chrome / Edge AppBound 加密保护、锁库或权限限制；'
                             '请使用内嵌网页登录或 Netscape Cookie 文件。',
    'IMPORT_FAILED': 'Cookie 导入失败，请使用内嵌网页登录或 Netscape Cookie 文件。',
}


class CookieImportError(Exception):
    """仅使用固定中文信息，避免工具输出或输入路径泄漏凭据。"""

    def __init__(self, code='IMPORT_FAILED'):
        self.code = code if code in _ERRORS else 'IMPORT_FAILED'
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True)
class ScopedCookie:
    name: str = field(repr=False)
    value: str = field(repr=False)
    domain: str = field(repr=False)
    path: str = field(repr=False)
    secure: bool
    http_only: bool
    expires: int | None
    host_only: bool


def _has_control(value):
    return any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)


def _canonical_host(value):
    if not value or _has_control(value) or len(value) > 253:
        return None
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        pass
    try:
        value = value.encode('idna').decode('ascii').lower()
    except (UnicodeError, ValueError):
        return None
    if len(value) > 253 or any(not _LABEL.fullmatch(label) for label in value.split('.')):
        return None
    # A malformed numeric IP must not be reinterpreted as a DNS parent domain.
    if all(char in '0123456789.' for char in value):
        return None
    return value


def _page_host(page_url):
    if not isinstance(page_url, str) or _has_control(page_url):
        raise CookieImportError('INVALID_PAGE')
    try:
        parsed = urlsplit(page_url)
        host = _canonical_host((parsed.hostname or '').removesuffix('.'))
        parsed.port  # Reject malformed ports even though the scope uses only the host.
        if parsed.scheme not in ('http', 'https') or not host or parsed.username is not None:
            raise ValueError
    except (ValueError, UnicodeError):
        raise CookieImportError('INVALID_PAGE') from None
    return host


def _scoped_domain(raw_domain, include_subdomains, page_host, jar, origin):
    if raw_domain.startswith('.'):
        if not include_subdomains:
            return None
        raw_domain = raw_domain[1:]
    domain = _canonical_host(raw_domain)
    if not domain:
        return None
    if not include_subdomains:
        return domain if domain == page_host else None
    try:
        ipaddress.ip_address(domain)
        return None  # IP addresses can only be host-only cookies.
    except ValueError:
        pass
    if '.' not in domain or not (page_host == domain or page_host.endswith('.' + domain)):
        return None
    probe = QNetworkCookie(b'mediaflow_scope_probe', b'1')
    probe.setDomain('.' + domain)
    probe.setPath('/')
    try:
        return '.' + domain if jar.validateCookie(probe, origin) else None
    except (RuntimeError, TypeError):
        raise CookieImportError('SCOPE_ERROR') from None


def load_cookie_file(path, page_url) -> list[ScopedCookie]:
    """读取有界 Netscape 文件，仅返回当前页面 host 的有效 Cookie。

    不按当前页面路径过滤：导入浏览器后由浏览器执行 path/Secure 匹配。
    过期、异常及无关域的条目跳过；文件本身不可读或越界时固定报错。
    """
    host = _page_host(page_url)
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(MAX_COOKIE_FILE_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise CookieImportError('INVALID_FILE') from None
    if len(data) > MAX_COOKIE_FILE_BYTES:
        raise CookieImportError('FILE_TOO_LARGE')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise CookieImportError('INVALID_FILE') from None
    lines = text.split('\n')
    if not lines or not re.fullmatch(r'#(?: Netscape)? HTTP Cookie File', lines[0].rstrip('\r')):
        raise CookieImportError('INVALID_FILE')
    jar = QNetworkCookieJar()
    origin = QUrl()
    origin.setScheme('https')
    origin.setHost(host)
    now = time.time()
    cookies = {}
    entries = 0
    for line in lines[1:]:
        if line.endswith('\r'):
            line = line[:-1]
        http_only = line.startswith('#HttpOnly_')
        if not line or (line.startswith('#') and not http_only):
            continue
        entries += 1
        if entries > MAX_COOKIE_COUNT:
            raise CookieImportError('TOO_MANY_COOKIES')
        if len(line.encode('utf-8')) > MAX_COOKIE_LINE_BYTES:
            continue
        if http_only:
            line = line[len('#HttpOnly_'):]
        parts = line.split('\t')
        if len(parts) != 7 or any(_has_control(part) for part in parts):
            continue
        raw_domain, tailmatch, cookie_path, secure_text, expiry, name, value = parts
        if tailmatch not in ('TRUE', 'FALSE') or secure_text not in ('TRUE', 'FALSE'):
            continue
        if not _NAME.fullmatch(name) or len(name) > 1024 or len(value.encode('utf-8')) > 16384:
            continue
        if not cookie_path.startswith('/') or len(cookie_path) > 2048:
            continue
        if expiry in ('', '0'):
            expires = None
        elif expiry.isascii() and expiry.isdecimal() and len(expiry) <= 12:
            expires = int(expiry)
            if expires <= now or expires > 253402300799:
                continue
        else:
            continue
        domain = _scoped_domain(raw_domain, tailmatch == 'TRUE', host, jar, origin)
        if domain is None:
            continue
        cookie = ScopedCookie(name, value, domain, cookie_path, secure_text == 'TRUE',
                              http_only, expires, tailmatch == 'FALSE')
        cookies[(domain, cookie_path, name)] = cookie
    return list(cookies.values())


def _stop_process(process, tree):
    """只终止本次启动的进程及其子进程，并等待文件句柄释放。"""
    if process is None:
        return
    try:
        if process.poll() is None:
            if tree is not None:
                tree.terminate()
            else:
                process.kill()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        if tree is not None:
            tree.close()


def export_browser_cookies(yt_dlp_path, browser, page_url, cancel_event=None,
                           timeout=40) -> list[ScopedCookie]:
    """用 yt-dlp 离线 infojson 导出 Cookie；不获取网页、不下载媒体。

    Chrome/Edge 系统加密保护与锁库失败直接报错，不尝试解密绕过。
    临时明文文件仅存在于本次 TemporaryDirectory，进程退出后清理。
    工具可能丢失 HttpOnly 属性，故系统导出的全部条目按 HttpOnly 保护；
    依赖 JavaScript Cookie 的页面可能需要在内嵌浏览器重新登录。
    """
    _page_host(page_url)
    if not isinstance(browser, str) or browser not in _BROWSERS:
        raise CookieImportError('INVALID_BROWSER')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise CookieImportError('INVALID_TIMEOUT')
    if cancel_event is not None and cancel_event.is_set():
        raise CookieImportError('CANCELLED')
    try:
        tool = Path(yt_dlp_path).resolve(strict=True)
        if not tool.is_file():
            raise OSError
    except (OSError, ValueError, TypeError, RuntimeError):
        raise CookieImportError('TOOL_UNAVAILABLE') from None
    process = tree = None
    try:
        with tempfile.TemporaryDirectory(prefix='mediaflow-cookie-import-') as directory:
            temporary = Path(directory)
            info = temporary / 'offline.info.json'
            cookie_file = temporary / 'cookies.txt'
            placeholder = 'https://cookie-import.invalid/placeholder'
            info.write_text(json.dumps({'id': 'cookie-import', 'title': 'MediaFlow cookie import',
                'extractor': 'generic', 'extractor_key': 'Generic', 'webpage_url': placeholder,
                'url': placeholder, 'ext': 'mp4', 'protocol': 'https',
                'vcodec': 'h264', 'acodec': 'aac'}), encoding='utf-8')
            args = [str(tool), '--ignore-config', '--no-plugin-dirs', '--no-cache-dir',
                    '--simulate', '--skip-download', '--no-check-formats',
                    '--load-info-json', str(info), '--cookies-from-browser', browser,
                    '--cookies', str(cookie_file)]
            try:
                process = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW, start_new_session=os.name != 'nt')
                tree = ProcessTree(process)
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise CookieImportError('CANCELLED')
                    if time.monotonic() >= deadline:
                        raise CookieImportError('TIMEOUT')
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                if cancel_event is not None and cancel_event.is_set():
                    raise CookieImportError('CANCELLED')
                if process.returncode != 0:
                    raise CookieImportError('BROWSER_EXPORT_FAILED')
                return [cookie if cookie.http_only else replace(cookie, http_only=True)
                        for cookie in load_cookie_file(cookie_file, page_url)]
            finally:
                _stop_process(process, tree)
    except CookieImportError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        raise CookieImportError('BROWSER_EXPORT_FAILED') from None
