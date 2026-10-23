from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtGui import QColor, QImage

from utils import thumbnails


class FakeProcess:
    def __init__(self, code=0):
        self.returncode = code
        self.terminated = self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('ffmpeg', timeout)
        return self.returncode


def jpeg(path):
    image = QImage(16, 10, QImage.Format.Format_RGB32)
    image.fill(QColor('#246c8a'))
    assert image.save(str(path), 'JPEG')


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.video = self.root / '视频 & $(private).mp4'
        self.original = b'local validated video placeholder for unit tests'
        self.video.write_bytes(self.original)
        self.tool = self.root / 'ffmpeg.exe'
        self.tool.write_bytes(b'unit fake executable')
        self.validation = {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'size': self.video.stat().st_size,
                           'duration': 8.0, 'video_streams': [{'index': 2, 'codec_type': 'video',
                                                             'codec_name': 'h264', 'width': 320, 'height': 180}],
                           'audio_streams': [{'index': 1, 'codec_type': 'audio', 'codec_name': 'aac'}]}
        self.cover = self.video.with_suffix('.jpg')

    def tearDown(self):
        self.assertEqual(self.video.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob('.mediaflow-thumbnail-*')), [])

    def generate(self, **kwargs):
        return thumbnails.generate_thumbnail(kwargs.pop('video_path', self.video),
                                               ffmpeg_path=kwargs.pop('ffmpeg_path', self.tool),
                                               validation=kwargs.pop('validation', self.validation), **kwargs)

    def launch(self, args, **kwargs):
        jpeg(Path(args[-1]))
        return FakeProcess()

    def test_first_frame_success_returns_sibling_jpeg_without_touching_video(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=self.launch) as process:
            result = self.generate()
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(result['path'], str(self.cover))
        self.assertEqual(result['attempts'], [1.0])
        self.assertTrue(thumbnails._valid_jpeg(self.cover))
        args, options = process.call_args
        args = args[0]
        self.assertEqual(options['shell'], False)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stdout'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(options['creationflags'], thumbnails.NO_WINDOW)
        self.assertEqual(args[args.index('-i') + 1], str(self.video))
        self.assertEqual(args[args.index('-map') + 1], '0:2')
        self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'file,pipe')
        self.assertEqual(args.count('-threads'), 2)
        self.assertNotEqual(args[-1], str(self.cover))

    def test_failed_first_seek_uses_three_seconds(self):
        calls = []

        def launch(args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                jpeg(Path(args[-1]))
            return FakeProcess()

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch):
            result = self.generate()
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(result['attempts'], [1.0, 3.0])

    def test_first_two_seeks_fail_then_midpoint_succeeds(self):
        calls = []

        def launch(args, **kwargs):
            calls.append(args)
            if len(calls) == 3:
                jpeg(Path(args[-1]))
            return FakeProcess()

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch):
            result = self.generate()
        self.assertEqual(result['status'], 'generated')
        self.assertEqual(result['attempts'], [1.0, 3.0, 4.0])

    def test_invalid_or_missing_duration_does_not_invent_a_midpoint(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen', return_value=FakeProcess()):
            for duration in (None, float('nan'), float('inf'), -1, 0, '8', True):
                with self.subTest(duration=duration):
                    validation = {**self.validation, 'duration': duration}
                    result = self.generate(validation=validation)
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['attempts'], [1.0, 3.0])
        self.assertFalse(self.cover.exists())

    def test_missing_or_invalid_validation_skips_without_decoding(self):
        variants = [None, {}, [], {'video_streams': []}, {**self.validation, 'video_streams': []},
                    {**self.validation, 'video_streams': 'bad'}, {**self.validation, 'size': True},
                    {**self.validation, 'format_name': 'https://secret.invalid/?token=private'}]
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            for validation in variants:
                with self.subTest(validation=validation):
                    result = self.generate(validation=validation)
                    self.assertEqual(result['status'], 'skipped')
                    self.assertEqual(result['attempts'], [])
            process.assert_not_called()

    def test_stale_validated_size_is_not_decoded(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            result = self.generate(validation={**self.validation, 'size': len(self.original) + 1})
        self.assertEqual(result['status'], 'skipped')
        process.assert_not_called()

    def test_audio_only_and_mp3_skip_cover_generation(self):
        audio = {**self.validation, 'format_name': 'mp3', 'video_streams': []}
        mp3 = self.root / 'audio.mp3'
        mp3.write_bytes(self.original)
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            self.assertEqual(self.generate(validation=audio)['status'], 'skipped')
            self.assertEqual(self.generate(video_path=mp3)['status'], 'skipped')
            process.assert_not_called()
        self.assertFalse(mp3.with_suffix('.jpg').exists())

    def test_cover_art_is_not_used_as_real_video(self):
        validation = deepcopy(self.validation)
        validation['video_streams'] = [{'index': 0, 'codec_type': 'video', 'codec_name': 'mjpeg',
                                        'disposition': {'attached_pic': 1}}]
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            self.assertEqual(self.generate(validation=validation)['status'], 'skipped')
            process.assert_not_called()

    def test_real_video_index_is_used_instead_of_attached_cover(self):
        validation = deepcopy(self.validation)
        validation['video_streams'].insert(0, {'index': 0, 'codec_type': 'video', 'codec_name': 'mjpeg',
                                               'attached_pic': True})
        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=self.launch) as process:
            self.assertEqual(self.generate(validation=validation)['status'], 'generated')
        args = process.call_args.args[0]
        self.assertEqual(args[args.index('-map') + 1], '0:2')

    def test_missing_stream_index_uses_video_selector(self):
        validation = deepcopy(self.validation)
        validation['video_streams'][0].pop('index')
        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=self.launch) as process:
            self.assertEqual(self.generate(validation=validation)['status'], 'generated')
        args = process.call_args.args[0]
        self.assertEqual(args[args.index('-map') + 1], '0:v:0')

    def test_invalid_stream_summary_is_not_decoded(self):
        variants = [None, {'codec_type': 'audio', 'codec_name': 'aac'},
                    {'codec_type': 'video', 'codec_name': 'png'},
                    {'codec_type': 'video', 'codec_name': ''},
                    {'codec_type': 'video', 'codec_name': 'h264', 'index': -1},
                    {'codec_type': 'video', 'codec_name': 'h264', 'index': '2'}]
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            for stream in variants:
                with self.subTest(stream=stream):
                    self.assertEqual(self.generate(validation={**self.validation, 'video_streams': [stream]})['status'], 'skipped')
            process.assert_not_called()

    def test_png_or_image_container_cannot_enter_thumbnail_decode(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            for container in ('png_pipe', 'image2', 'jpeg_pipe', 'gif'):
                with self.subTest(container=container):
                    self.assertEqual(self.generate(validation={**self.validation, 'format_name': container})['status'], 'skipped')
            process.assert_not_called()

    def test_existing_real_jpeg_is_preserved_without_ffmpeg(self):
        jpeg(self.cover)
        original = self.cover.read_bytes()
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            result = self.generate()
        self.assertEqual(result['status'], 'existing')
        self.assertEqual(result['path'], str(self.cover))
        self.assertEqual(result['attempts'], [])
        self.assertEqual(self.cover.read_bytes(), original)
        process.assert_not_called()

    def test_unknown_existing_cover_is_never_overwritten(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            for original in (b'not a JPEG', b'\xff\xd8fake\xff\xd9', b'\x89PNG\r\n\x1a\n'):
                with self.subTest(original=original):
                    self.cover.write_bytes(original)
                    result = self.generate()
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['attempts'], [])
                    self.assertEqual(self.cover.read_bytes(), original)
            process.assert_not_called()

    def test_existing_directory_is_not_replaced(self):
        self.cover.mkdir()
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            self.assertEqual(self.generate()['status'], 'failed')
        self.assertTrue(self.cover.is_dir())
        process.assert_not_called()

    def test_existing_cover_created_during_generation_wins(self):
        original = []

        def launch(args, **kwargs):
            jpeg(self.cover)
            original.append(self.cover.read_bytes())
            return self.launch(args, **kwargs)

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch):
            result = self.generate()
        self.assertEqual(result['status'], 'existing')
        self.assertEqual(result['attempts'], [1.0])
        self.assertEqual(self.cover.read_bytes(), original[0])

    def test_unrecognized_cover_created_during_generation_is_preserved(self):
        def launch(args, **kwargs):
            self.cover.write_bytes(b'new external cover')
            return self.launch(args, **kwargs)

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch):
            result = self.generate()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.cover.read_bytes(), b'new external cover')

    def test_source_jpg_name_cannot_be_overwritten(self):
        named_jpg = self.root / 'video.jpg'
        named_jpg.write_bytes(self.original)
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            result = self.generate(video_path=named_jpg)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(named_jpg.read_bytes(), self.original)
        process.assert_not_called()

    def test_nonzero_exit_is_not_success_even_if_it_left_a_jpeg(self):
        def launch(args, **kwargs):
            jpeg(Path(args[-1]))
            return FakeProcess(1)

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch):
            result = self.generate()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['attempts'], [1.0, 3.0, 4.0])
        self.assertFalse(self.cover.exists())

    def test_successful_exit_with_invalid_image_is_not_success(self):
        def launch(args, **kwargs):
            Path(args[-1]).write_bytes(b'\xff\xd8not a decoded JPEG\xff\xd9')
            return FakeProcess()

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch):
            result = self.generate()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(self.cover.exists())

    def test_early_cancellation_skips_everything(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            result = self.generate(cancel_event=event)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['attempts'], [])
        process.assert_not_called()

    def test_running_ffmpeg_is_terminated_on_cancellation(self):
        event = threading.Event()
        running = FakeProcess(None)

        def launch(*args, **kwargs):
            event.set()
            return running

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch):
            result = self.generate(cancel_event=event)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(running.terminated)
        self.assertFalse(self.cover.exists())

    def test_cancellation_during_image_check_prevents_publication(self):
        event = threading.Event()
        validate = thumbnails._valid_jpeg

        def check(path):
            valid = validate(path)
            event.set()
            return valid

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=self.launch), \
                mock.patch.object(thumbnails, '_valid_jpeg', side_effect=check):
            result = self.generate(cancel_event=event)
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(self.cover.exists())

    def test_timeout_stops_process_and_cleans_owned_temporary_file(self):
        running = FakeProcess(None)
        started = time.monotonic()
        with mock.patch.object(thumbnails.subprocess, 'Popen', return_value=running):
            result = self.generate(timeout=0.01)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('超时', result['message'])
        self.assertEqual(result['attempts'], [1.0])
        self.assertTrue(running.terminated)
        self.assertLess(time.monotonic() - started, 1)

    def test_kill_is_used_if_process_does_not_terminate(self):
        running = FakeProcess(None)
        event = threading.Event()

        def launch(*args, **kwargs):
            event.set()
            return running

        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(running, 'terminate', side_effect=OSError('stop failed')):
            result = self.generate(cancel_event=event)
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(running.killed)

    def test_process_start_failure_is_safe_and_preserves_video(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen', side_effect=OSError('SECRET private path')):
            result = self.generate()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['attempts'], [1.0])
        self.assertNotIn('SECRET', result['message'])
        self.assertNotIn('private', result['message'])

    def test_invalid_timeout_is_rejected_without_starting_a_process(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            for timeout in (0, -1, float('inf'), float('nan'), '20', True):
                with self.subTest(timeout=timeout):
                    self.assertEqual(self.generate(timeout=timeout)['status'], 'failed')
            process.assert_not_called()

    def test_remote_missing_and_directory_inputs_fail_without_launch(self):
        with mock.patch.object(thumbnails.subprocess, 'Popen') as process:
            for path in ('https://example.invalid/private.mp4', 'file:///private.mp4',
                         self.root / 'missing.mp4', self.root):
                with self.subTest(path=path):
                    self.assertEqual(self.generate(video_path=path)['status'], 'failed')
            self.assertEqual(self.generate(ffmpeg_path=self.root / 'missing-ffmpeg.exe')['status'], 'failed')
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
