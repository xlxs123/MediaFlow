"""Use real SQLite transactions to verify hash cache batch persistence."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest

from core.process_tree import NO_WINDOW, ProcessTree
from core.storage_cache import HashCache, StorageCacheError


IDENTITY = (1, 2, 3, 4, 5)
CHANGED_IDENTITY = (1, 2, 3, 4, 6)
OLD_DIGEST = 'a' * 64
NEW_DIGEST = 'b' * 64


class HashCacheTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='mediaflow-hash-cache-test-')
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'hashes.sqlite3'
        self.cache = self.open_cache()

    def open_cache(self):
        cache = HashCache(self.path)
        self.addCleanup(cache.close)
        return cache

    def test_batch_commits_once_and_last_duplicate_key_wins(self):
        observer = self.open_cache()
        self.cache.put('media', IDENTITY, OLD_DIGEST)
        self.assertEqual(observer.get('media', IDENTITY), OLD_DIGEST)
        statements = []
        self.cache.connection.set_trace_callback(statements.append)
        self.cache.put_many([
            ('media', IDENTITY, OLD_DIGEST),
            ('other', IDENTITY, NEW_DIGEST),
            ('media', CHANGED_IDENTITY, NEW_DIGEST),
        ])
        boundaries = [sql for sql in statements if sql.startswith(('BEGIN', 'COMMIT', 'ROLLBACK'))]
        self.assertEqual(boundaries, ['BEGIN IMMEDIATE', 'COMMIT'])
        self.cache.close()
        self.assertEqual(observer.get('media', IDENTITY), '')
        self.assertEqual(observer.get('media', CHANGED_IDENTITY), NEW_DIGEST)
        self.assertEqual(observer.get('other', IDENTITY), NEW_DIGEST)

    def test_empty_batch_and_parameter_error_start_no_transaction(self):
        statements = []
        self.cache.connection.set_trace_callback(statements.append)
        self.cache.put_many([])
        with self.assertRaises(TypeError):
            self.cache.put_many([
                ('first', IDENTITY, NEW_DIGEST),
                ('second', (object(),), NEW_DIGEST),
            ])
        self.assertEqual(statements, [])
        self.assertFalse(self.cache.connection.in_transaction)

    def test_insert_failure_rolls_back_prior_rows_of_batch(self):
        self.cache.put('first', IDENTITY, OLD_DIGEST)
        self.cache.connection.execute("""
            CREATE TEMP TRIGGER fail_second BEFORE INSERT ON hashes
            WHEN NEW.path = 'second'
            BEGIN SELECT RAISE(ABORT, 'test write failure'); END
        """)
        with self.assertRaises(StorageCacheError):
            self.cache.put_many([
                ('first', CHANGED_IDENTITY, NEW_DIGEST),
                ('second', IDENTITY, NEW_DIGEST),
            ])
        self.assertFalse(self.cache.connection.in_transaction)
        observer = self.open_cache()
        self.assertEqual(observer.get('first', IDENTITY), OLD_DIGEST)
        self.assertEqual(observer.get('first', CHANGED_IDENTITY), '')
        self.assertEqual(observer.get('second', IDENTITY), '')

    def test_commit_blocked_by_reader_rolls_back_and_releases_lock(self):
        self.cache.put('first', IDENTITY, OLD_DIGEST)
        connection = self.cache.connection
        self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 3000)
        connection.execute('PRAGMA journal_mode=DELETE').fetchone()
        connection.execute('PRAGMA busy_timeout=0')
        statements = []
        connection.set_trace_callback(statements.append)
        with closing(sqlite3.connect(self.path)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM hashes').fetchall()
            with self.assertRaises(StorageCacheError):
                self.cache.put_many([
                    ('first', CHANGED_IDENTITY, NEW_DIGEST),
                    ('second', IDENTITY, NEW_DIGEST),
                ])
            self.assertIn('COMMIT', statements)
            self.assertIn('ROLLBACK', statements)
            self.assertFalse(connection.in_transaction)
            reader.rollback()
            reader.execute('BEGIN IMMEDIATE')
            reader.rollback()
        self.assertEqual(self.cache.get('first', IDENTITY), OLD_DIGEST)
        self.assertEqual(self.cache.get('second', IDENTITY), '')

    def test_other_connection_sees_only_committed_batch(self):
        self.cache.put('first', IDENTITY, OLD_DIGEST)
        observer = self.open_cache()
        observer.connection.execute('PRAGMA busy_timeout=0')
        observation = {}

        def observe_uncommitted_batch():
            observation['first'] = observer.get('first', IDENTITY)
            try:
                observer.connection.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as error:
                observation['writer_error'] = error.sqlite_errorcode
            else:
                observer.connection.rollback()

        self.cache.connection.create_function('observe_batch', 0, observe_uncommitted_batch)
        self.cache.connection.execute("""
            CREATE TEMP TRIGGER inspect_second BEFORE INSERT ON hashes
            WHEN NEW.path = 'second'
            BEGIN SELECT observe_batch(); END
        """)
        self.cache.put_many([
            ('first', IDENTITY, NEW_DIGEST),
            ('second', IDENTITY, NEW_DIGEST),
        ])
        self.assertEqual(observation, {'first': OLD_DIGEST, 'writer_error': sqlite3.SQLITE_BUSY})
        self.assertEqual(observer.get('first', IDENTITY), NEW_DIGEST)
        self.assertEqual(observer.get('second', IDENTITY), NEW_DIGEST)

    def test_abrupt_process_exit_leaves_previous_batch_intact(self):
        self.cache.put('first', IDENTITY, OLD_DIGEST)
        self.cache.close()
        script = dedent("""
            import os
            import sys
            from core.storage_cache import HashCache

            cache = HashCache(sys.argv[1])
            cache.connection.create_function('exit_mid_batch', 0, lambda: os._exit(23))
            cache.connection.execute('''
                CREATE TEMP TRIGGER exit_before_second BEFORE INSERT ON hashes
                WHEN NEW.path = 'second'
                BEGIN SELECT exit_mid_batch(); END
            ''')
            cache.put_many([
                ('first', (1, 2, 3, 4, 6), 'b' * 64),
                ('second', (1, 2, 3, 4, 5), 'b' * 64),
            ])
        """)
        with subprocess.Popen(
            [sys.executable, '-B', '-c', script, str(self.path)],
            cwd=Path(__file__).resolve().parents[1], stderr=subprocess.PIPE,
            creationflags=NO_WINDOW, start_new_session=os.name != 'nt',
        ) as process:
            tree = ProcessTree(process)
            try:
                _, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                tree.terminate()
                raise
            finally:
                tree.close()
            self.assertEqual(process.returncode, 23, stderr.decode(errors='replace'))
        recovered = self.open_cache()
        self.assertEqual(recovered.get('first', IDENTITY), OLD_DIGEST)
        self.assertEqual(recovered.get('first', CHANGED_IDENTITY), '')
        self.assertEqual(recovered.get('second', IDENTITY), '')
        recovered.put_many([('later', IDENTITY, NEW_DIGEST)])
        self.assertEqual(recovered.get('later', IDENTITY), NEW_DIGEST)
