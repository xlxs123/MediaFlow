"""Publisher output round-trips through the real update importer and deployer."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from core.update_manifest import REQUIRED_FILES, UpdateValidationError, validate_manifest
from core.update_manager import UpdateManager
from scripts.package_update import PackageError, create_package


def proofs(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


class UpdatePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-update-package-test-')
        self.root = Path(self.temporary.name)
        self.release = self.root / 'release'
        self.output = self.root / 'MediaFlow-local.zip'
        self.stamp = {'app_version': '1.9.0', 'build_id': 'package-test-19',
                      'source_sha256': 'a' * 64, 'packaged': True}
        for name in REQUIRED_FILES:
            path = self.release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'MZ local package fixture: ' + name.encode())
        self.write_stamp()
        (self.release / 'README.md').write_text('本地测试发布说明', encoding='utf-8')

    def tearDown(self):
        self.temporary.cleanup()

    def write_stamp(self):
        (self.release / 'build-info.json').write_text(json.dumps(self.stamp), encoding='utf-8')

    def test_real_zip_has_exact_inventory_hashes_and_external_unsigned_receipt(self):
        before = proofs(self.release)
        result = create_package(self.release, self.output, release_notes='实际本地发布说明')
        self.assertEqual(proofs(self.release), before)
        self.assertEqual(result['version'], self.stamp['app_version'])
        self.assertEqual(result['build_id'], self.stamp['build_id'])
        self.assertFalse(result['authenticated'])
        self.assertIn('未经过发布者身份认证', result['authentication'])
        self.assertEqual(result['size'], self.output.stat().st_size)
        self.assertEqual(result['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        receipt = json.loads(Path(result['metadata_path']).read_text(encoding='utf-8'))
        self.assertNotIn('url', receipt)
        self.assertNotIn('signature', receipt)
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read('release.json'))
            self.assertFalse(validate_manifest(manifest)['authenticated'])
            self.assertEqual(set(archive.namelist()), set(before) | {'release.json'})
            self.assertNotIn('package', manifest)
            for item in manifest['files']:
                self.assertEqual(hashlib.sha256(archive.read(item['path'])).hexdigest(), item['sha256'])
                self.assertEqual(archive.getinfo(item['path']).file_size, item['size'])
        self.assertFalse(list(self.root.glob('.mediaflow-package*')))

    def test_real_update_manager_imports_and_deploys_without_touching_original_or_data(self):
        result = create_package(self.release, self.output)
        data = self.root / 'user-data'
        data.mkdir()
        (data / 'config.json').write_bytes(b'{"preserve":true}')
        (data / 'BrowserProfile').mkdir()
        (data / 'BrowserProfile/opaque.dat').write_bytes(b'keep existing profile')
        old_release = proofs(self.release)
        old_data = proofs(data)
        manager = UpdateManager(state_dir=self.root / 'Updates', release_root=self.release,
            current_identity=self.stamp, current_data_dir=data, health_data_root=self.root / 'health-data')
        preview = manager.prepare_local(self.output)
        self.assertEqual(preview['version'], '1.9.0')
        self.assertFalse(preview['authenticated'])
        self.assertEqual(preview['package_sha256'], result['sha256'])
        deployed = manager.deploy(preview['id'], confirmed=True, allow_unsigned=True)
        target = Path(deployed['target_path'])
        self.assertTrue((target / 'MediaFlow.exe').is_file())
        self.assertTrue((target / 'tools/ffprobe.exe').is_file())
        self.assertEqual({key: value for key, value in proofs(target).items() if key != 'release.json'}, old_release)
        self.assertEqual(old_release, proofs(self.release))
        self.assertEqual(old_data, proofs(data))

    def test_versions_are_read_from_real_build_stamp_without_fixed_new_version(self):
        self.stamp['app_version'] = '1.10.0rc2+local.1'
        self.stamp['build_id'] = 'actual-other-build'
        self.write_stamp()
        result = create_package(self.release, self.output)
        self.assertEqual(result['version'], '1.10.0rc2+local.1')
        self.assertEqual(result['build_id'], 'actual-other-build')

    def test_existing_zip_or_receipt_is_never_overwritten(self):
        for target in (self.output, self.output.with_name(self.output.name + '.metadata.json')):
            with self.subTest(target=target.name):
                target.write_bytes(b'preserve existing artifact')
                with self.assertRaisesRegex(PackageError, '已存在'):
                    create_package(self.release, self.output)
                self.assertEqual(target.read_bytes(), b'preserve existing artifact')
                target.unlink()

    def test_zip_inside_release_and_parent_escape_alias_are_rejected(self):
        before = proofs(self.release)
        for path in (self.release / 'output.zip',
                     self.root / 'release' / '..' / 'release' / 'output.zip'):
            with self.subTest(path=path), self.assertRaisesRegex(PackageError, '发布目录之外'):
                create_package(self.release, path)
        self.assertEqual(before, proofs(self.release))

    def test_user_data_secrets_unexpected_executables_and_existing_inner_manifest_are_rejected(self):
        for relative in ('config.json', 'tasks.json', 'private-key.txt', 'unexpected.exe', 'release.json'):
            path = self.release / relative
            path.write_bytes(b'not a release asset')
            before = proofs(self.release)
            with self.subTest(path=relative), self.assertRaises(UpdateValidationError):
                create_package(self.release, self.output)
            self.assertEqual(before, proofs(self.release))
            self.assertFalse(self.output.exists())
            path.unlink()

    def test_even_empty_user_profile_directory_is_rejected(self):
        (self.release / 'BrowserProfile').mkdir()
        with self.assertRaisesRegex(PackageError, '不允许的目录'):
            create_package(self.release, self.output)
        self.assertFalse(self.output.exists())

    def test_hard_linked_release_files_are_rejected(self):
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'original outside file')
        os.link(outside, self.release / 'linked.txt')
        with self.assertRaisesRegex(PackageError, '硬链接'):
            create_package(self.release, self.output)
        self.assertEqual(outside.read_bytes(), b'original outside file')

    def test_missing_tools_and_nonpackaged_or_invalid_identity_fail(self):
        (self.release / 'tools/ffprobe.exe').unlink()
        with self.assertRaisesRegex(UpdateValidationError, '必需'):
            create_package(self.release, self.output)
        (self.release / 'tools/ffprobe.exe').write_bytes(b'MZ')
        for key, value in [('packaged', False), ('source_sha256', 'wrong'),
                           ('app_version', 'v1.9.0'), ('build_id', '../outside')]:
            original = self.stamp[key]
            self.stamp[key] = value
            self.write_stamp()
            with self.subTest(key=key), self.assertRaises((PackageError, UpdateValidationError)):
                create_package(self.release, self.output)
            self.stamp[key] = original
        self.assertFalse(self.output.exists())

    def test_documentation_and_licenses_are_included_without_mutating_them(self):
        license_path = self.release / 'licenses/component/LICENSE.txt'
        license_path.parent.mkdir(parents=True)
        license_path.write_text('license text', encoding='utf-8')
        (self.release / 'verification.json').write_text('{"passed":true}', encoding='utf-8')
        result = create_package(self.release, self.output)
        self.assertIn('licenses/component/LICENSE.txt', {item['path'] for item in result['manifest']['files']})
        self.assertIn('verification.json', {item['path'] for item in result['manifest']['files']})

    def test_command_line_runs_outside_project_working_directory(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/package_update.py'
        outcome = subprocess.run([sys.executable, '-B', str(script), '--release-dir', str(self.release),
                                  '--output', str(self.output), '--notes', '本地发布包'],
                                 cwd=self.root, capture_output=True, timeout=30)
        self.assertEqual(outcome.returncode, 0, outcome.stderr.decode(errors='replace'))
        self.assertTrue(self.output.is_file())
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(json.loads(archive.read('release.json'))['release_notes'], '本地发布包')


if __name__ == '__main__':
    unittest.main()
