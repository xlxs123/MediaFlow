"""双轨 MPD 复用原下载桥：正文缓存、请求上下文和独立 Range。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import struct
import threading
import unittest
import xml.etree.ElementTree as ET

from core.discovery_models import NetworkObservation
from core.downloader import DownloadRequest
from core.http_client import ContextHttpClient
from core.media_bridge import MediaBridge
from core.media_discovery import MediaDiscoveryService
from core.media_guard import check_media
from core.request_context import RequestContext


class DashDownloadBridgeTests(unittest.TestCase):
    def test_paired_mpd_never_refetches_api_and_both_tracks_keep_context(self):
        hits = []
        payload = struct.pack('>I4s', 16, b'ftyp') + b'isom\0\0\0\0' + b'track-data' * 32

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                hits.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path.startswith('/api/'):
                    self.send_error(500)
                    return
                requested = self.headers.get('Range', '')
                match = re.fullmatch(r'bytes=(\d+)-(\d+)', requested)
                data = payload[int(match[1]):int(match[2])+1] if match else payload
                self.send_response(206 if match else 200)
                self.send_header('Content-Type', 'audio/mp4' if '/audio' in self.path else 'video/mp4')
                self.send_header('Content-Length', str(len(data)))
                if match:
                    self.send_header('Content-Range', f'bytes {match[1]}-{match[2]}/{len(payload)}')
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        bridge = None
        try:
            context = RequestContext(base + '/api/playurl', headers={'User-Agent': 'Fixture Browser',
                'Referer': 'https://page.example/watch', 'Cookie': 'session=fixture',
                'Authorization': 'Bearer fixture', 'Range': 'bytes=0-'})
            def track(role):
                return {'id': role, 'baseUrl': base + '/' + role + '.m4s?sign=original%2Bvalue',
                    'bandwidth': 100000, 'width': 640 if role == 'video' else 0,
                    'height': 360 if role == 'video' else 0,
                    'codecs': 'avc1.640028' if role == 'video' else 'mp4a.40.2',
                    'mimeType': role + '/mp4',
                    'SegmentBase': {'Initialization': '0-15', 'indexRange': '16-31'}}
            data = json.dumps({'data': {'dash': {'duration': 10, 'video': [track('video')],
                                                 'audio': [track('audio')]}}}).encode()
            service = MediaDiscoveryService()
            service.consume_network(NetworkObservation('api', context, 200, 'application/json', data,
                                                       body_complete=True))
            candidate = service.ranked(include_low=True)[0]
            request = DownloadRequest('yt-dlp.exe', candidate.url, 'unused',
                headers=candidate.request_context.headers, context_snapshot=candidate.request_context,
                **candidate.download_payload())
            inspection = check_media(request.to_context(), manifest_text=request.manifest_text,
                manifest_url=request.manifest_url, manifest_origin=request.manifest_origin,
                expected_media_type=request.download_protocol)
            self.assertEqual(hits, [])
            bridge = MediaBridge(inspection).start()
            client = ContextHttpClient()
            with client.open(RequestContext(bridge.url)) as response:
                mpd = ET.fromstring(response.read())
            local_urls = [node.text for node in mpd.findall('.//{*}Representation/{*}BaseURL')]
            self.assertEqual(len(local_urls), 2)
            for url in local_urls:
                self.assertTrue(url.startswith('http://127.0.0.1:'))
                with client.open(RequestContext(url, headers={'Range': 'bytes=0-15'})) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), payload[:16])
            self.assertEqual(len(hits), 2)
            self.assertTrue(any(path.startswith('/video.m4s?sign=original%2Bvalue') for path, _ in hits))
            self.assertTrue(any(path.startswith('/audio.m4s?sign=original%2Bvalue') for path, _ in hits))
            for _, headers in hits:
                self.assertEqual(headers['range'], 'bytes=0-15')
                for key in ('user-agent', 'referer', 'cookie', 'authorization'):
                    self.assertEqual(headers[key], context.headers[key])
                self.assertNotIn('origin', headers)
        finally:
            if bridge:
                bridge.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
