"""Real loopback boundary tests; no browser profile or remote media is used."""
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import urlencode, urlunsplit

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from core.request_context import RequestContext
from core.discovery_models import MediaCandidate
from tests.batch_import_acceptance import _LocalBatchPages
from tests.douyin_compare import Evidence, digest
from tests.recovery_acceptance import _FixtureServer
from tests.rou_current_fixture import CurrentRouFixture


def response(server, path, *, method='GET', headers=None):
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        received = connection.getresponse()
        return received.status, dict(received.getheaders()), received.read()
    finally:
        connection.close()


class RegisteredFixturePathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mediaflow-fixture-boundary-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / 'fixtures'
        self.directory.mkdir()
        (self.root / 'outside.txt').write_bytes(b'owned negative fixture, never served')

    def write(self, name, body):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_batch_routes_choose_registered_direct_hls_dash_files(self):
        self.write('direct.mp4', b'direct-fixture-bytes')
        self.write('source.m3u8', b'#EXTM3U\n')
        self.write('part000.ts', b'registered-ts-bytes')
        self.write('dash/source.mpd', b'<MPD/>')
        self.write('dash/chunk-stream0-00001.m4s', b'registered-dash-bytes')
        server = _LocalBatchPages(self.directory)
        try:
            for route, body in (
                    ('/media/direct/direct.mp4', b'direct-fixture-bytes'),
                    ('/media/hls/part000.ts', b'registered-ts-bytes'),
                    ('/media/dash/chunk-stream0-00001.m4s', b'registered-dash-bytes')):
                with self.subTest(route=route):
                    status, _, actual = response(server.server, route)
                    self.assertEqual((status, actual), (200, body))
            # A later local file is not a newly registered HTTP resource.
            self.write('part999.ts', b'unregistered-fixture')
            for route in ('/media/hls/../outside.txt', '/media/hls/%2e%2e%2foutside.txt',
                          '/media/hls/..\\outside.txt', '/media/hls/part999.ts',
                          '/media/unknown/direct.mp4'):
                with self.subTest(route=route):
                    status, _, actual = response(server.server, route)
                    self.assertEqual(status, 404)
                    self.assertNotIn(b'never served', actual)
        finally:
            server.close()

    def test_recovery_hls_routes_never_construct_paths_from_request_names(self):
        direct = self.write('direct.mp4', b'direct-fixture-bytes')
        self.write('hls/index.m3u8', b'#EXTM3U\n')
        self.write('hls/segment000.ts', b'registered-segment')
        assets = {'direct': direct, 'changed': direct, 'hls': self.directory / 'hls'}
        with _FixtureServer(assets) as server:
            status, _, body = response(server.server, '/hls-missing/segment000.ts')
            self.assertEqual((status, body), (200, b'registered-segment'))
            status, _, body = response(server.server, '/hls-missing/index.m3u8')
            self.assertEqual((status, body), (200, b'#EXTM3U\n'))
            self.write('hls/segment999.ts', b'unregistered-fixture')
            for route in ('/hls-missing/../outside.txt', '/hls-missing/%2e%2e%2foutside.txt',
                          '/hls-missing/..\\outside.txt', '/hls-missing/segment999.ts'):
                with self.subTest(route=route):
                    status, _, body = response(server.server, route)
                    self.assertEqual(status, 404)
                    self.assertNotIn(b'never served', body)


class FixtureOriginTests(unittest.TestCase):
    def test_cors_allows_only_canonical_actual_fixture_origins(self):
        with tempfile.TemporaryDirectory(prefix='mediaflow-cors-boundary-unit-') as directory:
            fixture = CurrentRouFixture(directory).start()
            try:
                origin = fixture.base
                status, headers, _ = response(fixture.servers[0], '/', method='OPTIONS', headers={'Origin': origin})
                self.assertEqual(status, 204)
                self.assertEqual(headers.get('Access-Control-Allow-Origin'), origin)
                self.assertEqual(headers.get('Access-Control-Allow-Credentials'), 'true')
                for untrusted in ('null', 'https://other.invalid', origin + '.invalid'):
                    with self.subTest(origin=untrusted):
                        _, headers, _ = response(fixture.servers[0], '/', method='OPTIONS', headers={'Origin': untrusted})
                        self.assertNotIn('Access-Control-Allow-Origin', headers)
                        self.assertNotIn('Access-Control-Allow-Credentials', headers)
            finally:
                fixture.close()

    def test_folded_origin_is_not_reflected_into_response_headers(self):
        with tempfile.TemporaryDirectory(prefix='mediaflow-cors-folded-unit-') as directory:
            fixture = CurrentRouFixture(directory).start()
            try:
                request = ('OPTIONS / HTTP/1.0\r\nHost: localhost\r\nOrigin: ' + fixture.base +
                           '\r\n X-Injected: unit-only\r\n\r\n').encode('ascii')
                with socket.create_connection(('127.0.0.1', fixture.servers[0].server_port), timeout=3) as channel:
                    channel.sendall(request)
                    received = bytearray()
                    while True:
                        block = channel.recv(4096)
                        if not block:
                            break
                        received.extend(block)
                headers = bytes(received).split(b'\r\n\r\n', 1)[0]
                self.assertNotIn(b'Access-Control-Allow-Origin:', headers)
                self.assertNotIn(b'X-Injected:', headers)
            finally:
                fixture.close()


class DiagnosticMediaIdentityTests(unittest.TestCase):
    def test_only_media_identifiers_enter_legacy_diagnostic_hash_field(self):
        evidence = Evidence('unit-work')
        sensitive = 'unit-' + 'x' * 24
        signed_url = urlunsplit(('https', 'private.invalid', '/asset', urlencode({'token': sensitive}), ''))
        media_ids = {'unit-video-identifier', 'unit-file-identifier'}
        video = {'vid': 'unit-video-identifier', 'file_id': 'unit-file-identifier',
                 'uri': signed_url, 'password': sensitive, 'access_token': sensitive}
        payload = json.dumps({'aweme_id': 'unit-work', 'video': video}).encode()
        context = RequestContext('https://unit.invalid/api')
        observation = SimpleNamespace(response_headers={}, request_id='unit-request', context=context,
            resource_type='XHR', status=200, content_type='application/json', content_length=len(payload),
            body=payload, body_complete=True, context_complete=True, frame_id='unit-frame')
        evidence.observation(observation)
        self.assertEqual(evidence.api_media_ids, media_ids)
        evidence.candidates([MediaCandidate('unit-media', RequestContext('https://cdn.invalid/unit-video-identifier.mp4'))])
        self.assertTrue(evidence.last_candidates[0]['api_identity_match'])
        dialog = SimpleNamespace(page=SimpleNamespace(url=lambda: SimpleNamespace(toString=lambda: 'https://unit.invalid/page')),
            page_url=SimpleNamespace(text=lambda: 'https://unit.invalid/page'),
            observer=SimpleNamespace(network_diagnostics=lambda: {}), discovery_counts=lambda: {},
            selected_candidate=lambda: None)
        report = evidence.report(dialog)
        self.assertEqual(report['api_token_hashes'], sorted(digest(value) for value in media_ids))
        self.assertNotIn(sensitive, json.dumps(report))
        self.assertNotIn(digest(sensitive), report['api_token_hashes'])
        self.assertNotIn(digest(signed_url), report['api_token_hashes'])
