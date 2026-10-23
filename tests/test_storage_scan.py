"""真实文件内容与磁盘身份驱动的去重契约。"""
from pathlib import Path
import os
import tempfile
import threading
import unittest

from core.media_storage_scan import StorageScanner


class StorageScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self, paths=(), **kwargs):
        return StorageScanner(self.root / 'hashes.sqlite3').scan([
            {'id': i + 1, 'path': str(path), 'media_type': 'unknown', 'source_site': 'example.test',
             'finished_at': '2026-10-21T10:00:00Z'} for i, path in enumerate(paths)], **kwargs)

    def test_full_content_only_different_names_extensions_and_titles(self):
        paths = [self.file('first.mp4', b'complete-content'), self.file('other-name.webm', b'complete-content'),
                 self.file('third-title.bin', b'complete-content')]
        result = self.scan(paths)
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(result['stats']['reclaimable_bytes'], 2 * len(b'complete-content'))
        self.assertEqual(result['hashed_files'], 3)
        self.assertEqual(result['stats']['total_files'], 3)

    def test_same_name_size_duration_never_confirms_different_bytes(self):
        a = self.file('one/same.mp4', b'0123456789')
        b = self.file('two/same.mp4', b'012345678X')
        result = self.scan([a, b])
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['hashed_files'], 2)
        self.assertNotEqual(result['files'][0]['sha256'], result['files'][1]['sha256'])

    def test_multiple_history_references_same_path_do_not_duplicate_size(self):
        path = self.file('one.mp4', b'abc')
        result = self.scan([path, path])
        self.assertEqual(result['stats']['total_files'], 1)
        self.assertEqual(result['stats']['total_bytes'], 3)
        self.assertEqual(result['files'][0]['entry_ids'], [1, 2])
        self.assertFalse(result['groups'])

    def test_hard_link_alias_not_reclaimable(self):
        a = self.file('first.mp4', b'hard-link')
        b = self.root / 'link.mp4'
        os.link(a, b)
        result = self.scan([a, b])
        self.assertEqual(result['stats']['total_files'], 1)
        self.assertEqual(result['stats']['total_bytes'], len(b'hard-link'))
        self.assertEqual(result['groups'], [])
        self.assertIn(str(b), result['files'][0]['aliases'])

    def test_missing_and_deleted_keep_history_without_stale_capacity(self):
        missing = self.root / 'deleted.mp4'
        a = self.file('one.mp4', b'real')
        result = self.scan([a, missing])
        self.assertEqual(result['stats']['total_bytes'], 4)
        self.assertEqual(result['stats']['missing_files'], 1)
        self.assertEqual(len(result['files']), 2)

    def test_cache_validity_includes_ctime_not_only_size_and_mtime(self):
        a = self.file('a.mp4', b'abcd')
        b = self.file('b.mp4', b'abcd')
        result = self.scan([a, b])
        self.assertEqual(len(result['groups']), 1)
        self.assertEqual(self.scan([a, b])['cache_hits'], 2)
        before = b.stat()
        b.write_bytes(b'abce')
        os.utime(b, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.scan([a, b])
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['cache_hits'], 1)
        self.assertEqual(result['hashed_files'], 1)

    def test_hash_phase_change_is_excluded(self):
        a = self.file('a.mp4', b'a' * 3_000_000)
        b = self.file('b.mp4', b'a' * 3_000_000)
        changed = []
        def progress(value):
            if value['phase'] == 'hash' and not changed:
                changed.append(True)
                with a.open('r+b') as stream:
                    stream.seek(2_000_000)
                    stream.write(b'X')
        result = self.scan([a, b], progress=progress)
        self.assertFalse(result['groups'])
        self.assertTrue(any(row['status'] == 'changed' for row in result['files']))

    def test_cancel_mid_hash_never_returns_cleanup_eligible_groups(self):
        a = self.file('a.mp4', b'a' * 3_000_000)
        b = self.file('b.mp4', b'a' * 3_000_000)
        cancel = threading.Event()
        events = []
        def progress(value):
            events.append(value)
            if value['phase'] == 'hash':
                cancel.set()
        result = self.scan([a, b], progress=progress, cancel_event=cancel)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['complete'])
        self.assertFalse(result['groups'])
        self.assertGreater(result['hash_bytes'], 0)
        self.assertLess(result['hash_bytes'], result['hash_total'])

    def test_scopes_deduplicate_dirs_and_skip_workspaces_and_credentials(self):
        directory = self.root / 'media'
        a = self.file('media/a.mp4', b'data')
        self.file('media/.stream-downloads/unfinished.mp4', b'not-final')
        self.file('media/BrowserProfile/Cookies', b'sensitive-fixture')
        result = self.scan([a], extra_dirs=[directory, directory])
        self.assertEqual(result['stats']['total_bytes'], 4)
        self.assertEqual(result['stats']['total_files'], 1)
        self.assertEqual(len(result['issues']), 2)

    def test_sidecar_is_known_media_thumbnail_is_not_default_scope(self):
        media = self.file('a.mp4', b'media')
        side = self.file('a.en.srt', b'1\n00:00:00,000 --> 00:00:01,000\nHello\n')
        cover = self.file('a.jpg', b'cover')
        result = StorageScanner().scan([{'id': 1, 'path': str(media), 'thumbnail_path': str(cover),
            'validation': {'subtitle_files': [{'path': str(side)}]}}])
        self.assertEqual(result['stats']['total_files'], 2)
        self.assertEqual(result['stats']['by_type']['subtitle']['count'], 1)

    def test_guard_excludes_active_files_from_duplicate_groups(self):
        a, b = self.file('a.mp4', b'abcd'), self.file('b.mp4', b'abcd')
        result = self.scan([a, b], guard=lambda p: '正在下载' if p == str(b) else '')
        self.assertFalse(result['groups'])
        self.assertEqual(result['stats']['total_bytes'], 8)

    @unittest.skipUnless(os.name == 'nt', 'Windows 文件访问共享保护')
    def test_real_unreadable_file_handle_is_reported_not_counted(self):
        import ctypes
        from ctypes import wintypes
        path = self.file('locked.mp4', b'not-accessible')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                      wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            result = self.scan([path])
            self.assertEqual(result['stats']['total_bytes'], 0)
            self.assertEqual(result['stats']['inaccessible_files'], 1)
        finally:
            kernel.CloseHandle(handle)
