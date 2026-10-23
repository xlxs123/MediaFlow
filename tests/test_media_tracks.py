from dataclasses import replace
import json
import unittest
import xml.etree.ElementTree as ET

from core.discovery_models import MediaCandidate
from core.request_context import RequestContext
from core.media_tracks import (apply_selection, catalog, dash_catalog, default_selection,
    hls_catalog, json_audio_catalog, language_name, normalize_preferences, same_language,
    validate_selection, ytdlp_catalog)
from core.dash_json import parse_dash_description

HLS = '''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Original",LANGUAGE="en",DEFAULT=YES,CHANNELS="2",URI="en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="日本語",LANGUAGE="ja",DEFAULT=NO,URI="ja.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="简体",LANGUAGE="zh-Hans",URI="zh.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=320x180,AUDIO="audio",SUBTITLES="subs"
video.m3u8
'''
MPD = '''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>
<AdaptationSet contentType="video"><Representation id="v" codecs="avc1.4d" width="320" height="180"><BaseURL>v.mp4</BaseURL></Representation></AdaptationSet>
<AdaptationSet contentType="audio" lang="en"><Representation id="a1" codecs="mp4a.40.2" bandwidth="60000" audioSamplingRate="48000"><AudioChannelConfiguration value="2"/><BaseURL>a1.mp4</BaseURL></Representation></AdaptationSet>
<AdaptationSet contentType="audio" lang="ja"><Representation id="a2" codecs="mp4a.40.2"><BaseURL>a2.mp4</BaseURL></Representation></AdaptationSet>
<AdaptationSet contentType="text" lang="en" mimeType="text/vtt"><Representation id="s"><BaseURL>en.vtt</BaseURL></Representation></AdaptationSet>
</Period></MPD>'''


def candidate(protocol, manifest):
    return MediaCandidate('test', RequestContext('https://test.local/master'), media_type=protocol,
        manifest_text=manifest, manifest_url='https://test.local/master', confidenceScore=66)


