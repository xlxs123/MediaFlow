"""媒体详情回归：只使用已捕获证据与本地协议夹具，无网络访问。"""
from copy import deepcopy
from dataclasses import asdict
import struct
import unittest

from core.discovery_models import MediaCandidate
from core.media_details import MAX_CAPTURE_BYTES, enrich_media_details
from core.request_context import RequestContext


def candidate(**fields):
    values = dict(id='details', request_context=RequestContext('https://media.example/resource.png'),
                  media_type='Direct')
    values.update(fields)
    return MediaCandidate(**values)


def atom(kind, payload=b'', *, large=False, zero=False):
    if large:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', 0 if zero else len(payload) + 8, kind) + payload


def track(kind=b'vide', *, width=1920, height=1080, bitrate=2_000_000,
          timescale=30_000, ticks=300_300, delta=1001, version=0):
    tkhd = bytearray(84 if version == 0 else 96)
    tkhd[0] = version
    struct.pack_into('>II', tkhd, 76 if version == 0 else 88, width * 65536, height * 65536)
    mdhd = bytearray(24 if version == 0 else 36)
    mdhd[0] = version
    if version == 0:
        struct.pack_into('>II', mdhd, 12, timescale, ticks)
    else:
        struct.pack_into('>IQ', mdhd, 20, timescale, ticks)
    hdlr = bytes(8) + kind
    btrt = atom(b'btrt', struct.pack('>III', 0, bitrate, bitrate))
    if kind == b'vide':
        entry = bytearray(78)
        struct.pack_into('>HH', entry, 24, width, height)
        sample = atom(b'avc1', bytes(entry) + atom(b'avcC', bytes([1, 100, 0, 40])) + btrt)
    else:
        sample = atom(b'mp4a', bytes(28) + btrt)
    stsd = atom(b'stsd', bytes(4) + struct.pack('>I', 1) + sample)
    stts = atom(b'stts', bytes(4) + struct.pack('>III', 1, 300, delta))
    mdia = atom(b'mdia', atom(b'mdhd', mdhd) + atom(b'hdlr', hdlr) +
                atom(b'minf', atom(b'stbl', stsd + stts)))
    return atom(b'trak', atom(b'tkhd', tkhd) + mdia)


def mp4(*, audio=True, version=0, moov_large=False, moov_zero=False):
    tracks = track(version=version)
    if audio:
        tracks += track(b'soun', width=0, height=0, bitrate=128_000, version=version)
    return (atom(b'ftyp', b'isom' + bytes(4) + b'iso6') +
            atom(b'moov', tracks, large=moov_large, zero=moov_zero))


