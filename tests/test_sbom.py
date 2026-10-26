"""Real synthetic PyInstaller archives, file hashes and wheel RECORD fixtures.

These exercise inventory binding, never native EXE execution or production
provenance. They reuse the existing complete release/receipt test schema.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter

from scripts import generate_sbom as sbom
from scripts.check_release_structure import check_release
from tests import test_ci_build_receipt as receipt_fixtures
from tests.test_release_structure import pe_bytes


class SBOMTests(unittest.TestCase):
    def setUp(self):
        self.fixture = receipt_fixtures.CIBuildReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.release = self.fixture.release
        self.build = self.root / 'build'
        self.build.mkdir()
        self.site = self.root / 'build-environment/site-packages'
        package = self.site / 'example_pkg'
        package.mkdir(parents=True)
        self.module = package / '__init__.py'
        self.module.write_bytes(b'VALUE = 42\n')
        self.resource = package / 'resource.bin'
        self.resource.write_bytes(b'Bundled data from the installed wheel.\n')
        dist_info = self.site / 'example_pkg-1.2.3.dist-info'
        dist_info.mkdir()
        self.metadata = dist_info / 'METADATA'
        self.metadata.write_bytes(b'Metadata-Version: 2.1\nName: example-pkg\nVersion: 1.2.3\n')
        records = []
        for path in (self.module, self.resource, self.metadata):
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            records.append(path.relative_to(self.site).as_posix() + ',sha256=' + digest + ',' + str(len(data)))
        records.append('example_pkg-1.2.3.dist-info/RECORD,,')
        self.record = dist_info / 'RECORD'
        self.record.write_text('\n'.join(records) + '\n', encoding='utf-8')
        unused = self.site / 'not_bundled-9.9.dist-info'
        unused.mkdir()
        (unused / 'METADATA').write_bytes(b'Metadata-Version: 2.1\nName: not-bundled\nVersion: 9.9\n')
        (unused / 'RECORD').write_bytes(b'not_bundled.py,,\n')
        self.pyz_entries = [('example_pkg', str(self.module), 'PYMODULE')]
        self.pyz_path = self.build / 'PYZ-00.pyz'
        code = compile(self.module.read_bytes(), str(self.module), 'exec', dont_inherit=True)
        ZlibArchiveWriter(str(self.pyz_path), self.pyz_entries, {'example_pkg': code})
        self.runtime = self.build / ('python' + str(sys.version_info.major) + str(sys.version_info.minor) + '.dll')
        self.runtime.write_bytes(pe_bytes() + b'Synthetic Python runtime bytes for unit testing.')
        self.pkg_entries = [('PYZ-00.pyz', str(self.pyz_path), 'PYZ'),
            (self.runtime.name, str(self.runtime), 'BINARY'),
            ('example_pkg/resource.bin', str(self.resource), 'DATA')]
        self.analysis_path = self.build / 'Analysis-00.toc'
        self.analysis_path.write_text(repr(([], sys.version, self.pyz_entries, self.pkg_entries)), encoding='utf-8')
        self.pyz_toc = self.build / 'PYZ-00.toc'
        self.pyz_toc.write_text(repr((str(self.pyz_path), self.pyz_entries)), encoding='utf-8')
        self.pkg_toc = self.build / 'PKG-00.toc'
        self.pkg_toc.write_text(repr(([], self.pkg_entries)), encoding='utf-8')
        self.package_path = self.build / 'MediaFlow.pkg'
        CArchiveWriter(str(self.package_path), [
            ('PYZ.pyz', str(self.pyz_path), False, 'z'),
            (self.runtime.name, str(self.runtime), True, 'b'),
            ('example_pkg/resource.bin', str(self.resource), True, 'x')], self.runtime.name)
        (self.release / 'MediaFlow.exe').write_bytes(pe_bytes() + self.package_path.read_bytes())
        self.fixture.save(self.fixture.structure_path,
            check_release(self.release, '2.1.0', runtime_identity=self.fixture.fixture.runtime))
        self.fixture.create()
        self.output = self.root / 'new-sbom'

    def generate(self, **changes):
        args = dict(release_dir=self.release, build_dir=self.build,
                    receipt_path=self.fixture.output, output_dir=self.output)
        args.update(changes)
        return sbom.generate(**args)

    def test_actual_archives_and_record_ownership_exclude_unbundled_environment(self):
        result = self.generate()
        self.assertTrue(result['passed'])
        self.assertEqual(result['python_modules'], 1)
        self.assertEqual(result['embedded_members'], 3)
        self.assertEqual(result['packaged_python_distributions'], 1)
        bom = json.loads((self.output / 'mediaflow.cdx.json').read_text('utf-8'))
        packages = {item['name']: item for item in bom['components']}
        self.assertEqual(packages['example-pkg']['version'], '1.2.3')
        self.assertEqual(packages['CPython']['version'], sys.version.split()[0])
        self.assertNotIn('not-bundled', packages)
        self.assertNotIn('PyInstaller', packages)
        self.assertIn('PyInstaller', [item['name'] for item in bom['metadata']['tools']['components']])
        self.assertEqual(bom['compositions'][0]['aggregate'], 'incomplete')
        properties = {item['name']: item['value'] for item in bom['metadata']['properties']}
        self.assertEqual(properties['mediaflow:receipt-authenticated'], 'False')
        self.assertEqual(properties['mediaflow:commit'], 'b' * 40)
        self.assertEqual(properties['mediaflow:workflow-run-id'], '4242')
        self.assertFalse(result['provenance_attestation_verified'])
        self.assertEqual(result['schema_validation'], 'pending-independent-official-schema-validation')
        for path in self.output.iterdir():
            self.assertNotIn(str(self.root), path.read_text('utf-8'))
        self.assertEqual(result['sbom_sha256'], hashlib.sha256((self.output / 'mediaflow.cdx.json').read_bytes()).hexdigest())
        inventory = json.loads((self.output / 'inventory.json').read_text('utf-8'))
        self.assertEqual(inventory['schema_version'], 2)
        native_objects = {item['ref']:item for item in inventory['native_inventory']['objects']}
        self.assertEqual(native_objects['native-file:MediaFlow.exe']['sha256'],
                         hashlib.sha256((self.release / 'MediaFlow.exe').read_bytes()).hexdigest())
        runtime_ref = 'native-file:MediaFlow.exe!/' + self.runtime.name
        self.assertIn(runtime_ref, native_objects)
        dependencies = {item['ref']:item['dependsOn'] for item in bom['dependencies']}
        self.assertIn(runtime_ref, dependencies['runtime:cpython'])
        self.assertEqual(inventory['python_modules'][0]['source_sha256'], hashlib.sha256(self.module.read_bytes()).hexdigest())
        self.assertEqual(inventory['python_modules'][0]['distributions'], ['example-pkg@1.2.3'])

    def test_changed_archive_bytes_or_release_files_fail_receipt_binding(self):
        for path in (self.release / 'MediaFlow.exe', self.release / 'README.md'):
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            try:
                with self.assertRaises(Exception):
                    self.generate()
                self.assertFalse(self.output.exists())
            finally:
                path.write_bytes(original)

    def test_stale_or_forged_toc_cannot_invent_module_or_native_members(self):
        self.pyz_toc.write_text(repr((str(self.pyz_path), self.pyz_entries + [
            ('invented_module', str(self.module), 'PYMODULE')])), encoding='utf-8')
        with self.assertRaisesRegex(sbom.SBOMError, 'actual EXE module'):
            self.generate()
        self.pyz_toc.write_text(repr((str(self.pyz_path), self.pyz_entries)), encoding='utf-8')
        self.pkg_toc.write_text(repr(([], self.pkg_entries + [
            ('invented.dll', str(self.runtime), 'BINARY')])), encoding='utf-8')
        with self.assertRaisesRegex(sbom.SBOMError, 'actual EXE archive'):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_changed_python_source_is_detected_by_actual_packaged_bytecode(self):
        self.module.write_bytes(b'VALUE = 43\n')
        with self.assertRaisesRegex(sbom.SBOMError, 'bytecode differs'):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_changed_native_build_input_is_detected_by_actual_archive_bytes(self):
        self.runtime.write_bytes(self.runtime.read_bytes() + b'changed')
        with self.assertRaisesRegex(sbom.SBOMError, 'archive member differs'):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_wheel_record_tampering_cannot_inherit_a_distribution_version(self):
        value = self.record.read_text('utf-8').replace('sha256=', 'sha256=' + 'wrong', 1)
        self.record.write_text(value, encoding='utf-8')
        with self.assertRaisesRegex(sbom.SBOMError, 'wheel RECORD'):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_shared_namespace_files_preserve_every_hash_verified_distribution_owner(self):
        shared = self.site / 'namespace_peer-1.2.3.dist-info'
        shared.mkdir()
        meta = shared / 'METADATA'
        meta.write_bytes(b'Metadata-Version: 2.1\nName: namespace-peer\nVersion: 1.2.3\n')
        rows = []
        for path in (self.module, meta):
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            rows.append(path.relative_to(self.site).as_posix() + ',sha256=' + digest + ',' + str(len(data)))
        (shared / 'RECORD').write_text('\n'.join(rows) + '\nnamespace_peer-1.2.3.dist-info/RECORD,,\n', encoding='utf-8')
        self.generate()
        inventory = json.loads((self.output / 'inventory.json').read_text('utf-8'))
        self.assertEqual(inventory['python_modules'][0]['distributions'],
                         ['example-pkg@1.2.3', 'namespace-peer@1.2.3'])

    def test_source_and_record_change_between_archive_comparison_and_attribution_is_refused(self):
        original = sbom._distributions

        def concurrent_change(entries, source_hashes):
            self.module.write_bytes(b'VALUE = 44\n')
            digest = base64.urlsafe_b64encode(hashlib.sha256(self.module.read_bytes()).digest()).rstrip(b'=').decode()
            lines = self.record.read_text('utf-8').splitlines()
            lines[0] = 'example_pkg/__init__.py,sha256=' + digest + ',' + str(self.module.stat().st_size)
            self.record.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            return original(entries, source_hashes)

        with patch.object(sbom, '_distributions', side_effect=concurrent_change), \
                self.assertRaisesRegex(sbom.SBOMError, 'changed after actual archive comparison'):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_pyinstaller_bootstrap_support_is_not_the_full_runtime_build_package(self):
        package = self.site / 'PyInstaller'
        package.mkdir()
        source = package / 'pyi_rth_fixture.py'
        source.write_bytes(b'pass\n')
        info = self.site / 'PyInstaller-1.2.3.dist-info'
        info.mkdir()
        meta = info / 'METADATA'
        meta.write_bytes(b'Metadata-Version: 2.1\nName: PyInstaller\nVersion: 1.2.3\n')
        records = []
        for path in (source, meta):
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            records.append(path.relative_to(self.site).as_posix() + ',sha256=' + digest + ',' + str(len(data)))
        (info / 'RECORD').write_text('\n'.join(records) + '\nPyInstaller-1.2.3.dist-info/RECORD,,\n', encoding='utf-8')
        self.pkg_entries.append(('pyi_rth_fixture', str(source), 'PYSOURCE'))
        self.pkg_toc.write_text(repr(([], self.pkg_entries)), encoding='utf-8')
        CArchiveWriter(str(self.package_path), [
            ('PYZ.pyz', str(self.pyz_path), False, 'z'),
            (self.runtime.name, str(self.runtime), True, 'b'),
            ('example_pkg/resource.bin', str(self.resource), True, 'x'),
            ('pyi_rth_fixture', str(source), True, 's')], self.runtime.name)
        (self.release / 'MediaFlow.exe').write_bytes(pe_bytes() + self.package_path.read_bytes())
        self.fixture.output.unlink()
        self.fixture.save(self.fixture.structure_path,
            check_release(self.release, '2.1.0', runtime_identity=self.fixture.fixture.runtime))
        self.fixture.create()
        result = self.generate()
        bom = json.loads((self.output / 'mediaflow.cdx.json').read_text('utf-8'))
        self.assertNotIn('pyinstaller', [item['name'].lower() for item in bom['components']])
        support = next(item for item in bom['components'] if item['name'] == 'pyi_rth_fixture')
        self.assertEqual(support['type'], 'file')
        self.assertEqual(result['bundled_build_support_members'], 1)

    def test_literal_tocs_are_parsed_without_executing_code(self):
        self.analysis_path.write_text("__import__('sys').exit(99)", encoding='utf-8')
        with self.assertRaisesRegex(sbom.SBOMError, 'literal TOC'):
            self.generate()
        self.assertFalse(self.output.exists())

    def test_output_is_new_external_and_unknown_versions_remain_unknown(self):
        for output in (self.release / 'new-sbom', self.build / 'new-sbom', self.release):
            with self.subTest(output=output.name), self.assertRaises(sbom.SBOMError):
                self.generate(output_dir=output)
        self.generate()
        with self.assertRaises(sbom.SBOMError):
            self.generate()
        bom = json.loads((self.output / 'mediaflow.cdx.json').read_text('utf-8'))
        tool = next(item for item in bom['components'] if item['name'] == 'ffmpeg.exe')
        self.assertNotIn('version', tool)

    def test_runtime_versions_must_match_original_python_environment(self):
        self.analysis_path.write_text(repr(([], '0.0.0 (Synthetic incompatible Python)', self.pyz_entries)), encoding='utf-8')
        with self.assertRaisesRegex(sbom.SBOMError, 'exact Python build environment'):
            self.generate()

    def test_public_paths_and_unsafe_archive_names_are_rejected(self):
        for value in ('../outside.dll', '/outside.dll', 'C:' + chr(92) + 'private.dll'):
            with self.subTest(value=value), self.assertRaises(sbom.SBOMError):
                sbom._relative(value)
        with self.assertRaisesRegex(sbom.SBOMError, 'absolute paths'):
            sbom.validate_public_document({'path': str(self.root)})

    def test_cli_errors_are_fixed_and_do_not_leak_private_build_paths(self):
        self.pyz_toc.unlink()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = sbom.main(['--release-dir', str(self.release), '--build-dir', str(self.build),
                '--receipt', str(self.fixture.output), '--output', str(self.output)])
        self.assertEqual(result, 1)
        error = json.loads(errors.getvalue())
        self.assertFalse(error['passed'])
        self.assertNotIn(str(self.root), errors.getvalue())
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
