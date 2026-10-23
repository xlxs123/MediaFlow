"""能力诊断的本地数据边界回归；不将夹具宣称为真实浏览器检测结果。"""
from copy import deepcopy
import json
import unittest

from core.browser_capabilities import CAPABILITY_SCRIPT, format_capabilities, normalize_capabilities


def capture():
    """Synthetic browser returnByValue data, intentionally without page data."""
    return {
        'schema_version': 1, 'video_tag': True, 'html_video_element': True, 'secure_context': True,
        'can_play_type': {'h264_mp4': '', 'aac_mp4': '', 'h264_aac_mp4': '',
                          'vp9_webm': 'probably', 'vp9_opus_webm': 'probably',
                          'hls_native': '', 'dash_native': ''},
        'mse': {'available': True, 'is_type_supported': {'h264_mp4': False, 'aac_mp4': False,
                 'h264_aac_mp4': False, 'vp9_webm': True, 'vp9_opus_webm': True}},
        'media_capabilities': {'available': True, 'decoding_info_available': True, 'decoding_info': {
            'h264_mp4': {'status': 'ok', 'supported': False, 'smooth': False, 'power_efficient': False},
            'aac_mp4': {'status': 'ok', 'supported': False, 'smooth': False, 'power_efficient': False},
            'vp9_opus_webm': {'status': 'ok', 'supported': True, 'smooth': True, 'power_efficient': False}}},
        'browser': {'user_agent': 'FixtureBrowser/1.0', 'platform': 'Win32', 'vendor': 'Fixture Vendor',
            'user_agent_data': {'available': True, 'mobile': False, 'platform': 'Windows',
                                'brands': [{'brand': 'Fixture Browser', 'version': '1'}]}},
        'cookie': {'enabled': True, 'writable': False, 'cleanup': True, 'status': 'blocked'},
        'local_storage': {'available': True, 'writable': True, 'cleanup': True, 'status': 'ok'},
        'session_storage': {'available': True, 'writable': True, 'cleanup': True, 'status': 'ok'},
        'video_elements': {'count': 2, 'states': [{'ready_state': 4, 'error_code': 0},
                                                {'ready_state': 0, 'error_code': 4}]},
    }


