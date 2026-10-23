import unittest
from core.request_context import RequestContext, header_category
from core.errors import MediaCheckError
from utils.security import Redactor, redact_data


class RequestContextTests(unittest.TestCase):
    def context(self, **kwargs):
        values = dict(url='https://origin.test/path/index?sign=a%2Fb%2Bc&t=123&token=one&token=two', method='POST',
                      headers={'Origin': 'https://page.test', 'Referer': 'https://page.test/watch',
                               'User-Agent': 'Browser', 'Cookie': 'session=example-cookie', 'Authorization': 'Bearer example-auth'},
                      body=b'sign=body%2Bvalue')
        values.update(kwargs)
        return RequestContext(**values)

    def test_raw_query_not_reconstructed(self):
        ctx = self.context()
        self.assertIn('sign=a%2Fb%2Bc&t=123&token=one&token=two', ctx.url)
        self.assertEqual(ctx.query_parameters[-2:], [('token', 'one'), ('token', 'two')])

    def test_relative_key_and_segment_inherit_all_context(self):
        for resource in ('key.dat', 'segmt0.ts', 'https://cdn.test/segmt1.ts'):
            child = self.context().derive(resource)
            self.assertEqual(child.headers, self.context().headers)
            self.assertEqual(child.method, 'GET')
            self.assertIsNone(child.body)
            self.assertEqual(child.cookies, 'session=example-cookie')

    def test_relative_uses_final_manifest_url(self):
        ctx = self.context().redirected('https://cdn.test/video/index?token=final', 307)
        self.assertEqual(ctx.original_url, self.context().url)
        self.assertEqual(ctx.derive('key.dat').url, 'https://cdn.test/video/key.dat')
        self.assertEqual(ctx.derive('part.ts?sign=child%2Fvalue&t=5').url, 'https://cdn.test/video/part.ts?sign=child%2Fvalue&t=5')

    def test_redirect_307_308_preserve_method_body(self):
        for code in (307, 308):
            ctx = self.context().redirected('https://cdn.test/next', code)
            self.assertEqual(ctx.method, 'POST')
            self.assertEqual(ctx.body, self.context().body)
            self.assertEqual(ctx.headers, self.context().headers)

    def test_redirect_301_302_303_post_to_get(self):
        for code in (301, 302, 303):
            ctx = self.context().redirected('https://cdn.test/next', code)
            self.assertEqual(ctx.method, 'GET')
            self.assertIsNone(ctx.body)

    def test_https_downgrade_and_non_http_refused(self):
        with self.assertRaises(MediaCheckError):
            self.context().redirected('http://cdn.test/next', 302)
        with self.assertRaises(MediaCheckError):
            self.context().derive('file:///tmp/key.dat')

    def test_context_never_mutates_input_headers(self):
        headers = {'User-Agent': 'a'}
        ctx = RequestContext('https://x.test/media', headers=headers)
        headers['User-Agent'] = 'b'
        self.assertEqual(ctx.user_agent, 'a')

    def test_resource_range_and_body_headers_never_inherit(self):
        headers = {**self.context().headers, 'Range': 'bytes=0-', 'If-Range': 'original-etag',
                   'If-None-Match': 'original-etag', 'Content-Type': 'application/json',
                   'Content-Encoding': 'gzip', 'Host': 'origin.test', 'Content-Length': '12',
                   'X-API-Key': 'custom-session-key'}
        root = self.context(headers=headers)
        for uri in ('enc.key', 'abc000.png', 'part.bin', 'next-manifest'):
            with self.subTest(uri=uri):
                child = root.derive(uri)
                self.assertNotIn('range', child.headers)
                self.assertNotIn('if-range', child.headers)
                self.assertNotIn('content-type', child.headers)
                self.assertEqual(child.headers['x-api-key'], 'custom-session-key')
                self.assertEqual(child.origin, root.origin)
                self.assertEqual(child.referer, root.referer)
                self.assertEqual(child.cookies, root.cookies)
                self.assertEqual(child.headers['authorization'], root.headers['authorization'])
                self.assertIn('range', child.summary()['dropped_headers'])
        self.assertEqual(root.headers['range'], 'bytes=0-')

    def test_origin_is_not_invented_from_referer_or_url(self):
        root = RequestContext('https://cdn.test/master', headers={'Referer': 'https://page.test/watch'})
        self.assertNotIn('origin', root.headers)
        self.assertNotIn('origin', root.derive('enc.key').headers)

    def test_browser_metadata_saved_but_not_inherited_by_default(self):
        names = ('Sec-Fetch-Dest', 'Sec-Fetch-Mode', 'Sec-Fetch-Site', 'Sec-Fetch-Storage-Access',
                 'sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform')
        headers = {name: 'browser-value' for name in names}
        root = self.context(headers={**self.context().headers, **headers})
        child = root.derive('abc000.png')
        for name in names:
            self.assertNotIn(name.lower(), child.headers)
            self.assertEqual(root.summary()['browser_metadata'][name.lower()], 'browser-value')
            self.assertEqual(child.summary()['browser_metadata'][name.lower()], 'browser-value')
        self.assertFalse(child.inherit_browser_metadata)

    def test_explicit_range_inherits_across_hosts_without_origin_or_credentials(self):
        headers = {'Referer': 'https://page.test/watch', 'User-Agent': 'Browser', 'Range': 'bytes=0-',
                   'If-Range': 'manifest-etag', 'Content-Type': 'application/json', 'Sec-Fetch-Site': 'cross-site'}
        root = RequestContext('https://manifest.test/path/index.m3u8?sign=keep%2Fraw', headers=headers,
                              inherit_range=True)
        children = [root.derive('enc.key'), root.derive('https://segment.test/abc000.png'),
                    root.derive('variant').derive('abc001.png')]
        for child in children:
            with self.subTest(url=child.url):
                self.assertEqual(child.headers, {'referer': headers['Referer'], 'user-agent': 'Browser', 'range': 'bytes=0-'})
                self.assertTrue(child.inherit_range)
                self.assertNotIn('range', child.dropped_headers)
                self.assertEqual(child.summary()['request_range'], 'bytes=0-')
                self.assertTrue(child.summary()['inherit_range'])
        self.assertIn('sign=keep%2Fraw', root.url)

    def test_range_strategy_survives_redirect_and_never_invents_range(self):
        root = RequestContext('https://manifest.test/index', headers={'Range': 'bytes=0-'}, inherit_range=True)
        for status in (301, 302, 307, 308):
            redirected = root.redirected('https://redirect.test/folder/index', status)
            self.assertEqual(redirected.derive('enc.key').headers['range'], 'bytes=0-')
            self.assertTrue(redirected.derive('abc000.png').inherit_range)
        empty = RequestContext(root.url, inherit_range=True).derive('enc.key')
        self.assertNotIn('range', empty.headers)

    def test_browser_and_range_options_are_independent(self):
        root = self.context(headers={'Range': 'bytes=0-', 'Sec-Fetch-Mode': 'cors'}, inherit_range=True)
        child = root.derive('enc.key')
        self.assertEqual(child.headers, {'range': 'bytes=0-'})
        self.assertFalse(child.inherit_browser_metadata)

    def test_explicit_browser_metadata_compatibility_never_restores_range(self):
        root = self.context(headers={'Sec-Fetch-Mode': 'cors', 'sec-ch-ua': 'Browser', 'Range': 'bytes=0-'},
                            inherit_browser_metadata=True)
        for child in (root.derive('enc.key'), root.derive('variant').derive('abc001.png')):
            self.assertEqual(child.headers['sec-fetch-mode'], 'cors')
            self.assertEqual(child.headers['sec-ch-ua'], 'Browser')
            self.assertNotIn('range', child.headers)

    def test_header_categories_are_not_a_credential_whitelist(self):
        for name, category in (('User-Agent', 'session'), ('Cookie', 'session'), ('Origin', 'origin'),
                               ('Range', 'resource'), ('Sec-Fetch-Site', 'browser'), ('X-API-Key', 'custom')):
            self.assertEqual(header_category(name), category)

    def test_invalid_method(self):
        with self.assertRaises(MediaCheckError):
            self.context(method='GET\r\nInjected')

    def test_summary_does_not_contain_secrets_or_body(self):
        summary = str(self.context().summary())
        for text in ('example-cookie', 'example-auth', 'body%2Bvalue'):
            self.assertNotIn(text, summary)

    def test_structured_redaction_keeps_numeric_http(self):
        redactor = Redactor(urls=['https://x.test/?token=403'])
        result = redact_data({'http_status': 403, 'drm': False, 'url': 'https://x.test/?token=403'}, redactor)
        self.assertEqual(result['http_status'], 403)
        self.assertIs(result['drm'], False)
        self.assertNotIn('token=403', result['url'])

    def test_relative_key_uri_queries_and_inline_key_are_redacted(self):
        result = redact_data({'keys': [{'uri': 'key.dat?token=new-private-token'},
                                       {'uri': 'data:application/octet-stream;base64,private-key-data'}]}, Redactor())
        self.assertNotIn('new-private-token', str(result))
        self.assertNotIn('private-key-data', str(result))

    def test_short_signature_never_corrupts_protocol_values(self):
        result = redact_data({'classification': 'HLS_AES128', 'encryption': 'AES-128',
                              'keys': [{'iv': '0x00000000000000000000000000000000', 'method': 'AES-128'}]},
                             Redactor(urls=['https://x.test/?sign=128&t=0']))
        self.assertEqual(result['classification'], 'HLS_AES128')
        self.assertEqual(result['keys'][0]['iv'], '0x00000000000000000000000000000000')
        result = redact_data({'request_range': 'bytes=0-'}, Redactor(urls=['https://x.test/?sign=0']))
        self.assertEqual(result['request_range'], 'bytes=0-')

    def test_query_redaction_keeps_public_origin_and_progress(self):
        redactor = Redactor(urls=['https://x.test/?sign=fixture&t=123'])
        self.assertEqual(redactor.redact('https://local.fixture'), 'https://local.fixture')
        self.assertEqual(redactor.redact('speed 123.5 KiB/s'), 'speed 123.5 KiB/s')
        self.assertNotIn('sign=fixture', redactor.redact('https://x.test/?sign=fixture&t=123'))
        self.assertNotIn('"fixture"', redactor.redact('signature "fixture"'))
