"""Path metadata fallback and scope safety using owned filesystem fixtures."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.media_storage_scan import StorageScanError, StorageScanner, _safe


def metadata_path(**fields):
    path = Mock()
    path.is_absolute.return_value = True
    path.parts = ('fixture', 'media.mp4')
    path.parents = ()
    path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG, **fields)
    path.is_junction.return_value = False
    return path


class StoragePathMetadataTests(unittest.TestCase):
    def test_complete_regular_snapshot_needs_no_second_junction_lookup(self):
        path = metadata_path(st_file_attributes=stat.FILE_ATTRIBUTE_ARCHIVE, st_reparse_tag=0)
        self.assertIs(_safe(path), path)
        path.lstat.assert_called_once_with()
        path.is_junction.assert_not_called()

    def test_symlink_mode_reparse_attribute_and_non_junction_tag_are_rejected(self):
        for mode, attributes, tag in (
            (stat.S_IFLNK, 0, 0),
            (stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT, 0),
            (stat.S_IFREG, 0, 0x9000001A),
        ):
            with self.subTest(mode=mode, attributes=attributes, tag=tag):
                path = metadata_path(st_file_attributes=attributes, st_reparse_tag=tag)
                path.lstat.return_value.st_mode = mode
                with self.assertRaises(StorageScanError):
                    _safe(path)
                path.is_junction.assert_not_called()

    def test_missing_or_partial_windows_metadata_keeps_junction_fallback(self):
        for fields in ({}, {'st_file_attributes': 0}, {'st_reparse_tag': 0}):
            with self.subTest(fields=fields):
                path = metadata_path(**fields)
                self.assertIs(_safe(path), path)
                path.is_junction.assert_called_once_with()
                path.is_junction.reset_mock()
                path.is_junction.return_value = True
                with self.assertRaises(StorageScanError):
                    _safe(path)
                path.is_junction.assert_called_once_with()

    def test_regular_posix_snapshot_without_optional_junction_method(self):
        path = SimpleNamespace(is_absolute=lambda: True, parts=('fixture', 'media.mp4'), parents=(),
                               lstat=lambda: SimpleNamespace(st_mode=stat.S_IFREG))
        self.assertIs(_safe(path), path)


class StoragePathScopeTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()).resolve()
        if any(part.casefold() == 'appdata' for part in base.parts):
            # Windows TEMP can be under AppData, which extra_dirs must reject.
            base = Path(__file__).resolve().parents[1]
        temp = tempfile.TemporaryDirectory(prefix='mediaflow-storage-path-test-', dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.contents = b'equal synthetic media contents'

    def make_file(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.contents)
        return path

    def directory_alias(self, target, alias):
        self.assertTrue(target.resolve().is_relative_to(self.root))
        self.assertTrue(alias.parent.resolve().is_relative_to(self.root))
        self.assertFalse(os.path.lexists(alias))
        if os.name == 'nt':
            from _winapi import CreateJunction
            CreateJunction(str(target), str(alias))
            self.addCleanup(alias.rmdir)
            self.assertTrue(alias.is_junction())
        else:
            alias.symlink_to(target, target_is_directory=True)
            self.addCleanup(alias.unlink)
            self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.resolve(), target.resolve())

    def test_directory_alias_cannot_escape_walk_or_history_scope(self):
        inside = self.make_file('library/kept.mp4')
        outside = self.make_file('outside/other.mp4')
        scope = inside.parent
        alias = scope / 'escape'
        self.directory_alias(outside.parent, alias)
        alias_file = alias / outside.name
        with patch.object(Path, 'open', autospec=True, side_effect=Path.open) as opened, \
                patch('core.media_storage_scan.os.scandir', wraps=os.scandir) as walked:
            result = StorageScanner().scan([{'path': str(alias_file)}], extra_dirs=(scope, alias))
        self.assertTrue(result['complete'])
        self.assertEqual({call.args[0] for call in opened.call_args_list}, {inside})
        self.assertEqual({Path(call.args[0]) for call in walked.call_args_list}, {scope})
        self.assertEqual(result['stats']['total_files'], 1)
        self.assertEqual(result['groups'], [])
        rows = {row['path']: row for row in result['files']}
        self.assertEqual(rows[str(alias_file)]['status'], 'skipped')
        self.assertTrue({str(alias), str(alias_file)} <= {item['path'] for item in result['issues']})
        self.assertEqual(outside.read_bytes(), self.contents)

    def test_enumerated_ancestor_turned_into_alias_is_rechecked_before_hash_open(self):
        victim = self.make_file('library/selected/media.mp4')
        peer = self.make_file('library/peer.mp4')
        original_stat = victim.stat()
        moved = self.root / 'moved-outside-library'
        replaced = False

        def replace_ancestor(event):
            nonlocal replaced
            if event['phase'] == 'enumerate' and event['files_done'] == 2 and not replaced:
                self.assertTrue(victim.parent.resolve().is_relative_to(self.root))
                self.assertTrue(moved.resolve().is_relative_to(self.root))
                victim.parent.rename(moved)
                self.directory_alias(moved, victim.parent)
                self.assertTrue(os.path.samestat(original_stat, victim.stat()))
                replaced = True

        with patch.object(Path, 'open', autospec=True, side_effect=Path.open) as opened:
            result = StorageScanner().scan([{'path': str(victim)}, {'path': str(peer)}],
                                           progress=replace_ancestor)
        self.assertTrue(replaced)
        self.assertTrue(result['complete'])
        self.assertEqual([call.args[0] for call in opened.call_args_list].count(victim), 1)
        self.assertEqual(result['files'][0]['status'], 'changed')
        self.assertTrue(any(item['path'] == str(victim) and 'junction' in item['reason']
                            for item in result['issues']))
        self.assertEqual(result['hashed_files'], 1)
        self.assertEqual(result['groups'], [])
        self.assertEqual(result['stats']['total_files'], 1)
        self.assertEqual((moved / victim.name).read_bytes(), self.contents)

    def test_candidate_replaced_during_open_is_excluded_before_hashing(self):
        victim = self.make_file('media.mp4')
        peer = self.make_file('peer.mp4')
        replacement = self.make_file('replacement.mp4')
        original = self.root / 'moved.mp4'
        victim_opens = 0
        real_open = Path.open

        def replace_before_open(path, *args, **kwargs):
            nonlocal victim_opens
            if path == victim:
                victim_opens += 1
                if victim_opens == 2:
                    victim.rename(original)
                    replacement.rename(victim)
            return real_open(path, *args, **kwargs)

        with patch.object(Path, 'open', replace_before_open):
            result = StorageScanner().scan([{'path': str(victim)}, {'path': str(peer)}])
        self.assertEqual(victim_opens, 2)
        self.assertTrue(result['complete'])
        self.assertEqual(result['files'][0]['status'], 'changed')
        self.assertEqual(result['hash_bytes'], len(self.contents))
        self.assertEqual(result['hashed_files'], 1)
        self.assertEqual(result['groups'], [])
        self.assertEqual(original.read_bytes(), self.contents)

    def test_nested_scope_dedup_keeps_sibling_with_similar_prefix(self):
        nested = self.make_file('library/nested/one.mp4')
        sibling = self.make_file('library-other/two.mp4')
        scope = nested.parent.parent
        result = StorageScanner().scan([], extra_dirs=(scope, nested.parent, sibling.parent, scope))
        self.assertTrue(result['complete'])
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['stats']['total_files'], 2)
        self.assertEqual({row['path'] for row in result['files']}, {str(nested), str(sibling)})
        self.assertEqual(result['stats']['reclaimable_bytes'], len(self.contents))
