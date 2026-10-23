"""Cookie 后端本地回归；所有凭据/Firefox profile 都是临时合成夹具。"""
from dataclasses import FrozenInstanceError
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import browser_cookies as backend
from core.browser_cookies import CookieImportError, ScopedCookie, export_browser_cookies, load_cookie_file


HEADER = '# Netscape HTTP Cookie File\n'


def entry(domain='example.com', include='FALSE', path='/', secure='FALSE',
          expires='0', name='session', value='synthetic-value', http_only=False):
    prefix = '#HttpOnly_' if http_only else ''
    return prefix + '\t'.join((domain, include, path, secure, str(expires), name, value)) + '\n'


class FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('synthetic-tool', timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeTree:
    def __init__(self, process):
        self.process = process
        self.terminated = self.closed = False

    def terminate(self):
        self.terminated = True
        self.process.kill()

    def close(self):
        self.closed = True


class CookieFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'synthetic-cookies.txt'

    def load(self, rows, url='https://example.com/watch', header=HEADER):
        self.path.write_text(header + rows, encoding='utf-8')
        return load_cookie_file(self.path, url)

    def assert_error(self, expected, operation):
        with self.assertRaises(CookieImportError) as raised:
            operation()
        self.assertEqual(raised.exception.code, expected)

    def test_scope_preserves_attributes_and_skips_unrelated_hosts(self):
        future = int(time.time()) + 3600
        cookies = self.load(entry('.example.com', 'TRUE', '/api', 'TRUE', future,
                                  'auth', 'synthetic-secret', True)
                            + entry('video.example.com', name='host')
                            + entry('elsewhere.example', name='unrelated')
                            + entry('.evil-example.com', 'TRUE', name='lookalike'),
                            'https://video.example.com/watch')
        self.assertEqual(len(cookies), 2)
        domain_cookie, host_cookie = cookies
        self.assertEqual((domain_cookie.name, domain_cookie.value, domain_cookie.domain,
                          domain_cookie.path, domain_cookie.secure, domain_cookie.http_only,
                          domain_cookie.expires, domain_cookie.host_only),
                         ('auth', 'synthetic-secret', '.example.com', '/api', True, True, future, False))
        self.assertTrue(host_cookie.host_only)
        self.assertIsNone(host_cookie.expires)

    def test_host_only_is_not_expanded_to_subdomains(self):
        cookies = self.load(entry('example.com') + entry('.example.com', 'TRUE', name='parent'),
                            'https://video.example.com/')
        self.assertEqual([cookie.name for cookie in cookies], ['parent'])
        self.assertEqual(len(self.load(entry('example.com'))), 1)

    def test_public_suffixes_are_rejected_with_existing_qt_psl(self):
        for suffix, host in (('com', 'www.example.com'), ('co.uk', 'www.example.co.uk'),
                             ('com.cn', 'www.example.com.cn'), ('github.io', 'user.github.io')):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.load(entry('.' + suffix, 'TRUE'), 'https://' + host), [])

    def test_ip_localhost_and_ipv6_support_only_host_cookies(self):
        for host, url in (('127.0.0.1', 'http://127.0.0.1:8123/watch'),
                          ('localhost', 'http://localhost/watch'), ('::1', 'http://[::1]/watch')):
            with self.subTest(host=host):
                cookies = self.load(entry(host, http_only=True), url)
                self.assertEqual(len(cookies), 1)
                self.assertTrue(cookies[0].host_only)
                self.assertTrue(cookies[0].http_only)
                self.assertEqual(self.load(entry('.' + host, 'TRUE'), url), [])

    def test_domains_are_canonicalized_without_widening(self):
        cookies = self.load(entry('.EXAMPLE.COM', 'TRUE') + entry('example.com.', name='bad'),
                            'https://VIDEO.EXAMPLE.COM./watch')
        self.assertEqual([cookie.domain for cookie in cookies], ['.example.com'])
        unicode_cookie = self.load(entry('例子.测试'), 'https://例子.测试/watch')
        self.assertEqual(unicode_cookie[0].domain, 'xn--fsqu00a.xn--0zwm56d')

    def test_invalid_domains_do_not_enter_jar(self):
        rows = ''.join(entry(domain, 'TRUE', name='d' + str(index)) for index, domain in enumerate(
            ('..example.com', '.-example.com', '.bad_.example.com', '.example..com',
             '.example.com:443', '.example.com/evil', '.127.0.0.1', '.com')))
        self.assertEqual(self.load(rows), [])
        self.assertEqual(self.load(entry('.example.com', 'FALSE')), [])

    def test_session_expiry_and_expired_values(self):
        rows = entry(expires='', name='blank') + entry(expires='0', name='zero')
        for expiry in (str(int(time.time()) - 1), '-1', 'not-a-time', '999999999999', '1.5'):
            rows += entry(expires=expiry, name='bad-' + expiry)
        cookies = self.load(rows)
        self.assertEqual([cookie.name for cookie in cookies], ['blank', 'zero'])
        self.assertTrue(all(cookie.expires is None for cookie in cookies))

    def test_control_characters_invalid_names_paths_and_flags_are_skipped(self):
        rows = (entry(value='bad\x00value') + entry(name='bad name') + entry(path='relative')
                + entry(secure='MAYBE') + entry(include='YES') + entry(value='bad\u202evalue')
                + entry(name='valid', value='literal=escaped%20value;data'))
        with mock.patch('warnings.warn') as warning:
            cookies = self.load(rows)
        warning.assert_not_called()
        self.assertEqual([(cookie.name, cookie.value) for cookie in cookies],
                         [('valid', 'literal=escaped%20value;data')])

    def test_duplicate_identity_last_value_wins(self):
        cookies = self.load(entry(value='first') + entry(value='second'))
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0].value, 'second')

    def test_bom_crlf_and_http_cookie_header_are_supported(self):
        self.path.write_bytes(('\ufeff# HTTP Cookie File\r\n' + entry(http_only=True).replace('\n', '\r\n')).encode())
        cookies = load_cookie_file(self.path, 'https://example.com')
        self.assertEqual(len(cookies), 1)
        self.assertTrue(cookies[0].http_only)

    def test_invalid_file_and_invalid_encoding_use_static_errors(self):
        self.assert_error('INVALID_FILE', lambda: load_cookie_file(self.path, 'https://example.com'))
        self.assert_error('INVALID_FILE', lambda: self.load('synthetic-secret', header='not-a-cookie-file\n'))
        self.path.write_bytes(HEADER.encode() + b'\xff')
        self.assert_error('INVALID_FILE', lambda: load_cookie_file(self.path, 'https://example.com'))

    def test_size_and_count_limits_fail_before_returning_partial_credentials(self):
        with mock.patch.object(backend, 'MAX_COOKIE_FILE_BYTES', 20):
            self.assert_error('FILE_TOO_LARGE', lambda: self.load(entry()))
        with mock.patch.object(backend, 'MAX_COOKIE_COUNT', 1):
            self.assert_error('TOO_MANY_COOKIES', lambda: self.load(entry() + entry('unrelated.test')))

    def test_oversized_cookie_line_is_skipped(self):
        with mock.patch.object(backend, 'MAX_COOKIE_LINE_BYTES', 100):
            cookies = self.load(entry(value='x' * 150) + entry(name='small', value='ok'))
        self.assertEqual([cookie.name for cookie in cookies], ['small'])

    def test_invalid_page_urls_are_rejected(self):
        for url in ('file:///tmp/x', 'javascript:alert(1)', 'https://', 'http://x:bad',
                    'https://user:synthetic-secret@example.com', 'https://exam\nple.com',
                    'https://-bad.test', 'https://127.999.0.1', None):
            with self.subTest(url=url):
                self.assert_error('INVALID_PAGE', lambda: self.load(entry(), url))

    def test_repr_and_errors_never_echo_credentials_or_arbitrary_error_text(self):
        cookie = ScopedCookie('sensitive-name', 'sensitive-value', 'sensitive-domain',
                              '/sensitive-path', True, True, None, True)
        for value in ('sensitive-name', 'sensitive-value', 'sensitive-domain', 'sensitive-path'):
            self.assertNotIn(value, repr(cookie))
        self.assertNotIn('raw-sensitive-error', str(CookieImportError('raw-sensitive-error')))
        with self.assertRaises(FrozenInstanceError):
            cookie.value = 'change'


class CookieExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tool = Path(self.directory.name) / 'synthetic-yt-dlp.exe'
        self.tool.write_bytes(b'not executed; process mock fixture')
        self.recorded = {}
        self.process = FakeProcess()
        self.tree = FakeTree(self.process)

    def launch(self, args, **kwargs):
        self.recorded.update(args=args, kwargs=kwargs)
        info = Path(args[args.index('--load-info-json') + 1])
        jar = Path(args[args.index('--cookies') + 1])
        self.recorded.update(info_path=info, jar_path=jar, info=json.loads(info.read_text(encoding='utf-8')))
        jar.write_text(HEADER + entry(http_only=True) + entry('unrelated.test'), encoding='utf-8')
        return self.process

    def export(self, **kwargs):
        with mock.patch.object(backend.subprocess, 'Popen', side_effect=self.launch), \
             mock.patch.object(backend, 'ProcessTree', return_value=self.tree):
            return export_browser_cookies(self.tool, 'firefox', 'https://example.com/page?token=do-not-save', **kwargs)

    def assert_cleaned(self):
        self.assertFalse(self.recorded['info_path'].exists())
        self.assertFalse(self.recorded['jar_path'].exists())
        self.assertTrue(self.tree.closed)

    def test_success_uses_offline_infojson_strict_args_and_cleans_plaintext(self):
        cookies = self.export()
        self.assertEqual(len(cookies), 1)
        self.assertTrue(cookies[0].http_only)
        args, kwargs = self.recorded['args'], self.recorded['kwargs']
        for flag in ('--ignore-config', '--no-plugin-dirs', '--no-cache-dir', '--simulate',
                     '--skip-download', '--no-check-formats', '--load-info-json', '--cookies'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--cookies-from-browser') + 1], 'firefox')
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['creationflags'], backend.NO_WINDOW)
        self.assertEqual(self.recorded['info']['url'], 'https://cookie-import.invalid/placeholder')
        self.assertNotIn('do-not-save', json.dumps(self.recorded['info']))
        self.assertNotIn('do-not-save', ' '.join(args))
        self.assert_cleaned()

    def test_browser_whitelist_rejects_shell_and_custom_profile(self):
        with mock.patch.object(backend.subprocess, 'Popen') as popen:
            for browser in ('chrome;evil', 'firefox:C:/profile', 'edge --arg', 'safari', '', None):
                with self.subTest(browser=browser), self.assertRaises(CookieImportError) as raised:
                    export_browser_cookies(self.tool, browser, 'https://example.com')
                self.assertEqual(raised.exception.code, 'INVALID_BROWSER')
        popen.assert_not_called()

    def test_nonzero_exit_uses_static_error_and_cleans(self):
        self.process.returncode = 1
        with self.assertRaises(CookieImportError) as raised:
            self.export()
        self.assertEqual(raised.exception.code, 'BROWSER_EXPORT_FAILED')
        self.assertIn('加密保护', str(raised.exception))
        self.assert_cleaned()

    def test_export_protects_missing_http_only_but_file_import_keeps_explicit_semantics(self):
        # A browser exporter can lose the source HttpOnly metadata. Never turn
        # that unknown attribute into a newly JavaScript-readable credential.
        original = ScopedCookie('session', 'synthetic-value', 'example.com', '/',
                                True, False, None, True)
        with mock.patch.object(backend, 'load_cookie_file', return_value=[original]):
            exported = self.export()
        self.assertTrue(exported[0].http_only)
        self.assertEqual((exported[0].name, exported[0].value, exported[0].domain,
                          exported[0].path, exported[0].secure, exported[0].expires,
                          exported[0].host_only),
                         (original.name, original.value, original.domain, original.path,
                          original.secure, original.expires, original.host_only))
        self.assertFalse(original.http_only)
        jar = Path(self.directory.name) / 'explicit-file.txt'
        jar.write_text(HEADER + entry(), encoding='utf-8')
        self.assertFalse(load_cookie_file(jar, 'https://example.com')[0].http_only)
        self.assert_cleaned()

    def test_cancel_before_launch_does_not_read_browser(self):
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(backend.subprocess, 'Popen') as popen, self.assertRaises(CookieImportError) as raised:
            self.export(cancel_event=cancelled)
        self.assertEqual(raised.exception.code, 'CANCELLED')
        popen.assert_not_called()

    def test_cancel_during_process_terminates_own_tree_and_cleans(self):
        self.process.returncode = None
        event = mock.Mock()
        event.is_set.side_effect = [False, True]
        with self.assertRaises(CookieImportError) as raised:
            self.export(cancel_event=event)
        self.assertEqual(raised.exception.code, 'CANCELLED')
        self.assertTrue(self.tree.terminated)
        self.assert_cleaned()

    def test_timeout_terminates_own_tree_and_cleans(self):
        self.process.returncode = None
        with mock.patch.object(backend.time, 'monotonic', side_effect=[0, 2]), \
             self.assertRaises(CookieImportError) as raised:
            self.export(timeout=1)
        self.assertEqual(raised.exception.code, 'TIMEOUT')
        self.assertTrue(self.tree.terminated)
        self.assert_cleaned()

    def test_missing_tool_and_invalid_timeout_do_not_spawn(self):
        with mock.patch.object(backend.subprocess, 'Popen') as popen:
            with self.assertRaises(CookieImportError) as raised:
                export_browser_cookies(self.tool.with_name('missing.exe'), 'chrome', 'https://example.com')
            self.assertEqual(raised.exception.code, 'TOOL_UNAVAILABLE')
            for timeout in (0, -1, float('nan'), float('inf'), '40', True):
                with self.assertRaises(CookieImportError) as raised:
                    self.export(timeout=timeout)
                self.assertEqual(raised.exception.code, 'INVALID_TIMEOUT')
        popen.assert_not_called()

    def test_popen_error_does_not_echo_tool_error(self):
        with mock.patch.object(backend.subprocess, 'Popen', side_effect=OSError('raw-sensitive-error')), \
             self.assertRaises(CookieImportError) as raised:
            export_browser_cookies(self.tool, 'chrome', 'https://example.com')
        self.assertNotIn('raw-sensitive-error', str(raised.exception))
        self.assertEqual(raised.exception.code, 'BROWSER_EXPORT_FAILED')

    def test_process_tree_initialization_failure_still_reaps_own_process(self):
        self.process.returncode = None
        with mock.patch.object(backend.subprocess, 'Popen', side_effect=self.launch), \
             mock.patch.object(backend, 'ProcessTree', side_effect=OSError('synthetic-error')), \
             self.assertRaises(CookieImportError):
            export_browser_cookies(self.tool, 'firefox', 'https://example.com')
        self.assertTrue(self.process.killed)
        self.assertFalse(self.recorded['jar_path'].exists())


