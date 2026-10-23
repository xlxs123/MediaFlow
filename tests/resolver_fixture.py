"""本地自生成媒体与 Rou 行为夹具；生产 Resolver 的域名规则保持不变。"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlsplit
import zlib

from core.site_resolvers import RouVideoResolver
from tests.hls_fixture import HlsFixture
from tests.test_rou_playlist_decoder import png


class LocalRouResolver(RouVideoResolver):
    """Only test code maps the protocol adapter to the fixture's loopback origin."""
    def can_handle(self, page):
        return urlsplit(page.url).hostname == '127.0.0.1' and urlsplit(page.url).path.startswith('/v/')


class ResolverFixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.media = HlsFixture(self.directory)
        self.server = self.thread = None
        self.requests = []
        self.lock = threading.Lock()
        self.token = 'old'
        self.expire = False
        self.fail_retry = False
        self.retry_count = 0
        self.retry_pattern = None

    def generate(self):
        self.media.generate()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def url(self, path):
        return self.base + path

    def entry(self, kind='aes', token=None, basename='index.png'):
        return self.url(f'/cdn/{kind}/{basename}?token={token or self.token}&t=1&t=2')

    def html(self, path):
        case = path.rsplit('/', 1)[-1]
        tag = '<video id="main" controls muted style="width:640px;height:360px"></video>'
        next_script, script, style = '', '', ''
        if path.startswith('/v/'):
            if case not in ('api', 'fallback', 'variants'):
                target = self.entry('clear' if case in ('plain', 'expiry') else 'variants' if case == 'variants' else 'normal' if case == 'normal' else 'aes')
                raw = json.dumps({'videoUrl': target})
                shifted = ''.join(chr(ord(char) + 5) for char in raw).encode()
                data = {'props': {'pageProps': {'video': {'id': case}, 'ev': {'d': base64.b64encode(shifted).decode(), 'k': 5}}}}
                next_script = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'
                if case in ('plain', 'deflate'):
                    script = 'fetch(' + json.dumps(target) + ',{headers:{Range:"bytes=0-"}}).then(r=>r.arrayBuffer());'
            if case == 'fallback':
                tag = '<video id="main" controls muted autoplay src="/direct.mp4"></video>'
        if case == 'config':
            script = 'const m=new MediaSource();document.getElementById("main").src=URL.createObjectURL(m);fetch("/config").then(r=>r.json());'
        if case in ('hide', 'close', 'login', 'player', 'frame'):
            style = 'html{overflow:hidden}body{overflow:hidden;margin:0} .cover{position:fixed;inset:0;z-index:9000;background:#ddd}'
            if case == 'hide':
                tag += '<div id="cover" class="cover" style="opacity:0.95"><div>普通临时提示</div></div>'
            elif case == 'close':
                tag += '<div id="cover" class="cover" style="opacity:0.95"><button id="close" aria-label="Close" onclick="window.closeClicked=(window.closeClicked||0)+1;this.parentElement.remove()">×</button></div>'
            elif case == 'login':
                tag += '<div id="login" class="cover"><input type="password"><p>登录以继续</p></div>'
            elif case == 'player':
                tag = '<div id="main-player" class="cover">' + tag + '<canvas></canvas></div>'
            elif case == 'frame':
                tag += f'<iframe id="cover" class="cover" src="http://localhost:{self.server.server_port}/tools/child"></iframe>'
        if case == 'fit':
            style = 'body{min-width:1600px;margin:0}'
        if case == 'too-wide':
            style = 'body{min-width:9000px;margin:0}'
        return ('<!DOCTYPE html><html><head><meta charset="utf-8"><style>' + style +
                '</style></head><body>' + tag + next_script + '<script>' + script + '</script></body></html>').encode()

    def start(self):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_):
                pass
            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    pass
            def do_GET(self):
                self.serve(False)
            def do_HEAD(self):
                self.serve(True)
            def reply(self, status, data=b'', mime='text/plain', headers=None, head=False):
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(data)
                    except (ConnectionResetError, BrokenPipeError):
                        pass
            def serve(self, head):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                path = parsed.path
                with fixture.lock:
                    fixture.requests.append({'path': path, 'token': query.get('token', [''])[0], 'method': self.command,
                                             'headers': {key.lower(): value for key, value in self.headers.items()}})
                if path.startswith(('/v/', '/tools/')):
                    return self.reply(200, fixture.html(path), 'text/html;charset=utf-8', head=head)
                if path.startswith('/api/hls/'):
                    video_id = path.rsplit('/', 1)[-1]
                    if video_id == 'api':
                        return self.reply(200, json.dumps({'videoUrl': fixture.entry()}).encode(), 'application/json', head=head)
                    if video_id == 'variants':
                        # 此旧兼容场景的 jpg 地址必须由 API 明确下发。
                        # 普通 index.png 仍可用于拒绝测试，但解析器不得猜相邻文件。
                        return self.reply(200, json.dumps({'videoUrl': fixture.entry('variants', basename='index.jpg')}).encode(),
                                          'application/json', head=head)
                    return self.reply(404, head=head)
                if path == '/config':
                    return self.reply(200, json.dumps({'data': {'media': {'hls': fixture.entry('clear', basename='index.m3u8')}}}).encode(), 'application/json', head=head)
                if path == '/direct.mp4':
                    data = (fixture.directory / 'direct.mp4').read_bytes()
                    request_range = self.headers.get('Range', '')
                    start, end, status = 0, len(data)-1, 200
                    headers = {'Accept-Ranges': 'bytes', 'ETag': '"fixture-direct-v1"'}
                    if request_range:
                        match = re.fullmatch(r'bytes=(\d+)-(\d*)', request_range)
                        if match:
                            start, end = int(match[1]), min(end, int(match[2]) if match[2] else end)
                            headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
                            status = 206
                    return self.reply(status, data[start:end+1], 'video/mp4', headers, head)
                match = re.fullmatch(r'/cdn/(aes|clear|variants|normal|retry)/([^/]+)', path)
                if not match:
                    return self.reply(404, head=head)
                kind, name = match.groups()
                token = query.get('token', [''])[0]
                if not token:
                    return self.reply(403, b'required signed query', head=head)
                if name in ('index.png', 'index.jpg', 'index.jpeg', 'index.m3u8', 'master.m3u8'):
                    if kind in ('normal', 'variants') and name == 'index.png':
                        return self.reply(200, png(), 'image/png', head=head)
                    if kind == 'normal' or kind == 'variants' and name != 'index.jpg':
                        return self.reply(404, head=head)
                    folder = fixture.directory / 'disguised' if kind in ('aes', 'variants') else fixture.directory
                    text = (folder / 'source.m3u8').read_bytes()
                    if name == 'index.m3u8':
                        payload, mime = text, 'application/vnd.apple.mpegurl'
                    else:
                        payload = png(zlib.compress(text), 1) if kind in ('aes', 'variants') else png(text)
                        mime = 'image/png'
                else:
                    folder = fixture.directory / 'disguised' if kind in ('aes', 'variants') else fixture.directory
                    item = folder / name
                    if not item.is_file():
                        return self.reply(404, head=head)
                    number = re.search(r'(\d+)', name)
                    if fixture.expire and token == 'old' and number and int(number[1]) >= 2:
                        return self.reply(403, b'expired token', head=head)
                    if fixture.fail_retry and kind == 'retry' and number and int(number[1]) == 2:
                        fixture.retry_count += 1
                        pattern = fixture.retry_pattern
                        if pattern is None or fixture.retry_count <= len(pattern):
                            status = 500 if pattern is None else pattern[fixture.retry_count-1]
                            return self.reply(status, b'temporary server error', head=head)
                    payload = item.read_bytes()
                    mime = 'image/png' if name.endswith('.png') else 'application/octet-stream'
                headers = {'Accept-Ranges': 'bytes', 'ETag': '"resolver-fixture"'}
                status = 200
                if self.headers.get('Range') == 'bytes=0-':
                    status = 206
                    headers['Content-Range'] = f'bytes 0-{len(payload)-1}/{len(payload)}'
                return self.reply(status, payload, mime, headers, head)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':0.05}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
