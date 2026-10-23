"""Literal request reconstruction; no network, curl process or external files."""

import shlex
import unittest
from unittest.mock import patch

from core.curl_parser import CurlParseError, ParsedCurl, has_temporary_signature, parse_curl, parse_input
from core.request_context import RequestContext
from tests.test_curl_parser import chrome_cmd_quote


ENDPOINT = "https://example.com/playlist?sign=%2fA%3d&token=a+b%2B&empty=&t=123"


class InputAndContextCases(unittest.TestCase):
    def test_plain_signed_url_is_preserved(self):
        context = parse_input(ENDPOINT + "#chapter+1")
        self.assertIsInstance(context, RequestContext)
        self.assertEqual(context.url, ENDPOINT + "#chapter+1")
        self.assertEqual(context.original_url, context.url)
        self.assertEqual(context.final_url, context.url)
        self.assertEqual(context.method, "GET")
        self.assertIsNone(context.body)
        self.assertEqual(context.headers, {})

    def test_plain_url_has_no_extension(self):
        context = parse_input("https://example.com/api/stream")
        self.assertEqual(context.url, "https://example.com/api/stream")

    def test_plain_url_bom_and_outer_whitespace(self):
        self.assertEqual(parse_input(" \r\n\ufeff" + ENDPOINT + "\r\n ").url, ENDPOINT)

    def test_plain_url_optional_outer_quotes(self):
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                self.assertEqual(parse_input(quote + ENDPOINT + quote).url, ENDPOINT)

    def test_plain_url_is_not_shell_input(self):
        url = "https://example.com/playlist?literal=$TOKEN&code=$(unexecuted)&amp=1"
        self.assertEqual(parse_input(url).url, url)

    def test_plain_url_preserves_spaces_backslashes_and_encoding(self):
        url = r"https://example.com/video dir/playlist?path=one\two&literal=%2f+%20"
        self.assertEqual(parse_input(url).url, url)

    def test_parsed_query_is_a_fresh_view(self):
        parsed = parse_curl("curl " + shlex.quote(ENDPOINT))
        self.assertEqual(parsed.query_parameters, [("sign", "/A="), ("token", "a b+"), ("empty", ""), ("t", "123")])
        parsed.query_parameters.append(("changed", "value"))
        self.assertEqual(len(parsed.query_parameters), 4)
        self.assertEqual(parsed.url, ENDPOINT)

    def test_context_query_is_a_fresh_view(self):
        context = parse_input(ENDPOINT)
        context.query_parameters.clear()
        self.assertEqual(len(context.query_parameters), 4)
        self.assertEqual(context.url, ENDPOINT)

    def test_to_context_copies_method_body_and_headers(self):
        parsed = parse_curl("curl " + shlex.quote(ENDPOINT) + " -X PATCH --data-raw 'literal' -H 'Origin: https://site.example' -b 'session=fixture'")
        context = parsed.to_context()
        self.assertEqual(context.url, ENDPOINT)
        self.assertEqual(context.method, "PATCH")
        self.assertEqual(context.body, b"literal")
        self.assertEqual(context.origin, "https://site.example")
        self.assertEqual(context.cookies, "session=fixture")
        parsed.headers["origin"] = "https://changed.example"
        self.assertEqual(context.origin, "https://site.example")

    def test_dataclass_new_fields_keep_old_constructor_compatible(self):
        parsed = ParsedCurl(ENDPOINT, [ENDPOINT], [ENDPOINT], {"X-Fixture": "value"})
        self.assertEqual(parsed.method, "GET")
        self.assertIsNone(parsed.body)
        self.assertEqual(parsed.to_context().headers, {"x-fixture": "value"})

    def test_plain_input_does_not_reuse_old_request_body(self):
        old = parse_input("curl 'https://example.com/playlist' --json '{\"old\":1}'")
        fresh = parse_input("https://example.com/another")
        self.assertEqual(old.method, "POST")
        self.assertEqual(fresh.method, "GET")
        self.assertIsNone(fresh.body)
        self.assertEqual(fresh.headers, {})

    def test_single_unknown_curl_url_is_selected(self):
        parsed = parse_curl("curl 'https://example.com/api/no-extension' --data 'a=1'")
        self.assertEqual(parsed.url, "https://example.com/api/no-extension")
        self.assertEqual(parsed.media_urls, [parsed.url])
        self.assertEqual(parsed.to_context().method, "POST")

    def test_unknown_curl_urls_require_selection(self):
        command = "curl 'https://example.com/one' 'https://example.com/two' --json '{\"id\":1}'"
        parsed = parse_curl(command)
        self.assertIsNone(parsed.url)
        self.assertEqual(parsed.media_urls, ["https://example.com/one", "https://example.com/two"])
        self.assertEqual(parsed.query_parameters, [])
        with self.assertRaises(CurlParseError) as caught:
            parse_input(command)
        self.assertEqual(caught.exception.code, "multiple_urls")
        selected = parsed.to_context(parsed.media_urls[1])
        self.assertEqual(selected.url, "https://example.com/two")
        self.assertEqual(selected.method, "POST")
        self.assertEqual(selected.body, b'{"id":1}')

    def test_same_media_rank_curl_urls_require_selection(self):
        command = "curl 'https://example.com/one.m3u8' 'https://example.com/two.m3u8'"
        with self.assertRaises(CurlParseError) as caught:
            parse_input(command)
        self.assertEqual(caught.exception.code, "multiple_urls")

    def test_known_media_keeps_selection_priority(self):
        context = parse_input("curl 'https://example.com/one' 'https://example.com/two.mp4' 'https://example.com/index.m3u8'")
        self.assertEqual(context.url, "https://example.com/index.m3u8")

    def test_derived_resource_resets_post_and_keeps_browser_headers(self):
        context = parse_input("curl 'https://example.com/path/playlist' --json '{\"id\":1}' -H 'Origin: https://site.example' -H 'Referer: https://site.example/watch' -H 'User-Agent: Fixture Browser' -H 'Cookie: session=fixture' -H 'Authorization: Bearer fixture'")
        derived = context.derive("key.dat")
        self.assertEqual(derived.url, "https://example.com/path/key.dat")
        self.assertEqual(derived.method, "GET")
        self.assertIsNone(derived.body)
        for name in ("origin", "referer", "user-agent", "cookie", "authorization"):
            self.assertEqual(derived.headers[name], context.headers[name])

    def test_required_signature_names_are_retained(self):
        for name in ("auth", "signature", "sign", "token", "expires", "t"):
            with self.subTest(name=name):
                url = "https://example.com/playlist?" + name + "=fixture"
                self.assertTrue(has_temporary_signature(url))
                self.assertEqual(parse_input(url).url, url)


