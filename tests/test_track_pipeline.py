from pathlib import Path
import json
import shutil
import tempfile
import unittest

from core.errors import MediaCheckError
from core.media_tracks import catalog, default_selection
from core.output_validator import validate_output
from core.recovery_workspace import RecoveryWorkspace, file_hash
from core.track_pipeline import check_compatibility, validate_subtitle
from tests.track_cases import discovered, download, request_for
from tests.track_fixture import TrackFixture


class SubtitleValidationTests(unittest.TestCase):
    def test_content_not_extension(self):
        with self.assertRaises(MediaCheckError):
            validate_subtitle(b'<html>Login</html>', 'srt')
        with self.assertRaises(MediaCheckError):
            validate_subtitle(b'not a subtitle', 'srt')
        self.assertIn('hello', validate_subtitle(b'1\n00:00:00,100 --> 00:00:01,000\nhello\n', 'srt'))

    def test_webm_will_not_reencode_h264_aac(self):
        with self.assertRaisesRegex(MediaCheckError, '不会自动转码'):
            check_compatibility({'video_streams': [{'codec_name': 'h264'}], 'audio_streams': [{'codec_name': 'aac'}]}, [], 'webm')

    def test_ttml_only_sidecar(self):
        with self.assertRaisesRegex(MediaCheckError, '独立保存'):
            check_compatibility({}, [{'format': 'ttml'}], 'mkv')


class RealTrackPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='MediaFlowTracks-')
        cls.root = Path(cls.temporary.name)
        cls.fixture = TrackFixture(cls.root / 'fixture')
        cls.fixture.generate()
        cls.fixture.start()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()
        cls.temporary.cleanup()

    def perform(self, kind, preferences=None, **kwargs):
        request, candidate = request_for(self.fixture, self.root / self._testMethodName, kind, preferences, **kwargs)
        result, logs = download(request)
        self.assertTrue(result['success'], result.get('error', '') + '\n' + '\n'.join(logs[-8:]))
        return request, result

    def test_direct_preserves_multiple_original_audio_tracks(self):
        _, result = self.perform('Direct')
        summary = result['output_validation'][0]
        self.assertEqual(len(summary['audio_streams']), 2)
        self.assertEqual({s.get('language') for s in summary['audio_streams']}, {'eng', 'jpn'})

    def test_dash_selected_japanese_only_and_mp4_soft_subtitle(self):
        _, result = self.perform('DASH', {'audio_language': 'ja', 'subtitle_mode': 'mux',
                                        'subtitle_languages': ['en'], 'container': 'mp4'})
        summary = result['output_validation'][0]
        self.assertEqual(len(summary['audio_streams']), 1)
        self.assertEqual(summary['audio_streams'][0].get('language'), 'jpn')
        self.assertEqual(len(summary['subtitle_streams']), 1)
        self.assertEqual(summary['subtitle_streams'][0].get('language'), 'eng')
        self.assertEqual(summary['subtitle_streams'][0]['codec_name'], 'mov_text')

    def test_dash_multiple_subtitles_mkv(self):
        _, result = self.perform('DASH', {'audio_language': 'en', 'subtitle_mode': 'mux',
                                        'subtitle_languages': ['en', 'ja'], 'container': 'mkv'})
        summary = result['output_validation'][0]
        self.assertEqual(len(summary['subtitle_streams']), 2)
        self.assertEqual({s.get('language') for s in summary['subtitle_streams']}, {'eng', 'jpn'})
        self.assertIn('matroska', summary['format_name'])

    def test_hls_alternate_japanese_audio_and_sidecar(self):
        request, result = self.perform('HLS', {'audio_language': 'ja', 'subtitle_mode': 'sidecar',
                                             'subtitle_languages': ['zh-Hans']})
        summary = result['output_validation'][0]
        self.assertEqual(len(summary['audio_streams']), 1)
        self.assertEqual(summary['audio_streams'][0].get('language'), 'jpn')
        self.assertEqual(summary['subtitle_streams'], [])
        sidecars = summary['subtitle_files']
        self.assertEqual(len(sidecars), 1)
        self.assertEqual(sidecars[0]['language'], 'zh-Hans')
        self.assertTrue(Path(sidecars[0]['path']).is_file())
        self.assertNotEqual(Path(sidecars[0]['path']), Path(request.output_path))

    def test_subtitle_failure_keeps_base_and_resume_reuses_it(self):
        request, _ = request_for(self.fixture, self.root / self._testMethodName, 'DASH',
            {'audio_language': 'ja', 'subtitle_mode': 'sidecar', 'subtitle_languages': ['en']})
        self.fixture.failures['/en.vtt'] = 1
        failed, _ = download(request)
        self.assertFalse(failed['success'])
        self.assertEqual(failed['error_code'], 'HTTP_ERROR')
        self.assertFalse(Path(request.output_path).exists())
        staged = list((Path(request.temporary_directory) / 'staging').glob('*.mp4'))
        self.assertTrue(staged)
        old_hash = file_hash(staged[0])
        before = len(self.fixture.requests)
        result, _ = download(request)
        self.assertTrue(result['success'], result['error'])
        media_requests = [v for v in self.fixture.requests[before:] if v['path'].endswith('.m4s')]
        self.assertEqual(media_requests, [])
        self.assertTrue(old_hash)

    def test_single_audio_no_extra_subtitles(self):
        _, result = self.perform('Single')
        self.assertEqual(len(result['output_validation'][0]['audio_streams']), 1)
        self.assertEqual(result['output_validation'][0]['subtitle_streams'], [])

    def test_hls_child_selected_audio_uses_real_variant_segments(self):
        candidate, _ = discovered(self.fixture, 'HLS')
        candidate.metadata['track_variant_url'] = self.fixture.base + 'video.m3u8'
        candidate.request_context = candidate.request_context.derive(candidate.metadata['track_variant_url'])
        _, result = self.perform('HLS', {'audio_language': 'ja', 'container': 'mkv'}, candidate=candidate)
        summary = result['output_validation'][0]
        self.assertEqual(len(summary['video_streams']), 1)
        self.assertEqual(summary['audio_streams'][0].get('language'), 'jpn')

    def test_direct_html_external_caption_muxes_without_losing_audio(self):
        from core.media_tracks import html_subtitles
        candidate, _ = discovered(self.fixture, 'Single')
        candidate.metadata['page_subtitles'] = html_subtitles([{'src': self.fixture.base + 'en.vtt',
            'language': 'en', 'kind': 'subtitles', 'name': 'English'}])
        _, result = self.perform('Single', {'subtitle_mode': 'mux', 'subtitle_languages': ['en']}, candidate=candidate)
        summary = result['output_validation'][0]
        self.assertEqual(len(summary['audio_streams']), 1)
        self.assertEqual(len(summary['subtitle_streams']), 1)
        self.assertEqual(summary['subtitle_streams'][0]['language'], 'eng')


if __name__ == '__main__':
    unittest.main()
