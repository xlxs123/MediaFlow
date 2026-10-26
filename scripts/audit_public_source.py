"""Audit the exact proposed public Git files without printing their contents.

This is a source disclosure gate, not a guarantee that arbitrary credentials or
third-party licensing problems can be identified automatically. Findings needing
human interpretation require a review bound to the entire file's SHA-256.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import unicodedata


MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_FILES = 10000
SOURCE_DIRECTORIES = frozenset(('core', 'ui', 'utils', 'tests', 'scripts',
    'native', 'assets', 'tools', 'release-workflows', '.github', 'docs'))
TEXT_EXTENSIONS = frozenset(('.py', '.ps1', '.cmd', '.bat', '.cs', '.manifest',
    '.config', '.json', '.txt', '.md', '.yml', '.yaml', '.toml', '.cfg', '.ini', '.svg'))
ROOT_NAMES = frozenset(('main.py', 'build.ps1', 'MediaFlow.spec',
    'M3U8 Downloader.spec', 'LICENSE', 'requirements.txt', 'requirements-build.txt',
    'requirements-security.txt', 'MODIFIED_FILES.txt', '.gitignore',
    '.gitattributes', '.editorconfig'))
PUBLIC_SECURITY_FILES = frozenset(('security/dependency-policy.json',
    'security/gitleaks.toml', 'security/sbom-policy.json',
    'security/secret-exceptions.json', 'security/toolchain.json', 'security/codeql-reviews.json',
    'security/authenticode-policy.json', 'security/native-component-evidence.json'))
PROHIBITED_DIRECTORIES = frozenset(('.git', '.venv', 'venv', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.sdk', 'node_modules', 'browserprofile',
    'browserprofiles', 'ebwebview', 'userdata', 'user_data', 'downloads',
    'verification', 'logs', 'cache', 'recovery', 'publisher-keys'))
PROHIBITED_EXTENSIONS = frozenset(('.exe', '.dll', '.pyd', '.pyc', '.zip',
    '.7z', '.tar', '.gz', '.whl', '.mp4', '.mkv', '.webm', '.mov', '.avi',
    '.mp3', '.m4a', '.aac', '.flac', '.wav', '.ts', '.m4s', '.srt', '.vtt',
    '.ass', '.db', '.sqlite', '.sqlite3', '.log', '.pem', '.key', '.pfx',
    '.p12', '.part', '.tmp', '.bak', '.dpapi'))
PROHIBITED_NAMES = frozenset(('.env', '.netrc', '_netrc', 'cookies.txt',
    'cookies', 'login data', 'web data', 'local state', 'settings.json',
    'tasks.json', 'task-state.json', 'recovery.json', 'credentials.json'))
REVIEW_CLASSES = frozenset(('synthetic_test_data', 'security_code',
    'documented_placeholder'))

_PATTERNS = (
    ('private_key_material', 'error', re.compile(
        r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{48,}')),
    ('github_credential', 'error', re.compile(
        r'\b(?:github_pat_[A-Za-z0-9_]{22,}|gh[pousr]_[A-Za-z0-9]{30,})\b')),
    ('cloud_access_key', 'error', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('personal_windows_path', 'personal', re.compile(
        r'\b[A-Za-z]:[\\/]+(?:Users[\\/]+(?!Public\b|Default\b|[<{%])[^\\/\s\"\'<>]+|'
        r'create[ ]program(?:-codex)?\b|develop[\\/]|Git[\\/])', re.I)),
    ('credential_literal', 'review', re.compile(
        r'''(?i)["']?(?:password|passwd|cookie|authorization|access_token|api_key|client_secret)["']?\s*[:=]\s*(?:[rubf]{0,2})?["'][^"'\r\n]+["']''')),
    ('credential_header_literal', 'review', re.compile(
        r'''(?i)\b(?:cookie|authorization|proxy-authorization):\s+[^\s"']+''')),
    ('sensitive_url_literal', 'review', re.compile(
        r'(?i)[?&](?:token|signature|sign|x-amz-signature|x-amz-credential|mstoken|a_bogus|ac_signature)=[^\s\"\'<>]+')),
    ('url_credentials', 'review', re.compile(
        r'https?://[^/\s\"\']+:[^@/\s\"\']+@', re.I)),
    ('private_key_marker', 'review', re.compile(
        r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----')),
    ('private_key_generation', 'review', re.compile(
        r'(?:Ed25519PrivateKey\.generate\(|rsa\.generate_private_key\(|serialization\.BestAvailableEncryption\()')),
)


class AuditError(RuntimeError):
    """Fixed diagnostic only; never include a scanned value or tool stderr."""


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _path(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise AuditError('candidate_path_invalid')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise AuditError('candidate_path_invalid')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AuditError('candidate_path_invalid')
    if any(part.rstrip(' .') != part for part in path.parts):
        raise AuditError('candidate_path_invalid')
    return path.as_posix()


def _plain(path):
    current = path
    while True:
        information = current.lstat()
        if stat.S_ISLNK(information.st_mode) or getattr(information,
                'st_file_attributes', 0) & 0x400:
            raise AuditError('symlink_or_reparse_point')
        if current == current.parent:
            return
        current = current.parent


def _git(root, arguments, *, data=None):
    try:
        result = subprocess.run(['git', '-C', str(root), *arguments], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        raise AuditError('git_unavailable_or_timed_out') from None
    if result.returncode:
        raise AuditError('git_index_unreadable')
    return result.stdout


def _index_entries(root):
    value = _git(root, ['ls-files', '--stage', '-z'])
    result = []
    for record in value.split(b'\0'):
        if not record:
            continue
        try:
            header, path = record.split(b'\t', 1)
            mode, identity, stage = header.decode('ascii').split(' ')
            if stage != '0' or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', identity):
                raise ValueError()
            result.append((_path(path.decode('utf-8')), mode, identity))
        except (UnicodeError, ValueError):
            raise AuditError('git_index_entry_invalid_or_unmerged') from None
    return result, hashlib.sha256(value).hexdigest()


def _index_blobs(root, entries):
    wanted = [(path, mode, identity) for path, mode, identity in entries
              if mode in ('100644', '100755')]
    if not wanted:
        return {}
    request = ''.join(identity + '\n' for _, _, identity in wanted).encode('ascii')
    metadata = _git(root, ['cat-file', '--batch-check'], data=request).splitlines()
    if len(metadata) != len(wanted):
        raise AuditError('git_object_metadata_invalid')
    sizes = []
    for line, (_, _, identity) in zip(metadata, wanted):
        try:
            actual, kind, size = line.decode('ascii').split(' ')
            if actual != identity or kind != 'blob':
                raise ValueError()
            sizes.append(int(size))
        except (ValueError, UnicodeError):
            raise AuditError('git_object_metadata_invalid') from None
    if any(size < 0 or size > MAX_FILE for size in sizes) or sum(sizes) > MAX_TOTAL:
        raise AuditError('source_size_limit_exceeded')
    output = _git(root, ['cat-file', '--batch'], data=request)
    result = {}
    offset = 0
    for (path, _, identity), size in zip(wanted, sizes):
        newline = output.find(b'\n', offset)
        expected = f'{identity} blob {size}'.encode('ascii')
        if newline < 0 or output[offset:newline] != expected:
            raise AuditError('git_object_read_invalid')
        offset = newline + 1
        data = output[offset:offset + size]
        if len(data) != size or output[offset + size:offset + size + 1] != b'\n':
            raise AuditError('git_object_read_invalid')
        result[path] = data
        offset += size + 1
    if offset != len(output):
        raise AuditError('git_object_read_invalid')
    return result


def _worktree_data(root, name):
    path = root.joinpath(*PurePosixPath(name).parts)
    _plain(path)
    first = path.stat()
    if not stat.S_ISREG(first.st_mode):
        raise AuditError('not_a_regular_file')
    if first.st_size > MAX_FILE:
        raise AuditError('source_size_limit_exceeded')
    with path.open('rb') as stream:
        value = stream.read(MAX_FILE + 1)
    last = path.stat()
    fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(first, field) != getattr(last, field) for field in fields) or len(value) != first.st_size:
        raise AuditError('source_changed_during_read')
    return value


def _path_rule(name):
    path = PurePosixPath(name)
    components = [part.casefold() for part in path.parts]
    if any(part in PROHIBITED_DIRECTORIES or part.startswith(('dist-', 'build-', '_mei'))
           or part in ('dist', 'build') for part in components[:-1]):
        return 'private_or_generated_directory'
    if path.suffix.casefold() in PROHIBITED_EXTENSIONS or path.name.casefold() in PROHIBITED_NAMES:
        return 'private_binary_media_or_state_file'
    if path.name.casefold().endswith(('.sqlite-wal', '.sqlite-shm', '.db-wal', '.db-shm')):
        return 'database_runtime_file'
    if name in PUBLIC_SECURITY_FILES:
        return ''
    if len(path.parts) == 1:
        if name in ROOT_NAMES or path.suffix.casefold() == '.md':
            return ''
    elif path.parts[0] in SOURCE_DIRECTORIES:
        if path.name in ('.gitignore', '.gitattributes') or name == 'assets/app.ico' or path.suffix.casefold() in TEXT_EXTENSIONS:
            return ''
        if path.parts[0] == 'tools' and ('license' in path.name.casefold() or 'notice' in path.name.casefold()):
            return ''
    return 'outside_public_source_allowlist'


def _reviews(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('reviews'), list):
        raise AuditError('review_document_invalid')
    result = {}
    for entry in value['reviews']:
        if not isinstance(entry, dict):
            raise AuditError('review_document_invalid')
        name = _path(entry.get('path'))
        if name in result or not re.fullmatch('[0-9a-f]{64}', str(entry.get('sha256', ''))):
            raise AuditError('review_document_invalid')
        if entry.get('classification') not in REVIEW_CLASSES or not isinstance(entry.get('reason'), str) or len(entry['reason']) < 12:
            raise AuditError('review_document_invalid')
        if not isinstance(entry.get('reviewed_by'), str) or not entry['reviewed_by'].strip():
            raise AuditError('review_document_invalid')
        result[name] = entry
    return result


def audit(root, *, candidates=None, index=False, tracked=False, reviews=None):
    """Return only relative names, line numbers, rule IDs and file hashes.

    ``index`` reads immutable staged blobs, not unstaged worktree replacements.
    Explicit candidate manifests and ``tracked`` instead audit worktree bytes.
    """
    if sum((candidates is not None, index, tracked)) != 1:
        raise AuditError('choose_exactly_one_source_selection')
    root = Path(root).absolute()
    _plain(root)
    reviewed = _reviews(reviews)
    snapshot = None
    if index or tracked:
        entries, snapshot = _index_entries(root)
    else:
        if not isinstance(candidates, list):
            raise AuditError('candidate_manifest_invalid')
        entries = [(_path(name), '100644', '') for name in candidates]
    if not entries or len(entries) > MAX_FILES:
        raise AuditError('candidate_count_invalid')
    folded = [unicodedata.normalize('NFC', name).casefold() for name, _, _ in entries]
    if len(set(folded)) != len(folded):
        raise AuditError('duplicate_or_case_colliding_paths')
    blobs = _index_blobs(root, entries) if index else {}
    files, findings, total = [], [], 0
    for name, mode, identity in sorted(entries):
        if mode not in ('100644', '100755'):
            findings.append(dict(path=name, line=0, rule='symlink_submodule_or_invalid_mode', severity='error'))
            continue
        rule = _path_rule(name)
        if rule:
            findings.append(dict(path=name, line=0, rule=rule, severity='error'))
        try:
            value = blobs[name] if index else _worktree_data(root, name)
        except (OSError, AuditError) as error:
            findings.append(dict(path=name, line=0, rule=str(error) if isinstance(error, AuditError)
                                 else 'file_unreadable', severity='error'))
            continue
        total += len(value)
        if total > MAX_TOTAL:
            raise AuditError('source_size_limit_exceeded')
        digest = hashlib.sha256(value).hexdigest()
        files.append(dict(path=name, mode=mode, size=len(value), sha256=digest))
        if name == 'assets/app.ico':
            if not value.startswith(b'\x00\x00\x01\x00') or len(value) > 1024 * 1024:
                findings.append(dict(path=name, line=0, rule='invalid_icon', severity='error'))
            continue
        try:
            text = value.decode('utf-8-sig')
            if '\0' in text:
                raise UnicodeError()
        except UnicodeError:
            findings.append(dict(path=name, line=0, rule='non_text_source', severity='error'))
            continue
        accepted = reviewed.get(name)
        valid_review = accepted is not None and accepted['sha256'] == digest
        if accepted is not None and not valid_review:
            findings.append(dict(path=name, line=0, rule='review_hash_stale', severity='error'))
        locations = set()
        for rule, severity, pattern in _PATTERNS:
            for match in pattern.finditer(text):
                line = text.count('\n', 0, match.start()) + 1
                if (rule, line) in locations:
                    continue
                locations.add((rule, line))
                if severity == 'personal':
                    severity = 'review' if name.startswith('tests/') else 'error'
                status = 'reviewed' if severity == 'review' and valid_review else severity
                findings.append(dict(path=name, line=line, rule=rule, severity=status))
    if index or tracked:
        _, after = _index_entries(root)
        if after != snapshot:
            raise AuditError('git_index_changed_during_audit')
    errors = sum(item['severity'] == 'error' for item in findings)
    pending = sum(item['severity'] == 'review' for item in findings)
    return dict(schema_version=1, kind='MediaFlow.public-source-audit',
        checked_at=datetime.now(timezone.utc).isoformat(),
        selection='index' if index else 'tracked_worktree' if tracked else 'candidate_worktree',
        index_sha256=snapshot, file_count=len(files), total_bytes=total,
        manifest_sha256=hashlib.sha256(_json(files)).hexdigest(), files=files,
        findings=findings, error_count=errors, pending_review_count=pending,
        reviewed_finding_count=sum(item['severity'] == 'reviewed' for item in findings),
        passed=not errors and not pending,
        limitation='Pattern audit and explicit file review do not prove absence of all secrets or satisfy binary license obligations.')


def _document(path):
    try:
        value = Path(path).read_bytes()
        if len(value) > MAX_FILE:
            raise ValueError()
        return json.loads(value.decode('utf-8-sig'))
    except (OSError, UnicodeError, ValueError):
        raise AuditError('input_json_unreadable_or_invalid') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--index', action='store_true')
    selection.add_argument('--tracked', action='store_true')
    selection.add_argument('--candidates', help='JSON array of exact repository-relative file names')
    parser.add_argument('--reviews', help='Human review JSON, bound to each whole-file SHA-256')
    parser.add_argument('--report', help='Write the disclosure-safe full file inventory and findings')
    args = parser.parse_args(argv)
    try:
        report = audit(args.root, index=args.index, tracked=args.tracked,
            candidates=_document(args.candidates) if args.candidates else None,
            reviews=_document(args.reviews) if args.reviews else None)
        if args.report:
            Path(args.report).write_bytes(json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8') + b'\n')
        print(json.dumps({key: report[key] for key in ('passed', 'selection', 'file_count',
            'manifest_sha256', 'error_count', 'pending_review_count', 'reviewed_finding_count')}))
        if not args.report:
            for finding in report['findings']:
                print(json.dumps(finding, ensure_ascii=False))
        return 0 if report['passed'] else 1
    except AuditError as error:
        print(json.dumps({'passed': False, 'rule': str(error)}))
        return 2
    except OSError:
        print(json.dumps({'passed': False, 'rule': 'audit_io_failure'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
