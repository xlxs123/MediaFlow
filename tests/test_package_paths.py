"""Real receipt bytes and exact per-ancestor checks; no large allocation needed."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.native_sbom import _read_bound, MAX_FILE, NativeSBOMError
from scripts.package_update import _plain, PackageError


class BoundedReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-bound-read-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.bin'
        self.original = b'actual small receipt-bound fixture'
        self.path.write_bytes(self.original)
        self.record = dict(size=len(self.original), sha256=hashlib.sha256(self.original).hexdigest())

    def test_real_small_file_requests_only_receipt_length_plus_growth_sentinel(self):
        requested = []
        @contextmanager
        def guarded(path):
            with Path(path).open('rb') as stream:
                class Reader:
                    def read(_, length):
                        requested.append(length)
                        self.assertLessEqual(length, self.record['size'] + 1)
                        return stream.read(length)
                yield Reader()
        with patch('core.data_migration.frozen_file', guarded):
            self.assertEqual(_read_bound(self.path, self.record), self.original)
        self.assertEqual(requested, [len(self.original) + 1])

    def test_append_truncation_and_same_length_replacement_remain_rejected(self):
        for changed in (self.original + b'!', self.original + b'x' * 8192,
                        self.original[:-1], b'', b'x' * len(self.original)):
            with self.subTest(length=len(changed)):
                self.path.write_bytes(changed)
                with self.assertRaises(NativeSBOMError):
                    _read_bound(self.path, self.record)

    def test_empty_ordinary_file_retains_existing_compatibility(self):
        self.path.write_bytes(b'')
        self.assertEqual(_read_bound(self.path, dict(size=0, sha256=hashlib.sha256(b'').hexdigest())), b'')

    def test_invalid_receipt_lengths_fail_before_a_read_handle_is_opened(self):
        for length in (-1, MAX_FILE + 1):
            with self.subTest(length=length), patch('core.data_migration.frozen_file') as frozen:
                with self.assertRaises(NativeSBOMError):
                    _read_bound(self.path, dict(self.record, size=length))
                frozen.assert_not_called()


class PlainPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-path-probe-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).absolute()

    def test_every_component_is_checked_once_without_following_exists_probe(self):
        path = self.root / 'missing' / 'output.zip'
        original = Path.lstat
        checked = []
        def observe(part):
            checked.append(part)
            return original(part)
        with patch.object(Path, 'lstat', observe), patch.object(Path, 'exists', side_effect=AssertionError('No redundant following probe')):
            self.assertEqual(_plain(path), path)
        self.assertEqual(checked, [path, *path.parents])

    def test_leaf_and_ancestor_links_and_non_symlink_reparse_points_are_rejected(self):
        path = self.root / 'nested' / 'output.zip'
        original = Path.lstat
        for target in (path, path.parent):
            for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
                def observe(part):
                    if part == target:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return original(part)
                with self.subTest(target=target.name, attributes=attributes), patch.object(Path, 'lstat', observe):
                    with self.assertRaises(PackageError):
                        _plain(path)

    def test_permission_and_non_directory_errors_are_not_treated_as_safe_absence(self):
        path = self.root / 'blocked' / 'output.zip'
        original = Path.lstat
        for error in (PermissionError('denied'), NotADirectoryError('not a directory')):
            def observe(part):
                if part == path:
                    raise error
                return original(part)
            with self.subTest(error=type(error).__name__), patch.object(Path, 'lstat', observe):
                with self.assertRaises(type(error)):
                    _plain(path)

    def test_real_existing_file_and_missing_output_are_accepted(self):
        existing = self.root / 'known.bin'; existing.write_bytes(b'fixture')
        self.assertEqual(_plain(existing), existing)
        self.assertEqual(_plain(self.root / 'new' / 'output.zip'), self.root / 'new' / 'output.zip')


class SBOMEvidenceBoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='MediaFlow-sbom-evidence-bound-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'inventory.json'

    def test_large_sbom_index_uses_its_own_bound_without_expanding_manifest_reads(self):
        from scripts.release_pipeline import document
        from scripts.publish_release import PublishError
        value = dict(release_files=[], evidence='x' * (2 * 1024 * 1024))
        self.path.write_bytes(json.dumps(value).encode('utf-8'))
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(PublishError):
            document(self.path)
        self.assertEqual(document(self.path, maximum=32 * 1024 * 1024), value)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_sbom_bound_remains_finite_and_strict_json_validation_is_preserved(self):
        from scripts.release_pipeline import document
        from scripts.publish_release import PublishError
        for invalid in (b'{"duplicate":1,"duplicate":2}', b'{"value":NaN}', b'[]'):
            with self.subTest(kind=invalid):
                self.path.write_bytes(invalid)
                with self.assertRaises(PublishError):
                    document(self.path, maximum=32 * 1024 * 1024)
        with self.path.open('wb') as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        with self.assertRaises(PublishError):
            document(self.path, maximum=32 * 1024 * 1024)