class MethodAndLiteralDataCases(unittest.TestCase):
    def test_common_http_methods(self):
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                context = parse_input("curl " + shlex.quote(ENDPOINT) + " --request " + method.lower())
                self.assertEqual(context.method, method)
                self.assertIsNone(context.body)

    def test_last_request_method_takes_precedence(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -X POST --request=PATCH -XPUT --data 'a=1'")
        self.assertEqual(context.method, "PUT")
        self.assertEqual(context.body, b"a=1")

    def test_each_literal_data_option_uses_post_and_form_content_type(self):
        for option in ("--data", "--data-ascii", "--data-raw", "--data-binary", "-d"):
            with self.subTest(option=option):
                context = parse_input("curl " + shlex.quote(ENDPOINT) + " " + option + " 'a=1&b=%2f+2'")
                self.assertEqual(context.method, "POST")
                self.assertEqual(context.body, b"a=1&b=%2f+2")
                self.assertEqual(context.headers["content-type"], "application/x-www-form-urlencoded")

    def test_attached_short_data_and_equals_long_data(self):
        for option in ("-d'a=1&b=2'", "--data='a=1&b=2'", "--data-raw='a=1&b=2'", "--data-binary='a=1&b=2'"):
            with self.subTest(option=option):
                self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " " + option).body, b"a=1&b=2")

    def test_short_flags_may_end_with_data_option(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -sSLd'a=1&b=2'")
        self.assertEqual(context.method, "POST")
        self.assertEqual(context.body, b"a=1&b=2")

    def test_short_flags_may_end_with_request_option(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -sSLXPATCH -d value")
        self.assertEqual(context.method, "PATCH")
        self.assertEqual(context.body, b"value")

    def test_body_value_can_start_with_hyphen(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --data --literal-value")
        self.assertEqual(context.body, b"--literal-value")

    def test_body_can_look_like_a_url_without_becoming_candidate(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --data-raw 'https://other.example/misleading.m3u8'")
        self.assertEqual(context.url, ENDPOINT)
        self.assertEqual(context.body, b"https://other.example/misleading.m3u8")

    def test_literal_at_prefixed_raw_is_supported(self):
        for value in ("@literal", "@-", "@C:/never/read/file.txt"):
            with self.subTest(value=value):
                self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-raw " + shlex.quote(value)).body, value.encode())

    def test_inline_data_equal_at_text_is_literal(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data 'name=@literal'").body, b"name=@literal")

    def test_literal_data_keeps_crlf_and_tabs(self):
        for option in ("--data", "--data-raw", "--data-binary"):
            with self.subTest(option=option):
                context = parse_input("curl " + shlex.quote(ENDPOINT) + " " + option + r" $'one\r\ntwo\tthree'")
                self.assertEqual(context.body, b"one\r\ntwo\tthree")

    def test_bash_body_quoting_keeps_single_quotes(self):
        value = '{"name":"O\'Brien","literal":"a&b+c%2f"}'
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-raw " + shlex.quote(value)).body, value.encode())

    def test_bash_ansi_c_body_escapes(self):
        command = "curl " + shlex.quote(ENDPOINT) + r" --data-binary $'\x41\101\u4e2d\u6587\n'"
        self.assertEqual(parse_input(command).body, "AA中文\n".encode())

    def test_bash_ansi_byte_escapes_keep_binary_bytes(self):
        command = "curl " + shlex.quote(ENDPOINT) + r" --data-binary $'\x80\xff\377\u00ff'"
        self.assertEqual(parse_input(command).body, b"\x80\xff\xff\xc3\xbf")

    def test_bash_ansi_utf8_byte_escapes_are_not_double_encoded(self):
        command = "curl " + shlex.quote(ENDPOINT) + r" --data-raw $'\xe4\xb8\xad\xe6\x96\x87'"
        self.assertEqual(parse_input(command).body, "中文".encode())

    def test_cmd_nested_json_quotes_and_special_characters(self):
        value = '{"url":"https://example.com/key?sign=a&token=b+c%2f","text":"quoted \\\"value\\\""}'
        command = "curl " + chrome_cmd_quote(ENDPOINT) + " ^\r\n --data-raw " + chrome_cmd_quote(value)
        parsed = parse_curl(command)
        self.assertEqual(parsed.shell_format, "cmd")
        self.assertEqual(parsed.url, ENDPOINT)
        self.assertEqual(parsed.body, value.encode())

    def test_cmd_native_double_quoted_body(self):
        command = 'curl.exe "https://example.com/playlist" --data-raw "a=1&b=2"'
        self.assertEqual(parse_input(command).body, b"a=1&b=2")

    def test_chrome_cmd_body_newline_is_preserved(self):
        value = "first\r\nsecond\nthird"
        # DevTools normalizes source newlines to LF in its CMD clipboard text.
        command = "curl " + chrome_cmd_quote(ENDPOINT) + " --data-binary " + chrome_cmd_quote(value)
        self.assertEqual(parse_input(command).body, b"first\nsecond\nthird")

    def test_utf8_literal_body(self):
        value = "标题=中文视频🎬"
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " -d " + shlex.quote(value)).body, value.encode("utf-8"))

    def test_multiple_literal_data_values_merge_with_ampersand(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --data a=1 --data-raw b=2 --data-binary c=3")
        self.assertEqual(context.body, b"a=1&b=2&c=3")

    def test_empty_first_data_does_not_add_leading_separator(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " -d '' -d '' -d foo").body, b"foo")

    def test_empty_later_data_adds_separator(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " -d foo -d '' -d ''").body, b"foo&&")

    def test_empty_body_differs_from_absent_body(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --data ''")
        self.assertEqual(context.body, b"")
        self.assertEqual(context.method, "POST")
        self.assertEqual(context.headers["content-type"], "application/x-www-form-urlencoded")

    def test_explicit_get_preserves_body_without_G(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -X GET --data 'a=1'")
        self.assertEqual(context.method, "GET")
        self.assertEqual(context.body, b"a=1")
        self.assertEqual(context.url, ENDPOINT)

    def test_explicit_custom_head_preserves_body(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -X HEAD --data 'a=1'")
        self.assertEqual(context.method, "HEAD")
        self.assertEqual(context.body, b"a=1")

    def test_explicit_content_type_overrides_form_default(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -H 'Content-Type: text/plain' --data-raw 'plain text'")
        self.assertEqual(context.headers["content-type"], "text/plain")

    def test_body_is_omitted_from_parsed_repr(self):
        parsed = parse_curl("curl " + shlex.quote(ENDPOINT) + " --data-raw 'private-body-fixture'")
        self.assertNotIn("private-body-fixture", repr(parsed))
        self.assertFalse(any("private-body-fixture" in warning for warning in parsed.warnings))


class UrlencodedAndJsonCases(unittest.TestCase):
    def test_urlencode_named_content(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode 'name=a b+c&d=e%2f'")
        self.assertEqual(context.body, b"name=a+b%2Bc%26d%3De%252f")
        self.assertEqual(context.headers["content-type"], "application/x-www-form-urlencoded")

    def test_urlencode_content_only(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode 'a b+c'").body, b"a+b%2Bc")

    def test_urlencode_empty_name(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode '=a b&x=2'").body, b"a+b%26x%3D2")

    def test_urlencode_empty_value(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode 'name='").body, b"name=")

    def test_urlencode_empty_literal(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode ''").body, b"")

    def test_urlencode_name_is_not_reencoded(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode 'already%20encoded=a b'").body, b"already%20encoded=a+b")

    def test_urlencode_unicode_content(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode 'title=中文'").body, b"title=%E4%B8%AD%E6%96%87")

    def test_urlencode_ansi_bytes(self):
        command = "curl " + shlex.quote(ENDPOINT) + r" --data-urlencode $'value=\xff \u00ff'"
        self.assertEqual(parse_input(command).body, b"value=%FF+%C3%BF")

    def test_urlencode_unreserved_characters(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode 'v=AZaz09-._~'").body, b"v=AZaz09-._~")

    def test_urlencode_equal_precedes_file_marker(self):
        for value, expected in (("name=mail@example.com", b"name=mail%40example.com"), ("@name=content", b"@name=content")):
            with self.subTest(value=value):
                self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode " + shlex.quote(value)).body, expected)

    def test_urlencode_multiple_values(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --data-urlencode 'first=one two' --data-urlencode 'second=+'")
        self.assertEqual(context.body, b"first=one+two&second=%2B")

    def test_json_default_method_and_headers(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --json '{\"id\":1}'")
        self.assertEqual(context.method, "POST")
        self.assertEqual(context.body, b'{"id":1}')
        self.assertEqual(context.headers["content-type"], "application/json")
        self.assertEqual(context.headers["accept"], "application/json")

    def test_json_equals_attached(self):
        self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --json='{\"id\":1}'").body, b'{"id":1}')

    def test_json_does_not_validate_or_reformat_json(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --json 'not JSON; literal bytes'")
        self.assertEqual(context.body, b"not JSON; literal bytes")

    def test_repeated_json_concatenates_without_separator(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --json '{\"id\":' --json ' 1}'")
        self.assertEqual(context.body, b'{"id": 1}')

    def test_json_and_data_separator_depends_on_later_option(self):
        forward = parse_input("curl " + shlex.quote(ENDPOINT) + " --json first --data second")
        reverse = parse_input("curl " + shlex.quote(ENDPOINT) + " --data first --json second")
        self.assertEqual(forward.body, b"first&second")
        self.assertEqual(reverse.body, b"firstsecond")
        self.assertEqual(forward.headers["content-type"], "application/json")
        self.assertEqual(reverse.headers["content-type"], "application/json")

    def test_empty_json_is_explicit_post(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --json ''")
        self.assertEqual(context.body, b"")
        self.assertEqual(context.method, "POST")
        self.assertEqual(context.headers["accept"], "application/json")

    def test_explicit_headers_override_json_defaults_in_either_order(self):
        headers = "-H 'Content-Type: application/vendor+json' -H 'Accept: video/*'"
        for options in (headers + " --json '{}'", "--json '{}' " + headers):
            with self.subTest(options=options):
                context = parse_input("curl " + shlex.quote(ENDPOINT) + " " + options)
                self.assertEqual(context.headers["content-type"], "application/vendor+json")
                self.assertEqual(context.headers["accept"], "video/*")

    def test_empty_explicit_headers_suppress_json_defaults(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --json '{}' -H 'Content-Type:' -H 'Accept;'")
        self.assertNotIn("content-type", context.headers)
        self.assertNotIn("accept", context.headers)

    def test_empty_explicit_header_suppresses_form_default(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -H 'Content-Type:' --data value")
        self.assertNotIn("content-type", context.headers)


class GetQueryCases(unittest.TestCase):
    def test_G_preserves_existing_signed_query(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G --data 'extra=a+b%2f&empty='")
        self.assertEqual(context.url, ENDPOINT + "&extra=a+b%2f&empty=")
        self.assertEqual(context.method, "GET")
        self.assertIsNone(context.body)
        self.assertNotIn("content-type", context.headers)

    def test_G_adds_query_before_fragment(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT + "#part+1") + " --get -d 'extra=1'")
        self.assertEqual(context.url, ENDPOINT + "&extra=1#part+1")

    def test_G_adds_question_mark_without_query(self):
        context = parse_input("curl 'https://example.com/playlist#part' -G -d 'extra=1'")
        self.assertEqual(context.url, "https://example.com/playlist?extra=1#part")

    def test_G_uses_empty_existing_query(self):
        context = parse_input("curl 'https://example.com/playlist?#part' -G -d 'extra=1'")
        self.assertEqual(context.url, "https://example.com/playlist?extra=1#part")

    def test_G_query_value_ending_question_mark_needs_ampersand(self):
        context = parse_input("curl 'https://example.com/playlist?sign=value?' -G -d 'extra=1'")
        self.assertEqual(context.url, "https://example.com/playlist?sign=value?&extra=1")

    def test_G_uses_existing_trailing_ampersand(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT + "&#part") + " -G -d 'extra=1'")
        self.assertEqual(context.url, ENDPOINT + "&extra=1#part")

    def test_G_preserves_repeated_empty_query_fields(self):
        url = "https://example.com/playlist?auth=a%2f&&x=&x=2&"
        context = parse_input("curl " + shlex.quote(url) + " -G -d 'x=3'")
        self.assertEqual(context.url, url + "x=3")
        self.assertEqual(context.query_parameters, [("auth", "a/"), ("x", ""), ("x", "2"), ("x", "3")])

    def test_G_urlencode_only_changes_appended_content(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G --data-urlencode 'title=one two+'")
        self.assertEqual(context.url, ENDPOINT + "&title=one+two%2B")
        self.assertEqual(context.query_parameters[-1], ("title", "one two+"))

    def test_short_G_cluster_ending_in_data(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -sSLGd'extra=1'")
        self.assertEqual(context.url, ENDPOINT + "&extra=1")
        self.assertEqual(context.method, "GET")

    def test_head_with_G_moves_body_to_query(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -IG -d 'extra=1'")
        self.assertEqual(context.method, "HEAD")
        self.assertEqual(context.url, ENDPOINT + "&extra=1")
        self.assertIsNone(context.body)

    def test_no_get_disables_G(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G --data 'extra=1' --no-get")
        self.assertEqual(context.method, "POST")
        self.assertEqual(context.body, b"extra=1")
        self.assertEqual(context.url, ENDPOINT)

    def test_get_can_be_enabled_again(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G --no-get --get --data 'extra=1'")
        self.assertEqual(context.method, "GET")
        self.assertEqual(context.url, ENDPOINT + "&extra=1")

    def test_no_head_disables_head(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -I --no-head --data 'extra=1'")
        self.assertEqual(context.method, "POST")
        self.assertEqual(context.body, b"extra=1")

    def test_explicit_method_overrides_G_method(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G --data 'extra=1' -X POST")
        self.assertEqual(context.method, "POST")
        self.assertEqual(context.url, ENDPOINT + "&extra=1")
        self.assertIsNone(context.body)

    def test_G_empty_data_preserves_url(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G --data ''")
        self.assertEqual(context.url, ENDPOINT)
        self.assertEqual(context.method, "GET")
        self.assertIsNone(context.body)

    def test_G_without_data_preserves_url(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G")
        self.assertEqual(context.url, ENDPOINT)
        self.assertEqual(context.method, "GET")

    def test_G_json_keeps_explicit_json_headers(self):
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " -G --json '{}'")
        self.assertEqual(context.url, ENDPOINT + "&{}")
        self.assertIsNone(context.body)
        self.assertEqual(context.headers["accept"], "application/json")
        self.assertEqual(context.headers["content-type"], "application/json")

    def test_G_applies_to_each_candidate_before_selection(self):
        parsed = parse_curl("curl 'https://example.com/one' 'https://example.com/two?old=%2f' -G -d 'x=1'")
        self.assertEqual(parsed.media_urls, ["https://example.com/one?x=1", "https://example.com/two?old=%2f&x=1"])
        self.assertIsNone(parsed.url)
        self.assertEqual(parsed.to_context(parsed.media_urls[0]).method, "GET")
        self.assertIsNone(parsed.to_context(parsed.media_urls[0]).body)


class SafeInputRejectionCases(unittest.TestCase):
    def assert_parse_error(self, command, code):
        with self.assertRaises(CurlParseError) as caught:
            parse_input(command)
        self.assertEqual(caught.exception.code, code)
        self.assertRegex(str(caught.exception), r"[\u4e00-\u9fff]")
        return str(caught.exception)

    def test_empty_input(self):
        for value in (None, "", " \r\n "):
            with self.subTest(value=value):
                self.assert_parse_error(value, "empty_input")

    def test_plain_invalid_url(self):
        for url in ("https:///playlist", "https://example.com:wrong/playlist", "https://example.com:65536/playlist", "https://[invalid/playlist"):
            with self.subTest(url=url):
                self.assert_parse_error(url, "invalid_url")

    def test_plain_url_credentials_are_rejected_without_leaking(self):
        message = self.assert_parse_error("https://user:private-fixture@example.com/playlist", "invalid_url")
        self.assertNotIn("private-fixture", message)

    def test_plain_url_controls_are_rejected(self):
        for control in ("\n", "\r", "\t", "\x00", "\x7f", "\ud800"):
            with self.subTest(control=control):
                self.assert_parse_error("https://example.com/playlist?x=" + control + "value", "invalid_url")

    def test_plain_non_http_input_is_not_a_request(self):
        for text in ("file:///C:/video.mp4", "ftp://example.com/video.mp4", "echo hello"):
            with self.subTest(text=text):
                self.assert_parse_error(text, "not_curl")

    def test_invalid_or_custom_methods_are_rejected(self):
        for method in ("GET1", "M-SEARCH", "GET POST", "GET\r\nInjected", "BREW", ""):
            with self.subTest(method=method):
                message = self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " --request " + shlex.quote(method), "unsupported_method")
                self.assertNotIn("Injected", message)

    def test_body_file_forms_are_rejected_without_open_or_execution(self):
        with patch("builtins.open", side_effect=AssertionError("must not read files")), patch("subprocess.Popen", side_effect=AssertionError("must not execute curl")):
            for option in ("--data", "--data-ascii", "--data-binary", "--json", "-d"):
                for value in ("@file.txt", "@-", "@C:/private-fixture"):
                    with self.subTest(option=option, value=value):
                        message = self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " " + option + " " + shlex.quote(value), "body_file")
                        self.assertNotIn("private-fixture", message)
            self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " --data-binary=@file.txt", "body_file")
            self.assertEqual(parse_input("curl " + shlex.quote(ENDPOINT) + " --data-raw @literal").body, b"@literal")

    def test_urlencoded_named_and_unnamed_files_are_rejected(self):
        for value in ("@file.txt", "@-", "name@file.txt", "name@-"):
            with self.subTest(value=value):
                self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " --data-urlencode " + shlex.quote(value), "body_file")

    def test_body_shell_expansion_is_rejected(self):
        for body in ("$TOKEN", "${TOKEN}", "$(unexecuted)", "`unexecuted`"):
            with self.subTest(body=body):
                self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + ' --data-raw "' + body + '"', "shell_expansion")

    def test_quoted_shell_text_is_literal_body(self):
        value = "$(unexecuted); | > $TOKEN"
        context = parse_input("curl " + shlex.quote(ENDPOINT) + " --data-raw " + shlex.quote(value))
        self.assertEqual(context.body, value.encode())

    def test_body_does_not_disable_command_operator_rejection(self):
        self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " --data-raw 'literal' && echo unsafe", "shell_syntax")

    def test_ansi_nul_body_is_rejected(self):
        self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + r" --data-binary $'\x00private-fixture'", "invalid_character")

    def test_invalid_unicode_body_is_rejected_with_typed_error(self):
        for option in ("--data-raw", "--data-urlencode"):
            with self.subTest(option=option):
                self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " " + option + " 'private-fixture\ud800'", "invalid_character")

    def test_non_unicode_ansi_bytes_in_url_or_header_are_rejected(self):
        self.assert_parse_error(r"curl $'https://example.com/playlist?raw=\xff'", "invalid_url")
        self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + r" -H $'x-value: \xff'", "invalid_header")

    def test_native_head_and_body_need_G(self):
        for options in ("-I -d body", "--head --json '{}'", "--data-raw body -I"):
            with self.subTest(options=options):
                self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " " + options, "incompatible_options")

    def test_head_and_get_flags_do_not_accept_values(self):
        for option in ("--head=1", "--get=1", "--no-get=1", "--no-head=1"):
            with self.subTest(option=option):
                self.assert_parse_error("curl " + shlex.quote(ENDPOINT) + " " + option, "unsupported_option")

    def test_no_selected_url_cannot_create_context(self):
        with self.assertRaises(CurlParseError) as caught:
            ParsedCurl(None).to_context()
        self.assertEqual(caught.exception.code, "multiple_urls")

    def test_invalid_selected_url_context_error_is_typed(self):
        parsed = parse_curl("curl " + shlex.quote(ENDPOINT))
        with self.assertRaises(CurlParseError) as caught:
            parsed.to_context("https://user:private-fixture@example.com/playlist")
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertNotIn("private-fixture", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