class BrowserCapabilitiesTests(unittest.TestCase):
    def test_empty_capture_is_unknown_and_never_false(self):
        report = normalize_capabilities({})
        self.assertIsNone(report['video_tag'])
        self.assertIsNone(report['html_video_element'])
        self.assertIsNone(report['secure_context'])
        self.assertTrue(all(value is None for value in report['can_play_type'].values()))
        self.assertIsNone(report['mse']['available'])
        self.assertTrue(all(value is None for value in report['mse']['is_type_supported'].values()))
        for name in ('cookie', 'local_storage', 'session_storage'):
            self.assertIsNone(report[name]['writable'])
            self.assertIsNone(report[name]['cleanup'])
            self.assertEqual(report[name]['status'], 'unknown')
        for result in report['media_capabilities']['decoding_info'].values():
            self.assertEqual(result, {'status': 'unknown', 'supported': None,
                                      'smooth': None, 'power_efficient': None})
        self.assertEqual(report['video_elements'], {'count': None, 'states': []})

    def test_non_object_capture_stays_unknown(self):
        expected = normalize_capabilities({})
        for raw in (None, False, 0, [], 'not a browser report', 3.5):
            self.assertEqual(normalize_capabilities(raw), expected)

    def test_captured_results_keep_definite_positive_and_negative_values(self):
        report = normalize_capabilities(capture())
        self.assertEqual(report['can_play_type']['h264_mp4'], '')
        self.assertEqual(report['can_play_type']['vp9_webm'], 'probably')
        self.assertIs(report['mse']['available'], True)
        self.assertIs(report['mse']['is_type_supported']['h264_mp4'], False)
        self.assertIs(report['mse']['is_type_supported']['vp9_webm'], True)
        self.assertIs(report['cookie']['enabled'], True)
        self.assertIs(report['cookie']['writable'], False)
        self.assertIs(report['cookie']['cleanup'], True)

    def test_booleans_are_strict_not_coerced(self):
        for value in ('true', 'false', 1, 0, 1.0, {}, [], None):
            raw = {'video_tag': value, 'html_video_element': value, 'secure_context': value,
                   'mse': {'available': value, 'is_type_supported': {'h264_mp4': value}},
                   'cookie': {'enabled': value, 'writable': value, 'cleanup': value},
                   'local_storage': {'available': value},
                   'browser': {'user_agent_data': {'available': value, 'mobile': value}}}
            report = normalize_capabilities(raw)
            for result in (report['video_tag'], report['html_video_element'], report['secure_context'],
                           report['mse']['available'], report['mse']['is_type_supported']['h264_mp4'],
                           report['cookie']['enabled'], report['cookie']['writable'], report['cookie']['cleanup'],
                           report['local_storage']['available'], report['browser']['user_agent_data']['mobile']):
                self.assertIsNone(result)

    def test_canplaytype_only_accepts_browser_enum(self):
        for value in ('probably', 'maybe', ''):
            report = normalize_capabilities({'can_play_type': {'h264_mp4': value}})
            self.assertEqual(report['can_play_type']['h264_mp4'], value)
        for value in (True, False, 'yes', 'SUPPORTED', 'Probably', 'probably\n', 1, [], None):
            report = normalize_capabilities({'can_play_type': {'h264_mp4': value}})
            self.assertIsNone(report['can_play_type']['h264_mp4'])

    def test_malformed_nested_objects_do_not_crash_or_imply_absence(self):
        for value in (None, [], '', False, 5):
            raw = {key: value for key in ('can_play_type', 'mse', 'media_capabilities', 'browser',
                                         'cookie', 'local_storage', 'session_storage', 'video_elements')}
            report = normalize_capabilities(raw)
            self.assertIsNone(report['mse']['available'])
            self.assertIsNone(report['cookie']['enabled'])
            self.assertIsNone(report['browser']['user_agent'])
            self.assertIsInstance(format_capabilities(raw), str)

    def test_timeout_or_error_cannot_turn_into_codec_unsupported(self):
        for status in ('timeout', 'error', 'unavailable', 'unknown'):
            report = normalize_capabilities({'media_capabilities': {'decoding_info': {
                'h264_mp4': {'status': status, 'supported': False, 'smooth': True, 'power_efficient': True}}}})
            value = report['media_capabilities']['decoding_info']['h264_mp4']
            self.assertEqual(value['status'], status)
            self.assertIsNone(value['supported'])
            self.assertIsNone(value['smooth'])
            self.assertIsNone(value['power_efficient'])
            self.assertNotIn('H.264 检测未支持', format_capabilities(report))

    def test_successful_decoding_info_uses_only_real_boolean_fields(self):
        report = normalize_capabilities({'media_capabilities': {'decoding_info': {
            'h264_mp4': {'status': 'ok', 'supported': True, 'smooth': 1, 'power_efficient': 'yes'}}}})
        self.assertEqual(report['media_capabilities']['decoding_info']['h264_mp4'],
                         {'status': 'ok', 'supported': True, 'smooth': None, 'power_efficient': None})

    def test_unrecognized_status_does_not_leak_error_message(self):
        secret = 'PRIVATE_RAW_ERROR_cookie_value'
        report = normalize_capabilities({'cookie': {'status': secret}, 'media_capabilities': {
            'decoding_info': {'h264_mp4': {'status': secret, 'supported': False, 'error': secret}}}})
        self.assertEqual(report['cookie']['status'], 'unknown')
        self.assertEqual(report['media_capabilities']['decoding_info']['h264_mp4']['status'], 'unknown')
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, format_capabilities(report))

    def test_browser_identity_is_actual_input_not_fabricated(self):
        report = normalize_capabilities(capture())
        self.assertEqual(report['browser']['user_agent'], 'FixtureBrowser/1.0')
        self.assertEqual(report['browser']['platform'], 'Win32')
        self.assertEqual(report['browser']['vendor'], 'Fixture Vendor')
        self.assertEqual(report['browser']['user_agent_data']['brands'], [{'brand': 'Fixture Browser', 'version': '1'}])
        empty = normalize_capabilities({})['browser']
        self.assertIsNone(empty['user_agent'])
        self.assertEqual(empty['user_agent_data']['brands'], [])

    def test_browser_strings_are_bounded_and_control_characters_removed(self):
        raw = {'browser': {'user_agent': 'UA\x00\n\r\t\u202e\ud800' + 'a' * 2000,
                          'platform': 'p' * 500, 'vendor': 'v' * 500,
                          'user_agent_data': {'platform': 'x' * 500,
                           'brands': [{'brand': '\x00Brand\n' + 'b' * 200, 'version': '1' * 200}] * 20}}}
        browser = normalize_capabilities(raw)['browser']
        self.assertEqual(len(browser['user_agent']), 512)
        self.assertEqual(len(browser['platform']), 128)
        self.assertEqual(len(browser['vendor']), 128)
        self.assertEqual(len(browser['user_agent_data']['platform']), 128)
        self.assertEqual(len(browser['user_agent_data']['brands']), 8)
        self.assertEqual(len(browser['user_agent_data']['brands'][0]['brand']), 64)
        self.assertEqual(len(browser['user_agent_data']['brands'][0]['version']), 32)
        for value in ('\x00', '\n', '\r', '\t', '\u202e', '\ud800'):
            self.assertNotIn(value, browser['user_agent'])

    def test_invalid_brand_entries_are_ignored(self):
        report = normalize_capabilities({'browser': {'user_agent_data': {'brands': [
            None, 'raw-secret', {'brand': []}, {'brand': '\x00'}, {'brand': 'Chromium', 'version': 140}]}}})
        self.assertEqual(report['browser']['user_agent_data']['brands'], [{'brand': 'Chromium', 'version': None}])

    def test_video_statistics_are_bounded_and_typed(self):
        report = normalize_capabilities({'video_elements': {'count': 100,
            'states': [{'ready_state': 4, 'error_code': 0}] * 100}})
        self.assertEqual(report['video_elements']['count'], 100)
        self.assertEqual(len(report['video_elements']['states']), 8)
        for value in (True, -1, 1.2, '2', 1_000_001):
            self.assertIsNone(normalize_capabilities({'video_elements': {'count': value}})['video_elements']['count'])
        value = normalize_capabilities({'video_elements': {'states': [
            {'ready_state': 5, 'error_code': True}, {'ready_state': -1, 'error_code': '4'}, None]}})
        self.assertEqual(value['video_elements']['states'], [{'ready_state': None, 'error_code': None}] * 3)

    def test_fixed_projection_drops_credentials_urls_page_content_and_storage(self):
        secret = 'DO_NOT_EXPORT_PRIVATE_MARKER'
        raw = capture()
        raw.update(cookie_values=secret, cookie_header=secret, storage={secret: secret},
                   page_text=secret, url='https://example.invalid/?token=' + secret, console=secret)
        raw['cookie'].update(value=secret, name=secret, cookies=secret, error=secret)
        raw['local_storage'].update(content=secret, keys=[secret], values=[secret])
        raw['browser'].update(authorization=secret, headers={'Cookie': secret})
        raw['browser']['user_agent_data']['brands'][0]['extra'] = secret
        raw['mse']['is_type_supported'][secret] = True
        raw['can_play_type'][secret] = 'probably'
        raw['media_capabilities']['decoding_info'][secret] = {'supported': True}
        raw['video_elements']['states'][0].update(current_src=secret, poster=secret, text=secret)
        report = normalize_capabilities(raw)
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))
        self.assertNotIn(secret, format_capabilities(raw))

    def test_normalization_does_not_mutate_input_and_is_idempotent(self):
        raw = capture()
        original = deepcopy(raw)
        report = normalize_capabilities(raw)
        self.assertEqual(raw, original)
        self.assertEqual(normalize_capabilities(report), report)

    def test_formatter_unknown_capability_is_not_definite_codec_failure(self):
        text = format_capabilities({})
        self.assertIn('MSE 可用：未知', text)
        self.assertIn('H.264 MP4：未知', text)
        self.assertNotIn('H.264 检测未支持', text)
        self.assertNotIn('AAC 检测未支持', text)
        self.assertNotIn('建议：', text)

    def test_formatter_mse_does_not_claim_native_hls_or_dash(self):
        text = format_capabilities(capture())
        self.assertIn('MSE 可用：是', text)
        self.assertIn('HLS 原生：不支持；DASH 原生：不支持', text)
        self.assertIn('MSE 可用不代表 HLS / DASH 一定可播放', text)
        self.assertIn('页面播放器解析清单', text)

    def test_formatter_missing_codecs_names_the_real_limit_and_ua_is_not_fix(self):
        text = format_capabilities(capture())
        self.assertIn('H.264 检测未支持', text)
        self.assertIn('AAC 检测未支持', text)
        self.assertIn('MSE 未支持 H.264', text)
        self.assertIn('QtWebEngine 构建', text)
        self.assertIn('WebView2', text)
        self.assertIn('修改 User-Agent 不会补充缺失的编解码器', text)

    def test_file_codec_support_does_not_imply_mse_codec_support(self):
        raw = capture()
        raw['can_play_type'].update(h264_mp4='probably', aac_mp4='probably', h264_aac_mp4='probably')
        text = format_capabilities(raw)
        self.assertNotIn('H.264 检测未支持', text)
        self.assertIn('MSE 未支持 H.264', text)

    def test_cookie_denial_is_not_misdiagnosed_as_codec_failure(self):
        raw = {'cookie': {'enabled': True, 'writable': False, 'cleanup': True, 'status': 'blocked'}}
        text = format_capabilities(raw)
        self.assertIn('随机项写读 否', text)
        self.assertIn('可能影响登录或验证', text)
        self.assertIn('不能单独证明媒体解码失败', text)
        self.assertNotIn('H.264 检测未支持', text)

    def test_error_and_timeout_remain_unknown(self):
        text = format_capabilities({'media_capabilities': {'available': True, 'decoding_info_available': True,
            'decoding_info': {'h264_mp4': {'status': 'timeout'}, 'aac_mp4': {'status': 'error'}}}})
        self.assertIn('H.264 MP4：超时（未知）；支持 未知', text)
        self.assertIn('AAC / MP4：检测异常（未知）；支持 未知', text)
        self.assertNotIn('检测未支持', text)

    def test_media_error_two_is_network_error_not_codec_evidence(self):
        text = format_capabilities({'video_elements': {'count': 1, 'states': [{'ready_state': 0, 'error_code': 2}]}})
        self.assertIn('网络错误', text)
        self.assertNotIn('检测未支持', text)

    def test_native_protocol_maybe_is_reported_without_promising_playback(self):
        text = format_capabilities({'can_play_type': {'hls_native': 'maybe', 'dash_native': 'probably'}})
        self.assertIn('HLS 原生：可能支持；DASH 原生：支持（浏览器声明）', text)
        self.assertNotIn('一定可播放：是', text)

    def test_script_has_no_network_collection_or_bulk_storage_operation(self):
        # Privacy invariant: the capability expression itself must not request/collect page media.
        self.assertTrue(CAPABILITY_SCRIPT.startswith('(async () => {'))
        self.assertNotIn('__FORMAT_MAP__', CAPABILITY_SCRIPT)
        for expression in ('fetch(', 'XMLHttpRequest', 'console.', 'location.href', 'currentSrc',
                           'getEntriesByType', 'innerText', 'textContent', 'document.body',
                           'localStorage.clear', 'sessionStorage.clear', 'getHighEntropyValues'):
            self.assertNotIn(expression, CAPABILITY_SCRIPT)


if __name__ == '__main__':
    unittest.main()