class MediaDetailsTests(unittest.TestCase):
    def test_unknown_information_stays_unknown(self):
        item = candidate()
        enrich_media_details(item)
        for field in ('width', 'height', 'fps', 'videoCodec', 'audioCodec',
                      'videoBitrate', 'audioBitrate', 'estimatedSize', 'container'):
            self.assertIsNone(getattr(item, field), field)
        self.assertFalse(item.metadata['size_is_estimate'])

    def test_hls_declared_variant_overrides_current_player_dimensions(self):
        item = candidate(media_type='HLS', playlist_type='VOD', duration=20,
                         resolution=(1280, 720), bandwidth=4_000_000,
                         metadata={'declared_resolution': (3840, 2160),
                                   'declared_bandwidth': 8_000_000,
                                   'codecs': 'avc1.640033,mp4a.40.2', 'frame_rate': '60000/1001'})
        enrich_media_details(item)
        self.assertEqual((item.width, item.height), (3840, 2160))
        self.assertAlmostEqual(item.fps, 60000 / 1001)
        self.assertEqual((item.videoCodec, item.audioCodec), ('avc1.640033', 'mp4a.40.2'))
        self.assertIsNone(item.videoBitrate)  # HLS BANDWIDTH includes audio.
        self.assertIsNone(item.audioBitrate)
        self.assertEqual(item.estimatedSize, 20_000_000)
        self.assertTrue(item.metadata['size_is_estimate'])
        self.assertEqual(item.resolution, (1280, 720))
        self.assertEqual(item.bandwidth, 4_000_000)

    def test_invalid_declared_variant_does_not_copy_playing_rendition(self):
        item = candidate(media_type='HLS', playlist_type='VOD', duration=20,
                         resolution=(1280, 720), bandwidth=4_000_000,
                         metadata={'declared_resolution': (0, 0), 'declared_bandwidth': -1})
        enrich_media_details(item)
        self.assertIsNone(item.width)
        self.assertIsNone(item.estimatedSize)

    def test_hls_png_transport_is_not_container_or_media_length(self):
        item = candidate(media_type='HLS', playlist_type='VOD', duration=20,
                         content_type='image/png', http_status=200,
                         metadata={'response_headers': {'Content-Length': '1234'}})
        enrich_media_details(item, b'\x89PNG\r\n\x1a\n' + bytes(100))
        self.assertIsNone(item.container)
        self.assertIsNone(item.estimatedSize)
        self.assertEqual(item.media_type, 'HLS')

    def test_hls_segment_mime_can_identify_media_container(self):
        item = candidate(media_type='HLS', content_type='image/png',
                         metadata={'segment_content_type': 'video/mp2t'})
        enrich_media_details(item)
        self.assertEqual(item.container, 'MPEG-TS')

    def test_captured_hls_extinf_duration_is_detail_only(self):
        item = candidate(media_type='HLS', bandwidth=800_000)
        body = (b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n'
                b'#EXTINF:2.5,\nabc000.png\n#EXTINF:2.5,\nabc001.png\n#EXT-X-ENDLIST\n')
        enrich_media_details(item, body, body_complete=True, content_length=len(body))
        self.assertEqual(item.metadata['detail_duration'], 5)
        self.assertEqual(item.estimatedSize, 500_000)
        self.assertIsNone(item.duration)
        self.assertIsNone(item.container)

    def test_hls_audio_group_is_evidence_without_invented_codec(self):
        item = candidate(media_type='HLS')
        enrich_media_details(item, b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="main"\n')
        self.assertTrue(item.metadata['has_audio'])
        self.assertIsNone(item.audioCodec)

    def test_live_size_unknown_and_removes_previous_vod_estimate(self):
        item = candidate(media_type='HLS', playlist_type='VOD', duration=20, bandwidth=800_000)
        enrich_media_details(item)
        self.assertEqual(item.estimatedSize, 2_000_000)
        item.playlist_type = 'LIVE'
        enrich_media_details(item)
        self.assertIsNone(item.estimatedSize)
        self.assertFalse(item.metadata['size_is_estimate'])

    def test_complete_hls_body_without_endlist_is_live(self):
        item = candidate(media_type='HLS', playlist_type='VOD', duration=10, bandwidth=800_000)
        enrich_media_details(item, b'#EXTM3U\n#EXTINF:10,\nsegment.png\n', body_complete=True)
        self.assertIsNone(item.estimatedSize)

    def test_dash_separate_audio_video_details_and_size(self):
        reps = [{'id': 'low', 'width': 1280, 'height': 720, 'bandwidth': 1_000_000,
                 'codecs': 'avc1.64001f', 'frame_rate': '25', 'mime_type': 'video/mp4'},
                {'id': 'high', 'width': 1920, 'height': 1080, 'bandwidth': 2_000_000,
                 'codecs': 'avc1.640028', 'frame_rate': '30000/1001', 'mime_type': 'video/mp4'},
                {'id': 'audio', 'bandwidth': 128_000, 'codecs': 'mp4a.40.2', 'mime_type': 'audio/mp4'}]
        item = candidate(media_type='DASH', playlist_type='VOD', duration=20,
                         resolution=(1920, 1080), metadata={'representations': reps})
        enrich_media_details(item)
        self.assertEqual((item.width, item.height), (1920, 1080))
        self.assertEqual((item.videoBitrate, item.audioBitrate), (2_000_000, 128_000))
        self.assertAlmostEqual(item.fps, 30000 / 1001)
        self.assertEqual(item.container, 'MP4')
        self.assertEqual(item.estimatedSize, 5_320_000)
        self.assertTrue(item.metadata['has_audio'])

    def test_dash_captured_mpd_inherits_adaptation_metadata(self):
        item = candidate(media_type='DASH', http_status=200,
                         metadata={'response_headers': {'Content-Length': '400'}})
        data = b'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT10S">
          <Period><AdaptationSet mimeType="video/mp4" codecs="avc1.640028" frameRate="25">
           <Representation id="v" width="1920" height="1080" bandwidth="2000000"/>
          </AdaptationSet><AdaptationSet contentType="audio" codecs="mp4a.40.2">
           <Representation id="a" bandwidth="128000"/>
          </AdaptationSet></Period></MPD>'''
        enrich_media_details(item, data, body_complete=True, content_length=len(data))
        self.assertEqual((item.width, item.height, item.fps), (1920, 1080, 25))
        self.assertEqual((item.videoCodec, item.audioCodec), ('avc1.640028', 'mp4a.40.2'))
        self.assertEqual(item.metadata['detail_duration'], 10)
        self.assertEqual(item.estimatedSize, 2_660_000)
        self.assertIsNone(item.duration)
        self.assertTrue(item.metadata['size_is_estimate'])

    def test_dash_multiple_ambiguous_renditions_do_not_pick_first(self):
        item = candidate(media_type='DASH', playlist_type='VOD', duration=20,
                         metadata={'representations': [
                             {'width': 1280, 'height': 720, 'codecs': 'avc1', 'bandwidth': 1_000_000},
                             {'width': 1920, 'height': 1080, 'codecs': 'avc1', 'bandwidth': 2_000_000}]})
        enrich_media_details(item)
        self.assertIsNone(item.width)
        self.assertIsNone(item.videoBitrate)
        self.assertIsNone(item.estimatedSize)

    def test_dash_muxed_bandwidth_is_not_video_only(self):
        item = candidate(media_type='DASH', playlist_type='VOD', duration=8,
                         metadata={'representations': [
                             {'width': 1280, 'height': 720, 'codecs': 'avc1.64001f,mp4a.40.2',
                              'bandwidth': 1_000_000}]})
        enrich_media_details(item)
        self.assertIsNone(item.videoBitrate)
        self.assertIsNone(item.audioBitrate)
        self.assertEqual(item.estimatedSize, 1_000_000)
        self.assertTrue(item.metadata['has_audio'])

    def test_dash_multiple_audio_tracks_do_not_guess_bitrate(self):
        item = candidate(media_type='DASH', playlist_type='VOD', duration=8,
                         metadata={'representations': [
                             {'codecs': 'avc1', 'width': 1280, 'height': 720, 'bandwidth': 1_000_000},
                             {'codecs': 'mp4a.40.2', 'bandwidth': 128_000},
                             {'codecs': 'opus', 'bandwidth': 192_000}]})
        enrich_media_details(item)
        self.assertTrue(item.metadata['has_audio'])
        self.assertIsNone(item.audioCodec)
        self.assertIsNone(item.estimatedSize)

    def test_dash_dynamic_and_malformed_xml_keep_size_unknown(self):
        for body in (b'<MPD type="dynamic" mediaPresentationDuration="PT10S"/>', b'<MPD>',
                     b'<!DOCTYPE MPD [<!ENTITY a "b">]><MPD/>'):
            with self.subTest(body=body):
                item = candidate(media_type='DASH', http_status=200)
                enrich_media_details(item, body, body_complete=True, content_length=len(body))
                self.assertIsNone(item.estimatedSize)

    def test_direct_200_full_content_length_is_exact_without_full_capture(self):
        item = candidate(http_status=200, content_type='video/mp4; charset=binary',
                         metadata={'response_headers': {'CONTENT-LENGTH': '675064'}})
        enrich_media_details(item, b'partial', body_complete=False)
        self.assertEqual(item.estimatedSize, 675064)
        self.assertFalse(item.metadata['size_is_estimate'])
        self.assertEqual(item.container, 'MP4')

    def test_direct_complete_response_length_must_match_body(self):
        item = candidate(http_status=200)
        enrich_media_details(item, b'abcd', body_complete=True, content_length=4)
        self.assertEqual(item.estimatedSize, 4)
        other = candidate(http_status=200)
        enrich_media_details(other, b'abcd', body_complete=True, content_length=5)
        self.assertIsNone(other.estimatedSize)

    def test_206_uses_total_and_never_fragment_content_length(self):
        item = candidate(http_status=206, metadata={'response_headers': {
            'Content-Range': 'bytes 100-199/1000', 'Content-Length': '100'}})
        enrich_media_details(item, bytes(100), body_complete=True)
        self.assertEqual(item.estimatedSize, 1000)
        self.assertEqual(item.metadata['detail_sources']['estimatedSize'], 'http-content-range')
        self.assertFalse(item.metadata['size_is_estimate'])

    def test_206_fragment_only_invalid_or_conflicting_range_is_unknown(self):
        for headers in ({'Content-Length': '100'}, {'Content-Range': 'bytes 0-99/*'},
                        {'Content-Range': 'bytes 100-99/1000'},
                        {'Content-Range': 'bytes 0-99/1000', 'Content-Length': '50'},
                        {'Content-Range': 'bytes 0-99/1000', 'Content-Length': 'invalid'}):
            with self.subTest(headers=headers):
                item = candidate(http_status=206, metadata={'response_headers': headers})
                enrich_media_details(item)
                self.assertIsNone(item.estimatedSize)

    def test_compressed_response_length_is_not_media_length(self):
        for status in (200, 206):
            item = candidate(http_status=status, metadata={'response_headers': {
                'Content-Length': '100', 'Content-Range': 'bytes 0-99/1000', 'Content-Encoding': 'gzip'}})
            enrich_media_details(item)
            self.assertIsNone(item.estimatedSize)

    def test_manifest_content_beats_direct_label_for_http_size(self):
        item = candidate(media_type='Direct', http_status=200,
                         metadata={'response_headers': {'Content-Length': '50'}})
        enrich_media_details(item)
        self.assertEqual(item.estimatedSize, 50)
        enrich_media_details(item, b'#EXTM3U\n#EXTINF:10,\nsegment.png\n#EXT-X-ENDLIST\n')
        self.assertIsNone(item.estimatedSize)
        self.assertEqual(item.media_type, 'Direct')  # Details do not change dispatch.

    def test_captured_mp4_tracks_and_average_fps(self):
        item = candidate()
        enrich_media_details(item, mp4(), body_complete=True)
        self.assertEqual((item.width, item.height), (1920, 1080))
        self.assertEqual((item.videoCodec, item.audioCodec), ('avc1.640028', 'mp4a'))
        self.assertEqual((item.videoBitrate, item.audioBitrate), (2_000_000, 128_000))
        self.assertAlmostEqual(item.fps, 30000 / 1001)
        self.assertAlmostEqual(item.metadata['detail_duration'], 10.01)
        self.assertEqual(item.container, 'MP4')
        self.assertTrue(item.metadata['has_audio'])
        self.assertEqual(item.estimatedSize, round(10.01 * 2_128_000 / 8))
        self.assertTrue(item.metadata['size_is_estimate'])
        self.assertIsNone(item.duration)

    def test_complete_moov_with_incomplete_mdat_is_usable(self):
        item = candidate()
        data = mp4() + struct.pack('>I4s', 10_000_000, b'mdat') + bytes(16)
        enrich_media_details(item, data, body_complete=False)
        self.assertEqual((item.width, item.height), (1920, 1080))
        self.assertIsNotNone(item.fps)

    def test_mp4_version_one_and_large_atoms(self):
        item = candidate()
        enrich_media_details(item, mp4(version=1, moov_large=True), body_complete=True)
        self.assertEqual((item.width, item.height), (1920, 1080))
        self.assertAlmostEqual(item.metadata['detail_duration'], 10.01)

    def test_zero_size_moov_only_when_complete_capture(self):
        for complete, width in ((False, None), (True, 1920)):
            with self.subTest(complete=complete):
                item = candidate()
                enrich_media_details(item, mp4(moov_zero=True), body_complete=complete)
                self.assertEqual(item.width, width)

    def test_incomplete_or_malformed_moov_leaves_track_details_unknown(self):
        ftyp = atom(b'ftyp', b'isom' + bytes(4))
        for data in (mp4()[:-1], ftyp + atom(b'moov', b'bad'),
                     ftyp + atom(b'moov', struct.pack('>I4s', 2**32 - 1, b'trak')),
                     ftyp + struct.pack('>I4sQ', 1, b'moov', 2**64 - 1)):
            with self.subTest(data=data[-20:]):
                item = candidate()
                enrich_media_details(item, data, body_complete=True)
                self.assertEqual(item.container, 'MP4')
                self.assertIsNone(item.width)
                self.assertIsNone(item.videoCodec)
                self.assertIsNone(item.fps)

    def test_known_old_duration_not_overwritten_by_mp4(self):
        item = candidate(duration=20)
        enrich_media_details(item, mp4(), body_complete=True)
        self.assertEqual(item.duration, 20)
        self.assertNotIn('detail_duration', item.metadata)
        self.assertEqual(item.estimatedSize, 5_320_000)

    def test_actual_mp4_beats_dom_and_mime_on_refresh(self):
        item = candidate(resolution=(640, 360), content_type='video/webm')
        enrich_media_details(item, mp4(), body_complete=True)
        enrich_media_details(item)
        self.assertEqual((item.width, item.height), (1920, 1080))
        self.assertEqual(item.container, 'MP4')
        self.assertEqual(item.metadata['detail_sources']['width'], 'mp4-tkhd')

    def test_exact_http_size_beats_later_bandwidth_estimate(self):
        data = mp4()
        item = candidate(http_status=200, metadata={'response_headers': {'Content-Length': str(len(data))}})
        enrich_media_details(item, data, body_complete=True)
        item.metadata.pop('response_headers')
        enrich_media_details(item, data, body_complete=True)
        self.assertEqual(item.estimatedSize, len(data))
        self.assertFalse(item.metadata['size_is_estimate'])

    def test_no_container_guess_from_url_suffix(self):
        for suffix in ('.png', '.mp4', '.ts', '.m3u8'):
            item = candidate(request_context=RequestContext('https://media.example/media' + suffix))
            enrich_media_details(item)
            self.assertIsNone(item.container)

    def test_captured_bytes_have_bounded_input(self):
        item = candidate()
        enrich_media_details(item, mp4() + bytes(MAX_CAPTURE_BYTES), body_complete=True)
        self.assertIsNone(item.container)
        self.assertIsNone(item.width)

    def test_invalid_metadata_values_stay_unknown(self):
        for value in (None, True, -1, 0, float('nan'), float('inf'), 'NaN', '1/0', {}, []):
            with self.subTest(value=value):
                item = candidate(resolution=(value, value), metadata={'frame_rate': value,
                                 'video_bitrate': value, 'audio_bitrate': value, 'codecs': value})
                enrich_media_details(item)
                self.assertIsNone(item.width)
                self.assertIsNone(item.fps)
                self.assertIsNone(item.videoBitrate)
                self.assertIsNone(item.videoCodec)

    def test_malformed_existing_sources_do_not_crash(self):
        item = candidate(width=1920, height=1080, resolution=(1280, 720),
                         metadata={'detail_sources': {'width': [], 'height': {}}})
        enrich_media_details(item)
        self.assertEqual((item.width, item.height), (1920, 1080))

    def test_malformed_existing_optional_values_are_cleared(self):
        item = candidate(width=-1, height=float('inf'), fps=float('nan'), videoCodec='bad\ncodec',
                         audioCodec='x' * 100, videoBitrate=True, audioBitrate=-1,
                         estimatedSize=2**64, container='PNG')
        enrich_media_details(item)
        for field in ('width', 'height', 'fps', 'videoCodec', 'audioCodec',
                      'videoBitrate', 'audioBitrate', 'estimatedSize', 'container'):
            self.assertIsNone(getattr(item, field), field)

    def test_request_confidence_and_download_payload_are_preserved(self):
        context = RequestContext('https://media.example/index.png?sign=fixture',
                                 headers={'Referer': 'https://page.example/watch',
                                          'User-Agent': 'fixture', 'Range': 'bytes=0-'})
        item = candidate(request_context=context, media_type='HLS', duration=20,
                         resolution=(1280, 720), bandwidth=1_000_000, score=80,
                         confidenceScore=80, confidenceReasons=['当前播放器'], qualityScore=12,
                         manifest_text='#EXTM3U\n#EXTINF:20,\na.png\n#EXT-X-ENDLIST\n',
                         transport_url=context.url, manifest_url=context.url,
                         manifest_origin='embedded:png/roUd', validated=True,
                         metadata={'codecs': 'avc1,mp4a.40.2', 'frame_rate': 25})
        before_context = deepcopy(asdict(item.request_context))
        before_payload = deepcopy(item.download_payload())
        old = deepcopy((item.duration, item.resolution, item.bandwidth, item.score,
                        item.confidenceScore, item.confidenceReasons, item.qualityScore,
                        item.media_type, item.validated, item.confirmed))
        enrich_media_details(item, item.manifest_text.encode(), body_complete=True)
        self.assertEqual(asdict(item.request_context), before_context)
        self.assertEqual(item.download_payload(), before_payload)
        self.assertEqual((item.duration, item.resolution, item.bandwidth, item.score,
                          item.confidenceScore, item.confidenceReasons, item.qualityScore,
                          item.media_type, item.validated, item.confirmed), old)

    def test_existing_estimate_is_not_relabelled_as_exact_without_http_evidence(self):
        item = candidate(estimatedSize=1_000_000, metadata={'size_is_estimate': True})
        enrich_media_details(item)
        enrich_media_details(item)
        self.assertEqual(item.estimatedSize, 1_000_000)
        self.assertTrue(item.metadata['size_is_estimate'])
        item.http_status = 200
        item.metadata['response_headers'] = {'Content-Length': '1200000'}
        enrich_media_details(item)
        self.assertEqual(item.estimatedSize, 1_200_000)
        self.assertFalse(item.metadata['size_is_estimate'])


if __name__ == '__main__':
    unittest.main()
