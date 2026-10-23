"""当前 Rou API/PNG 协议的确定性回归；所有凭据和地址都是合成夹具。"""
from dataclasses import replace
import json
import time
import unittest
import zlib

from core.discovery_models import NetworkObservation
from core.errors import MediaCheckError
from core.manifests import parse_manifest
from core.request_context import RequestContext
from core.resolver_models import PageContext
from core.site_resolvers import ResolverRegistry, RouVideoResolver
from core.url_policy import UrlResolutionPolicy
from tests.test_rou_playlist_decoder import MEDIA, MASTER, png


PAGE = 'https://rou.video/v/cm5nlwtpm0000jpseusy9ni66'
API = 'https://rou.video/api/hls/cm5nlwtpm0000jpseusy9ni66'
INDEX = 'https://cdn.example/randomDir/index.png?v=6&exp=4102444800&auth=A%2FB%2BC&v=7'


def observation(body=None, *, location=INDEX, status=200, api_status=302, headers=None,
                complete=True, context_complete=True, final_url=None):
    final_url = final_url or location
    chain = ({'from': API, 'to': location, 'status': api_status, 'location': location},)
    context = RequestContext(final_url, headers=headers or {'Referer': PAGE, 'User-Agent': 'Fixture Browser'},
                             original_url=API, final_url=final_url, redirect_chain=chain)
    return NetworkObservation('current-api', context, status, 'image/png', body,
                              timestamp=time.time(), redirects=list(chain), body_complete=complete,
                              context_complete=context_complete)


def context(*observations, **snapshot):
    return PageContext(PAGE, observations=tuple(observations), dom_snapshot={'page_title': 'Fixture title', **snapshot})


def playlist(count, duration, style):
    seconds = [round(duration / count, 3)] * count
    seconds[-1] = round(duration - sum(seconds[:-1]), 3)
    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:20', '#EXT-X-PLAYLIST-TYPE:VOD']
    for index, value in enumerate(seconds):
        name = {'a': f'CLS-{index:03}.png', 'b': f'C-{index:03}.png', 'c': f'CLS-{index+1}-v1-a1.png'}[style]
        lines.extend((f'#EXTINF:{value:.3f},', f'https://v{index%3+1}.cdn.example/random/{name}?auth=actual-{index}&exp=4102444800'))
    return ('\n'.join([*lines, '#EXT-X-ENDLIST', ''])).encode()


