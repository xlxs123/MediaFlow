"""Acceptance harness tests; media/worker results are tested by the real EXE run."""
from __future__ import annotations

from http.client import HTTPConnection, IncompleteRead
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.recovery_acceptance import (
    CASES, _CUT_BYTES, _FixtureServer, _nonzero_range, _read_live_json, _request,
    _verified_partials, _write_json,
    parse_arguments,
)


class AcceptanceArgumentTests(unittest.TestCase):
    def test_source_and_exe_argument_forms_are_the_same(self):
        source = parse_arguments(['main.py', '--recovery-acceptance', 'new-evidence',
                                  '--recovery-case', 'direct-resume'])
        packaged = parse_arguments(['--recovery-acceptance', 'new-evidence',
                                    '--recovery-case', 'direct-resume'])
        self.assertEqual(source, packaged)

    def test_internal_stage_cannot_access_a_remote_url(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_arguments(['--recovery-acceptance', 'new-evidence',
                             '--recovery-stage', 'crash-download',
                             '--recovery-url', 'https://example.com/process-recovery/direct.mp4'])

    def test_internal_stage_requires_fixed_local_path_without_credentials(self):
        for url in ('http://127.0.0.1:1234/other',
                    'http://user:password@127.0.0.1:1234/process-recovery/direct.mp4',
                    'http://127.0.0.1:1234/process-recovery/direct.mp4?token=secret'):
            with self.subTest(url=url), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_arguments(['--recovery-acceptance', 'new-evidence',
                                 '--recovery-stage', 'resume-download', '--recovery-url', url])

    def test_only_nonzero_requested_ranges_are_resume_evidence(self):
        requests = [
            {'range': '', 'status': 200}, {'range': 'bytes=0-', 'status': 206},
            {'range': 'bytes=1024-', 'status': 200},
            {'range': 'bytes=2048-', 'status': 206},
            {'range': 'bytes=999-1000,2000-', 'status': 206},
        ]
        self.assertEqual(_nonzero_range(requests, status=206), [requests[3]])

    def test_mp3_and_ui_recovery_are_independent_selectable_cases(self):
        self.assertEqual(len(CASES), 13)
        for case in ('mp3', 'ui-recovery'):
            self.assertEqual(parse_arguments(['--recovery-acceptance', 'new-evidence',
                                              '--recovery-case', case]).recovery_case, case)
        self.assertTrue(parse_arguments(['--recovery-acceptance', 'new-evidence',
                                         '--recovery-case', 'ui-recovery',
                                         '--native-smoke']).native_smoke)


class AcceptanceServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.payload = bytes(range(256)) * ((_CUT_BYTES * 3) // 256)
        self.alternative = bytes(reversed(range(256))) * ((_CUT_BYTES * 3) // 256)
        direct, changed = root / 'direct.mp4', root / 'changed.mp4'
        direct.write_bytes(self.payload)
        changed.write_bytes(self.alternative)
        mp3 = root / 'audio.mp3'
        mp3.write_bytes(b'ID3' + bytes(range(256)) * 16)
        self.assets = dict(direct=direct, changed=changed, mp3=mp3)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _connection(server):
        return HTTPConnection('127.0.0.1', server.server.server_port, timeout=3)

    def _interrupt(self, server, case):
        # MediaGuard and yt-dlp's generic extractor each inspect the media.
        # The third full response is the transfer, interrupted at a known offset.
        for _ in range(2):
            connection = self._connection(server)
            connection.request('GET', f'/{case}/direct.mp4')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.payload)
            connection.close()
        connection = self._connection(server)
        connection.request('GET', f'/{case}/direct.mp4')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        with self.assertRaises(IncompleteRead) as failure:
            response.read()
        connection.close()
        self.assertEqual(len(failure.exception.partial), _CUT_BYTES)

    def test_server_really_truncates_then_serves_the_requested_206_suffix(self):
        with _FixtureServer(self.assets) as server:
            self._interrupt(server, 'direct-resume')
            connection = self._connection(server)
            connection.request('GET', '/direct-resume/direct.mp4',
                               headers={'Range': f'bytes={_CUT_BYTES}-'})
            response = connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.getheader('Content-Range'),
                             f'bytes {_CUT_BYTES}-{len(self.payload) - 1}/{len(self.payload)}')
            self.assertEqual(response.read(), self.payload[_CUT_BYTES:])
            connection.close()
            self.assertEqual(len(_nonzero_range(server.snapshot('direct-resume'), status=206)), 1)

    def test_ignored_range_returns_a_complete_200_body(self):
        with _FixtureServer(self.assets) as server:
            self._interrupt(server, 'direct-ignore-range')
            connection = self._connection(server)
            connection.request('GET', '/direct-ignore-range/direct.mp4',
                               headers={'Range': f'bytes={_CUT_BYTES}-'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIsNone(response.getheader('Content-Range'))
            self.assertEqual(response.read(), self.payload)
            connection.close()

    def test_wrong_range_and_changed_etag_are_observable_faults(self):
        for case in ('direct-bad-range', 'direct-changed'):
            with self.subTest(case=case), _FixtureServer(self.assets) as server:
                self._interrupt(server, case)
                connection = self._connection(server)
                connection.request('GET', f'/{case}/direct.mp4',
                                   headers={'Range': f'bytes={_CUT_BYTES}-'})
                response = connection.getresponse()
                self.assertEqual(response.status, 206)
                if case == 'direct-bad-range':
                    self.assertTrue(response.getheader('Content-Range').startswith(
                        f'bytes {_CUT_BYTES + 1}-'))
                    self.assertEqual(response.read(), self.payload[_CUT_BYTES:])
                else:
                    self.assertEqual(response.getheader('ETag'), '"changed-v2"')
                    self.assertEqual(response.read(), self.alternative[_CUT_BYTES:])
                connection.close()

    def test_416_is_injected_once_and_does_not_claim_the_partial_is_complete(self):
        with _FixtureServer(self.assets) as server:
            self._interrupt(server, 'direct-416')
            connection = self._connection(server)
            connection.request('GET', '/direct-416/direct.mp4',
                               headers={'Range': f'bytes={_CUT_BYTES}-'})
            response = connection.getresponse()
            self.assertEqual(response.status, 416)
            self.assertEqual(response.getheader('Content-Range'), f'bytes */{len(self.payload)}')
            self.assertGreater(len(self.payload), _CUT_BYTES)
            self.assertEqual(response.read(), b'')
            connection.close()
            connection = self._connection(server)
            connection.request('GET', '/direct-416/direct.mp4',
                               headers={'Range': f'bytes={_CUT_BYTES}-'})
            response = connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.payload[_CUT_BYTES:])
            connection.close()

    def test_paths_outside_the_registered_fixture_are_rejected(self):
        with _FixtureServer(self.assets) as server:
            for path in ('/direct-resume/../direct.mp4', '/arbitrary/direct.mp4',
                         '/direct-resume/direct.mp4?token=secret'):
                with self.subTest(path=path):
                    connection = self._connection(server)
                    connection.request('GET', path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 404)
                    response.read()
                    connection.close()

    def test_audio_route_preserves_mp3_mime_and_body(self):
        with _FixtureServer(self.assets) as server:
            connection = self._connection(server)
            connection.request('GET', '/mp3/audio.mp3')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type'), 'audio/mpeg')
            self.assertEqual(response.read(), self.assets['mp3'].read_bytes())
            connection.close()

    def _prime_ui_inspections(self, server):
        for _ in range(2):
            connection = self._connection(server)
            connection.request('GET', '/ui-recovery/direct.mp4',
                               headers={'Range': 'bytes=0-1023'})
            response = connection.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), self.payload[:1024])
            connection.close()

    def test_ui_window_holds_a_real_incomplete_body_until_explicit_release(self):
        with _FixtureServer(self.assets) as server:
            self._prime_ui_inspections(server)
            window = server.hold_ui_transfer(timeout=3, hold_after=_CUT_BYTES, chunk_delay=0)
            connection = self._connection(server)
            connection.request('GET', '/ui-recovery/direct.mp4')
            response = connection.getresponse()
            self.assertEqual(response.read(_CUT_BYTES), self.payload[:_CUT_BYTES])
            self.assertTrue(window.entered.wait(1))
            self.assertFalse(window.released.is_set())
            tail = []
            reader = threading.Thread(target=lambda: tail.append(response.read()))
            reader.start()
            try:
                reader.join(timeout=.1)
                self.assertTrue(reader.is_alive(), 'HTTP body reached EOF before the close test')
                request = server.snapshot('ui-recovery')[-1]
                self.assertEqual(request['ui_window_offset'], _CUT_BYTES)
                self.assertLess(request['sent_bytes'], request['content_length'])
            finally:
                window.release()
                reader.join(timeout=3)
                connection.close()
            self.assertFalse(reader.is_alive())
            self.assertEqual(tail, [self.payload[_CUT_BYTES:]])
            self.assertFalse(window.timed_out)

    def test_ui_window_timeout_is_bounded_and_keeps_response_incomplete(self):
        with _FixtureServer(self.assets) as server:
            self._prime_ui_inspections(server)
            window = server.hold_ui_transfer(timeout=.1, hold_after=_CUT_BYTES, chunk_delay=0)
            connection = self._connection(server)
            connection.request('GET', '/ui-recovery/direct.mp4')
            response = connection.getresponse()
            with self.assertRaises(IncompleteRead) as failure:
                response.read()
            connection.close()
            self.assertEqual(failure.exception.partial, self.payload[:_CUT_BYTES])
            self.assertTrue(window.timed_out)
            self.assertTrue(server.snapshot('ui-recovery')[-1]['ui_window_timed_out'])

    def test_server_cleanup_releases_the_ui_window(self):
        tail = []
        with _FixtureServer(self.assets) as server:
            self._prime_ui_inspections(server)
            window = server.hold_ui_transfer(timeout=3, hold_after=_CUT_BYTES, chunk_delay=0)
            connection = self._connection(server)
            connection.request('GET', '/ui-recovery/direct.mp4')
            response = connection.getresponse()
            self.assertEqual(response.read(_CUT_BYTES), self.payload[:_CUT_BYTES])
            self.assertTrue(window.entered.wait(1))
            reader = threading.Thread(target=lambda: tail.append(response.read()))
            reader.start()
        try:
            self.assertTrue(window.released.is_set())
            reader.join(timeout=3)
            self.assertFalse(reader.is_alive())
            self.assertEqual(tail, [self.payload[_CUT_BYTES:]])
            self.assertFalse(window.timed_out)
        finally:
            connection.close()

    def test_audio_request_keeps_real_output_extension_and_owned_workspace(self):
        root = Path(self.temporary.name)
        request = _request({'ffmpeg': 'ffmpeg.exe', 'yt_dlp': 'yt-dlp.exe'}, root,
                           'http://127.0.0.1:1234/mp3/audio.mp3', extension='mp3')
        from core.downloader import build_arguments
        args = build_arguments(request)
        self.assertEqual(request.output_extension, 'mp3')
        self.assertEqual(Path(request.output_path).suffix, '.mp3')
        self.assertEqual(Path(request.temporary_directory).parent,
                         root / 'outputs' / '.stream-downloads')
        self.assertEqual(args[args.index('--remux-video') + 1], 'mp3')

    def test_atomic_report_write_preserves_structured_failures(self):
        import json
        path = Path(self.temporary.name) / 'evidence' / 'results.json'
        _write_json(path, {'passed': False, 'error': '实际失败', 'requests': []})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['error'], '实际失败')
        self.assertFalse(path.with_name('results.json.writing').exists())

    def test_crash_waits_for_nonzero_worker_checkpoint_not_only_partial_bytes(self):
        root = Path(self.temporary.name) / 'workspace'
        root.mkdir()
        partial = root / 'download.mp4.part'
        partial.write_bytes(self.payload)
        _write_json(root / 'workspace.json', {
            'files': {'download.mp4.part': {'size': 0, 'blocks': []}}})
        self.assertEqual(_verified_partials(root), [])
        _write_json(root / 'workspace.json', {
            'files': {'download.mp4.part': {'size': 1024, 'blocks': ['worker-checkpoint']}}})
        evidence = _verified_partials(root)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['verified_bytes'], 1024)

    def test_light_checkpoint_observation_does_not_hash_an_active_partial(self):
        root = Path(self.temporary.name) / 'workspace'
        root.mkdir()
        partial = root / 'download.mp4.part'
        partial.write_bytes(self.payload)
        _write_json(root / 'workspace.json', {
            'files': {'download.mp4.part': {'size': 1024, 'blocks': ['worker-checkpoint']}}})
        with patch('tests.recovery_acceptance._sha') as hashed:
            observed = _verified_partials(root, include_hashes=False)
        hashed.assert_not_called()
        self.assertEqual(observed, [{'path': str(partial.resolve()),
                                    'size': len(self.payload), 'verified_bytes': 1024}])
        self.assertIn('sha256', _verified_partials(root)[0])

    def test_live_observer_releases_checkpoint_handle_before_parsing(self):
        import json
        path = Path(self.temporary.name) / 'workspace.json'
        _write_json(path, {'generation': 'old'})
        opened, replaced = threading.Event(), threading.Event()
        observed, errors = [], []
        original_loads = json.loads

        def hold_parser(content):
            opened.set()
            if not replaced.wait(3):
                raise TimeoutError('writer did not finish while reader held its handle')
            return original_loads(content)

        def reader():
            try:
                observed.append(_read_live_json(path))
            except Exception as error:
                errors.append(error)

        with patch('tests.recovery_acceptance.json.loads', side_effect=hold_parser):
            thread = threading.Thread(target=reader)
            thread.start()
            try:
                self.assertTrue(opened.wait(3))
                _write_json(path, {'generation': 'new'})
            finally:
                replaced.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(observed, [{'generation': 'old'}])
        self.assertEqual(_read_live_json(path), {'generation': 'new'})


if __name__ == '__main__':
    unittest.main()
