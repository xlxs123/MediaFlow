from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import library_validation as checks
from core.errors import MediaCheckCancelled, MediaCheckError
from core.process_tree import NO_WINDOW
from core.tool_manager import ToolInfo, get_tool_manager


class LibraryFileStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'media.mp4'
        self.path.write_bytes(b'fixture media')

    def test_existing_file_is_readable_and_is_unchanged(self):
        before = self.path.read_bytes()
        result = checks.file_status(self.path)
        self.assertEqual(result['file_state'], 'exists')
        self.assertEqual(result['size'], len(before))
        self.assertGreater(result['mtime_ns'], 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_and_directory_are_distinct(self):
        self.assertEqual(checks.file_status(self.root / 'missing.mp4')['file_state'], 'missing')
        result = checks.file_status(self.root)
        self.assertEqual(result['file_state'], 'inaccessible')
        self.assertIn('普通文件', result['message'])

    def test_permission_error_is_not_reported_as_missing(self):
        with mock.patch.object(Path, 'open', side_effect=PermissionError):
            result = checks.file_status(self.path)
        self.assertEqual(result['file_state'], 'inaccessible')
        self.assertIn('读取权限', result['message'])

    def test_url_and_invalid_path_are_not_accessed(self):
        with mock.patch.object(Path, 'stat') as stat:
            for path in ('https://example.test/media.mp4?token=private', '\x00', None, ''):
                self.assertEqual(checks.file_status(path)['file_state'], 'inaccessible')
        stat.assert_not_called()


class LibraryRevalidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'media.mp4'
        self.path.write_bytes(b'fixture media')
        self.manager = mock.Mock()
        self.manager.detect.side_effect = lambda name, *args, **kwargs: ToolInfo(name + '.exe', '9.0.2')
        self.manager_patch = mock.patch.object(checks, 'get_tool_manager', return_value=self.manager)
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
        self.summary = {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'size': self.path.stat().st_size,
                        'duration': 2.0, 'video_streams': [{'codec_type': 'video', 'codec_name': 'h264',
                                                         'index': 0, 'width': 320, 'height': 180}],
                        'audio_streams': [{'codec_type': 'audio', 'codec_name': 'aac', 'index': 1}]}

    def validation(self):
        return mock.patch.object(checks, 'validate_output', return_value=self.summary)

    def test_missing_file_never_invokes_tools(self):
        result = checks.revalidate_media(self.path.with_name('missing.mp4'))
        self.assertEqual(result['file_state'], 'missing')
        self.assertEqual(result['validation_state'], 'unverified')
        self.manager.detect.assert_not_called()

    def test_missing_probe_is_unverified_not_damaged(self):
        self.manager.detect.side_effect = None
        self.manager.detect.return_value = ToolInfo(error='ffprobe 不存在。', status='missing')
        result = checks.revalidate_media(self.path)
        self.assertEqual(result['validation_state'], 'unverified')
        self.assertIn('ffprobe', result['validation_reason'])

    def test_missing_encoder_keeps_structural_summary_but_is_unverified(self):
        self.manager.detect.side_effect = [ToolInfo('ffprobe.exe', '9.0.2'),
                                         ToolInfo(error='ffmpeg 不存在。', status='missing')]
        with self.validation():
            result = checks.revalidate_media(self.path)
        self.assertEqual(result['validation_state'], 'unverified')
        self.assertEqual(result['duration'], 2.0)
        self.assertEqual(result['video_codec'], 'h264')
        self.assertNotIn('full_decode', result['validation'])

    def test_cancelled_and_timeout_are_unverified(self):
        event = threading.Event()
        event.set()
        cancelled = checks.revalidate_media(self.path, cancel_event=event)
        self.assertEqual(cancelled['validation_state'], 'unverified')
        self.assertIn('取消', cancelled['validation_reason'])
        self.manager.detect.assert_not_called()

        def timed_probe(*args, **kwargs):
            kwargs['cancel_event'].deadline = time.monotonic() - 1
            raise MediaCheckCancelled('cancelled')
        with mock.patch.object(checks, 'validate_output', side_effect=timed_probe):
            timed = checks.revalidate_media(self.path)
        self.assertEqual(timed['validation_state'], 'unverified')
        self.assertIn('超时', timed['validation_reason'])

    def test_structural_tool_failure_and_corrupt_file_have_different_states(self):
        for error, expected in ((MediaCheckError('ffprobe 无法启动。'), 'unverified'),
                                (MediaCheckError('输出媒体探测超时。'), 'unverified'),
                                (MediaCheckError('ffprobe 无法解析输出文件。'), 'failed'),
                                (MediaCheckError('输出实际为图片容器。', 'OUTPUT_NOT_VIDEO'), 'failed')):
            with self.subTest(error=str(error)), mock.patch.object(checks, 'validate_output', side_effect=error):
                result = checks.revalidate_media(self.path)
                self.assertEqual(result['validation_state'], expected)

    def test_full_decode_and_metadata_are_returned(self):
        decode = {'status': 'passed', 'streams_read': 2, 'progress_end': True, 'decoded_frames': 60}
        with self.validation(), mock.patch.object(checks, '_stream_details', return_value=[]), \
                mock.patch.object(checks, '_full_decode', return_value=decode):
            result = checks.revalidate_media(self.path)
        self.assertEqual(result['validation_state'], 'valid')
        self.assertEqual(result['validation']['full_decode'], decode)
        self.assertEqual((result['width'], result['height'], result['media_type']), (320, 180, 'video'))

    def test_decode_failure_does_not_change_original_file(self):
        original = self.path.read_bytes()
        with self.validation(), mock.patch.object(checks, '_stream_details', return_value=[]), \
                mock.patch.object(checks, '_full_decode', side_effect=checks._InvalidMedia('音频数据损坏。')):
            result = checks.revalidate_media(self.path)
        self.assertEqual(result['validation_state'], 'failed')
        self.assertIn('音频', result['validation_reason'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_file_modified_during_verification_is_not_marked_valid(self):
        def decode(*args):
            self.path.write_bytes(b'a different file during verification')
            return {'status': 'passed'}
        with self.validation(), mock.patch.object(checks, '_stream_details', return_value=[]), \
                mock.patch.object(checks, '_full_decode', side_effect=decode):
            result = checks.revalidate_media(self.path)
        self.assertEqual(result['validation_state'], 'unverified')
        self.assertIn('发生变化', result['validation_reason'])

    def test_actual_timebase_is_preserved_for_each_video_stream(self):
        streams = [{'index': 0, 'codec_type': 'video', 'time_base': '1:16000', 'nb_frames': 60},
                   {'index': 2, 'codec_type': 'audio', 'time_base': '1:48000'},
                   {'index': 3, 'codec_type': 'video', 'time_base': '1:90000'}]
        with mock.patch.object(checks, '_run', return_value=(0, b'frame=60\ndrop_frames=0\nprogress=end\n', b'')) as run:
            result = checks._full_decode(self.path, 'ffmpeg.exe', streams, checks._Control(None, 5))
        args = run.call_args.args[0]
        for name, value in (('-enc_time_base:v:0', '1:16000'), ('-enc_time_base:v:1', '1:90000'),
                            ('-fps_mode:v:0', 'passthrough'), ('-fps_mode:v:1', 'passthrough')):
            self.assertEqual(args[args.index(name) + 1], value)
        self.assertEqual(result['streams_read'], 3)
        self.assertNotIn('-1', args)

    def test_incomplete_frame_count_and_decoder_errors_are_not_accepted(self):
        streams = [{'index': 0, 'codec_type': 'video', 'time_base': '1:16000', 'nb_frames': 60}]
        for code, output, errors in ((0, b'frame=59\nprogress=end\n', b''),
                                      (0, b'progress=end\n', b'invalid data secret-url?token=private')):
            with self.subTest(errors=bool(errors)), mock.patch.object(checks, '_run', return_value=(code, output, errors)):
                with self.assertRaises(checks._InvalidMedia) as caught:
                    checks._full_decode(self.path, 'ffmpeg.exe', streams, checks._Control(None, 5))
                self.assertNotIn('private', str(caught.exception))

    def test_unavailable_decoder_is_not_reported_as_corruption(self):
        streams = [{'index': 0, 'codec_type': 'video', 'time_base': '1:16000'}]
        with mock.patch.object(checks, '_run', return_value=(1, b'', b'No decoder found for input stream #0:0')):
            with self.assertRaises(checks._Incomplete):
                checks._full_decode(self.path, 'ffmpeg.exe', streams, checks._Control(None, 5))

    def test_file_removed_during_decode_keeps_missing_state(self):
        def decode(*args):
            self.path.unlink()
            raise checks._InvalidMedia('无法读取媒体。')
        with self.validation(), mock.patch.object(checks, '_stream_details', return_value=[]), \
                mock.patch.object(checks, '_full_decode', side_effect=decode):
            result = checks.revalidate_media(self.path)
        self.assertEqual(result['validation_state'], 'unverified')
        self.assertEqual(result['file_state'], 'missing')


class LibraryProcessControlTests(unittest.TestCase):
    def test_hung_process_is_terminated_on_timeout(self):
        actual_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            children.append(process)
            return process
        started = time.monotonic()
        with mock.patch.object(checks.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaises(checks._Incomplete) as caught:
                checks._run([sys.executable, '-c', 'import time;time.sleep(20)'], checks._Control(None, 0.2))
        self.assertIn('超时', str(caught.exception))
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_hung_process_is_terminated_on_cancel(self):
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(checks._Incomplete) as caught:
            checks._run([sys.executable, '-c', 'import time;time.sleep(20)'], checks._Control(event, 10))
        self.assertIn('取消', str(caught.exception))
        self.assertLess(time.monotonic() - started, 5)

    def test_late_process_launch_remains_owned_and_is_cleaned_up(self):
        actual_popen = subprocess.Popen
        created, finished = threading.Event(), threading.Event()
        children = []
        def launch(*args, **kwargs):
            time.sleep(0.3)
            process = actual_popen(*args, **kwargs)
            children.append(process)
            created.set()
            return process
        actual_stop = checks._stop
        def stop(*args, **kwargs):
            try:
                return actual_stop(*args, **kwargs)
            finally:
                finished.set()
        with mock.patch.object(checks.subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(checks, '_stop', side_effect=stop):
            with self.assertRaises(checks._Incomplete):
                checks._run([sys.executable, '-c', 'import time;time.sleep(20)'], checks._Control(None, 0.1))
            self.assertTrue(created.wait(3))
            self.assertTrue(finished.wait(3))
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))


class LibraryActualMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manager = get_tool_manager()
        cls.ffprobe, cls.ffmpeg = manager.detect('ffprobe', refresh=False), manager.detect('ffmpeg', refresh=False)
        if not cls.ffprobe.found or not cls.ffmpeg.found:
            raise unittest.SkipTest('实际媒体测试需要现有 FFmpeg 和 ffprobe。')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.video = cls.root / 'video.mp4'
        cls.audio = cls.root / 'audio.mp3'
        commands = [
            ['-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=660',
             '-t', '1.2', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(cls.video)],
            ['-f', 'lavfi', '-i', 'sine=frequency=660:duration=0.4', '-c:a', 'libmp3lame', str(cls.audio)],
        ]
        for command in commands:
            subprocess.run([cls.ffmpeg.path, '-v', 'error', '-nostdin', '-y', *command], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=20)

    def verify(self, path):
        return checks.revalidate_media(path, self.ffmpeg.path, self.ffprobe.path, timeout=20)

    def test_real_mp4_video_and_audio_tracks_are_fully_read(self):
        before = self.video.read_bytes()
        result = self.verify(self.video)
        self.assertEqual(result['validation_state'], 'valid', result['validation_reason'])
        self.assertEqual(result['media_type'], 'video')
        self.assertEqual((result['width'], result['height']), (96, 64))
        self.assertEqual(result['validation']['full_decode']['streams_read'], 2)
        self.assertEqual(result['validation']['full_decode']['decoded_frames'], 36)
        self.assertEqual(self.video.read_bytes(), before)

    def test_real_mp3_has_audio_and_no_video(self):
        result = self.verify(self.audio)
        self.assertEqual(result['validation_state'], 'valid', result['validation_reason'])
        self.assertEqual(result['media_type'], 'audio')
        self.assertEqual(result['container'], 'mp3')
        self.assertEqual(result['video_codec'], '')
        self.assertEqual(result['audio_codec'], 'mp3')
        self.assertEqual(result['validation']['full_decode']['streams_read'], 1)

    def test_fake_mp4_is_rejected_without_deleting_file(self):
        fake = self.root / 'not-media.mp4'
        fake.write_bytes(b'<html>not a media file</html>')
        result = self.verify(fake)
        self.assertEqual(result['validation_state'], 'failed')
        self.assertTrue(fake.exists())


if __name__ == '__main__':
    unittest.main()
