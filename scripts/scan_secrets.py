"""Fail-closed Gitleaks gate for all Git refs and the exact current index.

Only public Git objects are inspected. Detector JSON stays in process memory;
reports deliberately discard Secret, Match, author, email and commit messages.
No credential helper, user database or publisher vault is opened by this gate.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
GITLEAKS_VERSION = '8.30.1'
# Filled only from the reviewed official release binary; never from user input.
GITLEAKS_BINARY_SHA256 = frozenset((
    '17157e2ee8b76fc8b1d8bee607a250e34b8a8023c8bc81822d4b5ee4d78fcb7c',
    '88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509',
))
CONFIG = ROOT / 'security/gitleaks.toml'
EXCEPTIONS = ROOT / 'security/secret-exceptions.json'
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_REPORT = 32 * 1024 * 1024
MAX_FILES = 100000
MAX_COMMITS = 50000
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
RULE = re.compile(r'[a-z0-9][a-z0-9_-]{0,100}\Z')
LEAK_EXIT = 23
CUSTOM_RULES = frozenset(('mediaflow-credential-literal', 'mediaflow-sensitive-url'))


class SecretScanError(RuntimeError):
    """A fixed diagnostic code, never detector output or a scanned value."""


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    commit: str
    line: int
    secret_sha256: str = ''
    blob_id: str = ''

    def public(self):
        return {'type': self.rule, 'commit': self.commit,
                'path': self.path, 'line': self.line}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _ordinary(path):
    path = Path(path).absolute()
    try:
        for item in (path, *path.parents):
            metadata = item.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                raise SecretScanError('symlink_or_reparse_input')
        if not path.is_file():
            raise SecretScanError('input_not_regular_file')
    except OSError:
        raise SecretScanError('input_unavailable') from None
    return path


def _git_path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or '\\' in value or ':' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise SecretScanError('git_path_invalid')
    path = PurePosixPath(value)
    if (path.is_absolute() or any(p in ('', '.', '..') or p.casefold() == '.git'
            or p.rstrip(' .') != p or re.match(r'(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', p)
            for p in value.split('/'))):
        raise SecretScanError('git_path_invalid')
    return path.as_posix()


def _environment(home):
    # No host auth tokens, credential settings, CA overrides or Gitleaks config.
    result = {key: value for key, value in os.environ.items() if key.upper() in {
        'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
        'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS'}}
    result.update(HOME=str(home), USERPROFILE=str(home), LANG='C.UTF-8',
        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1',
        GIT_CONFIG_COUNT='0', GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0')
    return result


def _run(arguments, *, env, cwd=None, data=None, timeout=90, output_limit=MAX_REPORT):
    try:
        result = subprocess.run(list(map(str, arguments)), input=data,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=cwd, env=env,
            timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise SecretScanError('command_timeout') from None
    except OSError:
        raise SecretScanError('command_unavailable') from None
    if len(result.stdout) > output_limit:
        raise SecretScanError('command_output_exceeds_bound')
    return result.returncode, result.stdout


def _git(root, arguments, env, data=None, output_limit=MAX_REPORT):
    code, output = _run(['git', '-C', root, *arguments], env=env,
                        data=data, output_limit=output_limit)
    if code:
        raise SecretScanError('git_objects_unavailable_or_invalid')
    return output


def _refs(root, env):
    return _git(root, ['show-ref', '--head'], env)


def _entries(raw, *, index):
    entries = []
    for record in raw.split(b'\0'):
        if not record:
            continue
        try:
            header, name = record.split(b'\t', 1)
            fields = header.decode('ascii').split(' ')
            if len(fields) != 3:
                raise ValueError()
            mode, identity = fields[:2] if index else (fields[0], fields[2])
            if (index and fields[2] != '0') or (not index and fields[1] not in ('blob', 'commit')):
                raise ValueError()
            if not SHA.fullmatch(identity):
                raise ValueError()
            entries.append((_git_path(name.decode('utf-8')), mode, identity))
        except (ValueError, UnicodeError):
            raise SecretScanError('git_entry_invalid_or_unmerged') from None
    if len(entries) > MAX_FILES:
        raise SecretScanError('git_file_count_exceeds_bound')
    names = [name.casefold() for name, _, _ in entries]
    if len(set(names)) != len(names):
        raise SecretScanError('git_path_case_collision')
    return entries


def sensitive_path(name):
    path = PurePosixPath(name)
    parts = [p.casefold() for p in path.parts]
    basename = parts[-1]
    if any(p in {'browserprofile', 'browserprofiles', 'ebwebview', 'userdata',
            'user_data', 'publisher-keys', '.ssh', '.aws', '.azure', '.gnupg'} for p in parts[:-1]):
        return 'sensitive-user-data-directory'
    if (basename in {'cookies', 'cookies.txt', 'login data', 'web data', 'local state',
            '.netrc', '_netrc', 'credentials', 'credentials.json', 'settings.json',
            'tasks.json', 'task-state.json', 'recovery.json', 'id_rsa', 'id_ed25519'}
            or basename == '.env' or basename.startswith('.env.')):
        return 'sensitive-user-data-file'
    if path.suffix.casefold() in {'.db', '.sqlite', '.sqlite3', '.dpapi', '.pfx', '.p12', '.key', '.pem'}:
        return 'sensitive-database-or-key-file'
    if path.suffix.casefold() in {'.zip', '.7z', '.rar', '.gz', '.tgz', '.tar', '.bz2', '.xz', '.zst'}:
        return 'sensitive-archive-needs-review'
    if basename.endswith(('.sqlite-wal', '.sqlite-shm', '.db-wal', '.db-shm')):
        return 'sensitive-database-runtime-file'
    if basename == '.gitleaksignore':
        return 'unreviewed-detector-ignore-file'
    return ''


def sensitive_content(data):
    if data.startswith(b'SQLite format 3\0'):
        return 'sensitive-database-content'
    if data.lstrip().startswith(b'# Netscape HTTP Cookie File'):
        return 'sensitive-cookie-jar-content'
    if data.startswith((b'PK\x03\x04', b'7z\xbc\xaf\x27\x1c', b'Rar!\x1a\x07', b'\x1f\x8b')):
        return 'sensitive-archive-content'
    if re.match(rb'\s*-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----', data):
        return 'sensitive-private-key-file-content'
    return ''


def _blobs(root, identities, env):
    """Two bounded, read-only Git calls instead of starting Git per source file."""
    wanted = sorted(set(identities))
    if not wanted:
        return {}
    request = ''.join(identity + '\n' for identity in wanted).encode('ascii')
    rows = _git(root, ['cat-file', '--batch-check'], env, request).splitlines()
    if len(rows) != len(wanted):
        raise SecretScanError('git_blob_metadata_incomplete')
    sizes = []
    for identity, row in zip(wanted, rows):
        try:
            actual, kind, length = row.decode('ascii').split(' ')
            size = int(length)
            if actual != identity or kind != 'blob' or size < 0 or size > MAX_FILE:
                raise ValueError()
            sizes.append(size)
        except (UnicodeError, ValueError):
            raise SecretScanError('git_blob_metadata_invalid_or_exceeds_bound') from None
    if sum(sizes) > MAX_TOTAL:
        raise SecretScanError('git_content_exceeds_scan_bound')
    # No filters, textconv or checkout: each object is returned verbatim.
    raw = _git(root, ['cat-file', '--batch'], env, request,
               output_limit=MAX_TOTAL + 128 * len(wanted))
    data, offset = {}, 0
    for identity, size in zip(wanted, sizes):
        newline = raw.find(b'\n', offset)
        if newline < 0 or raw[offset:newline] != f'{identity} blob {size}'.encode('ascii'):
            raise SecretScanError('git_blob_batch_header_invalid')
        offset = newline + 1
        if len(raw[offset:offset + size]) != size or raw[offset + size:offset + size + 1] != b'\n':
            raise SecretScanError('git_blob_read_incomplete')
        data[identity] = raw[offset:offset + size]
        offset += size + 1
    if offset != len(raw):
        raise SecretScanError('git_blob_batch_trailing_data')
    return data


def _load_config(path):
    data = _ordinary(path).read_bytes()
    try:
        value = tomllib.loads(data.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise SecretScanError('detector_config_invalid') from None
    if (set(value) != {'title', 'extend', 'rules'} or value['extend'] != {'useDefault': True}
            or not isinstance(value['rules'], list) or not value['rules']):
        raise SecretScanError('detector_config_may_not_disable_default_rules')
    for rule in value['rules']:
        if (not isinstance(rule, dict) or any(key not in {'id', 'description', 'regex', 'secretGroup', 'keywords'}
                for key in rule) or not RULE.fullmatch(str(rule.get('id', '')))):
            raise SecretScanError('detector_rule_or_allowlist_invalid')
    return data


def _load_exceptions(path):
    data = _ordinary(path).read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise SecretScanError('exception_document_exceeds_bound')
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError):
        raise SecretScanError('exception_document_invalid') from None
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'exceptions'}
            or value['schema_version'] != 1 or not isinstance(value['exceptions'], list)):
        raise SecretScanError('exception_document_invalid')
    result = set()
    for entry in value['exceptions']:
        if (not isinstance(entry, dict) or set(entry) != {'rule', 'path', 'commit', 'line',
                'value_sha256', 'classification', 'reason', 'reviewed_by'}
                or entry.get('rule') not in CUSTOM_RULES
                or not re.fullmatch(r'(?:INDEX:)?(?:[0-9a-f]{40}|[0-9a-f]{64})', str(entry.get('commit', '')))
                or not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('value_sha256', '')))
                or not isinstance(entry.get('path'), str) or any(c in entry['path'] for c in '*?[')
                or type(entry.get('line')) is not int or entry['line'] < 1
                or entry.get('classification') != 'synthetic_test_data'
                or not isinstance(entry.get('reason'), str) or len(entry['reason'].strip()) < 20
                or not isinstance(entry.get('reviewed_by'), str) or not entry['reviewed_by'].strip()):
            raise SecretScanError('exception_must_bind_exact_synthetic_finding')
        key = (entry['rule'], _git_path(entry['path']), entry['commit'], entry['line'], entry['value_sha256'])
        if key in result:
            raise SecretScanError('duplicate_secret_exception')
        result.add(key)
    return result, hashlib.sha256(data).hexdigest()


def apply_exceptions(findings, approved):
    blocking, waived = [], []
    for finding in sorted(set(findings), key=lambda f: (f.commit, f.path, f.line, f.rule)):
        key = (finding.rule, finding.path, finding.commit, finding.line, finding.secret_sha256)
        exact = key in approved
        # A history label is never rewritten. Reuse only a matching immutable
        # blob resolved from that real commit's tree, plus the other four fields.
        if (not exact and SHA.fullmatch(finding.commit) and SHA.fullmatch(finding.blob_id)):
            blob_key = (finding.rule, finding.path, 'INDEX:' + finding.blob_id,
                        finding.line, finding.secret_sha256)
            exact = blob_key in approved
        allowed = finding.rule in CUSTOM_RULES and finding.secret_sha256 and exact
        (waived if allowed else blocking).append(finding.public())
    return blocking, waived


def validate_tool(path, env):
    path = _ordinary(path)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest not in GITLEAKS_BINARY_SHA256:
        raise SecretScanError('gitleaks_binary_hash_not_approved')
    code, raw = _run([path, 'version'], env=env, timeout=10)
    if code or raw.decode('ascii', errors='replace').strip().removeprefix('v') != GITLEAKS_VERSION:
        raise SecretScanError('gitleaks_version_not_approved')
    return path, digest


def _detect(tool, source, *, history, config, env, commits, index, history_blobs=None):
    arguments = [tool, 'git' if history else 'dir', '--no-banner', '--no-color',
        '--log-level', 'error', '--config', config, '--report-format', 'json',
        '--report-path', '-', '--exit-code', str(LEAK_EXIT), '--ignore-gitleaks-allow',
        '--max-decode-depth', '5', '--max-archive-depth', '3', '--timeout', '600']
    if history:
        arguments += ['--log-opts=--all --full-history --root -m --no-renames --no-ext-diff --no-textconv']
    arguments += [source]
    code, raw = _run(arguments, env=env, cwd=source, timeout=630)
    if code not in (0, LEAK_EXIT):
        raise SecretScanError('gitleaks_scan_failed_or_incomplete')
    try:
        rows = json.loads(raw)
    except (UnicodeError, ValueError):
        raise SecretScanError('gitleaks_report_invalid') from None
    if rows is None:
        rows = []
    if not isinstance(rows, list) or bool(rows) != (code == LEAK_EXIT):
        raise SecretScanError('gitleaks_exit_and_report_disagree')
    findings = []
    for row in rows:
        if not isinstance(row, dict):
            raise SecretScanError('gitleaks_report_invalid')
        rule, line, secret = row.get('RuleID'), row.get('StartLine'), row.get('Secret')
        if (not RULE.fullmatch(str(rule)) or type(line) is not int or line < 1
                or not isinstance(secret, str) or not secret):
            raise SecretScanError('gitleaks_finding_invalid')
        name = row.get('File')
        if not isinstance(name, str):
            raise SecretScanError('gitleaks_path_invalid')
        if history:
            name = _git_path(name)
            commit = row.get('Commit')
            if commit not in commits:
                raise SecretScanError('gitleaks_commit_not_in_full_history')
            blob_id = ''
            if history_blobs is not None:
                blob_id = history_blobs.get((commit, name), '')
                if not SHA.fullmatch(blob_id):
                    raise SecretScanError('gitleaks_history_path_missing_from_commit_tree')
        else:
            candidate = Path(name)
            if candidate.is_absolute():
                try:
                    name = candidate.relative_to(source).as_posix()
                except ValueError:
                    raise SecretScanError('gitleaks_index_path_outside_snapshot') from None
            else:
                name = name.replace('\\', '/')
            name = _git_path(name)
            if name not in index:
                raise SecretScanError('gitleaks_index_path_not_in_index')
            commit = 'INDEX:' + index[name]
            blob_id = index[name]
        findings.append(Finding(rule, name, commit, line,
            hashlib.sha256(secret.encode('utf-8')).hexdigest(), blob_id))
    return findings


def scan(root, gitleaks, *, config=CONFIG, exceptions=EXCEPTIONS):
    root = Path(root).resolve()
    findings = []
    with tempfile.TemporaryDirectory(prefix='MediaFlow-secret-scan-') as temporary:
        private = Path(temporary)
        env = _environment(private)
        tool, tool_hash = validate_tool(gitleaks, env)
        config_data = _load_config(config)
        isolated_config = private / 'rules.toml'
        isolated_config.write_bytes(config_data)
        approved, exceptions_hash = _load_exceptions(exceptions)
        if _git(root, ['rev-parse', '--is-shallow-repository'], env).strip() != b'false':
            raise SecretScanError('full_history_required_shallow_repository')
        top = _git(root, ['rev-parse', '--show-toplevel'], env).decode('utf-8').strip()
        if Path(top).resolve() != root:
            raise SecretScanError('root_must_be_git_toplevel')
        graft = _git(root, ['rev-parse', '--git-path', 'info/grafts'], env).decode('utf-8').strip()
        graft = Path(graft) if Path(graft).is_absolute() else root / graft
        if graft.exists():
            raise SecretScanError('git_grafts_are_not_supported')
        if (root / '.gitleaksignore').exists():
            raise SecretScanError('worktree_gitleaks_ignore_is_forbidden')
        refs_before = _refs(root, env)
        raw_index = _git(root, ['ls-files', '--stage', '-z'], env)
        index_entries = _entries(raw_index, index=True)
        commits_raw = _git(root, ['rev-list', '--all'], env).splitlines()
        if not commits_raw or len(commits_raw) > MAX_COMMITS:
            raise SecretScanError('git_history_empty_or_exceeds_bound')
        commits = set()
        for identity in commits_raw:
            try:
                commit = identity.decode('ascii')
            except UnicodeError:
                raise SecretScanError('git_commit_invalid') from None
            if not SHA.fullmatch(commit):
                raise SecretScanError('git_commit_invalid')
            commits.add(commit)
        seen, history, history_blobs = set(), [], {}
        for commit in sorted(commits):
            entries = _entries(_git(root, ['ls-tree', '-r', '-z', commit], env), index=False)
            history_blobs.update({(commit, name): identity for name, _, identity in entries})
            fresh = [entry for entry in entries if (entry[0], entry[1], entry[2]) not in seen]
            seen.update(fresh)
            if len(seen) > MAX_FILES:
                raise SecretScanError('git_history_file_versions_exceed_bound')
            history.append((commit, fresh))
        blobs = _blobs(root, [identity for _, entries in history for _, mode, identity in entries
            if mode in ('100644', '100755')] + [identity for _, mode, identity in index_entries
            if mode in ('100644', '100755')], env)
        blob_types = {identity: sensitive_content(data) for identity, data in blobs.items()}

        def inspect(entries, commit, destination=None):
            for name, mode, identity in entries:
                label = commit if destination is None else 'INDEX:' + identity
                if mode not in ('100644', '100755'):
                    findings.append(Finding('git-symlink-or-submodule-not-scanned', name, label, 0))
                    continue
                rule = sensitive_path(name)
                if rule:
                    findings.append(Finding(rule, name, label, 0))
                if blob_types[identity]:
                    findings.append(Finding(blob_types[identity], name, label, 0))
                if destination is not None and name != '.gitleaksignore':
                    target = destination.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Data from the exact index object, never the worktree file.
                    target.write_bytes(blobs[identity])

        for commit, fresh in history:
            inspect(fresh, commit)
        index_directory = private / 'index'
        index_directory.mkdir()
        inspect(index_entries, '', destination=index_directory)
        index = {name: identity for name, mode, identity in index_entries if mode in ('100644', '100755')}
        findings += _detect(tool, root, history=True, config=isolated_config,
                            env=env, commits=commits, index=index, history_blobs=history_blobs)
        findings += _detect(tool, index_directory, history=False, config=isolated_config,
                            env=env, commits=commits, index=index)
        if (_refs(root, env) != refs_before or _git(root, ['ls-files', '--stage', '-z'], env) != raw_index
                or (root / '.gitleaksignore').exists()):
            raise SecretScanError('git_refs_or_index_changed_during_scan')
        # Reject a changed tool after execution as well; no report can claim pass.
        validate_tool(tool, env)
        blocking, waived = apply_exceptions(findings, approved)
        return {'schema_version': 1, 'passed': not blocking,
            'selection': 'all_git_refs_and_exact_index', 'complete': True,
            'tool': {'name': 'gitleaks', 'version': GITLEAKS_VERSION, 'sha256': tool_hash},
            'config_sha256': hashlib.sha256(config_data).hexdigest(),
            'exceptions_sha256': exceptions_hash, 'commit_count': len(commits),
            'history_file_versions': len(seen), 'index_file_count': len(index_entries),
            'index_sha256': hashlib.sha256(raw_index).hexdigest(),
            'refs_sha256': hashlib.sha256(refs_before).hexdigest(),
            'blocking_count': len(blocking), 'exact_synthetic_exception_count': len(waived),
            'findings': blocking, 'synthetic_exceptions_used': waived,
            'finished_at': datetime.now(timezone.utc).isoformat()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--gitleaks', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.absolute()
    created = False
    try:
        if output.exists():
            raise SecretScanError('output_must_be_a_new_directory')
        output.mkdir(parents=True)
        created = True
        result = scan(args.root, args.gitleaks)
        code = 0 if result['passed'] else 1
    except SecretScanError as error:
        result = {'schema_version': 1, 'passed': False, 'complete': False, 'error': str(error)}
        code = 2
    except Exception:
        result = {'schema_version': 1, 'passed': False, 'complete': False,
                  'error': 'secret_scan_stopped_without_logging_scanned_data'}
        code = 2
    # Never overwrite earlier evidence, including when the selected output exists.
    if created and not (output / 'report.json').exists():
        (output / 'report.json').write_bytes(_json(result) + b'\n')
    print(json.dumps({key: result[key] for key in ('passed', 'complete')}, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
