"""自生成无版权问题的 HLS，在 localhost 校验真实请求和重试行为。"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import threading
import time
import secrets
import re
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_HEADERS = {'origin': 'https://local.fixture', 'referer': 'https://local.fixture/player?viewkey=456&x=2',
                   'user-agent': 'Fixture Browser (Windows NT 10.0) Test/1.0', 'accept': '*/*',
                   'accept-language': 'zh-CN,zh;q=0.9', 'cookie': 'session=fixture-private-cookie',
                   'authorization': 'Bearer fixture-private-auth', 'x-api-key': 'fixture-private-key'}
FIXTURE_BROWSER_METADATA = {'sec-fetch-dest': 'empty', 'sec-fetch-mode': 'cors', 'sec-fetch-site': 'cross-site',
                            'sec-fetch-storage-access': 'none', 'sec-ch-ua': '"Fixture Browser";v="1"',
                            'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"'}
DISGUISED_HEADERS = {**FIXTURE_HEADERS, **FIXTURE_BROWSER_METADATA, 'range': 'bytes=0-'}
OBSERVED_HEADERS = {name: DISGUISED_HEADERS[name] for name in ('referer', 'user-agent', 'range')}


class HlsFixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.server = None
        self.thread = None
        self.requests = []
        self.retry_counts = {}
        self.lock = threading.Lock()
        self.parallel_headers = {}
        self.parallel_stats = {}
        self.parallel_delay = 0.4
        self.parallel_active_jobs_peak = 0

    def generate(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        ffmpeg = ROOT / 'tools' / 'ffmpeg.exe'
        subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                        '-i', 'testsrc2=size=320x180:rate=25', '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=48000',
                        '-t', '7', '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-g', '25',
                        '-sc_threshold', '0', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k',
                        '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0', '-hls_segment_filename',
                        str(self.directory / 'part%03d.ts'), str(self.directory / 'source.m3u8')],
                       check=True, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
        subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(self.directory / 'source.m3u8'),
                        '-c', 'copy', '-movflags', '+faststart', str(self.directory / 'direct.mp4')], check=True,
                       shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
        subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-stream_loop', '5',
                        '-i', str(self.directory / 'direct.mp4'), '-t', '42', '-c', 'copy', str(self.directory / 'tail.mp4')],
                       check=True, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
        aes = self.directory / 'aes'
        aes.mkdir(exist_ok=True)
        (aes / 'key.dat').write_bytes(secrets.token_bytes(16))
        (aes / 'key-info.txt').write_text('key.dat\n' + str(aes / 'key.dat') + '\n' + '0' * 32 + '\n', encoding='utf-8')
        subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(self.directory / 'direct.mp4'),
                        '-c', 'copy', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0', '-hls_key_info_file',
                        str(aes / 'key-info.txt'), '-hls_segment_filename', str(aes / 'enc%03d.ts'), str(aes / 'source.m3u8')],
                       check=True, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
        disguised = self.directory / 'disguised'
        disguised.mkdir(exist_ok=True)
        (disguised / 'enc.key').write_bytes(secrets.token_bytes(16))
        (disguised / 'key-info.txt').write_text('enc.key\n' + str(disguised / 'enc.key') + '\n' + '0' * 32 + '\n', encoding='utf-8')
        subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(self.directory / 'direct.mp4'),
                        '-c', 'copy', '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0', '-hls_playlist_type', 'vod',
                        '-hls_key_info_file', str(disguised / 'key-info.txt'), '-hls_segment_filename',
                        str(disguised / 'abc%03d.png'), str(disguised / 'source.m3u8')],
                       check=True, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30)
        dash = self.directory / 'dash'
        dash.mkdir(exist_ok=True)
        subprocess.run([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(self.directory / 'direct.mp4'),
                        '-map', '0', '-c', 'copy', '-f', 'dash', '-seg_duration', '1', '-use_template', '1',
                        '-use_timeline', '1', str(dash / 'source.mpd')], check=True, shell=False,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=30, cwd=dash)

    def start(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    # yt-dlp 停止或关闭 keep-alive 的正常情形。
                    pass

            def log_message(self, *_):
                pass

            def do_GET(self):
                self.serve(False)

            def do_HEAD(self):
                self.serve(True)

            def do_POST(self):
                self.body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                self.serve(False)

            def reply(self, code, data=b'', mime='text/plain', head=False, headers=None):
                self.send_response(code)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass

            def serve(self, head):
                parsed = urlsplit(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                header_match = all(self.headers.get(name, '') == value for name, value in FIXTURE_HEADERS.items())
                intact = query.get('sign') == ['fixture/signed+value'] and query.get('t') == ['1234567890']
                with fixture.lock:
                    fixture.requests.append({'path': path, 'signed_query_intact': intact,
                                             'headers_match': header_match, 'head': head, 'method': self.command,
                                             'host': self.headers.get('Host'),
                                             'range': self.headers.get('Range'), 'has_origin': bool(self.headers.get('Origin')),
                                             'has_cookie': bool(self.headers.get('Cookie')),
                                             'has_authorization': bool(self.headers.get('Authorization')),
                                             'observed_headers_match': all(self.headers.get(name) == value for name, value in OBSERVED_HEADERS.items()),
                                             'browser_metadata_present': any(self.headers.get(name) is not None for name in FIXTURE_BROWSER_METADATA),
                                             'browser_metadata_match': all(self.headers.get(name) == value for name, value in FIXTURE_BROWSER_METADATA.items()),
                                             'post_body_match': getattr(self, 'body', b'') == b'fixture=body%2Bvalue'})
                if path.startswith('/parallel/'):
                    parts = path.split('/')
                    if len(parts) != 4 or parts[2] not in fixture.parallel_headers:
                        self.reply(404, b'Unknown fixture job', head=head)
                        return
                    job, filename = parts[2:]
                    expected = fixture.parallel_headers[job]
                    match = all(self.headers.get(name) == value for name, value in expected.items())
                    match = match and not any(self.headers.get(name) is not None and name.lower() not in expected
                                              for name in ('Origin', 'Cookie', 'Authorization'))
                    with fixture.lock:
                        stats = fixture.parallel_stats.setdefault(job, {'active': 0, 'peak': 0, 'requests': 0,
                                                                        'headers_ok': True, 'first': None, 'last': None})
                        stats['headers_ok'] = stats['headers_ok'] and match
                    if not match:
                        self.reply(403, b'Per-job request context mismatch', head=head)
                        return
                    if filename == 'index.m3u8':
                        data = (fixture.directory / 'disguised' / 'source.m3u8').read_bytes()
                        self.reply(200, data, 'application/octet-stream', head)
                        return
                    target = fixture.directory / 'disguised' / filename
                    segments = {p.name for p in (fixture.directory / 'disguised').glob('abc*')}
                    if filename not in segments | {'enc.key'} or not target.is_file():
                        self.reply(404, b'Not Found', head=head)
                        return
                    if filename == 'enc.key':
                        self.reply(200, target.read_bytes(), 'application/octet-stream', head)
                        return
                    # 延迟源响应，记录实际并行的媒体请求，避免仅验证命令参数。
                    with fixture.lock:
                        stats['active'] += 1
                        stats['requests'] += 1
                        stats['peak'] = max(stats['peak'], stats['active'])
                        stats['first'] = stats['first'] or time.monotonic()
                        fixture.parallel_active_jobs_peak = max(fixture.parallel_active_jobs_peak,
                                                                 sum(item['active'] > 0 for item in fixture.parallel_stats.values()))
                    try:
                        time.sleep(fixture.parallel_delay)
                        self.reply(200, target.read_bytes(), 'image/png', head)
                    finally:
                        with fixture.lock:
                            stats['active'] -= 1
                            stats['last'] = time.monotonic()
                    return
                if path.startswith(('/observed/', '/observed-cross/')) or path == '/observed-redirect':
                    filename = Path(path).name
                    is_manifest = filename in ('index.m3u8', 'master.m3u8') or path == '/observed-redirect'
                    if (any(self.headers.get(name) is not None for name in ('Origin', 'Cookie', 'Authorization'))
                        or any(self.headers.get(name) != OBSERVED_HEADERS[name] for name in ('referer', 'user-agent'))
                        or not self.headers.get('Range')):
                        self.reply(403, b'Only Referer, User-Agent and Range are required; no invented context', head=head)
                        return
                    if (is_manifest or filename == 'enc.key') and self.headers.get('Range') != 'bytes=0-':
                        self.reply(403, b'Full open Range required for manifest/key', head=head)
                        return
                    if path == '/observed-redirect':
                        self.reply(307, head=head, headers={'Location': fixture.base.replace('127.0.0.1', 'localhost')
                                                         + '/observed/index.m3u8?sign=redirect%2Bkeep&t=123'})
                        return
                    if is_manifest:
                        text = (fixture.directory / 'disguised' / 'source.m3u8').read_text(encoding='utf-8').replace(',IV=', ',\nIV=')
                        if filename == 'master.m3u8':
                            text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nindex.m3u8\n'
                        if path.startswith('/observed-cross/'):
                            base = fixture.base.replace('127.0.0.1', 'localhost') + '/observed-cross/'
                            text = text.replace('URI="enc.key"', f'URI="{base}enc.key"')
                            text = '\n'.join(base + line if line.startswith('abc') else line for line in text.splitlines()) + '\n'
                        data, mime = text.encode(), 'application/octet-stream'
                    else:
                        target = fixture.directory / 'disguised' / filename
                        if filename not in {'enc.key'} | {p.name for p in (fixture.directory / 'disguised').glob('abc*')} or not target.is_file():
                            self.reply(404, b'Not Found', head=head)
                            return
                        data = target.read_bytes()
                        mime = 'application/octet-stream' if filename == 'enc.key' else 'image/png'
                    bounds = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers['Range'])
                    if not bounds:
                        self.reply(416, b'Unsupported Range', head=head)
                        return
                    start = int(bounds[1])
                    end = min(len(data) - 1, int(bounds[2]) if bounds[2] else len(data) - 1)
                    if start > end:
                        self.reply(416, head=head, headers={'Content-Range': f'bytes */{len(data)}'})
                        return
                    self.reply(206, data[start:end+1], mime, head,
                               {'Content-Range': f'bytes {start}-{end}/{len(data)}', 'Accept-Ranges': 'bytes'})
                    return
                if path.startswith(('/disguised/', '/disguised-compat/', '/disguised-no-origin/')):
                    expected = FIXTURE_HEADERS if not path.startswith('/disguised-no-origin/') else {
                        name: value for name, value in FIXTURE_HEADERS.items() if name != 'origin'}
                    if not all(self.headers.get(name, '') == value for name, value in expected.items()):
                        self.reply(403, b'Session headers mismatch', head=head)
                        return
                    filename = Path(path).name
                    is_manifest = filename in ('index', 'mixed')
                    browser_match = all(self.headers.get(name) == value for name, value in FIXTURE_BROWSER_METADATA.items())
                    if is_manifest and (self.headers.get('Range') != 'bytes=0-' or not browser_match):
                        self.reply(403, b'Manifest request headers mismatch', head=head)
                        return
                    if not is_manifest and self.headers.get('Range') == 'bytes=0-':
                        self.reply(403, b'Manifest range must not reach key/segment', head=head)
                        return
                    if not is_manifest and path.startswith('/disguised-compat/') and not browser_match:
                        self.reply(403, b'Explicit browser metadata compatibility required', head=head)
                        return
                    if is_manifest:
                        data = (fixture.directory / 'disguised' / 'source.m3u8').read_text(encoding='utf-8')
                        data = data.replace(',IV=', ',\nIV=')
                        if filename == 'mixed':
                            for number, suffix in enumerate(('bin', 'dat', '', 'other')):
                                data = data.replace(f'abc{number:03}.png', f'media{number:03}' + ('.' + suffix if suffix else ''))
                        self.reply(200, data.encode(), 'application/octet-stream', head)
                        return
                    aliases = {f'abc{number:03}.png': f'abc{number:03}.png' for number in range(4)}
                    for number, suffix in enumerate(('bin', 'dat', '', 'other')):
                        aliases[f'media{number:03}' + ('.' + suffix if suffix else '')] = f'abc{number:03}.png'
                    target_name = aliases.get(filename, 'enc.key' if filename == 'enc.key' else '')
                    target = fixture.directory / 'disguised' / target_name
                    if target_name and target.is_file():
                        data = target.read_bytes()
                        mime = 'application/octet-stream' if filename == 'enc.key' else 'image/png'
                        if self.headers.get('Range', '').startswith('bytes='):
                            bounds = self.headers['Range'][6:].split('-', 1)
                            start = int(bounds[0])
                            end = min(len(data) - 1, int(bounds[1]) if bounds[1] else len(data) - 1)
                            self.reply(206, data[start:end+1], mime, head,
                                       {'Content-Range': f'bytes {start}-{end}/{len(data)}', 'Accept-Ranges': 'bytes'})
                        else:
                            self.reply(200, data, mime, head)
                        return
                    self.reply(404, b'Not Found', head=head)
                    return
                if path == '/disconnect.m3u8':
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if path == '/forbidden.m3u8':
                    self.reply(403, b'Forbidden', head=head)
                    return
                if path == '/page.html':
                    self.reply(200, b'<html><body>Not a video</body></html>', 'text/html', head)
                    return
                if path == '/protected.m3u8':
                    data = b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="secret.key",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:1,\npart000.ts\n#EXT-X-ENDLIST\n'
                    self.reply(200, data, 'application/vnd.apple.mpegurl', head)
                    return
                if path in ('/drm/widevine', '/drm/playready'):
                    scheme = 'edef8ba9-79d6-4ace-a3c8-27dcd51d21ed' if path.endswith('widevine') else '9a04f079-9840-4286-ab92-e65be0885f95'
                    data = ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>'
                            f'<ContentProtection schemeIdUri="urn:uuid:{scheme}"/>'
                            '<BaseURL>/secret.key</BaseURL></AdaptationSet></Period></MPD>').encode()
                    self.reply(200, data, 'application/dash+xml', head)
                    return
                if path.startswith(('/aes/', '/dash/', '/post/')) and not header_match:
                    self.reply(403, b'Headers mismatch', head=head)
                    return
                if path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', fixture.base.replace('127.0.0.1', 'localhost') + '/aes/endpoint?sign=redirect%2Bvalue&t=9')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if path == '/post/endpoint':
                    if self.command != 'POST' or getattr(self, 'body', b'') != b'fixture=body%2Bvalue':
                        self.reply(403, b'Method/body mismatch', head=head)
                        return
                    data = (fixture.directory / 'source.m3u8').read_text(encoding='utf-8')
                    self.reply(200, data.encode(), 'application/octet-stream', head)
                    return
                if path in ('/aes/endpoint', '/aes/rotate', '/cross-host'):
                    data = (fixture.directory / 'aes' / 'source.m3u8').read_text(encoding='utf-8')
                    data = data.replace(',IV=', ',\nIV=')
                    if path == '/cross-host':
                        # 不同origin的声明资源仍须继承整套headers，使用localhost/127两种主机名。
                        base = fixture.base.replace('127.0.0.1', 'localhost') + '/aes/'
                        data = data.replace('URI="key.dat"', f'URI="{base}key.dat"')
                        data = '\n'.join(base + line if line.startswith('enc') else line for line in data.splitlines()) + '\n'
                    self.reply(200, data.encode(), 'application/octet-stream', head)
                    return
                if path.startswith('/aes/'):
                    target = fixture.directory / 'aes' / Path(path).name
                    if target.is_file() and (target.name == 'key.dat' or target.suffix == '.ts'):
                        self.reply(200, target.read_bytes(), 'application/octet-stream' if target.name == 'key.dat' else 'video/mp2t', head)
                        return
                if path.startswith('/dash/'):
                    if path == '/dash/pssh-endpoint':
                        data = (fixture.directory / 'dash' / 'source.mpd').read_text(encoding='utf-8')
                        data = data.replace('init-stream', 'protected-init-stream')
                        self.reply(200, data.encode(), 'application/dash+xml', head)
                        return
                    if Path(path).name.startswith('protected-init-stream'):
                        filename = Path(path).name.replace('protected-init-stream', 'init-stream')
                        system_id = bytes.fromhex('edef8ba979d64acea3c827dcd51d21ed')
                        pssh = (32).to_bytes(4, 'big') + b'pssh' + b'\x00' * 4 + system_id + b'\x00' * 4
                        self.reply(200, (fixture.directory / 'dash' / filename).read_bytes() + pssh, 'video/mp4', head)
                        return
                    target = fixture.directory / 'dash' / ('source.mpd' if Path(path).name == 'endpoint' else Path(path).name)
                    if target.is_file() and target.suffix in ('.mpd', '.m4s'):
                        self.reply(200, target.read_bytes(), 'application/octet-stream' if target.suffix == '.mpd' else 'video/mp4', head)
                        return
                if path.startswith('/headers/') and not header_match:
                    self.reply(403, b'Headers mismatch', head=head)
                    return
                if path.startswith('/signed/') and not intact:
                    self.reply(403, b'Signature query mismatch', head=head)
                    return
                if path == '/master.m3u8':
                    self.reply(200, b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n/plain/index.m3u8\n',
                               'application/vnd.apple.mpegurl', head)
                    return
                if path.endswith('index.m3u8'):
                    data = (fixture.directory / 'source.m3u8').read_text(encoding='utf-8')
                    if path.startswith('/signed/'):
                        data = '\n'.join(line + '?sign=fixture%2Fsigned%2Bvalue&t=1234567890' if line.endswith('.ts') else line
                                         for line in data.splitlines()) + '\n'
                    self.reply(200, data.encode(), 'application/vnd.apple.mpegurl', head)
                    return
                filename = Path(path).name
                if filename.endswith('.ts') and path.startswith('/retry/') and filename == 'part002.ts':
                    with fixture.lock:
                        attempt = fixture.retry_counts.get(path, 0)
                        fixture.retry_counts[path] = attempt + 1
                    if attempt < 2:
                        self.reply(423, b'Locked: please retry', head=head)
                        return
                if path.startswith('/slow/') and filename.endswith('.ts'):
                    time.sleep(1.5)
                target = fixture.directory / filename
                if filename in {p.name for p in fixture.directory.glob('part*.ts')} or filename in ('direct.mp4', 'tail.mp4'):
                    data = target.read_bytes()
                    if self.headers.get('Range', '').startswith('bytes='):
                        bounds = self.headers['Range'][6:].split('-', 1)
                        start = int(bounds[0]) if bounds[0] else max(0, len(data) - int(bounds[1]))
                        end = min(len(data) - 1, int(bounds[1]) if bounds[0] and bounds[1] else len(data) - 1)
                        self.reply(206, data[start:end+1], 'video/mp4' if filename.endswith('.mp4') else 'video/mp2t', head,
                                   {'Content-Range': f'bytes {start}-{end}/{len(data)}', 'Accept-Ranges': 'bytes'})
                    else:
                        self.reply(200, data, 'video/mp4' if filename.endswith('.mp4') else 'video/mp2t', head)
                    return
                self.reply(404, b'Not Found', head=head)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
        self.thread.start()
        return self

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)

    def curl(self, mode='cmd', route='/headers/index.m3u8', headers=None):
        url = self.base + route
        headers = FIXTURE_HEADERS if headers is None else headers
        if mode == 'cmd':
            return 'curl --url ^"' + url + '^" ^\n' + ' ^\n'.join('-H ^"' + key + ': ' + value.replace('&', '^&').replace('"', '^\\^"') + '^"' for key, value in headers.items())
        return "curl --url '" + url + "' \\\n" + ' \\\n'.join("-H '" + key + ': ' + value + "'" for key, value in headers.items())
