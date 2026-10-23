import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from core.errors import MediaCheckError
from core.http_client import ContextHttpClient
from core.request_context import RequestContext
from core.segment_cache import SegmentCache


class CacheStreamingTests(unittest.TestCase):
    payload = bytes(range(256)) * 16
    modified = 'Wed, 21 Oct 2015 07:28:00 GMT'

    @classmethod
    def setUpClass(cls):
        cls.received = []
        cls.release_body = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                path = urlsplit(self.path).path
                requested = self.headers.get('Range')
                previous = sum(item['path'] == path for item in cls.received)
                cls.received.append({'path': path, 'range': requested,
                                     'if-range': self.headers.get('If-Range')})
                data = cls.payload[:16] if path == '/small' else cls.payload
                start, end = 0, len(data) - 1
                status = 200
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', requested or '')
                if match and path != '/ignore-range':
                    start = int(match[1])
                    end = min(int(match[2]), end) if match[2] else end
                    status = 206
                if path == '/bad-range' and requested:
                    start += 1
                self.send_response(status)
                if path not in ('/no-validator', '/last-modified', '/no-validator-new-etag'):
                    self.send_header('ETag', '"asset-v2"' if path == '/changing' and requested else '"asset-v1"')
                if path == '/no-validator-new-etag' and previous:
                    self.send_header('ETag', '"asset-v2"')
                if path == '/last-modified':
                    self.send_header('Last-Modified', cls.modified)
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                body = data[start:end + 1]
                if path not in ('/range-no-length', '/range-short', '/unknown-length'):
                    self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Connection', 'close')
                self.send_header('Set-Cookie', 'session=PRIVATE-RESPONSE')
                self.end_headers()
                self.close_connection = True
                try:
                    if path == '/slow-large':
                        self.wfile.write(body[:16])
                        self.wfile.flush()
                        cls.release_body.wait(3)
                        self.wfile.write(body[16:])
                    elif path == '/disconnect' and not requested and not previous:
                        self.wfile.write(body[:1024])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)
                    elif path == '/range-short':
                        self.wfile.write(body[:4])
                    else:
                        self.wfile.write(body)
                except OSError:
                    pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.release_body.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.progress = []
        self.cache = SegmentCache(self.folder.name, 'same-inspected-layout', self.progress.append,
                                  max_resource_bytes=32)
        self.client = ContextHttpClient(timeout=2)
        self.received.clear()
        self.release_body.clear()

    def tearDown(self):
        self.release_body.set()
        self.folder.cleanup()

    def context(self, route):
        return RequestContext(self.base + route, headers={'Cookie': 'session=PRIVATE-REQUEST'})

    def validators(self):
        return [json.loads(path.read_text('utf-8')) for path in Path(self.folder.name).glob('*.validators.json')]

    def test_large_prefix_is_original_live_stream_before_body_finishes(self):
        started = time.monotonic()
        response = self.cache.open(self.client, self.context('/slow-large'), {}, 'prefix')
        try:
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(response.stream.closed)
            self.assertEqual(response.expected_length, len(self.payload))
            self.assertEqual(response.read(16), self.payload[:16])
            self.release_body.set()
            self.assertEqual(response.read(), self.payload[16:])
        finally:
            response.close()
        self.assertEqual(list(Path(self.folder.name).glob('*.blob')), [])
        self.assertEqual(self.cache.completed, 0)

    def test_unknown_length_prefix_returns_live_stream_without_full_pre_read(self):
        with self.cache.open(self.client, self.context('/unknown-length'), {}, 'prefix') as response:
            self.assertIsNone(response.expected_length)
            self.assertEqual(response.read(), self.payload)
        self.assertFalse(list(Path(self.folder.name).glob('*.blob')))
        self.assertEqual(self.validators()[0]['etag'], '"asset-v1"')

    def test_large_interruption_resumes_with_old_etag_and_exact_remaining_bytes(self):
        context = self.context('/disconnect?signature=PRIVATE-SIGNATURE')
        with self.cache.open(self.client, context, {}, 'prefix') as response:
            prefix = response.read(1024)
            self.assertEqual(prefix, self.payload[:1024])
            with self.assertRaises(MediaCheckError) as caught:
                response.read()
            self.assertEqual(caught.exception.code, 'NETWORK_ERROR')
        # 模拟进程重启：验证器从磁盘还原，不依赖上一个 cache 实例的内存。
        restarted = SegmentCache(self.folder.name, 'same-inspected-layout', max_resource_bytes=32)
        with restarted.open(self.client, context, {'range': 'bytes=1024-'}, 'prefix') as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.expected_length, 3072)
            tail = response.read()
        self.assertEqual(hashlib.sha256(prefix + tail).digest(), hashlib.sha256(self.payload).digest())
        self.assertEqual(self.received[-1]['range'], 'bytes=1024-')
        self.assertEqual(self.received[-1]['if-range'], '"asset-v1"')
        self.assertFalse(list(Path(self.folder.name).glob('*.blob')))

    def test_initial_bounded_probe_uses_content_range_total_for_later_resume(self):
        context = self.context('/large')
        with self.cache.open(self.client, context, {'range': 'bytes=0-7'}, 'prefix') as response:
            self.assertEqual(response.read(), self.payload[:8])
        record = self.validators()[0]
        self.assertEqual(record['content-length'], '8')
        self.assertEqual(record['content-range'], 'bytes 0-7/4096')
        with self.cache.open(self.client, context, {'range': 'bytes=1024-'}, 'prefix') as response:
            self.assertEqual(response.expected_length, 3072)
            self.assertEqual(response.read(), self.payload[1024:])
        self.assertEqual(self.received[-1]['if-range'], '"asset-v1"')

    def test_range_without_content_length_is_cached_only_after_exact_length(self):
        context = self.context('/range-no-length')
        for _ in range(2):
            with self.cache.open(self.client, context, {'range': 'bytes=8-15'}, 'prefix') as response:
                self.assertEqual(response.read(), self.payload[8:16])
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.cache.hits, 1)
        self.assertEqual(next(Path(self.folder.name).glob('*.blob')).stat().st_size, 8)

    def test_truncated_206_without_content_length_never_becomes_cache_hit(self):
        context = self.context('/range-short')
        for _ in range(2):
            with self.assertRaises(MediaCheckError) as caught:
                self.cache.open(self.client, context, {'range': 'bytes=8-15'}, 'prefix')
            self.assertEqual(caught.exception.code, 'NETWORK_ERROR')
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.cache.hits, 0)
        self.assertEqual(self.cache.completed, 0)
        self.assertFalse(list(Path(self.folder.name).glob('*.blob')))

    def test_legacy_short_cache_with_self_consistent_hash_is_quarantined(self):
        context = self.context('/range-no-length')
        overrides = {'range': 'bytes=8-15'}
        key = self.cache._key(context, overrides, 'prefix')
        body = Path(self.folder.name, key + '.blob')
        metadata = Path(self.folder.name, key + '.json')
        body.write_bytes(b'abcd')
        metadata.write_text(json.dumps({'version': 1, 'status': 206, 'size': 4,
            'sha256': hashlib.sha256(b'abcd').hexdigest(), 'headers': {'Content-Range': 'bytes 8-15/4096'},
            'role': 'prefix'}), 'utf-8')
        with self.cache.open(self.client, context, overrides, 'prefix') as response:
            self.assertEqual(response.read(), self.payload[8:16])
        self.assertEqual(self.cache.hits, 0)
        self.assertEqual(len(list(Path(self.folder.name).glob('*.corrupt-*'))), 1)

    def test_missing_old_validator_strips_range_instead_of_trusting_new_etag(self):
        context = self.context('/no-validator-new-etag')
        with self.cache.open(self.client, context, {}, 'prefix') as response:
            self.assertEqual(response.read(1024), self.payload[:1024])
        for _ in range(2):
            with self.cache.open(self.client, context, {'range': 'bytes=1024-', 'if-range': 'untrusted-new'},
                                 'prefix') as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), self.payload)
            self.assertIsNone(self.received[-1]['range'])
            self.assertIsNone(self.received[-1]['if-range'])
        self.assertNotIn('etag', self.validators()[0])
        self.assertTrue(any(values.get('recovery_state') == 'restarting' for values in self.progress))

    def test_leading_zero_nonzero_resume_offset_also_requires_old_validator(self):
        with self.cache.open(self.client, self.context('/no-validator'), {'range': 'bytes=0008-'}, 'prefix') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.payload)
        self.assertIsNone(self.received[-1]['range'])

    def test_changed_etag_and_wrong_resume_range_abort_without_updating_old_identity(self):
        for route in ('/changing', '/bad-range'):
            with self.subTest(route=route):
                context = self.context(route)
                with self.cache.open(self.client, context, {}, 'prefix') as response:
                    response.read(16)
                with self.assertRaises(MediaCheckError) as caught:
                    self.cache.open(self.client, context, {'range': 'bytes=1024-'}, 'prefix')
                self.assertIn(caught.exception.code, ('RESUME_MISMATCH', 'INCOMPLETE_MEDIA_RESPONSE'))
                self.assertEqual(self.received[-1]['if-range'], '"asset-v1"')
        self.assertTrue(all(values['etag'] == '"asset-v1"' for values in self.validators()))

    def test_valid_last_modified_can_resume_without_strong_etag(self):
        context = self.context('/last-modified')
        with self.cache.open(self.client, context, {}, 'prefix') as response:
            response.read(16)
        with self.cache.open(self.client, context, {'range': 'bytes=1024-'}, 'prefix') as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.payload[1024:])
        self.assertEqual(self.received[-1]['if-range'], self.modified)

    def test_ignored_bounded_range_is_rejected_even_with_old_reliable_validator(self):
        context = self.context('/ignore-range')
        with self.cache.open(self.client, context, {}, 'prefix') as response:
            response.read(16)
        with self.assertRaises(MediaCheckError) as caught:
            self.cache.open(self.client, context, {'range': 'bytes=8-15'}, 'prefix')
        self.assertEqual(caught.exception.code, 'INCOMPLETE_MEDIA_RESPONSE')

    def test_ignored_unbounded_resume_range_returns_full_200_for_safe_restart(self):
        context = self.context('/ignore-range')
        with self.cache.open(self.client, context, {}, 'prefix') as response:
            response.read(16)
        with self.cache.open(self.client, context, {'range': 'bytes=1024-'}, 'prefix') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.payload)
        self.assertTrue(any(values.get('recovery_state') == 'restarting' for values in self.progress))

    def test_small_fragments_keep_complete_sha_cache_and_validator_files_are_private(self):
        context = self.context('/small?token=PRIVATE-SIGNATURE')
        for _ in range(2):
            with self.cache.open(self.client, context, {}, 'segment') as response:
                self.assertEqual(response.read(), self.payload[:16])
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.cache.completed, 1)
        for path in Path(self.folder.name).glob('*.json'):
            text = path.read_text('utf-8')
            self.assertNotIn('PRIVATE', text)
            self.assertNotIn('127.0.0.1', text)
            self.assertNotIn('/small', text)
            self.assertNotIn('Set-Cookie', text)
            if path.name.endswith('.validators.json'):
                self.assertLessEqual(set(json.loads(text)), {'etag', 'last-modified', 'content-length', 'content-range'})

    def test_default_cache_limit_and_invalid_limits(self):
        self.assertEqual(SegmentCache(self.folder.name, 'other').max_resource_bytes, 32 * 1024 * 1024)
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SegmentCache(self.folder.name, 'other', max_resource_bytes=value)


if __name__ == '__main__':
    unittest.main()
