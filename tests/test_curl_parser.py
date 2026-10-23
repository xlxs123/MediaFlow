"""Regression tests for the 30 requested cases and shell/escaping boundaries."""

import re
import random
import shlex
import unittest

from core.curl_parser import CurlParseError, has_temporary_signature, parse_curl


URL = "https://cdn.example.com/video/index.m3u8?sign=abc&t=123"
ORIGIN = "https://example.com"
REFERER = "https://example.com/poster.html?viewkey=123"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0"


def chrome_cmd_quote(value):
    """Current Chromium NetworkLogView.escapeStringWin (ASCII fixtures).

    Source: https://chromium.googlesource.com/devtools/devtools-frontend/+/HEAD/
    front_end/panels/network/NetworkLogView.ts
    This fixture generator exercises both escaping layers rather than testing
    only hand-written simplified examples.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = re.sub(r"[^a-zA-Z0-9\s_\-:=+~'/.,?;*]", lambda match: "^" + match.group(), escaped)
    escaped = re.sub(r"%(?=[a-zA-Z0-9_])", "%^", escaped)
    escaped = re.sub(r"[^ -~\r\n]", " ", escaped)
    escaped = re.sub(r"\r?\n|\r", "^\n\n", escaped)
    return '^"' + escaped + '^"'


def chrome_cmd_url_quote(value):
    # DevTools additionally shields URL-glob delimiters after shell quoting.
    return re.sub(r"[\[\]{}]", lambda match: "\\" + match.group(), chrome_cmd_quote(value))


class RequestedThirtyCases(unittest.TestCase):
    def test_01_bash_curl_m3u8(self):
        result = parse_curl(f"curl '{URL}' -H 'origin: {ORIGIN}'")
        self.assertEqual(result.url, URL)
        self.assertEqual(result.origin, ORIGIN)
        self.assertEqual(result.shell_format, "bash")

    def test_02_cmd_curl_m3u8_exact_user_example(self):
        text = '''curl --url ^"https://example.com/index.m3u8?sign=abc^&t=123^" ^
-H ^"origin: https://site.example.com^" ^
-H ^"referer: https://site.example.com/poster.html?viewkey=456^"'''
        result = parse_curl(text)
        self.assertEqual(result.url, "https://example.com/index.m3u8?sign=abc&t=123")
        self.assertEqual(result.origin, "https://site.example.com")
        self.assertEqual(result.referer, "https://site.example.com/poster.html?viewkey=456")
        self.assertEqual(result.shell_format, "cmd")

    def test_03_url_ampersand_t_parameter(self):
        url = "https://example.com/index.m3u8?t=123&extra=yes"
        self.assertEqual(parse_curl(f'curl "{url}"').url, url)

    def test_04_url_sign_parameter(self):
        self.assertEqual(parse_curl(f"curl '{URL}'").url, URL)
        self.assertTrue(has_temporary_signature(URL))

    def test_05_url_multiple_query_parameters(self):
        url = URL + "&foo=one&bar=two&empty="
        self.assertEqual(parse_curl(f"curl '{url}'").url, url)

    def test_06_referer_viewkey_query(self):
        self.assertEqual(parse_curl(f"curl '{URL}' -H 'referer: {REFERER}'").referer, REFERER)

    def test_07_header_value_commas(self):
        value = "zh-CN,zh;q=0.9,en;q=0.8"
        result = parse_curl(f"curl '{URL}' -H 'accept-language: {value}'")
        self.assertEqual(result.headers["accept-language"], value)

    def test_08_user_agent_parentheses(self):
        self.assertEqual(parse_curl(f"curl '{URL}' -H 'user-agent: {UA}'").user_agent, UA)

    def test_09_sec_ch_ua_multiple_double_quotes(self):
        value = '"Not/A)Brand";v="8", "Chromium";v="154", "Google Chrome";v="154"'
        bash = parse_curl(f"curl '{URL}' -H 'Sec-CH-UA: {value}'")
        cmd = parse_curl("curl " + chrome_cmd_quote(URL) + " -H " + chrome_cmd_quote("Sec-CH-UA: " + value))
        self.assertEqual(bash.headers["sec-ch-ua"], value)
        self.assertEqual(cmd.headers["sec-ch-ua"], value)

    def test_10_missing_origin(self):
        result = parse_curl(f"curl '{URL}'")
        self.assertEqual(result.origin, "")
        self.assertNotIn("origin", result.headers)

    def test_11_missing_referer(self):
        result = parse_curl(f"curl '{URL}'")
        self.assertEqual(result.referer, "")
        self.assertNotIn("referer", result.headers)

    def test_12_cookie_is_returned_only_in_memory(self):
        result = parse_curl(f"curl '{URL}' -H 'Cookie: session=private; preference=1'")
        self.assertEqual(result.headers["cookie"], "session=private; preference=1")
        self.assertFalse(any("private" in warning for warning in result.warnings))

    def test_13_multiple_urls_media_priority(self):
        result = parse_curl(f"curl 'https://example.com/page' 'https://example.com/movie.mp4' '{URL}'")
        self.assertEqual(len(result.urls), 3)
        self.assertEqual(result.url, URL)

    def test_14_explicit_url_option(self):
        self.assertEqual(parse_curl(f"curl --url '{URL}'").url, URL)

    def test_15_positional_url(self):
        self.assertEqual(parse_curl(f"curl '{URL}'").url, URL)

    def test_16_multiline_bash(self):
        text = f"curl --url '{URL}' \\\n  -H 'origin: {ORIGIN}' \\\n  -H 'referer: {REFERER}'"
        result = parse_curl(text)
        self.assertEqual(result.url, URL)
        self.assertEqual(result.origin, ORIGIN)
        self.assertEqual(result.referer, REFERER)

    def test_17_single_line_bash(self):
        self.assertEqual(parse_curl(f"curl '{URL}' -H 'accept: */*' --compressed").url, URL)

    def test_18_multiline_cmd(self):
        text = "curl " + chrome_cmd_quote(URL) + " ^\r\n  -H " + chrome_cmd_quote("User-Agent: " + UA)
        self.assertEqual(parse_curl(text).user_agent, UA)

    def test_19_single_line_cmd(self):
        self.assertEqual(parse_curl("curl --url " + chrome_cmd_quote(URL)).url, URL)

    def test_20_windows_caret_escapes(self):
        url = "https://example.com/index.m3u8?x=1&literal=^&pipe=|"
        text = 'curl ^"https://example.com/index.m3u8?x=1^&literal=^^^&pipe=^|^"'
        self.assertEqual(parse_curl(text).url, url)

    def test_21_bash_backslash_line_continuation(self):
        result = parse_curl(f"curl \\\r\n '{URL}' \\\r\n -H 'origin: {ORIGIN}'")
        self.assertEqual(result.url, URL)
        self.assertEqual(result.origin, ORIGIN)

    def test_22_ampersands_not_lost(self):
        url = "https://example.com/index.m3u8?a=1&b=2&c=3"
        self.assertEqual(parse_curl("curl " + chrome_cmd_quote(url)).url, url)

    def test_23_equal_signs_not_lost(self):
        url = "https://example.com/index.m3u8?a=a==&b=b=c"
        self.assertEqual(parse_curl(f"curl '{url}'").url, url)

    def test_24_header_names_case_insensitive(self):
        result = parse_curl(f"curl '{URL}' -H 'ORIGIN: old' -H 'Origin: {ORIGIN}' -H 'ReFeReR: {REFERER}'")
        self.assertEqual(result.headers, {"origin": ORIGIN, "referer": REFERER})

    def test_25_percent_preserved(self):
        url = "https://example.com/index.m3u8?percent=100%&encoded=%25%20"
        self.assertEqual(parse_curl("curl " + chrome_cmd_quote(url)).url, url)

    def test_26_plus_preserved(self):
        url = "https://example.com/index.m3u8?token=a+b+c%2B"
        self.assertEqual(parse_curl(f"curl '{url}'").url, url)

    def test_27_fragment_preserved(self):
        url = "https://example.com/index.m3u8?a=1#piece+2"
        self.assertEqual(parse_curl("curl " + chrome_cmd_quote(url)).url, url)

    def test_28_url_encoding_not_decoded(self):
        url = "https://example.com/dir%20name/index.m3u8?sign=%2Fabc%3D%26t%3D123"
        self.assertEqual(parse_curl(f"curl '{url}'").url, url)

    def test_29_referer_ampersand(self):
        value = REFERER + "&id=42&sign=xyz"
        result = parse_curl("curl " + chrome_cmd_quote(URL) + " -H " + chrome_cmd_quote("referer: " + value))
        self.assertEqual(result.referer, value)

    def test_30_user_agent_spaces(self):
        result = parse_curl(f'curl "{URL}" -A "{UA}"')
        self.assertEqual(result.user_agent, UA)


class EscapingAndSelectionCases(unittest.TestCase):
    def test_chromium_cmd_roundtrip_varied_literal_characters(self):
        generator = random.Random(20261004)
        alphabet = "abZ09 '&=+%#;:/?\\\"^!$()[]{}|`~,*"
        for case in range(250):
            value = "".join(generator.choice(alphabet) for _ in range(generator.randrange(1, 70)))
            url = "https://example.com/index.m3u8?value=" + value
            with self.subTest(case=case):
                result = parse_curl("curl " + chrome_cmd_url_quote(url) + " -H " + chrome_cmd_quote("x-fixture: " + value))
                self.assertEqual(result.url, url)
                self.assertEqual(result.headers.get("x-fixture", ""), value.strip())

    def test_bash_roundtrip_varied_literal_characters(self):
        generator = random.Random(44)
        alphabet = "abZ09 '&=+%#;:/?\\\"^!$()[]{}|`~,*"
        for case in range(250):
            value = "".join(generator.choice(alphabet) for _ in range(generator.randrange(1, 70)))
            url = "https://example.com/index.m3u8?value=" + value
            # Match Chrome's extra URL-glob escaping while using Python's
            # independent POSIX quote helper as a tokenizer regression oracle.
            curl_url = re.sub(r"[\[\]{}]", lambda match: "\\" + match.group(), url)
            with self.subTest(case=case):
                result = parse_curl("curl " + shlex.quote(curl_url) + " -H " + shlex.quote("x-fixture: " + value))
                self.assertEqual(result.url, url)
                self.assertEqual(result.headers.get("x-fixture", ""), value.strip())

    def test_actual_chrome_cmd_all_headers_and_backslashes(self):
        headers = {
            "origin": ORIGIN, "referer": REFERER,
            "user-agent": UA, "accept": "*/*", "accept-language": "zh-CN,zh;q=0.9",
            "priority": "u=1, i", "sec-ch-ua": '"Chromium";v="154", "Not A(Brand";v="99"',
            "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "cross-site",
            "x-custom": r"one\two\path with spaces", "cookie": "session=a=b; foo=two",
        }
        command = "curl " + chrome_cmd_quote(URL)
        for name, value in headers.items():
            command += " ^\r\n  -H " + chrome_cmd_quote(name + ": " + value)
        self.assertEqual(parse_curl(command).headers, headers)

    def test_chrome_cmd_quote_backslash_and_caret_in_url(self):
        url = 'https://example.com/index.m3u8?x=one\\two&literal="double"&apos=\'one\'&caret=^'
        self.assertEqual(parse_curl("curl " + chrome_cmd_quote(url)).url, url)

    def test_chrome_cmd_repeated_backslashes_before_quote(self):
        url = 'https://example.com/index.m3u8?x=\\\\"two\\end'
        self.assertEqual(parse_curl("curl " + chrome_cmd_quote(url)).url, url)

    def test_bash_ansi_c_quoting(self):
        command = r"curl $'https://example.com/index.m3u8?x=one\'two&t=123' -H $'sec-ch-ua: \"Chromium\";v=\"154\"'"
        result = parse_curl(command)
        self.assertEqual(result.url, "https://example.com/index.m3u8?x=one'two&t=123")
        self.assertEqual(result.headers["sec-ch-ua"], '"Chromium";v="154"')

    def test_bash_ansi_c_unicode_hex_octal(self):
        result = parse_curl(r"curl $'https://example.com/\u89c6\u9891/index.m3u8?x=\x41\101' -H $'x-label: \u4e2d\u6587' ")
        self.assertEqual(result.url, "https://example.com/视频/index.m3u8?x=AA")
        self.assertEqual(result.headers["x-label"], "中文")

    def test_bash_single_quote_concatenation(self):
        result = parse_curl(r"curl 'https://example.com/index.m3u8?x=one'\''two' -H 'user-agent: O'\''Brien Browser'")
        self.assertEqual(result.url, "https://example.com/index.m3u8?x=one'two")
        self.assertEqual(result.user_agent, "O'Brien Browser")

    def test_bash_mixed_quoted_token_concatenation(self):
        result = parse_curl('curl https://example.com/"video dir"/index.m3u8?x=one\\ two')
        self.assertEqual(result.url, "https://example.com/video dir/index.m3u8?x=one two")

    def test_bash_double_quote_escapes_and_literal_backslashes(self):
        command = r'curl "https://example.com/index.m3u8?x=\"quoted\"&path=one\two&dollar=\$HOME"'
        self.assertEqual(parse_curl(command).url, 'https://example.com/index.m3u8?x="quoted"&path=one\\two&dollar=$HOME')

    def test_bash_single_quotes_preserve_carets_and_backslashes(self):
        url = r'https://example.com/index.m3u8?x=^"carets"&path=one\two'
        result = parse_curl("curl '" + url + "'")
        self.assertEqual(result.url, url)
        self.assertEqual(result.shell_format, "bash")

    def test_url_space_and_semicolon_preserved(self):
        url = "https://example.com/video dir/index.m3u8?a=one two;part=two"
        self.assertEqual(parse_curl("curl " + chrome_cmd_quote(url)).url, url)

    def test_cmd_native_double_quotes_with_executable_name(self):
        command = r'curl.exe "https://example.com/index.m3u8?x=abc&t=123" -H "Sec-CH-UA: \"Chromium\";v=\"154\""'
        result = parse_curl(command)
        self.assertEqual(result.shell_format, "cmd")
        self.assertEqual(result.headers["sec-ch-ua"], '"Chromium";v="154"')

    def test_chrome_bash_ipv6_glob_escapes(self):
        result = parse_curl(r"curl 'http://\[::1\]:8080/index.m3u8?x=\{literal\}'")
        self.assertEqual(result.url, "http://[::1]:8080/index.m3u8?x={literal}")

    def test_media_selection_tie_returns_none(self):
        urls = [URL, "https://another.example/video.m3u8"]
        result = parse_curl(f"curl '{urls[0]}' --url '{urls[1]}'")
        self.assertIsNone(result.url)
        self.assertEqual(result.media_urls, urls)
        self.assertIn("发现多个媒体 URL，请选择一个。", result.warnings)

    def test_repeated_identical_media_is_not_ambiguous(self):
        result = parse_curl(f"curl '{URL}' --url '{URL}'")
        self.assertEqual(result.url, URL)
        self.assertEqual(result.urls, [URL])

    def test_mpd_prioritized_over_mp4(self):
        result = parse_curl("curl 'https://example.com/clip.mp4' 'https://example.com/manifest.MPD'")
        self.assertEqual(result.url, "https://example.com/manifest.MPD")

    def test_headers_containing_url_are_not_candidates(self):
        result = parse_curl(f"curl '{URL}' -H 'referer: https://other.example/misleading.m3u8' -H 'origin: https://third.example/a.mp4'")
        self.assertEqual(result.urls, [URL])

    def test_encoded_media_path_is_classified_without_rewriting(self):
        url = "https://example.com/%69ndex%2Em3u8?x=%2f+%25"
        self.assertEqual(parse_curl(f"curl '{url}'").url, url)

    def test_encoded_query_media_is_classified_without_rewriting(self):
        url = "https://example.com/play?url=https%3A%2F%2Fcdn.example%2Findex.m3u8%3Fsign%3Da%26t%3D123"
        self.assertEqual(parse_curl(f"curl '{url}'").url, url)

    def test_media_extension_in_directory_does_not_override_actual_file(self):
        url = "https://example.com/cache.m3u8/clip.mp4"
        result = parse_curl(f"curl '{url}' 'https://example.com/manifest.mpd'")
        self.assertEqual(result.url, "https://example.com/manifest.mpd")

    def test_attached_and_equals_options(self):
        result = parse_curl(f"curl --url='{URL}' --header='Origin: {ORIGIN}' -H'referer: {REFERER}' -A'{UA}' --cookie='session=private'")
        self.assertEqual(result.origin, ORIGIN)
        self.assertEqual(result.referer, REFERER)
        self.assertEqual(result.user_agent, UA)
        self.assertEqual(result.headers["cookie"], "session=private")

    def test_short_cookie_and_referer_options(self):
        result = parse_curl(f"curl '{URL}' -b 'a=1; b=2' -e '{REFERER}'")
        self.assertEqual(result.headers["cookie"], "a=1; b=2")
        self.assertEqual(result.referer, REFERER)

    def test_chrome_cookie_option_is_parsed(self):
        value = 'first=a; second="quoted"; token=a+b%26'
        result = parse_curl("curl " + chrome_cmd_quote(URL) + " -b " + chrome_cmd_quote(value))
        self.assertEqual(result.headers["cookie"], value)

    def test_head_allowed_with_explicit_warning(self):
        result = parse_curl(f"curl -I -X HEAD '{URL}'")
        self.assertEqual(result.url, URL)
        self.assertEqual(result.method, "HEAD")
        self.assertEqual(result.warnings, ["原请求为 HEAD，下载时会使用 GET 获取媒体。"])

    def test_control_options_not_transferred(self):
        result = parse_curl(f"curl -sSL --retry 3 --output '/tmp/custom file' --compressed '{URL}'")
        self.assertEqual(result.headers, {})
        self.assertEqual(result.urls, [URL])

    def test_insecure_is_explicitly_not_applied(self):
        result = parse_curl(f"curl --insecure '{URL}'")
        self.assertIn("cURL 的 --insecure 未应用；下载仍会验证 HTTPS 证书。", result.warnings)

    def test_empty_headers_are_omitted(self):
        result = parse_curl(f"curl '{URL}' -H 'origin: previous' -H 'origin:' -H 'x-empty;' --cookie ''")
        self.assertEqual(result.headers, {})
        self.assertIn("空值请求头不会发送。", result.warnings)

    def test_separator_followed_by_positional_url(self):
        self.assertEqual(parse_curl(f"curl -- '{URL}'").url, URL)

    def test_signature_parameter_names_case_insensitive(self):
        for parameter in ("auth", "signature", "sign", "token", "expires", "expire", "expiry", "t", "SIGN", "x-amz-signature", "%74oken"):
            with self.subTest(parameter=parameter):
                self.assertTrue(has_temporary_signature("https://example.com/index.m3u8?" + parameter + "=abc"))

    def test_signature_detection_avoids_substring_and_fragment_false_positive(self):
        for url in (
            "https://example.com/sign/index.m3u8", "https://example.com/index.m3u8?assignment=yes",
            "https://example.com/index.m3u8?title=sign%3Dabc", "https://example.com/index.m3u8#token=abc",
            "https://example.com/index.m3u8?signing=yes", "not a url",
        ):
            with self.subTest(url=url):
                self.assertFalse(has_temporary_signature(url))


class RejectionCases(unittest.TestCase):
    def assert_parse_error(self, text, expected_code):
        with self.assertRaises(CurlParseError) as caught:
            parse_curl(text)
        self.assertEqual(caught.exception.code, expected_code)
        self.assertRegex(str(caught.exception), r"[\u4e00-\u9fff]")
        return str(caught.exception)

    def test_empty_input(self):
        for text in ("", " \r\n ", None):
            with self.subTest(text=text):
                self.assert_parse_error(text, "not_curl")

    def test_non_curl_input(self):
        self.assert_parse_error(URL, "not_curl")

    def test_no_url(self):
        self.assert_parse_error("curl -H 'origin: https://example.com'", "no_url")

    def test_unknown_extension_is_left_to_content_detection(self):
        result = parse_curl("curl 'https://example.com/page.html'")
        self.assertEqual(result.url, "https://example.com/page.html")
        self.assertEqual(result.media_urls, [result.url])
        self.assertTrue(any("返回内容" in warning for warning in result.warnings))

    def test_bash_incomplete_quotes(self):
        for text in (f"curl '{URL}", f'curl "{URL}', "curl $'https://example.com/index.m3u8"):
            with self.subTest(text=text):
                self.assert_parse_error(text, "incomplete_quotes")

    def test_cmd_incomplete_quotes(self):
        self.assert_parse_error('curl ^"https://example.com/index.m3u8', "incomplete_quotes")

    def test_bash_operators_are_not_executed(self):
        for operator in (";", "&&", "|", "||", ">", "<", "&", "\n"):
            with self.subTest(operator=operator):
                self.assert_parse_error(f"curl '{URL}' {operator} echo unsafe", "shell_syntax")

    def test_cmd_operators_are_not_executed(self):
        for operator in ("&", "&&", "|", ">", "<", "(", "\r\n"):
            with self.subTest(operator=operator):
                self.assert_parse_error("curl " + chrome_cmd_quote(URL) + " " + operator + " echo unsafe", "shell_syntax")

    def test_unquoted_bash_ampersand_requires_fresh_copy(self):
        self.assert_parse_error("curl " + URL, "shell_syntax")

    def test_bash_command_and_variable_expansion_rejected(self):
        for body in ("$TOKEN", "${TOKEN}", "$(echo unsafe)", "`echo unsafe`", "$$", "$?"):
            with self.subTest(body=body):
                self.assert_parse_error(f'curl "https://example.com/index.m3u8?token={body}"', "shell_expansion")

    def test_bash_single_quoted_expansion_text_is_literal(self):
        url = "https://example.com/index.m3u8?token=$TOKEN&literal=$(unexecuted)"
        self.assertEqual(parse_curl(f"curl '{url}'").url, url)

    def test_cmd_environment_expansion_rejected(self):
        for value in ("%TOKEN%", "!TOKEN!"):
            with self.subTest(value=value):
                self.assert_parse_error('curl.exe "https://example.com/index.m3u8?token=' + value + '"', "shell_expansion")

    def test_chrome_cmd_escaped_percent_variable_text_is_literal(self):
        url = "https://example.com/index.m3u8?token=%TOKEN%&delayed=!TOKEN!"
        self.assertEqual(parse_curl("curl " + chrome_cmd_quote(url)).url, url)

    def test_common_non_get_requests_are_preserved(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(parse_curl(f"curl '{URL}' -X {method}").method, method)

    def test_multipart_and_uploads_rejected(self):
        for option in ("--form", "--form-string", "--upload-file", "-F", "-T", "-sFsecret", "-sTfile"):
            with self.subTest(option=option):
                self.assert_parse_error(f"curl '{URL}' {option} 'secret body'", "request_body")

    def test_missing_option_values(self):
        for option in ("--url", "-H", "--header", "-b", "-A", "-X", "--output", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "-d"):
            with self.subTest(option=option):
                self.assert_parse_error(f"curl '{URL}' {option}", "missing_option_value")

    def test_cookie_file_is_not_read(self):
        for value in ("cookies.txt", "@cookies.txt"):
            with self.subTest(value=value):
                self.assert_parse_error(f"curl '{URL}' -b '{value}'", "cookie_file")

    def test_header_file_is_not_read(self):
        self.assert_parse_error(f"curl '{URL}' -H '@headers.txt'", "header_file")

    def test_config_and_cookie_jar_not_accessed(self):
        for option in ("--config", "-K", "--cookie-jar", "-c"):
            with self.subTest(option=option):
                self.assert_parse_error(f"curl '{URL}' {option} file.txt", "external_file")

    def test_header_crlf_injection_rejected(self):
        message = self.assert_parse_error(f"curl '{URL}' -H $'Authorization: secret\\r\\nx-injected: bad'", "invalid_header")
        self.assertNotIn("secret", message)

    def test_header_invalid_name_rejected(self):
        for header in ("bad name: value", "missing-colon", ":authority: site.example"):
            with self.subTest(header=header):
                self.assert_parse_error(f"curl '{URL}' -H '{header}'", "invalid_header")

    def test_url_control_characters_rejected(self):
        self.assert_parse_error(r"curl $'https://example.com/index.m3u8?x=\r\ninjected'", "invalid_url")

    def test_non_http_url_rejected(self):
        for url in ("file:///tmp/index.m3u8", "ftp://example.com/index.m3u8", "https:///index.m3u8", "https://example.com:invalid/index.m3u8"):
            with self.subTest(url=url):
                self.assert_parse_error("curl '" + url + "'", "invalid_url")

    def test_invalid_cmd_url_has_specific_chinese_reason(self):
        message = self.assert_parse_error('curl --url ^"not a url^"', "invalid_url")
        self.assertIn("CMD", message)

    def test_invalid_ansi_c_escape(self):
        for text in (r"curl $'https://example.com/index.m3u8?x=\u'", r"curl $'https://example.com/index.m3u8?x=\U00110000'"):
            with self.subTest(text=text):
                self.assert_parse_error(text, "invalid_escape")

    def test_unknown_option_rejected_without_leaking_value(self):
        message = self.assert_parse_error(f"curl '{URL}' --unknown-secret=private-token", "unsupported_option")
        self.assertNotIn("private-token", message)

    def test_auto_referer_rejected(self):
        self.assert_parse_error(f"curl '{URL}' -e '{REFERER};auto'", "automatic_referer")

    def test_nul_character_rejected(self):
        self.assert_parse_error("curl '\0" + URL + "'", "invalid_character")

    def test_dangling_bash_escape_rejected(self):
        self.assert_parse_error(f"curl '{URL}' \\", "invalid_escape")

    def test_dangling_cmd_escape_rejected(self):
        self.assert_parse_error("curl " + chrome_cmd_quote(URL) + " ^", "invalid_escape")


if __name__ == "__main__":
    unittest.main()
