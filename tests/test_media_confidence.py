"""媒体真实性纯评分：每个权重、边界与真实场景证据的回归。"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest

from core.discovery_models import MediaCandidate
from core.media_confidence import ConfidenceRating, confidence_tier, score_candidate
from core.request_context import RequestContext


def candidate(**fields):
    values = dict(id='candidate', request_context=RequestContext('https://arbitrary.example/media'),
                  media_type='Direct')
    values.update(fields)
    return MediaCandidate(**values)


class MediaConfidenceTests(unittest.TestCase):
    def test_empty_candidate_is_unreliable(self):
        rating = score_candidate(candidate())
        self.assertEqual(rating, ConfidenceRating(0, [], []))
        self.assertEqual(confidence_tier(rating.score), 'unreliable')

    def test_current_main_player_adds_40(self):
        rating = score_candidate(candidate(), {'current_player': True})
        self.assertEqual(rating.score, 40)
        self.assertEqual(rating.details[0]['points'], 40)
        self.assertIn('currentSrc', rating.reasons[0])

    def test_visible_associated_player_adds_20(self):
        self.assertEqual(score_candidate(candidate(), {'visible': True}).score, 20)

    def test_positive_duration_adds_20(self):
        self.assertEqual(score_candidate(candidate(duration=5)).score, 20)

    def test_duration_above_5_adds_additional_10(self):
        rating = score_candidate(candidate(duration=5.0001))
        self.assertEqual(rating.score, 30)
        self.assertEqual([item['points'] for item in rating.details], [20, 10])

    def test_video_content_type_adds_10(self):
        for mime in ('video/mp4', 'Video/WEBM; codecs=vp9', ' video/mp2t '):
            with self.subTest(mime=mime):
                self.assertEqual(score_candidate(candidate(content_type=mime)).score, 10)

    def test_non_video_content_type_does_not_add_10(self):
        for mime in ('application/octet-stream', 'application/vnd.apple.mpegurl', 'audio/mp4',
                     'text/html', '', None, 123):
            with self.subTest(mime=mime):
                self.assertEqual(score_candidate(candidate(content_type=mime)).score, 0)

    def test_plausible_landscape_portrait_dimensions_add_10(self):
        for size in ((1280, 720), (1080, 1920), (64, 64), (16384, 16384), [854, 480],
                     (1920.0, 1080.0)):
            with self.subTest(size=size):
                self.assertEqual(score_candidate(candidate(resolution=size)).score, 10)

    def test_portrait_reason_uses_short_side_not_fake_1920p(self):
        rating = score_candidate(candidate(resolution=(1080, 1920)))
        self.assertIn('1080P', rating.reasons[0])
        self.assertNotIn('1920P', rating.reasons[0])

    def test_invalid_dimensions_are_ignored(self):
        for size in ((0, 720), (1280, 0), (-1, 720), (63, 64), (16385, 720),
                     (float('inf'), 720), (1280, float('nan')), (True, 720),
                     ('1920', '1080'), (1280.5, 720), '1920x1080', None, (), (1,),
                     (1280, 720, 32), {'width': 1920, 'height': 1080}):
            with self.subTest(size=size):
                self.assertEqual(score_candidate(candidate(resolution=size)).score, 0)

    def test_hidden_player_subtracts_30(self):
        rating = score_candidate(candidate(), {'current_player': True, 'hidden': True})
        self.assertEqual(rating.score, 10)
        self.assertEqual([item['points'] for item in rating.details], [40, -30])

    def test_short_duration_subtracts_30_after_valid_duration(self):
        rating = score_candidate(candidate(duration=2.999), {'current_player': True})
        self.assertEqual(rating.score, 30)
        self.assertEqual([item['points'] for item in rating.details], [40, 20, -30])

    def test_duration_thresholds(self):
        for duration, expected in ((0, 0), (0.001, 0), (2.999, 0), (3, 20),
                                   (5, 20), (5.001, 30), (19, 30)):
            with self.subTest(duration=duration):
                self.assertEqual(score_candidate(candidate(duration=duration)).score, expected)

    def test_invalid_durations_are_not_positive_or_short_evidence(self):
        for duration in (None, -1, float('nan'), float('inf'), -float('inf'), True,
                         False, '19', {}, [], complex(1, 2)):
            with self.subTest(duration=duration):
                rating = score_candidate(candidate(duration=duration))
                self.assertEqual(rating.score, 0)
                self.assertEqual(rating.details, [])

    def test_thumbnail_subtracts_50(self):
        rating = score_candidate(candidate(duration=19), {'current_player': True, 'thumbnail': True})
        self.assertEqual(rating.score, 20)
        self.assertEqual(rating.details[-1]['points'], -50)

    def test_preview_subtracts_50(self):
        rating = score_candidate(candidate(duration=19), {'current_player': True, 'preview': True})
        self.assertEqual(rating.score, 20)
        self.assertEqual(rating.details[-1]['points'], -50)

    def test_thumbnail_and_preview_penalties_are_independent(self):
        rating = score_candidate(candidate(duration=19, resolution=(1920, 1080), content_type='video/mp4'),
                                 {'current_player': True, 'visible': True, 'thumbnail': True, 'preview': True})
        self.assertEqual(rating.score, 10)
        self.assertEqual([item['points'] for item in rating.details[-2:]], [-50, -50])

    def test_image_response_subtracts_80(self):
        rating = score_candidate(candidate(duration=19, content_type='image/png'),
                                 {'current_player': True, 'visible': True})
        self.assertEqual(rating.score, 10)
        self.assertEqual(rating.details[-1]['points'], -80)

    def test_explicit_image_evidence_is_respected(self):
        rating = score_candidate(candidate(duration=19, media_type='HLS'),
                                 {'current_player': True, 'visible': True, 'image': True})
        self.assertEqual(rating.score, 10)
        self.assertEqual(rating.details[-1]['points'], -80)

    def test_sum_then_clamp_not_clamp_each_step(self):
        # Positive total 110 minus hidden 30 = 80; early clamp would incorrectly yield 70.
        rating = score_candidate(candidate(duration=19, content_type='video/mp4', resolution=(1920, 1080)),
                                 {'current_player': True, 'visible': True, 'hidden': True})
        self.assertEqual(sum(item['points'] for item in rating.details), 80)
        self.assertEqual(rating.score, 80)

    def test_upper_and_lower_clamps(self):
        rating = score_candidate(candidate(duration=19, content_type='video/mp4', resolution=(1920, 1080)),
                                 {'current_player': True, 'visible': True})
        self.assertEqual(sum(item['points'] for item in rating.details), 110)
        self.assertEqual(rating.score, 100)
        penalty = score_candidate(candidate(), {'hidden': True, 'thumbnail': True, 'image': True})
        self.assertEqual(penalty.score, 0)
        self.assertLess(sum(item['points'] for item in penalty.details), 0)

    def test_metadata_current_requires_main_player(self):
        for metadata, expected in (({'dom_current': True}, 0), ({'dom_main': True}, 0),
                                   ({'dom_current': True, 'dom_main': True}, 40),
                                   ({'dom_current': True, 'dom_main': False}, 0)):
            with self.subTest(metadata=metadata):
                self.assertEqual(score_candidate(candidate(metadata=metadata)).score, expected)

    def test_visibility_metadata_requires_association(self):
        for metadata, expected in (({'dom_visible': True}, 0),
                                   ({'dom_visible': False}, 0),
                                   ({'dom_associated': True, 'dom_visible': True}, 20),
                                   ({'dom_associated': True, 'dom_visible': None}, 0)):
            with self.subTest(metadata=metadata):
                self.assertEqual(score_candidate(candidate(metadata=metadata)).score, expected)

    def test_hidden_metadata_requires_association_and_explicit_state(self):
        for metadata, expected in (({'dom_current': True, 'dom_main': True}, 40),
                                   ({'dom_current': True, 'dom_main': True, 'dom_associated': True}, 40),
                                   ({'dom_current': True, 'dom_main': True, 'dom_visible': False}, 40),
                                   ({'dom_current': True, 'dom_main': True, 'dom_associated': True,
                                     'dom_visible': False}, 10)):
            with self.subTest(metadata=metadata):
                self.assertEqual(score_candidate(candidate(metadata=metadata)).score, expected)

    def test_fresh_false_evidence_overrides_stale_true_metadata(self):
        value = candidate(metadata={'dom_current': True, 'dom_main': True, 'dom_associated': True,
                                    'dom_visible': True, 'resolver_verified': True, 'is_preview': True,
                                    'is_thumbnail': True})
        rating = score_candidate(value, {'current_player': False, 'visible': None, 'hidden': False,
                                        'resolver_entry': False, 'preview': False, 'thumbnail': False})
        self.assertEqual(rating, ConfidenceRating(0, [], []))

    def test_booleans_are_not_inferred_from_truthy_strings_or_numbers(self):
        for value in ('true', 'false', 'yes', '1', 1, [], {}):
            with self.subTest(value=value):
                rating = score_candidate(candidate(), dict.fromkeys(
                    ('current_player', 'visible', 'hidden', 'resolver_entry', 'thumbnail', 'preview', 'image'), value))
                self.assertEqual(rating, ConfidenceRating(0, [], []))

    def test_video_poster_or_thumbnail_url_is_not_thumbnail_resource(self):
        value = candidate(duration=19, metadata={'poster': 'https://image.example/poster.png',
                                               'thumbnail': 'https://image.example/thumb.jpg'})
        rating = score_candidate(value, {'current_player': True})
        self.assertEqual(rating.score, 70)
        self.assertFalse(any(item['points'] < 0 for item in rating.details))

    def test_explicit_thumbnail_resource_role_is_penalized(self):
        for role in ('Thumbnail', 'poster', 'SPRITE', 'thumbnail_track', 'Thumbnail Track'):
            with self.subTest(role=role):
                rating = score_candidate(candidate(duration=19, metadata={'resource_role': role}),
                                         {'current_player': True})
                self.assertEqual(rating.score, 20)

    def test_explicit_preview_metadata_and_role_is_penalized(self):
        for metadata in ({'preview': True}, {'is_preview': True}, {'resource_role': 'preview'},
                         {'role': 'Trailer'}, {'resource_kind': 'sample'}):
            with self.subTest(metadata=metadata):
                self.assertEqual(score_candidate(candidate(duration=19, metadata=metadata),
                                                 {'current_player': True}).score, 20)

    def test_url_suffix_does_not_override_hls_segment_or_direct_media_semantics(self):
        for media_type in ('HLS', 'DASH', 'Direct'):
            for suffix in ('png', 'jpg', 'ts', 'bin', ''):
                with self.subTest(media_type=media_type, suffix=suffix):
                    value = candidate(media_type=media_type, duration=19)
                    value.request_context = RequestContext('https://arbitrary.example/abc000.' + suffix)
                    rating = score_candidate(value, {'current_player': True})
                    self.assertEqual(rating.score, 70)
                    self.assertFalse(any(item['points'] < 0 for item in rating.details))

    def test_decoded_hls_png_transport_is_not_an_image(self):
        value = candidate(media_type='HLS', duration=19, content_type='image/png',
                          metadata={'resolver_verified': True}, manifest_text='#EXTM3U\n#EXTINF:19,\nabc000.png\n')
        self.assertEqual(score_candidate(value).score, 70)

    def test_verified_manifest_entry_adds_current_equivalent_without_fixed_100(self):
        value = candidate(media_type='HLS', duration=19, metadata={'resolver_verified': True,
                          'resolver_confidence': 100}, score=100)
        rating = score_candidate(value)
        self.assertEqual(rating.score, 70)
        self.assertIn('入口已验证', rating.reasons[0])
        self.assertEqual(confidence_tier(rating.score), 'auto')

    def test_current_and_verified_entry_do_not_double_count(self):
        rating = score_candidate(candidate(metadata={'resolver_verified': True}), {'current_player': True})
        self.assertEqual(rating.score, 40)
        self.assertEqual(len(rating.details), 1)

    def test_unverified_resolver_confidence_is_not_media_authenticity(self):
        value = candidate(score=100, metadata={'resolver_confidence': 100, 'resolver_verified': False})
        self.assertEqual(score_candidate(value).score, 0)

    def test_douyin_reported_samples_use_browser_evidence_without_cdn_rules(self):
        for duration, size, url in ((17.368, (1280, 720), 'https://v26-web.douyinvod.com/media'),
                                    (88.53, (1080, 1920), 'https://new-cdn.example/media?token=secret'),
                                    (10.35, (720, 1280), 'https://different.example/opaque')):
            with self.subTest(duration=duration):
                value = candidate(duration=duration, resolution=size, content_type='video/mp4', http_status=206)
                value.request_context = RequestContext(url)
                self.assertEqual(score_candidate(value, {'current_player': True, 'visible': True}).score, 100)

    def test_paused_current_player_has_same_rating(self):
        value = candidate(duration=19, content_type='video/mp4', resolution=(1280, 720),
                          metadata={'dom_current': True, 'dom_main': True, 'dom_visible': True,
                                    'dom_associated': True, 'dom_playing': False})
        playing = replace(value, metadata={**value.metadata, 'dom_playing': True})
        self.assertEqual(score_candidate(value), score_candidate(playing))
        self.assertEqual(score_candidate(value).score, 100)

    def test_current_video_outranks_visible_recommendation_and_hidden_preload(self):
        fields = dict(duration=19, content_type='video/mp4', resolution=(1280, 720))
        main = score_candidate(candidate(**fields), {'current_player': True, 'visible': True})
        recommendation = score_candidate(candidate(**fields), {'current_player': False, 'visible': True})
        preload = score_candidate(candidate(**fields), {'current_player': False, 'visible': False, 'hidden': True})
        self.assertEqual((main.score, recommendation.score, preload.score), (100, 70, 20))
        self.assertGreater(main.score, recommendation.score)
        self.assertEqual(confidence_tier(preload.score), 'unreliable')

    def test_thresholds_40_and_70_are_unambiguous(self):
        for score, tier in ((-1, 'unreliable'), (0, 'unreliable'), (39, 'unreliable'),
                            (39.999, 'unreliable'), (40, 'confirm'), (69.999, 'confirm'),
                            (70, 'auto'), (100, 'auto'), (101, 'auto')):
            with self.subTest(score=score):
                self.assertEqual(confidence_tier(score), tier)

    def test_tier_rejects_non_finite_or_non_numeric_values(self):
        for score in (float('nan'), float('inf'), -float('inf'), '70', True, None, {}, []):
            with self.subTest(score=score):
                self.assertEqual(confidence_tier(score), 'unreliable')

    def test_scoring_does_not_mutate_candidate_or_evidence(self):
        value = candidate(duration=19, metadata={'dom_current': True, 'dom_main': True})
        evidence = {'visible': True}
        old_value, old_evidence = deepcopy(value), deepcopy(evidence)
        score_candidate(value, evidence)
        self.assertEqual(value, old_value)
        self.assertEqual(evidence, old_evidence)

    def test_high_score_does_not_grant_download_or_drm_authorization(self):
        value = candidate(duration=19, content_type='video/mp4', resolution=(1280, 720), drm=True,
                          confirmed=False, context_complete=False, is_ad=True)
        self.assertEqual(score_candidate(value, {'current_player': True, 'visible': True}).score, 100)
        self.assertFalse(value.downloadable)
        self.assertTrue(value.is_ad)
        self.assertTrue(value.drm)

    def test_score_reasons_contain_no_urls_or_credentials(self):
        value = candidate(duration=19, content_type='video/mp4', resolution=(1280, 720),
                          metadata={'dom_current': True, 'dom_main': True, 'cookie': 'secret-cookie',
                                    'authorization': 'Bearer secret-token', 'title': 'private-title'})
        value.request_context = RequestContext('https://arbitrary.example/media?signature=secret-signature',
                                               headers={'Cookie': 'secret-cookie'})
        rating = score_candidate(value)
        for secret in ('secret-cookie', 'secret-token', 'private-title', 'secret-signature', 'https://'):
            self.assertNotIn(secret, repr(rating))

    def test_malformed_mime_does_not_copy_url_or_header_text_into_reasons(self):
        for mime in ('video/https://private.example?signature=secret',
                     'video/mp4\nCookie: private-cookie', 'video/' + 'a' * 129):
            with self.subTest(mime=mime):
                rating = score_candidate(candidate(content_type=mime))
                self.assertEqual(rating, ConfidenceRating(0, [], []))

    def test_malformed_metadata_and_evidence_do_not_crash(self):
        for metadata in (None, '', [], 1):
            with self.subTest(metadata=metadata):
                self.assertEqual(score_candidate(candidate(metadata=metadata), evidence=[]).score, 0)

    def test_supports_minimal_candidate_objects_and_mappings(self):
        self.assertEqual(score_candidate(SimpleNamespace(duration=19)).score, 30)
        self.assertEqual(score_candidate({'duration': 19, 'metadata': {'resolver_verified': True}}).score, 70)

    def test_extreme_integer_is_handled_without_overflow(self):
        rating = score_candidate(candidate(duration=10 ** 400))
        self.assertEqual(rating.score, 30)
        self.assertEqual(confidence_tier(10 ** 400), 'auto')


if __name__ == '__main__':
    unittest.main()
