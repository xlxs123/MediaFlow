"""字幕独立读取、实际轨道详情以及原音视频验证边界。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from core import library_validation as checks
from core.process_tree import NO_WINDOW
from core.tool_manager import APP_DIR, ToolInfo, get_tool_manager
from tests.track_fixture import TrackFixture
from ui.media_library import _status, track_details_text


def packet_result(*indices):
    return 0, json.dumps({'streams': [{'index': index, 'nb_read_packets': '2'} for index in indices]}).encode(), b''


class SubtitleReadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-library-track-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'media.mp4'
        self.path.write_bytes(b'unit bytes, actual media cases below')
        self.streams = [{'index': 3, 'codec_type': 'subtitle', 'codec_name': 'mov_text', 'language': 'eng'}]

    def check(self, streams=None):
        return checks.check_subtitle_tracks(self.path, self.streams if streams is None else streams,
                                            'ffmpeg.exe', 'ffprobe.exe', checks._Control(None, 10))

    def test_no_subtitle_does_not_launch_an_extra_process(self):
        with patch.object(checks, '_run') as run:
            result = self.check([])
        run.assert_not_called()
        self.assertEqual(result, {'status': 'none', 'count': 0, 'streams': []})

    def test_text_subtitle_is_decoded_separately_from_av(self):
        with patch.object(checks, '_run', side_effect=[packet_result(3),
                (0, b'progress=continue\nprogress=end\n', b'')]) as run:
            result = self.check()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['streams'], [{'index': 3, 'status': 'passed', 'method': 'text_decode'}])
        args = run.call_args_list[1].args[0]
        self.assertEqual(args[args.index('-map') + 1], '0:3')
        self.assertEqual(args[args.index('-c:s') + 1], 'ass')
        self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'file,pipe')
        self.assertNotIn('0:v', args)
        self.assertNotIn('0:a', args)

    def test_image_subtitles_only_read_packets_and_never_claim_full_decode(self):
        streams = [{'index': 3, 'codec_type': 'subtitle', 'codec_name': 'hdmv_pgs_subtitle'}]
        with patch.object(checks, '_run', return_value=packet_result(3)) as run:
            result = self.check(streams)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['status'], 'not_checked')
        self.assertEqual(result['streams'][0]['method'], 'packet_read')
        self.assertIn('未完成', result['reason'])

    def test_mixed_subtitles_keep_individual_reading_scope(self):
        streams = self.streams + [{'index': 4, 'codec_type': 'subtitle', 'codec_name': 'dvd_subtitle'}]
        with patch.object(checks, '_run', side_effect=[packet_result(3, 4), (0, b'progress=end\n', b'')]):
            result = self.check(streams)
        self.assertEqual(result['status'], 'not_checked')
        self.assertEqual([item['status'] for item in result['streams']], ['passed', 'not_checked'])

    def test_empty_subtitle_track_is_not_marked_valid(self):
        with patch.object(checks, '_run', return_value=(0,
                b'{"streams":[{"index":3,"nb_read_packets":"0"}]}', b'')):
            result = self.check()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('没有可读取', result['reason'])

    def test_decoder_errors_are_actionable_and_do_not_leak_raw_text(self):
        with patch.object(checks, '_run', side_effect=[packet_result(3),
                (1, b'', b'Invalid data https://private.test/sub?token=secret-value')]):
            result = self.check()
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('secret-value', json.dumps(result))
        self.assertNotIn('https://', json.dumps(result))

    def test_unavailable_packet_reader_decoder_is_not_reported_as_damage(self):
        with patch.object(checks, '_run', return_value=(1, b'', b'No decoder found for input stream')):
            result = self.check()
        self.assertEqual(result['status'], 'not_checked')
        self.assertTrue(result['incomplete'])

    def test_missing_decoder_or_unconfirmed_end_is_unverified(self):
        for response in ((1, b'', b'Unknown decoder subtitle'), (0, b'progress=continue\n', b'')):
            with self.subTest(response=response), patch.object(checks, '_run', side_effect=[packet_result(3), response]):
                result = self.check()
            self.assertEqual(result['status'], 'not_checked')
            self.assertTrue(result['incomplete'])

    def test_unreliable_packet_counts_are_not_accepted(self):
        for payload in (b'not json', b'{"streams":[]}',
                        b'{"streams":[{"index":3,"nb_read_packets":"N/A"}]}'):
            with self.subTest(payload=payload), patch.object(checks, '_run', return_value=(0, payload, b'')):
                result = self.check()
            self.assertEqual(result['status'], 'not_checked')
            self.assertTrue(result['incomplete'])

    def test_invalid_indices_cannot_reach_a_tool_command(self):
        for streams in ([{'index': '3'}], [{'index': -1}], [{'index': 3}, {'index': 3}]):
            with self.subTest(streams=streams), patch.object(checks, '_run') as run:
                with self.assertRaises(checks._Incomplete):
                    self.check(streams)
                run.assert_not_called()

    def test_shared_deadline_cancels_before_a_process_can_start(self):
        with patch.object(checks.subprocess, 'Popen') as launch:
            result = checks.check_subtitle_tracks(self.path, self.streams, 'ffmpeg.exe', 'ffprobe.exe',
                                                 checks._Control(None, 0))
        self.assertEqual(result['status'], 'not_checked')
        self.assertTrue(result['incomplete'])
        launch.assert_not_called()

    def test_revalidate_preserves_original_av_flow_and_reports_subtitle_failure(self):
        summary = {'format_name': 'mp4', 'duration': 2.0, 'size': self.path.stat().st_size,
            'video_streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'h264'}],
            'audio_streams': [{'index': 1, 'codec_type': 'audio', 'codec_name': 'aac'}],
            'subtitle_streams': self.streams}
        manager = Mock()
        manager.detect.return_value = ToolInfo('tool.exe', '9.0.2')
        previous = self.path.read_bytes()
        with patch.object(checks, 'get_tool_manager', return_value=manager), \
                patch.object(checks, 'validate_output', return_value=summary), \
                patch.object(checks, '_stream_details', return_value=[{'index': 0}, {'index': 1}]), \
                patch.object(checks, '_full_decode', return_value={'status': 'passed', 'streams_read': 2}) as decode, \
                patch.object(checks, 'check_subtitle_tracks', return_value={
                    'status': 'failed', 'count': 1, 'streams': [], 'reason': '文本字幕完整解码失败。'}):
            result = checks.revalidate_media(self.path)
        self.assertEqual(result['validation_state'], 'failed')
        self.assertEqual(result['validation']['full_decode']['status'], 'passed')
        self.assertEqual(decode.call_args.args[2], [{'index': 0}, {'index': 1}])
        self.assertEqual(self.path.read_bytes(), previous)


class TrackDetailsTextTests(unittest.TestCase):
    def test_actual_language_audio_fields_default_and_sidecar_status_are_readable(self):
        validation = {'video_streams': [{'index': 0, 'codec_name': 'h264'}],
            'audio_streams': [{'index': 1, 'codec_name': 'aac', 'language': 'eng', 'title': '<b>Original</b>',
                              'channels': 2, 'sample_rate': 48000, 'bit_rate': 128000,
                              'channel_layout': 'stereo', 'default': True}],
            'subtitle_streams': [{'index': 2, 'codec_name': 'mov_text', 'language': 'zho', 'default': False}],
            'full_decode': {'status': 'passed', 'streams_read': 2},
            'subtitle_check': {'status': 'passed', 'count': 1,
                'streams': [{'index': 2, 'status': 'passed', 'method': 'text_decode'}]},
            'subtitle_files': [{'path': 'G:/media/字幕.en.srt', 'language': 'en', 'format': 'srt', 'status': 'missing'}]}
        before = deepcopy(validation)
        with patch.object(Path, 'stat', side_effect=AssertionError('UI must not stat')):
            result = track_details_text(validation)
        for expected in ('英语 (eng)', '<b>Original</b>', '声道：2', '48 kHz', '128 kb/s', '默认：是',
                         '文本字幕完整解码通过', '已移动或丢失', 'G:/media/字幕.en.srt'):
            self.assertIn(expected, result)
        self.assertNotIn("{'", result)
        self.assertEqual(validation, before)

    def test_picture_subtitle_scope_is_explicit_in_details_and_row_status(self):
        validation = {'subtitle_streams': [{'index': 3, 'codec_name': 'hdmv_pgs_subtitle'}],
            'subtitle_check': {'status': 'not_checked', 'count': 1,
                'streams': [{'index': 3, 'status': 'not_checked', 'method': 'packet_read'}]}}
        self.assertIn('仅完成数据包读取，尚未完整解码', track_details_text(validation))
        self.assertIn('字幕待完整检查', _status({'task_state': 'completed', 'file_state': 'exists',
            'validation_state': 'valid', 'validation': validation}))

    def test_legacy_missing_fields_stay_unknown_and_offer_revalidation(self):
        result = track_details_text({'audio_streams': [{'codec_name': 'aac'}]})
        self.assertIn('未知语言', result)
        self.assertIn('采样率：未知', result)
        self.assertIn('流索引 未知', result)
        self.assertIn('旧记录尚未采集', result)
        self.assertIn('尚未完成完整读取', result)


class ActualLibraryTracksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manager = get_tool_manager()
        cls.ffmpeg, cls.ffprobe = manager.detect('ffmpeg', refresh=False), manager.detect('ffprobe', refresh=False)
        if not cls.ffmpeg.found or not cls.ffprobe.found:
            raise unittest.SkipTest('实际字幕读取测试需要现有 FFmpeg 和 ffprobe。')
        cls.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-library-tracks-real-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.fixture = TrackFixture(cls.root / 'fixtures', APP_DIR / 'tools')
        cls.fixture.generate()
        cls.mp4, cls.mkv = cls.root / 'subtitled.mp4', cls.root / 'subtitled.mkv'
        for destination, codec in ((cls.mp4, 'mov_text'), (cls.mkv, 'subrip')):
            args = [cls.ffmpeg.path, '-nostdin', '-v', 'error', '-n',
                '-i', str(cls.fixture.directory / 'multi.mp4'),
                '-i', str(cls.fixture.directory / 'en.srt'), '-i', str(cls.fixture.directory / 'ja.srt'),
                '-map', '0', '-map', '1:0', '-map', '2:0', '-c', 'copy', '-c:s', codec,
                '-metadata:s:s:0', 'language=eng', '-metadata:s:s:1', 'language=jpn', str(destination)]
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           creationflags=NO_WINDOW, timeout=30, check=True)

    def verify(self, path):
        return checks.revalidate_media(path, self.ffmpeg.path, self.ffprobe.path, timeout=45)

    def test_real_mp4_text_subtitles_and_both_audio_tracks_are_fully_read(self):
        before = hashlib.sha256(self.mp4.read_bytes()).hexdigest()
        result = self.verify(self.mp4)
        self.assertEqual(result['validation_state'], 'valid', result['validation_reason'])
        validation = result['validation']
        self.assertEqual(validation['full_decode']['streams_read'], 3)
        self.assertEqual(validation['subtitle_check']['status'], 'passed')
        self.assertEqual(validation['subtitle_check']['count'], 2)
        self.assertEqual({item['language'] for item in validation['subtitle_streams']}, {'eng', 'jpn'})
        self.assertTrue(all(item['method'] == 'text_decode' and item['status'] == 'passed'
                            for item in validation['subtitle_check']['streams']))
        self.assertEqual(hashlib.sha256(self.mp4.read_bytes()).hexdigest(), before)

    def test_real_mkv_subrip_uses_the_same_separate_reading_checks(self):
        result = self.verify(self.mkv)
        self.assertEqual(result['validation_state'], 'valid', result['validation_reason'])
        self.assertEqual(result['validation']['subtitle_check']['status'], 'passed')
        self.assertEqual({item['codec_name'] for item in result['validation']['subtitle_streams']}, {'subrip'})

    def test_real_video_without_subtitles_has_explicit_none_status(self):
        result = self.verify(self.fixture.directory / 'single.mp4')
        self.assertEqual(result['validation_state'], 'valid', result['validation_reason'])
        self.assertEqual(result['validation']['subtitle_check']['status'], 'none')
        self.assertEqual(result['validation']['full_decode']['streams_read'], 2)

    def test_real_standalone_subtitle_cannot_masquerade_as_av_media(self):
        path = self.fixture.directory / 'en.srt'
        original = path.read_bytes()
        result = self.verify(path)
        self.assertEqual(result['validation_state'], 'failed')
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
