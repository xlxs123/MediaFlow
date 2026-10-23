"""真实当前用户 DPAPI、事务恢复以及与现有任务快照的隔离。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from core.batch_store import BatchStore, BatchStoreError
from core.task_store import TaskStore


class BatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'batches.sqlite'
        self.store = BatchStore(self.path)
        self.batch_id = str(uuid.uuid4())
        self.batch = {'id': self.batch_id, 'title': '批次测试',
                      'created_at': '2026-10-20T08:00:00+00:00', 'updated_at': '2026-10-20T08:01:00+00:00',
                      'paused': False, 'options': {'output_directory': str(self.root / 'output')}}
        self.store.save_batch(self.batch)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def item(self, **changes):
        record = {'id': str(uuid.uuid4()), 'batch_id': self.batch_id, 'ordinal': 1,
                  'url': 'https://www.douyin.com/user/self?modal_id=7526879737634950451&token=private-source-token',
                  'title': '目标作品', 'source_site': 'www.douyin.com', 'state': 'waiting_discovery',
                  'candidate_count': 0, 'progress': {}, 'queue_key': 'a' * 64, 'job_id': 2,
                  'error': '', 'output_path': str(self.root / 'video.mp4'),
                  'created_at': self.batch['created_at'], 'updated_at': self.batch['updated_at'],
                  'attempts': 0, 'context_error': ''}
        record.update(changes)
        return record

    def reopen(self):
        self.store.close()
        self.store = BatchStore(self.path)

    def test_real_dpapi_roundtrip_restart_and_incremental_upsert(self):
        first, second = self.item(), self.item(ordinal=2)
        self.store.save_items([first, second])
        first.update(state='completed', progress={'percent': 100, 'downloaded_bytes': 12800})
        self.store.save_item(first)
        self.batch.update(paused=True)
        self.store.save_batch(self.batch)
        self.reopen()
        data = self.store.load()
        self.assertEqual(len(data['batches']), 1)
        self.assertTrue(data['batches'][0]['paused'])
        self.assertEqual(len(data['items']), 2)
        self.assertEqual(data['items'][0]['state'], 'completed')
        self.assertEqual(data['items'][1]['state'], 'waiting_discovery')
        self.assertEqual(data['items'][0]['url'], first['url'])
        self.assertEqual(data['items'][0]['url_hash'], hashlib.sha256(first['url'].encode()).hexdigest())

    def test_source_url_is_not_plaintext_in_database_or_wal(self):
        record = self.item()
        record.update(title='secret-source private-source-token ' + record['url'],
                      error='token=private-error-token Bearer private-bearer-token ' + record['url'])
        self.store.save_item(record)
        raw = b''.join(path.read_bytes() for path in self.root.iterdir() if path.is_file())
        for secret in (record['url'], 'private-source-token', 'private-error-token', 'private-bearer-token'):
            self.assertNotIn(secret.encode(), raw)
        data = self.store.load()['items'][0]
        self.assertEqual(data['url'], record['url'])
        self.assertNotIn('https://', data['title'])
        self.assertNotIn('private-source-token', data['title'])

    def test_unprotect_failure_keeps_completed_state_and_cipher_on_next_save(self):
        record = self.item(state='completed')
        self.store.save_item(record)
        before = self.store._connection.execute('SELECT url_cipher FROM items').fetchone()[0]
        with patch.object(TaskStore, '_unprotect', side_effect=RuntimeError('private details')):
            restored = self.store.load()['items'][0]
        self.assertEqual(restored['url'], '')
        self.assertEqual(restored['state'], 'completed')
        self.assertIn('需重新提供来源', restored['context_error'])
        self.assertNotIn('private details', restored['context_error'])
        restored['progress'] = {'percent': 100}
        self.store.save_item(restored)
        after = self.store._connection.execute('SELECT url_cipher FROM items').fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(self.store.load()['items'][0]['url'], record['url'])

    def test_encrypt_failure_never_falls_back_to_plaintext_or_overwrites_existing(self):
        record = self.item()
        self.store.save_item(record)
        old = self.store._connection.execute('SELECT url_cipher,metadata FROM items').fetchone()
        record.update(url='https://example.test/changed?token=new-secret', title='changed')
        with patch.object(TaskStore, '_protect', side_effect=RuntimeError('secret exception')):
            with self.assertRaises(BatchStoreError) as caught:
                self.store.save_item(record)
        self.assertNotIn('secret exception', str(caught.exception))
        self.assertEqual(self.store._connection.execute('SELECT url_cipher,metadata FROM items').fetchone(), old)

    def test_group_transaction_rolls_back_if_later_encryption_fails(self):
        original_protect = TaskStore._protect
        count = 0

        def protect(value):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError('unavailable')
            return original_protect(value)

        with patch.object(TaskStore, '_protect', side_effect=protect):
            with self.assertRaises(BatchStoreError):
                self.store.save_items([self.item(), self.item()])
        self.assertEqual(self.store.load()['items'], [])

    def test_unknown_fields_credentials_and_unsafe_options_are_discarded(self):
        self.batch['options'].update(headers={'Cookie': 'unsafe-cookie'}, candidate={'url': 'unsafe-candidate'},
                                     password='unsafe-password', max_concurrent=900, retry_limit=999,
                                     quality='unsafe-quality', discovery_seconds=999)
        self.batch['token'] = 'unsafe-token'
        self.store.save_batch(self.batch)
        record = self.item(headers={'Authorization': 'unsafe-header'}, candidate={'url': 'unsafe-url'},
                           cookie='unsafe-cookie', progress={'percent': 110, 'speed': 'unsafe-speed',
                           'headers': 'unsafe-progress', 'downloaded_bytes': 12, 'total_bytes': float('nan')})
        self.store.save_item(record)
        data = self.store.load()
        options = data['batches'][0]['options']
        self.assertEqual(options['max_concurrent'], 10)
        self.assertEqual(options['retry_limit'], 3)
        self.assertEqual(options['discovery_seconds'], 120)
        self.assertEqual(options['quality'], 'auto')
        self.assertEqual(data['items'][0]['progress'], {'percent': 100.0, 'downloaded_bytes': 12})
        raw = b''.join(path.read_bytes() for path in self.root.iterdir() if path.is_file())
        self.assertNotIn(b'unsafe-', raw)

    def test_defaults_and_invalid_option_types_are_safe(self):
        self.batch['options'] = {'max_concurrent': True, 'retry_limit': '9', 'discovery_seconds': False,
                                 'output_directory': 'https://example.test/?secret=secret-path'}
        self.store.save_batch(self.batch)
        options = self.store.load()['batches'][0]['options']
        self.assertEqual(options, {'max_concurrent': 3, 'retry_limit': 3, 'discovery_seconds': 60,
                                  'output_directory': '', 'quality': 'auto', 'naming': 'title'})

    def test_same_id_cannot_move_to_other_batch(self):
        record = self.item()
        self.store.save_item(record)
        another = deepcopy(self.batch)
        another['id'] = str(uuid.uuid4())
        self.store.save_batch(another)
        record['batch_id'] = another['id']
        with self.assertRaises(BatchStoreError):
            self.store.save_item(record)
        self.assertEqual(self.store.load()['items'][0]['batch_id'], self.batch_id)

    def test_batch_pause_and_all_item_states_restore_without_policy_rewrite(self):
        states = ['waiting_discovery', 'analyzing', 'waiting_selection', 'waiting_download', 'downloading',
                  'merging', 'verifying', 'completed', 'paused', 'cancelled', 'failed', 'waiting_context']
        self.store.save_items([self.item(ordinal=index, state=state) for index, state in enumerate(states)])
        self.reopen()
        self.assertEqual([item['state'] for item in self.store.load()['items']], states)

    def test_existing_task_store_bytes_remain_untouched(self):
        tasks = self.root / 'tasks.json'
        tasks.write_bytes(b'preserve-encrypted-existing-recovery')
        before = tasks.read_bytes()
        self.store.save_item(self.item())
        self.reopen()
        self.store.load()
        self.assertEqual(tasks.read_bytes(), before)

    def test_corrupt_or_unknown_database_is_not_rebuilt(self):
        self.store.close()
        for raw in (b'corrupt-batch-store-secret', b''):
            path = self.root / (str(uuid.uuid4()) + '.sqlite')
            path.write_bytes(raw)
            with self.assertRaises(BatchStoreError):
                BatchStore(path)
            self.assertEqual(path.read_bytes(), raw)
        unknown = self.root / 'unknown.sqlite'
        connection = sqlite3.connect(unknown)
        try:
            connection.execute('CREATE TABLE original(value TEXT)')
            connection.execute('PRAGMA user_version=9')
            connection.commit()
        finally:
            connection.close()
        original = unknown.read_bytes()
        with self.assertRaises(BatchStoreError):
            BatchStore(unknown)
        self.assertEqual(unknown.read_bytes(), original)

    def test_sql_lock_failure_is_bounded_and_record_retained(self):
        record = self.item()
        self.store.save_item(record)
        other = sqlite3.connect(self.path)
        other.execute('BEGIN IMMEDIATE')
        try:
            record['title'] = 'not committed'
            with self.assertRaises(BatchStoreError) as caught:
                self.store.save_item(record)
            self.assertIn('存储被占用', str(caught.exception))
        finally:
            other.rollback()
            other.close()
        self.assertEqual(self.store.load()['items'][0]['title'], '目标作品')

    def test_invalid_identifiers_and_url_do_not_write(self):
        for changes in ({'id': 'bad-id'}, {'url': 'https://user:password@example.test/a'},
                        {'url': 'https://example.test/a b'}, {'batch_id': str(uuid.uuid4())}):
            with self.assertRaises(BatchStoreError):
                self.store.save_item(self.item(**changes))
        self.assertEqual(self.store.load()['items'], [])

    def test_create_batch_is_atomic_and_preserves_pause_scope(self):
        batch = deepcopy(self.batch)
        batch['id'] = str(uuid.uuid4())
        record = self.item(batch_id=batch['id'], pause_scope='batch', previous_state='analyzing', state='paused')
        self.store.create_batch(batch, [record])
        self.reopen()
        restored = next(item for item in self.store.load()['items'] if item['id'] == record['id'])
        self.assertEqual((restored['pause_scope'], restored['previous_state']), ('batch', 'analyzing'))
        broken = deepcopy(batch)
        broken['id'] = str(uuid.uuid4())
        with self.assertRaises(BatchStoreError):
            self.store.create_batch(broken, [self.item(batch_id=broken['id']), self.item(id='not-uuid')])
        self.assertNotIn(broken['id'], [batch['id'] for batch in self.store.load()['batches']])
        self.assertEqual(len(self.store.load()['items']), 1)

    def test_create_batch_rejects_repeated_id_without_resetting_completion(self):
        record = self.item(state='completed')
        self.store.save_item(record)
        record['state'] = 'waiting_discovery'
        with self.assertRaises(BatchStoreError):
            self.store.create_batch(self.batch, [record])
        self.assertEqual(self.store.load()['items'][0]['state'], 'completed')

    def test_unchanged_url_progress_updates_reuse_verified_cipher(self):
        record = self.item()
        self.store.save_item(record)
        with patch.object(TaskStore, '_protect', side_effect=AssertionError('must reuse cipher')):
            for percent in range(20):
                record['progress'] = {'percent': percent}
                self.store.save_item(record)
        self.reopen()
        self.store.load()
        with patch.object(TaskStore, '_protect', side_effect=AssertionError('must reuse restored cipher')):
            self.store.save_item(record)
        self.assertEqual(self.store.load()['items'][0]['progress']['percent'], 19)

    def test_huge_numbers_are_filtered_without_overflow(self):
        record = self.item(progress={'downloaded_bytes': 10 ** 1000, 'percent': float('inf')})
        self.store.save_item(record)
        self.assertEqual(self.store.load()['items'][0]['progress'], {})


if __name__ == '__main__':
    unittest.main()
