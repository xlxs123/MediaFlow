from copy import deepcopy
from dataclasses import replace
import json
import struct
import unittest

from core.discovery_models import NetworkObservation
from core.media_discovery import MediaDiscoveryService, MAX_CANDIDATES, MAX_OBSERVATIONS, MAX_RESOURCES
from core.request_context import RequestContext


CLEAR_HLS = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:2,\na.ts\n#EXTINF:2,\nb.ts\n#EXT-X-ENDLIST\n'
AES_HLS = b'#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-KEY:METHOD=AES-128,URI="enc.key",\nIV=0x00000000000000000000000000000000\n#EXTINF:2,\nabc000.png\n#EXTINF:2,\nabc001.png\n#EXT-X-ENDLIST\n'
MASTER = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=854x480,CODECS="avc1.4d401e,mp4a.40.2",FRAME-RATE=24\nlow/index.m3u8?token=variant-low\n#EXT-X-STREAM-INF:BANDWIDTH=2300000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2",FRAME-RATE=30\nmid/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=4200000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",FRAME-RATE=60\nhigh/index.m3u8\n'
DASH = b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2M30S"><Period><AdaptationSet mimeType="video/mp4" codecs="avc1.640028"><SegmentTemplate initialization="init_$RepresentationID$.mp4" media="chunk_$RepresentationID$_$Number%05d$.m4s"/><Representation id="low" width="1280" height="720" bandwidth="2000000"/><Representation id="high" width="1920" height="1080" bandwidth="4000000"/></AdaptationSet></Period></MPD>'


def box(kind, payload=b''):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


MP4 = box(b'ftyp', b'isom\0\0\0\0') + box(b'moov') + box(b'mdat', b'video')


class MediaDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.service = MediaDiscoveryService()
        self.next_request = 1

    def network(self, url='https://cdn.example/video.mp4', *, mime='video/mp4', body=None,
                headers=None, frame='main', status=200, context=None, **overrides):
        request_id = str(self.next_request)
        self.next_request += 1
        context = context or RequestContext(url, headers=headers or {})
        observation = NetworkObservation(request_id, context, status, mime, body,
                                         frame_id=frame, **overrides)
        self.service.consume_network(observation)
        return next((candidate for candidate in self.service.candidates.values()
                     if candidate.url == context.url), None)

    def dom(self, videos=None, frame='main', page='https://page.example/watch', **extra):
        snapshot = {'page_url': page, 'frame_id': frame, 'timestamp': 1000,
                    'videos': videos or []}
        snapshot.update(extra)
        self.service.consume_dom(snapshot)

    def video(self, url, **extra):
        value = {'src': url, 'current_src': url, 'width': 1280, 'height': 720,
                 'duration': 600, 'paused': False, 'ended': False, 'visible': True,
                 'area': 921600, 'is_main': True, 'events': []}
        value.update(extra)
        return value

    def test_html_subtitle_belongs_only_to_matched_player(self):
        from core.media_tracks import catalog
        first = self.network('https://cdn.example/first.mp4', body=MP4, body_complete=True)
        second = self.network('https://cdn.example/second.mp4', body=MP4, body_complete=True)
        self.dom([self.video(first.url, subtitle_tracks=[{'src': 'https://cdn.example/first.vtt',
                   'language': 'ja', 'name': '日本語', 'kind': 'subtitles'}]),
                  self.video(second.url, is_main=False, subtitle_tracks=[])])
        self.assertEqual([value['language'] for value in catalog(first)['subtitles']], ['ja'])
        self.assertEqual(catalog(second)['subtitles'], [])

    def test_verified_variant_type_survives_parent_refresh(self):
        from core.manifests import parse_manifest
        parent = self.network('https://cdn.example/master.m3u8', mime='application/vnd.apple.mpegurl', body=MASTER, body_complete=True)
        child = next(c for c in self.service.candidates.values() if c.metadata.get('parent_manifest') == parent.id)
        self.service.apply_probe(child.id, {**parse_manifest(CLEAR_HLS, child.url).to_dict(), 'http_status': 200, 'error_code': ''})
        self.assertEqual(child.manifest_type, 'Media')
        self.network(parent.url, mime='application/vnd.apple.mpegurl', body=MASTER, body_complete=True)
        self.assertEqual(child.manifest_type, 'Media')
        self.assertTrue(child.confirmed)

    def test_master_drm_evidence_propagates_to_previously_verified_variant(self):
        from core.manifests import parse_manifest
        parent = self.network('https://cdn.example/master.m3u8', mime='application/vnd.apple.mpegurl', body=MASTER, body_complete=True)
        child = next(c for c in self.service.candidates.values() if c.metadata.get('parent_manifest') == parent.id)
        self.service.apply_probe(child.id, {**parse_manifest(CLEAR_HLS, child.url).to_dict(), 'http_status': 200, 'error_code': ''})
        protected = MASTER.replace(b'#EXTM3U\n', b'#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="skd://license",KEYFORMAT="com.apple.streamingkeydelivery"\n')
        self.network(parent.url, mime='application/vnd.apple.mpegurl', body=protected, body_complete=True)
        self.assertTrue(child.drm)
        self.assertFalse(child.downloadable)

    def test_scenario01_unverified_206_preserves_range_without_claiming_direct(self):
        candidate = self.network(status=206, headers={'Range': 'bytes=4000-9999',
                                                     'Referer': 'https://page.example/player',
                                                     'User-Agent': 'Actual Browser'})
        self.assertEqual(candidate.media_type, 'Segment')
        self.assertEqual(candidate.http_status, 206)
        self.assertEqual(candidate.request_context.headers['range'], 'bytes=4000-9999')
        self.assertNotIn('origin', candidate.request_context.headers)
        self.assertIsNone(candidate.drm)
        self.assertFalse(candidate.downloadable)

    def test_scenario02_clear_hls_content_wins_opaque_name_and_mime(self):
        candidate = self.network('https://cdn.example/endpoint?sign=abc', mime='application/octet-stream', body=CLEAR_HLS)
        self.assertEqual(candidate.media_type, 'HLS')
        self.assertEqual(candidate.manifest_type, 'Media')
        self.assertEqual(candidate.playlist_type, 'VOD')
        self.assertEqual(candidate.classification, 'CLEAR_HLS')
        self.assertIs(candidate.drm, False)
        self.assertEqual(candidate.duration, 4)
        self.assertTrue(candidate.downloadable)

    def test_scenario03_standard_aes128_is_not_drm(self):
        candidate = self.network('https://cdn.example/index.m3u8', mime='application/x-mpegURL', body=AES_HLS)
        self.assertEqual(candidate.encryption, 'AES-128')
        self.assertEqual(candidate.classification, 'HLS_AES128')
        self.assertIs(candidate.drm, False)
        self.assertEqual(candidate.metadata['manifest']['keys'][0]['uri'], 'enc.key')
        self.assertTrue(candidate.downloadable)

    def test_scenario04_png_segments_are_removed_even_if_arriving_before_manifest(self):
        self.network('https://cdn.example/abc000.png', mime='video/mp2t', body=b'G' + b'\0' * 187 + b'G')
        self.network('https://cdn.example/abc001.png', mime='video/mp4', body=MP4)
        self.assertEqual(len(self.service.candidates), 2)
        manifest = self.network('https://cdn.example/index.m3u8', mime='text/plain', body=AES_HLS)
        self.assertEqual(list(self.service.candidates), [manifest.id])
        self.network('https://cdn.example/abc000.png', mime='image/png', body=b'encrypted bytes')
        self.network('https://cdn.example/enc.key', mime='audio/mpeg', body=b'1234567890123456')
        self.assertEqual(list(self.service.candidates), [manifest.id])
        resources = manifest.metadata['manifest']['resources']
        self.assertTrue(all(resource['role'] == 'Media Segment' for resource in resources))
        self.assertEqual(manifest.metadata['segment_requests'], 2)

    def test_scenario05_dash_representation_quality_and_segment_templates(self):
        self.network('https://cdn.example/chunk_high_00003.m4s', body=MP4)
        self.network('https://cdn.example/init_high.mp4', body=MP4)
        candidate = self.network('https://cdn.example/manifest', mime='application/octet-stream', body=DASH)
        self.assertEqual(candidate.media_type, 'DASH')
        self.assertEqual(candidate.resolution, (1920, 1080))
        self.assertEqual(candidate.bandwidth, 4000000)
        self.assertEqual(candidate.duration, 150)
        self.assertEqual(candidate.playlist_type, 'VOD')
        self.assertEqual(list(self.service.candidates), [candidate.id])
        self.network('https://cdn.example/chunk_low_00004.m4s', body=MP4)
        self.assertEqual(list(self.service.candidates), [candidate.id])

    def test_scenario06_blob_never_becomes_download_url_and_network_is_associated(self):
        self.dom([self.video('blob:https://page.example/opaque')])
        self.assertEqual(self.service.player_type, 'BLOB_MSE')
        self.assertEqual(self.service.candidates, {})
        candidate = self.network('https://cdn.example/index.m3u8', body=CLEAR_HLS)
        self.assertIn('MSE', candidate.sources)
        self.assertEqual(candidate.metadata['player_type'], 'BLOB_MSE')
        self.assertTrue(candidate.metadata['dom_current'])
        self.assertFalse(candidate.url.startswith('blob:'))

    def test_scenario07_master_variants_keep_real_resolution_and_derived_headers(self):
        context = RequestContext('https://cdn.example/master?sign=parent&t=raw',
                                 headers={'Referer': 'https://page.example/', 'User-Agent': 'Browser',
                                          'Cookie': 'session=private', 'Range': 'bytes=0-'}, inherit_range=True)
        master = self.network(context=context, mime='application/octet-stream', body=MASTER)
        variants = [candidate for candidate in self.service.candidates.values() if candidate.id != master.id]
        self.assertEqual(len(variants), 3)
        self.assertEqual({candidate.resolution for candidate in variants}, {(854, 480), (1280, 720), (1920, 1080)})
        self.assertEqual(master.resolution_label, '1080p')
        self.assertEqual(master.metadata['frame_rate'], '60')
        low = next(candidate for candidate in variants if candidate.resolution[1] == 480)
        self.assertEqual(low.url, 'https://cdn.example/low/index.m3u8?token=variant-low')
        self.assertEqual(low.request_context.headers['range'], 'bytes=0-')
        self.assertEqual(low.request_context.cookies, 'session=private')
        self.assertNotIn('origin', low.request_context.headers)
        self.assertEqual(self.service.ranked()[0].id, master.id)
        self.assertFalse(low.confirmed)

    def test_scenario08_redirect_original_final_and_chain_are_saved(self):
        initial = RequestContext('https://page.example/redirect?token=initial', headers={'Referer': 'https://page.example/'})
        final = initial.redirected('https://cdn.example/index?sign=final', 302)
        candidate = self.network(context=final, mime='application/octet-stream', body=CLEAR_HLS)
        self.assertEqual(candidate.request_context.original_url, initial.url)
        self.assertEqual(candidate.final_url, final.url)
        self.assertEqual(candidate.request_context.redirect_chain[0]['status'], 302)
        self.assertEqual(candidate.redirects[0]['from'], initial.url)
        self.assertEqual(candidate.metadata['manifest']['resources'][0]['resolved_uri'], 'https://cdn.example/a.ts')

    def test_scenario09_query_bytes_are_preserved_and_display_masked(self):
        url = 'https://cdn.example/v?secure=a%2Fb+c&sign=xyz&token=private&expires=9999999999&t=123&auth=z&signature=sig'
        candidate = self.network(url)
        self.assertEqual(candidate.url, url)
        safe = json.dumps(candidate.safe_diagnostic())
        for secret in ('a%2Fb+c', 'xyz', 'private', '9999999999', '123', 'sig'):
            self.assertNotIn('='+secret, safe)
        self.assertIn('secure=********', safe)

    def test_scenario10_vast_ad_does_not_win_over_main_movie(self):
        ad = self.network('https://cdn.example/spot.mp4', initiator='https://scripts.example/ad-provider.js')
        movie = self.network('https://other-cdn.example/main.mp4')
        self.dom([self.video(ad.url, duration=10, is_main=False, area=40000),
                  self.video(movie.url, width=1920, height=1080)])
        self.assertTrue(ad.is_ad)
        self.assertFalse(movie.is_ad)
        self.assertEqual(self.service.ranked()[0].id, movie.id)
        self.assertNotIn(ad.id, [candidate.id for candidate in self.service.ranked()])
        self.assertIn(ad.id, [candidate.id for candidate in self.service.ranked(include_low=True)])

    def test_scenario11_thumbnail_vtt_is_excluded(self):
        self.network('https://cdn.example/thumbnail.vtt', mime='text/vtt', body=b'WEBVTT\n00:00.000 --> 00:01.000\nposter.jpg')
        self.assertEqual(self.service.ranked(include_low=True), [])

    def test_scenario12_video_poster_is_not_a_media_candidate(self):
        self.dom([{'poster': 'https://cdn.example/poster.png', 'src': '', 'current_src': '', 'paused': True}])
        self.network('https://cdn.example/poster.png', mime='image/png', body=b'\x89PNG\r\n\x1a\nposter')
        self.assertEqual(self.service.candidates, {})

    def test_scenario13_multiple_videos_use_main_and_current_source(self):
        small = self.network('https://cdn.example/small.mp4')
        main = self.network('https://cdn.example/main.mp4')
        self.dom([self.video(small.url, duration=20, width=320, height=180, area=57600, paused=True, is_main=False),
                  self.video(main.url, width=1920, height=1080)])
        self.assertEqual(self.service.ranked()[0].id, main.id)
        self.assertGreater(main.score, small.score)
        self.assertEqual(main.resolution, (1920, 1080))

    def test_scenario14_real_drm_is_blocked_and_never_default_above_clear(self):
        drm = self.network('https://cdn.example/protected.m3u8', body=b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://license",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:2,\np.ts\n')
        clear = self.network('https://cdn.example/clear.m3u8', body=CLEAR_HLS)
        self.dom([self.video(drm.url, width=3840, height=2160)])
        self.assertTrue(drm.drm)
        self.assertFalse(drm.downloadable)
        self.assertEqual(self.service.ranked()[0].id, clear.id)
        self.assertIn(drm.id, [candidate.id for candidate in self.service.ranked()])

    def test_late_clear_probe_cannot_erase_existing_explicit_drm_evidence(self):
        candidate = self.network('https://cdn.example/master', body=MASTER)
        protected = {'media_type': 'HLS', 'drm': True, 'encryption': 'CENC',
                     'classification': 'DRM_WIDEVINE', 'error_code': 'PROTECTED_MEDIA',
                     'evidence': ['子清单明确包含 Widevine。'],
                     'keys': [{'classification': 'DRM_WIDEVINE', 'drm': True,
                               'keyformat': 'com.widevine.alpha'}]}
        self.service.apply_probe(candidate.id, protected)
        self.service.apply_probe(candidate.id, {'media_type': 'HLS', 'drm': False,
                                               'encryption': 'None', 'classification': 'CLEAR_HLS',
                                               'evidence': ['普通 Root 未包含 KEY。'], 'keys': []})
        self.assertIs(candidate.drm, True)
        self.assertFalse(candidate.downloadable)
        self.assertEqual(candidate.classification, 'DRM_WIDEVINE')
        self.assertEqual(candidate.encryption, 'CENC')
        self.assertIn('子清单明确包含 Widevine。', candidate.metadata['manifest']['evidence'])
        self.assertEqual(candidate.metadata['manifest']['keys'][0]['keyformat'], 'com.widevine.alpha')

    def test_late_clear_master_body_keeps_child_drm_until_new_search(self):
        candidate = self.network('https://cdn.example/master', body=MASTER)
        self.service.apply_probe(candidate.id, {'media_type': 'HLS', 'drm': True, 'encryption': 'CENC',
                                                'classification': 'DRM_PLAYREADY', 'error_code': 'PROTECTED_MEDIA',
                                                'evidence': ['子清单 ContentProtection 为 PlayReady。']})
        self.network(candidate.url, mime='application/x-mpegurl', body=MASTER,
                     headers={'Cookie': 'session=latest-actual'})
        self.assertIs(candidate.drm, True)
        self.assertEqual(candidate.classification, 'DRM_PLAYREADY')
        self.assertFalse(candidate.downloadable)
        self.assertEqual(candidate.request_context.cookies, 'session=latest-actual')
        self.assertIn('子清单 ContentProtection 为 PlayReady。', candidate.safe_diagnostic()['manifest']['evidence'])
        old_id = candidate.id
        self.service.clear()
        fresh = self.network('https://cdn.example/master', body=MASTER)
        self.assertNotEqual(old_id, fresh.id)
        self.assertIs(fresh.drm, False)
        self.assertTrue(fresh.downloadable)

    def test_scenario15_page_without_media_has_no_candidates(self):
        self.dom([])
        self.network('https://page.example/', mime='text/html', body=b'<html>ordinary page</html>')
        self.network('https://page.example/config.json', mime='application/json', body=b'{"ok":true}')
        self.assertEqual(self.service.ranked(include_low=True), [])

    def test_direct_mp4_complete_moov_is_clear_but_partial_header_is_unknown(self):
        complete = self.network('https://cdn.example/whole.mp4', body=MP4, body_complete=True)
        partial = self.network('https://cdn.example/partial.mp4', body=MP4[:16], status=206)
        self.assertIs(complete.drm, False)
        self.assertIsNone(partial.drm)

    def test_direct_embedded_pssh_is_real_drm(self):
        system = bytes.fromhex('edef8ba979d64acea3c827dcd51d21ed')
        pssh = box(b'pssh', b'\0\0\0\0' + system + b'\0\0\0\0')
        candidate = self.network(body=box(b'ftyp', b'isom\0\0\0\0') + box(b'moov', pssh))
        self.assertIs(candidate.drm, True)
        self.assertEqual(candidate.classification, 'DRM_WIDEVINE')

    def test_widevine_and_playready_dash_manifests_are_drm(self):
        for scheme, classification in (('edef8ba9-79d6-4ace-a3c8-27dcd51d21ed', 'DRM_WIDEVINE'),
                                       ('9a04f079-9840-4286-ab92-e65be0885f95', 'DRM_PLAYREADY')):
            with self.subTest(scheme=scheme):
                candidate = self.network(f'https://cdn.example/{scheme}', body=(f'<MPD><Period><AdaptationSet><ContentProtection schemeIdUri="urn:uuid:{scheme}"/></AdaptationSet></Period></MPD>').encode())
                self.assertIs(candidate.drm, True)
                self.assertEqual(candidate.classification, classification)
                self.assertFalse(candidate.downloadable)

    def test_sample_aes_identity_remains_unknown_without_fake_drm(self):
        candidate = self.network('https://cdn.example/sample', body=b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.dat"\n#EXTINF:2,\np.ts\n')
        self.assertIsNone(candidate.drm)
        self.assertEqual(candidate.classification, 'HLS_SAMPLE_AES')

    def test_http403_does_not_imply_drm(self):
        candidate = self.network('https://cdn.example/index.m3u8', mime='application/x-mpegurl', status=403)
        self.service.apply_probe(candidate.id, {'error_code': 'HTTP_403', 'http_status': 403, 'drm': None})
        self.assertIsNone(candidate.drm)
        self.assertNotEqual(candidate.classification, 'DRM_UNKNOWN')
        self.assertFalse(candidate.confirmed)

    def test_headers_body_method_and_range_are_latest_actual_snapshot(self):
        context = RequestContext('https://cdn.example/opaque?token=abc', 'POST',
                                 {'User-Agent': 'UA', 'Range': 'bytes=0-', 'Cookie': 'session=first',
                                  'Authorization': 'Bearer first', 'Referer': 'https://page.example/'},
                                 b'body=first', inherit_range=True)
        candidate = self.network(context=context, body=CLEAR_HLS)
        context.headers['cookie'] = 'changed outside'
        latest = replace(context, headers={'range': 'bytes=0-', 'cookie': 'session=latest'}, body=b'body=latest')
        self.network(context=latest, body=CLEAR_HLS)
        self.assertEqual(len(self.service.candidates), 1)
        self.assertEqual(candidate.request_context.method, 'POST')
        self.assertEqual(candidate.request_context.body, b'body=latest')
        self.assertEqual(candidate.request_context.headers, {'range': 'bytes=0-', 'cookie': 'session=latest'})
        self.assertNotIn('origin', candidate.request_context.headers)

    def test_query_values_are_not_removed_for_deduplication(self):
        first = self.network('https://cdn.example/index?sign=one&t=1', body=CLEAR_HLS)
        second = self.network('https://cdn.example/index?sign=two&t=2', body=CLEAR_HLS)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(self.service.candidates), 2)

    def test_network_and_dom_same_final_url_are_deduplicated(self):
        url = 'https://cdn.example/video?secure=actual'
        self.dom([self.video(url)])
        self.assertEqual(len(self.service.candidates), 1)
        candidate = self.network(url, headers={'Cookie': 'session=actual'})
        self.assertEqual(len(self.service.candidates), 1)
        self.assertTrue(candidate.context_complete)
        self.assertTrue(candidate.downloadable)
        self.assertEqual(candidate.request_context.cookies, 'session=actual')
        self.assertIn('DOM', candidate.sources)
        self.assertIn('Network', candidate.sources)

    def test_dom_without_actual_request_headers_is_not_downloadable(self):
        self.dom([self.video('https://cdn.example/v.mp4')])
        candidate = next(iter(self.service.candidates.values()))
        self.assertTrue(candidate.confirmed)
        self.assertFalse(candidate.context_complete)
        self.assertFalse(candidate.downloadable)
        self.assertEqual(candidate.request_context.headers, {})

    def test_probe_does_not_replace_browser_context_or_original_range(self):
        context = RequestContext('https://cdn.example/video?token=actual', 'POST',
                                 {'Range': 'bytes=0-', 'Cookie': 'session=actual', 'User-Agent': 'Browser'},
                                 b'body=actual', inherit_range=True)
        candidate = self.network(context=context)
        snapshot = deepcopy(candidate.request_context)
        self.service.apply_probe(candidate.id, {'media_type': 'Direct', 'drm': False, 'encryption': 'None',
                                               'http_status': 206, 'original_url': 'https://other.example/',
                                               'final_url': 'https://other.example/', 'headers': {'cookie': 'wrong'},
                                               'request_range': 'bytes=1-999', 'method': 'GET'})
        self.assertEqual(candidate.request_context, snapshot)
        self.assertIs(candidate.drm, False)

    def test_synthetic_probe_body_keeps_actual_network_context_and_playback_evidence(self):
        context = RequestContext('https://cdn.example/master', headers={'cookie': 'session=browser',
                                 'range': 'bytes=0-', 'user-agent': 'Real Browser'}, inherit_range=True)
        candidate = self.network(context=context, mime='application/x-mpegurl', context_complete=False,
                                 initiator='script https://scripts.example/ad-provider.js')
        self.dom([self.video('blob:https://page.example/player', events=[{'type': 'seeked', 'timestamp': 15}])])
        actual = deepcopy(candidate.request_context)
        network_count = len(self.service._network)
        sequence = self.service._sequence
        self.service.consume_network(NetworkObservation('probe:'+candidate.id,
                                      replace(context, headers={'cookie': 'session=synthetic'}, method='POST', body=b'probe-body'),
                                      200, 'application/x-mpegurl', MASTER, frame_id='synthetic-frame',
                                      resource_type='Probe', body_complete=True, context_complete=True))
        self.assertEqual(candidate.request_context, actual)
        self.assertFalse(candidate.context_complete)
        self.assertEqual(candidate.frame_id, 'main')
        self.assertEqual(len(self.service._network), network_count)
        self.assertEqual(self.service._sequence, sequence)
        self.assertTrue(candidate.is_ad)
        self.assertFalse(candidate.metadata.get('seek_range', False))
        self.assertEqual(candidate.metadata.get('segment_requests', 0), 0)
        self.assertEqual(candidate.resolution, (1920, 1080))
        variants = [item for item in self.service.candidates.values() if item.id != candidate.id]
        self.assertEqual(len(variants), 3)
        self.assertTrue(all(item.request_context.cookies == 'session=browser' for item in variants))
        self.assertTrue(all(not item.context_complete for item in variants))

    def test_synthetic_probe_partial_direct_body_keeps_guard_clear_decision(self):
        candidate = self.network()
        self.service.apply_probe(candidate.id, {'media_type': 'Direct', 'drm': False,
                                               'encryption': 'None', 'classification': 'DIRECT_MEDIA'})
        self.service.consume_network(NetworkObservation('probe:'+candidate.id, candidate.request_context,
                                      200, 'video/mp4', MP4[:16], resource_type='Probe'))
        self.assertIs(candidate.drm, False)
        self.assertEqual(candidate.encryption, 'None')

    def test_probe_resource_roles_remove_early_key_and_init_candidates(self):
        key = self.network('https://cdn.example/key.dat', mime='video/mp4')
        init = self.network('https://cdn.example/init.mp4', body=MP4)
        manifest = self.network('https://cdn.example/index.m3u8', mime='application/x-mpegurl')
        self.service.apply_probe(manifest.id, {'media_type': 'HLS', 'manifest_type': 'Media',
                                               'drm': False, 'encryption': 'AES-128', 'classification': 'HLS_AES128',
                                               'keys': [{'resolved_uri': key.url, 'uri': 'key.dat'}],
                                               'resources': [{'resolved_uri': init.url, 'uri': 'init.mp4', 'role': 'Initialization Segment'}]})
        self.assertEqual(list(self.service.candidates), [manifest.id])

    def test_blob_ad_eme_does_not_mark_unrelated_main_stream(self):
        ad = self.network('https://cdn.example/ads/spot.mp4')
        movie = self.network('https://cdn.example/movie.mp4')
        self.dom([self.video(ad.url, is_main=False, media_keys='com.widevine.alpha'),
                  self.video(movie.url)])
        self.assertTrue(ad.drm)
        self.assertIsNone(movie.drm)
        self.assertEqual(self.service.ranked()[0].id, movie.id)

    def test_failed_capability_probe_or_eme_api_presence_is_not_drm(self):
        candidate = self.network()
        self.dom([self.video(candidate.url, media_keys=False, encrypted=False,
                             eme_available=True, capability_probe={'supported': False, 'key_system': 'com.widevine.alpha'})])
        self.assertIsNone(candidate.drm)
        self.assertTrue(candidate.downloadable)

    def test_actual_encrypted_event_for_current_video_is_drm(self):
        candidate = self.network()
        self.dom([self.video(candidate.url, events=[{'type': 'encrypted', 'timestamp': 10}])])
        self.assertTrue(candidate.drm)
        self.assertFalse(candidate.downloadable)

    def test_ambiguous_blob_eme_is_not_applied_to_whole_frame(self):
        first = self.network('https://cdn.example/a.mp4')
        second = self.network('https://cdn.example/b.mp4')
        self.dom([self.video('blob:https://page.example/player', media_keys=True)])
        self.assertIsNone(first.drm)
        self.assertIsNone(second.drm)
        self.assertEqual(self.service.player_type, 'BLOB_MSE')

    def test_frame_association_does_not_share_eme_between_iframes(self):
        first = self.network('https://cdn.example/a.mp4', frame='ad-frame')
        second = self.network('https://cdn.example/b.mp4', frame='main-frame')
        self.dom([self.video('blob:https://page.example/ad', media_keys=True)], frame='ad-frame')
        self.dom([self.video('blob:https://page.example/main')], frame='main-frame')
        self.assertTrue(first.drm)
        self.assertIsNone(second.drm)

    def test_download_shadow_and_cdn_host_names_are_not_ads(self):
        for name in ('download.mp4', 'shadow-video.mp4', 'advance.mp4', 'spotlight.mp4', 'vastness.mp4'):
            with self.subTest(name=name):
                candidate = self.network('https://independent-cdn.example/'+name)
                self.assertFalse(candidate.is_ad)
                self.assertIn(candidate.id, [item.id for item in self.service.ranked(limit=12)])

    def test_vast_mediafile_reference_marks_opaque_video_as_ad_retroactively(self):
        ad = self.network('https://cdn.example/opaque123')
        main = self.network('https://cdn.example/movie')
        self.network('https://page.example/metadata', mime='application/xml',
                     body=b'<VAST><Ad><InLine><Creatives><Creative><Linear><MediaFiles><MediaFile>https://cdn.example/opaque123</MediaFile></MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>')
        self.assertTrue(ad.is_ad)
        self.assertEqual(self.service.ranked()[0].id, main.id)

    def test_parser_page_named_vast_or_ads_does_not_mark_main_media_as_ad(self):
        for page in ('https://page.example/pages/vast', 'https://page.example/ads-document'):
            with self.subTest(page=page):
                candidate = self.network('https://cdn.example/movie'+str(self.next_request)+'.mp4',
                                         initiator='parser '+page)
                self.assertFalse(candidate.is_ad)
                self.assertIn(candidate.id, [item.id for item in self.service.ranked()])

    def test_script_initiator_random_query_ad_word_does_not_mark_movie_as_ad(self):
        candidate = self.network(initiator='script https://page.example/player.js?token=ad&signature=vast&auth=ads loadMedia')
        self.assertFalse(candidate.is_ad)
        actual_ad = self.network('https://cdn.example/opaque-ad-resource', initiator='script https://scripts.example/ad-provider.js?token=safe loadMedia')
        self.assertTrue(actual_ad.is_ad)

    def test_probe_chinese_error_and_code_are_preserved_without_body_credentials(self):
        context = RequestContext('https://cdn.example/movie', 'POST', body=b'password=body-private')
        candidate = self.network(context=context)
        self.service.apply_probe(candidate.id, {'error_code': 'HTTP_403', 'http_status': 403,
                                               'probe_error': '服务器拒绝 body-private，请重新登录。'})
        self.assertEqual(candidate.metadata['error_code'], 'HTTP_403')
        self.assertIn('服务器拒绝', candidate.metadata['probe_error'])
        self.assertNotIn('body-private', candidate.metadata['probe_error'])
        self.assertIn('重新登录', candidate.safe_diagnostic()['probe_error'])

    def test_successful_probe_clears_previous_error_detail(self):
        candidate = self.network()
        self.service.apply_probe(candidate.id, {'error_code': 'HTTP_403', 'probe_error': '之前的失败。'})
        self.service.apply_probe(candidate.id, {'media_type': 'Direct', 'drm': False, 'encryption': 'None', 'http_status': 200})
        self.assertNotIn('error_code', candidate.metadata)
        self.assertNotIn('probe_error', candidate.metadata)
        self.assertEqual(candidate.encryption, 'None')

    def test_malformed_initiator_url_does_not_crash_media_analysis(self):
        candidate = self.network(initiator='script https://[invalid-player loadMedia')
        self.assertFalse(candidate.is_ad)
        self.assertTrue(candidate.downloadable)

    def test_self_referencing_master_does_not_create_parent_metadata_cycle(self):
        master = self.network('https://cdn.example/master.m3u8', body=b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nmaster.m3u8\n')
        self.assertEqual(len(self.service.candidates), 1)
        self.assertNotIn('parent_manifest', master.metadata)

    def test_partial_moov_header_does_not_claim_clear_direct_media(self):
        partial = box(b'ftyp', b'isom\0\0\0\0') + struct.pack('>I4s', 100, b'moov') + b'partial'
        candidate = self.network(body=partial, status=206)
        self.assertIsNone(candidate.drm)
        self.assertEqual(candidate.encryption, 'Unknown')

    def test_confirmed_clear_direct_drm_survives_later_http_error_probe(self):
        candidate = self.network(body=MP4)
        self.assertIs(candidate.drm, False)
        self.service.apply_probe(candidate.id, {'media_type': 'Direct', 'drm': None,
                                               'error_code': 'HTTP_403', 'probe_error': '请求被拒绝。'})
        self.assertIs(candidate.drm, False)

    def test_actual_png_body_cannot_become_direct_even_with_wrong_video_mime(self):
        self.network('https://cdn.example/poster', mime='video/mp4', body=b'\x89PNG\r\n\x1a\nimage')
        self.assertEqual(self.service.candidates, {})

    def test_deterministic_merge_when_redirect_joins_two_existing_candidates(self):
        first = self.network('https://page.example/redirect', mime='application/x-mpegurl')
        final = self.network('https://cdn.example/movie', body=CLEAR_HLS)
        self.assertEqual(len(self.service.candidates), 2)
        redirected = RequestContext(first.url).redirected(final.url, 302)
        merged = self.network(context=redirected, body=CLEAR_HLS)
        self.assertEqual(len(self.service.candidates), 1)
        self.assertEqual(merged.id, first.id)
        self.assertEqual(merged.final_url, final.url)

    def test_late_body_update_of_pre_seek_request_does_not_get_seek_bonus(self):
        context = RequestContext('https://cdn.example/movie', headers={'range': 'bytes=0-'})
        early = NetworkObservation('old', context, 206, 'video/mp4', frame_id='main')
        self.service.consume_network(early)
        candidate = next(iter(self.service.candidates.values()))
        self.dom([self.video('blob:https://page.example/player', events=[{'type': 'seeked', 'timestamp': 15}])])
        self.service.consume_network(replace(early, body=MP4))
        self.assertFalse(candidate.metadata.get('seek_range', False))

    def test_hls_plain_segment_after_seek_also_gives_behavior_bonus(self):
        manifest = self.network('https://cdn.example/index', body=CLEAR_HLS)
        self.dom([self.video('blob:https://page.example/player', events=[{'type': 'seeked', 'timestamp': 15}])])
        self.network('https://cdn.example/a.ts', mime='video/mp2t')
        self.assertTrue(manifest.metadata['seek_range'])

    def test_blob_master_and_observed_variant_are_one_media_family(self):
        master = self.network('https://cdn.example/master', body=MASTER)
        child = self.network('https://cdn.example/high/index.m3u8', body=CLEAR_HLS)
        self.dom([self.video('blob:https://page.example/player', media_keys=True)])
        self.assertTrue(master.metadata['dom_current'])
        self.assertTrue(child.metadata['dom_current'])
        self.assertTrue(master.drm)
        self.assertTrue(child.drm)

    def test_utf16_dash_content_overrides_misleading_image_mime(self):
        candidate = self.network('https://cdn.example/opaque.png', mime='image/png', body='<MPD><Period/></MPD>'.encode('utf-16'))
        self.assertEqual(candidate.media_type, 'DASH')
        self.assertTrue(candidate.confirmed)

    def test_many_signed_redirects_keep_alias_index_bounded(self):
        initial = RequestContext('https://page.example/redirect')
        for index in range(MAX_OBSERVATIONS * 3 + 10):
            context = initial.redirected(f'https://cdn.example/movie?sign={index}', 302)
            self.network(context=context)
        self.assertEqual(len(self.service.candidates), 1)
        self.assertLessEqual(len(self.service._urls), MAX_OBSERVATIONS * 3)

    def test_filename_1080p_does_not_supply_real_resolution(self):
        candidate = self.network('https://cdn.example/1080p.mp4')
        self.assertEqual(candidate.resolution, (0, 0))
        self.assertEqual(candidate.resolution_label, '--')
        self.dom([self.video(candidate.url, width=640, height=360)])
        self.assertEqual(candidate.resolution_label, '360p')

    def test_live_hls_and_dynamic_dash_keep_live_type(self):
        hls = self.network('https://cdn.example/live.m3u8', body=b'#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:2,\na.ts\n')
        dash = self.network('https://cdn.example/live.mpd', body=b'<MPD type="dynamic"><Period/></MPD>')
        self.assertEqual(hls.playlist_type, 'LIVE')
        self.assertEqual(dash.playlist_type, 'LIVE')
        self.assertIsNone(hls.duration)

    def test_hls_subtitle_and_image_renditions_are_not_complete_media(self):
        self.network('https://cdn.example/subtitle.m3u8', body=CLEAR_HLS)
        self.network('https://cdn.example/images.m3u8', body=CLEAR_HLS)
        master = self.network('https://cdn.example/master.m3u8', body=b'#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="subtitle.m3u8"\n#EXT-X-IMAGE-STREAM-INF:BANDWIDTH=1000,URI="images.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\nvideo.m3u8\n')
        self.assertEqual(len(self.service.candidates), 2)
        self.assertEqual(self.service.ranked()[0].id, master.id)

    def test_manifest_media_semantics_ignore_arbitrary_extensions(self):
        names = ('a.ts', 'b.m4s', 'c.aac', 'd.mp4', 'e.bin', 'f.dat', 'g.png', 'no-extension', 'other.weird')
        candidate = self.network('https://cdn.example/master', body=('#EXTM3U\n' + ''.join('#EXTINF:2,\n'+name+'\n' for name in names)).encode())
        for name in names:
            self.network('https://cdn.example/'+name, mime='video/mp4', body=MP4)
        self.assertEqual(list(self.service.candidates), [candidate.id])

    def test_seek_then_new_direct_range_increases_score(self):
        first = self.network('https://cdn.example/a.mp4', headers={'Range': 'bytes=0-'}, status=206)
        second = self.network('https://cdn.example/b.mp4', headers={'Range': 'bytes=0-'}, status=206)
        self.dom([self.video('blob:https://page.example/player', events=[{'type': 'seeked', 'timestamp': 15}])])
        self.network(first.url, headers={'Range': 'bytes=5000-'}, status=206)
        self.assertTrue(first.metadata['seek_range'])
        self.assertFalse(second.metadata.get('seek_range', False))
        self.assertGreater(first.score, second.score)

    def test_continuous_segments_increase_the_owning_manifest_score(self):
        first = self.network('https://cdn.example/a/index', body=CLEAR_HLS)
        second = self.network('https://cdn.example/b/index', body=CLEAR_HLS)
        self.network('https://cdn.example/a/a.ts', mime='video/mp2t')
        self.network('https://cdn.example/a/b.ts', mime='video/mp2t')
        self.assertEqual(first.metadata['segment_requests'], 2)
        self.assertGreater(first.score, second.score)

    def test_actual_request_context_cannot_be_modified_through_ranked_snapshot(self):
        candidate = self.network(headers={'Cookie': 'session=private'})
        result = self.service.ranked()
        result[0].request_context.headers['cookie'] = 'changed'
        result[0].score_reasons.append({'points': 99, 'reason': 'fake'})
        self.assertEqual(candidate.request_context.cookies, 'session=private')
        self.assertNotIn('fake', repr(candidate.score_reasons))

    def test_sensitive_headers_and_queries_never_appear_in_safe_diagnostic(self):
        candidate = self.network('https://cdn.example/v?token=private-sign', headers={'Cookie': 'session=private-cookie',
                                  'Authorization': 'Bearer private-auth', 'Referer': 'https://page.example/?token=private-ref'})
        safe = json.dumps(candidate.safe_diagnostic())
        for secret in ('private-sign', 'private-cookie', 'private-auth', 'private-ref'):
            self.assertNotIn(secret, safe)
        self.assertNotIn('private-cookie', repr(candidate))

    def test_failed_probe_diagnostic_does_not_turn_clear_hls_into_drm(self):
        candidate = self.network('https://cdn.example/index', body=CLEAR_HLS)
        self.service.apply_probe(candidate.id, {'error_code': 'HTTP_403', 'http_status': 403})
        self.assertIs(candidate.drm, False)
        self.assertEqual(candidate.metadata['probe_error'], 'HTTP_403')

    def test_network_context_incomplete_does_not_download_even_if_body_is_media(self):
        candidate = self.network(body=MP4, context_complete=False)
        self.assertTrue(candidate.confirmed)
        self.assertFalse(candidate.downloadable)

    def test_manifest_mime_without_body_can_be_probed_but_extension_alone_is_low(self):
        mime = self.network('https://cdn.example/opaque', mime='application/dash+xml')
        weak = self.network('https://cdn.example/fake.m3u8', mime='application/octet-stream')
        self.assertTrue(mime.confirmed)
        self.assertFalse(weak.confirmed)
        self.assertNotIn(weak.id, [candidate.id for candidate in self.service.ranked()])

    def test_clear_resets_all_candidates_roles_blob_and_credentials(self):
        candidate = self.network('https://cdn.example/index', body=AES_HLS, headers={'Cookie': 'private-cookie'})
        old_id = candidate.id
        self.dom([self.video('blob:https://page.example/opaque')])
        self.service.clear()
        self.assertEqual(self.service.candidates, {})
        self.assertEqual(self.service.player_type, '')
        self.assertEqual(self.service.page_url, '')
        self.assertEqual(len(self.service._roles), 0)
        self.assertEqual(len(self.service._network), 0)
        self.assertEqual(len(self.service._frames), 0)
        replacement = self.network('https://cdn.example/new.mp4')
        self.assertNotEqual(old_id, replacement.id)
        self.service.apply_probe(old_id, {'drm': True})
        self.assertIsNone(replacement.drm)

    def test_candidate_and_observation_indexes_are_bounded(self):
        for index in range(MAX_OBSERVATIONS + 20):
            self.network(f'https://cdn.example/video{index}.mp4')
        self.assertLessEqual(len(self.service.candidates), MAX_CANDIDATES)
        self.assertLessEqual(len(self.service._network), MAX_OBSERVATIONS)
        self.assertLessEqual(len(self.service._urls), MAX_CANDIDATES * 3)

    def test_live_manifest_resource_table_is_bounded(self):
        for batch in range(3):
            body = '#EXTM3U\n' + ''.join(f'#EXTINF:2,\npart{batch*2000+index}.bin\n' for index in range(2000))
            self.network('https://cdn.example/live', body=body.encode())
        self.assertLessEqual(len(self.service._roles), MAX_RESOURCES)
        self.assertEqual(len(self.service.candidates), 1)

    def test_score_reasons_have_real_integers_and_score_is_rule_score_not_probability(self):
        candidate = self.network()
        self.dom([self.video(candidate.url, width=3840, height=2160)])
        self.assertGreaterEqual(candidate.score, 0)
        self.assertLessEqual(candidate.score, 99)
        self.assertTrue(candidate.score_reasons)
        self.assertTrue(all(type(reason['points']) is int and isinstance(reason['reason'], str) for reason in candidate.score_reasons))


if __name__ == '__main__':
    unittest.main()
