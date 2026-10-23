from copy import deepcopy
from dataclasses import replace
import unittest

from core.discovery_models import MediaCandidate, NetworkObservation
from core.media_discovery import MediaDiscoveryService
from core.media_groups import available_candidate, deduplicate_candidates, group_media, ordered_candidates, quality_key
from core.request_context import RequestContext


PAGE = 'https://page.example/watch'


def candidate(name='a', *, url=None, height=720, duration=120, metadata=None, **fields):
    values = dict(id=name, request_context=RequestContext(url or f'https://cdn.example/{name}.mp4'),
                  media_type='Direct', resolution=(height * 16 // 9, height), duration=duration,
                  score=80, confirmed=True, http_status=200, metadata=metadata or {})
    values.update(fields)
    return MediaCandidate(**values)


class MediaGroupTests(unittest.TestCase):
    def test_scenario01_three_independent_videos_are_three_selected_groups(self):
        groups = group_media([candidate(name, metadata={'video_id': name, 'title': f'Video {name}', 'page_url': PAGE})
                              for name in ('A', 'B', 'C')])
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(group.selected for group in groups))
        self.assertEqual(len({group.id for group in groups}), 3)

    def test_scenario02_three_qualities_default_to_one_1080p_video(self):
        values = [candidate(str(height), height=height, metadata={'video_id': 'movie', 'page_url': PAGE})
                  for height in (480, 1080, 720)]
        groups = group_media(values)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].selected_candidate.resolution[1], 1080)
        self.assertEqual(len(groups[0].candidates), 3)

    def test_scenario03_same_titles_do_not_merge_independent_videos(self):
        groups = group_media([candidate(name, metadata={'title': 'Video'}) for name in ('a', 'b', 'c')],
                             page_title='Same Page', source_page=PAGE)
        self.assertEqual(len(groups), 3)
        self.assertEqual([group.title for group in groups], ['Video'] * 3)

    def test_scenario06_vast_ad_plus_five_movies_only_selects_movies(self):
        values = [candidate('advert', url='https://ad-provider.example/preroll.mp4',
                            metadata={'vast_ad': True}, source_type='VAST')]
        values += [candidate(str(index), metadata={'video_id': str(index), 'page_url': PAGE}) for index in range(5)]
        groups = group_media(values)
        self.assertEqual(len(groups), 6)
        self.assertEqual(sum(group.selected for group in groups), 5)
        ad = next(group for group in groups if group.is_ad)
        self.assertFalse(ad.selected)
        self.assertEqual(ordered_candidates(ad), [])

    def test_scenario07_signed_direct_urls_use_latest_valid_context(self):
        old = candidate('old', url='https://cdn.example/video.mp4?token=A&quality=1080', height=1080,
                        metadata={'video_id': 'video', 'page_url': PAGE, 'discovered_at': 100})
        new = candidate('new', url='https://cdn.example/video.mp4?token=B&quality=1080', height=1080,
                        metadata={'video_id': 'video', 'page_url': PAGE, 'discovered_at': 200})
        new.request_context = replace(new.request_context, headers={'Cookie': 'fresh=secret', 'Referer': PAGE})
        groups = group_media([old, new])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].candidates), 1)
        self.assertEqual(groups[0].selected_candidate.url, new.url)
        self.assertEqual(groups[0].selected_candidate.request_context.cookies, 'fresh=secret')
        self.assertEqual(old.url, 'https://cdn.example/video.mp4?token=A&quality=1080')

    def test_scenario10_all_qualities_are_retained_in_descending_order(self):
        group = group_media([candidate(str(height), height=height, metadata={'video_id': 'movie', 'page_url': PAGE})
                             for height in (480, 1080, 720)])[0]
        self.assertEqual([quality_key(value)[0] for value in ordered_candidates(group)], [1080, 720, 480])
        self.assertEqual(sum(item.selected for item in [group]), 1)

    def test_actual_master_parent_ids_group_all_variants(self):
        data = (b'#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080,BANDWIDTH=4000000\nhi/index.m3u8\n'
                b'#EXT-X-STREAM-INF:RESOLUTION=1280x720,BANDWIDTH=2000000\nmid/index.m3u8\n'
                b'#EXT-X-STREAM-INF:RESOLUTION=854x480,BANDWIDTH=1000000\nlo/index.m3u8\n')
        service = MediaDiscoveryService()
        service.consume_network(NetworkObservation('master', RequestContext('https://cdn.example/master.m3u8'),
                                                   200, 'application/x-mpegURL', data, body_complete=True))
        values = list(service.candidates.values())
        groups = group_media(values, source_page=PAGE)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].candidates), 4)
        # 未观察到播放器或时长的清单仍分组，但不自动下载。
        self.assertIsNotNone(groups[0].selected_candidate)
        self.assertTrue(available_candidate(groups[0].selected_candidate))
        self.assertFalse(groups[0].selected)

    def test_explicit_master_children_group_without_parent_metadata(self):
        child = candidate('child', media_type='HLS', manifest_type='Media')
        master = candidate('master', media_type='HLS', manifest_type='Master',
                           metadata={'manifest': {'children': [child.final_url]}})
        self.assertEqual(len(group_media([child, master])), 1)

    def test_confirmed_variant_is_preferred_over_same_quality_master(self):
        master = candidate('master', height=1080, media_type='HLS', manifest_type='Master')
        variant = candidate('variant', height=1080, media_type='HLS', manifest_type='Media',
                            score=30, confidenceScore=master.confidence, metadata={'parent_manifest': 'master'})
        self.assertEqual(group_media([master, variant])[0].selected_candidate.id, 'variant')

    def test_dash_representations_share_one_mpd(self):
        values = [candidate(str(height), height=height, media_type='DASH',
                            metadata={'mpd_url': 'https://cdn.example/video.mpd', 'representation_id': str(height)})
                  for height in (480, 720, 1080)]
        group = group_media(values)[0]
        self.assertEqual(len(group_media(values)), 1)
        self.assertEqual(len(group.candidates), 3)
        self.assertEqual(group.selected_candidate.resolution[1], 1080)

    def test_dom_video_id_scoped_to_page_and_frame(self):
        first = candidate('a', metadata={'dom_video_id': 'player', 'page_url': PAGE}, frame_id='main')
        same = candidate('b', metadata={'dom_video_id': 'player', 'page_url': PAGE}, frame_id='main')
        other_frame = candidate('c', metadata={'dom_video_id': 'player', 'page_url': PAGE}, frame_id='iframe')
        self.assertEqual(len(group_media([first, same, other_frame])), 2)

    def test_video_id_scoped_to_site_and_resolver(self):
        values = [candidate('a', metadata={'video_id': '123', 'page_url': 'https://a.example/watch'}),
                  candidate('b', metadata={'video_id': '123', 'page_url': 'https://b.example/watch'}),
                  candidate('c', metadata={'video_id': '123', 'page_url': 'https://a.example/watch', 'resolver_id': 'other'})]
        self.assertEqual(len(group_media(values)), 3)

    def test_strong_video_id_conflict_overrides_weak_filename_evidence(self):
        values = [candidate(str(height), url=f'https://cdn.example/movie-{height}p.mp4', height=height,
                            metadata={'video_id': name, 'title': 'Movie', 'page_url': PAGE})
                  for height, name in ((1080, 'one'), (720, 'two'))]
        self.assertEqual(len(group_media(values)), 2)

    def test_conflicting_video_ids_do_not_merge_through_shared_canonical_alias(self):
        values = [candidate(name, metadata={'video_id': name, 'canonical_media_id': 'shared', 'page_url': PAGE})
                  for name in ('one', 'two')]
        self.assertEqual(len(group_media(values)), 2)

    def test_conflicting_ids_keep_stable_unique_ui_ids_despite_shared_alias_and_url(self):
        values = [candidate(name, url='https://cdn.example/entry.mp4',
                            metadata={'video_id': name, 'canonical_media_id': '0-shared', 'page_url': PAGE})
                  for name in ('z-one', 'z-two', 'z-three')]
        groups = group_media(values)
        self.assertEqual(len(groups), 3)
        self.assertEqual(len({group.id for group in groups}), 3)
        self.assertEqual({group.title: group.id for group in groups},
                         {group.title: group.id for group in group_media(list(reversed(values)))})

    def test_weak_quality_filename_requires_additional_title_duration_page(self):
        values = [candidate(str(height), url=f'https://cdn.example/movie-{height}p.mp4', height=height,
                            metadata={'title': 'Movie', 'page_url': PAGE}) for height in (1080, 720)]
        self.assertEqual(len(group_media(values)), 1)
        no_title = deepcopy(values)
        for value in no_title:
            value.metadata.pop('title')
        self.assertEqual(len(group_media(no_title)), 2)
        wrong_duration = deepcopy(values)
        wrong_duration[1].duration = 50
        self.assertEqual(len(group_media(wrong_duration)), 2)

    def test_filename_alone_is_not_group_identity(self):
        values = [candidate(str(height), url=f'https://cdn.example/movie-{height}p.mp4', height=height)
                  for height in (1080, 720)]
        self.assertEqual(len(group_media(values)), 2)

    def test_unknown_identity_query_is_preserved_for_dedup(self):
        values = [candidate(name, url=f'https://cdn.example/file.mp4?id={name}&token=secret') for name in ('a', 'b')]
        self.assertEqual(len(deduplicate_candidates(values)), 2)
        self.assertEqual(len(group_media(values, page_title='Same', source_page=PAGE)), 2)

    def test_quality_query_differences_are_not_deduplicated(self):
        values = [candidate(str(height), url=f'https://cdn.example/video.mp4?height={height}&token=secret',
                            height=height, metadata={'video_id': 'video', 'page_url': PAGE}) for height in (720, 1080)]
        self.assertEqual(len(deduplicate_candidates(values)), 2)
        self.assertEqual(len(group_media(values)), 1)

    def test_request_method_and_body_are_part_of_resource_identity(self):
        first = candidate('a', url='https://cdn.example/api/video')
        second = deepcopy(first)
        second.request_context = RequestContext(first.url, method='POST', body=b'variant=720')
        self.assertEqual(len(deduplicate_candidates([first, second])), 2)

    def test_latest_failed_context_does_not_replace_still_valid_one(self):
        old = candidate('old', url='https://cdn.example/video.mp4?token=old',
                        metadata={'discovered_at': 100, 'video_id': 'movie', 'page_url': PAGE})
        expired = candidate('new', url='https://cdn.example/video.mp4?token=new',
                            metadata={'discovered_at': 200, 'error_code': 'HTTP_403', 'video_id': 'movie', 'page_url': PAGE}, http_status=403)
        value = deduplicate_candidates([old, expired])[0]
        self.assertEqual(value.url, old.url)
        self.assertTrue(available_candidate(value))

    def test_new_incomplete_context_does_not_replace_valid_headers(self):
        old = candidate('old', url='https://cdn.example/video.mp4?token=old', metadata={'video_id': 'movie', 'page_url': PAGE})
        old.request_context = replace(old.request_context, headers={'Authorization': 'Bearer actual'})
        new = candidate('new', url='https://cdn.example/video.mp4?token=new', context_complete=False,
                        metadata={'discovered_at': 999, 'video_id': 'movie', 'page_url': PAGE})
        value = deduplicate_candidates([old, new])[0]
        self.assertEqual(value.request_context.headers['authorization'], 'Bearer actual')

    def test_duplicate_protection_evidence_cannot_be_erased(self):
        protected = candidate('protected', url='https://cdn.example/movie.mp4?token=old',
                              drm=True, encryption='CENC', classification='DRM_WIDEVINE',
                              metadata={'video_id': 'movie', 'page_url': PAGE})
        clear = candidate('clear', url='https://cdn.example/movie.mp4?token=new', drm=False,
                          metadata={'discovered_at': 200, 'video_id': 'movie', 'page_url': PAGE})
        group = group_media([protected, clear])[0]
        self.assertIs(group.candidates[0].drm, True)
        self.assertIsNone(group.selected_candidate)
        self.assertFalse(group.selected)

    def test_duplicate_ad_evidence_is_preserved(self):
        ad = candidate('ad', url='https://cdn.example/opaque.mp4', metadata={'vast_ad': True})
        ordinary = candidate('ordinary', url=ad.url, metadata={'discovered_at': 999})
        group = group_media([ad, ordinary])[0]
        self.assertEqual(len(group.candidates), 1)
        self.assertTrue(group.is_ad)
        self.assertFalse(group.selected)

    def test_unavailable_high_quality_does_not_override_confirmed_lower_quality(self):
        values = [candidate('low', height=720, metadata={'video_id': 'movie', 'page_url': PAGE}),
                  candidate('high', height=1080, http_status=403, metadata={'video_id': 'movie', 'page_url': PAGE})]
        group = group_media(values)[0]
        self.assertEqual(group.selected_candidate.resolution[1], 720)
        self.assertEqual([value.id for value in ordered_candidates(group)], ['low'])

    def test_probe_error_and_unconfirmed_versions_are_not_available(self):
        self.assertFalse(available_candidate(candidate(metadata={'probe_error': 'network failure'})))
        self.assertFalse(available_candidate(candidate(confirmed=False)))
        self.assertTrue(available_candidate(candidate(http_status=304)))

    def test_low_confidence_group_is_not_default_selected(self):
        group = group_media([candidate(score=24)])[0]
        self.assertFalse(group.selected)
        self.assertIsNotNone(group.selected_candidate)
        self.assertTrue(available_candidate(group.selected_candidate))

    def test_preview_is_marked_and_not_selected_even_when_only_media(self):
        group = group_media([candidate('movie-preview')])[0]
        self.assertTrue(group.is_preview)
        self.assertFalse(group.selected)
        self.assertIsNotNone(group.selected_candidate)

    def test_preview_and_full_video_remain_separate_even_with_same_id(self):
        full = candidate('full', metadata={'video_id': 'movie', 'page_url': PAGE})
        preview = candidate('preview', metadata={'video_id': 'movie', 'page_url': PAGE, 'preview': True})
        groups = group_media([full, preview])
        self.assertEqual(len(groups), 2)
        self.assertEqual(sum(group.selected for group in groups), 1)
        self.assertEqual(len({group.id for group in groups}), 2)

    def test_thumbnail_and_protocol_resources_are_not_independent_videos(self):
        values = [candidate('poster', content_type='image/png'),
                  candidate('segment', metadata={'resource_role': 'Media Segment'}),
                  candidate('key', metadata={'resource_role': 'Key'}), candidate('movie')]
        self.assertEqual(len(group_media(values)), 1)

    def test_hls_manifest_semantics_win_over_png_extension_and_mime(self):
        value = candidate('manifest', url='https://cdn.example/index.png', media_type='HLS', content_type='image/png',
                          classification='HLS_AES128', encryption='AES-128', drm=False)
        group = group_media([value])[0]
        self.assertTrue(group.selected)
        self.assertIs(group.selected_candidate.drm, False)

    def test_metadata_title_priority_and_generic_filename_fallback(self):
        value = candidate(metadata={'video_title': 'Specific Title', 'title': 'Metadata Title', 'page_title': 'Page'})
        self.assertEqual(group_media([value], page_title='Fallback')[0].title, 'Specific Title')
        value.metadata = {'title': 'index.m3u8', 'page_title': 'Page'}
        self.assertEqual(group_media([value])[0].title, 'Page')
        value.metadata = {'video_id': 'actual-video-id'}
        self.assertEqual(group_media([value])[0].title, 'actual-video-id')

    def test_page_title_metadata_cannot_override_video_id_with_manifest_filename(self):
        for filename in ('index.m3u8', 'master.m3u8', 'abc000.png'):
            with self.subTest(filename=filename):
                value = candidate(metadata={'page_title': filename, 'video_id': 'actual-video-id'})
                self.assertEqual(group_media([value])[0].title, 'actual-video-id')

    def test_page_title_argument_cannot_be_manifest_or_segment_filename(self):
        for filename in ('index.m3u8', 'master.m3u8', 'abc000.png'):
            with self.subTest(filename=filename):
                group = group_media([candidate()], page_title=filename)[0]
                self.assertTrue(group.title.startswith('media_'))

    def test_generic_video_title_does_not_hide_valid_metadata_title(self):
        value = candidate(metadata={'video_title': 'index.m3u8', 'title': 'Actual Movie'})
        self.assertEqual(group_media([value])[0].title, 'Actual Movie')

    def test_malformed_metadata_does_not_become_group_identity_or_title(self):
        value = candidate(metadata={'video_id': {'invalid': True}, 'title': ['invalid'],
                                    'media_metadata': 'invalid', 'probe': None})
        group = group_media([None, value])[0]
        self.assertTrue(group.title.startswith('media_'))

    def test_quality_uses_actual_resolution_before_filename(self):
        value = candidate(url='https://cdn.example/video-2160p.mp4', height=720)
        self.assertEqual(quality_key(value)[0], 720)
        value.resolution = (0, 0)
        value.metadata = {'probe': {'width': 1920, 'height': 1080}}
        self.assertEqual(quality_key(value)[:2], (1080, 1920))
        value.metadata = {}
        self.assertEqual(quality_key(value)[0], 2160)

    def test_bandwidth_breaks_equal_resolution_ties(self):
        values = [candidate('low', bandwidth=2000000, metadata={'video_id': 'video', 'page_url': PAGE}),
                  candidate('high', bandwidth=4000000, metadata={'video_id': 'video', 'page_url': PAGE})]
        self.assertEqual(group_media(values)[0].selected_candidate.id, 'high')

    def test_stable_group_id_survives_new_candidate_ids_and_signed_url_refresh(self):
        old = candidate('media-0001', url='https://cdn.example/video.mp4?token=old', metadata={'video_id': 'movie', 'page_url': PAGE})
        new = candidate('media-0088', url='https://cdn.example/video.mp4?token=new', metadata={'video_id': 'movie', 'page_url': PAGE})
        self.assertEqual(group_media([old])[0].id, group_media([new])[0].id)

    def test_tokens_without_identity_evidence_remain_distinct(self):
        values = [candidate(name, url=f'https://cdn.example/video.mp4?token={name}',
                            metadata={'page_url': PAGE}) for name in ('a', 'b')]
        self.assertEqual(len(deduplicate_candidates(values)), 2)
        groups = group_media(values)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len({group.id for group in groups}), 2)
        self.assertEqual({c.id: group_media([c])[0].id for c in values},
                         {g.candidates[0].id: g.id for g in groups})

    def test_same_cdn_path_with_different_source_pages_is_not_signature_duplicate(self):
        values = [candidate(name, url=f'https://cdn.example/video.mp4?token={name}',
                            metadata={'video_id': '123', 'page_url': f'https://page.example/watch/{name}'})
                  for name in ('a', 'b')]
        self.assertEqual(len(deduplicate_candidates(values)), 2)
        self.assertEqual(len(group_media(values)), 2)

    def test_signed_master_without_shared_identity_is_not_shared_parent(self):
        values = [candidate(name, url=f'https://cdn.example/master.m3u8?token={name}', media_type='HLS',
                            manifest_type='Master', metadata={'page_url': PAGE}) for name in ('a', 'b')]
        self.assertEqual(len(group_media(values)), 2)

    def test_signed_refresh_with_same_dom_element_is_deduplicated(self):
        values = [candidate(name, url=f'https://cdn.example/video.mp4?token={name}',
                            metadata={'dom_video_id': 'player', 'page_url': PAGE}, frame_id='main') for name in ('a', 'b')]
        self.assertEqual(len(deduplicate_candidates(values)), 1)

    def test_stable_video_group_id_survives_adding_quality_and_input_order(self):
        low = candidate('low', height=720, metadata={'video_id': 'movie', 'page_url': PAGE})
        high = candidate('high', height=1080, metadata={'video_id': 'movie', 'page_url': PAGE})
        self.assertEqual(group_media([low])[0].id, group_media([high, low])[0].id)
        self.assertEqual(group_media([low, high])[0].id, group_media([high, low])[0].id)

    def test_master_group_id_survives_new_confirmed_variant(self):
        master = candidate('master', media_type='HLS', manifest_type='Master')
        variant = candidate('variant', height=1080, media_type='HLS', manifest_type='Media',
                            metadata={'parent_manifest': master.id})
        self.assertEqual(group_media([master], source_page=PAGE)[0].id,
                         group_media([master, variant], source_page=PAGE)[0].id)

    def test_separate_weak_identity_groups_never_share_ui_id(self):
        values = [candidate('a', url='https://cdn.example/movie-1080p.mp4', duration=120,
                            metadata={'title': 'Movie', 'page_url': PAGE}),
                  candidate('b', url='https://cdn.example/movie-720p.mp4', duration=122,
                            metadata={'title': 'Movie', 'page_url': PAGE})]
        groups = group_media(values)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len({group.id for group in groups}), 2)

    def test_inputs_are_not_modified(self):
        values = [candidate('one', metadata={'video_id': 'movie', 'page_url': PAGE}),
                  candidate('two', metadata={'video_id': 'movie', 'page_url': PAGE})]
        previous = deepcopy(values)
        group_media(values)
        self.assertEqual(values, previous)


if __name__ == '__main__':
    unittest.main()
