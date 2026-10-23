from email.utils import formatdate
import errno
from http.client import IncompleteRead, RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError

from core.errors import MediaCheckCancelled, MediaCheckError
from core.http_client import ContextHttpClient, http_failure, network_failure
from core.http_ranges import validate_direct_response, validate_resume_response
from core.request_context import RequestContext
from core.retry_policy import is_retryable, retry_delay, wait_for_retry


class ResumeResponseTests(unittest.TestCase):
    modified = 'Wed, 21 Oct 2015 07:28:00 GMT'

    def expected(self, **updates):
        result = {'etag': '"asset-v1"', 'content-length': '16', 'last-modified': self.modified}
        result.update(updates)
        return result

    def partial(self, **updates):
        result = {'ETag': '"asset-v1"', 'Content-Length': '8', 'Content-Range': 'bytes 8-15/16'}
        result.update(updates)
        return result

    def test_correct_206_allows_only_the_requested_remaining_bytes(self):
        result = validate_resume_response(206, self.partial(), 'bytes=8-', self.expected())
        self.assertEqual(result, {'mode': 'resume', 'expected_length': 8, 'total': 16, 'offset': 8})
        full = validate_resume_response(206, self.partial(**{'Content-Length': '16',
                                       'Content-Range': 'bytes 0-15/16'}), 'bytes=0-', self.expected())
        self.assertEqual(full['mode'], 'download')

    def test_probe_content_length_is_not_confused_with_entire_resource_length(self):
        expected = self.expected(**{'content-length': '4', 'content-range': 'bytes 0-3/16'})
        self.assertEqual(validate_resume_response(206, self.partial(), 'bytes=8-', expected)['total'], 16)

    def test_ignored_range_200_requires_full_restart(self):
        result = validate_resume_response(200, self.expected(), 'bytes=8-', self.expected())
        self.assertEqual(result['mode'], 'restart')
        self.assertEqual(result['offset'], 0)
        self.assertEqual(result['expected_length'], 16)
        self.assertEqual(validate_resume_response(200, self.expected())['mode'], 'download')

    def test_missing_content_length_uses_range_length_or_known_total(self):
        partial = self.partial()
        partial.pop('Content-Length')
        self.assertEqual(validate_resume_response(206, partial, 'bytes=8-', self.expected())['expected_length'], 8)
        self.assertEqual(validate_resume_response(200, {'ETag': '"asset-v1"'}, '',
                                                  self.expected())['expected_length'], 16)

    def test_wrong_range_missing_range_and_inconsistent_length_are_rejected(self):
        cases = [{'Content-Range': 'bytes 7-14/16'}, {'Content-Range': 'bytes 8-14/16'},
                 {'Content-Range': 'bytes 15-8/16'}, {'Content-Range': 'bytes 8-15/*'},
                 {'Content-Range': ''}, {'Content-Length': '7'}, {'Content-Length': '-8'},
                 {'Content-Length': '8,8'}, {'Content-Length': '+8'}]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(MediaCheckError) as caught:
                validate_resume_response(206, self.partial(**updates), 'bytes=8-', self.expected())
            self.assertEqual(caught.exception.code, 'INCOMPLETE_MEDIA_RESPONSE')

    def test_changed_or_missing_etag_and_changed_total_abort_append(self):
        cases = [{'ETag': '"asset-v2"'}, {'ETag': 'W/"asset-v1"'}, {'ETag': ''},
                 {'Content-Range': 'bytes 8-16/17', 'Content-Length': '9'}]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(MediaCheckError) as caught:
                validate_resume_response(206, self.partial(**updates), 'bytes=8-', self.expected())
            self.assertEqual(caught.exception.code, 'RESUME_MISMATCH')

    def test_weak_etag_requires_last_modified_comparison_if_available(self):
        expected = self.expected(etag='W/"weak-v1"')
        response = self.partial(**{'ETag': 'W/"weak-v2"', 'Last-Modified': self.modified})
        self.assertEqual(validate_resume_response(206, response, 'bytes=8-', expected)['mode'], 'resume')
        response['Last-Modified'] = 'Wed, 21 Oct 2015 07:29:00 GMT'
        with self.assertRaises(MediaCheckError) as caught:
            validate_resume_response(206, response, 'bytes=8-', expected)
        self.assertEqual(caught.exception.code, 'RESUME_MISMATCH')

    def test_nonzero_range_without_reliable_validator_cannot_append(self):
        for expected in ({'content-length': '16'}, {'etag': 'W/"weak-v1"'},
                         {'last-modified': 'invalid-date'}, {'etag': 'not-a-valid-entity-tag'}):
            with self.subTest(expected=expected), self.assertRaises(MediaCheckError) as caught:
                validate_resume_response(206, self.partial(), 'bytes=8-', expected)
            self.assertEqual(caught.exception.code, 'RESUME_MISMATCH')
        self.assertEqual(validate_resume_response(206, {'Content-Range': 'bytes 0-15/16',
                         'Content-Length': '16'}, 'bytes=0-', {})['mode'], 'download')

    def test_416_only_accepts_exact_complete_offset_and_confirmed_identity(self):
        response = {'Content-Range': 'bytes */16', 'ETag': '"asset-v1"', 'Content-Length': '0'}
        self.assertEqual(validate_resume_response(416, response, 'bytes=16-', self.expected())['mode'], 'complete')
        for offset in (8, 17):
            with self.subTest(offset=offset), self.assertRaises(MediaCheckError):
                validate_resume_response(416, response, f'bytes={offset}-', self.expected())
        for changed in ({'ETag': '"asset-v2"'}, {'ETag': ''}, {'Content-Range': 'bytes */17'}):
            with self.subTest(changed=changed), self.assertRaises(MediaCheckError):
                validate_resume_response(416, {**response, **changed}, 'bytes=16-', self.expected())
        with self.assertRaises(MediaCheckError):
            validate_resume_response(416, {'Content-Range': 'bytes */16'}, 'bytes=16-', {'content-length': '16'})

    def test_416_can_use_valid_last_modified_but_not_weak_or_invalid_validator(self):
        response = {'Content-Range': 'bytes */16', 'Last-Modified': self.modified}
        expected = {'content-length': '16', 'last-modified': self.modified}
        self.assertEqual(validate_resume_response(416, response, 'bytes=16-', expected)['mode'], 'complete')
        for value in ('invalid-date', ''):
            with self.subTest(value=value), self.assertRaises(MediaCheckError):
                validate_resume_response(416, {'Content-Range': 'bytes */16', 'Last-Modified': value,
                    'ETag': 'W/"weak"'}, 'bytes=16-', {'last-modified': value, 'etag': 'W/"weak"'})

    def test_closed_and_suffix_ranges_are_checked_and_multiple_ranges_rejected(self):
        for request in ('bytes=8-15', 'bytes=8-99', 'bytes=-8'):
            self.assertEqual(validate_resume_response(206, self.partial(), request, self.expected())['expected_length'], 8)
        for request in ('bytes=8-7', 'bytes=-0', 'bytes=0-3,8-15', 'items=8-', 'bytes=-', 'bytes=invalid'):
            with self.subTest(request=request), self.assertRaises(MediaCheckError):
                validate_resume_response(206, self.partial(), request, self.expected())

    def test_unsolicited_partial_response_or_conflicting_200_range_is_rejected(self):
        with self.assertRaises(MediaCheckError):
            validate_resume_response(206, self.partial(), '', self.expected())
        with self.assertRaises(MediaCheckError):
            validate_resume_response(200, self.partial(), '', self.expected())

    def test_old_direct_response_api_remains_compatible(self):
        self.assertEqual(validate_direct_response(206, self.partial(), 'bytes=8-', 16), 8)
        self.assertEqual(validate_direct_response(200, {'Content-Length': '16'}, '', 16), 16)


