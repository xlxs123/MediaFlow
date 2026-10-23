import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zlib

from core.errors import MediaCheckCancelled, MediaCheckError
from core import output_validator
from core.output_validator import validate_output
from core.process_tree import NO_WINDOW


def probe_data(format_name='mov,mp4,m4a,3gp,3g2,mj2', *, video=True, audio=False, codec='h264'):
    streams = []
    if video:
        streams.append({'index': 0, 'codec_type': 'video', 'codec_name': codec,
                        'width': 32, 'height': 32, 'disposition': {'attached_pic': 0}})
    if audio:
        streams.append({'index': 1, 'codec_type': 'audio', 'codec_name': 'aac'})
    return {'format': {'format_name': format_name, 'duration': '0.25'}, 'streams': streams}


def png_image(width, height):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    pixels = (b'\x00' + b'\x80\xa0\xc0' * width) * height
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


class OutputValidatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'result.mp4'
        self.path.write_bytes(b'local test file')
        self.finder = mock.patch.object(output_validator, '_find_ffprobe', return_value='ffprobe.exe')
        self.finder.start()
        self.addCleanup(self.finder.stop)

    def check(self, data, **kwargs):
        with mock.patch.object(output_validator, '_probe_file', return_value=data):
            return validate_output(self.path, **kwargs)

    def assert_code(self, code, callback):
        with self.assertRaises(MediaCheckError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_mp4_container_with_video_is_required(self):
        summary = self.check(probe_data())
        self.assertEqual(summary['format_name'], 'mov,mp4,m4a,3gp,3g2,mj2')
        self.assertEqual(summary['video_streams'][0]['codec_name'], 'h264')
        self.assertEqual(summary['size'], self.path.stat().st_size)

    def test_png_named_mp4_fails_and_is_preserved(self):
        original = self.path.read_bytes()
        error = self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data('png_pipe', codec='png')))
        self.assertEqual(error.diagnostic['format_name'], 'png_pipe')
        self.assertEqual(self.path.read_bytes(), original)

    def test_image_containers_are_rejected_regardless_of_extension(self):
        for name in ('image2', 'image2pipe', 'png_pipe', 'jpeg_pipe', 'webp_pipe', 'gif', 'apng'):
            with self.subTest(format=name):
                self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data(name), expected_extension='bin'))

    def test_non_mp4_container_cannot_pass_as_mp4(self):
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data('matroska,webm')))

    def test_png_remuxed_into_mp4_still_fails(self):
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data(codec='png')))

    def test_audio_only_mp4_fails_video_download(self):
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data(video=False, audio=True)))

    def test_attached_picture_does_not_count_as_video(self):
        data = probe_data(audio=True, codec='mjpeg')
        data['streams'][0]['disposition']['attached_pic'] = 1
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(data))

    def test_ordinary_mjpeg_video_in_mp4_is_allowed(self):
        self.assertEqual(self.check(probe_data(codec='mjpeg'))['video_streams'][0]['codec_name'], 'mjpeg')

    def test_video_and_cover_art_count_only_the_video(self):
        data = probe_data()
        data['streams'].append({'codec_type': 'video', 'codec_name': 'png', 'disposition': {'attached_pic': 1}})
        self.assertEqual(len(self.check(data)['video_streams']), 1)

    def test_webm_and_mkv_video_are_preserved(self):
        for extension in ('webm', 'mkv'):
            with self.subTest(extension=extension):
                self.assertEqual(len(self.check(probe_data('matroska,webm'), expected_extension=extension)['video_streams']), 1)

    def test_audio_formats_are_preserved(self):
        for extension, format_name in (('m4a', 'mov,mp4,m4a,3gp,3g2,mj2'), ('mp3', 'mp3'),
                                       ('opus', 'ogg'), ('ogg', 'ogg'), ('wav', 'wav'),
                                       ('flac', 'flac'), ('aac', 'aac')):
            with self.subTest(extension=extension):
                data = probe_data(format_name, video=False, audio=True)
                if extension == 'opus':
                    data['streams'][0]['codec_name'] = 'opus'
                self.assertEqual(len(self.check(data, expected_extension=extension)['audio_streams']), 1)

    def test_audio_only_webm_is_preserved(self):
        self.assertEqual(len(self.check(probe_data('matroska,webm', video=False, audio=True),
                                       expected_extension='webm')['audio_streams']), 1)

    def test_audio_output_requires_actual_audio(self):
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data(), expected_extension='m4a'))

    def test_audio_output_container_mismatch_fails(self):
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data('mp3', video=False, audio=True),
                                                               expected_extension='m4a'))

    def test_opus_output_requires_opus_codec(self):
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data('ogg', video=False, audio=True),
                                                               expected_extension='opus'))

    def test_unknown_extension_uses_actual_streams(self):
        self.assertEqual(len(self.check(probe_data('mpegts'), expected_extension='bin')['video_streams']), 1)

    def test_no_usable_stream_is_rejected(self):
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(probe_data(video=False), expected_extension='bin'))

    def test_only_data_or_subtitle_streams_are_not_media(self):
        data = probe_data(video=False)
        data['streams'] = [{'codec_type': 'data'}, {'codec_type': 'subtitle'}]
        self.assert_code('OUTPUT_NOT_VIDEO', lambda: self.check(data))

    def test_probe_summary_excludes_tags_paths_and_untrusted_text(self):
        data = probe_data(audio=True)
        data['format'].update({'tags': {'title': 'SECRET'}, 'filename': 'SECRET'})
        data['streams'][0].update({'tags': {'title': 'SECRET'}, 'codec_name': 'SECRET https://x/?token=s'})
        summary = self.check(data)
        self.assertNotIn('SECRET', json.dumps(summary))
        self.assertNotIn('token', json.dumps(summary))

    def test_malformed_probe_structure_is_clear_validation_error(self):
        for data in ({}, {'format': {'format_name': 'mp4'}, 'streams': 'bad'},
                     {'format': {'format_name': 'mp4'}, 'streams': [None]},
                     {'format': {'format_name': 'https://secret/'}, 'streams': []}):
            with self.subTest(data=data):
                self.assert_code('OUTPUT_VALIDATION_ERROR', lambda: self.check(data))

    def test_stream_count_is_bounded(self):
        data = probe_data()
        data['streams'] *= output_validator.MAX_STREAMS + 1
        self.assert_code('OUTPUT_VALIDATION_ERROR', lambda: self.check(data))

    def test_missing_or_empty_file_cannot_succeed(self):
        absent = self.path.parent / 'absent.mp4'
        self.assert_code('OUTPUT_VALIDATION_ERROR', lambda: validate_output(absent))
        self.path.write_bytes(b'')
        self.assert_code('OUTPUT_VALIDATION_ERROR', lambda: validate_output(self.path))

    def test_directory_and_remote_url_are_not_local_output_files(self):
        for path in (self.path.parent, 'https://example.test/result.mp4', 'file:///result.mp4'):
            with self.subTest(path=path):
                self.assert_code('OUTPUT_VALIDATION_ERROR', lambda: validate_output(path))

    def test_invalid_expected_extension_is_rejected(self):
        for extension in ('../mp4', 'mp4\x00', 123):
            with self.subTest(extension=extension):
                self.assert_code('OUTPUT_VALIDATION_ERROR', lambda: self.check(probe_data(), expected_extension=extension))

    def test_early_cancellation_skips_probe(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(output_validator, '_probe_file') as probe, self.assertRaises(MediaCheckCancelled):
            validate_output(self.path, cancel_event=event)
        probe.assert_not_called()

    def test_missing_probe_tool_is_not_success(self):
        with mock.patch.object(output_validator, '_find_ffprobe', side_effect=MediaCheckError('missing', 'OUTPUT_PROBE_UNAVAILABLE')):
            self.assert_code('OUTPUT_PROBE_UNAVAILABLE', lambda: validate_output(self.path))

    def test_post_probe_cancellation_is_observed(self):
        event = threading.Event()

        def probe(*args):
            event.set()
            return probe_data()

        with mock.patch.object(output_validator, '_probe_file', side_effect=probe), self.assertRaises(MediaCheckCancelled):
            validate_output(self.path, cancel_event=event)


class OutputProbeProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'name with spaces & $.mp4'
        self.path.write_bytes(b'not used by fake probe')
        self.original_popen = subprocess.Popen
        self.calls = []

    def execute_fake(self, script, **kwargs):
        def start(args, **popen_kwargs):
            self.calls.append((args, popen_kwargs))
            return self.original_popen([sys.executable, '-c', script], **popen_kwargs)

        with mock.patch.object(output_validator, '_find_ffprobe', return_value='ffprobe.exe'), \
                mock.patch.object(output_validator.subprocess, 'Popen', side_effect=start):
            return validate_output(self.path, **kwargs)

    def assert_error(self, script, code='OUTPUT_VALIDATION_ERROR'):
        with self.assertRaises(MediaCheckError) as error:
            self.execute_fake(script)
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_shell_is_disabled_and_media_protocols_are_bounded(self):
        self.execute_fake('print(' + repr(json.dumps(probe_data())) + ')')
        args, options = self.calls[0]
        self.assertFalse(options['shell'])
        self.assertEqual(options['creationflags'], NO_WINDOW)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(args[-1], str(self.path.resolve()))
        self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'file,pipe')
        self.assertNotIn('-show_format', args)
        self.assertNotIn('-show_streams', args)

    def test_exit_zero_with_invalid_json_cannot_succeed(self):
        self.assert_error('print("not json")')

    def test_invalid_utf8_cannot_succeed(self):
        self.assert_error('import sys; sys.stdout.buffer.write(b"\\xff\\xfe")')

    def test_json_scalar_cannot_succeed(self):
        self.assert_error('print("0")')

    def test_nonzero_exit_is_failed_without_raw_stderr(self):
        error = self.assert_error('import sys; sys.stderr.write("Cookie: SECRET"); sys.exit(2)')
        self.assertNotIn('SECRET', str(error) + repr(error.diagnostic))

    def test_stdout_size_is_bounded(self):
        self.assert_error('import sys; sys.stdout.write("x" * ' + str(output_validator.MAX_PROBE_OUTPUT + 8192) + ')')

    def test_probe_timeout_terminates_process(self):
        with mock.patch.object(output_validator, 'PROBE_TIMEOUT', 0.15):
            self.assert_error('import time; time.sleep(10)')

    def test_inflight_cancellation_terminates_process(self):
        event = threading.Event()
        timer = threading.Timer(0.15, event.set)
        timer.start()
        try:
            with self.assertRaises(MediaCheckCancelled):
                self.execute_fake('import time; time.sleep(10)', cancel_event=event)
        finally:
            timer.cancel()

    def test_unlaunchable_probe_is_failed(self):
        with mock.patch.object(output_validator, '_find_ffprobe', return_value='ffprobe.exe'), \
                mock.patch.object(output_validator.subprocess, 'Popen', side_effect=OSError('SECRET')):
            with self.assertRaises(MediaCheckError) as error:
                validate_output(self.path)
        self.assertEqual(error.exception.code, 'OUTPUT_VALIDATION_ERROR')
        self.assertNotIn('SECRET', str(error.exception))


class OutputProbeLocationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.name = 'ffprobe.exe' if os.name == 'nt' else 'ffprobe'
        self.app = self.root / 'app'
        (self.app / 'tools').mkdir(parents=True)
        self.app_probe = self.app / 'tools' / self.name
        self.app_probe.write_bytes(b'probe')
        self.app_patch = mock.patch.object(output_validator, 'APP_DIR', self.app)
        self.app_patch.start()
        self.addCleanup(self.app_patch.stop)
        self.path_patch = mock.patch.object(output_validator.shutil, 'which', return_value=None)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_explicit_probe_has_priority(self):
        preferred = self.root / self.name
        preferred.write_bytes(b'preferred')
        self.assertEqual(output_validator._find_ffprobe(preferred), str(preferred.resolve()))

    def test_portable_probe_precedes_unconfigured_ffmpeg_sibling(self):
        sibling = self.root / self.name
        sibling.write_bytes(b'sibling')
        self.assertEqual(output_validator._find_ffprobe(ffmpeg_path=self.root / 'ffmpeg.exe'), str(self.app_probe.resolve()))

    def test_project_tools_are_used_before_path(self):
        with mock.patch.object(output_validator.shutil, 'which', return_value=str(self.root / self.name)):
            self.assertEqual(output_validator._find_ffprobe(), str(self.app_probe.resolve()))

    def test_path_is_final_fallback(self):
        probe = self.root / self.name
        probe.write_bytes(b'path probe')
        with mock.patch.object(output_validator, 'APP_DIR', self.root / 'absent-app'), \
                mock.patch.object(output_validator.shutil, 'which', return_value=str(probe)):
            self.assertEqual(output_validator._find_ffprobe(), str(probe.resolve()))

    def test_missing_tool_fails_closed(self):
        with mock.patch.object(output_validator, 'APP_DIR', self.root / 'absent-app'), self.assertRaises(MediaCheckError) as error:
            output_validator._find_ffprobe()
        self.assertEqual(error.exception.code, 'OUTPUT_PROBE_UNAVAILABLE')


class ActualPngOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffprobe = output_validator._find_ffprobe()
        except MediaCheckError:
            raise unittest.SkipTest('实际 PNG 回归需要 ffprobe；当前环境未提供。')

    def assert_actual_png(self, width, height):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'signed-index-saved-as-video.mp4'
            data = png_image(width, height)
            path.write_bytes(data)
            with self.assertRaises(MediaCheckError) as error:
                validate_output(path, ffprobe_path=self.ffprobe, expected_extension='mp4')
            self.assertEqual(error.exception.code, 'OUTPUT_NOT_VIDEO')
            self.assertEqual(error.exception.diagnostic['format_name'], 'png_pipe')
            self.assertEqual(error.exception.diagnostic['video_streams'][0]['codec_name'], 'png')
            self.assertEqual(error.exception.diagnostic['video_streams'][0]['width'], width)
            self.assertEqual(error.exception.diagnostic['video_streams'][0]['height'], height)
            self.assertEqual(path.read_bytes(), data)

    def test_real_320_by_192_png_named_mp4(self):
        self.assert_actual_png(320, 192)

    def test_real_864_by_864_png_named_mp4(self):
        self.assert_actual_png(864, 864)


class ActualMediaOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.ffprobe = output_validator._find_ffprobe()
        except MediaCheckError:
            raise unittest.SkipTest('实际媒体回归需要 ffprobe。')
        filename = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
        local_ffmpeg = Path(cls.ffprobe).parent / filename
        cls.ffmpeg = str(local_ffmpeg) if local_ffmpeg.is_file() else shutil.which('ffmpeg')
        if not cls.ffmpeg:
            raise unittest.SkipTest('实际视频/音频回归需要 ffmpeg 生成本地媒体。')
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)

    def generate(self, extension, codec, *, audio=False):
        path = self.root / ('audio-' if audio else 'video-') / (codec + '.' + extension)
        path.parent.mkdir(exist_ok=True)
        source = 'sine=frequency=880:sample_rate=48000:duration=0.2' if audio else 'color=c=blue:s=32x32:r=5:d=0.4'
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', source,
                '-c:a' if audio else '-c:v', codec, '-threads', '1', str(path)]
        result = subprocess.run(args, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
        return path

    def test_real_mp4_video(self):
        path = self.generate('mp4', 'mpeg4')
        summary = validate_output(path, ffprobe_path=self.ffprobe)
        self.assertTrue(set(summary['format_name'].split(',')) & output_validator.MP4_FORMATS)
        self.assertEqual(summary['video_streams'][0]['codec_type'], 'video')

    def test_real_mjpeg_mp4_video_is_not_mistaken_for_still_image(self):
        path = self.generate('mp4', 'mjpeg')
        self.assertEqual(validate_output(path, ffprobe_path=self.ffprobe)['video_streams'][0]['codec_name'], 'mjpeg')

    def test_real_webm_video(self):
        path = self.generate('webm', 'libvpx')
        self.assertTrue(validate_output(path, ffprobe_path=self.ffprobe)['video_streams'])

    def test_real_mkv_video(self):
        path = self.generate('mkv', 'ffv1')
        self.assertTrue(validate_output(path, ffprobe_path=self.ffprobe)['video_streams'])

    def test_real_audio_outputs_keep_working(self):
        for extension, codec in (('m4a', 'aac'), ('mp3', 'libmp3lame'), ('opus', 'libopus'),
                                  ('ogg', 'libvorbis'), ('wav', 'pcm_s16le'), ('flac', 'flac'), ('aac', 'aac')):
            with self.subTest(extension=extension):
                path = self.generate(extension, codec, audio=True)
                self.assertTrue(validate_output(path, ffprobe_path=self.ffprobe)['audio_streams'])

    def test_real_audio_only_mp4_is_not_successful_video(self):
        path = self.generate('mp4', 'aac', audio=True)
        with self.assertRaises(MediaCheckError) as error:
            validate_output(path, ffprobe_path=self.ffprobe)
        self.assertEqual(error.exception.code, 'OUTPUT_NOT_VIDEO')


if __name__ == '__main__':
    unittest.main()
