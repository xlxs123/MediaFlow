"""真实文件/SQLite 的整理事务与故障注入；字节夹具只验证文件组织，不冒充媒体验收。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.library_organizer import LibraryOrganizer, OrganizationError
from core.media_history import HistoryStore, HistoryStoreError


class SimulatedPowerLoss(BaseException):
    pass


class OrganizerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-organize-unit-')
        self.root = Path(self.temporary.name)
        self.source = self.root / '原始媒体'
        self.source.mkdir()
        self.target = self.root / '整理后'
        self.store = HistoryStore(self.root / 'history.sqlite3')
        self.organizer = LibraryOrganizer(self.root / 'organization-journal')
        self.snapshots = {}

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, name='视频甲.mp4', *, key=None, sidecar=False, thumbnail=False, path=None, **fields):
        key = key or 'task-' + str(len(self.snapshots) + 1)
        media = path or self.source / name
        if not media.exists():
            media.write_bytes(b'filesystem transaction fixture ' + name.encode('utf-8') * 30)
        info = media.stat()
        validation = {'format_name': 'mp4', 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
                      'duration': 4.0, 'video_streams': [{'codec_name': 'h264', 'width': 320, 'height': 180}],
                      'audio_streams': [{'codec_name': 'aac', 'language': 'eng'}]}
        if sidecar:
            subtitle = media.with_name(media.stem + '.zh-Hans.unit.srt')
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n单元测试\n', encoding='utf-8')
            validation['subtitle_files'] = [{'path': str(subtitle), 'language': 'zh-Hans', 'format': 'srt',
                'sha256': hashlib.sha256(subtitle.read_bytes()).hexdigest(), 'size': subtitle.stat().st_size,
                'status': 'verified'}]
        file = {'path': str(media), 'validation': validation}
        if thumbnail:
            cover = media.with_suffix('.jpg')
            cover.write_bytes(b'local recorded thumbnail bytes')
            file['thumbnail_path'] = str(cover)
        snapshot = {'task_key': key, 'queue_id': len(self.snapshots) + 1, 'title': media.stem,
            'source_site': 'bilibili.com', 'source_url': 'https://www.bilibili.com/video/BV1CjaQ6NE42',
            'task_state': 'completed', 'finished_at': '2026-10-21T12:34:56+08:00', 'files': [file], **fields}
        self.store.record(snapshot)
        self.snapshots[key] = deepcopy(snapshot)
        return next(item for item in self.store.query(view='history')['items'] if item['task_key'] == key)

    def preview(self, *entries, **options):
        return self.organizer.preview(self.store, [entry['id'] for entry in entries], str(self.target), options)

    def apply(self, plan):
        return self.organizer.apply(self.store, plan['token'], confirmed=True)


class OrganizerTransactionTests(OrganizerFixture):
    def test_preview_does_not_create_directories_move_or_touch_unselected_media(self):
        selected = self.add(sidecar=True)
        unselected = self.add('视频乙.mp4')
        plan = self.preview(selected, classification='type_site', filename_template='{site}_{title}_{date}.{ext}')
        self.assertEqual(plan['ready_count'], 1)
        self.assertEqual(len(plan['rows']), 2)
        self.assertTrue(all(row['mkdirs'] for row in plan['rows']))
        self.assertFalse(self.target.exists())
        self.assertTrue(Path(selected['path']).exists())
        self.assertTrue(Path(unselected['path']).exists())
        self.assertIn('视频', plan['rows'][0]['new'])
        self.assertIn('Bilibili', plan['rows'][0]['new'])

    def test_media_sidecars_and_explicit_local_cover_move_together(self):
        entry = self.add(sidecar=True, thumbnail=True)
        before = Path(entry['path']).read_bytes()
        plan = self.preview(entry, classification='site', filename_template='{title}_{resolution}.{ext}',
                            move_thumbnails=True)
        result = self.apply(plan)
        self.assertEqual((result['completed'], result['failed']), (1, 0))
        self.assertEqual({item['kind'] for item in result['mappings']}, {'media', 'subtitle', 'thumbnail'})
        current = self.store.get(entry['id'])
        self.assertEqual(Path(current['path']).read_bytes(), before)
        self.assertFalse(Path(entry['path']).exists())
        self.assertTrue(Path(current['thumbnail_path']).exists())
        subtitle = current['validation']['subtitle_files'][0]
        self.assertTrue(Path(subtitle['path']).name.startswith(Path(current['path']).stem + '.'))
        self.assertEqual(subtitle['sha256'], hashlib.sha256(Path(subtitle['path']).read_bytes()).hexdigest())
        self.assertEqual(current['validation_state'], 'valid')
        self.assertEqual(Path(current['path']).stat().st_mtime_ns, entry['validation']['mtime_ns'])

    def test_default_keeps_registered_thumbnail_and_never_scans_unknown_cached_files(self):
        entry = self.add(thumbnail=True)
        unknown = self.source / 'unrelated-cache.jpg'
        unknown.write_bytes(b'unrelated')
        self.apply(self.preview(entry))
        current = self.store.get(entry['id'])
        self.assertEqual(current['thumbnail_path'], entry['thumbnail_path'])
        self.assertTrue(Path(entry['thumbnail_path']).exists())
        self.assertEqual(unknown.read_bytes(), b'unrelated')

    def test_explicit_confirmation_is_required_and_plan_is_single_use(self):
        entry = self.add()
        plan = self.preview(entry)
        with self.assertRaisesRegex(OrganizationError, '明确确认'):
            self.organizer.apply(self.store, plan['token'])
        self.assertTrue(Path(entry['path']).exists())
        self.assertEqual(self.apply(plan)['completed'], 1)
        with self.assertRaisesRegex(OrganizationError, '已经使用'):
            self.apply(plan)

    def test_same_name_uses_numbering_and_never_treats_existing_bytes_as_result(self):
        entry = self.add()
        self.target.mkdir()
        existing = self.target / Path(entry['path']).name
        existing.write_bytes(Path(entry['path']).read_bytes())
        plan = self.preview(entry)
        self.assertTrue(plan['rows'][0]['conflict'])
        self.assertIn('(1)', plan['rows'][0]['new'])
        result = self.apply(plan)
        self.assertEqual(result['completed'], 1)
        self.assertTrue(existing.exists())
        self.assertNotEqual(self.store.get(entry['id'])['path'], str(existing))

    def test_new_conflict_after_preview_aborts_without_overwrite(self):
        entry = self.add()
        plan = self.preview(entry)
        new = Path(plan['rows'][0]['new'])
        new.parent.mkdir(parents=True)
        new.write_bytes(b'normal file created after preview')
        result = self.apply(plan)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(new.read_bytes(), b'normal file created after preview')
        self.assertTrue(Path(entry['path']).exists())
        self.assertEqual(self.store.get(entry['id'])['path'], entry['path'])

    def test_source_changed_after_preview_is_preserved_without_publishing(self):
        entry = self.add()
        plan = self.preview(entry)
        Path(entry['path']).write_bytes(b'newly changed source')
        result = self.apply(plan)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(Path(entry['path']).read_bytes(), b'newly changed source')
        self.assertFalse(self.target.exists())

    def test_second_associated_file_move_failure_rolls_back_entire_group(self):
        entry = self.add(sidecar=True)
        plan = self.preview(entry)
        original = self.organizer._copy
        def fail_subtitle(item, journal, index):
            if item['kind'] == 'subtitle':
                raise PermissionError('injected move error')
            return original(item, journal, index)
        with patch.object(self.organizer, '_copy', side_effect=fail_subtitle):
            result = self.apply(plan)
        self.assertEqual((result['completed'], result['failed']), (0, 1))
        self.assertTrue(Path(entry['path']).exists())
        self.assertTrue(Path(entry['validation']['subtitle_files'][0]['path']).exists())
        self.assertTrue(all(not Path(row['new']).exists() for row in plan['rows']))
        self.assertEqual(self.store.get(entry['id'])['path'], entry['path'])

    def test_database_update_failure_keeps_original_and_removes_only_our_targets(self):
        entry = self.add(sidecar=True)
        plan = self.preview(entry)
        with patch.object(self.store, 'commit_organization', side_effect=HistoryStoreError('数据库注入失败')):
            result = self.apply(plan)
        self.assertEqual(result['failed'], 1)
        self.assertIn('数据库', result['errors'][0]['reason'])
        self.assertTrue(Path(entry['path']).exists())
        self.assertEqual(self.store.get(entry['id'])['path'], entry['path'])
        self.assertTrue(all(not Path(row['new']).exists() for row in plan['rows']))

    def test_failed_group_does_not_prevent_other_selected_group(self):
        one = self.add('甲.mp4')
        two = self.add('乙.mp4')
        plan = self.preview(one, two)
        Path(one['path']).write_bytes(b'changed')
        result = self.apply(plan)
        self.assertEqual((result['completed'], result['failed']), (1, 1))
        self.assertTrue(Path(one['path']).exists())
        self.assertFalse(Path(two['path']).exists())
        self.assertTrue(Path(self.store.get(two['id'])['path']).exists())

    def test_store_cas_rejects_concurrent_record_change_and_rolls_back(self):
        entry = self.add()
        plan = self.preview(entry)
        original = self.store.commit_organization
        def change_then_commit(*args):
            self.store.update(entry['id'], {'validation_state': 'unverified'})
            return original(*args)
        with patch.object(self.store, 'commit_organization', side_effect=change_then_commit):
            result = self.apply(plan)
        self.assertEqual(result['failed'], 1)
        self.assertTrue(Path(entry['path']).exists())
        self.assertEqual(self.store.get(entry['id'])['validation_state'], 'unverified')
        self.assertFalse(Path(plan['rows'][0]['new']).exists())

    def test_real_sqlite_error_halfway_through_shared_record_updates_rolls_back_all_rows(self):
        entry = self.add(sidecar=True)
        second = self.add(key='shared-atomic', path=Path(entry['path']), sidecar=True)
        plan = self.preview(entry, second)
        self.store._connection.execute(f'''CREATE TRIGGER fail_second_organize BEFORE UPDATE OF path ON entries
            WHEN OLD.id={second['id']} BEGIN SELECT RAISE(ABORT,'injected'); END''')
        result = self.apply(plan)
        self.assertEqual(result['failed'], 2)
        self.assertTrue(Path(entry['path']).exists())
        self.assertEqual(self.store.get(entry['id'])['path'], entry['path'])
        self.assertEqual(self.store.get(second['id'])['path'], entry['path'])
        self.assertTrue(all(not Path(row['new']).exists() for row in plan['rows']))

    def test_new_unselected_reference_at_commit_is_rejected_inside_sqlite_transaction(self):
        entry = self.add()
        plan = self.preview(entry)
        original = self.store.commit_organization
        def share_then_commit(*args):
            self.add(key='late-shared-record', path=Path(entry['path']))
            return original(*args)
        with patch.object(self.store, 'commit_organization', side_effect=share_then_commit):
            result = self.apply(plan)
        self.assertEqual(result['failed'], 1)
        self.assertTrue(Path(entry['path']).exists())
        self.assertFalse(Path(plan['rows'][0]['new']).exists())
        self.assertIn('引用', result['errors'][0]['reason'])

    def test_long_chinese_name_conflict_and_subtitle_keep_real_extension_within_path_budget(self):
        from core.file_names import _units
        entry = self.add(sidecar=True, title='中文标题' * 80)
        self.target = self.root / ('分类目录' * 5)
        first = self.preview(entry, filename_template='{title}.{ext}')
        occupied = Path(first['rows'][0]['new'])
        occupied.parent.mkdir(parents=True)
        occupied.write_bytes(b'normal existing file')
        plan = self.preview(entry, filename_template='{title}.{ext}')
        self.assertTrue(plan['rows'][0]['conflict'])
        self.assertTrue(all(_units(row['new']) <= 240 for row in plan['rows']))
        self.assertEqual(self.apply(plan)['completed'], 1)
        current = self.store.get(entry['id'])
        self.assertTrue(Path(current['validation']['subtitle_files'][0]['path']).name.startswith(
            Path(current['path']).stem + '.'))
        self.assertEqual(occupied.read_bytes(), b'normal existing file')

    def test_late_download_and_thumbnail_callbacks_cannot_restore_old_path(self):
        entry = self.add(sidecar=True)
        result = self.apply(self.preview(entry, filename_template='{title}_整理.{ext}'))
        new = self.store.get(entry['id'])
        self.store.record(self.snapshots[entry['task_key']])
        self.store.update_thumbnails(entry['task_key'], [{'status': 'generated', 'path': str(self.source / 'late.jpg')}])
        after = self.store.get(entry['id'])
        self.assertEqual(after['path'], new['path'])
        self.assertEqual(after['validation']['subtitle_files'], new['validation']['subtitle_files'])
        self.assertEqual(after['thumbnail_path'], new['thumbnail_path'])
        file_name = self.store._connection.execute('SELECT file_name FROM entries WHERE id=?', (entry['id'],)).fetchone()[0]
        self.assertEqual(file_name, Path(result['mappings'][0]['new']).name)

    def test_history_same_path_without_rename_is_an_explicit_no_op(self):
        entry = self.add()
        plan = self.organizer.preview(self.store, [entry['id']], str(self.source))
        self.assertEqual((plan['ready_count'], plan['unchanged_count']), (0, 1))
        self.assertEqual(plan['rows'][0]['old'], plan['rows'][0]['new'])
        result = self.apply(plan)
        self.assertEqual(result['mappings'], [])
        self.assertTrue(Path(entry['path']).exists())

    def test_unselected_record_shared_media_path_is_not_moved(self):
        entry = self.add()
        unselected = self.add(key='shared-record', path=Path(entry['path']))
        plan = self.preview(entry)
        self.assertEqual(plan['ready_count'], 0)
        self.assertIn('未选中', plan['rows'][0]['reason'])
        self.assertTrue(Path(entry['path']).exists())
        self.assertEqual(self.store.get(unselected['id'])['path'], entry['path'])

    def test_selected_duplicate_records_with_same_plan_share_one_file_transaction(self):
        entry = self.add(sidecar=True)
        duplicate = self.add(key='shared-selected', path=Path(entry['path']), sidecar=True)
        plan = self.preview(entry, duplicate)
        self.assertEqual(plan['ready_count'], 2)
        result = self.apply(plan)
        self.assertEqual(result['completed'], 2)
        self.assertEqual(self.store.get(entry['id'])['path'], self.store.get(duplicate['id'])['path'])
        self.assertEqual({item['entry_id'] for item in result['mappings'] if item['kind'] == 'media'},
                         {entry['id'], duplicate['id']})

    def test_shared_subtitle_in_unselected_other_media_blocks_whole_group(self):
        entry = self.add(sidecar=True)
        other = self.add('不同媒体.mp4')
        self.store.update(other['id'], {'validation': {**other['validation'],
                          'subtitle_files': entry['validation']['subtitle_files']}})
        plan = self.preview(entry)
        self.assertEqual(plan['ready_count'], 0)
        self.assertIn('引用', plan['rows'][0]['reason'])

    def test_linked_directory_temporary_missing_and_unverified_files_are_skipped(self):
        entry = self.add()
        real = Path.is_junction
        with patch.object(Path, 'is_junction', lambda path: path == self.source or real(path)):
            plan = self.preview(entry)
        self.assertEqual(plan['ready_count'], 0)
        self.assertIn('链接', plan['rows'][0]['reason'])
        self.store.update(entry['id'], {'validation_state': 'unverified'})
        self.assertEqual(self.preview(entry)['ready_count'], 0)
        self.store.update(entry['id'], {'validation_state': 'valid'})
        Path(entry['path']).unlink()
        self.assertEqual(self.preview(entry)['ready_count'], 0)
        temporary = self.source / '.stream-downloads'
        temporary.mkdir()
        part = temporary / 'looks-valid.mp4'
        part.write_bytes(b'not final')
        temp_entry = self.add(path=part)
        self.assertIn('临时文件', self.preview(temp_entry)['rows'][0]['reason'])

    def test_modified_destination_during_db_failure_is_not_deleted(self):
        entry = self.add()
        plan = self.preview(entry)
        new = Path(plan['rows'][0]['new'])
        def fail(*args):
            new.write_bytes(b'external replacement content')
            raise HistoryStoreError('数据库注入失败')
        with patch.object(self.store, 'commit_organization', side_effect=fail):
            result = self.apply(plan)
        self.assertTrue(result['warnings'])
        self.assertEqual(new.read_bytes(), b'external replacement content')
        self.assertTrue(Path(entry['path']).exists())

    def test_original_cleanup_failure_keeps_committed_output_and_retries_safely(self):
        entry = self.add()
        plan = self.preview(entry)
        original = Path.unlink
        def occupied(path, *args, **kwargs):
            if str(path) == entry['path']:
                raise PermissionError('occupied original')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', occupied):
            result = self.apply(plan)
        self.assertEqual(result['completed'], 1)
        self.assertTrue(result['warnings'])
        self.assertTrue(Path(entry['path']).exists())
        self.assertTrue(Path(self.store.get(entry['id'])['path']).exists())
        recovered = LibraryOrganizer(self.organizer.directory).recover(self.store)
        self.assertFalse(Path(entry['path']).exists())
        self.assertEqual(recovered['mappings'], result['mappings'])


class OrganizerRecoveryTests(OrganizerFixture):
    def test_crash_after_target_publish_before_db_commit_rolls_back_on_restart(self):
        entry = self.add(sidecar=True)
        plan = self.preview(entry)
        with patch.object(self.store, 'commit_organization', side_effect=SimulatedPowerLoss):
            with self.assertRaises(SimulatedPowerLoss):
                self.apply(plan)
        self.assertTrue(all(Path(row['new']).exists() for row in plan['rows']))
        self.assertTrue(Path(entry['path']).exists())
        recovered = LibraryOrganizer(self.organizer.directory).recover(self.store)
        self.assertEqual(recovered['recovered'], 1)
        self.assertEqual(recovered['mappings'], [])
        self.assertTrue(all(not Path(row['new']).exists() for row in plan['rows']))
        self.assertEqual(self.store.get(entry['id'])['path'], entry['path'])

    def test_crash_after_atomic_db_commit_finishes_move_and_persistently_replays_mapping(self):
        entry = self.add(sidecar=True)
        plan = self.preview(entry)
        with patch.object(self.organizer, '_cleanup', side_effect=SimulatedPowerLoss):
            with self.assertRaises(SimulatedPowerLoss):
                self.apply(plan)
        self.assertNotEqual(self.store.get(entry['id'])['path'], entry['path'])
        self.assertTrue(Path(entry['path']).exists())
        restarted = LibraryOrganizer(self.organizer.directory)
        first = restarted.recover(self.store)
        second = restarted.recover(self.store)
        self.assertFalse(Path(entry['path']).exists())
        self.assertEqual(first['mappings'], second['mappings'])
        self.assertEqual(len(first['mappings']), 2)
        self.assertEqual(second['recovered'], 0)

    def test_crash_with_partial_stage_removes_only_owned_partial(self):
        entry = self.add()
        plan = self.preview(entry)
        def partial(item, journal, index):
            self.organizer._mkdir(Path(item['new']).parent, journal)
            with Path(item['stage']).open('xb') as handle:
                import os
                info = os.fstat(handle.fileno())
                journal['owned'][str(index)] = [info.st_dev, info.st_ino]
                self.organizer._save(journal)
                handle.write(b'partial')
            raise SimulatedPowerLoss()
        with patch.object(self.organizer, '_copy', side_effect=partial):
            with self.assertRaises(SimulatedPowerLoss):
                self.apply(plan)
        unrelated = self.target / 'other-download.part'
        unrelated.write_bytes(b'other task')
        result = LibraryOrganizer(self.organizer.directory).recover(self.store)
        self.assertEqual(result['recovered'], 1)
        self.assertEqual(unrelated.read_bytes(), b'other task')
        self.assertEqual(list(self.target.glob('.mediaflow-organize-*.part')), [])
        self.assertTrue(Path(entry['path']).exists())

    def test_tampered_journal_cannot_move_or_delete_arbitrary_files(self):
        entry = self.add()
        self.apply(self.preview(entry))
        journal = next(self.organizer.directory.glob('*.json'))
        data = json.loads(journal.read_text(encoding='utf-8'))
        victim = self.source / 'normal.mp4'
        victim.write_bytes(b'unrelated file')
        data['files'][0]['old'] = str(victim)
        journal.write_text(json.dumps(data), encoding='utf-8')
        result = LibraryOrganizer(self.organizer.directory).recover(self.store)
        self.assertTrue(result['warnings'])
        self.assertEqual(result['mappings'], [])
        self.assertEqual(victim.read_bytes(), b'unrelated file')

    def test_journal_has_no_source_webpage_credentials_or_unrelated_metadata(self):
        entry = self.add(sidecar=True)
        self.apply(self.preview(entry))
        text = next(self.organizer.directory.glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('https://', text)
        self.assertNotIn('source_url', text)
        self.assertNotIn('Cookie', text)
        self.assertNotIn('video_streams', text)

    def test_completed_mapping_chain_replays_in_order_after_later_organization(self):
        entry = self.add()
        first = self.apply(self.preview(entry))
        current = self.store.get(entry['id'])
        second = self.apply(self.preview(current, filename_template='{title}_第二次.{ext}'))
        replay = LibraryOrganizer(self.organizer.directory).recover(self.store)
        self.assertEqual(replay['mappings'], first['mappings'] + second['mappings'])
        self.assertFalse(Path(first['mappings'][0]['new']).exists())
        self.assertTrue(Path(second['mappings'][0]['new']).exists())

    def test_existing_version_one_history_data_survives_additive_transaction_table(self):
        entry = self.add()
        self.store._connection.execute('DROP TABLE organization_transactions')
        path = self.store.path
        self.store.close()
        self.store = HistoryStore(path)
        self.assertEqual(self.store.get(entry['id'])['path'], entry['path'])
        self.assertEqual(self.store._connection.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(self.apply(self.preview(entry))['completed'], 1)
