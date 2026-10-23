from dataclasses import FrozenInstanceError, replace
import json
import unittest
import xml.etree.ElementTree as ET

from core.dash_json import build_dash_manifest, choose_audio, parse_dash_description
from core.dash_models import DashTrack
from core.discovery_models import NetworkObservation
from core.errors import MediaCheckError, ProtectedMediaError
from core.manifests import parse_manifest
from core.media_discovery import MediaDiscoveryService
from core.request_context import RequestContext


NS = {'d': 'urn:mpeg:dash:schema:mpd:2011'}


def video(**values):
    return {'id': 80, 'baseUrl': 'https://cdn.test/video.m4s?sign=x%2Fy&expires=123',
            'backupUrl': ['https://backup.test/video.m4s?token=a%2Bb'],
            'bandwidth': 1_500_000, 'width': 1920, 'height': 1080,
            'codecs': 'avc1.640028', 'mimeType': 'video/mp4', 'frameRate': '30000/1001',
            'SegmentBase': {'Initialization': '0-929', 'indexRange': '930-1141'}, **values}


def audio(**values):
    return {'id': 30216, 'baseUrl': 'https://cdn.test/audio.m4s?token=a%2Bb&x=1',
            'bandwidth': 64_000, 'codecs': 'mp4a.40.2', 'mimeType': 'audio/mp4',
            'SegmentBase': {'Initialization': '0-700', 'indexRange': '701-900'}, **values}


def description(videos=None, audios=None, **dash_values):
    return {'data': {'dash': {'duration': 19.5, 'video': [video()] if videos is None else videos,
                              'audio': [audio()] if audios is None else audios, **dash_values}}}


def parse(value, **options):
    return parse_dash_description(json.dumps(value).encode(), 'https://page.test/api/playurl', **options)


