"""Synthetic scanner contracts plus optional real pinned-Gitleaks Git fixtures."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import scan_secrets as scanner


class SecretScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-secret-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def file(self, name, data):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)
        return path

    def exception(self, **changes):
        return dict({'rule': 'mediaflow-credential-literal', 'path': 'tests/fixture.py',
            'commit': '1' * 40, 'line': 3, 'value_sha256': 'a' * 64,
            'classification': 'synthetic_test_data', 'reviewed_by': 'unit-maintainer',
            'reason': 'Exact deliberately synthesized unit input, never a real credential.'}, **changes)

    def write_exceptions(self, entries):
        return self.file('exceptions.json', json.dumps({'schema_version': 1, 'exceptions': entries}))

    def row(self, **changes):
        value = 'SYNTHETIC' + '-scanner-test-value'
        return dict({'RuleID': 'mediaflow-credential-literal', 'File': 'tests/fixture.py',
            'Commit': '1' * 40, 'StartLine': 3, 'Secret': value,
            'Match': 'synthetic complete matched line', 'Author': 'SYNTHETIC AUTHOR',
            'Email': 'synthetic@example.invalid', 'Message': 'synthetic private commit message'}, **changes)

    def detect(self, rows, *, history=True, code=scanner.LEAK_EXIT, history_blobs=None):
        with patch.object(scanner, '_run', return_value=(code, json.dumps(rows).encode('utf-8'))) as call:
            result = scanner._detect(self.directory / 'tool', self.directory,
                history=history, config=self.directory / 'rules.toml', env={},
                commits={'1' * 40}, index={'tests/fixture.py': '2' * 40}, history_blobs=history_blobs)
            return result, call.call_args

    def test_public_findings_never_include_secret_match_identity_or_value_hash(self):
        rows = [self.row()]
        found, _ = self.detect(rows)
        serialized = json.dumps([finding.public() for finding in found])
        self.assertEqual(set(found[0].public()), {'type', 'commit', 'path', 'line'})
        for key in ('Secret', 'Match', 'Author', 'Email', 'Message'):
            self.assertNotIn(rows[0][key], serialized)
        self.assertNotIn(found[0].secret_sha256, serialized)

    def test_full_history_flags_and_in_line_bypass_disabled(self):
        _, call = self.detect([] , code=0)
        command = call.args[0]
        self.assertIn('git', command)
        self.assertIn('--log-opts=--all --full-history --root -m --no-renames --no-ext-diff --no-textconv', command)
        self.assertIn('--ignore-gitleaks-allow', command)
        self.assertEqual(command[command.index('--report-path') + 1], '-')
        self.assertNotIn('--baseline-path', command)

    def test_index_findings_bind_exact_blob_instead_of_current_worktree(self):
        rows = [self.row(Commit='', File=str(self.directory / 'tests/fixture.py'))]
        findings, call = self.detect(rows, history=False)
        self.assertIn('dir', call.args[0])
        self.assertEqual(findings[0].commit, 'INDEX:' + '2' * 40)
        self.assertEqual(findings[0].path, 'tests/fixture.py')

    def test_wrong_commit_or_outside_index_report_is_rejected(self):
        for row, history in ((self.row(Commit='3' * 40), True),
                (self.row(File='../outside.txt'), False),
                (self.row(File='not-in-index.txt'), False)):
            with self.subTest(history=history), self.assertRaises(scanner.SecretScanError):
                self.detect([row], history=history)

    def test_detector_operational_error_is_not_a_clean_scan(self):
        for rows, code in (([], 1), ([], scanner.LEAK_EXIT), ([self.row()], 0)):
            with self.subTest(code=code), self.assertRaises(scanner.SecretScanError):
                self.detect(rows, code=code)
        with patch.object(scanner, '_run', return_value=(0, b'not-json')):
            with self.assertRaisesRegex(scanner.SecretScanError, 'report_invalid'):
                scanner._detect('tool', self.directory, history=True,
                    config='rules', env={}, commits=set(), index={})

    def test_sensitive_paths_and_magic_do_not_depend_on_extensions(self):
        for name in ('BrowserProfile/Default/Cookies', 'safe/Cookies', 'state/data.sqlite',
                'keys/formal.dpapi', 'keys/formal.pem', '.env', 'state/credentials.json',
                '.ssh/id_ed25519', '.gitleaksignore', 'archives/session.zip'):
            with self.subTest(name=name):
                self.assertTrue(scanner.sensitive_path(name))
        for content in (b'SQLite format 3\0rest', b'# Netscape HTTP Cookie File\n',
                b'PK\x03\x04SYNTHETIC-ARCHIVE', ('-----' + 'BEGIN PRIVATE KEY-----\n').encode('ascii')):
            self.assertTrue(scanner.sensitive_content(content))
        self.assertFalse(scanner.sensitive_path('assets/update-channel.json'))
        self.assertFalse(scanner.sensitive_content(b'normal public source'))

    def test_snapshot_paths_cannot_address_windows_devices_or_escape(self):
        for value in ('CON', 'data/NUL.txt', 'aux/source.py', '../outside', '/absolute',
                'C:/outside', 'data\\outside', 'data/../outside', 'data/.git/config', 'data/trailing. '):
            with self.subTest(value=value), self.assertRaises(scanner.SecretScanError):
                scanner._git_path(value)

    def test_exceptions_require_rule_path_value_commit_and_line(self):
        entry = self.exception()
        approved, _ = scanner._load_exceptions(self.write_exceptions([entry]))
        self.assertIn((entry['rule'], entry['path'], entry['commit'], entry['line'], entry['value_sha256']), approved)
        approved_index, _ = scanner._load_exceptions(self.write_exceptions([
            self.exception(commit='INDEX:' + '2' * 40)]))
        self.assertEqual(len(approved_index), 1)
        changes = ({'commit': '*'}, {'path': '../tests/fixture.py'}, {'path': 'tests/*'}, {'value_sha256': '*'},
            {'rule': '*'}, {'line': 0}, {'classification': 'entire_test_directory'},
            {'rule': 'sensitive-user-data-file'}, {'reason': 'short'})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(scanner.SecretScanError):
                scanner._load_exceptions(self.write_exceptions([self.exception(**change)]))
        with self.assertRaisesRegex(scanner.SecretScanError, 'duplicate'):
            scanner._load_exceptions(self.write_exceptions([entry, entry]))

    def test_exact_exception_does_not_waive_changed_value_path_rule_line_or_commit(self):
        entry = self.exception()
        approved, _ = scanner._load_exceptions(self.write_exceptions([entry]))
        original = scanner.Finding(entry['rule'], entry['path'], entry['commit'], entry['line'], entry['value_sha256'])
        blocking, waived = scanner.apply_exceptions([original], approved)
        self.assertEqual((len(blocking), len(waived)), (0, 1))
        for change in ({'secret_sha256': 'b' * 64}, {'path': 'tests/other.py'},
                {'rule': 'generic-api-key'}, {'line': 4}, {'commit': '2' * 40}):
            fields = dict(rule=original.rule, path=original.path, commit=original.commit,
                line=original.line, secret_sha256=original.secret_sha256, **{})
            fields.update(change)
            with self.subTest(change=change):
                blocking, waived = scanner.apply_exceptions([scanner.Finding(**fields)], approved)
                self.assertEqual((len(blocking), len(waived)), (1, 0))

    def test_provider_rules_cannot_have_exceptions(self):
        for rule in ('github-pat', 'generic-api-key', 'aws-access-token', 'sensitive-user-data-file'):
            with self.subTest(rule=rule), self.assertRaises(scanner.SecretScanError):
                scanner._load_exceptions(self.write_exceptions([self.exception(rule=rule)]))

    def test_history_reuses_only_exact_resolved_blob_and_preserves_real_commit(self):
        entry = self.exception(commit='INDEX:' + '2' * 40)
        approved, _ = scanner._load_exceptions(self.write_exceptions([entry]))
        fields = dict(rule=entry['rule'], path=entry['path'], commit='1' * 40,
            line=entry['line'], secret_sha256=entry['value_sha256'], blob_id='2' * 40)
        blocking, waived = scanner.apply_exceptions([scanner.Finding(**fields)], approved)
        self.assertEqual((len(blocking), len(waived)), (0, 1))
        self.assertEqual(waived[0]['commit'], '1' * 40)
        for change in ({'blob_id': '3' * 40}, {'blob_id': ''}, {'path': 'tests/other.py'},
                {'rule': 'mediaflow-sensitive-url'}, {'line': 4}, {'secret_sha256': 'b' * 64}):
            with self.subTest(change=change):
                changed = dict(fields, **change)
                blocking, waived = scanner.apply_exceptions([scanner.Finding(**changed)], approved)
                self.assertEqual((len(blocking), len(waived)), (1, 0))

    def test_report_supplied_blob_identity_cannot_replace_git_tree_resolution(self):
        row = self.row(BlobID='3' * 40)
        found, _ = self.detect([row], history_blobs={('1' * 40, row['File']): '2' * 40})
        self.assertEqual(found[0].blob_id, '2' * 40)
        with self.assertRaisesRegex(scanner.SecretScanError, 'missing_from_commit_tree'):
            self.detect([row], history_blobs={})

    def test_rules_extend_provider_defaults_and_cannot_contain_allowlists(self):
        self.assertIn(b'useDefault = true', scanner._load_config(scanner.CONFIG))
        value = self.file('invalid.toml', 'title="bad"\n[extend]\nuseDefault=false\n')
        with self.assertRaises(scanner.SecretScanError):
            scanner._load_config(value)
        text = scanner.CONFIG.read_text('utf-8') + '\n[allowlist]\npaths=["tests/"]\n'
        with self.assertRaises(scanner.SecretScanError):
            scanner._load_config(self.file('invalid.toml', text))

    def test_tool_digest_gate_precedes_execution(self):
        tool = self.file('tool.bin', b'SYNTHETIC-NOT-A-GITLEAKS-BINARY')
        with patch.object(scanner, '_run') as execute:
            with self.assertRaisesRegex(scanner.SecretScanError, 'hash_not_approved'):
                scanner.validate_tool(tool, {})
        execute.assert_not_called()

    def test_version_is_pinned_even_when_binary_hash_matches(self):
        tool = self.file('tool.bin', b'SYNTHETIC-VERSION-FIXTURE')
        digest = hashlib.sha256(tool.read_bytes()).hexdigest()
        with patch.object(scanner, 'GITLEAKS_BINARY_SHA256', {digest}), \
                patch.object(scanner, '_run', return_value=(0, b'8.0.0\n')):
            with self.assertRaisesRegex(scanner.SecretScanError, 'version_not_approved'):
                scanner.validate_tool(tool, {})

    def test_binary_pins_match_reviewed_toolchain_manifest(self):
        toolchain = json.loads((scanner.ROOT / 'security/toolchain.json').read_text('utf-8'))
        record = toolchain['binary_tools']['gitleaks']
        self.assertEqual(scanner.GITLEAKS_VERSION, record['version'])
        self.assertEqual(scanner.GITLEAKS_BINARY_SHA256,
            {value['executable_sha256'] for value in record['platforms'].values()})

    def test_child_environment_has_no_host_tokens_or_config_overrides(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'SYNTHETIC-TOKEN',
                'GITLEAKS_CONFIG': 'SYNTHETIC-LOOSE-CONFIG', 'GIT_CONFIG_COUNT': '99'}):
            result = scanner._environment(self.directory)
        self.assertNotIn('GH_TOKEN', result)
        self.assertNotIn('GITLEAKS_CONFIG', result)
        self.assertEqual(result['GIT_CONFIG_COUNT'], '0')
        self.assertEqual(result['GIT_NO_REPLACE_OBJECTS'], '1')
        self.assertEqual(result['GIT_TERMINAL_PROMPT'], '0')

    def test_cli_failure_is_fixed_and_never_serializes_exception_input(self):
        with patch.object(scanner, 'scan', side_effect=ValueError('SYNTHETIC-PRIVATE-DIAGNOSTIC')):
            captured = io.StringIO()
            with redirect_stdout(captured):
                code = scanner.main(['--gitleaks', 'tool', '--output', str(self.directory / 'report')])
        self.assertEqual(code, 2)
        serialized = captured.getvalue() + (self.directory / 'report/report.json').read_text('utf-8')
        self.assertNotIn('SYNTHETIC-PRIVATE-DIAGNOSTIC', serialized)
        self.assertIn('secret_scan_stopped_without_logging_scanned_data', serialized)

    def test_existing_output_is_never_used_or_changed(self):
        existing = self.directory / 'previous-report'
        existing.mkdir()
        keep = existing / 'keep.txt'
        keep.write_text('previous immutable evidence', 'utf-8')
        with patch.object(scanner, 'scan') as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(scanner.main(['--gitleaks', 'tool', '--output', str(existing)]), 2)
        execute.assert_not_called()
        self.assertEqual(list(existing.iterdir()), [keep])
        self.assertEqual(keep.read_text('utf-8'), 'previous immutable evidence')


@unittest.skipUnless(shutil.which('git') and os.environ.get('MEDIAFLOW_GITLEAKS'),
    'Real pinned binary supplied explicitly via MEDIAFLOW_GITLEAKS')
class RealPinnedGitleaksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='MediaFlow-gitleaks-real-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git('init', '--quiet')
        self.git('config', 'user.name', 'Synthetic scanner fixture')
        self.git('config', 'user.email', 'scanner-fixture@example.invalid')
        (self.root / 'main.py').write_text('print("clean synthetic fixture")\n', 'utf-8')
        self.git('add', '--', 'main.py')
        self.git('commit', '--quiet', '-m', 'synthetic clean fixture')

    def git(self, *arguments):
        result = subprocess.run(['git', '-C', str(self.root), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, 'Synthetic fixture Git setup failed')
        return result.stdout.decode('utf-8').strip()

    def scan(self, **kwargs):
        return scanner.scan(self.root, os.environ['MEDIAFLOW_GITLEAKS'], **kwargs)

    def test_real_clean_scan_and_staged_secret_hidden_by_clean_worktree(self):
        self.assertTrue(self.scan()['passed'])
        marker = 'gh' + 'p_' + 'SYNTHETICONLY' * 4
        source = self.root / 'staged.txt'
        source.write_text('access_' + 'token = "' + marker + '"\n', 'utf-8')
        self.git('add', '--', source.name)
        source.write_text('clean unstaged replacement\n', 'utf-8')
        result = self.scan()
        self.assertFalse(result['passed'])
        self.assertTrue(any(f['commit'].startswith('INDEX:') for f in result['findings']))
        self.assertNotIn(marker, json.dumps(result))

    def test_real_all_refs_find_deleted_secret_on_an_unmerged_branch(self):
        branch = self.git('branch', '--show-current')
        self.git('checkout', '--quiet', '-b', 'synthetic-history-only')
        marker = 'gh' + 'p_' + 'SYNTHETICONLY' * 4
        source = self.root / 'past.txt'
        source.write_text('access_' + 'token = "' + marker + '"\n', 'utf-8')
        self.git('add', '--', source.name)
        self.git('commit', '--quiet', '-m', 'synthetic historical negative fixture')
        secret_commit = self.git('rev-parse', 'HEAD')
        self.git('rm', '--quiet', '--', source.name)
        self.git('commit', '--quiet', '-m', 'remove synthetic negative fixture')
        self.git('checkout', '--quiet', branch)
        result = self.scan()
        self.assertFalse(result['passed'])
        self.assertTrue(any(f['commit'] == secret_commit and f['path'] == 'past.txt' for f in result['findings']))
        self.assertNotIn(marker, json.dumps(result))

    def test_real_inline_allow_and_sensitive_database_have_no_implicit_exception(self):
        marker = 'gh' + 'p_' + 'SYNTHETICONLY' * 4
        (self.root / 'fixture.txt').write_text('access_' + 'token = "' + marker + '" # gitleaks:allow\n', 'utf-8')
        (self.root / 'innocent.txt').write_bytes(b'SQLite format 3\0SYNTHETIC-NOT-A-USER-DATABASE')
        self.git('add', '--', 'fixture.txt', 'innocent.txt')
        result = self.scan()
        self.assertFalse(result['passed'])
        self.assertTrue(any(f['type'] == 'sensitive-database-content' for f in result['findings']))
        self.assertTrue(any(f['path'] == 'fixture.txt' for f in result['findings']))
        self.assertNotIn(marker, json.dumps(result))

    def test_real_shallow_history_stops_before_claiming_a_complete_scan(self):
        (self.root / '.git/shallow').write_text(self.git('rev-parse', 'HEAD') + '\n', 'ascii')
        with self.assertRaisesRegex(scanner.SecretScanError, 'full_history_required'):
            self.scan()

    def test_real_ignore_file_cannot_silently_waive_any_findings(self):
        (self.root / '.gitleaksignore').write_text('*\n', 'utf-8')
        with self.assertRaisesRegex(scanner.SecretScanError, 'ignore_is_forbidden'):
            self.scan()

    def test_real_staged_symlink_is_blocked_without_following_target(self):
        result = subprocess.run(['git', '-C', str(self.root), 'hash-object', '-w', '--stdin'],
            input=b'../../synthetic-unread-private-target', stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0)
        blob = result.stdout.decode('ascii').strip()
        self.git('update-index', '--add', '--cacheinfo', '120000', blob, 'link.py')
        scanned = self.scan()
        self.assertFalse(scanned['passed'])
        self.assertTrue(any(f['type'] == 'git-symlink-or-submodule-not-scanned'
            and f['path'] == 'link.py' for f in scanned['findings']))

    def test_real_force_staged_ignored_secret_is_still_scanned(self):
        marker = 'gh' + 'p_' + 'SYNTHETICONLY' * 4
        (self.root / '.gitignore').write_text('ignored.txt\n', 'utf-8')
        (self.root / 'ignored.txt').write_text('access_' + 'token = "' + marker + '"\n', 'utf-8')
        self.git('add', '--', '.gitignore')
        self.git('add', '--force', '--', 'ignored.txt')
        result = self.scan()
        self.assertFalse(result['passed'])
        self.assertTrue(any(f['path'] == 'ignored.txt' for f in result['findings']))
        self.assertNotIn(marker, json.dumps(result))

    def test_real_secret_introduced_only_in_merge_commit_is_scanned(self):
        branch = self.git('branch', '--show-current')
        self.git('checkout', '--quiet', '-b', 'synthetic-merge-side')
        (self.root / 'side.txt').write_text('synthetic unrelated change\n', 'utf-8')
        self.git('add', '--', 'side.txt')
        self.git('commit', '--quiet', '-m', 'synthetic side branch')
        self.git('checkout', '--quiet', branch)
        self.git('merge', '--quiet', '--no-ff', '--no-commit', 'synthetic-merge-side')
        marker = 'gh' + 'p_' + 'SYNTHETICONLY' * 4
        source = self.root / 'merge-only.txt'
        source.write_text('access_' + 'token = "' + marker + '"\n', 'utf-8')
        self.git('add', '--', source.name)
        self.git('commit', '--quiet', '-m', 'synthetic merge-only negative fixture')
        merge_commit = self.git('rev-parse', 'HEAD')
        self.git('rm', '--quiet', '--', source.name)
        self.git('commit', '--quiet', '-m', 'remove synthetic merge-only fixture')
        result = self.scan()
        self.assertFalse(result['passed'])
        self.assertTrue(any(f['commit'] == merge_commit and f['path'] == source.name for f in result['findings']))
        self.assertNotIn(marker, json.dumps(result))

    def test_real_reviewed_blob_survives_merge_but_changed_blob_still_blocks(self):
        branch = self.git('branch', '--show-current')
        self.git('checkout', '--quiet', '-b', 'synthetic-reviewed-side')
        marker = 'unit-' + 'a' * 20  # Deliberately low-entropy synthetic literal.
        source = self.root / 'fixture.py'
        source.write_text('pass' + 'word = "' + marker + '"\n', 'utf-8')
        self.git('add', '--', source.name)
        blob = self.git('rev-parse', ':' + source.name)
        review = self.root / 'exact-review.json'
        review.write_text(json.dumps({'schema_version': 1, 'exceptions': [{
            'rule': 'mediaflow-credential-literal', 'path': source.name,
            'commit': 'INDEX:' + blob, 'line': 1,
            'value_sha256': hashlib.sha256(marker.encode()).hexdigest(),
            'classification': 'synthetic_test_data', 'reviewed_by': 'unit-test maintainer',
            'reason': 'Exact generated low-entropy synthetic fixture, never an active credential.'}]}), 'utf-8')
        self.assertTrue(self.scan(exceptions=review)['passed'])
        self.git('commit', '--quiet', '-m', 'known synthetic reviewed blob')
        introduced = self.git('rev-parse', 'HEAD')
        self.git('checkout', '--quiet', branch)
        self.git('merge', '--quiet', '--no-ff', 'synthetic-reviewed-side', '-m', 'merge same reviewed blob')
        merged = self.git('rev-parse', 'HEAD')
        accepted = self.scan(exceptions=review)
        self.assertTrue(accepted['passed'])
        actual_commits = {f['commit'] for f in accepted['synthetic_exceptions_used']}
        self.assertIn(introduced, actual_commits)
        self.assertIn(merged, actual_commits)
        source.write_text(source.read_text('utf-8') + '# different immutable blob\n', 'utf-8')
        self.git('add', '--', source.name)
        changed = self.git('rev-parse', ':' + source.name)
        self.assertNotEqual(blob, changed)
        blocked = self.scan(exceptions=review)
        self.assertFalse(blocked['passed'])
        self.assertTrue(any(f['commit'] == 'INDEX:' + changed and f['path'] == source.name
            for f in blocked['findings']))
        self.assertNotIn(marker, json.dumps(blocked))


if __name__ == '__main__':
    unittest.main()
