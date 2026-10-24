"""Real synthetic files test CI evidence binding, not native EXE acceptance."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import ci_build_receipt as receipts
from scripts.check_release_structure import ReleaseStructureError, check_release
from scripts.package_update import PackageError
from core.update_manifest import UpdateValidationError
from tests import test_release_structure as fixtures


class CIBuildReceiptTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing complete structure fixture instead of inventing a
        # second portable-release schema. It contains synthetic PE headers only.
        self.fixture = fixtures.ReleaseStructureTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.release = self.fixture.root, self.fixture.release
        self.output = self.root / 'build-attestation.json'
        self.results_path = self.root / 'group-results.json'
        self.structure_path = self.root / 'release-structure.json'
        self.source_sha256 = self.fixture.stamp['source_sha256']
        groups = [dict(group=name, passed=True, source_unchanged=True,
            source_sha256=self.source_sha256, tests=3, failures=0, errors=0,
            skipped=0, skips=[], unexpected_skips=[], exit_code=0)
            for name in receipts.REQUIRED_GROUPS]
        self.results = dict(passed=True, source_unchanged=True, source_sha256=self.source_sha256,
            groups=groups, tests=9, failures=0, errors=0, skipped=0)
        # Raw reports can contain private diagnostic fields. The public receipt
        # must retain only fixed normalized counters and document digests.
        self.results['private_diagnostic'] = str(self.root / 'private-unit-context')
        self.save(self.results_path, self.results)
        self.save(self.structure_path, check_release(self.release, '2.1.0', runtime_identity=self.fixture.runtime))
        self.arguments = dict(release_dir=self.release, output_path=self.output, expected_version='2.1.0',
            runtime_identity=self.fixture.runtime, test_results=self.results_path,
            structure_report=self.structure_path, repository='unit-owner/MediaFlow',
            commit='b' * 40, run_id='4242')

    @staticmethod
    def save(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def create(self, **changes):
        return receipts.create_receipt(**dict(self.arguments, **changes))

    def verify(self, **changes):
        return receipts.verify_receipt(self.output, self.release, **changes)

    def test_complete_file_binding_is_public_and_never_authenticates_github_or_publisher(self):
        created = self.create()
        verified = self.verify(expected_version='2.1.0', expected_commit='b' * 40,
            expected_repository='unit-owner/MediaFlow', expected_run_id=4242,
            expected_source_sha256=self.source_sha256)
        self.assertEqual(created, verified)
        value = json.loads(self.output.read_text('utf-8'))
        self.assertEqual(len(value['files']), len([p for p in self.release.rglob('*') if p.is_file()]))
        self.assertEqual([p['path'] for p in value['files']], sorted(p['path'] for p in value['files']))
        self.assertNotIn(str(self.root), self.output.read_text('utf-8'))
        self.assertNotIn('private_diagnostic', self.output.read_text('utf-8'))
        self.assertTrue(verified['runtime_identity_bound'])
        for flag in ('signed', 'publisher_authenticated', 'ci_provenance_verified', 'native_exe_executed'):
            self.assertFalse(verified[flag])
        self.assertEqual(verified['tests']['tests'], 9)

    def test_changed_release_bytes_are_rejected_even_with_same_size_and_mtime(self):
        self.create()
        path = self.release / 'README.md'
        info = path.stat()
        original = path.read_bytes()
        path.write_bytes(original[:-1] + b'.')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(path.stat().st_size, len(original))
        with self.assertRaisesRegex(receipts.CIReceiptError, '文件清单、字节'):
            self.verify()

    def test_exe_resource_or_complete_inventory_changes_never_verify(self):
        self.create()
        for name in ('MediaFlow.exe', 'tools/webview2/NOTICE.txt', 'licenses/cffi-unit.dist-info/LICENSE'):
            with self.subTest(name=name):
                path = self.release / name
                original = path.read_bytes()
                path.write_bytes(original + b'\nchanged unit bytes')
                with self.assertRaises((receipts.CIReceiptError, ReleaseStructureError, PackageError, UpdateValidationError)):
                    self.verify()
                path.write_bytes(original)
        (self.release / 'FILES.sha256').write_text('New unrecorded file.\n', encoding='utf-8')
        with self.assertRaisesRegex(receipts.CIReceiptError, '文件清单、字节'):
            self.verify()

    def test_failed_incomplete_or_inconsistent_tests_never_create_a_receipt(self):
        mutations = [lambda r: r.update(passed=False),
            lambda r: r.update(source_unchanged=False),
            lambda r: r.update(source_sha256='c' * 64),
            lambda r: r['groups'][0].update(exit_code=1),
            lambda r: (r.update(failures=1), r['groups'][0].update(failures=1)),
            lambda r: (r.update(errors=1), r['groups'][0].update(errors=1)),
            lambda r: r['groups'].pop(),
            lambda r: r['groups'].__setitem__(2, deepcopy(r['groups'][0])),
            lambda r: r.update(tests=8),
            lambda r: r['groups'][0].update(tests=0),
            lambda r: r['groups'][1].update(source_unchanged=False)]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                value = deepcopy(self.results)
                mutate(value)
                self.save(self.results_path, value)
                with patch.object(receipts, '_snapshot', side_effect=AssertionError('Invalid tests must fail before a complete release scan')), \
                        self.assertRaises(receipts.CIReceiptError):
                    self.create()
                self.assertFalse(self.output.exists())

    def test_unapproved_or_unaccounted_skips_never_create_a_receipt(self):
        choices = [({'test': 'tests.unit.capability', 'reason': 'Skipped verification'}, []),
            (deepcopy(receipts._ALLOWED_SKIP), [{'test': 'unexpected', 'reason': 'unit'}])]
        for skip, unexpected in choices:
            with self.subTest(skip=skip['test']):
                value = deepcopy(self.results)
                value.update(skipped=1)
                value['groups'][0].update(skipped=1, skips=[skip], unexpected_skips=unexpected)
                self.save(self.results_path, value)
                with patch.object(receipts, '_snapshot', side_effect=AssertionError('Unauthorized skips must fail before a complete release scan')), \
                        self.assertRaisesRegex(receipts.CIReceiptError, '跳过|skip'):
                    self.create()
                self.assertFalse(self.output.exists())
        self.results['groups'][0]['skipped'] = 1
        self.save(self.results_path, self.results)
        with patch.object(receipts, '_snapshot', side_effect=AssertionError('Unaccounted skips must fail before a complete release scan')), \
                self.assertRaises(receipts.CIReceiptError):
            self.create()
        self.assertFalse(self.output.exists())

    def test_only_the_existing_explicit_symlink_permission_skip_is_accepted(self):
        self.results.update(skipped=1)
        self.results['groups'][0].update(skipped=1, skips=[deepcopy(receipts._ALLOWED_SKIP)])
        self.save(self.results_path, self.results)
        self.assertEqual(self.create()['tests']['skipped'], 1)
        self.assertEqual(self.verify()['tests']['groups'][0]['skips'], [receipts._ALLOWED_SKIP])

    def test_bad_runtime_identity_or_stale_structure_report_prevents_creation(self):
        original = json.loads(self.fixture.runtime.read_text('utf-8'))
        self.save(self.fixture.runtime, dict(original, build_id='different-unit-build'))
        with self.assertRaises(ReleaseStructureError):
            self.create()
        self.assertFalse(self.output.exists())
        self.save(self.fixture.runtime, original)
        structure = json.loads(self.structure_path.read_text('utf-8'))
        self.save(self.structure_path, dict(structure, runtime_identity_verified=False))
        with self.assertRaisesRegex(receipts.CIReceiptError, '结构检查报告'):
            self.create()
        self.assertFalse(self.output.exists())

    def test_changed_file_during_creation_cannot_produce_an_attestation(self):
        real_check = receipts.check_release
        attempted = []
        def change_after_structure(*args, **kwargs):
            result = real_check(*args, **kwargs)
            attempted.append(True)
            path = self.release / 'README.md'
            path.write_bytes(path.read_bytes() + b'changed during snapshot')
            return result
        # Windows denies the change while frozen handles are open; on POSIX the
        # second full-byte comparison detects it. Neither path may create output.
        with patch.object(receipts, 'check_release', side_effect=change_after_structure), \
                self.assertRaises((OSError, receipts.CIReceiptError)):
            self.create()
        self.assertEqual(attempted, [True])
        self.assertFalse(self.output.exists())

    def test_independent_expected_git_version_and_source_bindings_are_mandatory_when_supplied(self):
        self.create()
        for changes in ({'expected_commit': 'c' * 40}, {'expected_repository': 'unit-owner/Other'},
                        {'expected_run_id': 4243}, {'expected_source_sha256': 'c' * 64},
                        {'expected_version': '2.1.1'}):
            with self.subTest(changes=changes), self.assertRaises((receipts.CIReceiptError, ReleaseStructureError)):
                self.verify(**changes)

    def test_local_json_cannot_upgrade_its_own_authentication_or_add_private_fields(self):
        self.create()
        original = json.loads(self.output.read_text('utf-8'))
        for changes in ({'ci_provenance_verified': True}, {'signed': True},
                        {'authorization': 'UNIT-NOT-A-REAL-CREDENTIAL'}):
            with self.subTest(fields=list(changes)):
                self.save(self.output, dict(original, **changes))
                with self.assertRaises(receipts.CIReceiptError) as error:
                    self.verify()
                self.assertNotIn('UNIT-NOT-A-REAL-CREDENTIAL', str(error.exception))
        changed = deepcopy(original)
        changed['github']['run_id'] = '9999'
        self.save(self.output, changed)
        self.assertFalse(self.verify()['ci_provenance_verified'])
        with self.assertRaises(receipts.CIReceiptError):
            self.verify(expected_run_id=4242)

    def test_existing_evidence_and_release_files_are_never_overwritten(self):
        self.output.write_bytes(b'Existing unit evidence.')
        with self.assertRaises(receipts.CIReceiptError):
            self.create()
        self.assertEqual(self.output.read_bytes(), b'Existing unit evidence.')
        output = self.release / 'build-attestation.json'
        with self.assertRaises(receipts.CIReceiptError):
            self.create(output_path=output)
        self.assertFalse(output.exists())

    def test_cli_create_verify_and_failed_verification_use_sanitized_output(self):
        arguments = ['create', '--release-dir', str(self.release), '--output', str(self.output),
            '--expected-version', '2.1.0', '--runtime-identity', str(self.fixture.runtime),
            '--test-results', str(self.results_path), '--structure-report', str(self.structure_path),
            '--repository', 'unit-owner/MediaFlow', '--commit', 'b' * 40, '--run-id', '4242']
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(receipts.main(arguments), 0)
            self.assertEqual(receipts.main(['verify', '--receipt', str(self.output),
                '--release-dir', str(self.release), '--expected-run-id', '4242']), 0)
            self.assertEqual(receipts.main(['verify', '--receipt', str(self.output),
                '--release-dir', str(self.release), '--expected-repository',
                'https://unit:UNIT-PRIVATE-DIAGNOSTIC@invalid/']), 1)
        self.assertNotIn(str(self.root), output.getvalue() + errors.getvalue())
        self.assertNotIn('UNIT-PRIVATE-DIAGNOSTIC', errors.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 2)


if __name__ == '__main__':
    unittest.main()
