import hashlib
import io
import ssl
import threading
import time
import unittest
from unittest.mock import patch

from core.update_transport import TransportError, TransportCancelled, fetch, https_url
from tests.update_https_fixture import HTTPSFixture


class HTTPSTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPSFixture()
        self.addCleanup(self.server.close)

    def get(self, route, **kwargs):
        return fetch(self.server.url+route, context=self.server.context,
                     connect_timeout=.5, total_timeout=6, **kwargs)

    def test_real_tls_bytes_digest_and_progress(self):
        data = bytes(range(256))*2048
        self.server.routes['/package'] = data
        target, events = io.BytesIO(), []
        size, digest = self.get('/package', destination=target, size=len(data),
                                 limit=len(data), progress=events.append)
        self.assertEqual((size,digest),(len(data),hashlib.sha256(data).hexdigest()))
        self.assertEqual(target.getvalue(), data)
        self.assertEqual(events[-1]['completed'],len(data))
        self.assertTrue(all(e['total']==len(data) for e in events))

    def test_invalid_or_credential_urls_rejected_before_process(self):
        for url in ('http://localhost/file','https://user@localhost/file','https://localhost/file#part',
                    'https://localhost:0/file', 'https://localhost\\file', ''):
            with self.subTest(url=url), self.assertRaises(TransportError):
                https_url(url)

    def test_untrusted_tls_and_tls_bypass_rejected(self):
        self.server.routes['/ok'] = b'ok'
        with self.assertRaisesRegex(TransportError,'TLS'):
            fetch(self.server.url+'/ok',context=ssl.create_default_context(),total_timeout=5)
        with self.assertRaisesRegex(TransportError,'TLS'):
            fetch(self.server.url+'/ok',context=ssl._create_unverified_context())

    def test_same_origin_redirect_allowed_cross_origin_and_downgrade_blocked(self):
        self.server.routes['/ok'] = b'expected'
        self.server.routes['/redirect'] = (302,b'',{'Location':'/ok'})
        self.assertEqual(self.get('/redirect'), b'expected')
        for destination in ('https://localhost:1/file','http://localhost/file','https://other.invalid/file'):
            self.server.routes['/blocked'] = (302,b'',{'Location':destination})
            with self.subTest(destination=destination), self.assertRaisesRegex(TransportError,'来源|HTTPS'):
                self.get('/blocked')

    def test_http_status_and_size_limits(self):
        self.server.routes['/unavailable'] = (503,b'try again')
        with self.assertRaisesRegex(TransportError,'503'):
            self.get('/unavailable')
        self.server.routes['/large'] = b'123456789'
        with self.assertRaisesRegex(TransportError,'大小'):
            self.get('/large',limit=3)
        with self.assertRaisesRegex(TransportError,'大小'):
            self.get('/large',size=8)

    def test_interrupted_response_is_not_success(self):
        def partial(handler):
            handler.send_response(200)
            handler.send_header('Content-Length','10000')
            handler.send_header('Connection','close')
            handler.end_headers()
            handler.wfile.write(b'short')
            handler.close_connection = True
        self.server.routes['/partial'] = partial
        with self.assertRaisesRegex(TransportError,'不完整|中断'):
            self.get('/partial')

    def test_total_timeout_includes_helper_start_and_blocked_response(self):
        def stall(handler):
            self.server.stopped.wait(5)
        self.server.routes['/stall'] = stall
        started = time.monotonic()
        with self.assertRaisesRegex(TransportError,'总请求时限'):
            fetch(self.server.url+'/stall',context=self.server.context,connect_timeout=3,total_timeout=.35)
        self.assertLess(time.monotonic()-started, 3)

    def test_cancel_before_start_and_after_real_bytes(self):
        event = threading.Event()
        event.set()
        with patch('core.update_transport.subprocess.Popen') as popen:
            with self.assertRaises(TransportCancelled):
                self.get('/ok',cancel=event)
            popen.assert_not_called()
        event.clear()
        self.server.routes['/large'] = b'x'*(1024*1024)
        values = []
        def progress(value):
            values.append(value)
            event.set()
        with self.assertRaises(TransportCancelled):
            self.get('/large',cancel=event,progress=progress)
        self.assertTrue(values)
        self.assertGreater(values[0]['completed'], 0)


if __name__ == '__main__':
    unittest.main()
