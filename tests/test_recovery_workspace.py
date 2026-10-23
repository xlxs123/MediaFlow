"""Recovery file ownership, verified prefixes, and cache integrity contracts."""
import io
import os
from email.message import Message
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.downloader import DownloadRequest
from core.errors import MediaCheckError
from core.http_client import HttpResponse
from core.recovery_workspace import BLOCK_SIZE, RecoveryWorkspace, atomic_json
from core.request_context import RequestContext
from core.segment_cache import SegmentCache


class RecoveryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.output = Path(self.folder.name)
        self.request = DownloadRequest('yt-dlp.exe', 'https://example.test/video.mp4', str(self.output),
            filename='sample', output_extension='mp4', output_path=str(self.output / 'sample.mp4'))
        self.identity = {'protocol': 'Direct', 'layout_sha256': 'fixture'}
        self.notices = []

    def tearDown(self):
        self.folder.cleanup()

    def workspace(self, protocol='Direct'):
        return RecoveryWorkspace(self.request, self.identity, protocol, self.notices.append)

    def test_direct_clean_pause_keeps_exact_offset(self):
        first = self.workspace()
        first.prepare()
        part = first.temporary / 'sample.mp4.part'
        part.write_bytes(b'a' * (BLOCK_SIZE + 77))
        first.checkpoint(final=True)
        self.workspace().prepare()
        self.assertEqual(part.stat().st_size, BLOCK_SIZE + 77)

    def test_crash_rolls_back_only_unverified_tail(self):
        first = self.workspace()
        first.prepare()
        part = first.temporary / 'sample.mp4.part'
        part.write_bytes(b'a' * (BLOCK_SIZE + 77))
        first.checkpoint()
        with part.open('ab') as stream:
            stream.write(b'new-uncheckpointed-tail')
        self.workspace().prepare()
        self.assertEqual(part.stat().st_size, BLOCK_SIZE)

    def test_corrupt_direct_prefix_is_preserved_and_never_appended(self):
        first = self.workspace()
        first.prepare()
        part = first.temporary / 'sample.mp4.part'
        part.write_bytes(b'a' * (BLOCK_SIZE + 77))
        first.checkpoint(final=True)
        with part.open('r+b') as stream:
            stream.write(b'changed')
        self.workspace().prepare()
        self.assertFalse(part.exists())
        self.assertEqual(len(list((first.root / 'retained').rglob('*.part'))), 1)

    def test_hls_cursor_and_part_are_rejected_together(self):
        first = self.workspace('HLS')
        first.prepare()
        part = first.temporary / 'sample.mp4.part'
        cursor = first.temporary / 'sample.mp4.ytdl'
        part.write_bytes(b'complete-prefix')
        cursor.write_text('{"fragment_index": 2}')
        first.checkpoint(final=True)
        part.write_bytes(b'corrupt-prefix')
        self.workspace('HLS').prepare()
        self.assertFalse(part.exists())
        self.assertFalse(cursor.exists())

    def test_complete_dash_track_survives_other_track_crash(self):
        first = self.workspace('DASH')
        first.prepare()
        video = first.temporary / 'sample.fvideo.mp4'
        audio = first.temporary / 'sample.faudio.m4a.part'
        video.write_bytes(b'complete-video-track')
        audio.write_bytes(b'incomplete-audio')
        first.checkpoint()
        self.workspace('DASH').prepare()
        self.assertTrue(video.exists())
        self.assertTrue(audio.exists())
        self.assertEqual(audio.stat().st_size, 0)

    def test_crashed_dash_http_track_keeps_verified_prefix(self):
        first = self.workspace('DASH')
        first.prepare()
        part = first.temporary / 'sample.faudio.m4a.part'
        part.write_bytes(b'a' * (BLOCK_SIZE + 77))
        first.checkpoint()
        self.workspace('DASH').prepare()
        self.assertEqual(part.stat().st_size, BLOCK_SIZE)

    def test_identity_change_never_reuses_workspace(self):
        self.workspace().prepare()
        with self.assertRaises(MediaCheckError) as caught:
            RecoveryWorkspace(self.request, {'protocol': 'Direct', 'layout_sha256': 'changed'}, 'Direct')
        self.assertEqual(caught.exception.code, 'RESUME_MISMATCH')

    def test_unowned_directory_is_rejected(self):
        self.request.temporary_directory = str(self.output / 'user-files')
        with self.assertRaises(MediaCheckError):
            self.workspace()

    def test_publish_does_not_overwrite_existing_file(self):
        workspace = self.workspace()
        workspace.prepare()
        source = workspace.home / 'sample.mp4'
        source.write_bytes(b'verified-stage')
        (self.output / 'sample.mp4').write_bytes(b'user-original')
        with self.assertRaises(MediaCheckError) as caught:
            workspace.publish([source], [{}])
        self.assertEqual(caught.exception.code, 'OUTPUT_EXISTS')
        self.assertEqual((self.output / 'sample.mp4').read_bytes(), b'user-original')
        self.assertTrue(source.exists())

    def test_publish_records_digest_and_cleans_only_owned_disposables(self):
        workspace = self.workspace()
        workspace.prepare()
        source = workspace.home / 'sample.mp4'
        source.write_bytes(b'verified-stage')
        user = self.output / 'unrelated.txt'
        user.write_bytes(b'keep')
        validations = [{}]
        files = workspace.publish([source], validations)
        self.assertEqual(Path(files[0]).read_bytes(), b'verified-stage')
        self.assertEqual(validations[0]['path'], files[0])
        self.assertEqual(user.read_bytes(), b'keep')
        self.assertEqual(workspace.committed_files(), files)

    def test_crash_between_rename_and_completed_record_is_reconciled(self):
        workspace = self.workspace()
        workspace.prepare()
        source = workspace.home / 'sample.mp4'
        source.write_bytes(b'verified-stage')
        def simulated_crash(path, value):
            if value.get('status') == 'completed':
                raise OSError('simulated process crash before completed record')
            atomic_json(path, value)
        with patch('core.recovery_workspace.atomic_json', side_effect=simulated_crash):
            with self.assertRaises(OSError):
                workspace.publish([source], [{}])
        restored = self.workspace()
        self.assertEqual(restored.state['status'], 'publishing')
        self.assertEqual(restored.committed_files(), [str(self.output / 'sample.mp4')])
        self.assertEqual(restored.state['status'], 'completed')

    def test_checkpoint_tolerates_internal_fragment_rename(self):
        workspace = self.workspace('HLS')
        workspace.prepare()
        fragment = workspace.temporary / 'sample.part-Frag1'
        fragment.write_bytes(b'transient')
        with patch('core.recovery_workspace.file_hash', side_effect=FileNotFoundError):
            workspace.checkpoint()
        self.assertNotIn(str(fragment.relative_to(workspace.root)), workspace.state['files'])

    def test_workspace_lock_prevents_two_writers_and_releases_on_close(self):
        first = RecoveryWorkspace(self.request, self.identity, 'Direct', lock_workspace=True)
        first.prepare()
        try:
            with self.assertRaises(MediaCheckError) as caught:
                RecoveryWorkspace(self.request, self.identity, 'Direct', lock_workspace=True)
            self.assertEqual(caught.exception.code, 'RECOVERY_BUSY')
        finally:
            first.close()
        second = RecoveryWorkspace(self.request, self.identity, 'Direct', lock_workspace=True)
        second.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows transient sharing violation')
    def test_atomic_record_retries_transient_windows_reader(self):
        path = self.output / 'record.json'
        original_replace = os.replace
        calls = []
        def replace_once_locked(source, target):
            calls.append(1)
            if len(calls) == 1:
                error = PermissionError('temporary sharing violation')
                error.winerror = 5
                raise error
            original_replace(source, target)
        with patch('core.recovery_workspace.os.replace', side_effect=replace_once_locked):
            atomic_json(path, {'verified': True})
        self.assertEqual(len(calls), 2)
        self.assertIn('true', path.read_text())