class DashJsonTests(unittest.TestCase):
    def test_real_description_preserves_signed_urls_and_ranges(self):
        result = parse(description())
        self.assertIsNotNone(result)
        self.assertEqual(result.duration, 19.5)
        self.assertFalse(result.drm)
        self.assertEqual(result.classification, 'CLEAR_DASH')
        track = result.videos[0]
        self.assertEqual((track.id, track.width, track.height, track.bandwidth), ('80', 1920, 1080, 1_500_000))
        self.assertEqual(track.url, video()['baseUrl'])
        self.assertEqual(track.backup_urls, tuple(video()['backupUrl']))
        self.assertEqual((track.initialization, track.index_range, track.frame_rate), ('0-929', '930-1141', '30000/1001'))

    def test_root_and_result_wrappers_and_snake_fields(self):
        item = {'id': 'v1', 'base_url': '../media/signed.png?sign=abc%2Fdef&t=3',
                'backup_url': ['//backup.test/video?auth=a%2Bb'], 'bandwidth': '99000',
                'mime_type': 'video/mp4', 'frame_rate': '24', 'width': '640', 'height': '360',
                'segment_base': {'initialization': '0-29', 'index_range': '30-99'}}
        for wrapper in (lambda dash: {'dash': dash}, lambda dash: {'result': {'dash': dash}}):
            with self.subTest(wrapper=wrapper):
                result = parse(wrapper({'video': [item]}))
                self.assertEqual(result.videos[0].url, 'https://page.test/media/signed.png?sign=abc%2Fdef&t=3')
                self.assertEqual(result.videos[0].backup_urls, ('https://backup.test/video?auth=a%2Bb',))
                self.assertEqual(result.videos[0].initialization, '0-29')
                self.assertEqual(result.videos[0].index_range, '30-99')
                self.assertEqual(result.videos[0].height, 360)
                self.assertEqual(result.audios, ())

    def test_duration_units_and_precedence(self):
        value = description(duration=None)
        value['data']['timelength'] = 344_000
        self.assertEqual(parse(value).duration, 344)
        value['data']['dash']['duration'] = 18.25
        self.assertEqual(parse(value).duration, 18.25)
        value['data']['dash']['duration'] = -1
        value['data']['timelength'] = '19500'
        self.assertEqual(parse(value).duration, 19.5)
        self.assertIsNone(parse(description(duration=0)).duration)

    def test_missing_video_is_not_a_complete_media_description(self):
        for value in ({'dash': {'audio': [audio()]}}, {'data': {}}, {'dash': {'video': []}},
                      {'dash': {'video': 'https://cdn.test/video'}}, {'video': [video()]}, []):
            with self.subTest(value=value):
                self.assertIsNone(parse(value))

    def test_short_metadata_strings_are_not_scanned_as_urls(self):
        result = parse(description(videos=[video(codecs='avc1.640028')],
                                   audios=[audio(codecs='mp4a.40.2')]))
        self.assertEqual(len(result.videos), 1)
        self.assertEqual(len(result.audios), 1)
        self.assertEqual(result.videos[0].url, video()['baseUrl'])
        self.assertNotIn('avc1', result.videos[0].url)
        self.assertIsNone(parse({'dash': {'video': [{'codecs': 'avc1.640028',
                                                     'SegmentBase': {'Initialization': '0-929'}}]}}))

    def test_primary_and_backup_urls_are_resolved_without_query_reconstruction(self):
        original = 'https://cdn.test/noextension?z=2&z=1&sign=a%2Fb+X&token=&t=0001#part'
        result = parse(description(videos=[video(baseUrl=original, backupUrl=[original, '../alt?auth=a%2Bb'])]))
        self.assertEqual(result.videos[0].url, original)
        self.assertEqual(result.videos[0].backup_urls, ('https://page.test/alt?auth=a%2Bb',))
        result = parse(description(videos=[video(baseUrl='', backupUrl=['https://backup.test/track'])]))
        self.assertEqual(result.videos[0].url, 'https://backup.test/track')

    def test_invalid_urls_do_not_create_tracks(self):
        for url in ('javascript:alert(1)', 'blob:https://cdn.test/key', 'file:///C:/video',
                    'https://user:pass@cdn.test/video', 'https://cdn.test/a\nHeader: value',
                    'https://cdn.test/with space', 'https://cdn.test/\ud800'):
            with self.subTest(url=url):
                self.assertIsNone(parse(description(videos=[video(baseUrl=url, backupUrl=[])])))

    def test_ranges_are_retained_or_rejected_without_inventing_values(self):
        result = parse(description(videos=[video(SegmentBase={'Initialization': {'range': '0-10'}, 'indexRange': '11-20'})]))
        self.assertEqual(result.videos[0].initialization, '0-10')
        for invalid in ('10-0', 'bytes=0-10', '-1-10', '0-10\n', '0-99999999999999999999999'):
            with self.subTest(invalid=invalid):
                self.assertIsNone(parse(description(videos=[video(SegmentBase={'indexRange': invalid})])))
        self.assertIsNone(parse(description(videos=[video(SegmentBase={
            'Initialization': {'range': '0-10', 'sourceURL': 'https://other.test/init.mp4'}})])))

    def test_one_valid_video_does_not_depend_on_an_invalid_alternative(self):
        result = parse(description(videos=[{}, video(), video(baseUrl='javascript:bad', backupUrl=[])]))
        self.assertEqual(len(result.videos), 1)

    def test_choose_audio_prefers_aac_before_bandwidth(self):
        result = parse(description(audios=[audio(id=1, codecs='ec-3', bandwidth=999_999),
                                            audio(id=2, codecs='mp4a.40.2', bandwidth=64_000),
                                            audio(id=3, codecs='mp4a.40.2', bandwidth=128_000)]))
        self.assertEqual(choose_audio(result.audios).id, '3')
        self.assertIsNone(choose_audio(()))
        self.assertIsNone(choose_audio(result.videos))
        self.assertEqual(choose_audio(parse(description(audios=[audio(id=5, codecs='opus', bandwidth=128_000),
                                                               audio(id=6, codecs='ec-3', bandwidth=64_000)])).audios).id, '5')

    def test_explicit_drm_is_reported_and_builder_refuses(self):
        cases = [({'keySystem': 'com.widevine.alpha'}, 'DRM_WIDEVINE'),
                 ({'ContentProtection': {'schemeIdUri': 'urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95'}}, 'DRM_PLAYREADY'),
                 ({'key_format': 'com.apple.streamingkeydelivery'}, 'DRM_FAIRPLAY'),
                 ({'PSSH': 'AAAA'}, 'DRM_UNKNOWN'), ({'encryption': 'CENC'}, 'DRM_UNKNOWN'),
                 ({'drm': True}, 'DRM_UNKNOWN'), ({'keySystems': ['com.widevine.alpha']}, 'DRM_WIDEVINE'),
                 ({'license_server_url': 'https://license.test/request'}, 'DRM_UNKNOWN')]
        for evidence, classification in cases:
            with self.subTest(evidence=evidence):
                result = parse(description(**evidence))
                self.assertTrue(result.drm)
                self.assertEqual(result.classification, classification)
                self.assertTrue(all(track.drm for track in result.videos + result.audios))
                with self.assertRaises(ProtectedMediaError):
                    build_dash_manifest(result.videos[0], choose_audio(result.audios), result.duration)

    def test_protected_audio_also_protects_whole_description(self):
        result = parse(description(audios=[audio(key_system='com.microsoft.playready.hardware')]))
        self.assertTrue(result.drm)
        self.assertTrue(result.videos[0].drm)
        clear_video = replace(result.videos[0], drm=False)
        with self.assertRaises(ProtectedMediaError):
            build_dash_manifest(clear_video, result.audios[0], result.duration)

    def test_aes_ordinary_key_or_titles_are_not_drm_evidence(self):
        result = parse(description(encryption='AES-128', key='enc.key', keyformat='identity',
                                   drm=False, title='widevine', note='AES encrypted segments'))
        self.assertFalse(result.drm)
        self.assertEqual(result.classification, 'CLEAR_DASH')

    def test_manifest_contains_only_chosen_video_and_audio(self):
        result = parse(description(videos=[video(id=80), video(id=64, height=720)], audios=[audio()]))
        root = ET.fromstring(build_dash_manifest(result.videos[1], choose_audio(result.audios), result.duration))
        representations = root.findall('.//d:Representation', NS)
        self.assertEqual(len(representations), 2)
        self.assertEqual(root.get('mediaPresentationDuration'), 'PT19.5S')
        self.assertEqual([node.get('contentType') for node in root.findall('.//d:AdaptationSet', NS)], ['video', 'audio'])
        self.assertEqual(representations[0].get('height'), '720')
        self.assertEqual(representations[0].get('codecs'), 'avc1.640028')
        self.assertEqual(representations[1].get('codecs'), 'mp4a.40.2')
        self.assertEqual([node.text for node in root.findall('.//d:BaseURL', NS)],
                         [result.videos[1].url, result.audios[0].url])
        self.assertEqual(representations[0].find('d:SegmentBase', NS).get('indexRange'), '930-1141')
        self.assertEqual(representations[1].find('d:SegmentBase/d:Initialization', NS).get('range'), '0-700')

    def test_video_only_mpd_and_unknown_duration(self):
        result = parse(description(audios=[], duration=None))
        root = ET.fromstring(build_dash_manifest(result.videos[0], None, result.duration))
        self.assertEqual(len(root.findall('.//d:Representation', NS)), 1)
        self.assertNotIn('mediaPresentationDuration', root.attrib)

    def test_generated_mpd_enters_existing_protocol_discovery(self):
        result = parse(description())
        text = build_dash_manifest(result.videos[0], result.audios[0], result.duration)
        info = parse_manifest(text.encode(), 'https://page.test/api/playurl', 'application/dash+xml')
        self.assertEqual(info.media_type, 'DASH')
        self.assertFalse(info.drm)
        service = MediaDiscoveryService()
        service.consume_network(NetworkObservation('mpd', RequestContext('https://page.test/api/playurl'),
                                200, 'application/dash+xml', text.encode(), resource_type='Fetch',
                                context_complete=True, body_complete=True))
        candidates = service.ranked(limit=128, include_low=True)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0].downloadable)
        self.assertEqual(candidates[0].media_type, 'DASH')
        self.assertEqual(len(candidates[0].metadata['representations']), 2)

    def test_xml_escaping_preserves_signed_query_and_cannot_inject_elements(self):
        url = 'https://cdn.test/track?sign=a%2Fb&name="<evil>"&token=x+y'
        result = parse(description(videos=[video(id='x"/><Injected/>', baseUrl=url)]))
        text = build_dash_manifest(result.videos[0], None, result.duration)
        root = ET.fromstring(text)
        self.assertEqual(root.find('.//d:BaseURL', NS).text, url)
        self.assertEqual(root.find('.//d:Representation', NS).get('id'), 'video-x"/><Injected/>')
        self.assertIsNone(root.find('.//Injected'))

    def test_size_decode_json_and_nonfinite_limits(self):
        data = json.dumps(description()).encode()
        self.assertIsNone(parse_dash_description(data, max_bytes=len(data) - 1))
        self.assertIsNotNone(parse_dash_description(data, max_bytes=len(data)))
        for invalid in (b'', b'\xff', b'{broken', b'[]', b'null', b'{"dash":{"duration":NaN,"video":[]}}',
                        b'x' * (16 * 1024 * 1024 + 1)):
            with self.subTest(size=len(invalid)):
                self.assertIsNone(parse_dash_description(invalid))
        for limit in (0, -1, True, 1.5):
            self.assertIsNone(parse_dash_description(data, max_bytes=limit))
        self.assertIsNotNone(parse_dash_description(b'\xef\xbb\xbf' + data))

    def test_depth_node_and_track_limits_fail_closed(self):
        value = description()
        deep = {}
        value['debug'] = deep
        for _ in range(30):
            deep['child'] = {}
            deep = deep['child']
        self.assertIsNone(parse(value))
        value = description()
        value['debug'] = [0] * 100_001
        self.assertIsNone(parse(value))
        self.assertIsNone(parse(description(videos=[video()] * 257)))

    def test_configurable_size_limit_can_exceed_default_sixteen_mib(self):
        data = b' ' * (16 * 1024 * 1024 + 1) + json.dumps(description()).encode()
        self.assertIsNone(parse_dash_description(data))
        result = parse_dash_description(data, max_bytes=32 * 1024 * 1024)
        self.assertIsNotNone(result)
        self.assertEqual(result.videos[0].url, video()['baseUrl'])

    def test_numeric_frame_rates_and_invalid_range_denominators(self):
        for value, expected in ((29.97, '29.97'), ('30000/1001', '30000/1001'), ('0/0', ''), ('abc', ''), (True, '')):
            with self.subTest(value=value):
                result = parse(description(videos=[video(frameRate=value)]))
                self.assertEqual(result.videos[0].frame_rate, expected)

    def test_builder_rejects_invalid_model_without_exposing_url(self):
        track = parse(description()).videos[0]
        for invalid in (replace(track, url='javascript:secret'), replace(track, role='audio'),
                        replace(track, index_range='4-1'), replace(track, frame_rate='30/0')):
            with self.subTest(invalid=invalid.id):
                with self.assertRaises(MediaCheckError) as caught:
                    build_dash_manifest(invalid, None, 20)
                self.assertNotIn('secret', str(caught.exception))

    def test_models_are_immutable_and_repr_omits_signed_urls(self):
        result = parse(description())
        self.assertIsInstance(result.videos, tuple)
        with self.assertRaises(FrozenInstanceError):
            result.videos[0].height = 720
        self.assertNotIn('sign=', repr(result))
        self.assertNotIn('token=', repr(result.videos[0]))


if __name__ == '__main__':
    unittest.main()
