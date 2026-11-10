"""Real SQLite cache behavior across complete, cancelled and failed scans."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from core.media_storage_scan import StorageScanner
from core.storage_cache import HashCache
from core.storage_safety import path_key


class StorageScanBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.cache_path = root / 'hashes.sqlite3'
        self.paths = [root / f'sample-{index}.mp4' for index in range(7)]
        for path in self.paths:
            path.write_bytes(b'real duplicated content')
        self.entries = [{'id': index, 'path': str(path)} for index, path in enumerate(self.paths)]

    def keys(self):
        with closing(sqlite3.connect(self.cache_path)) as connection:
            return {key for key, in connection.execute('SELECT path FROM hashes')}

    def scan(self, **kwargs):
        return StorageScanner(self.cache_path).scan(self.entries, **kwargs)

    @patch('core.media_storage_scan._CACHE_BATCH_SIZE', 4)
    def test_full_batch_is_visible_during_scan_and_tail_is_committed(self):
        counts = {}

        def progress(value):
            if value['phase'] == 'hash' and value['files_done'] in (3, 4, 7):
                if value['files_done'] not in counts:
                    counts[value['files_done']] = len(self.keys())

        cold = self.scan(progress=progress)
        self.assertEqual(counts, {3: 0, 4: 4, 7: 4})
        self.assertEqual(len(self.keys()), 7)
        warm = self.scan()
        self.assertEqual((cold['hashed_files'], warm['cache_hits'], warm['hashed_files']), (7, 7, 0))
        self.assertEqual(cold['groups'], warm['groups'])

    @patch('core.media_storage_scan._CACHE_BATCH_SIZE', 2)
    def test_cancel_discards_pending_batch_but_keeps_committed_rows(self):
        cancel = threading.Event()

        def progress(value):
            if value['phase'] == 'hash' and value['files_done'] == 3:
                cancel.set()

        result = self.scan(progress=progress, cancel_event=cancel)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['groups'], [])
        self.assertEqual(self.keys(), {path_key(path) for path in self.paths[:2]})
        resumed = self.scan()
        self.assertEqual((resumed['cache_hits'], resumed['hashed_files']), (2, 5))
        self.assertEqual(len(resumed['groups'][0]['files']), 7)

    @patch('core.media_storage_scan._CACHE_BATCH_SIZE', 2)
    def test_write_error_rolls_back_entire_batch_without_losing_scan_results(self):
        HashCache(self.cache_path).close()
        with closing(sqlite3.connect(self.cache_path)) as connection, connection:
            connection.execute("""CREATE TRIGGER reject_file BEFORE INSERT ON hashes
                WHEN NEW.path LIKE '%sample-3.mp4'
                BEGIN SELECT RAISE(ABORT, 'test write error'); END""")
        result = self.scan()
        self.assertTrue(result['complete'])
        self.assertEqual(result['hashed_files'], 7)
        self.assertEqual(len(result['groups'][0]['files']), 7)
        self.assertEqual(len(result['issues']), 1)
        self.assertIn('哈希缓存保存失败', result['issues'][0]['reason'])
        self.assertEqual(self.keys(), {path_key(path) for path in self.paths[:2]})

    @patch('core.media_storage_scan._CACHE_BATCH_SIZE', 2)
    def test_scan_exception_closes_cache_and_drops_uncommitted_rows(self):
        scanner = StorageScanner(self.cache_path)

        def progress(value):
            if value['phase'] == 'hash' and value['files_done'] == 3:
                raise RuntimeError('synthetic interruption')

        with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
            scanner.scan(self.entries, progress=progress)
        self.assertEqual(self.keys(), {path_key(path) for path in self.paths[:2]})
        self.assertEqual(scanner.scan(self.entries)['cache_hits'], 2)

    @patch('core.media_storage_scan._CACHE_BATCH_SIZE', 4)
    def test_file_changed_while_buffered_is_not_a_cache_hit(self):
        path = self.paths[0]
        before = path.stat()
        changed = False

        def progress(value):
            nonlocal changed
            if value['phase'] == 'hash' and value['files_done'] == 3 and not changed:
                path.write_bytes(b'real duplicated contenX')
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                changed = True

        first = self.scan(progress=progress)
        self.assertEqual(first['files'][0]['status'], 'changed')
        self.assertEqual(len(first['groups'][0]['files']), 6)
        second = self.scan()
        self.assertEqual((second['cache_hits'], second['hashed_files']), (6, 1))
        self.assertEqual(len(second['groups'][0]['files']), 6)
        self.assertEqual(self.scan()['cache_hits'], 7)

    @patch('core.media_storage_scan._CACHE_BATCH_SIZE', 2)
    def test_concurrent_scans_use_separate_connections_on_the_same_cache(self):
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait(timeout=10)
            return self.scan()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        for result in results:
            self.assertTrue(result['complete'])
            self.assertEqual(result['issues'], [])
            self.assertEqual(len(result['groups'][0]['files']), 7)
            self.assertEqual(result['cache_hits'] + result['hashed_files'], 7)
        self.assertEqual(self.scan()['cache_hits'], 7)
