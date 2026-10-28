"""Fail-closed SignPath orchestration; synthetic PE tables never prove trust."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from scripts import signpath_integration as bridge
from scripts.release_security import ReleaseSecurityError
from tests.test_release_security import unsigned_pe, synthetic_certificate_append


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/signpath_integration.py'
DEFAULT_POLICY = ROOT / 'security/signpath-policy.json'


def configured_policy():
    """Public, deliberately artificial configuration; no token or real signer."""
    return dict(schema_version=1, product='MediaFlow', enabled=True,
        foundation_approved=True, license_review_accepted=True,
        organization_id='00000000-0000-4000-8000-000000000001',
        project_slug='unit-project', signing_policy_slug='unit-manual-approval',
        artifact_configuration_slug='unit-three-pe', certificate_thumbprint='A' * 40,
        certificate_subject='CN=UNIT ONLY public signer description',
        action_commit=bridge.ACTION_COMMIT, environment='signpath-foundation')


def patch_central_field(path, offset, value, fmt='<I'):
    """Mutate an actual ZIP central-header field, keeping the payload real."""
    data = bytearray(path.read_bytes())
    start = data.index(b'PK\x01\x02')
    struct.pack_into(fmt, data, start + offset, value)
    path.write_bytes(data)


class SignPathConfigurationTests(unittest.TestCase):
    def test_default_status_is_disabled_without_credentials_network_or_outputs(self):
        original_get = os.environ.get

        def no_secret_read(name, default=None):
            if name == 'SIGNPATH_API_TOKEN':
                raise AssertionError('status must not inspect a real credential')
            return original_get(name, default)

        with patch.object(os.environ, 'get', side_effect=no_secret_read), \
                patch('urllib.request.urlopen', side_effect=AssertionError('No network')), \
                patch.object(bridge, 'verified_source', side_effect=AssertionError('No build access')), \
                patch.object(Path, 'mkdir', side_effect=AssertionError('No output creation')):
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = bridge.main(['status', '--policy', str(DEFAULT_POLICY)])
        self.assertEqual(code, 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result['state'], 'not_enabled')
        for name in ('configured', 'request_submitted', 'formal_signed', 'production_ready'):
            self.assertIs(result[name], False)

    def test_unconfigured_operations_refuse_before_network_or_output_creation(self):
        for operation in ('preflight', 'prepare', 'complete'):
            with self.subTest(operation=operation), \
                    patch('urllib.request.urlopen', side_effect=AssertionError('No network')), \
                    patch.object(bridge, 'verified_source', side_effect=AssertionError('No build access')), \
                    patch.object(Path, 'mkdir', side_effect=AssertionError('No output creation')):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    code = bridge.main([operation, '--policy', str(DEFAULT_POLICY)])
                self.assertEqual(code, 1)
                result = json.loads(stream.getvalue())
                self.assertIs(result['passed'], False)
                self.assertIs(result['formal_signed'], False)
                self.assertIn('not enabled/approved', result['error'])

    def test_status_and_preflight_work_in_clean_interpreter_without_site_packages(self):
        for operation, expected in (('status', 0), ('preflight', 1)):
            with self.subTest(operation=operation):
                result = subprocess.run([sys.executable, '-I', '-S', '-B', str(SCRIPT), operation],
                    cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    encoding='utf-8', timeout=30,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, expected, result.stderr)
                value = json.loads(result.stdout)
                self.assertIs(value['formal_signed'], False)
                self.assertNotIn('ModuleNotFoundError', result.stdout + result.stderr)
                if operation == 'preflight':
                    self.assertIn('not enabled/approved', value['error'])

    def test_all_three_explicit_approvals_are_required(self):
        for name in ('enabled', 'foundation_approved', 'license_review_accepted'):
            value = configured_policy(); value[name] = False
            with self.subTest(name=name), self.assertRaises(bridge.SignPathError):
                bridge.request_context(value, repository=bridge.REPOSITORY,
                    ref='refs/heads/main', event='workflow_dispatch', token_present=True)

    def test_untrusted_source_event_or_absent_token_is_rejected(self):
        valid = dict(repository=bridge.REPOSITORY, ref='refs/heads/main',
                     event='workflow_dispatch', token_present=True)
        cases = [('repository', 'untrusted/MediaFlow'), ('ref', 'refs/heads/feature'),
            ('ref', 'refs/tags/v2.1.0'), ('event', 'pull_request'), ('event', 'push'),
            ('event', 'pull_request_target'), ('token_present', False),
            ('token_present', 'true'), ('token_present', 1), ('token_present', None)]
        for name, value in cases:
            context = {**valid, name: value}
            with self.subTest(name=name, value=value), self.assertRaises(bridge.SignPathError):
                bridge.request_context(configured_policy(), **context)
        self.assertIsNone(bridge.request_context(configured_policy(), **valid))

    def test_reviewed_policy_rejects_unknown_keys_wrong_types_or_action_identity(self):
        cases = [('schema_version', True), ('schema_version', 2), ('product', 'Other'),
            ('environment', 'unprotected'), ('action_commit', 'v3'),
            ('enabled', 'true'), ('foundation_approved', 1), ('license_review_accepted', None)]
        for name, replacement in cases:
            value = configured_policy(); value[name] = replacement
            with self.subTest(name=name), self.assertRaises(bridge.SignPathError):
                bridge.validate_policy(value, request=True)
        for value in ({**configured_policy(), 'trusted_remote_key': 'not allowed'},
                      {k:v for k,v in configured_policy().items() if k != 'certificate_thumbprint'}):
            with self.assertRaises(bridge.SignPathError):
                bridge.validate_policy(value, request=True)

    def test_signer_and_service_identity_require_explicit_canonical_public_values(self):
        cases = [('organization_id', ''), ('organization_id', 'not-a-uuid'),
            ('project_slug', '../project'), ('project_slug', 'with space'),
            ('signing_policy_slug', 'line\nbreak'), ('artifact_configuration_slug', '-invalid'),
            ('certificate_thumbprint', ''), ('certificate_thumbprint', '*'),
            ('certificate_thumbprint', 'a' * 40), ('certificate_thumbprint', 'A' * 39),
            ('certificate_subject', ''), ('certificate_subject', 'CN=unit\r\npoison'),
            ('certificate_subject', 'A' * 513)]
        for name, replacement in cases:
            value = configured_policy(); value[name] = replacement
            with self.subTest(name=name, replacement=replacement), self.assertRaises(bridge.SignPathError):
                bridge.validate_policy(value, request=True)
        result = bridge.validate_policy(configured_policy(), request=True)
        self.assertTrue(result['configured'])
        self.assertFalse(result['formal_signed'])

    def test_bounded_policy_reader_rejects_duplicate_nonobject_and_oversized_data(self):
        with tempfile.TemporaryDirectory(prefix='MediaFlow-signpath-policy-') as directory:
            path = Path(directory) / 'policy.json'
            for text in ('{"enabled":false,"enabled":true}', '[]', '{} ' + ' ' * 65536):
                path.write_text(text, encoding='utf-8')
                with self.subTest(size=len(text)), self.assertRaises(bridge.SignPathError):
                    bridge.load_policy(path)


class SignPathSBOMBindingTests(unittest.TestCase):
    def setUp(self):
        data = unsigned_pe()
        self.files = [dict(path=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())
                      for name in bridge.FIRST_PARTY]
        self.receipt = {'files': self.files,
            'github': {'repository': bridge.REPOSITORY, 'commit': 'a' * 40, 'run_id': '123'}}
        self.receipt_sha = hashlib.sha256(json.dumps(self.receipt, sort_keys=True).encode()).hexdigest()
        self.inventory = {'release_files': deepcopy(self.files)}
        self.generation = {'receipt_sha256': self.receipt_sha,
            'github': deepcopy(self.receipt['github']), 'release_files': len(self.files)}

    def bind(self, inventory=None, generation=None):
        return bridge.bind_sbom_receipt(self.inventory if inventory is None else inventory,
            self.generation if generation is None else generation, self.receipt, self.receipt_sha)

    def test_same_complete_byte_inventory_in_different_order_is_accepted(self):
        inventory = {'release_files': list(reversed(self.files))}
        original = deepcopy((inventory, self.generation, self.receipt))
        self.assertIsNone(self.bind(inventory=inventory))
        self.assertEqual((inventory, self.generation, self.receipt), original)

    def test_generation_must_bind_the_actual_verified_receipt_digest(self):
        for digest in (None, '', 'b' * 64):
            with self.subTest(digest=digest), self.assertRaises(bridge.SignPathError):
                self.bind(generation={**self.generation, 'receipt_sha256': digest})

    def test_repository_commit_and_run_cannot_be_mixed_across_provenance_documents(self):
        for field, value in (('repository', 'unit/other'), ('commit', 'b' * 40), ('run_id', '124')):
            github = {**self.receipt['github'], field: value}
            with self.subTest(field=field), self.assertRaises(bridge.SignPathError):
                self.bind(generation={**self.generation, 'github': github})
        with self.assertRaises(bridge.SignPathError):
            self.bind(generation={k:v for k,v in self.generation.items() if k != 'github'})

    def test_every_member_path_size_and_hash_must_match_the_current_receipt(self):
        for field, value in (('path', 'other.exe'), ('size', 513), ('sha256', 'b' * 64)):
            inventory = deepcopy(self.inventory); inventory['release_files'][0][field] = value
            with self.subTest(field=field), self.assertRaises(bridge.SignPathError):
                self.bind(inventory=inventory)

    def test_missing_extra_duplicate_and_malformed_members_are_rejected(self):
        rows = deepcopy(self.files)
        cases = [rows[:-1], rows + [rows[0]], [rows[0], rows[1], rows[0]],
            [{k:v for k,v in rows[0].items() if k != 'path'}, *rows[1:]],
            [None, *rows[1:]], None]
        for index, files in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(bridge.SignPathError):
                self.bind(inventory={'release_files': files})

    def test_generation_count_is_exact_and_cannot_be_a_truthy_boolean_or_string(self):
        for count in (None, True, '3', 2, 4):
            with self.subTest(count=count), self.assertRaises(bridge.SignPathError):
                self.bind(generation={**self.generation, 'release_files': count})


class SignPathArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-signpath-zip-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.unsigned = self.root / 'unsigned'
        self.original = unsigned_pe()
        self.signed = synthetic_certificate_append(self.original)
        for name in bridge.FIRST_PARTY:
            path = self.unsigned / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.original)
        self.verified_files = {name: {'path': name,
            'size': (self.unsigned / name).stat().st_size,
            'sha256': hashlib.sha256((self.unsigned / name).read_bytes()).hexdigest()}
            for name in bridge.FIRST_PARTY}
        (self.unsigned / 'LICENSE').write_bytes(b'UNIT PUBLIC LICENSE')

    def write_archive(self, rows=None, *, name='returned.zip'):
        path = self.root / name
        rows = rows if rows is not None else [(entry, self.signed) for entry in bridge.FIRST_PARTY]
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message='Duplicate name:.*', category=UserWarning)
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
                for entry, data in rows:
                    archive.writestr(entry, data)
        return path

    def test_actual_three_pe_zip_payload_preservation_does_not_imply_signature_trust(self):
        archive = self.write_archive()
        result = bridge.inspect_archive(archive, self.unsigned)
        self.assertEqual(set(result), set(bridge.FIRST_PARTY))
        self.assertTrue(all(value == self.signed for value in result.values()))
        self.assertNotIn('formal_signed', result)
        self.assertEqual((self.unsigned / 'MediaFlow.exe').read_bytes(), self.original)

    def test_exact_optional_parent_directories_are_allowed(self):
        rows = [('tools/', b''), ('tools/webview2/', b'')]
        rows += [(name, self.signed) for name in bridge.FIRST_PARTY]
        self.assertEqual(len(bridge.inspect_archive(self.write_archive(rows), self.unsigned)), 3)

    def test_incomplete_or_excess_member_sets_are_rejected(self):
        rows = [(name, self.signed) for name in bridge.FIRST_PARTY]
        cases = [rows[:2], rows[:2] + [('tools/', b'')],
            rows + [('tools/', b''), ('tools/webview2/', b''), ('unexpected/', b'')]]
        for index, entries in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(bridge.SignPathError):
                bridge.inspect_archive(self.write_archive(entries, name=f'count-{index}.zip'), self.unsigned)

    def test_duplicate_exact_and_case_colliding_members_are_rejected(self):
        rows = [(name, self.signed) for name in bridge.FIRST_PARTY]
        for name in ('MediaFlow.exe', 'MEDIAFLOW.EXE'):
            with self.subTest(name=name), self.assertRaises(bridge.SignPathError):
                bridge.inspect_archive(self.write_archive(rows + [(name, self.signed)]), self.unsigned)

    def test_traversal_absolute_backslash_and_third_party_pe_names_are_rejected(self):
        replacements = ('../MediaFlow.exe', '/MediaFlow.exe', 'C:/MediaFlow.exe',
            'tools\\MediaFlow.Recycle.exe', 'tools/../MediaFlow.exe',
            'tools/ffmpeg.exe', 'tools/webview2/WebView2Loader.dll', 'MediaFlow.exe:stream')
        for name in replacements:
            rows = [(name, self.signed)] + [(entry, self.signed) for entry in bridge.FIRST_PARTY[1:]]
            with self.subTest(name=name), self.assertRaises(bridge.SignPathError):
                bridge.inspect_archive(self.write_archive(rows), self.unsigned)

    def test_symlink_special_file_and_windows_reparse_member_attributes_are_rejected(self):
        for mode, flags in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFIFO | 0o600, 0),
                            (stat.S_IFREG | 0o600, 0x400)):
            entry = zipfile.ZipInfo(bridge.FIRST_PARTY[0]); entry.create_system = 3
            entry.external_attr = (mode << 16) | flags
            rows = [(entry, self.signed)] + [(name, self.signed) for name in bridge.FIRST_PARTY[1:]]
            with self.subTest(mode=mode, flags=flags), self.assertRaises(bridge.SignPathError):
                bridge.inspect_archive(self.write_archive(rows), self.unsigned)

    def test_encryption_header_is_rejected_before_any_payload_is_accepted(self):
        path = self.write_archive()
        patch_central_field(path, 8, 1, '<H')
        with self.assertRaises(bridge.SignPathError):
            bridge.inspect_archive(path, self.unsigned)

    def test_zero_length_or_oversized_declared_member_is_rejected_without_large_allocation(self):
        rows = [(bridge.FIRST_PARTY[0], b'')] + [(name, self.signed) for name in bridge.FIRST_PARTY[1:]]
        with self.assertRaises(bridge.SignPathError):
            bridge.inspect_archive(self.write_archive(rows), self.unsigned)
        path = self.write_archive(name='oversized-header.zip')
        patch_central_field(path, 24, 600 * 1024 * 1024 + 1)
        with self.assertRaises(bridge.SignPathError):
            bridge.inspect_archive(path, self.unsigned)

    def test_bad_zip_is_not_accepted(self):
        path = self.root / 'corrupt.zip'; path.write_bytes(b'not a real zip')
        with self.assertRaises(zipfile.BadZipFile):
            bridge.inspect_archive(path, self.unsigned)

    def test_unsigned_returned_pe_and_payload_tampering_are_rejected_by_real_byte_checker(self):
        tampered = bytearray(self.signed); tampered[420] = 1
        for data in (self.original, bytes(tampered), self.signed + b'forbidden-overlay'):
            rows = [(bridge.FIRST_PARTY[0], data)] + [(name, self.signed) for name in bridge.FIRST_PARTY[1:]]
            with self.subTest(digest=hashlib.sha256(data).hexdigest()), self.assertRaises(ReleaseSecurityError):
                bridge.inspect_archive(self.write_archive(rows), self.unsigned)

    def test_prepare_copies_only_fixed_first_party_files_and_preserves_existing_outputs(self):
        output = self.root / 'first-party'
        bridge.prepare(self.unsigned, output, expected_files=self.verified_files)
        self.assertEqual({p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()},
                         set(bridge.FIRST_PARTY))
        self.assertTrue(all((output / name).read_bytes() == self.original for name in bridge.FIRST_PARTY))
        marker = output / 'keep'; marker.write_bytes(b'preserve')
        with self.assertRaises(bridge.SignPathError):
            bridge.prepare(self.unsigned, output, expected_files=self.verified_files)
        self.assertEqual(marker.read_bytes(), b'preserve')
        with self.assertRaises(bridge.SignPathError):
            bridge.prepare(self.unsigned, self.unsigned / 'nested', expected_files=self.verified_files)
        self.assertFalse((self.unsigned / 'nested').exists())

    def test_prepare_refuses_copy_tampering_or_mismatched_receipt_before_submission(self):
        copy = bridge.shutil.copyfile

        def tamper_after_actual_copy(source, target):
            result = copy(source, target)
            data = bytearray(Path(target).read_bytes()); data[420] ^= 1
            Path(target).write_bytes(data)
            return result

        for case in ('copy_tampered', 'receipt_mismatch'):
            output = self.root / case
            records = deepcopy(self.verified_files)
            if case == 'receipt_mismatch':
                records[bridge.FIRST_PARTY[0]]['sha256'] = 'b' * 64
            with self.subTest(case=case), patch.object(bridge.shutil, 'copyfile',
                    side_effect=tamper_after_actual_copy if case == 'copy_tampered' else copy):
                with self.assertRaisesRegex(bridge.SignPathError, 'submission is blocked'):
                    bridge.prepare(self.unsigned, output, expected_files=records)
            self.assertEqual((self.unsigned / 'MediaFlow.exe').read_bytes(), self.original)
            self.assertTrue(output.exists(), 'Failed copy evidence is retained; it is not a verified upload')

    def test_complete_unapproved_policy_does_not_read_archive_or_create_output(self):
        policy = configured_policy(); policy['foundation_approved'] = False
        output = self.root / 'signed-release'; work = self.root / 'work'; work.mkdir()
        with patch.object(bridge, 'inspect_archive', side_effect=AssertionError('No archive read')):
            with self.assertRaises(bridge.SignPathError):
                bridge.complete(policy, self.unsigned, self.root / 'unused.json',
                    self.root / 'absent.zip', output, work)
        self.assertFalse(output.exists())

    def test_native_wrong_subject_blocks_final_acceptance_and_leaves_original_unchanged(self):
        policy = configured_policy(); output = self.root / 'signed'; work = self.root / 'work'; work.mkdir()
        archive = self.write_archive()
        # Stub only the external Windows result to drive the mismatch path.
        # No synthetic certificate is ever asserted to be cryptographically trusted.
        with patch.object(bridge, 'authenticode', return_value={
                'certificate': {'subject': 'CN=DIFFERENT UNIT SIGNER'}}) as native, \
                patch.object(bridge, 'signing_derivation', side_effect=AssertionError('No accepted derivation')):
            with self.assertRaises(bridge.SignPathError):
                bridge.complete(policy, self.unsigned, self.root / 'unused.json', archive, output, work)
        self.assertEqual(native.call_args.args[0], 'Verify')
        self.assertEqual(native.call_args.kwargs['options']['thumbprint'], policy['certificate_thumbprint'])
        self.assertFalse((work / 'signpath-result.json').exists())
        self.assertEqual((self.unsigned / 'MediaFlow.exe').read_bytes(), self.original)
        self.assertEqual((output / 'LICENSE').read_bytes(), b'UNIT PUBLIC LICENSE')

    def test_native_verification_failure_never_creates_accepted_result(self):
        output = self.root / 'signed'; work = self.root / 'work'; work.mkdir()
        with patch.object(bridge, 'authenticode', side_effect=ReleaseSecurityError('unit native refusal')), \
                patch.object(bridge, 'signing_derivation', side_effect=AssertionError('No accepted derivation')):
            with self.assertRaises(ReleaseSecurityError):
                bridge.complete(configured_policy(), self.unsigned, self.root / 'unused.json',
                    self.write_archive(), output, work)
        self.assertFalse((work / 'signpath-result.json').exists())
        self.assertTrue(output.exists(), 'Rejected evidence is preserved, not auto-deleted')


if __name__ == '__main__':
    unittest.main()
