import base64
from dataclasses import replace
import json
import unittest
import zlib

from core.discovery_models import NetworkObservation
from core.errors import MediaCheckError
from core.media_discovery import MediaDiscoveryService
from core.request_context import RequestContext
from core.resolver_models import PageContext, ResolveResult
from core.site_resolvers import ResolverRegistry, RouVideoResolver, SiteResolver
from tests.test_rou_playlist_decoder import png, MEDIA, MASTER

PAGE = 'https://rou.video/v/current-video'
ENTRY = 'https://cdn.test/current/index.png?token=a%2Fb&t=1&t=2'
AES = b'#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-TARGETDURATION:2\n#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x00000000000000000000000000000000\n#EXTINF:2,\nabc000.png\n#EXTINF:2,\nabc001.png\n#EXT-X-ENDLIST\n'


def ev(url=ENTRY, k=7):
    value = json.dumps({'videoUrl': url}, ensure_ascii=True)
    encoded = ''.join(chr(ord(char) + k) for char in value).encode('utf-8')
    return {'d': base64.b64encode(encoded).decode(), 'k': k}


def page(data=None, observations=(), **snapshot):
    # 旧版 NEXT 路线仅在当前 API 已明确失败后测试，不能遮盖主流程。
    failure = observed(None, 'https://rou.video/api/hls/current-video', status=404)
    return PageContext(PAGE, data or {'props': {'pageProps': {'ev': ev()}}}, (failure, *observations), snapshot)


def observed(data, url=ENTRY, **kwargs):
    context = RequestContext(url, headers={'Referer': PAGE, 'User-Agent': 'Actual browser', 'Range': 'bytes=0-'}, inherit_range=True)
    fields = dict(request_id='wire', context=context, status=200, content_type='image/png', body=data,
                  body_complete=True, context_complete=True, timestamp=1234)
    fields.update(kwargs)
    return NetworkObservation(**fields)


class RouResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = RouVideoResolver()

    def test_legacy_ev_after_api_failure_and_context_is_not_fabricated(self):
        candidate = self.resolver.resolve(page(observations=[observed(png(MEDIA))])).candidates[0]
        self.assertEqual(candidate.media_type, 'HLS')
        self.assertEqual(candidate.request_context.user_agent, 'Actual browser')
        self.assertFalse(candidate.request_context.cookies)
        self.assertFalse(candidate.request_context.origin)
        self.assertNotIn('authorization', candidate.request_context.headers)
        self.assertEqual(candidate.request_context.headers['range'], 'bytes=0-')
        self.assertEqual(candidate.metadata['video_id'], 'current-video')

    def test_no_next_api_is_first_fallback(self):
        result = self.resolver.resolve(PageContext(PAGE))
        self.assertEqual([request.url for request in result.requests], ['https://rou.video/api/hls/current-video'])
        self.assertFalse(result.candidates)

    def test_current_api_json_media_entry_only(self):
        api = observed(json.dumps({'data': {'videoUrl': ENTRY}}).encode(), 'https://rou.video/api/hls/current-video', content_type='application/json')
        result = self.resolver.resolve(PageContext(PAGE, observations=(api,)))
        self.assertEqual(result.requests[0].url, ENTRY)

    def test_plain_ro_ud(self):
        result = self.resolver.resolve(page(observations=[observed(png(MEDIA))]))
        self.assertTrue(result.success)
        self.assertEqual(result.candidates[0].drm, False)

    def test_deflate_ro_ud(self):
        self.assertTrue(self.resolver.resolve(page(observations=[observed(png(zlib.compress(MEDIA), 1))])).success)

    def test_png_aes_segments_semantics(self):
        candidate = self.resolver.resolve(page(observations=[observed(png(AES))])).candidates[0]
        self.assertEqual((candidate.classification, candidate.drm, candidate.playlist_type), ('HLS_AES128', False, 'VOD'))
        resources = candidate.metadata['manifest']['resources']
        self.assertTrue(all(item['role'] == 'Media Segment' for item in resources))
        self.assertEqual(candidate.metadata['manifest']['keys'][0]['uri'], 'enc.key')

    def test_scoped_query_inheritance_and_resource_query_wins(self):
        candidate = self.resolver.resolve(page(observations=[observed(png(AES))])).candidates[0]
        self.assertEqual(candidate.metadata['manifest']['resources'][0]['resolved_uri'],
                         'https://cdn.test/current/abc000.png?token=a%2Fb&t=1&t=2')
        self.assertEqual(candidate.request_context.derive('abc001.png?own=1').url,
                         'https://cdn.test/current/abc001.png?own=1')
        self.assertFalse(RequestContext(ENTRY).derive('abc001.png').query_parameters)

    def test_resolver_failure_keeps_generic_fallback_available(self):
        result = ResolverRegistry().resolve(PageContext('https://unknown.test/watch'))
        self.assertFalse(result.success)
        service = MediaDiscoveryService()
        service.consume_network(observed(MEDIA, 'https://other-cdn.test/no-ext', content_type='text/plain'))
        self.assertEqual(service.ranked(include_low=True)[0].media_type, 'HLS')

    def test_ordinary_png_not_media_candidate(self):
        result = self.resolver.resolve(page(observations=[observed(png())]))
        self.assertFalse(result.success)
        self.assertFalse(result.candidates)
        self.assertIn('ROU_CHUNK_NOT_FOUND', [item['error_code'] for item in result.diagnostics])

    def test_homepage_is_not_video_page(self):
        self.assertEqual(self.resolver.resolve(PageContext('https://rou.video')).diagnostics[0]['error_code'], 'ROU_VIDEO_ID_NOT_FOUND')

    def test_exact_site_host_matching(self):
        for url in ('https://rou.video/v/id', 'https://www.rou.video/v/id'):
            self.assertTrue(self.resolver.can_handle(PageContext(url)))
        for url in ('https://rou.video.evil.test/v/id', 'https://notrou.video/v/id', 'javascript:x'):
            self.assertFalse(self.resolver.can_handle(PageContext(url)))

    def test_video_id_uses_path_without_guessing_length_or_prefix(self):
        for path in ('/', '/search?id=x', '/v/', '/v/a/b'):
            self.assertIsNone(self.resolver.extract_video_id('https://rou.video' + path))
        for identifier in ('x'*129, 'arbitrary.prefix', '%E6%B5%8B%E8%AF%95', '7'):
            self.assertEqual(self.resolver.extract_video_id('https://rou.video/v/' + identifier), identifier)

    def test_both_page_props_paths_and_script_input(self):
        for data in ({'pageProps': {'ev': ev()}}, {'props': {'pageProps': {'ev': ev()}}},
                     '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'pageProps': {'ev': ev()}}) + '</script>'):
            self.assertEqual(self.resolver.resolve(page(data)).requests[0].url, ENTRY)

    def test_ev_malformed_does_not_crash_and_falls_back(self):
        for bad in ({}, {'d': '!', 'k': 3}, {'d': 'a', 'k': True}, {'d': 'a', 'k': 1.2}, {'d': 'a', 'k': float('inf')}):
            with self.assertRaises(MediaCheckError):
                self.resolver.decode_ev(bad)
        self.assertEqual(self.resolver.decode_ev(dict(ev(), k=7.0))['videoUrl'], ENTRY)
        result = self.resolver.resolve(page({'pageProps': {'ev': {}}}))
        self.assertFalse(result.success)
        self.assertFalse(result.requests)

    def test_thumbnails_api_not_positive_media(self):
        thumb = observed(MEDIA, 'https://rou.video/api/hls/current-video?kind=thumbs')
        result = self.resolver.resolve(PageContext(PAGE, observations=(thumb,)))
        self.assertFalse(result.success)
        self.assertEqual(result.requests[0].url, 'https://rou.video/api/hls/current-video')

    def test_legacy_never_guesses_adjacent_filenames(self):
        urls = self.resolver._siblings(ENTRY)
        self.assertEqual(urls, [ENTRY])
        self.assertTrue(all(url.endswith('?token=a%2Fb&t=1&t=2') for url in urls))
        result = self.resolver.resolve(page(observations=[observed(MEDIA, urls[0])]))
        self.assertTrue(result.success)
        self.assertEqual(result.requests, [])

    def test_current_api_precedes_next_data(self):
        result = self.resolver.resolve(PageContext(PAGE, {'pageProps': {'ev': ev()}}))
        self.assertEqual([r.url for r in result.requests], ['https://rou.video/api/hls/current-video'])

    def test_bodyless_image_can_be_fetched_once(self):
        obs = observed(None, body_complete=False)
        self.assertEqual(self.resolver.resolve(page(observations=[obs])).requests[0].url, ENTRY)
        next_result = self.resolver.resolve(page(observations=[obs], resolver_attempted_urls=[ENTRY]))
        self.assertFalse(next_result.requests)
        self.assertFalse(self.resolver.resolve(page(observations=[obs], resolver_inflight_url=ENTRY)).requests)

    def test_403_not_drm_and_missing_cookie_not_auth_evidence(self):
        result = self.resolver.resolve(page(observations=[observed(None, status=403)]))
        self.assertFalse(result.success)
        self.assertIn('HTTP_403', [item['error_code'] for item in result.diagnostics])
        self.assertNotIn('AUTH_REQUIRED', [item['error_code'] for item in result.diagnostics])

    def test_real_drm_is_detected_but_not_downloadable(self):
        drm = MEDIA.replace(b'#EXTINF:2,', b'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://content",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:2,')
        candidate = self.resolver.resolve(page(observations=[observed(png(drm))])).candidates[0]
        self.assertTrue(candidate.drm)
        self.assertFalse(candidate.downloadable)

    def test_partial_wire_context_or_body_does_not_confirm(self):
        for obs in (observed(MEDIA, context_complete=False), observed(MEDIA, body_complete=False)):
            self.assertFalse(self.resolver.resolve(page(observations=[obs])).success)

    def test_raw_secrets_are_not_in_diagnostic(self):
        obs = observed(png(MEDIA))
        obs.context = replace(obs.context, headers={'Cookie': 'session=PRIVATE', 'Authorization': 'Bearer SECRET'})
        candidate = self.resolver.resolve(page(observations=[obs])).candidates[0]
        display = json.dumps(candidate.safe_diagnostic())
        self.assertNotIn('PRIVATE', display)
        self.assertNotIn('SECRET', display)
        self.assertNotIn('token=a%2Fb', display)

    def test_unrelated_iframe_hls_does_not_claim_current_video_identity(self):
        obs = observed(MEDIA, 'https://advert.test/index.m3u8', frame_id='unrelated-iframe')
        result = self.resolver.resolve(PageContext(PAGE, observations=(obs,)))
        self.assertFalse(result.success)
        self.assertFalse(result.candidates)
        service = MediaDiscoveryService()
        service.consume_network(obs)
        self.assertTrue(service.candidates)
        self.assertTrue(all(not item.metadata.get('resolver_id') for item in service.candidates.values()))

    def test_master_ro_ud_candidate_uses_playlist_instead_of_blob(self):
        result = self.resolver.resolve(page(observations=[observed(png(MASTER))],
                                           videos=[{'src': 'blob:' + PAGE}]))
        candidate = result.candidates[0]
        self.assertEqual(candidate.metadata['manifest']['playlist_type'], 'Master')
        self.assertEqual(candidate.request_context.url, ENTRY)
        self.assertFalse(candidate.url.startswith('blob:'))
