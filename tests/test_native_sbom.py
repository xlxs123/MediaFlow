"""Real bounded PE/CArchive fixtures and independent SBOM closure mutations.

Synthetic binaries are never executed, signed or described as real releases.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from PyInstaller.archive.writers import CArchiveWriter

from scripts import native_sbom as native
from scripts.validate_sbom import validate_documents, SBOMValidationError


def pe_fixture(imports=(), delayed=()):
    """A real PE32+ header, section and import/delay-import tables for pefile."""
    data = bytearray(0x800)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, 1, 0, 0, 0, 240, 0x22)
    opt = 0x98
    struct.pack_into('<H', data, opt, 0x20b)
    struct.pack_into('<Q', data, opt + 24, 0x140000000)
    struct.pack_into('<II', data, opt + 32, 0x1000, 0x200)
    struct.pack_into('<HH', data, opt + 40, 6, 0)
    struct.pack_into('<HH', data, opt + 48, 6, 0)
    struct.pack_into('<II', data, opt + 56, 0x2000, 0x200)
    struct.pack_into('<H', data, opt + 68, 3)
    struct.pack_into('<I', data, opt + 108, 16)
    section = 0x188
    data[section:section + 8] = b'.rdata\0\0'
    struct.pack_into('<IIII', data, section + 8, 0x600, 0x1000, 0x600, 0x200)
    struct.pack_into('<I', data, section + 36, 0x40000040)
    def string(offset, value):
        raw = value.encode('ascii') + b'\0'
        data[offset:offset + len(raw)] = raw
    if imports:
        struct.pack_into('<II', data, opt + 112 + 8, 0x1000, (len(imports) + 1) * 20)
        for index, module in enumerate(imports):
            name = 0x400 + index * 64
            string(name, module)
            struct.pack_into('<IIIII', data, 0x200 + index * 20, 0x1300, 0, 0, name + 0xe00, 0x1320)
        struct.pack_into('<QQ', data, 0x500, 0x1380, 0)
        struct.pack_into('<QQ', data, 0x520, 0x1380, 0)
        string(0x582, 'fixture_function')
    if delayed:
        struct.pack_into('<II', data, opt + 112 + 13 * 8, 0x1100, (len(delayed) + 1) * 32)
        for index, module in enumerate(delayed):
            name = 0x440 + index * 64
            string(name, module)
            struct.pack_into('<IIIIIIII', data, 0x300 + index * 32, 1, name + 0xe00, 0, 0x1360, 0x1340, 0, 0, 0)
        struct.pack_into('<QQ', data, 0x540, 0x13c0, 0)
        struct.pack_into('<QQ', data, 0x560, 0x13c0, 0)
        string(0x5c2, 'delayed_fixture_function')
    return bytes(data)


class NativeSBOMTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mediaflow-native-sbom-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / 'release'
        self.release.mkdir()
        self.policy = json.loads((native.ROOT / 'security/sbom-policy.json').read_bytes())
        child = self.root / 'child.dll'
        child.write_bytes(pe_fixture(imports=('kernel32.dll',)))
        package = self.root / 'payload.pkg'
        CArchiveWriter(str(package), [('child.dll', str(child), True, 'b')], 'python-fixture.dll')
        (self.release / 'MediaFlow.exe').write_bytes(pe_fixture(imports=('kernel32.dll',), delayed=('helper.dll',)) + package.read_bytes())
        (self.release / 'helper.dll').write_bytes(pe_fixture(imports=('kernel32.dll',)))
        # A system-named shim really exists in the archive. Its actual bytes
        # must not be excluded merely because an OS prefix matches its name.
        self.files = self.bound_files()

    def bound_files(self):
        return [dict(path=p.relative_to(self.release).as_posix(), size=p.stat().st_size,
                     sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(self.release.rglob('*')) if p.is_file()]

    def discover(self):
        return native.discover_native(self.release, self.bound_files(), policy=self.policy)

    def documents(self):
        inventory = dict(schema_version=2, release_files=self.bound_files(), python_modules=[], distribution_metadata={})
        inventory['native_inventory'] = discovered = self.discover()
        inventory['embedded_members'] = [dict(row, distributions=[]) for row in discovered['containers'][0]['members']]
        components, edges = native.native_components(discovered)
        root = dict(type='application', name='MediaFlow fixture', **{'bom-ref':'application:fixture'},
                    hashes=[dict(alg='SHA-256', content=next(row['sha256'] for row in inventory['release_files'] if row['path'] == 'MediaFlow.exe'))])
        graph = {root['bom-ref']:set(c['bom-ref'] for c in components)}
        for edge in edges:
            graph.setdefault(edge['ref'], set()).update(set(edge['dependsOn']) - {edge['ref']})
        bom = dict(bomFormat='CycloneDX', specVersion='1.6', version=1,
            metadata=dict(component=root, tools=dict(components=[])), components=components,
            dependencies=[dict(ref=ref, dependsOn=sorted(targets)) for ref, targets in sorted(graph.items())],
            compositions=[dict(aggregate='incomplete', assemblies=[root['bom-ref']])])
        return bom, inventory

    def test_actual_import_delay_tables_and_embedded_content_have_closed_graph(self):
        bom, inventory = self.documents()
        actual = inventory['native_inventory']
        self.assertEqual(len(actual['objects']), 3)
        self.assertTrue(all(obj['pe']['status'] == 'parsed' for obj in actual['objects']))
        delayed = [edge for edge in actual['relations'] if edge['kind'] == 'delay-import']
        self.assertEqual(delayed[0]['targets'], ['native-file:helper.dll'])
        self.assertTrue(all(edge['resolution'] == 'external-system' for edge in actual['relations'] if edge['module'] == 'kernel32.dll'))
        report = validate_documents(bom, inventory, require_native=True)
        self.assertTrue(report['native_coverage_verified'])
        self.assertEqual(report['native_objects'], 3)
        self.assertEqual(report['embedded_native_files'], 1)
        self.assertEqual(report['static_dependency_enumeration'], 'incomplete')

    def test_changed_byte_hash_and_unsafe_named_native_are_rejected(self):
        files = self.bound_files()
        (self.release / 'helper.dll').write_bytes(pe_fixture() + b'changed')
        with self.assertRaisesRegex(native.NativeSBOMError, 'actual file bytes|Actual file bytes'):
            native.discover_native(self.release, files, policy=self.policy)
        (self.release / 'helper.dll').write_bytes(b'not PE')
        with self.assertRaisesRegex(native.NativeSBOMError, 'does not contain PE'):
            self.discover()

    def test_real_system_named_shim_is_distributed_and_same_directory_wins(self):
        (self.release / 'api-ms-win-fixture.dll').write_bytes(pe_fixture())
        (self.release / 'helper.dll').write_bytes(pe_fixture(imports=('api-ms-win-fixture.dll',)))
        inventory = self.discover()
        row = next(edge for edge in inventory['relations'] if edge['module'] == 'api-ms-win-fixture.dll')
        self.assertEqual(row['resolution'], 'distributed')
        self.assertEqual(row['targets'], ['native-file:api-ms-win-fixture.dll'])

    def test_cross_container_basename_is_not_falsely_associated(self):
        (self.release / 'helper.dll').write_bytes(pe_fixture(imports=('child.dll',)))
        inventory = self.discover()
        row = next(edge for edge in inventory['relations'] if edge['module'] == 'child.dll')
        self.assertEqual(row['resolution'], 'unresolved')
        self.assertEqual(row['targets'], [])

    def test_license_evidence_uses_exact_record_owner_not_neighbor_package(self):
        for package in ('My_Pkg-1.0.dist-info', 'My_Pkg_Addon-1.0.dist-info'):
            directory = self.release / 'licenses' / package
            directory.mkdir(parents=True)
            (directory / 'LICENSE').write_bytes(b'Synthetic license attribution fixture only.')
        initial = self.discover()
        members = [dict(item, distributions=['my-pkg@1.0']) for item in initial['containers'][0]['members']]
        result = native.discover_native(self.release, self.bound_files(), members, policy=self.policy)
        child = next(obj for obj in result['objects'] if obj['kind'] == 'embedded')
        self.assertEqual(child['license_evidence_paths'], ['licenses/My_Pkg-1.0.dist-info/LICENSE'])
        document = next(item for item in result['evidence_files'] if item['path'] == child['license_evidence_paths'][0])
        self.assertEqual(document['sha256'], hashlib.sha256((self.release / document['path']).read_bytes()).hexdigest())

    def test_same_directory_dependency_wins_over_other_same_name_files(self):
        for name in ('a', 'b'):
            directory = self.release / name
            directory.mkdir()
            (directory / 'same.dll').write_bytes(pe_fixture())
        (self.release / 'a' / 'consumer.dll').write_bytes(pe_fixture(imports=('same.dll',)))
        result = self.discover()
        edge = next(row for row in result['relations'] if row['module'] == 'same.dll')
        self.assertEqual(edge['resolution'], 'distributed')
        self.assertEqual(edge['targets'], ['native-file:a/same.dll'])

    def test_omitted_native_file_member_edge_or_forged_component_is_rejected(self):
        bom, inventory = self.documents()
        cases = []
        changed = deepcopy(inventory); changed['native_inventory']['objects'].pop(); cases.append((bom, changed))
        changed = deepcopy(inventory); changed['native_inventory']['relations'].pop(); cases.append((bom, changed))
        changed = deepcopy(inventory); changed['native_inventory']['containers'][0]['members'].pop(); cases.append((bom, changed))
        changed = deepcopy(inventory); changed['native_inventory']['gaps'].clear(); cases.append((bom, changed))
        changed = deepcopy(bom); changed['components'][0]['hashes'][0]['content'] = 'a' * 64; cases.append((changed, inventory))
        changed = deepcopy(bom); changed['dependencies'][0]['dependsOn'].append('not-an-existing-ref'); cases.append((changed, inventory))
        for changed_bom, changed_inventory in cases:
            with self.subTest(kind=len(cases)), self.assertRaises(SBOMValidationError):
                validate_documents(changed_bom, changed_inventory, require_native=True)

    def test_ambiguous_library_resolution_is_explicit_and_not_a_false_dependency(self):
        for directory in ('a', 'b'):
            folder = self.release / directory
            folder.mkdir()
            (folder / 'ambiguous.dll').write_bytes(pe_fixture())
        (self.release / 'helper.dll').write_bytes(pe_fixture(imports=('ambiguous.dll',)))
        result = self.discover()
        edge = next(e for e in result['relations'] if e['module'] == 'ambiguous.dll')
        self.assertEqual(edge['resolution'], 'ambiguous')
        self.assertEqual(len(edge['targets']), 2)
        _, dependencies = native.native_components(result)
        self.assertNotIn('native-file:helper.dll', {row['ref'] for row in dependencies})

    def test_unparsed_native_is_reported_but_cannot_claim_required_coverage(self):
        (self.release / 'helper.dll').write_bytes(b'MZinvalid-test-only')
        bom, inventory = self.documents()
        self.assertFalse(validate_documents(bom, inventory)['native_coverage_verified'])
        with self.assertRaisesRegex(SBOMValidationError, 'coverage is incomplete'):
            validate_documents(bom, inventory, require_native=True)

    def test_reported_static_library_versions_require_exact_reviewed_binary_hash(self):
        result = self.discover()
        self.assertTrue(all('reported_component_evidence' not in obj for obj in result['objects']))
        bom, inventory = self.documents()
        inventory['native_inventory']['objects'][0]['reported_component_evidence'] = dict(reported_linked_libraries=[dict(name='libavcodec', version='999.1')])
        with self.assertRaisesRegex(SBOMValidationError, 'exact reviewed binary'):
            validate_documents(bom, inventory, require_native=True)

    def test_generation_claim_cannot_invent_native_coverage_after_rehashing(self):
        bom, inventory = self.documents()
        from scripts.validate_sbom import _canonical
        generation = dict(passed=True, receipt_verified=True,
            sbom_sha256=hashlib.sha256(_canonical(bom)).hexdigest(),
            inventory_sha256=hashlib.sha256(_canonical(inventory)).hexdigest(), native_objects=999)
        with self.assertRaisesRegex(SBOMValidationError, 'independently counted evidence'):
            validate_documents(bom, inventory, generation, require_native=True)

    def test_identity_only_signed_derivative_preserves_payload_graph_and_original(self):
        bom, inventory = self.documents()
        original_bom, original_inventory = deepcopy(bom), deepcopy(inventory)
        file = next(row for row in inventory['release_files'] if row['path'] == 'MediaFlow.exe')
        changes = {'MediaFlow.exe':dict(unsigned_sha256=file['sha256'], signed_sha256='a' * 64, signed_size=file['size'] + 256)}
        rebound_bom, rebound_inventory = native.rebind_signed_bom(bom, inventory, changes)
        self.assertEqual(bom, original_bom)
        self.assertEqual(inventory, original_inventory)
        self.assertEqual(rebound_bom['dependencies'], bom['dependencies'])
        self.assertEqual(rebound_inventory['embedded_members'], inventory['embedded_members'])
        self.assertEqual(rebound_inventory['native_inventory']['containers'][0]['members'], inventory['native_inventory']['containers'][0]['members'])
        self.assertTrue(validate_documents(rebound_bom, rebound_inventory, require_native=True)['passed'])
        with self.assertRaises(native.NativeSBOMError):
            native.rebind_signed_bom(bom, inventory, {'helper.dll':changes['MediaFlow.exe']})
        with self.assertRaises(native.NativeSBOMError):
            native.rebind_signed_bom(bom, inventory, {'MediaFlow.exe':dict(changes['MediaFlow.exe'], unsigned_sha256='b' * 64)})


if __name__ == '__main__':
    unittest.main()
