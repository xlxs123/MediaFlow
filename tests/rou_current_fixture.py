"""当前 API -> 302 -> PNG(roUd) 协议的 localhost 夹具，媒体由 FFmpeg 自生成。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import struct
import threading
import time
from urllib.parse import parse_qs, urlsplit
import zlib

from core.site_resolvers import RouVideoResolver
from tests.hls_fixture import HlsFixture
from tests.test_rou_playlist_decoder import chunk, png


def image_png(width, height):
    pixels = (b'\x00' + b'\x80\xa0\xc0' * width) * height
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND'))


class LocalCurrentRouResolver(RouVideoResolver):
    """生产站点匹配不变；只在测试将已确认协议映射到回环站点。"""
    def can_handle(self, page):
        return urlsplit(page.url).hostname == '127.0.0.1' and urlsplit(page.url).path.startswith('/v/')


class CurrentRouFixture:
    cookie = 'rou_fixture_session=local-authorized-session'

    def __init__(self, directory):
        self.directory = Path(directory)
        self.media = HlsFixture(self.directory)
        self.servers, self.threads, self.requests = [], [], []
        self.lock = threading.Lock()
        self.api_counts = {}
        self.expiry_cookie_issued = False
        self.expiry_triggered = False
        self.fail_once = set()
        self.wrapped_segment_requests = 0
        self.segment_flags = set()

    def generate(self):
        self.media.generate()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.servers[0].server_port}'

    def url(self, path):
        return self.base + path

    def cdn(self, number, path):
        # 真正不同的 origin；CDN 也可以用另一个 host，当前测试用不同端口。
        return f'http://127.0.0.1:{self.servers[1 + number % 3].server_port}' + path

    def directory_for(self, identifier, version):
        return {'sample-a': identifier+'-402', 'sample-b': identifier+'-720'}.get(identifier,
                    'elAo4Ubecpd' if identifier == 'sample-c' else 'new-random' if version == 'new' else 'old-random')

    def index(self, identifier, version='old'):
        folder = self.directory_for(identifier, version)
        number = {'sample-a':0,'sample-b':1,'sample-c':2}.get(identifier,1 if version=='new' else 0)
        return self.cdn(number,f'/hls/{identifier}/{folder}/index.png?v=6&exp=4102444800&auth={version}%2Factual%2Bvalue&v=6')

    def playlist(self, identifier, version='old'):
        aes = identifier == 'aes'
        folder = self.directory / 'disguised' if aes else self.directory
        text = (folder/'source.m3u8').read_text(encoding='utf-8')
        names = []
        for line in text.splitlines():
            if line and not line.startswith('#'):
                names.append(line)
        output=[]
        for line in text.splitlines():
            if line and not line.startswith('#'):
                index=names.index(line)
                name = ('CLS-'+f'{index:03}.png' if identifier=='sample-a' else
                        'C-'+f'{index:03}.png' if identifier=='sample-b' else
                        f'CLS-{index+1}-v1-a1.png' if identifier=='sample-c' else f'opaque-{index}.png')
                line=self.cdn(index,f'/asset/{identifier}/{self.directory_for(identifier,version)}/{name}'
                                    f'?exp=4102444800&auth={version}%2Fsegment&v=6')
            elif '#EXT-X-KEY:' in line and aes:
                line=line.replace('URI="enc.key"', 'URI="'+self.cdn(2,f'/asset/aes/key/enc.key?auth={version}%2Fkey&exp=4102444800')+'"')
            output.append(line)
        if '#EXT-X-PLAYLIST-TYPE:' not in text:
            output.insert(1,'#EXT-X-PLAYLIST-TYPE:VOD')
        if identifier=='drm':
            output.insert(2,'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://fixture",KEYFORMAT="com.apple.streamingkeydelivery"')
        return ('\n'.join(output)+'\n').encode()

    def start(self):
        fixture=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*_):
                pass
            def do_OPTIONS(self):
                self.reply(204,headers={'Access-Control-Allow-Headers':'Range, Authorization, Accept',
                                       'Access-Control-Allow-Methods':'GET, OPTIONS'})
            def do_GET(self):
                self.serve()
            def reply(self,status,data=b'',mime='text/plain',headers=None):
                self.send_response(status)
                self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(len(data)))
                # Echo only a canonical fixture origin, never the request value.
                origins = {f'http://127.0.0.1:{server.server_port}': f'http://127.0.0.1:{server.server_port}'
                           for server in fixture.servers}
                origin = origins.get(self.headers.get('Origin', ''))
                if origin and 'Access-Control-Allow-Origin' not in (headers or {}):
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Access-Control-Allow-Credentials','true')
                for key,value in (headers or {}).items():
                    self.send_header(key,value)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):
                    pass
            def serve(self):
                parsed=urlsplit(self.path)
                query=parse_qs(parsed.query)
                path=parsed.path
                with fixture.lock:
                    fixture.requests.append({'path':path,'query':parsed.query,'headers':{k.lower():v for k,v in self.headers.items()},
                                             'server':self.server.server_port})
                if path.startswith('/v/'):
                    identifier=path.rsplit('/',1)[-1]
                    headers={}
                    if identifier!='denied' and (identifier!='expiry' or not fixture.expiry_cookie_issued):
                        headers['Set-Cookie']=fixture.cookie+'; HttpOnly; SameSite=Lax; Path=/api'
                    elif identifier=='denied':
                        # Each scenario must explicitly establish its auth state
                        # now that discovery windows retain the app Profile.
                        headers['Set-Cookie']='rou_fixture_session=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/api'
                        if identifier=='expiry':
                            fixture.expiry_cookie_issued=True
                    tag='<video id="advert" muted autoplay src="/ads/preroll.mp4"></video>'
                    api='/api/hls/'+identifier
                    script=('fetch('+json.dumps(api+'?kind=thumbs')+').then(r=>r.arrayBuffer()).catch(()=>{});'
                            'fetch('+json.dumps(api)+',{headers:{Range:"bytes=0-"}}).then(r=>r.arrayBuffer()).catch(()=>{});')
                    if identifier=='image':
                        tag='<img src="'+api+'">'
                        script=''
                    if identifier=='generic':
                        tag='<video muted autoplay controls src="/direct.mp4"></video>'
                        script=''
                    # 没有 NEXT_DATA，模拟当前 TSR 页；错误 Generic MP4 不与正片争排名。
                    html=('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Current Rou Fixture</title></head>'
                          '<body>'+tag+'<script>window.$_TSR={};'+script+'</script></body></html>').encode()
                    return self.reply(200,html,'text/html;charset=utf-8',headers)
                if path.startswith('/api/hls/'):
                    identifier=path.rsplit('/',1)[-1]
                    if identifier=='generic':
                        return self.reply(404)
                    if fixture.cookie not in self.headers.get('Cookie',''):
                        return self.reply(401,b'authorized browser session required')
                    if query.get('kind')==['thumbs']:
                        return self.reply(302,headers={'Location':fixture.cdn(2,'/thumbs/index.png?kind=thumbs&auth=thumb')})
                    with fixture.lock:
                        fixture.api_counts[identifier]=fixture.api_counts.get(identifier,0)+1
                        count=fixture.api_counts[identifier]
                    version='new' if identifier=='expiry' and fixture.expiry_triggered else 'old'
                    return self.reply(302,headers={'Location':fixture.index(identifier,version)})
                if path=='/thumbs/index.png':
                    body=b'#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:1,\nthumb.png\n#EXT-X-ENDLIST\n'
                    return self.reply(200,png(zlib.compress(body),1,before=chunk(b'IDAT',zlib.compress(b'\0\0\0\0\xff'))),'image/png')
                match=re.fullmatch(r'/hls/([^/]+)/[^/]+/index.png',path)
                if match:
                    identifier=match[1]
                    version='new' if query.get('auth',[''])[0].startswith('new/') else 'old'
                    body=png(zlib.compress(fixture.playlist(identifier,version)),1,
                             before=chunk(b'IDAT',zlib.compress(b'\0\0\0\0\xff')))
                    status,headers=200,{'ETag':'"current-protocol-v6"','Accept-Ranges':'bytes'}
                    if identifier == 'cors':
                        headers['Access-Control-Allow-Origin'] = '*'
                    if self.headers.get('Range')=='bytes=0-':
                        status=206
                        headers['Content-Range']=f'bytes 0-{len(body)-1}/{len(body)}'
                    return self.reply(status,body,'image/png',headers)
                match=re.fullmatch(r'/asset/([^/]+)/[^/]+/([^/]+)',path)
                if match:
                    identifier,name=match.groups()
                    if name=='enc.key':
                        return self.reply(200,(fixture.directory/'disguised/enc.key').read_bytes(),'application/octet-stream')
                    if identifier == 'png-segments':
                        return self.reply(200, image_png(864, 864), 'image/png')
                    number=re.search(r'\d+',name)
                    index=int(number[0])-(1 if identifier=='sample-c' else 0) if number else 0
                    version='new' if query.get('auth',[''])[0].startswith('new/') else 'old'
                    if identifier=='expiry' and version=='old' and index>=2:
                        fixture.expiry_triggered = True
                        time.sleep(.08)
                        return self.reply(401,b'expired signed media URL')
                    key=(identifier,index)
                    if identifier=='retry' and index==2 and key not in fixture.fail_once:
                        fixture.fail_once.add(key)
                        return self.reply(500,b'temporary failure')
                    time.sleep(.03)
                    file=(fixture.directory/'disguised'/f'abc{index:03}.png' if identifier=='aes'
                          else fixture.directory/f'part{index:03}.ts')
                    if not file.is_file():
                        return self.reply(404)
                    payload = file.read_bytes()
                    flag = 1 if identifier == 'sample-a' or identifier == 'sample-c' and index % 2 else 0
                    body = png(zlib.compress(payload) if flag else payload, flag,
                               before=chunk(b'IDAT', zlib.compress(b'\0\0\0\0\xff')))
                    with fixture.lock:
                        fixture.wrapped_segment_requests += 1
                        fixture.segment_flags.add(flag)
                    status, headers = 200, {'Accept-Ranges': 'bytes'}
                    if self.headers.get('Range') == 'bytes=0-':
                        status = 206
                        headers['Content-Range'] = f'bytes 0-{len(body)-1}/{len(body)}'
                    return self.reply(status,body,'image/png',headers)
                if path in ('/direct.mp4','/ads/preroll.mp4'):
                    data=(fixture.directory/'direct.mp4').read_bytes()
                    headers={'Accept-Ranges':'bytes'}
                    value=self.headers.get('Range','')
                    match=re.fullmatch(r'bytes=(\d+)-(\d*)',value)
                    if match:
                        start=int(match[1]);end=min(len(data)-1,int(match[2]) if match[2] else len(data)-1)
                        headers['Content-Range']=f'bytes {start}-{end}/{len(data)}'
                        return self.reply(206,data[start:end+1],'video/mp4',headers)
                    return self.reply(200,data,'video/mp4',headers)
                if path == '/invalid-output/index.mp4':
                    # 真实 PNG + 欺骗性 video/mp4 MIME，旧版会原样保存并报成功。
                    return self.reply(200, image_png(320, 192), 'video/mp4')
                return self.reply(404)
        for _ in range(4):
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            server.daemon_threads=True
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
            self.servers.append(server);self.threads.append(thread);thread.start()
        return self

    def close(self):
        for server,thread in zip(self.servers,self.threads):
            server.shutdown();server.server_close();thread.join(timeout=2)
