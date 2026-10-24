"""Independent schema boundaries using minimal documents, not packaged builds.

Run only in the pinned security-tools environment. These standard-format small
fixtures do not claim that a real EXE was built, executed or authenticated.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import validate_sbom as validation


class SBOMValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mediaflow-sbom-schema-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.bom = {
            'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
            'metadata': {
                'component': {'type': 'application', 'name': 'MediaFlow fixture',
                              'bom-ref': 'application:fixture'},
                'tools': {'components': [{'type': 'application', 'name': 'Fixture build tool',
                                          'bom-ref': 'build-tool:fixture'}]}},
            'components': [{'type': 'library', 'name': 'example-fixture', 'version': '1.0.0',
                            'bom-ref': 'pkg:pypi/example-fixture@1.0.0'}],
            'dependencies': [{'ref': 'application:fixture',
                              'dependsOn': ['pkg:pypi/example-fixture@1.0.0']}],
            'compositions': [{'aggregate': 'incomplete', 'assemblies': ['application:fixture']}]}
        self.inventory = {'schema_version': 1, 'synthetic_minimal_fixture': True,
                          'release_files': [], 'embedded_members': [], 'python_modules': []}
        self.github = {'repository': 'unit-owner/MediaFlow', 'commit': 'b' * 40, 'run_id': '4242'}
        self.write_bound_documents()

    @staticmethod
    def canonical(document):
        return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')

    def write_bound_documents(self):
        """Recompute all declarations so rejection cannot be a stale-hash effect."""
        documents = {'mediaflow.cdx.json': self.canonical(self.bom),
                     'inventory.json': self.canonical(self.inventory)}
        self.hashes = {name: hashlib.sha256(data).hexdigest() for name, data in documents.items()}
        generation = {'passed': True, 'receipt_verified': True,
            'sbom_sha256': self.hashes['mediaflow.cdx.json'],
            'inventory_sha256': self.hashes['inventory.json'], 'github': self.github,
            'synthetic_minimal_fixture': True}
        documents['generation.json'] = self.canonical(generation)
        for name, data in documents.items():
            (self.directory / name).write_bytes(data)
        (self.directory / 'SHA256SUMS.txt').write_text(
            ''.join(digest + '  ' + name + '\n' for name, digest in self.hashes.items()), encoding='ascii')

    def test_valid_minimal_schema_and_all_bound_document_hashes_pass(self):
        report = validation.validate(self.directory)
        self.assertTrue(report['passed'])
        self.assertEqual(report['schema_errors'], 0)
        self.assertEqual(report['components'], 1)
        self.assertEqual(report['build_tool_components'], 1)
        self.assertEqual(report['github'], self.github)
        self.assertFalse(report['cryptographic_provenance_verified'])
        for name, digest in report['document_sha256'].items():
            self.assertEqual(digest, hashlib.sha256((self.directory / name).read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.directory / 'validation.json').read_text('utf-8')), report)

    def test_invalid_official_schema_is_refused_after_all_hashes_are_updated(self):
        self.bom['components'][0]['type'] = 'invalid-component-type'
        self.write_bound_documents()
        with self.assertRaisesRegex(validation.SBOMValidationError, 'Official CycloneDX 1.6 schema rejected'):
            validation.validate(self.directory)
        self.assertFalse((self.directory / 'validation.json').exists())

    def test_wrong_declared_spec_version_is_refused_after_all_hashes_are_updated(self):
        # The official schema accepts any string in specVersion. The independent
        # helper must enforce its declared 1.6-only policy explicitly as well.
        self.bom['specVersion'] = '0.0'
        self.write_bound_documents()
        with self.assertRaisesRegex(validation.SBOMValidationError, 'CycloneDX 1.6'):
            validation.validate(self.directory)
        self.assertFalse((self.directory / 'validation.json').exists())

    def test_schema_valid_host_paths_are_refused_even_with_matching_hashes(self):
        for private_path in ('C:' + chr(92) + 'Users' + chr(92) + 'fixture-person' + chr(92) + 'build',
                             '/home/fixture-person/build'):
            with self.subTest(path_kind='Windows' if private_path.startswith('C:') else 'Unix'):
                self.bom['metadata']['properties'] = [{'name': 'fixture:source-location', 'value': private_path}]
                self.write_bound_documents()
                with self.assertRaisesRegex(validation.SBOMValidationError, 'host path or secret marker') as raised:
                    validation.validate(self.directory)
                self.assertNotIn(private_path, str(raised.exception))
                self.assertFalse((self.directory / 'validation.json').exists())

    def test_existing_validation_evidence_is_never_overwritten(self):
        validation.validate(self.directory)
        path = self.directory / 'validation.json'
        original = path.read_bytes()
        self.bom['components'][0]['type'] = 'invalid-component-type'
        self.write_bound_documents()
        with self.assertRaisesRegex(validation.SBOMValidationError, 'Existing SBOM validation evidence is preserved'):
            validation.validate(self.directory)
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
