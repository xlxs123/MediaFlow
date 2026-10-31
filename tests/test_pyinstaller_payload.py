"""Normal data archives only; no PE execution or certificate construction."""
import io
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from scripts import pyinstaller_payload as payload
from scripts import release_security as security
from scripts import signpath_integration as bridge
from tests.pyinstaller_archive_fixture import archive_data


class ArchiveBindingTests(unittest.TestCase):
    def test_normal_archive_reports_exact_cookie_toc_and_pyz_ranges(self):
        data = archive_data()
        result = payload.archive_unchanged(data, data)
        self.assertTrue(result['passed'])
        self.assertEqual(result['archive_start'], len(b'plain carrier prefix\0'))
        self.assertEqual(result['cookie_offset'], len(data) - payload.COOKIE.size)
        self.assertEqual(result['archive_end'], len(data))
        self.assertEqual(result['members'], 1)
        self.assertEqual(result['pyz'][0]['offset'], result['archive_start'])

    def test_physical_eof_selection_agrees_with_actual_pinned_upstream_reader(self):
        from PyInstaller.archive.readers import CArchiveReader
        for length in (1, 8184, 8191, 8192, 8193, 16384):
            for suffix in (b'', b'ordinary nonarchive trailing data' * 400):
                data = archive_data(prefix=b'x' * length) + suffix
                with self.subTest(length=length, suffix=len(suffix)):
                    selected = payload.selected_archive(data)
                    self.assertEqual(selected['cookie_offset'],
                        CArchiveReader._find_magic_pattern(io.BytesIO(data), payload.MAGIC))

    def test_writer_supported_repeated_bootstrap_names_retain_the_raw_toc(self):
        data = archive_data(extra_types=(b'm', b'm', b'o', b's'))
        identity = payload.archive_unchanged(data, data)
        self.assertEqual(identity['members'], 5)
        self.assertEqual(identity['toc_size'], 160)

    def test_original_and_returned_data_archives_must_have_identical_contents(self):
        original = archive_data()
        changed = archive_data(payload=b'different ordinary member data')
        with self.assertRaisesRegex(payload.PayloadError, 'identity'):
            payload.archive_unchanged(original, changed)

    def test_equal_archive_content_at_a_different_offset_is_refused(self):
        with self.assertRaisesRegex(payload.PayloadError, 'identity'):
            payload.archive_unchanged(archive_data(prefix=b'A'), archive_data(prefix=b'BB'))

    def test_nonarchive_trailing_data_does_not_change_selection(self):
        data = archive_data()
        self.assertTrue(payload.archive_unchanged(data, data + b'ordinary trailing data')['passed'])

    def test_unsigned_source_requires_cookie_at_original_eof(self):
        data = archive_data() + b'ordinary trailing data'
        with self.assertRaisesRegex(payload.PayloadError, 'unsigned carrier EOF'):
            payload.archive_unchanged(data, data)

    def test_missing_incomplete_and_invalid_data_metadata_fail_closed(self):
        data = archive_data()
        invalid_length = bytearray(data)
        struct.pack_into('!I', invalid_length, len(data) - payload.COOKIE.size + 8, len(data) + 1)
        for case in (b'', b'ordinary data', data[:-1], bytes(invalid_length)):
            with self.subTest(size=len(case)), self.assertRaises(payload.PayloadError):
                payload.selected_archive(case)

    def test_main_program_gate_requires_selection_proof_after_byte_derivation(self):
        # Isolate call ordering with data-only inputs: real PE byte checks retain
        # their existing tests; actual build acceptance uses the normal EXE.
        with patch.object(security, 'signature_only_change', return_value={'byte_check': True}) as byte_check:
            with self.assertRaises(security.ReleaseSecurityError):
                security.first_party_change('MediaFlow.exe', b'not an archive', b'not an archive')
            byte_check.assert_called_once()
            data = archive_data()
            result = security.first_party_change('MediaFlow.exe', data, data)
            self.assertTrue(result['pyinstaller_payload']['passed'])

    def test_helpers_keep_their_own_pe_contract_without_requiring_pyinstaller(self):
        with patch.object(security, 'signature_only_change', return_value={'byte_check': True}), \
                patch.object(payload, 'archive_unchanged', side_effect=AssertionError('Not a PyInstaller program')):
            for name in security.FIRST_PARTY[1:]:
                self.assertEqual(security.first_party_change(name, b'helper', b'helper'), {'byte_check': True})

    def test_bridge_and_local_derivation_use_the_same_named_first_party_gate(self):
        import ast
        for module, function in ((bridge, 'inspect_archive'), (security, 'signing_derivation')):
            tree = ast.parse(Path(module.__file__).read_text('utf-8-sig'))
            node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == function)
            calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name) and item.func.id == 'first_party_change']
            self.assertEqual(len(calls), 1)
            self.assertIsInstance(calls[0].args[0], ast.Name)
            self.assertEqual(calls[0].args[0].id, 'name')


if __name__ == '__main__':
    unittest.main()
