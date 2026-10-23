from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from core.http_client import ContextHttpClient, http_failure, network_failure, HttpResponse
from core.request_context import RequestContext
from core.errors import MediaCheckError, MediaCheckCancelled


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.serve()

            def do_GET(self):
                self.serve()

            def serve(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                cls.received.append({'method': self.command, 'body': body, 'path': self.path,
                                     'headers': {k.lower(): v for k, v in self.headers.items()}})
                if self.path.startswith('/redirect/'):
                    code = int(self.path.split('/')[2].split('?')[0])
                    self.send_response(code)
                    self.send_header('Location', f'http://localhost:{cls.server.server_port}/final/index?sign=f%2Fb&t=2')
                    self.end_headers()
                elif self.path.startswith('/403'):
                    self.send_response(403)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
                    self.end_headers()
                    self.wfile.write(b'#EXTM3U\n#EXT-X-ENDLIST\n')

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_raw_signature_and_headers_transmitted(self):
        ctx = RequestContext(self.base + '/raw?sign=a%2Fb%2Bc&t=123&auth=x', headers={'Origin': 'https://page.test',
                             'Referer': 'https://page.test/watch', 'User-Agent': 'Actual browser',
                             'Cookie': 's=context-cookie', 'Authorization': 'Bearer context-auth'})
        with ContextHttpClient().open(ctx) as response:
            self.assertEqual(response.status, 200)
        received = self.received[-1]
        self.assertEqual(received['path'], '/raw?sign=a%2Fb%2Bc&t=123&auth=x')
        for name, value in ctx.headers.items():
            self.assertEqual(received['headers'][name], value)

    def test_all_required_redirect_statuses_actual(self):
        for code in (301, 302, 307, 308):
            ctx = RequestContext(self.base + f'/redirect/{code}', 'POST',
                                 {'Cookie': 'session=redirect', 'Origin': 'https://page.test',
                                  'Referer': 'https://page.test/watch', 'User-Agent': 'UA', 'Authorization': 'Bearer retained'}, b'body=value')
            with ContextHttpClient().open(ctx) as response:
                self.assertEqual(response.context.original_url, ctx.url)
                self.assertIn('/final/index?sign=f%2Fb&t=2', response.context.final_url)
                self.assertEqual(response.redirects[0]['status'], code)
            final = self.received[-1]
            self.assertEqual(final['method'], 'POST' if code in (307, 308) else 'GET')
            self.assertEqual(final['body'], ctx.body if code in (307, 308) else b'')
            for name, value in ctx.headers.items():
                self.assertEqual(final['headers'][name], value)

    def test_403_is_not_drm(self):
        with self.assertRaises(MediaCheckError) as caught:
            ContextHttpClient().open(RequestContext(self.base + '/403?sign=current&t=5'))
        self.assertEqual(caught.exception.code, 'HTTP_403')
        self.assertIsNone(caught.exception.diagnostic['drm'])
        self.assertEqual(caught.exception.diagnostic['http_status'], 403)
        self.assertIn('origin', caught.exception.diagnostic['missing_context'])

    def test_401_wwwauth_is_auth_required(self):
        self.assertEqual(http_failure(RequestContext(self.base), 401).code, 'AUTH_REQUIRED')
        self.assertEqual(http_failure(RequestContext(self.base), 403, {'WWW-Authenticate': 'Bearer'}).code, 'AUTH_REQUIRED')

    def test_expired_url_requires_actual_expiry_evidence(self):
        self.assertEqual(http_failure(RequestContext(self.base + '/?expires=1700000000'), 403).code, 'EXPIRED_URL')
        self.assertEqual(http_failure(RequestContext(self.base + '/?t=1700000000&sign=old'), 403).code, 'HTTP_403')

    def test_missing_cookie_is_not_authentication_evidence(self):
        failure = http_failure(RequestContext(self.base + '/?secure=temporary'), 403)
        self.assertNotIn('AUTH_REQUIRED', failure.diagnostic['possible_causes'])
        self.assertIn('POSSIBLE_URL_EXPIRED', failure.diagnostic['possible_causes'])
        self.assertEqual(failure.code, 'HTTP_403')

    def test_other_http_status_and_proxy_have_own_codes(self):
        self.assertEqual(http_failure(RequestContext(self.base), 404).code, 'HTTP_ERROR')
        self.assertEqual(http_failure(RequestContext(self.base), 500).code, 'HTTP_ERROR')
        self.assertEqual(http_failure(RequestContext(self.base), 407).code, 'PROXY_ERROR')

    def test_concrete_transport_exception_classification(self):
        import socket
        import ssl
        from http.client import RemoteDisconnected
        from urllib.error import URLError
        cases = [(TimeoutError('timeout'), 'connect', 'NETWORK_ERROR', 'ConnectTimeout'),
                 (TimeoutError('timeout'), 'read', 'NETWORK_ERROR', 'ReadTimeout'),
                 (ConnectionResetError('reset'), 'read', 'NETWORK_ERROR', 'ConnectionResetError'),
                 (RemoteDisconnected('closed'), 'connect', 'NETWORK_ERROR', 'RemoteDisconnected'),
                 (socket.gaierror('lookup'), 'connect', 'NETWORK_ERROR', 'DNS_ERROR'),
                 (ssl.SSLError('certificate'), 'connect', 'TLS_ERROR', 'TLS_ERROR'),
                 (OSError('Tunnel connection failed: 407 Proxy'), 'connect', 'PROXY_ERROR', 'ProxyError')]
        for reason, phase, code, kind in cases:
            failure = network_failure(RequestContext(self.base), URLError(reason), phase)
            self.assertEqual(failure.code, code)
            self.assertEqual(failure.diagnostic['exception_type'], kind)

    def test_read_timeout_keeps_actual_context(self):
        from unittest.mock import Mock
        stream = Mock()
        stream.read.side_effect = TimeoutError('late')
        response = HttpResponse(RequestContext(self.base, headers={'Referer': 'https://page.test'}), stream, [])
        with self.assertRaises(MediaCheckError) as caught:
            response.read(100)
        self.assertEqual(caught.exception.diagnostic['exception_type'], 'ReadTimeout')
        self.assertEqual(caught.exception.diagnostic['referer'], 'https://page.test')

    def test_cancel_before_request(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(MediaCheckCancelled):
            ContextHttpClient(event).open(RequestContext(self.base))

    def test_range_override_response_context_matches_wire_request(self):
        context = RequestContext(self.base + '/range', headers={'Range': 'bytes=0-'}, inherit_range=True)
        with ContextHttpClient().open(context, {'range': 'bytes=8-23'}) as response:
            self.assertEqual(response.context.summary()['request_range'], 'bytes=8-23')
            self.assertTrue(response.context.inherit_range)
        self.assertEqual(self.received[-1]['headers']['range'], 'bytes=8-23')
        self.assertEqual(context.headers['range'], 'bytes=0-')

    def test_range_override_or_removal_is_visible_in_403_diagnostic(self):
        context = RequestContext(self.base + '/403', headers={'Range': 'bytes=0-'}, inherit_range=True)
        for value in ('bytes=8-23', None):
            with self.subTest(range=value), self.assertRaises(MediaCheckError) as caught:
                ContextHttpClient().open(context, {'range': value})
            self.assertEqual(caught.exception.diagnostic['request_range'], value or '')
            self.assertIsNone(caught.exception.diagnostic['drm'])
            self.assertEqual(self.received[-1]['headers'].get('range'), value)

    def test_range_override_survives_actual_cross_host_redirect(self):
        context = RequestContext(self.base + '/redirect/307', headers={'Range': 'bytes=0-'}, inherit_range=True)
        with ContextHttpClient().open(context, {'range': 'bytes=8-23'}) as response:
            self.assertEqual(response.context.headers['range'], 'bytes=8-23')
            self.assertTrue(response.context.inherit_range)
        self.assertEqual(self.received[-1]['headers']['range'], 'bytes=8-23')
