from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from core.errors import MediaCheckCancelled, MediaCheckError
from core.media_bridge import _BridgeCancel, _RecoveryHttpClient
from core.request_context import RequestContext


class CooldownTests(unittest.TestCase):
    def test_retry_after_gates_different_ranges_and_manifest_refresh(self):
        hits = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                hits.append((time.monotonic(), self.path, self.headers.get('Range')))
                if len(hits) == 1:
                    self.send_response(503)
                    self.send_header('Retry-After', '1')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'ok')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            context = RequestContext(base + '/segment', headers={'Range': 'bytes=0-3'})
            client = _RecoveryHttpClient(threading.Event(), base_delay=0)
            with self.assertRaises(MediaCheckError) as caught:
                client.open(context)
            client.record_failure(context, caught.exception)
            # A different path/range on the same origin cannot bypass the cooldown.
            with client.open(RequestContext(base + '/manifest.m3u8'), {'range': 'bytes=4-7'}) as response:
                self.assertEqual(response.read(), b'ok')
            self.assertGreaterEqual(hits[1][0] - hits[0][0], .95)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_bridge_close_does_not_cancel_parent_retry(self):
        parent = threading.Event()
        current = _BridgeCancel(parent)
        current.set()
        self.assertFalse(parent.is_set())
        self.assertTrue(current.is_set())
        self.assertFalse(_BridgeCancel(parent).is_set())

    def test_cooldown_can_be_cancelled_without_another_remote_request(self):
        event = threading.Event()
        client = _RecoveryHttpClient(event)
        context = RequestContext('https://example.test/media')
        client.record_failure(context, MediaCheckError('busy', 'HTTP_ERROR',
            {'http_status': 503, 'retry_after': '60'}))
        event.set()
        with self.assertRaises(MediaCheckCancelled):
            client.open(context)


if __name__ == '__main__':
    unittest.main()
