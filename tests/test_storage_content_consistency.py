"""Duplicate groups require current, stable content even when metadata matches."""
from contextlib import ExitStack, closing, contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import media_storage_scan
from core.media_history import HistoryStore
from core.storage_cache import HashCache
from core.storage_cleanup import StorageCleanup, StorageCleanupError
from core.storage_safety import ProtectionPolicy, path_key


ORIGINAL = b'real duplicated content'
EDITED = b'real duplicated contenX'


class StorageContentConsistencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='mediaflow-content-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.identity_for = media_storage_scan.identity_for

    def files(self, count, content=ORIGINAL, directory=None):
        root = directory or self.root
        root.mkdir(parents=True, exist_ok=True)
        paths = [root / f'file-{index}.mp4' for index in range(count)]
        for path in paths:
            path.write_bytes(content)
        return paths

    def scan(self, paths, **options):
        cache = paths[0].parent / 'hashes.sqlite3'
        return media_storage_scan.StorageScanner(cache).scan(
            [{'id': index, 'path': str(path)} for index, path in enumerate(paths)], **options)

    def cached_digest(self, path, identity):
        with closing(HashCache(path.parent / 'hashes.sqlite3')) as cache:
            return cache.get(path_key(path), identity)

    @contextmanager
    def unchanged_change_time(self, path):
        before = self.identity_for(path)
        stream_identity = getattr(media_storage_scan, 'identity_from_stream', None)

        def read_identity(candidate, info=None):
            result = self.identity_for(candidate, info)
            if Path(candidate) == path:
                return [*result[:4], before[4]]
            return result

        def read_stream_identity(stream, info):
            result = stream_identity(stream, info)
            if result[:2] == before[:2]:
                return [*result[:4], before[4]]
            return result

        with ExitStack() as stack:
            stack.enter_context(patch.object(media_storage_scan, 'identity_for', read_identity))
            stack.enter_context(patch('core.storage_cleanup.identity_for', read_identity))
            if stream_identity is not None:
                stack.enter_context(patch.object(media_storage_scan, 'identity_from_stream', read_stream_identity))
            yield before

    def rewrite(self, path, expected, content, offset=0):
        times = path.stat()
        with path.open('r+b') as stream:
            stream.seek(offset)
            self.assertEqual(stream.write(content), len(content))
        os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))
        self.assertEqual(media_storage_scan.identity_for(path), expected)

    def assert_group(self, result, paths, content=ORIGINAL):
        digest = hashlib.sha256(content).hexdigest()
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['groups']), 1)
        group = result['groups'][0]
        self.assertEqual(group['sha256'], digest)
        self.assertEqual(len(group['files']), len(paths))
        self.assertEqual({row['path'] for row in group['files']}, {str(path) for path in paths})
        self.assertTrue(all(row['status'] == 'exists' and row['sha256'] == digest
                            for row in group['files']))

    def assert_unconfirmed(self, result, path):
        row = next(row for row in result['files'] if row['path'] == str(path))
        self.assertNotEqual(row['status'], 'exists')
        self.assertFalse(any(member['path'] == str(path) for group in result['groups']
                             for member in group['files']))

    def test_hot_cache_seven_becomes_six_with_same_observed_identity(self):
        paths = self.files(7)
        with self.unchanged_change_time(paths[0]) as before:
            self.assert_group(self.scan(paths), paths)
            old_digest = hashlib.sha256(ORIGINAL).hexdigest()
            self.assertEqual(self.cached_digest(paths[0], before), old_digest)
            self.rewrite(paths[0], before, EDITED)
            self.assertEqual(paths[0].read_bytes(), EDITED)
            self.assertEqual(self.cached_digest(paths[0], before), old_digest)
            result = self.scan(paths)

        self.assert_group(result, paths[1:])
        edited_row = next(row for row in result['files'] if row['path'] == str(paths[0]))
        new_digest = hashlib.sha256(EDITED).hexdigest()
        self.assertEqual(edited_row['sha256'], new_digest)
        self.assertEqual(self.cached_digest(paths[0], before), new_digest)

    def test_cached_distinct_digests_do_not_hide_a_new_duplicate(self):
        a, b = self.files(2)
        b.write_bytes(EDITED)
        with self.unchanged_change_time(b) as before:
            self.assertEqual(self.scan([a, b])['groups'], [])
            self.rewrite(b, before, ORIGINAL)
            self.assertEqual(b.read_bytes(), ORIGINAL)
            self.assert_group(self.scan([a, b]), [a, b])

    def test_real_edit_after_first_hash_chunk_is_not_confirmed(self):
        content = b'a' * 3_000_000
        a, b = self.files(2, content)
        changed = False

        with self.unchanged_change_time(a) as before:
            def progress(event):
                nonlocal changed
                if (not changed and event['phase'] == 'hash' and event['path'] == str(a)
                        and event['files_done'] == 0 and event['bytes_done'] == 1_048_576):
                    self.rewrite(a, before, b'X', offset=2_000_000)
                    changed = True

            result = self.scan([a, b], progress=progress)

        self.assertTrue(changed)
        with a.open('rb') as stream:
            stream.seek(2_000_000)
            self.assertEqual(stream.read(1), b'X')
        self.assertEqual(result['groups'], [])
        self.assert_unconfirmed(result, a)

    def test_edit_after_earlier_candidate_read_excludes_it_from_this_scan(self):
        paths = self.files(7)
        victim = paths[0]
        changed = False

        with self.unchanged_change_time(victim) as before:
            def progress(event):
                nonlocal changed
                if event['phase'] == 'hash' and event['files_done'] == 3 and not changed:
                    self.rewrite(victim, before, EDITED)
                    changed = True

            first = self.scan(paths, progress=progress)
            self.assertTrue(changed)
            self.assertEqual(victim.read_bytes(), EDITED)
            self.assert_group(first, paths[1:])
            self.assert_unconfirmed(first, victim)
            self.assert_group(self.scan(paths), paths[1:])

    def test_delete_replace_and_rename_after_earlier_candidate_read(self):
        for operation in ('delete', 'replace', 'rename'):
            with self.subTest(operation=operation):
                root = self.root / operation
                paths = self.files(4, directory=root)
                victim = paths[0]
                replacement = root / 'replacement.mp4'
                replacement.write_bytes(EDITED)
                moved = root / 'moved.mp4'
                applied = False

                def progress(event):
                    nonlocal applied
                    if event['phase'] != 'hash' or event['files_done'] != 3 or applied:
                        return
                    if operation == 'delete':
                        victim.unlink()
                    elif operation == 'replace':
                        replacement.replace(victim)
                    else:
                        victim.rename(moved)
                    applied = True

                result = self.scan(paths, progress=progress)
                self.assertTrue(applied)
                self.assert_group(result, paths[1:])
                self.assert_unconfirmed(result, victim)

    def test_cache_hit_cannot_mask_a_content_read_error(self):
        paths = self.files(3)
        victim = paths[0]
        self.assert_group(self.scan(paths), paths)
        original_open = Path.open
        original_locked = getattr(media_storage_scan, 'open_read_locked', None)
        enumerated = False
        failures = []

        @contextmanager
        def failing_stream(opener, path, *args, **kwargs):
            with opener(path, *args, **kwargs) as stream:
                def fail(*_args, **_kwargs):
                    failures.append(path)
                    raise OSError('injected fixture read failure')

                proxy = Mock(wraps=stream)
                proxy.read.side_effect = fail
                yield proxy

        def open_file(path, mode='r', *args, **kwargs):
            if enumerated and path == victim and mode == 'rb':
                return failing_stream(original_open, path, mode, *args, **kwargs)
            return original_open(path, mode, *args, **kwargs)

        def open_locked(path, *args, **kwargs):
            if enumerated and path == victim:
                return failing_stream(original_locked, path, *args, **kwargs)
            return original_locked(path, *args, **kwargs)

        def progress(event):
            nonlocal enumerated
            if event['phase'] == 'enumerate' and event['files_done'] == len(paths):
                enumerated = True

        # The immutable baseline used Path.open directly. The fixed scanner
        # gets a stream from the guarded reader, so inject the same read fault there.
        opener_patch = (patch.object(Path, 'open', open_file) if original_locked is None
                        else patch.object(media_storage_scan, 'open_read_locked', open_locked))
        with opener_patch:
            result = self.scan(paths, progress=progress)

        self.assert_group(result, paths[1:])
        self.assert_unconfirmed(result, victim)
        self.assertIn(victim, failures)

    @unittest.skipUnless(os.name == 'nt', 'Windows 文件共享模式')
    def test_confirmed_member_stays_locked_until_its_group_is_assembled(self):
        import ctypes
        from ctypes import wintypes

        a, b = self.files(2)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        open_file = kernel.CreateFileW
        open_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        open_file.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        attempted = []

        def progress(event):
            if event['phase'] != 'confirm' or event['path'] != str(b) or attempted:
                return
            # GENERIC_WRITE, share all, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL.
            handle = open_file(str(a), 0x40000000, 7, None, 3, 0x80, None)
            if handle != ctypes.c_void_p(-1).value:
                kernel.CloseHandle(handle)
                self.fail('first group member accepted a writer during confirmation')
            attempted.append(ctypes.get_last_error())

        self.assert_group(self.scan([a, b], progress=progress), [a, b])
        self.assertEqual(attempted, [32])  # ERROR_SHARING_VIOLATION
        self.assertEqual(a.read_bytes(), ORIGINAL)
        self.assertEqual(b.read_bytes(), ORIGINAL)
        with a.open('r+b') as stream:
            self.assertEqual(stream.write(EDITED), len(EDITED))
        self.assertEqual(a.read_bytes(), EDITED)
        self.assertEqual(b.read_bytes(), ORIGINAL)

    def test_cleanup_rechecks_stale_scan_and_existing_preview(self):
        a, b = self.files(2)
        store = HistoryStore(self.root / 'history.sqlite3')
        self.addCleanup(store.close)
        for index, path in enumerate((a, b), 1):
            store.record({'task_key': f'fixture-{index}', 'queue_id': index,
                          'task_state': 'completed',
                          'files': [{'path': str(path), 'file_state': 'exists'}]})
        recycler = Mock(side_effect=AssertionError('fixture must not be recycled'))
        cleanup = StorageCleanup(self.root / 'cleanup', recycler)
        policy = ProtectionPolicy({'jobs': []})

        with self.unchanged_change_time(b) as before:
            old_scan = media_storage_scan.StorageScanner().scan(store.storage_entries())
            self.assert_group(old_scan, [a, b])
            old_preview = cleanup.preview(store, old_scan, [str(b)], policy)
            self.assertTrue(old_preview['token'])
            self.rewrite(b, before, EDITED)
            new_preview = cleanup.preview(store, old_scan, [str(b)], policy)
            self.assertFalse(new_preview['token'])
            self.assertTrue(new_preview['errors'])
            with self.assertRaises(StorageCleanupError):
                cleanup.apply(store, old_preview['token'], confirmed=True, protection=policy)

        recycler.assert_not_called()
        self.assertEqual(a.read_bytes(), ORIGINAL)
        self.assertEqual(b.read_bytes(), EDITED)