class SyntheticFirefoxIntegrationTests(unittest.TestCase):
    def test_bundled_yt_dlp_offline_flags_with_synthetic_sqlite_profile(self):
        """真实运行本地 yt-dlp；只注入临时 Firefox SQLite，无真实 profile/页面。"""
        tool = Path(__file__).resolve().parents[1] / 'tools' / 'yt-dlp.exe'
        if not tool.is_file():
            self.skipTest('项目本地 yt-dlp.exe 不存在')
        real_popen = subprocess.Popen
        paths = []
        with tempfile.TemporaryDirectory(prefix='mediaflow-synthetic-firefox-') as directory:
            profile = Path(directory)
            with closing(sqlite3.connect(profile / 'cookies.sqlite')) as database:
                database.execute('CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT, '
                                 'name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, '
                                 'isSecure INTEGER, isHttpOnly INTEGER)')
                database.execute('INSERT INTO moz_cookies VALUES (1,?,?,?,?,?,?,?,?)',
                    ('', 'synthetic_session', 'local-only-fixture', 'example.com', '/',
                     int(time.time()) + 3600, 1, 1))
                database.execute('INSERT INTO moz_cookies VALUES (2,?,?,?,?,?,?,?,?)',
                    ('', 'unrelated', 'local-only-fixture', 'unrelated.test', '/',
                     int(time.time()) + 3600, 0, 0))
                database.commit()

            def synthetic_profile_launch(args, **kwargs):
                arguments = list(args)
                # Test-only replacement; production accepts browser names only.
                arguments[arguments.index('--cookies-from-browser') + 1] = 'firefox:' + str(profile)
                paths.append(Path(arguments[arguments.index('--cookies') + 1]))
                return real_popen(arguments, **kwargs)

            with mock.patch.object(backend.subprocess, 'Popen', side_effect=synthetic_profile_launch):
                cookies = export_browser_cookies(tool, 'firefox', 'https://example.com/watch', timeout=25)
            self.assertEqual(len(cookies), 1)
            self.assertEqual(cookies[0].name, 'synthetic_session')
            self.assertTrue(cookies[0].secure)
            # Bundled yt-dlp drops Firefox isHttpOnly while creating its jar.
            # System export conservatively protects omitted flags as HttpOnly;
            # this does not claim reconstruction of all original attributes.
            self.assertTrue(cookies[0].http_only)
            self.assertTrue(cookies[0].host_only)
            self.assertTrue(all(not path.exists() for path in paths))


if __name__ == '__main__':
    unittest.main()
