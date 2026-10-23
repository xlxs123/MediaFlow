"""历史生命周期、分页、隐私以及与磁盘文件/恢复存储的隔离。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.media_history import HistoryStore, HistoryStoreError, snapshot_job


def video_validation(size=1200, duration=19.0, width=576, height=1024):
    return {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2', 'size': size, 'duration': duration,
            'video_streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': width,
                               'height': height, 'index': 0}],
            'audio_streams': [{'codec_type': 'audio', 'codec_name': 'aac', 'index': 1}]}


def audio_validation():
    return {'format_name': 'mp3', 'size': 900, 'duration': 42.5,
            'video_streams': [], 'audio_streams': [{'codec_type': 'audio', 'codec_name': 'mp3', 'index': 0}]}


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / 'history.sqlite'
        self.store = HistoryStore(self.database)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def job(self, *, name='十九秒作品', state='completed', job_id=1, workspace='a' * 32,
            files=None, validations=None, **request_values):
        request = SimpleNamespace(url='https://cdn.example.test/video.mp4?token=media-secret',
                                  headers={}, body=None, media_metadata={
                                      'page_url': 'https://www.douyin.com/user/self?modal_id=7526879737634950451&from_tab_name=main'},
                                  temporary_directory=str(self.root / '.stream-downloads' / workspace),
                                  transport_url='', manifest_url='')
        for key, value in request_values.items():
            setattr(request, key, value)
        result = {'success': state == 'completed', 'cancelled': state == 'cancelled',
                  'files': files if files is not None else [str(self.root / (name + '.mp4'))],
                  'output_validation': validations if validations is not None else [video_validation()],
                  'error': '' if state == 'completed' else '网络请求失败', 'error_code': ''}
        return SimpleNamespace(id=job_id, name=name, request=request, state=state,
                               created_at='2026-10-19T10:20:00.000+00:00',
                               updated_at='2026-10-19T10:21:00.000+00:00', result=result,
                               progress={'percent': 100 if state == 'completed' else 25,
                                         'downloaded_bytes': 1200}, context_error='', status='下载失败')

    def add(self, **kwargs):
        snapshot = snapshot_job(self.job(**kwargs))
        self.assertTrue(self.store.record(snapshot))
        return snapshot, self.store.query(view='history')['items'][0]


class HistoryMappingTests(HistoryFixture):
    def test_stable_uuid_identity_survives_directory_move_and_numeric_id_reuse(self):
        first = self.job()
        same = deepcopy(first)
        same.request.temporary_directory = 'Z:\\moved media\\.stream-downloads\\' + 'a' * 32
        same.id = 900
        same.created_at = '2026-10-20T12:00:00+00:00'
        self.assertEqual(snapshot_job(first)['task_key'], snapshot_job(same)['task_key'])
        another = self.job(job_id=1, workspace='b' * 32)
        self.assertNotEqual(snapshot_job(first)['task_key'], snapshot_job(another)['task_key'])

    def test_non_uuid_fallback_is_stable_but_distinguishes_new_task(self):
        first = self.job(workspace='legacy-workspace')
        same = deepcopy(first)
        self.assertEqual(snapshot_job(first)['task_key'], snapshot_job(same)['task_key'])
        same.created_at = '2026-10-20T12:00:00+00:00'
        self.assertNotEqual(snapshot_job(first)['task_key'], snapshot_job(same)['task_key'])

    def test_paused_cancel_signal_is_not_cancelled_history(self):
        job = self.job(state='paused')
        job.result['cancelled'] = True
        for state in ('queued', 'running', 'paused', 'stopping', 'waiting_network', 'retrying'):
            job.state = state
            self.assertEqual(snapshot_job(job), {})
        job.state = 'cancelled'
        snapshot = snapshot_job(job)
        self.assertEqual(snapshot['task_state'], 'cancelled')
        self.assertEqual(snapshot['cancelled_at'], snapshot['finished_at'])

    def test_only_real_waiting_context_failure_is_recorded(self):
        job = self.job(state='waiting_context')
        job.result['error_code'] = 'EXPIRED_URL'
        self.assertEqual(snapshot_job(job)['task_state'], 'failed')
        job.context_error = '恢复数据解密失败，请重新分析'
        self.assertEqual(snapshot_job(job), {})
        job.context_error = ''
        job.result['error_code'] = 'CONTEXT_UNAVAILABLE'
        self.assertEqual(snapshot_job(job), {})
        job.result = None
        self.assertEqual(snapshot_job(job), {})

    def test_mapping_has_no_io_and_keeps_only_validation_whitelist(self):
        job = self.job()
        job.result['output_validation'][0].update(headers={'Cookie': 'secret'},
                                                 download_url='https://cdn.test/a?token=secret',
                                                 tags={'password': 'private'})
        with patch.object(Path, 'stat', side_effect=AssertionError('mapping must not stat')), \
                patch.object(Path, 'resolve', side_effect=AssertionError('mapping must not resolve')):
            snapshot = snapshot_job(job, started_at='2026-10-19T10:20:20Z')
        self.assertEqual(snapshot['started_at'], '2026-10-19T10:20:20.000+00:00')
        self.assertEqual(set(snapshot['files'][0]['validation']),
                         {'format_name', 'size', 'duration', 'video_streams', 'audio_streams'})

    def test_media_and_context_urls_headers_and_body_never_enter_report_or_db(self):
        cookie = 'cookie-private-value'
        auth = 'access-private-value'
        body_secret = 'body-private-value'
        media_secret = 'media-private-signature'
        job = self.job(state='failed', files=[], validations=[],
                       headers={'Cookie': 'sid=' + cookie, 'Authorization': 'Bearer ' + auth},
                       body=json.dumps({'password': body_secret}).encode(),
                       url='https://cdn.example.test/path/' + media_secret + '/video.mp4?sign=' + media_secret,
                       media_metadata={'page_url': 'https://www.douyin.com/user/self?modal_id=7526879737634950451&token=page-private-token'})
        job.result['error'] = (f'失败 {cookie} {auth} {body_secret} '
                               f'https://cdn.test/{media_secret}/video.mp4?sign={media_secret}\n'
                               'Cookie: sid=' + cookie)
        snapshot = snapshot_job(job)
        self.assertEqual(snapshot['source_url'], '')
        self.assertEqual(snapshot['source_site'], 'douyin.com')
        self.assertTrue(self.store.record(snapshot))
        serialized = json.dumps(self.store.query(view='history'), ensure_ascii=False)
        for secret in (cookie, auth, body_secret, media_secret, 'page-private-token'):
            self.assertNotIn(secret, serialized)
        for field in ('headers', 'body', 'request_context', 'request'):
            self.assertNotIn(field, snapshot)
        self.store.close()
        raw = self.database.read_bytes()
        for secret in (cookie, auth, body_secret, media_secret, 'page-private-token'):
            self.assertNotIn(secret.encode(), raw)

    def test_source_url_only_public_page_identity(self):
        cases = [
            ('https://www.douyin.com/user/self?modal_id=7526879737634950451&showTab=record',
             'https://www.douyin.com/user/self?modal_id=7526879737634950451'),
            ('https://www.bilibili.com/video/BV1CjaQ6NE42?p=2&spm_id_from=333.1007',
             'https://www.bilibili.com/video/BV1CjaQ6NE42?p=2'),
            ('https://rou.video/v/cmuw7m0kr000cs6fz3ceohcua',
             'https://rou.video/v/cmuw7m0kr000cs6fz3ceohcua'),
            ('https://cdn.test/a.mp4', ''),
            ('https://cdn.test/path/signed-secret/video', ''),
            ('https://cdn.test/' + 'a' * 64 + '/play', ''),
            ('https://cdn.test/play?X-Amz-Signature=private', ''),
            ('https://user:password@site.test/watch', ''),
            ('javascript:alert(1)', ''),
            ('https://site.test/watch?video_id=public&unknown=private',
             'https://site.test/watch?video_id=public'),
        ]
        for page, expected in cases:
            with self.subTest(page=page):
                self.assertEqual(snapshot_job(self.job(media_metadata={'page_url': page}))['source_url'], expected)
        job = self.job(media_metadata={})
        self.assertEqual(snapshot_job(job)['source_url'], '')
        self.assertEqual(snapshot_job(job)['source_site'], 'cdn.example.test')

    def test_invalid_nonfinite_numbers_do_not_escape(self):
        job = self.job()
        job.progress = {'percent': float('nan'), 'total_bytes': 10 ** 1000, 'cookie': 'secret'}
        job.result['output_validation'][0].update(size=10 ** 1000, duration=float('inf'))
        snapshot = snapshot_job(job)
        self.assertEqual(snapshot['progress'], {})
        self.assertTrue(self.store.record(snapshot))
        result = self.store.query()['items'][0]
        self.assertEqual(result['size'], 0)
        self.assertEqual(result['duration'], 0.0)
        self.assertEqual(result['validation_state'], 'unverified')


class HistoryStoreTests(HistoryFixture):
    def test_restart_idempotence_and_active_tasks_stay_separate(self):
        recovery_path = self.root / 'tasks.json'
        recovery_path.write_bytes(b'{"encrypted":"unchanged-recovery-fixture"}')
        snapshot, entry = self.add()
        self.assertTrue(self.store.record(snapshot))
        self.assertEqual(self.store.query()['total'], 1)
        self.store.close()
        self.store = HistoryStore(self.database)
        self.assertFalse(self.store.record(snapshot, only_if_absent=True))
        self.assertEqual(self.store.get(entry['id'])['task_key'], snapshot['task_key'])
        self.assertEqual(recovery_path.read_bytes(), b'{"encrypted":"unchanged-recovery-fixture"}')

    def test_multifile_track_types_are_not_inferred_from_filename(self):
        files = [str(self.root / 'actual video.mp3'), str(self.root / 'actual audio.mp4')]
        _, _ = self.add(files=files, validations=[video_validation(), audio_validation()])
        rows = self.store.query()['items']
        self.assertEqual(len(rows), 2)
        by_path = {item['path']: item for item in rows}
        self.assertEqual(by_path[files[0]]['media_type'], 'video')
        self.assertEqual(by_path[files[1]]['media_type'], 'audio')
        self.assertEqual(by_path[files[1]]['audio_codec'], 'mp3')
        self.assertEqual(by_path[files[0]]['width'], 576)
        self.assertTrue(all(item['validation_state'] == 'valid' for item in rows))

    def test_failed_and_cancelled_without_files_only_appear_in_history(self):
        self.add(state='failed', files=[], validations=[])
        _, entry = self.add(state='cancelled', files=[], validations=[], workspace='b' * 32)
        self.assertEqual(self.store.query()['total'], 0)
        rows = self.store.query(view='history')['items']
        self.assertEqual({item['task_state'] for item in rows}, {'failed', 'cancelled'})
        self.assertTrue(entry['cancelled_at'])
        self.assertEqual(entry['progress']['percent'], 25)
        self.assertTrue(all(not item['path'] for item in rows))

    def test_filename_literal_search_type_site_sort_and_pagination(self):
        records = [
            ('literal_100%', video_validation(size=100, duration=3, width=100, height=100), 'douyin.com'),
            ('literalX100rest', video_validation(size=200, duration=2, width=200, height=200), 'bilibili.com'),
            ('audio', audio_validation(), 'douyin.com'),
        ]
        for index, (name, validation, site) in enumerate(records):
            self.add(name=name, workspace=hex(index + 1)[2:] * 32, validations=[validation],
                     media_metadata={'page_url': 'https://' + site + '/watch'})
        self.assertEqual(self.store.query(search='literal_100%')['total'], 1)
        self.assertEqual(self.store.query(media_type='audio')['total'], 1)
        self.assertEqual(self.store.query(source_site='douyin.com')['total'], 2)
        self.assertEqual(self.store.query(source_site="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.store.query(sort='size', limit=1)['items'][0]['title'], 'audio')
        self.assertEqual(self.store.query(sort='duration', offset=1, limit=1)['items'][0]['title'], 'literal_100%')
        self.assertEqual(self.store.query(sort='resolution', limit=1)['items'][0]['title'], 'literalX100rest')
        self.assertEqual(self.store.query()['sources'], ['bilibili.com', 'douyin.com'])
        self.assertEqual(self.store.query(search="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.store.query()['total'], 3)  # 清空筛选恢复完整列表。

    def test_large_history_query_is_bounded_and_does_no_file_checks(self):
        for index in range(140):
            snapshot = snapshot_job(self.job(job_id=index + 1, workspace=f'{index:032x}', name=f'作品 {index:04d}'))
            self.store.record(snapshot)
        with patch.object(Path, 'stat', side_effect=AssertionError('queries must not stat files')):
            result = self.store.query(limit=10000)
            other = self.store.query(offset=100, limit=50)
        self.assertEqual(result['total'], 140)
        self.assertEqual(len(result['items']), 100)
        self.assertEqual(len(other['items']), 40)
        self.assertFalse({item['id'] for item in result['items']} & {item['id'] for item in other['items']})

    def test_remove_record_keeps_media_and_tombstone_blocks_reimport(self):
        file = self.root / 'keep.mp4'
        file.write_bytes(b'user-media-must-stay')
        snapshot, entry = self.add(files=[str(file)])
        self.assertTrue(self.store.remove(entry['id']))
        self.assertEqual(file.read_bytes(), b'user-media-must-stay')
        self.assertIsNone(self.store.get(entry['id']))
        self.assertFalse(self.store.record(snapshot, only_if_absent=True))
        self.assertFalse(self.store.record(snapshot))
        self.assertFalse(self.store.update_thumbnails(snapshot['task_key'], [{'status': 'generated', 'path': str(self.root / 'cover.jpg')}]))
        self.assertEqual(self.store.query(view='history')['total'], 0)
        self.store.close()
        self.store = HistoryStore(self.database)
        self.assertFalse(self.store.record(snapshot))

    def test_multifile_remove_does_not_hide_other_files_or_resurrect_slot(self):
        snapshot, _ = self.add(files=[str(self.root / 'one.mp4'), str(self.root / 'two.mp4')],
                               validations=[video_validation(), video_validation()])
        rows = self.store.query()['items']
        removed = next(item for item in rows if item['path'].endswith('one.mp4'))
        self.assertTrue(self.store.remove(removed['id']))
        self.assertTrue(self.store.record(snapshot))
        self.assertEqual(self.store.query()['total'], 1)
        self.assertTrue(self.store.query()['items'][0]['path'].endswith('two.mp4'))

    def test_missing_file_status_is_preserved_and_does_not_remove_history(self):
        _, entry = self.add()
        self.assertFalse(Path(entry['path']).exists())
        self.assertTrue(self.store.update(entry['id'], {'file_state': 'missing'}))
        self.store.close()
        self.store = HistoryStore(self.database)
        self.assertEqual(self.store.get(entry['id'])['file_state'], 'missing')
        self.assertEqual(self.store.query()['total'], 1)

    def test_relocate_and_manual_verification_survive_restore_and_duplicate_callback(self):
        snapshot, entry = self.add()
        moved = str(self.root / 'moved.mp4')
        self.assertTrue(self.store.update(entry['id'], {'path': moved, 'thumbnail_path': '',
                                                       'validation': {}, 'validation_state': 'unverified',
                                                       'file_state': 'unverified'}))
        self.assertFalse(self.store.record(snapshot, only_if_absent=True))
        self.assertTrue(self.store.record(snapshot))
        self.assertEqual(self.store.get(entry['id'])['path'], moved)
        self.assertEqual(self.store.get(entry['id'])['validation'], {})
        self.assertEqual(self.store.get(entry['id'])['size'], 0)
        summary = video_validation(size=2000, duration=20)
        summary.update(full_decode={'status': 'passed', 'streams_read': ['video', 'audio'],
                                    'progress_end': True, 'decoded_frames': 600, 'expected_frames': 600,
                                    'raw_stderr': 'cookie=private'}, checked_at='2026-10-19T12:00:00Z')
        self.store.update(entry['id'], {'validation': summary, 'validation_state': 'valid', 'file_state': 'exists'})
        self.store.record(snapshot)
        result = self.store.get(entry['id'])
        self.assertEqual(result['size'], 2000)
        self.assertEqual(result['duration'], 20)
        self.assertEqual(result['validation']['full_decode']['decoded_frames'], 600)
        self.assertNotIn('raw_stderr', result['validation']['full_decode'])
        self.assertEqual(result['validation']['checked_at'], '2026-10-19T12:00:00.000+00:00')
        self.assertFalse(self.store.update_thumbnails(snapshot['task_key'], [{'status': 'generated', 'path': str(self.root / 'old.jpg')}]))

    def test_thumbnails_update_only_existing_records_and_survive_repeated_completion(self):
        snapshot, entry = self.add()
        thumbnail = str(self.root / 'cover.jpg')
        self.assertTrue(self.store.update_thumbnails(snapshot['task_key'], [{'status': 'generated', 'path': thumbnail}]))
        self.assertFalse(self.store.update_thumbnails(snapshot['task_key'], [{'status': 'generated', 'path': thumbnail}]))
        self.store.record(snapshot)
        self.assertEqual(self.store.get(entry['id'])['thumbnail_path'], thumbnail)
        self.assertFalse(self.store.update_thumbnails('no-such-task', [{'status': 'generated', 'path': thumbnail}]))

    def test_restore_without_probe_is_unverified_and_later_summary_can_fill(self):
        snapshot, entry = self.add(validations=[])
        self.assertEqual(entry['validation_state'], 'unverified')
        self.assertEqual(entry['media_type'], 'unknown')
        snapshot['files'][0]['validation'] = video_validation()
        self.assertTrue(self.store.record(snapshot))
        self.assertEqual(self.store.get(entry['id'])['validation_state'], 'valid')
        snapshot['files'][0]['validation'] = {}
        self.store.record(snapshot)
        self.assertEqual(self.store.get(entry['id'])['validation_state'], 'valid')

    def test_update_whitelist_prevents_context_storage_and_clear_paths_is_supported(self):
        _, entry = self.add()
        self.assertFalse(self.store.update(entry['id'], {'Cookie': 'never-store', 'headers': {'Authorization': 'secret'}}))
        self.assertTrue(self.store.update(entry['id'], {'path': '', 'thumbnail_path': '',
                                                       'validation_state': 'failed', 'file_state': 'invalid',
                                                       'validation_reason': 'Cookie: private-value'}))
        result = self.store.get(entry['id'])
        self.assertEqual(result['path'], '')
        self.assertEqual(result['file_state'], 'invalid')
        self.assertNotIn('private-value', result['validation_reason'])
        self.assertNotIn('Cookie', result)
        self.assertFalse(self.store.update(9999, {'file_state': 'missing'}))
        self.assertFalse(self.store.remove(9999))

    def test_locked_database_returns_actionable_safe_error(self):
        blocker = sqlite3.connect(self.database)
        try:
            blocker.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(HistoryStoreError, '正被占用'):
                self.store.record(snapshot_job(self.job()))
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(self.store.query()['total'], 0)
        self.assertTrue(self.store.record(snapshot_job(self.job())))

    def test_corrupt_and_incompatible_databases_are_not_rebuilt(self):
        self.store.close()
        corrupt = self.root / 'corrupt.sqlite'
        raw = b'This is an unrecoverable database; retain original bytes.'
        corrupt.write_bytes(raw)
        with self.assertRaisesRegex(HistoryStoreError, '损坏|无法读写'):
            HistoryStore(corrupt)
        self.assertEqual(corrupt.read_bytes(), raw)
        empty = self.root / 'empty.sqlite'
        empty.write_bytes(b'')
        with self.assertRaisesRegex(HistoryStoreError, '为空'):
            HistoryStore(empty)
        self.assertEqual(empty.read_bytes(), b'')
        incompatible = self.root / 'foreign.sqlite'
        connection = sqlite3.connect(incompatible)
        try:
            connection.execute('CREATE TABLE foreign_data(value TEXT)')
            connection.execute("INSERT INTO foreign_data VALUES ('keep-me')")
            connection.commit()
        finally:
            connection.close()
        before = incompatible.read_bytes()
        with self.assertRaisesRegex(HistoryStoreError, '不兼容'):
            HistoryStore(incompatible)
        self.assertEqual(incompatible.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
