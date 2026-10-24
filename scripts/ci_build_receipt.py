"""Bind successful CI checks to every byte of one complete portable build.

This unsigned receipt is evidence, not publisher authentication or proof that
GitHub ran anything. A release orchestrator must independently verify the real
workflow run, commit, repository, and downloaded artifact digest through GitHub.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))

from core.data_migration import frozen_file
from scripts.check_release_structure import check_release
from scripts.package_update import _inventory, _plain
from scripts.publish_release import _read_document, _save_new


KIND = 'MediaFlow.ci-build-attestation'
REQUIRED_GROUPS = ('core', 'ui', 'output')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z')
_RUN_ID = re.compile(r'[1-9][0-9]{0,23}\Z')
_ALLOWED_SKIP = {'test': 'tests.test_public_source_audit.PublicSourceAuditTests.test_worktree_symlink_is_never_followed',
                 'reason': 'Creating symlinks is not permitted for this test user'}
_SCOPE = 'File and reported-check binding only; independently verify GitHub run and artifact provenance.'
_FIELDS = {'schema_version', 'kind', 'product', 'version', 'build_id', 'source_sha256',
    'exe_sha256', 'packaged', 'passed', 'signed', 'publisher_authenticated',
    'ci_provenance_verified', 'created_at', 'github', 'files', 'tests', 'checks', 'evidence', 'scope'}


class CIReceiptError(RuntimeError):
    pass


def _github(repository, commit, run_id):
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise CIReceiptError('CI repository 必须是明确的 owner/name，不能包含路径或凭据。')
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit.lower()):
        raise CIReceiptError('CI commit 必须是完整的 Git 提交摘要。')
    if type(run_id) is int:
        run_id = str(run_id)
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise CIReceiptError('CI run_id 必须是有效的正整数标识。')
    return {'repository': repository, 'commit': commit.lower(), 'run_id': run_id}


def _count(value, name, *, positive=False):
    number = value.get(name)
    if type(number) is not int or not (1 if positive else 0) <= number <= 1000000:
        raise CIReceiptError('分组测试计数缺失或无效。')
    return number


def _tests(value, source_sha256):
    if (type(value) is not dict or value.get('passed') is not True
            or value.get('source_unchanged') is not True or value.get('source_sha256') != source_sha256):
        raise CIReceiptError('测试未全部通过，或测试的运行源码摘要与实际 EXE 不一致。')
    groups = value.get('groups')
    if type(groups) is not list or len(groups) != len(REQUIRED_GROUPS):
        raise CIReceiptError('需要完整的 core、ui、output 分组测试结果。')
    checked = {}
    for group in groups:
        name = group.get('group') if type(group) is dict else None
        if name not in REQUIRED_GROUPS or name in checked:
            raise CIReceiptError('测试分组缺失、重复或不受支持。')
        if (group.get('passed') is not True or group.get('source_unchanged') is not True
                or group.get('source_sha256') != source_sha256
                or type(group.get('exit_code')) is not int or group['exit_code'] != 0):
            raise CIReceiptError('测试分组失败、进程退出异常或源码在测试期间变化。')
        counts = {key: _count(group, key, positive=key == 'tests')
                  for key in ('tests', 'failures', 'errors', 'skipped')}
        skips = group.get('skips')
        if (counts['failures'] or counts['errors'] or type(skips) is not list
                or counts['skipped'] != len(skips) or counts['skipped'] >= counts['tests']
                or group.get('unexpected_skips') != []):
            raise CIReceiptError('测试存在失败、错误、不完整的 skip 记录或未授权的跳过。')
        if skips and (name != 'core' or skips != [_ALLOWED_SKIP]):
            raise CIReceiptError('测试跳过不在明确允许的能力限制清单内。')
        checked[name] = dict(group=name, passed=True, exit_code=0,
            source_sha256=source_sha256, source_unchanged=True, **counts,
            skips=[dict(item) for item in skips], unexpected_skips=[])
    totals = {key: sum(group[key] for group in checked.values())
              for key in ('tests', 'failures', 'errors', 'skipped')}
    if any(_count(value, key, positive=key == 'tests') != totals[key] for key in totals):
        raise CIReceiptError('测试汇总与实际分组计数不一致。')
    return dict(passed=True, source_sha256=source_sha256, source_unchanged=True,
                groups=[checked[name] for name in REQUIRED_GROUPS], **totals)


@contextmanager
def _snapshot(release_dir, expected_version, runtime_identity=None):
    """Reuse the complete structure gate and hold the original release handles."""
    root = _plain(release_dir).resolve()
    before = _inventory(root)
    with ExitStack() as stack:
        handles = {name: stack.enter_context(frozen_file(root / name)) for name in before}
        structure = check_release(root, expected_version, runtime_identity=runtime_identity)
        files = [{'path': name, 'size': info[0],
                  'sha256': hashlib.file_digest(handles[name], 'sha256').hexdigest()}
                 for name, info in before.items()]
        if (structure['sha256'] != {item['path']: item['sha256'] for item in files}
                or _inventory(root) != before):
            raise CIReceiptError('完整发布目录在构建凭据检查期间发生变化。')
        yield structure, files


def _summary(value, receipt_sha256):
    return {'passed': True, 'product': value['product'], 'version': value['version'],
        'build_id': value['build_id'], 'source_sha256': value['source_sha256'],
        'exe_sha256': value['exe_sha256'], 'receipt_sha256': receipt_sha256,
        'github': dict(value['github']), 'files': len(value['files']),
        'total_bytes': sum(item['size'] for item in value['files']),
        'tests': value['tests'], 'runtime_identity_bound': True,
        'signed': False, 'publisher_authenticated': False, 'ci_provenance_verified': False,
        'native_exe_executed': False, 'scope': _SCOPE}


def create_receipt(release_dir, output_path, *, expected_version, runtime_identity,
                   test_results, structure_report, repository, commit, run_id):
    """Create a new external receipt only after every supplied check passes.

    Runtime identity must be the actual CI EXE output. This helper binds that
    input; independent workflow verification establishes who executed the EXE.
    """
    root, output = _plain(release_dir).resolve(), _plain(output_path).resolve()
    github = _github(repository, commit, run_id)
    if output.is_relative_to(root) or output.exists() or not output.parent.is_dir():
        raise CIReceiptError('构建凭据必须写入发布目录外的新文件，且父目录已存在。')
    paths = {'runtime_identity': _plain(runtime_identity).resolve(),
             'tests': _plain(test_results).resolve(), 'release_structure': _plain(structure_report).resolve()}
    if len(set(paths.values())) != len(paths) or any(path.is_relative_to(root) for path in paths.values()):
        raise CIReceiptError('CI 检查报告必须是发布目录外相互独立的文件。')
    with ExitStack() as stack:
        documents, evidence = {}, {}
        for name, path in paths.items():
            stack.enter_context(frozen_file(path))
            document, sha256, size = _read_document(path, 4 * 1024 * 1024)
            documents[name], evidence[name] = document, {'size': size, 'sha256': sha256}
        claimed_source = documents['runtime_identity'].get('source_sha256')
        if not isinstance(claimed_source, str) or not _HASH.fullmatch(claimed_source):
            raise CIReceiptError('实际 EXE 身份报告缺少有效运行源码摘要。')
        # Obvious failures and unauthorized skips need no release-file scans.
        # Successful reports still undergo the complete frozen snapshot below.
        tests = _tests(documents['tests'], claimed_source)
        with _snapshot(root, expected_version, runtime_identity=paths['runtime_identity']) as (structure, files):
            if (not structure['runtime_identity_verified'] or structure != documents['release_structure']
                    or tests['source_sha256'] != structure['source_sha256']):
                raise CIReceiptError('结构检查报告与当前完整目录或实际 EXE 身份报告不一致。')
            receipt = {'schema_version': 1, 'kind': KIND, 'product': 'MediaFlow',
                'version': structure['version'], 'build_id': structure['build_id'],
                'source_sha256': structure['source_sha256'],
                'exe_sha256': structure['sha256']['MediaFlow.exe'],
                'packaged': True, 'passed': True, 'signed': False,
                'publisher_authenticated': False, 'ci_provenance_verified': False,
                'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'github': github, 'files': files, 'tests': tests,
                'checks': {'runtime_identity_verified': True, 'release_structure_verified': True},
                'evidence': evidence, 'scope': _SCOPE}
            data = (json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
            _save_new(output, data)
    return _summary(receipt, hashlib.sha256(data).hexdigest())


def verify_receipt(receipt_path, release_dir, *, expected_version=None, expected_commit=None,
                   expected_repository=None, expected_run_id=None, expected_source_sha256=None):
    """Verify exact build bytes; never treat local JSON as GitHub authentication."""
    root = _plain(release_dir).resolve()
    path = _plain(receipt_path).resolve()
    if path.is_relative_to(root):
        raise CIReceiptError('构建凭据必须位于发布目录之外。')
    value, receipt_sha256, _ = _read_document(path, 4 * 1024 * 1024)
    if (set(value) != _FIELDS or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or value.get('kind') != KIND or value.get('product') != 'MediaFlow'
            or value.get('packaged') is not True or value.get('passed') is not True
            or any(value.get(name) is not False for name in ('signed', 'publisher_authenticated', 'ci_provenance_verified'))
            or value.get('scope') != _SCOPE
            or value.get('checks') != {'runtime_identity_verified': True, 'release_structure_verified': True}):
        raise CIReceiptError('构建凭据格式、通过状态或验证范围无效。')
    try:
        datetime.strptime(value['created_at'], '%Y-%m-%dT%H:%M:%SZ')
    except (ValueError, TypeError):
        raise CIReceiptError('构建凭据时间格式无效。') from None
    github = value.get('github')
    if type(github) is not dict or set(github) != {'repository', 'commit', 'run_id'}:
        raise CIReceiptError('构建凭据的 GitHub 绑定格式无效。')
    if github != _github(**github):
        raise CIReceiptError('构建凭据的 GitHub 绑定格式未规范化。')
    expected = _github(expected_repository if expected_repository is not None else github['repository'],
        expected_commit if expected_commit is not None else github['commit'],
        expected_run_id if expected_run_id is not None else github['run_id'])
    if github != expected:
        raise CIReceiptError('构建凭据与独立验证的 repository、commit 或 run_id 不一致。')
    evidence = value.get('evidence')
    if type(evidence) is not dict or set(evidence) != {'runtime_identity', 'tests', 'release_structure'}:
        raise CIReceiptError('构建凭据缺少必要的检查报告摘要。')
    for record in evidence.values():
        if (type(record) is not dict or set(record) != {'size', 'sha256'}
                or type(record.get('size')) is not int or not 0 < record['size'] <= 4 * 1024 * 1024
                or not isinstance(record.get('sha256'), str) or not _HASH.fullmatch(record['sha256'])):
            raise CIReceiptError('构建凭据的检查报告摘要无效。')
    version = expected_version if expected_version is not None else value['version']
    with _snapshot(root, version) as (structure, files):
        if (value['version'] != structure['version'] or value['build_id'] != structure['build_id']
                or value['source_sha256'] != structure['source_sha256'] or value['files'] != files
                or value['exe_sha256'] != structure['sha256']['MediaFlow.exe']):
            raise CIReceiptError('完整发布目录的文件清单、字节、版本、构建或源码摘要与 CI 凭据不一致。')
        if expected_source_sha256 is not None and value['source_sha256'] != expected_source_sha256:
            raise CIReceiptError('实际 EXE 的运行源码摘要与独立验证的预期值不一致。')
        if _tests(value['tests'], structure['source_sha256']) != value['tests']:
            raise CIReceiptError('构建凭据中的分组测试摘要包含未声明字段。')
    return _summary(value, receipt_sha256)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Bind successful CI reports to a complete build')
    create.add_argument('--release-dir', required=True)
    create.add_argument('--output', required=True)
    create.add_argument('--expected-version', required=True)
    create.add_argument('--runtime-identity', required=True)
    create.add_argument('--test-results', required=True)
    create.add_argument('--structure-report', required=True)
    for name in ('repository', 'commit', 'run-id'):
        create.add_argument('--' + name, required=True)
    verify = commands.add_parser('verify', help='Verify receipt binding without trusting its claimed GitHub origin')
    verify.add_argument('--receipt', required=True)
    verify.add_argument('--release-dir', required=True)
    for name in ('version', 'commit', 'repository', 'run-id', 'source-sha256'):
        verify.add_argument('--expected-' + name)
    options = vars(parser.parse_args(argv))
    command = options.pop('command')
    try:
        if command == 'create':
            options['output_path'] = options.pop('output')
            result = create_receipt(**options)
        else:
            options['receipt_path'] = options.pop('receipt')
            result = verify_receipt(**options)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({'passed': False, 'error_type': type(error).__name__,
            'message': str(error) if isinstance(error, CIReceiptError)
                else '构建凭据验证失败：检查输入报告、完整发布文件或权限；未执行签名或发布。'}, ensure_ascii=False),
            file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
