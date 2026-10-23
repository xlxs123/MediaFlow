"""批量输入只做格式与身份去重，不访问页面或改写作品参数。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.batch_input import BatchInputError, MAX_INPUT_BYTES, parse_urls, read_url_file


class BatchInputTests(unittest.TestCase):
    def test_lines_whitespace_invalid_empty_and_existing_are_counted(self):
        result = parse_urls('  https://example.test/one  \n\nnot-a-url\nhttps://example.test/one\n'
                            'https://example.test/old\nhttps://example.test/two',
                            existing=['https://example.test/old'])
        self.assertEqual(result['urls'], ['https://example.test/one', 'https://example.test/two'])
        self.assertEqual((result['valid'], result['duplicates'], result['invalid'], result['empty']), (2, 2, 1, 1))
        self.assertEqual([row['line'] for row in result['rows']], [1, 3, 4, 5, 6])
        self.assertEqual(result['rows'][2]['status'], 'duplicate')
        self.assertEqual(result['error'], '')

    def test_work_ids_and_original_query_fragment_order_remain_exact(self):
        first = 'https://www.douyin.com/user/self?from_tab_name=main&modal_id=7526879737634950451#here'
        second = first.replace('7526879737634950451', '7657489271422549747')
        query_order = 'https://www.douyin.com/user/self?modal_id=7526879737634950451&from_tab_name=main#here'
        self.assertEqual(parse_urls('\n'.join((first, second, query_order)))['urls'], [first, second, query_order])

    def test_exact_dedup_does_not_normalize_case_encoding_or_trailing_slash(self):
        urls = ['https://example.test/item?a=%2F', 'https://example.test/item?a=%2f',
                'https://EXAMPLE.test/item?a=%2F', 'https://example.test/item/?a=%2F']
        self.assertEqual(parse_urls('\n'.join(urls))['urls'], urls)

    def test_invalid_urls_and_curl_are_never_submitted(self):
        urls = ['ftp://example.test/a', 'https://', 'https://user:pass@example.test/a',
                'https://exa mple.test/a', 'https://example.test/a b', 'https://example.test:invalid/a',
                'https://example.test:0/a', 'https://example.test:65536/a', 'https://exa%mple.test/a',
                'https://a..test/a', 'https://-foo.test/a', 'https://example.test\\other/a',
                'curl https://example.test/a', 'https://example.test/a\t-H token', 'https://example.test/a\x00']
        result = parse_urls('\n'.join(urls))
        self.assertEqual(result['urls'], [])
        self.assertEqual(result['invalid'], len(urls))
        self.assertTrue(all(row['status'] == 'invalid' for row in result['rows']))

    def test_localhost_ipv6_and_international_domains_are_valid(self):
        urls = ['http://localhost:8080/a', 'http://127.0.0.1/a', 'http://[::1]:8123/a',
                'https://例子.测试/作品?作品=一']
        self.assertEqual(parse_urls('\n'.join(urls))['urls'], urls)

    def test_501_unique_urls_fail_as_whole_and_no_silent_truncation(self):
        result = parse_urls('\n'.join(f'https://example.test/{index}' for index in range(501)))
        self.assertEqual(result['valid'], 501)
        self.assertEqual(result['urls'], [])
        self.assertIn('500', result['error'])

    def test_500_urls_plus_duplicates_is_allowed(self):
        urls = [f'https://example.test/{index}' for index in range(500)]
        result = parse_urls('\n'.join(urls + urls))
        self.assertEqual(result['urls'], urls)
        self.assertEqual(result['duplicates'], 500)
        self.assertFalse(result['error'])

    def test_oversized_text_and_bounded_rows(self):
        self.assertTrue(parse_urls('a' * (MAX_INPUT_BYTES + 1))['error'])
        result = parse_urls('invalid\n' * 6000)
        self.assertEqual(result['invalid'], 6000)
        self.assertEqual(len(result['rows']), 5000)

    def test_parser_does_not_access_network(self):
        with patch('socket.create_connection', side_effect=AssertionError('must remain offline')):
            self.assertEqual(parse_urls('https://example.test/video')['valid'], 1)

    def test_txt_encodings_are_strictly_decoded(self):
        text = 'https://example.test/作品?item=一\r\nhttps://example.test/other'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / '来源.txt'
            for encoding in ('utf-8-sig', 'utf-16', 'utf-16-be', 'gb18030'):
                raw = text.encode(encoding)
                if encoding == 'utf-16-be':
                    raw = b'\xfe\xff' + raw
                path.write_bytes(raw)
                self.assertEqual(read_url_file(path), text)
                self.assertEqual(parse_urls(read_url_file(path))['valid'], 2)

    def test_txt_binary_invalid_encoding_oversize_and_missing_file_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'list.txt'
            for raw in (b'https://example.test/a\x00garbage', b'\xff', b'\xff\xfe\x00', b'a' * (MAX_INPUT_BYTES + 1)):
                path.write_bytes(raw)
                with self.assertRaises(BatchInputError):
                    read_url_file(path)
            with self.assertRaises(BatchInputError):
                read_url_file(Path(temporary) / 'missing.txt')


if __name__ == '__main__':
    unittest.main()
