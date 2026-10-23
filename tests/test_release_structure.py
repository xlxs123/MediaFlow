"""Real local file fixtures for CI structure checks; never release acceptance."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from core.update_manifest import REQUIRED_FILES, UpdateValidationError
from scripts.check_release_structure import (LICENSE_PACKAGES, REQUIRED_DOCUMENTS,
    REQUIRED_TOOL_NOTICES, ReleaseStructureError, check_release, main)
from scripts.package_update import PackageError


def pe_bytes(machine=0x8664):
    data = bytearray(256)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, machine)
    return bytes(data)


class ReleaseStructureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-release-structure-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release = self.root / 'release'
        self.release.mkdir()
        for name in set(REQUIRED_FILES) | set(REQUIRED_DOCUMENTS) | set(REQUIRED_TOOL_NOTICES):
            path = self.release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pe_bytes() if path.suffix.lower() in ('.exe', '.dll')
                             else b'Unit fixture public notice.\n')
        self.stamp = {'packaged': True, 'app_version': '2.1.0', 'build_id': 'unit-structure-only',
            'source_sha256': 'a' * 64, 'update_channel': {'key_state': 'pending',
                'source_sha256': hashlib.sha256(b'').hexdigest(), 'key_fingerprints': {}}}
        for feature in ('dash', 'track', 'organization', 'storage', 'migration', 'update'):
            self.stamp[feature + '_module_loaded'] = True
        self.save_stamp()
        for prefix in LICENSE_PACKAGES:
            directory = self.release / 'licenses' / (prefix + 'unit.dist-info')
            directory.mkdir(parents=True)
            (directory / 'LICENSE').write_text('Unit fixture license.\n', 'utf-8')
            if prefix == 'cryptography-':
                for name in ('LICENSE.APACHE', 'LICENSE.BSD'):
                    (directory / name).write_text('Unit fixture license.\n', 'utf-8')
        notices = self.release / 'licenses/QtWebEngine'
        notices.mkdir(parents=True)
        pages = []
        for number in range(10):
            data = ('Unit fixture license ' + str(number)).encode()
            name = 'notice-' + str(number) + '.html'
            (notices / name).write_bytes(data)
            pages.append({'file': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
        (notices / 'provenance.json').write_text(json.dumps({'files': pages}), 'utf-8')
        self.provenance = {}
        for tool in ('yt-dlp.exe', 'ffmpeg.exe', 'ffprobe.exe'):
            self.provenance[tool] = {'source': 'https://tools.invalid/' + tool,
                'sha256': hashlib.sha256((self.release / 'tools' / tool).read_bytes()).hexdigest()}
        (self.release / 'tools/provenance.json').write_text(json.dumps(self.provenance), 'utf-8')
        self.runtime = self.root / 'private-runtime-report.json'
        self.runtime.write_text(json.dumps(dict(self.stamp,
            executable=str(self.release / 'MediaFlow.exe'), runtime_root=str(self.root / 'test-runtime'))), 'utf-8')

    def save_stamp(self):
        (self.release / 'build-info.json').write_text(json.dumps(self.stamp), 'utf-8')

    def check(self, **changes):
        return check_release(self.release, '2.1.0', **changes)

    def test_complete_folder_hashes_actual_bytes_and_never_claims_execution_or_publish(self):
        result = self.check(runtime_identity=self.runtime)
        self.assertTrue(result['passed'])
        self.assertTrue(result['runtime_identity_verified'])
        self.assertFalse(result['native_exe_executed'])
        self.assertFalse(result['signed'])
        self.assertFalse(result['published'])
        self.assertEqual(result['sha256']['MediaFlow.exe'], hashlib.sha256(pe_bytes()).hexdigest())
        self.assertEqual(result['files'], len([path for path in self.release.rglob('*') if path.is_file()]))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_every_required_tool_document_and_notice_is_mandatory(self):
        for name in ('tools/ffprobe.exe', 'UPDATE_AUDIT.md', 'tools/webview2/LICENSE.txt',
                     'licenses/QtWebEngine/provenance.json'):
            with self.subTest(name=name):
                path = self.release / name
                before = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(ReleaseStructureError, '缺少必需文件'):
                    self.check()
                path.write_bytes(before)

    def test_empty_file_corrupt_pe_and_wrong_main_architecture_are_rejected(self):
        path = self.release / 'MediaFlow.exe'
        for data in (b'', b'MZ truncated', pe_bytes(0x14C), b'not an executable'):
            with self.subTest(data=data[:15]), self.assertRaises(ReleaseStructureError):
                path.write_bytes(data)
                self.check()

    def test_declared_version_loaded_modules_and_channel_are_checked(self):
        original = deepcopy(self.stamp)
        for field, value in (('app_version', '2.0.1'), ('update_module_loaded', False),
                             ('source_sha256', 'not-a-hash'), ('update_channel', {'key_state': 'invalid'})):
            with self.subTest(field=field):
                self.stamp = dict(original, **{field: value})
                self.save_stamp()
                with self.assertRaises((ReleaseStructureError, PackageError)):
                    self.check()

    def test_published_stamp_cannot_contain_private_runtime_paths(self):
        for field in ('executable', 'runtime_root'):
            with self.subTest(field=field):
                self.stamp[field] = 'portable-but-forbidden-runtime-field'
                self.save_stamp()
                with self.assertRaisesRegex(ReleaseStructureError, '运行时绝对路径字段'):
                    self.check()
                self.stamp.pop(field)

    def test_private_text_and_credentials_are_rejected_without_returning_the_secret(self):
        document = self.release / 'README.md'
        for content in ('-----BEGIN ENCRYPTED PRIVATE KEY-----\nfixture only',
                        'Authorization: Bearer UNIT-TEST-NEVER-PRODUCTION',
                        'C:' + chr(92) + 'Users' + chr(92) + 'unit-person' + chr(92) + 'private'):
            with self.subTest(content=content[:12]):
                document.write_text(content, 'utf-8')
                with self.assertRaisesRegex(ReleaseStructureError, '公开文本含') as error:
                    self.check()
                self.assertNotIn('UNIT-TEST-NEVER-PRODUCTION', str(error.exception))
                self.assertNotIn('unit-person', str(error.exception))
        document.write_text('Safe public notice.\n', 'utf-8')
        self.stamp['authorization'] = 'UNIT-TEST-CREDENTIAL'
        self.save_stamp()
        with self.assertRaisesRegex(ReleaseStructureError, '凭据或授权字段'):
            self.check()

    def test_user_profile_media_and_unexpected_extra_file_are_rejected(self):
        for name in ('BrowserProfile', 'recording.mp4', 'personal-photo.jpg'):
            path = self.release / name
            with self.subTest(name=name):
                if name == 'BrowserProfile':
                    path.mkdir()
                else:
                    path.write_bytes(b'UNIT TEST DATA')
                with self.assertRaises((ReleaseStructureError, PackageError, UpdateValidationError)):
                    self.check()
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()

    def test_full_tool_hash_and_license_hash_are_verified(self):
        tool = self.release / 'tools/ffprobe.exe'
        original = tool.read_bytes()
        tool.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ReleaseStructureError, '实际 SHA-256'):
            self.check()
        tool.write_bytes(original)
        page = self.release / 'licenses/QtWebEngine/notice-1.html'
        page.write_bytes(b'changed')
        with self.assertRaisesRegex(ReleaseStructureError, '许可正文'):
            self.check()

    def test_crypto_license_set_and_duplicate_json_are_rejected(self):
        license_file = self.release / 'licenses/cryptography-unit.dist-info/LICENSE.BSD'
        original = license_file.read_bytes()
        license_file.unlink()
        with self.assertRaisesRegex(ReleaseStructureError, 'cryptography 许可文件不完整'):
            self.check()
        license_file.write_bytes(original)
        (self.release / 'build-info.json').write_text('{"packaged":true,"packaged":false}', 'utf-8')
        with self.assertRaisesRegex(ReleaseStructureError, '重复字段'):
            self.check()

    def test_hardlinked_files_are_rejected_without_reading_external_contents(self):
        external = self.root / 'external-unit-document.txt'
        external.write_bytes(b'EXTERNAL UNIT TEST CONTENT')
        document = self.release / 'README.md'
        document.unlink()
        os.link(external, document)
        with self.assertRaisesRegex(PackageError, '硬链接'):
            self.check()
        self.assertEqual(external.read_bytes(), b'EXTERNAL UNIT TEST CONTENT')

    def test_actual_runtime_identity_must_match_sanitized_build_identity(self):
        runtime = json.loads(self.runtime.read_text('utf-8'))
        runtime['build_id'] = 'different-build'
        self.runtime.write_text(json.dumps(runtime), 'utf-8')
        with self.assertRaisesRegex(ReleaseStructureError, '实际 EXE 身份报告'):
            self.check(runtime_identity=self.runtime)

    def test_cli_writes_only_a_new_external_report_and_returns_nonzero_on_failure(self):
        output = self.root / 'structure.json'
        arguments = ['--release-dir', str(self.release), '--expected-version', '2.1.0',
                     '--runtime-identity', str(self.runtime), '--report', str(output)]
        self.assertEqual(main(arguments), 0)
        before = output.read_bytes()
        self.assertEqual(main(arguments), 1)
        self.assertEqual(output.read_bytes(), before)
        command = [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/check_release_structure.py'),
                   '--release-dir', str(self.release), '--expected-version', '2.0.1',
                   '--runtime-identity', str(self.runtime), '--report', str(self.root / 'failure.json')]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'failure.json').exists())


if __name__ == '__main__':
    unittest.main()
