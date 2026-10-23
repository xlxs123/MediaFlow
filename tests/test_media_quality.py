"""质量选择回归：本地协议/浏览器证据夹具，不代表真实站点在线实测。"""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zlib

from core.discovery_models import MediaCandidate, NetworkObservation
from core.media_details import enrich_media_details
from core.media_discovery import MediaDiscoveryService
from core.media_groups import MediaGroup, group_media
from core.media_quality import (QUALITY_PREFERENCES, quality_candidates,
                                recommend_candidate, update_quality)
from core.request_context import RequestContext
from core.site_resolvers import RouVideoResolver
from tests.test_rou_current_protocol import context as rou_context, observation as rou_observation, playlist
from tests.test_rou_playlist_decoder import png


def candidate(identifier='media', **fields):
    values = dict(id=identifier, request_context=RequestContext(f'https://cdn.example/{identifier}'),
                  media_type='Direct', confirmed=True, drm=False, http_status=200,
                  confidenceScore=80)
    values.update(fields)
    return MediaCandidate(**values)


def group(*members):
    return MediaGroup('identity', '同一视频', list(members), members[0] if members else None)


class MediaQualityTests(unittest.TestCase):
    def test_preferences_are_stable_public_values(self):
        self.assertEqual(QUALITY_PREFERENCES, (('auto', '自动推荐'), ('highest', '最高画质'),
                                               ('balanced', '平衡'), ('smallest', '最小文件')))

    def test_resolution_tiers_are_single_and_not_cumulative(self):
        for dimensions, points in (((3840, 2160), 40), ((1920, 1080), 30),
                                   ((1280, 720), 20), ((854, 480), 10), ((640, 360), 0)):
            with self.subTest(dimensions=dimensions):
                item = candidate(resolution=dimensions)
                update_quality(item)
                self.assertEqual(item.qualityScore, points)
                self.assertEqual(len(item.metadata['quality_details']), int(points > 0))

    def test_vertical_resolution_uses_short_edge(self):
        item = candidate(resolution=(1080, 1920))
        update_quality(item)
        self.assertEqual(item.qualityScore, 30)
        self.assertIn('1080P', item.metadata['quality_details'][0]['reason'])

    def test_quality_maximum_is_sixty_without_invented_normalization(self):
        item = candidate(width=3840, height=2160, videoCodec='av01.0.08M.08', audioCodec='mp4a.40.2',
                         confidenceScore=100)
        update_quality(item)
        self.assertEqual(item.qualityScore, 60)
        self.assertEqual([detail['points'] for detail in item.metadata['quality_details']], [40, 10, 10])
        self.assertEqual(item.metadata['combined_score'], 76)

    def test_video_codec_factors(self):
        for codec, points in (('av01.0.08M.08', 10), ('av1', 10), ('hvc1.1.6.L120', 10),
                              ('hev1', 10), ('h265', 10), ('H.265', 10), ('hevc', 10), ('avc1.640028', 5),
                              ('h264', 5), ('H.264', 5), ('AVC', 5), ('vp09', 0), ('unknown', 0), (None, 0)):
            with self.subTest(codec=codec):
                item = candidate(videoCodec=codec)
                update_quality(item)
                self.assertEqual(item.qualityScore, points)

    def test_display_codec_aliases_in_existing_metadata(self):
        for codec, points in (('H.264', 5), ('H.265', 10), ('AVC', 5)):
            item = candidate(metadata={'codecs': codec})
            update_quality(item)
            self.assertEqual(item.qualityScore, points)

    def test_audio_evidence_is_explicit_not_video_mime(self):
        item = candidate(content_type='video/mp4')
        update_quality(item)
        self.assertEqual(item.qualityScore, 0)
        for fields in ({'audioCodec': 'opus'}, {'metadata': {'has_audio': True}},
                       {'metadata': {'codecs': 'avc1,mp4a.40.2'}}):
            item = candidate(**fields)
            update_quality(item)
            self.assertIn({'reason': '存在有效音频轨', 'points': 10}, item.metadata['quality_details'])
        for codec in ('none', 'unknown', 'avc1', ''):
            item = candidate(audioCodec=codec)
            update_quality(item)
            self.assertEqual(item.qualityScore, 0)

    def test_actual_details_override_existing_resolution_and_metadata(self):
        item = candidate(width=1920, height=1080, videoCodec='hvc1',
                         resolution=(3840, 2160), metadata={'codecs': 'avc1'})
        update_quality(item)
        self.assertEqual(item.qualityScore, 40)

    def test_unknown_resolution_is_not_guessed_from_filename_or_label(self):
        item = candidate('4k_2160p.mp4', metadata={'quality_label': '4K', 'quality': '2160p'})
        update_quality(item)
        self.assertEqual(item.qualityScore, 0)

    def test_invalid_declared_variant_does_not_use_root_resolution(self):
        item = candidate(resolution=(3840, 2160), metadata={'declared_resolution': (0, 0)})
        update_quality(item)
        self.assertEqual(item.qualityScore, 0)

    def test_combined_formula_is_exact_and_does_not_change_confidence(self):
        item = candidate(resolution=(1920, 1080), videoCodec='avc1', audioCodec='aac', confidenceScore=73)
        update_quality(item)
        self.assertEqual(item.qualityScore, 45)
        self.assertEqual(item.metadata['combined_score'], 56.2)
        self.assertEqual(item.confidenceScore, 73)

    def test_huge_low_resolution_file_has_bounded_penalty(self):
        item = candidate(resolution=(854, 480), videoCodec='h264', audioCodec='aac',
                         duration=10, estimatedSize=1_000_000_000)
        update_quality(item)
        self.assertEqual(item.qualityScore, 5)
        self.assertEqual(item.metadata['quality_details'][-1]['points'], -20)
        self.assertIn('参考上限 8 Mbps', item.metadata['quality_details'][-1]['reason'])

    def test_known_size_bitrate_penalty_threshold_and_clamp(self):
        for size, points in ((10_000_000, 10), (20_000_000, 5), (100_000_000, 0)):
            with self.subTest(size=size):
                item = candidate(resolution=(854, 480), duration=10, estimatedSize=size)
                update_quality(item)
                self.assertEqual(item.qualityScore, points)

    def test_unknown_size_or_duration_has_no_invented_penalty(self):
        for size, duration in ((None, 10), (1_000_000_000, None), (float('inf'), 10), (1_000_000_000, -1)):
            item = candidate(resolution=(854, 480), estimatedSize=size, duration=duration)
            update_quality(item)
            self.assertEqual(item.qualityScore, 10)

    def test_tiny_positive_duration_cannot_overflow_penalty(self):
        item = candidate(resolution=(854, 480), duration=5e-324, estimatedSize=2**63-1)
        update_quality(item)
        self.assertEqual(item.qualityScore, 0)
        self.assertEqual(item.metadata['quality_details'][-1]['points'], -20)

    def test_hls_versions_exclude_master_and_keep_existing_candidate_references(self):
        master = candidate('master', media_type='HLS', manifest_type='Master')
        low = candidate('low', media_type='HLS', manifest_type='Media Playlist', resolution=(1280, 720))
        high = candidate('high', media_type='HLS', manifest_type='Media Playlist', resolution=(1920, 1080))
        choices = quality_candidates(group(master, low, high))
        self.assertEqual(choices, [low, high])
        self.assertIs(choices[0], low)
        self.assertIs(choices[1], high)
        self.assertIs(recommend_candidate(choices), high)

    def test_hls_master_remains_when_no_available_media_rendition(self):
        master = candidate('master', media_type='HLS', manifest_type='Master')
        failed = candidate('failed', media_type='HLS', manifest_type='Media Playlist', http_status=403)
        self.assertIn(master, quality_candidates(group(master, failed)))
        self.assertIs(recommend_candidate(quality_candidates(group(master, failed))), master)

    def test_unconfirmed_media_rendition_not_in_choices(self):
        confirmed = candidate('confirmed')
        speculative = candidate('speculative', confirmed=False)
        self.assertEqual(quality_candidates(group(confirmed, speculative)), [confirmed])

    def test_quality_choices_never_pull_other_groups(self):
        one, two = candidate('one'), candidate('two', resolution=(3840, 2160))
        self.assertEqual(quality_candidates(group(one)), [one])
        self.assertEqual(quality_candidates(group(two)), [two])

    def test_dash_renditions_have_per_representation_details_and_stable_selectors(self):
        reps = [{'id': 'low', 'width': 1280, 'height': 720, 'bandwidth': 1_000_000,
                 'codecs': 'avc1.4d401f', 'frame_rate': '25', 'mime_type': 'video/mp4'},
                {'id': 'high', 'width': 3840, 'height': 2160, 'bandwidth': 8_000_000,
                 'codecs': 'hvc1.1.6.L150', 'frame_rate': '60', 'mime_type': 'video/mp4'},
                {'id': 'audio', 'bandwidth': 128_000, 'codecs': 'mp4a.40.2', 'mime_type': 'audio/mp4'}]
        root = candidate('mpd', media_type='DASH', manifest_type='MPD', playlist_type='VOD', duration=20,
                         width=3840, height=2160, fps=60, videoCodec='hvc1', videoBitrate=8_000_000,
                         estimatedSize=100_000_000, resolution=(3840, 2160), bandwidth=8_000_000,
                         metadata={'representations': reps, 'codecs': 'hvc1', 'frame_rate': 60,
                                   'declared_resolution': (3840, 2160), 'filesize': 100_000_000})
        before = deepcopy(root)
        choices = quality_candidates(group(root))
        self.assertEqual([value.id for value in choices], ['mpd:dash:low', 'mpd:dash:high'])
        low, high = choices
        self.assertEqual((low.width, low.height, low.fps, low.videoCodec), (1280, 720, 25, 'avc1.4d401f'))
        self.assertEqual((high.width, high.height, high.fps, high.videoCodec), (3840, 2160, 60, 'hvc1.1.6.L150'))
        self.assertEqual((low.videoBitrate, low.audioBitrate), (1_000_000, 128_000))
        self.assertEqual((low.estimatedSize, high.estimatedSize), (2_820_000, 20_320_000))
        self.assertEqual((low.qualityScore, high.qualityScore), (35, 60))
        self.assertIn('[width=1280][height=720][vcodec^=avc1]', low.metadata['format_selector'])
        self.assertIn('[width=3840][height=2160][vcodec^=hvc1]', high.metadata['format_selector'])
        self.assertEqual(root, before)
        for value in choices:
            self.assertIsNot(value, root)
            self.assertIsNot(value.request_context, root.request_context)
            self.assertEqual(value.request_context, root.request_context)
            self.assertEqual(value.download_payload(), root.download_payload())
            self.assertEqual(value.confidenceScore, root.confidenceScore)
        self.assertEqual([item.id for item in quality_candidates(group(root))], [value.id for value in choices])

    def test_dash_invalid_or_missing_representations_are_safe(self):
        for reps in (None, [], {}, [None, {}], [{'height': -1}], 'invalid', [None] * 129):
            with self.subTest(reps=reps):
                root = candidate('dash', media_type='DASH', metadata={'representations': reps})
                self.assertEqual(quality_candidates(group(root)), [root])

    def test_dash_codec_selector_cannot_contain_injected_filters(self):
        root = candidate('dash', media_type='DASH', metadata={'representations': [
            {'id': 'v', 'width': 1280, 'height': 720, 'codecs': 'avc1][height=9999]'}]})
        value = quality_candidates(group(root))[0]
        self.assertIn('[height=720]', value.metadata['format_selector'])
        self.assertNotIn('9999', value.metadata['format_selector'])

    def test_dash_selectors_execute_with_existing_local_ytdlp(self):
        tool = Path(__file__).resolve().parents[1] / 'tools' / 'yt-dlp.exe'
        if os.name != 'nt' or not tool.is_file():
            self.skipTest('项目本地 Windows yt-dlp 工具不可用')
        reps = [{'id': 'low', 'width': 1280, 'height': 720, 'codecs': 'avc1.4d401f'},
                {'id': 'high', 'width': 3840, 'height': 2160, 'codecs': 'hvc1.1.6.L150'}]
        root = candidate('dash', media_type='DASH', metadata={'representations': reps})
        choices = quality_candidates(group(root))
        formats = [
            {'format_id': 'low-avc', 'width': 1280, 'height': 720, 'vcodec': 'avc1.4d401f', 'acodec': 'none', 'ext': 'mp4'},
            {'format_id': 'low-hevc', 'width': 1280, 'height': 720, 'vcodec': 'hvc1.1.6.L120', 'acodec': 'none', 'ext': 'mp4'},
            {'format_id': 'high-hevc', 'width': 3840, 'height': 2160, 'vcodec': 'hvc1.1.6.L150', 'acodec': 'none', 'ext': 'mp4'},
            {'format_id': 'audio', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'ext': 'm4a'}]
        for value in formats:
            value.update(url='http://127.0.0.1:9/' + value['format_id'], protocol='http')
        info = {'id': 'local-protocol-fixture', 'title': 'Local format selector test',
                'extractor': 'generic', 'extractor_key': 'Generic', 'formats': formats,
                'webpage_url': 'http://127.0.0.1:9/not-requested'}
        with tempfile.TemporaryDirectory(prefix='mediaflow-selector-') as temporary:
            info_path = Path(temporary) / 'formats.json'
            info_path.write_text(json.dumps(info), encoding='utf-8')
            for value, expected in zip(choices, ('low-avc+audio', 'high-hevc+audio')):
                result = subprocess.run([str(tool), '--ignore-config', '--no-check-formats',
                    '--skip-download', '--no-warnings', '--load-info-json', str(info_path),
                    '-f', value.metadata['format_selector'], '--print', '%(format_id)s'],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                    timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_reliability_thresholds_precede_all_quality_preferences(self):
        for preference, _ in QUALITY_PREFERENCES:
            low = candidate('low', confidenceScore=39, resolution=(3840, 2160))
            confirm = candidate('confirm', confidenceScore=69, resolution=(3840, 2160), estimatedSize=10)
            reliable = candidate('reliable', confidenceScore=70, resolution=(1280, 720), estimatedSize=100)
            self.assertIs(recommend_candidate([low, confirm, reliable], preference), reliable)
            self.assertIs(recommend_candidate([low, confirm], preference), confirm)
            self.assertIsNone(recommend_candidate([low], preference))
        boundary = candidate(confidenceScore=40)
        self.assertIs(recommend_candidate([boundary]), boundary)
        self.assertEqual(boundary.confidenceScore, 40)  # UI still requires confirmation.

    def test_ad_preview_image_drm_and_unavailable_are_excluded(self):
        valid = candidate('valid', confidenceScore=70)
        invalid = [candidate('ad', is_ad=True, confidenceScore=100),
                   candidate('preview', metadata={'is_preview': True}, confidenceScore=100),
                   candidate('thumbnail', metadata={'resource_role': 'thumbnail'}, confidenceScore=100),
                   candidate('picture', content_type='image/png', confidenceScore=100),
                   candidate('protected', drm=True, confidenceScore=100),
                   candidate('failed', http_status=403, confidenceScore=100),
                   candidate('context', context_complete=False, confidenceScore=100),
                   candidate('notconfirmed', confirmed=False, confidenceScore=100),
                   candidate('error', metadata={'error_code': 'EXPIRED_URL'}, confidenceScore=100)]
        for preference, _ in QUALITY_PREFERENCES:
            self.assertIs(recommend_candidate([*invalid, valid], preference), valid)
            self.assertIsNone(recommend_candidate(invalid, preference))

    def test_correct_current_1080_beats_unassociated_visible_4k_preload(self):
        current = candidate('current', resolution=(1920, 1080), confidenceScore=100,
                            estimatedSize=20_000_000, metadata={'confidence_evidence': {'current_player': True}})
        preload = candidate('preloaded', resolution=(3840, 2160), videoCodec='av01', audioCodec='aac',
                            confidenceScore=100, estimatedSize=1,
                            metadata={'confidence_evidence': {'current_player': False, 'associated': True}})
        for preference, _ in QUALITY_PREFERENCES:
            self.assertIs(recommend_candidate([preload, current], preference), current)

    def test_inherited_current_hls_evidence_keeps_higher_real_rendition(self):
        evidence = {'confidence_details': [{'points': 40, 'reason': '当前主播放器 video.currentSrc'}]}
        low = candidate('low', media_type='HLS', resolution=(1280, 720), metadata=deepcopy(evidence))
        high = candidate('high', media_type='HLS', resolution=(1920, 1080), metadata=deepcopy(evidence))
        self.assertIs(recommend_candidate([low, high]), high)

    def test_auto_uses_combined_formula_within_reliable_same_video(self):
        fullhd = candidate('fullhd', resolution=(1920, 1080), videoCodec='avc1', audioCodec='aac', confidenceScore=100)
        fourk = candidate('fourk', resolution=(3840, 2160), videoCodec='av01', audioCodec='aac', confidenceScore=80)
        self.assertIs(recommend_candidate([fullhd, fourk]), fourk)
        self.assertEqual((fullhd.metadata['combined_score'], fourk.metadata['combined_score']), (67, 68))

    def test_highest_uses_actual_resolution_before_codec_score(self):
        fullhd = candidate('fullhd', resolution=(1920, 1080), videoCodec='av01', audioCodec='aac', confidenceScore=100)
        fourk = candidate('fourk', resolution=(3840, 2160), confidenceScore=70)
        self.assertIs(recommend_candidate([fullhd, fourk], 'highest'), fourk)

    def test_balanced_prefers_fullhd_and_hd_over_4k(self):
        hd = candidate('hd', resolution=(1280, 720))
        fullhd = candidate('fullhd', resolution=(1920, 1080))
        fourk = candidate('fourk', resolution=(3840, 2160), videoCodec='av01', audioCodec='aac')
        self.assertIs(recommend_candidate([hd, fullhd, fourk], 'balanced'), fullhd)
        self.assertIs(recommend_candidate([hd, fourk], 'balanced'), hd)

    def test_smallest_uses_known_size_and_unknown_is_not_zero(self):
        unknown = candidate('unknown', resolution=(640, 360))
        large = candidate('large', estimatedSize=20_000_000)
        small = candidate('small', estimatedSize=10_000_000)
        self.assertIs(recommend_candidate([unknown, large, small], 'smallest'), small)

    def test_smallest_unknown_size_falls_back_to_bitrate_then_resolution(self):
        high = candidate('high', resolution=(1920, 1080), bandwidth=4_000_000)
        low = candidate('low', resolution=(1280, 720), bandwidth=1_000_000)
        unknown = candidate('unknown', resolution=(640, 360))
        self.assertIs(recommend_candidate([unknown, high, low], 'smallest'), low)
        self.assertIs(recommend_candidate([high, unknown], 'smallest'), high)
        high.bandwidth = 0
        self.assertIs(recommend_candidate([high, unknown], 'smallest'), unknown)

    def test_deterministic_ties_preserve_input_order(self):
        first, second = candidate('first'), candidate('second')
        for preference, _ in QUALITY_PREFERENCES:
            self.assertIs(recommend_candidate([first, second], preference), first)
            self.assertIs(recommend_candidate([second, first], preference), second)

    def test_empty_inputs_and_unknown_preference(self):
        self.assertIsNone(recommend_candidate([]))
        self.assertEqual(quality_candidates(group()), [])
        value = candidate()
        self.assertIs(recommend_candidate([value], 'unknown'), value)

    def test_malformed_metadata_and_optional_values_are_safe(self):
        for metadata in (None, [], {'confidence_evidence': [], 'codecs': {}, 'has_audio': 'true'},
                         {'representations': [None], 'quality_details': {}, 'estimated_size': float('inf')}):
            item = candidate(metadata=metadata, width=float('nan'), height=True, fps=float('inf'),
                             videoCodec=[], audioCodec={}, videoBitrate=-1, estimatedSize=float('inf'))
            update_quality(item)
            self.assertEqual(item.qualityScore, 0)
            self.assertIs(recommend_candidate([item]), item)

    def test_request_payload_confidence_and_old_media_fields_are_untouched(self):
        context = RequestContext('https://cdn.example/index.png?sign=fixture&t=123',
                                 headers={'Referer': 'https://page.example/watch', 'User-Agent': 'Fixture',
                                          'Range': 'bytes=0-'})
        item = candidate(request_context=context, media_type='HLS', resolution=(1920, 1080),
                         duration=20, bandwidth=2_000_000, score=90, confidenceScore=90,
                         confidenceReasons=['当前播放器'], manifest_text='#EXTM3U\n',
                         manifest_origin='embedded:png/roUd', validated=True,
                         videoCodec='avc1', audioCodec='aac')
        old = deepcopy((asdict(context), item.download_payload(), item.score, item.confidenceScore,
                        item.confidenceReasons, item.resolution, item.duration, item.bandwidth))
        update_quality(item)
        recommend_candidate([item])
        self.assertEqual((asdict(context), item.download_payload(), item.score, item.confidenceScore,
                          item.confidenceReasons, item.resolution, item.duration, item.bandwidth), old)

    def test_verified_rou_decoded_png_transport_is_hls_not_image(self):
        body = playlist(2, 19, 'a')
        observation = rou_observation(png(zlib.compress(body), 1))
        result = RouVideoResolver(legacy_fallback=False).resolve(rou_context(observation))
        self.assertTrue(result.success, result.diagnostics)
        service = MediaDiscoveryService()
        service.consume_resolver(result.candidates[0])
        actual = service.ranked()[0]
        context, payload = deepcopy(actual.request_context), deepcopy(actual.download_payload())
        choices = quality_candidates(group(actual))
        self.assertEqual(actual.confidenceScore, 70)
        self.assertEqual(actual.media_type, 'HLS')
        self.assertTrue(actual.transport_url.partition('?')[0].endswith('.png'))
        self.assertTrue(actual.manifest_text.startswith('#EXTM3U'))
        unassociated = candidate('other', resolution=(3840, 2160), confidenceScore=100)
        for preference, _ in QUALITY_PREFERENCES:
            self.assertIs(recommend_candidate([unassociated, *choices], preference), actual)
        self.assertIsNone(actual.container)
        self.assertEqual(actual.request_context, context)
        self.assertEqual(actual.download_payload(), payload)

    def test_douyin_normal_video_uses_existing_generic_discovery_data(self):
        page = 'https://www.douyin.com/video/1234567890123456789'
        url = 'https://v26-web.douyinvod.com/opaque?sign=fixture&t=173'
        context = RequestContext(url, headers={'Referer': page, 'User-Agent': 'Fixture', 'Range': 'bytes=0-'})
        service = MediaDiscoveryService()
        service.consume_network(NetworkObservation('douyin-data', context, 206, 'video/mp4',
            frame_id='main', content_length=2_655_560,
            response_headers={'Content-Range': 'bytes 0-2655559/2655560', 'Content-Length': '2655560'}))
        service.consume_dom({'page_url': page, 'frame_id': 'main', 'page_title': 'Fixture', 'videos': [{
            'src': url, 'current_src': url, 'width': 1280, 'height': 720, 'duration': 17.368,
            'visible': True, 'is_main': True, 'paused': True, 'ready_state': 4}]})
        actual = service.ranked()[0]
        update_quality(actual)
        self.assertEqual((actual.confidenceScore, actual.qualityScore), (100, 20))
        self.assertEqual(actual.estimatedSize, 2_655_560)
        self.assertFalse(actual.metadata['size_is_estimate'])
        self.assertIsNone(actual.audioCodec)  # MP4 response alone does not prove an audio track.
        choices = quality_candidates(group_media([actual])[0])
        self.assertIs(recommend_candidate(choices), choices[0])
        self.assertEqual(actual.request_context, context)


if __name__ == '__main__':
    unittest.main()
