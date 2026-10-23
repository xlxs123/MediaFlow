"""Real file/index disclosure tests. All credential-shaped inputs are synthetic."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.audit_public_source import AuditError, audit, main


class PublicSourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-public-audit-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode('utf-8') if isinstance(value, str) else value)
        return path

    def review(self, name, classification='synthetic_test_data'):
        return {'schema_version': 1, 'reviews': [{'path': name,
            'sha256': hashlib.sha256((self.root / name).read_bytes()).hexdigest(),
            'classification': classification, 'reviewed_by': 'unit-test-maintainer',
            'reason': 'Only deliberately constructed synthetic test input; no user data.'}]}

    def test_clean_exact_inventory_and_stable_manifest(self):
        self.file('main.py', 'print("合成公开源码")\n')
        self.file('LICENSE', 'MIT unit fixture\n')
        first = audit(self.root, candidates=['main.py', 'LICENSE'])
        second = audit(self.root, candidates=['LICENSE', 'main.py'])
        self.assertTrue(first['passed'])
        self.assertEqual(first['file_count'], 2)
        self.assertEqual(first['manifest_sha256'], second['manifest_sha256'])
        self.assertEqual(first['files'][0]['sha256'], hashlib.sha256(b'MIT unit fixture\n').hexdigest())

    def test_test_secrets_are_not_implicitly_exempt_and_require_exact_review(self):
        name = 'tests/test_synthetic.py'
        self.file(name, "password = 'synthetic-unit-passphrase'\n")
        first = audit(self.root, candidates=[name])
        self.assertFalse(first['passed'])
        self.assertEqual(first['pending_review_count'], 1)
        self.assertNotIn('synthetic-unit-passphrase', json.dumps(first))
        second = audit(self.root, candidates=[name], reviews=self.review(name))
        self.assertTrue(second['passed'])
        self.assertEqual(second['reviewed_finding_count'], 1)

    def test_changed_review_fails_even_when_same_rule_remains(self):
        name = 'tests/test_synthetic.py'
        self.file(name, "password = 'synthetic-first-value'\n")
        approved = self.review(name)
        self.file(name, "password = 'synthetic-second-value'\n")
        result = audit(self.root, candidates=[name], reviews=approved)
        self.assertFalse(result['passed'])
        self.assertIn('review_hash_stale', [item['rule'] for item in result['findings']])

    def test_full_private_key_blocks_and_token_shapes_cannot_be_reviewed_away(self):
        name = 'tests/test_bad_material.py'
        marker = '-----' + 'BEGIN PRIVATE KEY' + '-----'
        token = 'gh' + 'p_' + 'SYNTHETICONLY' * 4
        self.file(name, marker + '\n' + 'SYNTHETICBASE64' * 8 + '\n' + token)
        result = audit(self.root, candidates=[name], reviews=self.review(name))
        self.assertFalse(result['passed'])
        rules = [item['rule'] for item in result['findings'] if item['severity'] == 'error']
        self.assertIn('private_key_material', rules)
        self.assertIn('github_credential', rules)
        serialized = json.dumps(result)
        self.assertNotIn(token, serialized)
        self.assertNotIn('SYNTHETICBASE64', serialized)

    def test_key_generation_and_marker_have_explicit_review_findings(self):
        name = 'tests/test_generated.py'
        self.file(name, "key = Ed25519PrivateKey.generate()\nmarker = '-----BEGIN "
                       "ENCRYPTED PRIVATE KEY-----'\n")
        result = audit(self.root, candidates=[name])
        self.assertFalse(result['passed'])
        self.assertEqual(result['pending_review_count'], 2)
        self.assertTrue(audit(self.root, candidates=[name], reviews=self.review(name))['passed'])

    def test_source_path_privacy_is_an_error_and_not_reported_verbatim(self):
        name = 'README.md'
        private_path = 'G:' + '\\create program-codex\\PRIVATE-UNIT-USERNAME'
        self.file(name, 'launch ' + private_path)
        result = audit(self.root, candidates=[name], reviews=self.review(name, 'documented_placeholder'))
        self.assertFalse(result['passed'])
        self.assertEqual(result['findings'][0]['rule'], 'personal_windows_path')
        self.assertNotIn('PRIVATE-UNIT-USERNAME', json.dumps(result))

    def test_synthetic_test_windows_paths_require_human_review(self):
        name = 'tests/test_path.py'
        self.file(name, 'location = "C:' + '\\Users\\SYNTHETIC-UNIT-USER\\file"')
        self.assertFalse(audit(self.root, candidates=[name])['passed'])
        self.assertTrue(audit(self.root, candidates=[name], reviews=self.review(name))['passed'])

    def test_user_data_generated_binaries_media_and_unknown_files_are_rejected(self):
        names = ['verification/report.json', 'BrowserProfile/Default/Preferences',
                 'tools/ffmpeg.exe', 'tests/saved.mp4', 'assets/formal-private.pem',
                 'core/library.sqlite-wal', 'notes/private.json', 'dist-mediaflow/main.py',
                 'core/settings.json', '.env']
        for name in names:
            with self.subTest(name=name):
                self.file(name, 'synthetic-only')
                self.assertFalse(audit(self.root, candidates=[name])['passed'])

    def test_allowlist_does_not_make_nontext_source_safe(self):
        self.file('core/source.py', b'not source\0binary')
        result = audit(self.root, candidates=['core/source.py'])
        self.assertIn('non_text_source', [item['rule'] for item in result['findings']])

    def test_only_bounded_application_icon_is_allowed(self):
        self.file('assets/app.ico', b'\0\0\1\0unit-icon')
        self.assertTrue(audit(self.root, candidates=['assets/app.ico'])['passed'])
        self.file('assets/app.ico', b'not-an-icon')
        self.assertFalse(audit(self.root, candidates=['assets/app.ico'])['passed'])

    def test_traversal_absolute_and_case_collisions_fail_closed(self):
        for value in ('../secret.py', '/absolute.py', 'C:/secret.py',
                      'core\\source.py', 'core/../source.py', 'core//source.py',
                      'core/source.py\nsecret', 'core/trailing. '):
            with self.subTest(value=value), self.assertRaises(AuditError):
                audit(self.root, candidates=[value])
        with self.assertRaises(AuditError):
            audit(self.root, candidates=['core/Source.py', 'core/source.py'])

    def test_worktree_symlink_is_never_followed(self):
        self.file('main.py', 'safe\n')
        link = self.root / 'core/linked.py'
        link.parent.mkdir()
        try:
            link.symlink_to(self.root / 'main.py')
        except OSError:
            self.skipTest('Creating symlinks is not permitted for this test user')
        result = audit(self.root, candidates=['core/linked.py'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['findings'][0]['rule'], 'symlink_or_reparse_point')

    @unittest.skipUnless(shutil.which('git'), 'Git is needed for exact staged-byte tests')
    def test_index_audits_staged_bytes_not_a_clean_unstaged_replacement(self):
        def git(*arguments):
            return subprocess.run(['git', '-C', str(self.root), *arguments],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git('init', '--quiet')
        path = self.file('main.py', "password = 'synthetic-staged-only'\n")
        git('add', '--', 'main.py')
        path.write_text('print("clean replacement")\n', encoding='utf-8')
        result = audit(self.root, index=True)
        self.assertFalse(result['passed'])
        self.assertEqual(result['selection'], 'index')
        self.assertEqual(result['files'][0]['sha256'],
                         hashlib.sha256(b"password = 'synthetic-staged-only'\n").hexdigest())
        self.assertTrue(audit(self.root, tracked=True)['passed'])

    @unittest.skipUnless(shutil.which('git'), 'Git is needed for ignored staged-file tests')
    def test_gitignore_does_not_excuse_a_force_staged_user_database(self):
        def git(*arguments):
            subprocess.run(['git', '-C', str(self.root), *arguments], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git('init', '--quiet')
        self.file('.gitignore', 'BrowserProfile/\n')
        self.file('BrowserProfile/Default/Cookies', 'unit data')
        git('add', '--', '.gitignore')
        git('add', '--force', '--', 'BrowserProfile/Default/Cookies')
        result = audit(self.root, index=True)
        self.assertFalse(result['passed'])
        self.assertIn('private_or_generated_directory', [item['rule'] for item in result['findings']])

    @unittest.skipUnless(shutil.which('git'), 'Git is needed for staged symlink tests')
    def test_index_symlink_mode_is_rejected_without_reading_target(self):
        def git(*arguments, data=None):
            return subprocess.run(['git', '-C', str(self.root), *arguments], input=data,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        git('init', '--quiet')
        identity = git('hash-object', '-w', '--stdin', data=b'../../outside-private').decode().strip()
        git('update-index', '--add', '--cacheinfo', '120000', identity, 'core/link.py')
        result = audit(self.root, index=True)
        self.assertFalse(result['passed'])
        self.assertEqual(result['findings'][0]['rule'], 'symlink_submodule_or_invalid_mode')

    def test_cli_report_contains_inventory_without_matched_values(self):
        self.file('tests/test_fixture.py', "password = 'synthetic-cli-private-value'\n")
        candidates = self.root / 'candidates.json'
        candidates.write_text(json.dumps(['tests/test_fixture.py']), encoding='utf-8')
        report = self.root / 'audit-report.json'
        self.assertEqual(main(['--root', str(self.root), '--candidates', str(candidates),
                               '--report', str(report)]), 1)
        value = report.read_text(encoding='utf-8')
        self.assertNotIn('synthetic-cli-private-value', value)
        self.assertIn('credential_literal', value)


if __name__ == '__main__':
    unittest.main()