class CurrentRouProtocolTests(unittest.TestCase):
    def setUp(self):
        self.resolver = RouVideoResolver(legacy_fallback=False)

    def codes(self, result):
        return [item.get('error_code') for item in result.diagnostics]

    def candidate(self, body=MEDIA, **kwargs):
        result = self.resolver.resolve(context(observation(png(zlib.compress(body), 1), **kwargs)))
        self.assertTrue(result.success, result.diagnostics)
        return result.candidates[0]

    def test_video_id_does_not_depend_on_prefix_or_length(self):
        for identifier in ('cm5nlwtpm0000jpseusy9ni66', '7', 'arbitrary-name', 'a'*200, 'other.id'):
            self.assertEqual(self.resolver.extract_video_id('https://rou.video/v/' + identifier), identifier)

    def test_invalid_page_and_missing_id_have_separate_codes(self):
        self.assertIn('ROU_INVALID_PAGE_URL', self.codes(self.resolver.resolve(PageContext('https://example.test/v/x'))))
        self.assertIn('ROU_VIDEO_ID_NOT_FOUND', self.codes(self.resolver.resolve(PageContext('https://rou.video/'))))

    def test_api_is_first_request_even_with_old_next_data(self):
        result = self.resolver.resolve(PageContext(PAGE, {'pageProps': {'videoUrl': 'https://old.example/media'}}))
        self.assertEqual([r.url for r in result.requests], [API])
        self.assertEqual(result.requests[0].metadata['role'], 'rou-api')
        self.assertFalse(result.requests[0].headers)

    def test_identical_cache_replay_cannot_mask_actual_complete_browser_context(self):
        actual = observation(png(MEDIA))
        cached = replace(actual, request_id='cache-replay', from_cache=True, context_complete=False)
        result = self.resolver.resolve(context(actual, cached))
        self.assertTrue(result.success)
        self.assertEqual(result.candidates[0].request_context.headers, actual.context.headers)
        self.assertEqual(result.candidates[0].request_context.original_url, actual.context.original_url)
        self.assertEqual(result.candidates[0].request_context.final_url, actual.context.final_url)

    def test_changed_cache_payload_cannot_borrow_context_from_other_response(self):
        actual = observation(png(MEDIA))
        cached = replace(actual, request_id='cache-replay', from_cache=True, context_complete=False, body=png(MASTER))
        self.assertFalse(self.resolver.resolve(context(actual, cached)).success)

    def test_later_real_403_is_not_replaced_by_earlier_success(self):
        actual = observation(png(MEDIA))
        rejected = replace(actual, request_id='rejected', status=403)
        result = self.resolver.resolve(context(actual, rejected))
        self.assertFalse(result.success)
        self.assertIn('HTTP_403', self.codes(result))

    def test_thumbnails_have_explicit_semantics_and_no_candidate(self):
        thumb = replace(observation(png(MEDIA)), context=RequestContext(API + '?kind=thumbs'), redirects=[])
        result = self.resolver.resolve(context(thumb))
        self.assertFalse(result.candidates)
        self.assertEqual([r.url for r in result.requests], [API])
        self.assertIn('THUMBNAIL_TRACK', self.codes(result))
        self.assertEqual(self.resolver.classify_api_url(API+'?kind=thumbs'), 'THUMBNAIL_TRACK')
        self.assertEqual(self.resolver.classify_api_url(API), 'MAIN_VIDEO_ENTRY')

    def test_thumbnail_redirect_cannot_authorize_cdn_as_main_video(self):
        obs = observation(png(MEDIA))
        chain = [{'from': API+'?kind=thumbs', 'to': INDEX, 'status':302, 'location':INDEX}]
        obs.context = replace(obs.context, original_url=API+'?kind=thumbs', redirect_chain=tuple(chain))
        obs.redirects = chain
        self.assertFalse(self.resolver.resolve(context(obs)).success)

    def test_three_sample_shapes_use_exact_server_location(self):
        samples = [('hidx16.xyz', 'cm5nlwtpm0000jpseusy9ni66-402',106,860.333,'a'),
                   ('fuzb14.xyz', 'cm5nlwtpm0000jpseusy9ni66-720',151,1191.,'b'),
                   ('golb67.xyz', 'elAo4Ubecpd',26,259.493,'c')]
        for host, directory, count, duration, style in samples:
            with self.subTest(sample=style):
                location = f'https://{host}/hls/id/{directory}/index.png?v=6&exp=4102444800&auth=A%2FB'
                candidate = self.candidate(playlist(count,duration,style), location=location)
                self.assertEqual(candidate.url, location)
                self.assertEqual(candidate.original_url, API)
                self.assertEqual(candidate.metadata['location'], location)
                self.assertEqual(candidate.metadata['api_status'],302)
                self.assertEqual(candidate.metadata['segment_count'],count)
                self.assertAlmostEqual(candidate.duration,duration,places=3)
                self.assertEqual((candidate.media_type,candidate.manifest_type,candidate.playlist_type),('HLS','Media','VOD'))
                self.assertEqual((candidate.classification,candidate.drm),('CLEAR_HLS',False))

    def test_query_order_duplicates_and_encoding_are_preserved(self):
        candidate = self.candidate()
        self.assertEqual(candidate.url, INDEX)
        self.assertEqual(candidate.final_url, INDEX)
        self.assertEqual(candidate.request_context.redirect_chain[0]['location'], INDEX)
        self.assertEqual(candidate.metadata['manifest_url'],INDEX)
        self.assertEqual(candidate.metadata['original_api_url'],API)
        self.assertEqual(candidate.metadata['exp'],4102444800)
        self.assertTrue(candidate.metadata['possible_signed_url'])

    def test_relative_segments_use_standard_urljoin_without_copying_signed_query(self):
        candidate = self.candidate()
        self.assertEqual(candidate.request_context.url_resolution_policy,UrlResolutionPolicy.STANDARD_URLJOIN)
        self.assertEqual(candidate.request_context.derive('seg.png').url,'https://cdn.example/randomDir/seg.png')

    def test_absolute_multicdn_segments_stay_exact(self):
        body = playlist(26,259.493,'c')
        candidate = self.candidate(body)
        expected = [line for line in body.decode().splitlines() if line.startswith('https:')]
        resources = candidate.metadata['manifest']['resources']
        self.assertEqual([r['resolved_uri'] for r in resources],expected)
        self.assertTrue(all(r['role']=='Media Segment' for r in resources))
        self.assertEqual([candidate.request_context.derive(r['uri']).url for r in resources],expected)

    def test_arbitrary_segment_names_are_hls_semantics(self):
        for name in ('CLS-000.png','C-000.png','CLS-1-v1-a1.png','image.jpg','opaque','a.bin','x.dat'):
            body = MEDIA.replace(b'seg.png',name.encode())
            candidate=self.candidate(body)
            self.assertEqual(candidate.metadata['manifest']['resources'][0]['role'],'Media Segment')

    def test_actual_context_keeps_only_headers_present(self):
        candidate=self.candidate(headers={'Referer':PAGE,'User-Agent':'Actual UA','Range':'bytes=0-'})
        self.assertEqual(candidate.request_context.referer,PAGE)
        self.assertEqual(candidate.request_context.user_agent,'Actual UA')
        self.assertFalse(candidate.request_context.origin)
        self.assertFalse(candidate.request_context.cookies)
        self.assertNotIn('authorization',candidate.request_context.headers)

    def test_credentials_and_signed_location_are_redacted_in_candidate_diagnostic(self):
        candidate=self.candidate(headers={'Cookie':'session=PRIVATE','Authorization':'Bearer SECRET','Referer':PAGE})
        display=json.dumps(candidate.safe_diagnostic())
        for secret in ('PRIVATE','SECRET','auth=A%2FB%2BC','auth=A/B+C'):
            self.assertNotIn(secret,display)
        self.assertTrue(candidate.request_context.cookies)

    def test_verified_result_has_confidence_100_and_trace(self):
        candidate=self.candidate()
        self.assertEqual(candidate.confidence,100)
        self.assertTrue(candidate.metadata['resolver_verified'])
        trace=candidate.metadata['resolver_trace']
        self.assertEqual((trace['api_status'],trace['index_http']),(302,200))
        self.assertEqual((trace['container'],trace['roud'],trace['compression'],trace['playlist']),('PNG','FOUND','zlib','VALID'))
        self.assertEqual(trace['flag'],'0x01')

    def test_plain_hls_location_also_uses_standard_pipeline(self):
        result=self.resolver.resolve(context(observation(MEDIA)))
        self.assertTrue(result.success)
        self.assertEqual(result.candidates[0].metadata['resolver_trace']['container'],'HLS')

    def test_master_manifest_remains_master(self):
        candidate=self.candidate(MASTER)
        self.assertEqual(candidate.manifest_type,'Master')
        self.assertEqual(candidate.metadata['manifest']['children'],['https://cdn.example/randomDir/low/index.m3u8'])

    def test_aes128_is_not_drm(self):
        body=MEDIA.replace(b'#EXTINF:',b'#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x00000000000000000000000000000000\n#EXTINF:')
        candidate=self.candidate(body)
        self.assertEqual((candidate.classification,candidate.drm),('HLS_AES128',False))
        self.assertTrue(candidate.downloadable)
        self.assertEqual(candidate.metadata['manifest']['keys'][0]['uri'],'enc.key')

    def test_real_drm_remains_blocked(self):
        for keyformat, expected in (('com.widevine.alpha','DRM_WIDEVINE'),('com.microsoft.playready','DRM_PLAYREADY'),
                                    ('com.apple.streamingkeydelivery','DRM_FAIRPLAY')):
            body=MEDIA.replace(b'#EXTINF:',f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="https://license.example/key",KEYFORMAT="{keyformat}"\n#EXTINF:'.encode())
            candidate=self.candidate(body)
            self.assertEqual(candidate.classification,expected)
            self.assertTrue(candidate.drm)
            self.assertFalse(candidate.downloadable)

    def test_403_api_is_not_drm(self):
        obs=NetworkObservation('api',RequestContext(API),403)
        result=self.resolver.resolve(context(obs))
        self.assertIn('ROU_API_REQUEST_FAILED',self.codes(result))
        self.assertIn('HTTP_403',self.codes(result))
        self.assertNotIn('PROTECTED_MEDIA',self.codes(result))

    def test_401_api_reports_auth_requirement(self):
        result=self.resolver.resolve(context(NetworkObservation('api',RequestContext(API),401)))
        self.assertIn('AUTH_REQUIRED',self.codes(result))

    def test_missing_redirect_does_not_guess_cdn(self):
        result=self.resolver.resolve(context(NetworkObservation('api',RequestContext(API),200,'text/html',b'html')))
        self.assertFalse(result.requests)
        self.assertIn('ROU_API_REDIRECT_MISSING',self.codes(result))

    def test_redirect_header_only_plans_exact_index(self):
        obs=NetworkObservation('api',RequestContext(API),302,response_headers={'location':INDEX})
        result=self.resolver.resolve(context(obs))
        self.assertEqual([r.url for r in result.requests],[INDEX])
        self.assertEqual(result.requests[0].metadata['role'],'rou-index')

    def test_invalid_locations_have_specific_error(self):
        for location in ('file:///C:/x','javascript:alert(1)','https://user:password@cdn.example/x','http://cdn.example/x','https://cdn.example/x\nY'):
            result=self.resolver.resolve(context(NetworkObservation('api',RequestContext(API),302,response_headers={'location':location})))
            self.assertIn('ROU_API_INVALID_LOCATION',self.codes(result))
            self.assertFalse(result.requests)

    def test_incomplete_index_is_not_validated(self):
        for obs in (observation(png(MEDIA),complete=False),observation(png(MEDIA),context_complete=False)):
            self.assertFalse(self.resolver.resolve(context(obs)).success)

    def test_unrelated_ad_cannot_claim_api_identity(self):
        obs=NetworkObservation('ad',RequestContext('https://ad.example/index.png'),200,'image/png',png(MEDIA),body_complete=True)
        result=self.resolver.resolve(context(obs))
        self.assertFalse(result.success)
        self.assertEqual([r.url for r in result.requests],[API])

    def test_expired_index_403_has_rou_expiry_code(self):
        expired=INDEX.replace('4102444800','946684801')
        result=self.resolver.resolve(context(observation(status=403,location=expired)))
        self.assertIn('ROU_SIGNED_URL_EXPIRED',self.codes(result))

    def test_index_403_is_request_error_without_drm(self):
        result=self.resolver.resolve(context(observation(status=403)))
        self.assertIn('ROU_INDEX_REQUEST_FAILED',self.codes(result))
        self.assertIn('HTTP_403',self.codes(result))

    def test_decoder_error_reaches_resolver_without_generic_error_wrapping(self):
        for body,code in ((png(),'ROU_CHUNK_NOT_FOUND'),(png(b'bad zlib',1),'ROU_DEFLATE_FAILED'),
                          (png(zlib.compress(b'not HLS'),1),'ROU_PLAYLIST_INVALID'),(b'image data','ROU_NOT_PNG_OR_HLS')):
            result=self.resolver.resolve(context(observation(body)))
            self.assertIn(code,self.codes(result))
            self.assertFalse(result.success)

    def test_no_response_after_attempt_does_not_probe_neighbors(self):
        result=self.resolver.resolve(context(resolver_attempted_urls=[API]))
        self.assertFalse(result.requests)
        self.assertIn('ROU_API_REQUEST_FAILED',self.codes(result))

    def test_registry_keeps_unknown_site_generic_fallback(self):
        result=ResolverRegistry().resolve(PageContext('https://unknown.example/watch'))
        self.assertFalse(result.success)
        self.assertEqual(result.diagnostics[0]['error_code'],'NO_SITE_RESOLVER')


if __name__ == '__main__':
    unittest.main()
