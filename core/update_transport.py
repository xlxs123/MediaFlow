"""Bounded HTTPS in an owned fixed-purpose process, including DNS/TLS stalls.

The helper receives only a HTTPS request, never a command or filesystem target.
Only the parent writes the exclusive update cache and calculates its digest.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from core.process_tree import ProcessTree


class TransportError(RuntimeError):
    pass


class TransportCancelled(TransportError):
    pass


def https_url(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(char.isspace() or ord(char) < 32 for char in value) or '\\' in value):
        raise TransportError('更新地址必须为不含账号密码的有效 HTTPS 地址。')
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port == 0):
            raise ValueError()
    except ValueError:
        raise TransportError('更新地址必须为不含账号密码的有效 HTTPS 地址。') from None
    return value


def _origin(url):
    parsed = urllib.parse.urlsplit(https_url(url))
    return parsed.hostname.lower(), parsed.port or 443


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, origin, release_policy=None):
        super().__init__()
        self.origin = origin
        self.release_policy = release_policy

    def accepts(self, url, *, record=False):
        origin = _origin(url)
        if self.release_policy is not None and self.release_policy.enabled:
            return self.release_policy.accepts(url, record=record)
        return origin == self.origin

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not self.accepts(newurl, record=True):
            raise TransportError('更新被重定向到未经信任的其他来源，已停止下载。')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _standard_stream(name, handle_number, mode):
    # PyInstaller --windowed has no sys.stdout even when the parent supplies pipes.
    stream = getattr(sys, name)
    if stream is not None:
        return getattr(stream, 'buffer', stream)
    if os.name != 'nt':
        raise TransportError('更新传输管道不可用。')
    import ctypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [ctypes.c_uint32]
    kernel.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel.GetStdHandle(handle_number & 0xffffffff)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        raise TransportError('更新传输管道不可用。')
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_BINARY), mode, buffering=0)


def worker_main():
    """Private early CLI entry; never downloads to a worker-supplied disk path."""
    reader = _standard_stream('stdin', -10, 'rb')
    writer = _standard_stream('stdout', -11, 'wb')

    def emit(value):
        writer.write(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii') + b'\n')
        writer.flush()

    try:
        data = reader.readline(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise TransportError('更新传输配置超过安全上限。')
        config = json.loads(data)
        if type(config) is not dict or set(config) != {'url','timeout','limit','size','ca_certificates'}:
            raise TransportError('更新传输配置无效。')
        url = https_url(config['url'])
        timeout, limit, size = config['timeout'], config['limit'], config['size']
        if (type(timeout) not in (int, float) or not 0.1 <= timeout <= 30
                or type(limit) is not int or not 1 <= limit <= 4*1024**3
                or (size is not None and (type(size) is not int or not 1 <= size <= limit))):
            raise TransportError('更新传输边界无效。')
        ca = config['ca_certificates']
        if not isinstance(ca, str) or len(ca) > 1024*1024:
            raise TransportError('更新证书配置无效。')
        context = ssl.create_default_context()
        if ca:
            context.load_verify_locations(cadata=ca)
        from core.update_github import channel_redirect_policy
        redirect = _SameOriginRedirect(_origin(url), channel_redirect_policy(url))
        opener = urllib.request.build_opener(redirect,
                        urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(url, headers={
            'User-Agent':'MediaFlow-Updater/2.0', 'Accept-Encoding':'identity'})
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or not redirect.accepts(response.geturl()):
                raise TransportError('更新服务器未返回完整的可信来源响应。')
            declared = response.headers.get('Content-Length')
            if declared and (not declared.isdecimal() or int(declared) > limit
                             or size is not None and int(declared) != size):
                raise TransportError('服务器报告的更新文件大小与清单不一致或超过上限。')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise TransportError('更新服务器返回了未允许的内容编码。')
            written = 0
            emit({'kind':'headers','total':int(declared) if declared else 0})
            while block := response.read1(128*1024):
                written += len(block)
                if written > limit or size is not None and written > size:
                    raise TransportError('更新下载大小超过清单或安全上限。')
                emit({'kind':'data','value':base64.b64encode(block).decode('ascii')})
            if (size is not None and written != size
                    or declared is not None and written != int(declared)):
                raise TransportError('更新包下载不完整，实际大小与清单不一致。')
            emit({'kind':'done','size':written})
        return 0
    except TransportError as error:
        message = str(error)
    except urllib.error.HTTPError as error:
        message = f'更新服务器返回 HTTP {error.code}。'
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = getattr(error, 'reason', error)
        if isinstance(reason, ssl.SSLError):
            message = '更新源 TLS 证书验证失败，已停止连接。'
        elif isinstance(reason, socket.gaierror):
            message = '更新源 DNS 解析失败，请检查地址和网络。'
        elif isinstance(reason, TimeoutError):
            message = '更新连接或读取超时，请检查网络后重试。'
        else:
            message = '更新连接中断或响应不完整，请检查网络后重试。'
    except (ValueError, TypeError, KeyError):
        message = '更新传输配置或响应无效。'
    emit({'kind':'error','message':message})
    return 1


def fetch(url, *, context, cancel=None, progress=None, destination=None,
          size=None, limit=2*1024*1024, connect_timeout=4, total_timeout=25):
    https_url(url)
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise TransportError('更新连接必须验证 TLS 证书及主机名。')
    if cancel is not None and cancel.is_set():
        raise TransportCancelled('更新操作已取消；旧版本和用户数据保留。')
    # Only an explicitly injected test/admin SSL context can add roots. A release
    # manifest and normal client settings never supply a TLS bypass or CA.
    certificates = ''.join(ssl.DER_cert_to_PEM_cert(cert)
                          for cert in context.get_ca_certs(binary_form=True))
    config = {'url':url, 'timeout':connect_timeout, 'size':size,
              'limit':limit, 'ca_certificates':certificates}
    command = ([sys.executable, '--update-fetch-worker'] if getattr(sys, 'frozen', False)
               else [sys.executable, '-B', '-m', 'core.update_transport', '--worker'])
    environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='0')
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parents[1]),
            env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        raise TransportError('HTTPS 后台传输进程无法启动，请检查完整发布目录和安全软件。') from None
    tree = ProcessTree(process)
    frames = queue.Queue(maxsize=8)
    stop = threading.Event()

    def read_frames():
        try:
            process.stdin.write(json.dumps(config, separators=(',', ':')).encode('utf-8') + b'\n')
            process.stdin.close()
            while not stop.is_set():
                line = process.stdout.readline(256*1024)
                if not line:
                    item = None
                else:
                    item = json.loads(line)
                while not stop.is_set():
                    try:
                        frames.put(item, timeout=0.05)
                        break
                    except queue.Full:
                        pass
                if item is None:
                    return
        except (OSError, ValueError):
            if not stop.is_set():
                try:
                    frames.put_nowait({'kind':'error','message':'HTTPS 后台传输管道无效。'})
                except queue.Full:
                    pass

    reader = threading.Thread(target=read_frames, name='MediaFlow-HTTPS-pipe', daemon=True)
    reader.start()
    done, total = 0, size or 0
    memory, digest = bytearray(), hashlib.sha256()
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise TransportCancelled('更新操作已取消；旧版本和用户数据保留。')
            if time.monotonic() - started > total_timeout:
                raise TransportError('更新传输超过总请求时限，已停止后台连接。')
            try:
                frame = frames.get(timeout=0.05)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    raise TransportError('HTTPS 后台传输进程意外退出。')
                continue
            if frame is None or type(frame) is not dict:
                raise TransportError('HTTPS 后台连接中断。')
            if frame.get('kind') == 'error':
                raise TransportError(str(frame.get('message', 'HTTPS 更新失败。'))[:512])
            if frame.get('kind') == 'headers':
                total = size or frame.get('total', 0)
            elif frame.get('kind') == 'data':
                try:
                    block = base64.b64decode(frame['value'], validate=True)
                except (ValueError, binascii.Error, KeyError):
                    raise TransportError('HTTPS 后台传输数据无效。') from None
                done += len(block)
                if done > limit or size is not None and done > size:
                    raise TransportError('更新传输超过清单或安全上限。')
                digest.update(block)
                if destination is not None:
                    destination.write(block)
                else:
                    memory.extend(block)
                if progress:
                    progress({'stage':'download' if destination is not None else 'check',
                        'message':'正在通过 HTTPS 获取更新', 'completed':done, 'total':total})
            elif frame.get('kind') == 'done':
                if frame.get('size') != done or size is not None and done != size:
                    raise TransportError('更新包下载不完整，实际大小与清单不一致。')
                process.wait(timeout=3)
                if process.returncode != 0:
                    raise TransportError('HTTPS 后台传输未正常结束。')
                return (done, digest.hexdigest()) if destination is not None else bytes(memory)
            else:
                raise TransportError('HTTPS 后台传输状态无效。')
    except (BrokenPipeError, subprocess.TimeoutExpired):
        raise TransportError('HTTPS 后台传输进程没有正常响应。') from None
    finally:
        stop.set()
        if process.poll() is None:
            tree.terminate()
        tree.close()
        reader.join(timeout=1)
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()


if __name__ == '__main__':
    raise SystemExit(worker_main() if sys.argv[1:] == ['--worker'] else 2)