class _Stream(io.BytesIO):
    def __init__(self, payload, length=None):
        super().__init__(payload)
        self.status = 200
        self.headers = Message()
        self.headers['Content-Type'] = 'application/octet-stream'
        self.headers['Content-Length'] = str(len(payload) if length is None else length)


class _Client:
    def __init__(self, payload, length=None):
        self.cancel_event = threading.Event()
        self.payload, self.length = payload, length
        self.calls = 0

    def open(self, context, overrides):
        self.calls += 1
        return HttpResponse(context, _Stream(self.payload, self.length), [])


class SegmentCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.cache = SegmentCache(self.folder.name, 'inspected-layout')
        self.context = RequestContext('https://cdn.test/segment.ts?signature=PRIVATE',
                                      headers={'Cookie': 'session=PRIVATE'})
        self.client = _Client(b'verified-transport-bytes')

    def tearDown(self):
        self.folder.cleanup()

    def read(self):
        with self.cache.open(self.client, self.context, {}, 'segment') as response:
            return response.read()

    def test_verified_cache_prevents_repeat_remote_fetch(self):
        self.assertEqual(self.read(), self.client.payload)
        self.assertEqual(self.read(), self.client.payload)
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(self.cache.hits, 1)

    def test_corrupt_fragment_is_refetched_and_other_cache_is_retained(self):
        self.read()
        blob = next(Path(self.folder.name).glob('*.blob'))
        blob.write_bytes(b'x' * len(self.client.payload))
        self.assertEqual(self.read(), self.client.payload)
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(len(list(Path(self.folder.name).glob('*.corrupt-*'))), 1)

    def test_cache_files_never_contain_request_credentials(self):
        self.read()
        for path in Path(self.folder.name).glob('*.json'):
            self.assertNotIn('PRIVATE', path.read_text())
            self.assertNotIn('cdn.test', path.read_text())

    def test_truncated_body_is_never_cached_as_complete(self):
        self.client.length = 999
        with self.assertRaises(MediaCheckError):
            self.read()
        self.assertEqual(list(Path(self.folder.name).glob('*.blob')), [])

    def test_fragment_range_ignored_200_is_never_cached(self):
        with self.assertRaises(MediaCheckError):
            self.cache.open(self.client, self.context, {'range': 'bytes=8-15'}, 'prefix')
        self.assertEqual(list(Path(self.folder.name).glob('*.blob')), [])


if __name__ == '__main__':
    unittest.main()
