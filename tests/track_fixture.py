"""仅 localhost 的真实多音轨样本；不同语言用声明标签，音调用于身份检查。"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class TrackFixture:
    def __init__(self, directory, tools=None):
        self.directory = Path(directory).resolve()
        self.tools = Path(tools or ROOT / 'tools')
        self.requests = []
        self.failures = {}
        self.server = None

    def generate(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        ffmpeg = str(self.tools / 'ffmpeg.exe')
        def run(*args, cwd=None):
            subprocess.run([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-n', *map(str, args)],
                           check=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=cwd)
        run('-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12:duration=4', '-an',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-g', '12', self.directory / 'video.mp4')
        for language, frequency in (('en', 440), ('ja', 660), ('zh', 880)):
            run('-f', 'lavfi', '-i', f'sine=frequency={frequency}:sample_rate=48000:duration=4',
                '-c:a', 'aac', '-b:a', '64000', '-metadata:s:a:0', 'language=' + {'en': 'eng', 'ja': 'jpn', 'zh': 'zho'}[language],
                '-metadata:s:a:0', 'title=' + language, self.directory / (language + '.m4a'))
        run('-i', self.directory / 'video.mp4', '-i', self.directory / 'en.m4a', '-i', self.directory / 'ja.m4a',
            '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c', 'copy', self.directory / 'multi.mp4')
        run('-i', self.directory / 'video.mp4', '-i', self.directory / 'en.m4a', '-map', '0:v', '-map', '1:a',
            '-c', 'copy', self.directory / 'single.mp4')
        for language in ('en', 'ja', 'zh'):
            (self.directory / (language + '.srt')).write_text('1\n00:00:00,200 --> 00:00:01,800\n' + language +
                ' first\n\n2\n00:00:02,000 --> 00:00:03,600\n' + language + ' second\n', encoding='utf-8')
            (self.directory / (language + '.vtt')).write_text('WEBVTT\n\n00:00:00.200 --> 00:00:01.800\n' + language +
                ' first\n\n00:00:02.000 --> 00:00:03.600\n' + language + ' second\n', encoding='utf-8')
            (self.directory / (language + '-sub.m3u8')).write_text('#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\n' +
                language + '.vtt\n#EXT-X-ENDLIST\n', encoding='utf-8')
        for name in ('video', 'en', 'ja'):
            source = name + ('.mp4' if name == 'video' else '.m4a')
            run('-i', self.directory / source, '-c', 'copy', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0',
                '-hls_segment_filename', self.directory / (name + '-%03d.ts'), self.directory / (name + '.m3u8'))
        (self.directory / 'master.m3u8').write_text('''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE="en",DEFAULT=YES,CHANNELS="1",URI="en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="日本語",LANGUAGE="ja",DEFAULT=NO,CHANNELS="1",URI="ja.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="简体中文",LANGUAGE="zh-Hans",URI="zh-sub.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="en-sub.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=320x180,CODECS="avc1.42c00c,mp4a.40.2",AUDIO="audio",SUBTITLES="subs"
video.m3u8
''', encoding='utf-8')
        dash = self.directory / 'dash'
        dash.mkdir()
        run('-i', self.directory / 'multi.mp4', '-map', '0', '-c', 'copy', '-f', 'dash', '-seg_duration', '1',
            '-use_template', '1', '-use_timeline', '1', dash / 'master.mpd', cwd=dash)
        root = ET.fromstring((dash / 'master.mpd').read_text(encoding='utf-8'))
        namespace = '{urn:mpeg:dash:schema:mpd:2011}'
        period = root.find(namespace + 'Period')
        for language in ('en', 'ja'):
            adaptation = ET.SubElement(period, namespace + 'AdaptationSet', {
                'contentType': 'text', 'mimeType': 'text/vtt', 'lang': language})
            rep = ET.SubElement(adaptation, namespace + 'Representation', {'id': 'sub-' + language, 'bandwidth': '256'})
            ET.SubElement(rep, namespace + 'BaseURL').text = '../' + language + '.vtt'
        (dash / 'master.mpd').write_text(ET.tostring(root, encoding='unicode'), encoding='utf-8')

    def start(self):
        fixture = self
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(fixture.directory), **kwargs)
            def log_message(self, *_):
                pass
            def guess_type(self, path):
                suffix = Path(path).suffix.lower()
                return {'.m3u8': 'application/vnd.apple.mpegurl;charset=utf-8',
                        '.mpd': 'application/dash+xml;charset=utf-8', '.vtt': 'text/vtt;charset=utf-8',
                        '.srt': 'application/x-subrip;charset=utf-8'}.get(suffix, super().guess_type(path))
            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                fixture.requests.append({'path': path, 'range': self.headers.get('Range')})
                if path in ('/page/hls', '/page/dash', '/page/direct'):
                    resource = '/master.m3u8' if path.endswith('hls') else '/single.mp4' if path.endswith('direct') else '/dash/master.mpd'
                    track = '<track kind="subtitles" srclang="en" label="English" src="/en.vtt" default>' if path.endswith('direct') else ''
                    body = ('<!doctype html><html><head><meta charset="utf-8"><title>Local Multi Track</title></head>'
                            '<body><video id="main" data-video-id="' + path.rsplit('/', 1)[-1] +
                            '" controls muted autoplay style="width:640px;height:360px" src="' + resource +
                            '">' + track + '</video><script>fetch(' + repr(resource) + ').then(r=>r.arrayBuffer());</script></body></html>').encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html;charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if fixture.failures.get(path, 0):
                    fixture.failures[path] -= 1
                    self.send_response(503)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                super().do_GET()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port) + '/'
        return self

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
