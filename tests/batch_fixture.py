"""一键批量下载的本地真实媒体页面，无第三方资源。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

from tests.hls_fixture import HlsFixture


class BatchFixture:
    def __init__(self, directory, tools_directory):
        self.directory = Path(directory)
        self.tools_directory = Path(tools_directory)
        self.media = HlsFixture(self.directory)
        self.server = self.thread = None
        self.requests = []
        self.fail_direct = self.expired_hls = False
        self.long_titles = False
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.delay = .2

    def generate(self):
        self.media.generate()
        # Qt Chromium 在当前构建不含 H.264；使用标准 MP4 中的 VP9/Opus
        # 读取真实播放器证据，下载端与浏览器始终收到完全相同的字节。
        subprocess.run([str(self.tools_directory / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(self.directory / 'direct.mp4'), '-c:v', 'libvpx-vp9', '-threads', '1',
            '-deadline', 'realtime', '-cpu-used', '8', '-b:v', '300k', '-c:a', 'libopus', '-movflags', '+faststart',
            str(self.directory / 'batch-direct.mp4')], shell=False, check=True, timeout=45,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for height in (360, 720):
            target = self.directory / str(height)
            target.mkdir(exist_ok=True)
            subprocess.run([str(self.tools_directory / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
                '-i', str(self.directory / 'direct.mp4'), '-vf', f'scale={height * 16 // 9}:{height}',
                '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-g', '25', '-sc_threshold', '0',
                '-c:a', 'aac', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0',
                '-hls_segment_filename', str(target / 'part%03d.ts'), str(target / 'source.m3u8')],
                shell=False, check=True, timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        dash = self.directory / 'batch-dash'
        dash.mkdir(exist_ok=True)
        subprocess.run([str(self.tools_directory / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(self.directory / 'direct.mp4'), '-map', '0:v:0', '-map', '0:v:0', '-map', '0:a:0',
            '-filter:v:0', 'scale=640:360', '-filter:v:1', 'scale=1280:720', '-c:v', 'libx264',
            '-threads', '1', '-preset', 'ultrafast', '-b:v:0', '800k', '-b:v:1', '1400k', '-g', '25',
            '-sc_threshold', '0', '-c:a', 'aac', '-f', 'dash', '-seg_duration', '1',
            '-adaptation_sets', 'id=0,streams=v id=1,streams=a', str(dash / 'source.mpd')],
            shell=False, check=True, timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), cwd=dash)

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def page(self, mode='normal'):
        if mode == 'dash':
            return b'<!doctype html><html><head><title>DASH fixture</title></head><body><video title="DASH Video" src="/dash/index.mpd"></video><script>fetch("/dash/index.mpd").then(r=>r.arrayBuffer())</script></body></html>'
        sources = ['/hls/a/master.m3u8?token=fixture', '/direct/b.mp4?token=fixture', '/hls/c/index.m3u8?token=fixture']
        title = '视' * 220 + ' Video' if self.long_titles else 'Video'
        videos = ''.join(f'<video controls muted preload="metadata" title="{title}" data-video-id="{index}" '
                         f'src="{source}" style="width:200px;height:110px"></video>' for index, source in enumerate(sources))
        return ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Batch Fixture</title></head><body>'
                + videos + '<script>const sources=' + json.dumps(sources) + ';'
                'for(const s of sources)fetch(s,{headers:{Range:"bytes=0-"}}).then(r=>r.arrayBuffer());'
                'fetch("/ad/vast.xml").then(r=>r.text());fetch("/ad/preroll.mp4").then(r=>r.arrayBuffer());'
                'fetch("/thumbs/poster.png");</script></body></html>').encode()

    def start(self):
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_HEAD(self):
                self.serve(True)

            def do_GET(self):
                self.serve(False)

            def reply(self, status, data=b'', mime='text/plain', head=False):
                self.send_response(status)
                self.send_header('Content-Type', mime)
                if status in (200, 206) and self.headers.get('Range') and not head:
                    match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers['Range'])
                    if match:
                        start = int(match[1]); end = min(len(data) - 1, int(match[2]) if match[2] else len(data) - 1)
                        self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                        data = data[start:end + 1]
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(data)
                    except (OSError, BrokenPipeError):
                        pass

            def serve(self, head):
                path = urlsplit(self.path).path
                is_segment = path.endswith(('.ts', '.png')) and path.startswith('/hls/')
                with fixture.lock:
                    fixture.requests.append({'path': path, 'range': self.headers.get('Range'),
                        'referer': self.headers.get('Referer'), 'ua': bool(self.headers.get('User-Agent')),
                        'origin': bool(self.headers.get('Origin')), 'cookie': bool(self.headers.get('Cookie')),
                        'auth': bool(self.headers.get('Authorization'))})
                if path in ('/page', '/page/dash'):
                    return self.reply(200, fixture.page('dash' if path.endswith('/dash') else 'normal'), 'text/html', head)
                if path.startswith('/dash/'):
                    filename = path.rsplit('/', 1)[-1]
                    if filename == 'index.mpd' or re.fullmatch(r'(?:init|chunk)-stream\d+(?:-\d+)?\.m4s', filename):
                        target = fixture.directory / 'batch-dash' / ('source.mpd' if filename == 'index.mpd' else filename)
                        if target.is_file():
                            return self.reply(200, target.read_bytes(), 'application/dash+xml' if filename == 'index.mpd' else 'application/octet-stream', head)
                    return self.reply(404, head=head)
                if path == '/ad/vast.xml':
                    return self.reply(200, b'<VAST><Ad><InLine><Creatives><Creative><Linear><MediaFiles><MediaFile>/ad/preroll.mp4</MediaFile></MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>', 'application/xml', head)
                if path == '/ad/preroll.mp4':
                    return self.reply(200, (fixture.directory / 'direct.mp4').read_bytes(), 'video/mp4', head)
                if path == '/thumbs/poster.png':
                    return self.reply(200, b'\x89PNG\r\n\x1a\nordinary-image', 'image/png', head)
                if path == '/direct/b.mp4':
                    if fixture.fail_direct:
                        return self.reply(500, b'Test isolated failure', head=head)
                    return self.reply(206 if self.headers.get('Range') else 200,
                                      (fixture.directory / 'batch-direct.mp4').read_bytes(), 'video/mp4', head)
                if path.startswith('/hls/a/') and fixture.expired_hls:
                    return self.reply(403, b'Test signed expiry', head=head)
                if path == '/hls/a/master.m3u8':
                    master = '#EXTM3U\n'
                    for height in (180, 360, 720):
                        master += f'#EXT-X-STREAM-INF:BANDWIDTH={height * 5000},RESOLUTION={height * 16 // 9}x{height}\n{height}/index.m3u8?token=fixture\n'
                    return self.reply(200, master.encode(), 'application/octet-stream', head)
                target = None
                if path.startswith('/hls/a/'):
                    parts = path.split('/')
                    if len(parts) == 5 and parts[3] in ('180', '360', '720'):
                        folder = fixture.directory if parts[3] == '180' else fixture.directory / parts[3]
                        filename = 'source.m3u8' if parts[4] == 'index.m3u8' else parts[4]
                        if filename == 'source.m3u8' or re.fullmatch(r'part\d+\.ts', filename):
                            target = folder / filename
                elif path.startswith('/hls/c/'):
                    filename = path.rsplit('/', 1)[-1]
                    if filename == 'index.m3u8':
                        target = fixture.directory / 'disguised' / 'source.m3u8'
                    elif filename == 'enc.key' or re.fullmatch(r'abc\d+\.png', filename):
                        target = fixture.directory / 'disguised' / filename
                if target is None or not target.is_file():
                    return self.reply(404, head=head)
                if is_segment and not head:
                    with fixture.lock:
                        fixture.active += 1
                        fixture.peak = max(fixture.peak, fixture.active)
                    try:
                        time.sleep(fixture.delay)
                        self.reply(200, target.read_bytes(), 'image/png' if target.suffix == '.png' else 'video/mp2t', head)
                    finally:
                        with fixture.lock:
                            fixture.active -= 1
                else:
                    self.reply(200, target.read_bytes(), 'application/octet-stream', head)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(3)