class RetryPolicyTests(unittest.TestCase):
    def error(self, status=None, **diagnostic):
        return MediaCheckError('failure', 'NETWORK_ERROR' if status is None else 'HTTP_ERROR',
                               {'http_status': status, **diagnostic})

    def test_temporary_http_statuses_retry_and_permanent_statuses_do_not(self):
        for status in (429, 500, 502, 503, 504):
            self.assertTrue(is_retryable(self.error(status)), status)
        for status in (400, 401, 403, 404, 407, 410, 416, 423, 501, 505):
            self.assertFalse(is_retryable(self.error(status)), status)

    def test_temporary_connections_and_dns_retry_permanent_dns_does_not(self):
        for reason in (TimeoutError('timed out'), ConnectionResetError('reset'),
                       RemoteDisconnected('closed'), IncompleteRead(b'partial', 8),
                       socket.gaierror(socket.EAI_AGAIN, 'temporary lookup failure')):
            self.assertTrue(is_retryable(URLError(reason)), type(reason).__name__)
            self.assertTrue(is_retryable(network_failure(RequestContext('https://media.example/video'), reason)))
        permanent = socket.gaierror(socket.EAI_NONAME, 'no such host')
        self.assertFalse(is_retryable(permanent))
        self.assertFalse(is_retryable(network_failure(RequestContext('https://media.example/video'), permanent)))

    def test_early_eof_during_successful_http_response_is_still_temporary(self):
        for status in (200, 206):
            self.assertTrue(is_retryable(MediaCheckError('early EOF', 'NETWORK_ERROR',
                            {'http_status': status, 'exception_type': 'IncompleteRead'})))

    def test_unexpected_tls_transport_eof_retries_with_verification_still_required(self):
        eof = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, 'EOF occurred in violation of protocol')
        for error in (eof, URLError(eof)):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(is_retryable(error))
                failure = network_failure(RequestContext('https://media.example/video'), error)
                self.assertEqual(failure.code, 'NETWORK_ERROR')
                self.assertEqual(failure.diagnostic['exception_type'], 'SSLEOFError')
                self.assertTrue(is_retryable(failure))

    def test_certificate_and_unknown_tls_failures_still_do_not_retry(self):
        errors = (ssl.SSLCertVerificationError(1, 'certificate verify failed'),
                  ssl.SSLError(1, 'wrong version number'))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.assertFalse(is_retryable(error))
                failure = network_failure(RequestContext('https://media.example/video'), error)
                self.assertEqual(failure.code, 'TLS_ERROR')
                self.assertFalse(is_retryable(failure))

    def test_auth_drm_disk_cancel_tls_and_bad_ranges_never_blindly_retry(self):
        for code in ('AUTH_REQUIRED', 'DRM_PROTECTED', 'PROTECTED_MEDIA', 'RESUME_MISMATCH',
                     'INCOMPLETE_MEDIA_RESPONSE', 'DISK_FULL', 'TLS_ERROR', 'EXPIRED_URL'):
            self.assertFalse(is_retryable(MediaCheckError('failure', code, {'http_status': 503})), code)
        for error in (MediaCheckCancelled('cancel'), PermissionError('denied'),
                      OSError(errno.ENOSPC, 'full'), ssl.SSLError('certificate')):
            self.assertFalse(is_retryable(error), type(error).__name__)
        self.assertFalse(is_retryable(self.error(503, requires_context_refresh=True)))

    def test_signed_403_404_context_refresh_uses_evidence(self):
        plain = RequestContext('https://media.example/video')
        signed = RequestContext('https://media.example/video?token=signed')
        expired = RequestContext('https://media.example/video?expires=1700000000')
        for status in (403, 404, 410):
            self.assertFalse(http_failure(plain, status).diagnostic['requires_context_refresh'])
            self.assertTrue(http_failure(signed, status).diagnostic['requires_context_refresh'])
            self.assertEqual(http_failure(expired, status).code, 'EXPIRED_URL')
            self.assertFalse(is_retryable(http_failure(signed, status)))
        self.assertTrue(is_retryable(http_failure(expired, 503)))

    def test_bounded_exponential_backoff_and_invalid_retry_after(self):
        self.assertEqual([retry_delay(self.error(503), value) for value in (1, 2, 3)], [2, 4, 8])
        self.assertEqual(retry_delay(self.error(503), 100), 60)
        for value in ('-1', '1.5', 'invalid', 'inf', 'NaN', '9999999999999999999999999999999999999999999999999999999999999999999' * 10):
            self.assertEqual(retry_delay(self.error(503, retry_after=value), 2), 4)

    def test_retry_after_seconds_and_http_date_are_respected(self):
        self.assertEqual(retry_delay(self.error(429, retry_after='12'), 1), 12)
        self.assertEqual(retry_delay(MediaCheckError('busy', 'HTTP_503', {'retry_after': '9'}), 1), 9)
        self.assertEqual(retry_delay(self.error(503, response_headers={'Retry-After': '0'}), 3), 0)
        with patch('core.retry_policy.time.time', return_value=1_800_000_000):
            date = formatdate(1_800_000_015, usegmt=True)
            self.assertEqual(retry_delay(self.error(503, retry_after=date), 1), 15)
        self.assertEqual(retry_delay(self.error(500, retry_after='12'), 1), 2)

    def test_countdown_is_cancelled_immediately_and_finishes_at_zero(self):
        event = threading.Event()
        ticks = []
        self.assertTrue(wait_for_retry(event, 0.02, ticks.append))
        self.assertEqual(ticks[-1], 0)
        event.set()
        self.assertFalse(wait_for_retry(event, 10))
        event.clear()
        thread = threading.Timer(0.02, event.set)
        thread.start()
        started = time.monotonic()
        self.assertFalse(wait_for_retry(event, 5))
        self.assertLess(time.monotonic() - started, 0.5)
        thread.join()


class RecoveryHttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        cls.payload = b'0123456789abcdef'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                cls.received.append((self.path, self.headers.get('Range')))
                if self.path == '/temporary':
                    self.send_response(503)
                    self.send_header('Retry-After', '7')
                    self.end_headers()
                    return
                if self.path == '/complete':
                    self.send_response(416)
                    self.send_header('Content-Range', 'bytes */16')
                    self.send_header('ETag', '"asset-v1"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/ignore-range':
                    self.send_response(200)
                    body = cls.payload
                else:
                    self.send_response(206)
                    self.send_header('Content-Range', 'bytes 8-15/16')
                    body = cls.payload[8:]
                self.send_header('Content-Length', str(len(body)))
                self.send_header('ETag', '"asset-v1"')
                self.end_headers()
                self.wfile.write(body)

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

    def test_real_206_recovers_exact_tail_and_200_requires_truncate_restart(self):
        expected = {'ETag': '"asset-v1"', 'Content-Length': '16'}
        for route, mode, body in (('/range', 'resume', self.payload[8:]),
                                  ('/ignore-range', 'restart', self.payload)):
            context = RequestContext(self.base + route, headers={'Range': 'bytes=8-'})
            with ContextHttpClient().open(context) as response:
                check = validate_resume_response(response.status, response.response_headers, 'bytes=8-', expected)
                self.assertEqual(check['mode'], mode)
                self.assertEqual(response.read(), body)

    def test_real_416_retains_content_range_for_complete_offset_validation(self):
        context = RequestContext(self.base + '/complete', headers={'Range': 'bytes=16-'})
        with self.assertRaises(MediaCheckError) as caught:
            ContextHttpClient().open(context)
        error = caught.exception
        self.assertEqual(error.diagnostic['http_status'], 416)
        check = validate_resume_response(416, error.diagnostic['response_headers'], 'bytes=16-',
                                         {'ETag': '"asset-v1"', 'Content-Length': '16'})
        self.assertEqual(check['mode'], 'complete')

    def test_retry_after_is_preserved_without_hidden_http_retry(self):
        before = len([item for item in self.received if item[0] == '/temporary'])
        with self.assertRaises(MediaCheckError) as caught:
            ContextHttpClient().open(RequestContext(self.base + '/temporary'))
        error = caught.exception
        self.assertEqual(error.diagnostic['retry_after'], '7')
        self.assertEqual(error.diagnostic['response_headers']['retry-after'], '7')
        self.assertTrue(is_retryable(error))
        self.assertEqual(retry_delay(error, 1), 7)
        after = len([item for item in self.received if item[0] == '/temporary'])
        self.assertEqual(after - before, 1)


if __name__ == '__main__':
    unittest.main()
