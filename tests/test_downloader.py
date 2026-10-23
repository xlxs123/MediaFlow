from pathlib import Path
import unittest

from core.downloader import DownloadRequest, build_arguments, friendly_error, progress_from_data, validate_filename


class DownloaderTests(unittest.TestCase):
    def request(self, **kwargs):
        fields = dict(yt_dlp_path='yt-dlp.exe', url='https://example.com/index.m3u8?sign=abc&t=123', output_directory='E:\\download')
        fields.update(kwargs)
        return DownloadRequest(**fields)

    def test_url_preserved_as_single_argument(self):
        request = self.request(url='https://example.com/index.m3u8?sign=a%2Fb+t&t=123&x=abc#frag')
        args = build_arguments(request)
        self.assertEqual(args[-1], request.url)
        self.assertEqual(args[-2], '--')
        self.assertEqual(args.count(request.url), 1)

    def test_origin_and_referer(self):
        args = build_arguments(self.request(headers={'Origin': 'https://site.test', 'Referer': 'https://site.test/?viewkey=1&x=2'}))
        self.assertIn('origin:https://site.test', args)
        self.assertIn('referer:https://site.test/?viewkey=1&x=2', args)

    def test_empty_headers_not_emitted(self):
        args = build_arguments(self.request(headers={'origin': '', 'referer': ''}))
        self.assertNotIn('origin:', args)
        self.assertNotIn('referer:', args)

    def test_actual_user_agent_has_priority(self):
        args = build_arguments(self.request(headers={'User-Agent': 'Custom (Test) Agent'}))
        self.assertIn('user-agent:Custom (Test) Agent', args)
        self.assertEqual(sum(value.startswith('user-agent:') for value in args), 1)

    def test_default_name_and_custom_filename(self):
        self.assertNotIn('-o', build_arguments(self.request()))
        args = build_arguments(self.request(filename='video01.mp4'))
        self.assertEqual(args[args.index('-o') + 1], 'video01.%(ext)s')

    def test_literal_percent_in_filename(self):
        args = build_arguments(self.request(filename='100% complete'))
        self.assertEqual(args[args.index('-o') + 1], '100%% complete.%(ext)s')

    def test_bad_filename(self):
        for name in ('CON', 'NUL.mp4', '../escape', 'video:bad', 'abc.'):
            with self.assertRaises(ValueError):
                validate_filename(name)

    def test_custom_path_as_single_argument(self):
        args = build_arguments(self.request(output_directory='D:\\Downloads & Videos\\custom'))
        self.assertEqual(args[args.index('-P') + 1], 'D:\\Downloads & Videos\\custom')

    def test_mp4(self):
        args = build_arguments(self.request(ffmpeg_path='tools/ffmpeg.exe'))
        self.assertEqual(args[args.index('--remux-video') + 1], 'mp4')

    def test_curl_options_never_executed(self):
        args = build_arguments(self.request())
        self.assertIn('--ignore-config', args)
        self.assertIn('--no-plugin-dirs', args)
        self.assertIn('--no-allow-unplayable-formats', args)
        self.assertIn('--abort-on-unavailable-fragments', args)

    def test_progress(self):
        data = progress_from_data({'downloaded_bytes': 2048, 'total_bytes': 4096, 'speed': 1024, 'eta': 18,
                                  'fragment_index': 34, 'fragment_count': 73})
        self.assertEqual(data['percent'], 50)
        self.assertEqual(data['speed'], '1.0 KiB/s')
        self.assertEqual(data['eta'], '00:18')
        self.assertEqual(data['fragment_count'], 73)

    def test_unknown_progress(self):
        self.assertEqual(progress_from_data({})['eta'], '--')
        self.assertIsNone(progress_from_data({})['percent'])

    def test_errors(self):
        self.assertIn('过期', friendly_error('HTTP Error 403: Forbidden'))
        self.assertIn('分片', friendly_error('HTTP Error 423: Locked'))
        self.assertIn('Copy as cURL', friendly_error('RemoteDisconnected'))
        self.assertIn('普通网页', friendly_error('Unsupported URL'))
        self.assertIn('受保护', friendly_error('This video is DRM protected'))

    def test_encrypted_word_and_http403_never_imply_drm(self):
        self.assertNotIn('受保护媒体', friendly_error('encrypted media uses AES-128'))
        self.assertNotIn('受保护媒体', friendly_error('HTTP Error 403: Forbidden on encrypted video'))

    def test_drm_system_words_in_titles_paths_or_network_errors_are_not_evidence(self):
        for name in ('Widevine', 'FairPlay', 'PlayReady', 'drm protected', 'protected_media'):
            for error in (f'Permission denied: D:\\Videos\\{name}.mp4',
                          f'HTTP_ERROR: HTTP Error 500 on https://cdn.test/{name}/segment'):
                self.assertNotIn('检测到真正 DRM', friendly_error(error))
        self.assertIn('检测到真正 DRM', friendly_error('ERROR: This video is DRM protected.'))

    def test_request_method_body_context(self):
        ctx = self.request(method='POST', body=b'body=value', headers={'Cookie': 'session=test'}).to_context()
        self.assertEqual(ctx.method, 'POST')
        self.assertEqual(ctx.body, b'body=value')
        self.assertEqual(ctx.cookies, 'session=test')

    def test_browser_compatibility_option_preserves_only_metadata(self):
        request = self.request(headers={'sec-fetch-mode': 'cors', 'Range': 'bytes=0-'},
                               inherit_browser_metadata=True)
        child = request.to_context().derive('abc000.png')
        self.assertEqual(child.headers['sec-fetch-mode'], 'cors')
        self.assertNotIn('range', child.headers)

    def test_explicit_range_option_reaches_key_and_segment_context(self):
        request = self.request(headers={'Range': 'bytes=0-', 'Referer': 'https://page.test/watch',
                                        'User-Agent': 'Observed Browser'}, inherit_range=True)
        for uri in ('enc.key', 'abc000.png'):
            child = request.to_context().derive(uri)
            self.assertEqual(child.headers['range'], 'bytes=0-')
            self.assertEqual(child.referer, 'https://page.test/watch')
            self.assertEqual(child.user_agent, 'Observed Browser')
            self.assertNotIn('origin', child.headers)
            self.assertNotIn('cookie', child.headers)
            self.assertNotIn('authorization', child.headers)
        self.assertFalse(self.request().inherit_range)

    def test_fragment_concurrency_default_and_custom(self):
        for count in (1, 4, 8, 16):
            args = build_arguments(self.request(concurrent_fragments=count))
            self.assertEqual(args[args.index('--concurrent-fragments') + 1], str(count))
        self.assertEqual(self.request().concurrent_fragments, 4)

    def test_total_segment_attempts_and_backoff_arguments(self):
        for attempts in (1, 3, 10):
            args = build_arguments(self.request(segment_max_attempts=attempts))
            self.assertEqual(args[args.index('--fragment-retries') + 1], str(attempts - 1))
            self.assertEqual(args[args.index('--retry-sleep') + 1], 'fragment:exp=1:3:3')
            self.assertIn('--continue', args)
        for invalid in (0, 11, True, '3'):
            with self.assertRaises(ValueError):
                build_arguments(self.request(segment_max_attempts=invalid))

    def test_invalid_fragment_concurrency_cannot_enter_command(self):
        for count in (0, 17, -1, True, 2.5, '4', '4 --exec bad'):
            with self.subTest(value=count), self.assertRaises(ValueError):
                build_arguments(self.request(concurrent_fragments=count))


if __name__ == '__main__':
    unittest.main()
