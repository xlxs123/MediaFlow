"""HTML 内嵌 DASH 与 API JSON 具有同等协议语义。"""
from dataclasses import replace
import unittest

from core.embedded_media import embedded_dash_json
from core.discovery_models import NetworkObservation
from core.media_discovery import MediaDiscoveryService
from core.request_context import RequestContext
from tests.test_dash_discovery import dash_json, INIT
from tests.test_browser_observer import _NetworkStore, request, response


PAGE = 'https://page.example/watch/123'


class EmbeddedMediaTests(unittest.TestCase):
    def html(self, assignment='window.anyPlayer = ', trailing='; otherCode();'):
        return b'<html><script>' + assignment.encode() + dash_json() + trailing.encode() + b'</script></html>'

    def test_arbitrary_assignment_json_not_site_name(self):
        values = embedded_dash_json(self.html(), PAGE)
        self.assertEqual(len(values), 1)
        self.assertIn(b'"dash":', values[0])

    def test_const_and_self_assignments(self):
        for assignment in ('const info = ', 'self.__description__ = ', 'var data = '):
            with self.subTest(assignment=assignment):
                self.assertEqual(len(embedded_dash_json(self.html(assignment), PAGE)), 1)

    def test_json_script_element(self):
        html = b'<script type="application/json">' + dash_json() + b'</script>'
        self.assertEqual(len(embedded_dash_json(html, PAGE)), 1)

    def test_javascript_expression_not_evaluated(self):
        html = b'<script>window.info = fetch("private"); var x = {"dash": alert(1)};</script>'
        self.assertEqual(embedded_dash_json(html, PAGE), [])

    def test_external_script_and_regular_page_not_descriptions(self):
        self.assertEqual(embedded_dash_json(b'<script src="x.js">' + dash_json() + b'</script>', PAGE), [])
        self.assertEqual(embedded_dash_json(b'<p>video.mp4</p><script>var x={"dash": "not tracks"}</script>', PAGE), [])

    def test_bounds_and_deduplication(self):
        self.assertEqual(embedded_dash_json(self.html(), PAGE, max_bytes=40), [])
        self.assertEqual(len(embedded_dash_json(self.html() * 2, PAGE)), 1)

    def test_document_body_is_collected_with_standard_limit(self):
        store = _NetworkStore()
        store.consume('Network.requestWillBeSent', {**request(url=PAGE), 'type': 'Document'})
        store.consume('Network.responseReceived', {**response(url=PAGE, mime='text/html'), 'type': 'Document'})
        store.consume('Network.loadingFinished', {'requestId': 'r1', 'encodedDataLength': 100})
        self.assertTrue(store.wants_body(store.get('', 'r1')))
        store.get('', 'r1').hops[-1].content_length = store.max_body_bytes + 1
        self.assertFalse(store.wants_body(store.get('', 'r1')))

    def test_document_creates_pairs_and_late_ranges_do_not_downgrade(self):
        service = MediaDiscoveryService()
        context = RequestContext(PAGE, headers={'user-agent': 'Browser', 'referer': 'https://page.example/old'})
        document = NetworkObservation('html', context, 200, 'text/html', self.html(),
            frame_id='main', resource_type='Document', body_complete=True, context_complete=True)
        service.consume_network(document)
        candidates = service.ranked(include_low=True, limit=128)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(service.discovery_diagnostic()['statistics']['dash_inline_descriptions'], 1)
        for candidate in candidates:
            self.assertTrue(candidate.downloadable)
            self.assertTrue(candidate.video_track and candidate.audio_track)
            self.assertEqual(candidate.manifest_origin, 'embedded:html-dash-json')
            self.assertEqual(candidate.request_context.referer, PAGE)
            self.assertNotIn('origin', candidate.request_context.headers)
            service.consume_network(NetworkObservation('late-' + candidate.id,
                context.derive(candidate.video_url), 206, 'video/mp4', INIT,
                frame_id='main', resource_type='XHR', body_complete=True))
            self.assertTrue(service.get(candidate.id).downloadable)
            self.assertEqual(service.get(candidate.id).media_type, 'DASH')

    def test_incomplete_document_cannot_create_verified_pairs(self):
        service = MediaDiscoveryService()
        observation = NetworkObservation('html', RequestContext(PAGE), 200, 'text/html', self.html(),
            resource_type='Document', body_complete=False)
        service.consume_network(observation)
        self.assertFalse(any(c.downloadable for c in service.ranked(include_low=True, limit=128)))

    def test_drm_in_embedded_descriptor_remains_blocked(self):
        body = dash_json().replace(b'"duration": 97', b'"pssh": "evidence", "duration": 97')
        service = MediaDiscoveryService()
        service.consume_network(NetworkObservation('html', RequestContext(PAGE), 200, 'text/html',
            b'<script>window.player=' + body + b';</script>', resource_type='Document', body_complete=True))
        candidates = service.ranked(include_low=True, limit=128)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(c.drm and not c.downloadable for c in candidates))
