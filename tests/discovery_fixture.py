"""本地 Chromium 测试网页；媒体由 FFmpeg 生成，无第三方账号或媒体。"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlsplit

from tests.hls_fixture import HlsFixture

SIGNATURE_QUERY = 'secure=fixture%2F%2Bvalue&sign=fixture%2Ba&token=one&token=two&expires=4102444800&t=123&auth=fixture-auth&signature=fixture-sign'
AUTH_HEADER = 'Bearer discovery-fixture-auth'
COOKIE_VALUE = 'discovery-fixture-cookie'


class DiscoveryFixture:
    def __init__(self, directory):
        self.media = HlsFixture(directory)
        self.directory = Path(directory)
        self.server = self.thread = None
        self.requests = []
        self.lock = threading.Lock()
        self.slow_requested = threading.Event()
        self.slow_release = threading.Event()

    def generate(self):
        self.media.generate()
        import tests.hls_fixture as fixtures
        subprocess.run([str(fixtures.ROOT / 'tools' / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error',
                        '-y', '-i', str(self.directory / 'direct.mp4'), '-c:v', 'libvpx', '-threads', '2',
                        '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '300k', '-c:a', 'libvorbis',
                        str(self.directory / 'direct.webm')], check=True, timeout=40,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def generate_quality_versions(self):
        """单独用于质量验收：生成实际不同尺寸，避免仅改 Manifest 的标签。"""
        import tests.hls_fixture as fixtures
        folder = self.directory / 'quality'
        folder.mkdir(exist_ok=True)
        master = ['#EXTM3U']
        for height, bandwidth in ((480, 1_000_000), (720, 2_000_000), (1080, 4_000_000)):
            width = 2 * round(height * 16 / 9 / 2)
            subprocess.run([str(fixtures.ROOT / 'tools' / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
                '-i', str(self.directory / 'direct.mp4'), '-vf', f'scale={width}:{height}', '-c:v', 'libx264',
                '-threads', '1', '-preset', 'ultrafast', '-g', '25', '-sc_threshold', '0', '-c:a', 'copy',
                '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0', '-hls_playlist_type', 'vod',
                '-hls_segment_filename', str(folder / f'{height}-%03d.ts'), str(folder / f'{height}.m3u8')],
                check=True, timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            master.extend([f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={width}x{height},'
                           'CODECS="avc1.42E01E,mp4a.40.2",FRAME-RATE=25', f'{height}.m3u8'])
        (folder / 'master.m3u8').write_text('\n'.join(master) + '\n', encoding='utf-8')

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def page(self, case):
        return self.base + '/pages/' + case

    def html(self, case):
        tag = '<video id="main" controls muted autoplay style="width:640px;height:360px"></video>'
        script = ''
        headers = {'Range': 'bytes=0-'}
        stream = {'hls_clear': 'clear', 'hls_aes': 'aes', 'hls-low': 'aes', 'png_segments': 'png',
                  'dash': 'dash', 'mse': 'clear', 'variants': 'variants',
                  'redirect': 'redirect', 'signed': 'png', 'drm': 'drm', 'slow_navigation': 'clear',
                  'quality-versions': 'quality'}.get(case)
        if case.startswith('confidence-'):
            tag = '<video id="main" controls muted preload="auto" style="width:640px;height:360px" src="/media/main.webm"></video>'
            script = "document.getElementById('main').play().catch(()=>{});"
            if case in ('confidence-multiple', 'confidence-preload', 'confidence-switch'):
                tag += '<video id="recommendation" controls muted preload="auto" style="position:absolute;left:670px;top:60px;width:160px;height:90px" src="/media/recommendation.webm"></video>'
                tag += '<div style="display:none"><video id="preloaded" preload="auto" src="/media/preloaded.webm"></video></div>'
                script += "fetch('/media/preview.webm').then(r=>r.arrayBuffer());"
            elif case == 'confidence-ambiguous':
                tag = ''.join(f'<video id="{name}" controls muted preload="auto" style="display:inline-block;width:280px;height:158px" src="/media/{name}.webm"></video>'
                              for name in ('equal-one', 'equal-two'))
                script = ''
            elif case == 'confidence-poster':
                tag = tag.replace(' controls ', ' poster="/assets/poster.png" controls ')
        if case == 'slow_navigation':
            tag += '<img src="/assets/slow.png" alt="delayed load fixture">'
        if stream:
            target = '/redirect/manifest' if stream == 'redirect' else '/streams/' + stream + '/index'
            if case == 'signed':
                target += '?' + SIGNATURE_QUERY
            if case in ('hls_aes', 'hls-low'):
                headers['Authorization'] = AUTH_HEADER
            script = f'''
            const video = document.getElementById('main');
            const ms = new MediaSource(); video.src = URL.createObjectURL(ms);
            const capturedHeaders = {json.dumps(headers)};
            fetch({json.dumps(target)}, {{headers: capturedHeaders, credentials:'same-origin'}})
              .then(async response => {{
                const text = await response.text();
                window.fixtureManifestReceived = true;
                const base = response.url;
                const lines = text.split(/\\r?\\n/);
                const uris = lines.filter(x => x && !x.startsWith('#') && !x.startsWith('<'));
                const keys = [...text.matchAll(/URI="([^"]+)"/g)].map(x => x[1]);
                const resources = [...keys, ...uris].slice(0, 10);
                for (const uri of resources) {{
                  try {{ await fetch(new URL(uri, base), {{headers:capturedHeaders}}).then(r=>r.arrayBuffer()); }} catch (_) {{}}
                }}
              }}).catch(()=>{{}});
            '''
            if case == 'hls-low':
                tag = '<div style="display:none">' + tag + '</div>'
        elif case in ('direct', 'multiple', 'vast'):
            # WebM 真正播放供 DOM / seek 验收，Direct MP4 另以真实 206 请求验证。
            script = "document.getElementById('main').src='/media/main.webm';"
            if case == 'direct':
                script += "fetch('/media/direct.mp4',{headers:{Range:'bytes=0-'}}).then(r=>r.arrayBuffer());"
            if case in ('multiple', 'vast'):
                tag += '<video id="ad" controls muted autoplay style="width:96px;height:54px" src="/media/preroll.webm"></video>'
            if case == 'vast':
                tag += '<script src="/assets/ad-provider.js"></script>'
        elif case == 'thumbnail':
            tag += '<track src="/media/thumbnail.vtt">'
            script = "fetch('/media/thumbnail.vtt').then(r=>r.text());"
        elif case == 'poster':
            tag = '<video id="main" controls poster="/assets/poster.png" style="width:640px;height:360px"></video>'
        elif case == 'iframe':
            tag = f'<iframe style="width:720px;height:500px" src="http://localhost:{self.server.server_port}/pages/direct"></iframe>'
        elif case == 'post':
            script = """fetch('/media/post', {method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded',
                       'Authorization':'Bearer discovery-fixture-auth'},body:'fixture=body%2Bvalue'})
                       .then(r=>r.blob()).then(b=>{document.getElementById('main').src=URL.createObjectURL(b)});"""
        elif case == 'empty':
            tag = '<p>这个网页没有媒体。</p>'
        return ('<!doctype html><html><head><meta charset="utf-8"><title>Local media discovery fixture</title>'
                '<style>body{font-family:sans-serif;margin:12px;background:#eef4fb}video{background:#142333;display:block;margin-bottom:10px}</style>'
                '</head><body><p>本地自动查找验收网页：' + case + '</p>' + tag + '<script>' + script + '</script></body></html>').encode()

    def start(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    pass

            def do_GET(self):
                self.serve(False)

            def do_HEAD(self):
                self.serve(True)

            def do_POST(self):
                self.body = self.rfile.read(min(1024 * 1024, int(self.headers.get('Content-Length', '0'))))
                self.serve(False)

            def reply(self, data, mime, head=False, status=200, extra=None, ranged=False):
                total, start, end = len(data), 0, len(data) - 1
                if ranged and self.headers.get('Range'):
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers['Range'])
                    if not match:
                        status, data = 416, b''
                    else:
                        start = int(match[1] or '0')
                        end = min(int(match[2] or str(end)), end)
                        status = 206 if 0 <= start <= end else 416
                        data = data[start:end + 1] if status == 206 else b''
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Accept-Ranges', 'bytes')
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(data)
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        pass

            def serve(self, head):
                parsed = urlsplit(self.path)
                path = parsed.path
                with fixture.lock:
                    fixture.requests.append({'path': path, 'query': parsed.query, 'method': self.command,
                                             'headers': {key.lower(): value for key, value in self.headers.items()},
                                             'body': getattr(self, 'body', None)})
                    fixture.requests[:] = fixture.requests[-4096:]
                if path.startswith('/pages/'):
                    case = path.rsplit('/', 1)[-1]
                    extra = {'Set-Cookie': f'session={COOKIE_VALUE}; HttpOnly; SameSite=Lax; Path=/'} if case in ('hls_aes', 'hls-low') else {}
                    if case == 'png_segments':
                        # Establish the no-Cookie real-world scenario explicitly
                        # when test windows share a persistent browser profile.
                        extra['Set-Cookie'] = 'session=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/'
                    self.reply(fixture.html(case), 'text/html; charset=utf-8', head, extra=extra)
                    return
                if path == '/redirect/manifest':
                    self.reply(b'', 'text/plain', head, status=302,
                               extra={'Location': '/streams/clear/index?' + SIGNATURE_QUERY})
                    return
                if path == '/assets/ad-provider.js':
                    self.reply(b"fetch('/assets/vast.xml').then(r=>r.text());fetch('/media/ad.mp4').then(r=>r.arrayBuffer());",
                               'text/javascript', head)
                    return
                if path == '/assets/vast.xml':
                    self.reply(b'<VAST version="3.0"><Ad><InLine><Creatives><Creative><Linear><MediaFiles>'
                               b'<MediaFile type="video/mp4">' + (fixture.base + '/media/ad.mp4').encode() +
                               b'</MediaFile></MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>', 'application/xml', head)
                    return
                if path in ('/assets/poster.png', '/assets/slow.png'):
                    if path == '/assets/slow.png':
                        fixture.slow_requested.set()
                        fixture.slow_release.wait(8)
                    import base64
                    self.reply(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZY4AAAAASUVORK5CYII='), 'image/png', head)
                    return
                if path == '/media/thumbnail.vtt':
                    self.reply(b'WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nposter.png#xywh=0,0,1,1\n', 'text/vtt', head)
                    return
                if path.startswith('/media/'):
                    if path == '/media/post' and (self.command != 'POST' or getattr(self, 'body', None) != b'fixture=body%2Bvalue'):
                        self.reply(b'POST required', 'text/plain', head, status=403)
                        return
                    webm = path.endswith('.webm')
                    file = fixture.directory / ('direct.webm' if webm else 'direct.mp4')
                    self.reply(file.read_bytes(), 'video/webm' if webm else 'video/mp4', head, ranged=True)
                    return
                if path.startswith('/streams/'):
                    parts = path.split('/')
                    if len(parts) != 4:
                        self.reply(b'', 'text/plain', head, status=404)
                        return
                    folder, name = parts[2:]
                    if folder == 'drm':
                        self.reply(b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://license",'
                                   b'KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:2,\na.png\n#EXT-X-ENDLIST\n',
                                   'application/vnd.apple.mpegurl', head, ranged=True)
                        return
                    if folder == 'aes' and (self.headers.get('Authorization') != AUTH_HEADER or COOKIE_VALUE not in self.headers.get('Cookie', '')):
                        self.reply(b'authentication required', 'text/plain', head, status=403)
                        return
                    if name == 'index' and folder == 'variants':
                        self.reply(b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=854x480,CODECS="avc1.42E01E,mp4a.40.2",FRAME-RATE=25\nlow\n'
                                   b'#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,CODECS="avc1.42E01E,mp4a.40.2",FRAME-RATE=25\nmid\n'
                                   b'#EXT-X-STREAM-INF:BANDWIDTH=2600000,RESOLUTION=1920x1080,CODECS="avc1.42E01E,mp4a.40.2",FRAME-RATE=25\nhigh\n',
                                   'application/vnd.apple.mpegurl', head, ranged=True)
                        return
                    subfolder = {'aes': 'aes', 'png': 'disguised', 'dash': 'dash', 'quality': 'quality'}.get(folder, '')
                    root = fixture.directory / subfolder
                    if name in ('index', 'low', 'mid', 'high'):
                        name = 'source.mpd' if folder == 'dash' else 'master.m3u8' if folder == 'quality' else 'source.m3u8'
                    file = (root / name).resolve()
                    if not file.is_relative_to(root.resolve()) or not file.is_file():
                        self.reply(b'', 'text/plain', head, status=404)
                        return
                    suffix = file.suffix
                    mime = {'.m3u8': 'application/vnd.apple.mpegurl', '.mpd': 'application/dash+xml',
                            '.ts': 'video/mp2t', '.png': 'image/png', '.m4s': 'video/iso.segment'}.get(suffix, 'application/octet-stream')
                    self.reply(file.read_bytes(), mime, head, ranged=True)
                    return
                self.reply(b'', 'text/plain', head, status=404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.slow_release.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)