class TracksTests(unittest.TestCase):
    def test_language_is_declared_not_guessed(self):
        self.assertEqual(language_name('zh-Hans'), '简体中文 (zh-Hans)')
        self.assertEqual(language_name(''), '未知语言')
        self.assertEqual(language_name('xx'), '未知语言 (xx)')
        self.assertTrue(same_language('en', 'eng'))
        self.assertFalse(same_language('zh-Hans', 'zh-Hant'))

    def test_direct_unknown_does_not_block(self):
        value = candidate('Direct', '')
        self.assertFalse(catalog(value)['known'])
        selected = apply_selection(value)
        self.assertEqual(selected.confidenceScore, 66)
        self.assertEqual(selected.manifest_text, '')

    def test_hls_rendition_metadata(self):
        values = hls_catalog(HLS, 'https://test.local/master.m3u8')
        self.assertEqual(len(values['audio']), 2)
        self.assertEqual(values['audio'][0]['channels'], 2)
        self.assertEqual(values['subtitles'][0]['language'], 'zh-Hans')
        self.assertEqual(values['subtitles'][0]['format'], 'hls-vtt')

    def test_hls_selection_no_other_audio_or_subtitle_in_av_manifest(self):
        value = candidate('HLS', HLS)
        values = catalog(value)
        selected = default_selection(values, {'audio_language': 'ja'})
        result = apply_selection(value, selected)
        self.assertIn('ja.m3u8', result.manifest_text)
        self.assertNotIn('en.m3u8', result.manifest_text)
        self.assertNotIn('zh.m3u8', result.manifest_text)
        self.assertNotIn('SUBTITLES=', result.manifest_text)
        self.assertEqual(value.manifest_text, HLS)

    def test_hls_wrong_variant_group_rejected(self):
        value = candidate('HLS', HLS)
        value.metadata['track_variant_url'] = 'https://test.local/other.m3u8'
        with self.assertRaisesRegex(ValueError, '兼容音轨'):
            apply_selection(value)

    def test_hls_child_uses_parent_entry_without_aliasing_video_to_master(self):
        value = candidate('HLS', '')
        value.request_context = value.request_context.derive('https://test.local/video.m3u8')
        value.metadata.update(track_master_text=HLS, track_master_url='https://test.local/master',
                              track_variant_url=value.url, track_catalog=hls_catalog(HLS, 'https://test.local/master'))
        result = apply_selection(value, default_selection(catalog(value), {'audio_language': 'ja'}))
        self.assertEqual(result.url, 'https://test.local/master')
        self.assertEqual(result.manifest_url, result.url)
        self.assertIn('video.m3u8', result.manifest_text)
        self.assertEqual(value.url, 'https://test.local/video.m3u8')

    def test_hls_instream_not_faked_as_complete_track(self):
        values = hls_catalog('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="Original"', '')
        self.assertFalse(values['audio'][0]['supported'])
        self.assertEqual(default_selection(values)['audio_id'], '')

    def test_dash_actual_audio_selection(self):
        value = candidate('DASH', MPD)
        values = catalog(value)
        self.assertEqual(values['audio'][0]['sample_rate'], 48000)
        self.assertEqual(values['audio'][0]['channels'], 2)
        self.assertEqual(values['subtitles'][0]['url'], 'https://test.local/en.vtt')
        selected = default_selection(values, {'audio_language': 'ja'})
        result = apply_selection(value, selected)
        self.assertIn('a2.mp4', result.manifest_text)
        self.assertNotIn('a1.mp4', result.manifest_text)
        self.assertIn('v.mp4', result.manifest_text)
        self.assertNotIn('en.vtt', result.manifest_text)

    def test_dash_segmented_subtitle_visible_but_disabled(self):
        values = dash_catalog(MPD.replace('<BaseURL>en.vtt</BaseURL>', '<SegmentTemplate media="s-$Number$.m4s"/>'), '')
        self.assertFalse(values['subtitles'][0]['supported'])
        self.assertIn('分段', values['subtitles'][0]['reason'])

    def test_ytdlp_actual_formats_and_generated_flags(self):
        values = ytdlp_catalog({'formats': [{'format_id': '140', 'vcodec': 'none', 'acodec': 'aac', 'language': 'en'},
                                          {'format_id': 'evil]/best', 'vcodec': 'none', 'acodec': 'opus'}],
            'subtitles': {'en': [{'url': 'https://test.local/en.srt', 'ext': 'srt'}]},
            'automatic_captions': {'ja': [{'url': 'https://test.local/ja.vtt', 'ext': 'vtt'}]}})
        self.assertEqual(values['audio'][0]['format_id'], '140')
        self.assertFalse(values['audio'][1]['supported'])
        self.assertFalse(values['subtitles'][0]['automatic'])
        self.assertTrue(values['subtitles'][1]['automatic'])
        value = candidate('DASH', '')
        value.metadata['track_catalog'] = values
        result = apply_selection(value)
        self.assertEqual(result.metadata['format_selector'], 'bestvideo+140')

    def test_default_no_subtitle_and_missing_language_requires_choice(self):
        values = catalog(candidate('HLS', HLS))
        self.assertEqual(default_selection(values)['subtitle_ids'], [])
        with self.assertRaisesRegex(ValueError, '未替换'):
            default_selection(values, {'subtitle_mode': 'sidecar', 'subtitle_languages': ['en']})
        with self.assertRaisesRegex(ValueError, '实际可用'):
            default_selection(values, {'audio_language': 'fr'})

    def test_cross_media_ids_rejected(self):
        values = catalog(candidate('HLS', HLS))
        selection = default_selection(values)
        selection['audio_id'] = 'not-this-page'
        with self.assertRaisesRegex(ValueError, '不属于当前媒体'):
            validate_selection(values, selection)

    def test_reordered_dash_audio_keeps_semantic_identity_and_language(self):
        first = catalog(candidate('DASH', MPD))
        selection = default_selection(first, {'audio_language': 'ja'})
        root = ET.fromstring(MPD)
        period = list(root)[0]
        values = list(period)
        period.remove(values[1])
        period.insert(2, values[1])
        reordered = candidate('DASH', ET.tostring(root, encoding='unicode'))
        result = apply_selection(reordered, selection)
        self.assertEqual(result.metadata['selected_audio']['language'], 'ja')
        self.assertIn('a2.mp4', result.manifest_text)
        self.assertNotIn('a1.mp4', result.manifest_text)

    def test_language_change_cannot_silently_reuse_old_subtitle_id(self):
        first = catalog(candidate('DASH', MPD))
        selection = default_selection(first, {'subtitle_mode': 'sidecar', 'subtitle_languages': ['en']})
        changed = candidate('DASH', MPD.replace('contentType="text" lang="en"', 'contentType="text" lang="ja"'))
        with self.assertRaisesRegex(ValueError, '不属于当前媒体'):
            apply_selection(changed, selection)

    def test_json_all_audio_preserved_then_single_pair_built(self):
        raw = {'dash': {'video': [{'id': 'v', 'baseUrl': 'https://test.local/v', 'codecs': 'avc1'}],
             'audio': [{'id': 'a1', 'baseUrl': 'https://test.local/a1', 'codecs': 'mp4a.40.2', 'language': 'en'},
                       {'id': 'a2', 'baseUrl': 'https://test.local/a2', 'codecs': 'mp4a.40.2', 'language': 'ja'}]}}
        description = parse_dash_description(json.dumps(raw).encode())
        values = json_audio_catalog(description)
        value = candidate('DASH', '')
        value.video_track = description.videos[0]
        value.metadata['track_catalog'] = values
        result = apply_selection(value, default_selection(values, {'audio_language': 'ja'}))
        self.assertEqual(result.audio_track.language, 'ja')
        self.assertIn('a2', result.manifest_text)
        self.assertNotIn('a1', result.manifest_text)
        self.assertEqual(len(ET.fromstring(result.manifest_text)[0]), 2)

    def test_untrusted_preference_fields_do_not_persist(self):
        result = normalize_preferences({'cookie': 'SECRET', 'audio_language': '../../en',
                                        'subtitle_languages': ['ja', 'ja'], 'subtitle_mode': 'burn'})
        self.assertEqual(result['audio_language'], '')
        self.assertEqual(result['subtitle_languages'], ['ja'])
        self.assertEqual(result['subtitle_mode'], 'none')
        self.assertNotIn('cookie', result)

    def test_html_track_uses_declared_language_and_deduplicates(self):
        from core.media_tracks import html_subtitles
        value = candidate('Direct', '')
        rows = [{'src': '/English-name.vtt', 'language': 'ja', 'name': 'Actual caption', 'kind': 'subtitles'}]
        value.metadata['page_subtitles'] = html_subtitles(rows + rows, 'https://test.local/page')
        values = catalog(value)
        self.assertEqual(len(values['subtitles']), 1)
        self.assertEqual(values['subtitles'][0]['language'], 'ja')
        self.assertEqual(values['subtitles'][0]['url'], 'https://test.local/English-name.vtt')
        self.assertIsNone(values['subtitles'][0]['automatic'])

    def test_html_unknown_language_not_inferred_from_filename(self):
        from core.media_tracks import html_subtitles
        values = html_subtitles([{'src': 'https://test.local/english-en.vtt', 'kind': 'subtitles'}])
        self.assertEqual(values[0]['language'], '')
        self.assertEqual(language_name(values[0]['language']), '未知语言')


if __name__ == '__main__':
    unittest.main()
