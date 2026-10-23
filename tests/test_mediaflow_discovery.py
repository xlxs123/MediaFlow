"""MediaFlow 发现/评分/分组整合回归。

全部网络与 DOM 证据均为本地合成输入，不请求真实网站，也不代表在线下载验收。
实际执行 MediaDiscoveryService、RouVideoResolver、协议解析器及媒体分组。
"""
from copy import deepcopy
from dataclasses import replace
import unittest
import zlib

from core.discovery_models import MediaCandidate, NetworkObservation
from core.media_confidence import confidence_tier
from core.media_discovery import MediaDiscoveryService, is_verified_resolver
from core.media_groups import available_candidate, group_media, ordered_candidates
from core.request_context import RequestContext
from core.site_resolvers import RouVideoResolver
from tests.test_rou_current_protocol import context as rou_context, observation as rou_observation, playlist
from tests.test_rou_playlist_decoder import png


PAGE = 'https://www.douyin.com/video/1234567890123456789'
HLS = (b'#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-PLAYLIST-TYPE:VOD\n'
       b'#EXTINF:9,\nabc000.png\n#EXTINF:10,\nabc001.png\n#EXT-X-ENDLIST\n')
MASTER = (b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\nlow/index.m3u8\n'
          b'#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=3840x2160\nhigh/index.m3u8\n')


class MediaFlowDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.service = MediaDiscoveryService()
        self.sequence = 0

    def network(self, url='https://media.example/current.mp4', *, mime='video/mp4', body=None,
                context=None, status=200, frame='main', total=2655560, **extra):
        self.sequence += 1
        context = context or RequestContext(url, headers={'Referer': PAGE, 'User-Agent': 'Fixture Browser',
                                                          'Range': 'bytes=0-'})
        observation = NetworkObservation(f'fixture-{self.sequence}', context, status, mime, body,
            frame_id=frame, timestamp=float(self.sequence), content_length=total,
            response_headers={'Content-Type': mime, 'Content-Range': f'bytes 0-{total-1}/{total}'},
            **extra)
        original = deepcopy(observation)
        self.service.consume_network(observation)
        self.assertEqual(observation, original)
        return next((value for value in self.service.candidates.values()
                     if value.final_url == context.final_url), None)

    def video(self, url, **extra):
        value = {'src': url, 'current_src': url, 'width': 1280, 'height': 720,
                 'duration': 17.368, 'paused': False, 'ended': False, 'visible': True,
                 'area': 921600, 'is_main': True, 'focused': False, 'progressing': False,
                 'ready_state': 4, 'events': []}
        value.update(extra)
        return value

    def dom(self, videos, page=PAGE, frame='main'):
        snapshot = {'page_url': page, 'frame_id': frame, 'page_title': 'Fixture page',
                    'timestamp': 100 + self.sequence, 'videos': videos}
        original = deepcopy(snapshot)
        self.service.consume_dom(snapshot)
        self.assertEqual(snapshot, original)

    def has_reason(self, value, text):
        return any(text in reason for reason in value.confidenceReasons)

    def points(self, value):
        return [item['points'] for item in value.metadata['confidence_details']]

    def test_douyin_reported_normal_video_a_keeps_actual_context(self):
        url = 'https://v26-web.douyinvod.com/opaque?sign=PRIVATE&t=173&token=A%2FB'
        context = RequestContext(url, headers={'Referer': PAGE, 'User-Agent': 'Fixture Browser',
            'Range': 'bytes=0-', 'Cookie': 'session=PRIVATE', 'Authorization': 'Bearer PRIVATE'})
        actual = self.network(context=context)
        self.dom([self.video(actual.final_url)])
        self.assertEqual(actual.confidenceScore, 100)
        self.assertEqual(actual.confidence, 100)
        self.assertEqual(actual.duration, 17.368)
        self.assertEqual(actual.resolution, (1280, 720))
        self.assertEqual(self.points(actual), [40, 20, 20, 10, 10, 10])
        self.assertTrue(self.has_reason(actual, 'currentSrc'))
        self.assertTrue(self.has_reason(actual, '17.368'))
        self.assertEqual(actual.request_context, context)
        self.assertEqual(actual.final_url, url)
        self.assertNotIn('origin', actual.request_context.headers)
        self.assertEqual(self.service.ranked()[0].id, actual.id)

    def test_other_reported_durations_and_arbitrary_cdns_are_supported_by_same_flow(self):
        for seconds, size, total in ((88.53, (1080, 1920), 7756470), (10.35, (720, 1280), 771862)):
            with self.subTest(duration=seconds):
                self.service = MediaDiscoveryService()
                actual = self.network(f'https://cdn-{total}.unrelated.example/media.bin', total=total)
                self.dom([self.video(actual.url, duration=seconds, width=size[0], height=size[1])])
                self.assertEqual(actual.confidenceScore, 100)
                self.assertEqual(actual.duration, seconds)
                self.assertEqual(actual.resolution, size)

    def test_focused_smaller_player_beats_larger_visible_preload(self):
        main = self.network('https://cdn.example/current.mp4')
        preload = self.network('https://cdn.example/next.mp4')
        self.dom([self.video(main.url, area=44000, focused=True),
                  self.video(preload.url, area=230400, width=3840, height=2160,
                             is_main=False, paused=True)])
        self.assertEqual((main.confidenceScore, preload.confidenceScore), (100, 70))
        self.assertEqual(self.service.ranked()[0].id, main.id)

    def test_hidden_4k_preload_loses_to_current_720p_video(self):
        preload = self.network('https://media.example/preloaded.mp4')
        main = self.network('https://media.example/current.mp4')
        self.dom([self.video(preload.url, visible=False, width=3840, height=2160,
                             area=0, is_main=False), self.video(main.url)])
        self.assertEqual((main.confidenceScore, preload.confidenceScore), (100, 20))
        self.assertTrue(self.has_reason(preload, '隐藏'))
        self.assertFalse(self.has_reason(preload, 'currentSrc'))
        self.assertEqual(self.service.ranked()[0].id, main.id)
        groups = group_media(self.service.ranked(include_low=True), source_page=PAGE)
        self.assertEqual(groups[0].selected_candidate.id, main.id)
        self.assertFalse(next(group for group in groups if preload.id in
                              {item.id for item in group.candidates}).selected)

    def test_visible_4k_preload_loses_to_current_main_720p_player(self):
        preload = self.network('https://media.example/preloaded.mp4')
        main = self.network('https://media.example/current.mp4')
        self.dom([self.video(preload.url, width=3840, height=2160, is_main=False),
                  self.video(main.url)])
        self.assertEqual((main.confidenceScore, preload.confidenceScore), (100, 70))
        self.assertFalse(self.has_reason(preload, 'currentSrc'))
        self.assertEqual(self.service.ranked()[0].id, main.id)
        groups = group_media(self.service.ranked(include_low=True), source_page=PAGE)
        self.assertEqual(groups[0].selected_candidate.id, main.id)

    def test_equal_visible_players_without_focus_or_progress_remain_ambiguous(self):
        first = self.network('https://media.example/one.mp4')
        second = self.network('https://media.example/two.mp4')
        self.dom([self.video(first.url, is_main=False), self.video(second.url, is_main=False)])
        self.assertEqual((first.confidenceScore, second.confidenceScore), (70, 70))
        for value in (first, second):
            self.assertFalse(value.metadata['confidence_evidence']['current_player'])
            self.assertFalse(self.has_reason(value, 'currentSrc'))
        self.assertEqual(len(self.service.ranked()), 2)

    def test_focus_disambiguates_equal_visible_players(self):
        first = self.network('https://media.example/one.mp4')
        second = self.network('https://media.example/two.mp4')
        self.dom([self.video(first.url, is_main=False), self.video(second.url, is_main=False, focused=True)])
        self.assertEqual((first.confidenceScore, second.confidenceScore), (70, 100))
        self.assertEqual(self.service.ranked()[0].id, second.id)

    def test_actual_progress_disambiguates_equal_visible_players(self):
        first = self.network('https://media.example/one.mp4')
        second = self.network('https://media.example/two.mp4')
        self.dom([self.video(first.url, is_main=False), self.video(second.url, is_main=False, progressing=True)])
        self.assertEqual((first.confidenceScore, second.confidenceScore), (70, 100))
        self.assertEqual(self.service.ranked()[0].id, second.id)

    def test_paused_main_video_still_has_current_player_evidence(self):
        value = self.network()
        self.dom([self.video(value.url, paused=True, progressing=False)])
        self.assertEqual(value.confidenceScore, 100)
        self.assertTrue(self.has_reason(value, 'currentSrc'))
        self.assertFalse(value.metadata['dom_playing'])
        self.assertTrue(value.downloadable)

    def test_missing_visibility_does_not_fabricate_visible_or_hidden_evidence(self):
        value = self.network()
        video = self.video(value.url)
        video.pop('visible')
        self.dom([video])
        self.assertEqual(value.confidenceScore, 90)
        self.assertFalse(self.has_reason(value, '可见'))
        self.assertFalse(self.has_reason(value, '隐藏'))

    def test_switch_current_player_removes_old_current_and_visibility_points(self):
        old = self.network('https://media.example/old.mp4')
        new = self.network('https://media.example/new.mp4')
        self.dom([self.video(old.url)])
        self.assertEqual(old.confidenceScore, 100)
        self.dom([self.video(new.url)])
        self.assertEqual((old.confidenceScore, new.confidenceScore), (50, 100))
        self.assertFalse(self.has_reason(old, 'currentSrc'))
        self.assertFalse(self.has_reason(old, '可见'))
        self.assertEqual(confidence_tier(old.confidenceScore), 'confirm')
        self.assertEqual(self.service.ranked()[0].id, new.id)

    def test_late_network_event_cannot_resurrect_previous_current_player(self):
        old = self.network('https://media.example/old.mp4')
        new = self.network('https://media.example/new.mp4')
        self.dom([self.video(old.url)])
        self.dom([self.video(new.url)])
        repeated = self.network(old.url)
        self.assertIs(repeated, old)
        self.assertEqual((old.confidenceScore, new.confidenceScore), (50, 100))
        self.assertFalse(self.has_reason(old, 'currentSrc'))
        self.assertFalse(self.has_reason(old, '可见'))
        self.assertEqual(self.service.ranked()[0].id, new.id)

    def test_hidden_player_becoming_current_recomputes_without_stale_penalty(self):
        value = self.network()
        self.dom([self.video(value.url, visible=False, area=0, is_main=False)])
        self.assertEqual(value.confidenceScore, 20)
        self.dom([self.video(value.url)])
        self.assertEqual(value.confidenceScore, 100)
        self.assertFalse(self.has_reason(value, '隐藏'))

    def test_video_poster_does_not_make_main_video_a_thumbnail(self):
        value = self.network()
        self.dom([self.video(value.url, poster='https://images.example/thumbnail.png')])
        self.assertEqual(value.confidenceScore, 100)
        self.assertEqual(value.metadata['thumbnail'], 'https://images.example/thumbnail.png')
        self.assertFalse(self.has_reason(value, '缩略图'))
        self.assertFalse(self.has_reason(value, '预览'))
        self.assertTrue(available_candidate(value))

    def test_explicit_thumbnail_and_preview_are_deducted_independently(self):
        value = self.network()
        value.metadata.update(is_thumbnail=True, is_preview=True)
        self.dom([self.video(value.url)])
        self.assertEqual(value.confidenceScore, 10)
        self.assertEqual(self.points(value)[-2:], [-50, -50])
        self.assertFalse(available_candidate(value))

    def test_preview_network_resource_cannot_outrank_full_current_video(self):
        preview = self.network('https://media.example/preview.mp4')
        main = self.network('https://media.example/full.mp4')
        self.dom([self.video(preview.url, is_main=False), self.video(main.url)])
        self.assertEqual((preview.confidenceScore, main.confidenceScore), (20, 100))
        self.assertTrue(self.has_reason(preview, '预览'))
        self.assertEqual(self.service.ranked()[0].id, main.id)
        groups = group_media(self.service.ranked(include_low=True), source_page=PAGE)
        preview_group = next(group for group in groups if preview.id in {item.id for item in group.candidates})
        self.assertFalse(preview_group.selected)

    def test_explicit_thumbnail_role_affects_integrated_score(self):
        value = self.network()
        value.metadata['resource_role'] = 'Thumbnail'
        self.dom([self.video(value.url)])
        self.assertEqual(value.confidenceScore, 60)
        self.assertTrue(self.has_reason(value, '缩略图'))
        self.assertFalse(available_candidate(value))

    def test_explicit_preview_role_affects_integrated_score(self):
        value = self.network()
        value.metadata['resource_role'] = 'Preview'
        self.dom([self.video(value.url)])
        self.assertEqual(value.confidenceScore, 60)
        self.assertTrue(self.has_reason(value, '预览'))
        self.assertFalse(group_media([value], source_page=PAGE)[0].selected)

    def test_main_hls_parent_and_children_use_protocol_relationship_and_request_context(self):
        context = RequestContext('https://hls.example/master?sign=PRIVATE&t=171',
            headers={'Referer': PAGE, 'User-Agent': 'Fixture Browser', 'Range': 'bytes=0-', 'Cookie': 'id=PRIVATE'},
            inherit_range=True)
        parent = self.network(context=context, mime='application/vnd.apple.mpegurl', body=MASTER,
                              status=200, body_complete=True)
        children = [value for value in self.service.candidates.values()
                    if value.metadata.get('parent_manifest') == parent.id]
        self.assertEqual(len(children), 2)
        child_contexts = {value.id: deepcopy(value.request_context) for value in children}
        self.dom([self.video(parent.final_url, duration=19)])
        self.assertEqual(parent.confidenceScore, 100)
        for child in children:
            self.assertEqual(child.confidenceScore, 70)
            self.assertTrue(self.has_reason(child, 'currentSrc'))
            self.assertEqual(child.request_context, child_contexts[child.id])
            self.assertEqual(child.request_context.headers['referer'], PAGE)
            self.assertEqual(child.request_context.headers['range'], 'bytes=0-')
            self.assertEqual(child.request_context.cookies, 'id=PRIVATE')
            self.assertNotIn('origin', child.request_context.headers)
            self.assertFalse(child.confirmed)
        self.assertEqual(parent.request_context, context)
        groups = group_media(self.service.ranked(include_low=True), source_page=PAGE)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].selected_candidate.id, parent.id)

    def test_hls_children_lose_parent_current_evidence_after_player_switch(self):
        parent = self.network('https://hls.example/master.m3u8',
                              mime='application/vnd.apple.mpegurl', body=MASTER)
        children = [value for value in self.service.candidates.values()
                    if value.metadata.get('parent_manifest') == parent.id]
        self.dom([self.video(parent.final_url, duration=19)])
        self.assertTrue(all(child.confidenceScore == 70 for child in children))
        new = self.network('https://media.example/next-video.mp4')
        self.dom([self.video(new.final_url)])
        self.assertEqual(parent.confidenceScore, 40)
        self.assertTrue(all(child.confidenceScore == 10 for child in children))
        self.assertTrue(all(not self.has_reason(child, 'currentSrc') for child in children))
        self.assertEqual(self.service.ranked()[0].id, new.id)

    def test_network_only_video_is_retained_for_evidence_but_not_auto_selected(self):
        value = self.network()
        self.assertTrue(value.confirmed)
        self.assertEqual(value.confidenceScore, 10)
        self.assertEqual(self.service.ranked()[0].id, value.id)
        group = group_media([value], source_page=PAGE)[0]
        self.assertFalse(group.selected)
        self.assertIsNotNone(group.selected_candidate)
        self.assertTrue(available_candidate(group.selected_candidate))

    def test_hls_png_segments_remain_resources_and_do_not_get_image_penalty(self):
        value = self.network('https://hls.example/index.png?sign=PRIVATE',
                             mime='application/octet-stream', body=HLS, status=200, body_complete=True)
        self.dom([self.video(value.final_url, duration=19)])
        self.assertEqual(value.media_type, 'HLS')
        self.assertEqual(value.confidenceScore, 100)
        self.assertFalse(self.has_reason(value, '图片'))
        resources = value.metadata['manifest']['resources']
        self.assertEqual([resource['role'] for resource in resources], ['Media Segment', 'Media Segment'])
        self.network('https://hls.example/abc000.png', mime='image/png', body=b'segment bytes')
        self.assertEqual(len(self.service.candidates), 1)
        self.assertEqual(value.confidenceScore, 100)

    def test_rou_verified_decoded_png_manifest_fallback_is_70_without_dom(self):
        body = playlist(2, 19, 'a')
        observation = rou_observation(png(zlib.compress(body), 1))
        before = deepcopy(observation)
        result = RouVideoResolver(legacy_fallback=False).resolve(rou_context(observation))
        self.assertTrue(result.success, result.diagnostics)
        self.assertEqual(observation, before)
        resolved = result.candidates[0]
        original_context = deepcopy(resolved.request_context)
        original_text = resolved.manifest_text
        self.service.consume_resolver(resolved)
        actual = self.service.ranked()[0]
        self.assertTrue(is_verified_resolver(actual))
        self.assertEqual(actual.confidenceScore, 70)
        self.assertEqual(actual.score, 100)  # 旧协议验证字段仍兼容，真实性单独计分。
        self.assertTrue(self.has_reason(actual, '入口已验证'))
        self.assertFalse(self.has_reason(actual, '图片'))
        self.assertEqual(actual.media_type, 'HLS')
        self.assertEqual(actual.duration, 19)
        self.assertEqual(actual.request_context, original_context)
        self.assertEqual(actual.manifest_text, original_text)
        self.assertTrue(actual.final_url.partition('?')[0].endswith('.png'))
        self.assertEqual(actual.manifest_origin, 'embedded:png/roUd')
        self.assertTrue(actual.downloadable)
        self.assertTrue(group_media([actual])[0].selected)

    def test_rou_late_png_transport_response_does_not_replace_70_point_protocol_candidate(self):
        body = playlist(2, 19, 'a')
        observation = rou_observation(png(body))
        result = RouVideoResolver(legacy_fallback=False).resolve(rou_context(observation))
        self.assertTrue(result.success, result.diagnostics)
        self.service.consume_resolver(result.candidates[0])
        self.service.consume_network(replace(observation, request_id='late-transport'))
        actual = self.service.ranked()[0]
        self.assertEqual(actual.confidenceScore, 70)
        self.assertEqual(actual.media_type, 'HLS')
        self.assertEqual(actual.content_type, 'application/vnd.apple.mpegurl')
        self.assertFalse(self.has_reason(actual, '图片'))

    def test_legacy_validated_decoded_entry_keeps_70_point_association(self):
        from tests.test_site_resolvers import page, observed
        resolved = RouVideoResolver().resolve(page(observations=[observed(png(HLS))])).candidates[0]
        self.assertTrue(resolved.validated and resolved.manifest_text.startswith('#EXTM3U'))
        self.assertFalse(resolved.metadata.get('resolver_verified', False))
        self.service.consume_resolver(resolved)
        actual = self.service.ranked()[0]
        self.assertEqual(actual.confidenceScore, 70)
        self.assertTrue(self.has_reason(actual, '入口已验证'))
        self.assertEqual(actual.download_payload(), resolved.download_payload())

    def test_unvalidated_resolver_claim_does_not_get_entry_bonus(self):
        from tests.test_site_resolvers import page, observed
        resolved = RouVideoResolver().resolve(page(observations=[observed(png(HLS))])).candidates[0]
        resolved.validated = False
        self.service.consume_resolver(resolved)
        self.assertEqual(self.service.ranked()[0].confidenceScore, 30)

    def test_group_thresholds_39_40_69_70_and_legacy_score_are_distinct(self):
        # 权重都为 10 的倍数；69 分使用显式值验证分组公共接口的边界。
        for score, available, selected in ((39, True, False), (40, True, False),
                                           (69, True, False), (70, True, True)):
            with self.subTest(score=score):
                value = MediaCandidate('boundary', RequestContext('https://threshold.example/media.mp4'),
                    media_type='Direct', confirmed=True, http_status=200, score=99,
                    confidenceScore=score, duration=19, resolution=(1280, 720))
                self.assertEqual(available_candidate(value), available)
                group = group_media([value], source_page=PAGE)[0]
                self.assertEqual(group.selected, selected)
                self.assertEqual(group.selected_candidate is not None, available)

    def test_group_variant_order_prioritizes_confidence_before_resolution(self):
        base = dict(media_type='Direct', confirmed=True, http_status=200, duration=19,
                    metadata={'video_id': 'same-movie', 'page_url': PAGE})
        high_quality = MediaCandidate('4k', RequestContext('https://quality.example/4k.mp4'),
            resolution=(3840, 2160), confidenceScore=70, score=99, **deepcopy(base))
        current = MediaCandidate('current', RequestContext('https://quality.example/720p.mp4'),
            resolution=(1280, 720), confidenceScore=100, score=40, **deepcopy(base))
        group = group_media([high_quality, current], source_page=PAGE)[0]
        self.assertEqual(len(group.candidates), 2)
        self.assertEqual(group.selected_candidate.id, current.id)
        self.assertEqual([value.id for value in ordered_candidates(group)], ['current', '4k'])

    def test_drm_and_http_failures_keep_download_guards_at_high_confidence(self):
        value = self.network()
        self.dom([self.video(value.url, media_keys='com.widevine.alpha')])
        self.assertEqual(value.confidenceScore, 100)
        self.assertTrue(value.drm)
        self.assertFalse(value.downloadable)
        self.assertFalse(available_candidate(value))
        other = self.network('https://media.example/failed.mp4', status=403)
        self.dom([self.video(other.url)])
        self.assertEqual(other.confidenceScore, 100)
        self.assertFalse(available_candidate(other))

    def test_confidence_diagnostics_do_not_expose_context_secrets(self):
        value = self.network('https://media.example/current?signature=PRIVATE-SIGNATURE')
        self.dom([self.video(value.url)])
        diagnostic = value.safe_diagnostic()
        self.assertEqual(diagnostic['confidenceScore'], 100)
        self.assertEqual(diagnostic['confidenceReasons'], value.confidenceReasons)
        for secret in ('PRIVATE-SIGNATURE', PAGE, 'Fixture Browser'):
            self.assertNotIn(secret, repr(diagnostic['confidenceReasons']))
        self.assertNotIn('PRIVATE-SIGNATURE', repr(diagnostic))


if __name__ == '__main__':
    unittest.main()
