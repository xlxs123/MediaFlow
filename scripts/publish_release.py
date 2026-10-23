"""Explicit offline publisher workflow; never uploads or creates keys implicitly.

Only the ``init-key`` command creates a key. Normal publishing requires an
external encrypted Ed25519 PKCS8 PEM and an independently supplied public key.
"""
from __future__ import annotations

import argparse
import base64
from copy import deepcopy
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.data_migration import frozen_file
from core.update_manifest import (UpdateValidationError, canonical_bytes,
    safe_release_path, validate_manifest, validate_trusted_keys)
from scripts.package_update import (PackageError, _plain, _publish, _read_stamp,
    create_package)


ROOT = Path(__file__).resolve().parents[1]
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z')
_ENVIRONMENT_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}\Z')
_HASH = re.compile(r'[0-9a-fA-F]{64}\Z')
_ACCEPTANCE_KIND = 'MediaFlow.release-acceptance'
_ACCEPTANCE_FIELDS = {'schema_version', 'kind', 'product', 'version', 'build_id',
    'source_sha256', 'exe_sha256', 'packaged', 'passed', 'checked_at', 'reports'}


class PublishError(RuntimeError):
    pass


def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
        raise PublishError('时间需要使用 UTC YYYY-MM-DDTHH:MM:SSZ 格式。')
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        raise PublishError('时间不是有效日期。') from None
    return value


def _key_id(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PublishError('签名 key_id 必须是 1–96 位安全 ASCII 标识。')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublishError('JSON 中存在重复字段。')
        result[key] = value
    return result


def _read_document(path, maximum=2 * 1024 * 1024):
    path = _plain(path).resolve()
    if not path.is_file() or path.stat().st_size > maximum:
        raise PublishError('JSON 文件不存在、不可读或超过大小限制。')
    try:
        with frozen_file(path) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise PublishError('JSON 文件超过大小限制。')
        value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            raise ValueError()
        return value, hashlib.sha256(data).hexdigest(), len(data)
    except (ValueError, UnicodeError, RecursionError):
        raise PublishError('需要有效的 UTF-8 JSON 对象。') from None


def _file_hash(path):
    path = _plain(path).resolve()
    if not path.is_file():
        raise PublishError('需要的文件不存在或无法读取。')
    with frozen_file(path) as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _save_new(path, data):
    path = _plain(path).resolve()
    if not path.parent.is_dir() or path.exists():
        raise PublishError('输出父目录不存在或输出文件已存在，未覆盖。')
    descriptor, name = tempfile.mkstemp(prefix='.mediaflow-publish-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _publish(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_private_staging(path, parent, prefix):
    path = _plain(path).resolve()
    if path.parent != Path(parent).resolve() or not path.name.startswith(prefix):
        raise PublishError('暂存目录不在本次发布的专属范围，未清理。')
    for child in path.rglob('*'):
        _plain(child)
    shutil.rmtree(path)


def _identity(document, stamp, exe_sha256):
    if document.get('passed') is not True:
        raise PublishError('原生验收报告尚未通过，不能生成或使用发布验收收据。')
    identity = document.get('runtime_identity')
    if (type(identity) is not dict or identity.get('packaged') is not True
            or identity.get('app_version') != stamp['app_version']
            or identity.get('build_id') != stamp['build_id']
            or identity.get('source_sha256', '').lower() != stamp['source_sha256'].lower()):
        raise PublishError('原生验收报告的实际打包版本、构建或源码摘要与发布目录不一致。')
    executable = identity.get('executable')
    if (not isinstance(executable, str) or Path(executable).name != 'MediaFlow.exe'
            or not Path(executable).is_absolute() or _file_hash(executable) != exe_sha256):
        raise PublishError('验收报告所运行的 MediaFlow.exe 与实际发布 EXE 的 SHA-256 不一致。')


def create_acceptance_receipt(release_dir, report_files, output_file):
    """Bind existing successful native reports to the exact packaged EXE.

    This does not run tests or turn a manually supplied ``passed`` flag into a
    test result. Referenced report files remain necessary when publishing.
    """
    release = _plain(release_dir).resolve()
    output = _plain(output_file).resolve()
    if output.is_relative_to(release):
        raise PublishError('验收收据应保存在发布目录之外，不能修改已验收的发布文件。')
    stamp = _read_stamp(release)
    executable_sha = _file_hash(release / 'MediaFlow.exe')
    if not isinstance(report_files, (list, tuple)) or not 1 <= len(report_files) <= 64:
        raise PublishError('至少提供 1 份、最多 64 份真实打包 EXE 的验收报告。')
    reports, seen = [], set()
    for path in report_files:
        path = _plain(path).resolve()
        if path in seen or path == output:
            raise PublishError('验收报告重复，或与收据输出路径冲突。')
        seen.add(path)
        value, digest, size = _read_document(path, 32 * 1024 * 1024)
        _identity(value, stamp, executable_sha)
        reports.append({'path': str(path), 'sha256': digest, 'size': size})
    receipt = {'schema_version': 1, 'kind': _ACCEPTANCE_KIND, 'product': 'MediaFlow',
        'version': stamp['app_version'], 'build_id': stamp['build_id'],
        'source_sha256': stamp['source_sha256'].lower(), 'exe_sha256': executable_sha,
        'packaged': True, 'passed': True,
        'checked_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'reports': reports}
    _save_new(output, _json(receipt))
    return receipt


def _acceptance(path, stamp, exe_sha256):
    receipt, receipt_sha, _ = _read_document(path)
    if (set(receipt) != _ACCEPTANCE_FIELDS or receipt.get('schema_version') != 1
            or type(receipt.get('schema_version')) is not int
            or receipt.get('kind') != _ACCEPTANCE_KIND or receipt.get('product') != 'MediaFlow'
            or receipt.get('packaged') is not True or receipt.get('passed') is not True
            or receipt.get('version') != stamp['app_version'] or receipt.get('build_id') != stamp['build_id']
            or receipt.get('source_sha256') != stamp['source_sha256'].lower()
            or receipt.get('exe_sha256') != exe_sha256):
        raise PublishError('发布验收收据的通过状态、版本、构建或实际 EXE 摘要不一致。')
    _utc(receipt['checked_at'])
    reports = receipt['reports']
    if type(reports) is not list or not 1 <= len(reports) <= 64:
        raise PublishError('验收收据必须引用实际原生验收报告，不能使用空报告或仅 --stable 标记。')
    seen = set()
    for entry in reports:
        if (type(entry) is not dict or set(entry) != {'path', 'sha256', 'size'}
                or not isinstance(entry['path'], str) or not Path(entry['path']).is_absolute()
                or not isinstance(entry['sha256'], str) or not _HASH.fullmatch(entry['sha256'])
                or type(entry['size']) is not int or not 0 < entry['size'] <= 32 * 1024 * 1024):
            raise PublishError('验收报告引用格式无效。')
        report_path = _plain(entry['path']).resolve()
        if report_path in seen:
            raise PublishError('验收收据重复引用相同报告。')
        seen.add(report_path)
        report, digest, size = _read_document(report_path, 32 * 1024 * 1024)
        if digest != entry['sha256'].lower() or size != entry['size']:
            raise PublishError('验收报告在收据生成后发生变化，请重新执行和确认验收。')
        _identity(report, stamp, exe_sha256)
    return receipt, receipt_sha


def _outside_project(path, *blocked):
    """Keep private material outside the source workspace and release artifacts."""
    path = _plain(path).resolve()
    roots = [ROOT, *[Path(root).resolve() for root in blocked]]
    # MediaFlow's sibling version workspaces share this known development root.
    if ROOT.name.startswith('m3u8-downloader'):
        roots.append(ROOT.parent)
    if any(path == root or path.is_relative_to(root) for root in roots):
        raise PublishError('私钥必须位于项目源码、历史工作区、发布目录和输出目录之外。')
    for parent in path.parents:
        if any((parent / marker).exists() for marker in ('.git', 'pyproject.toml', 'package.json', 'requirements.txt')):
            raise PublishError('私钥路径位于可识别的项目目录内，请使用独立凭据目录。')
    return path


def _environment(name, label):
    if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
        raise PublishError(label + '的环境变量名无效。')
    value = os.environ.get(name)
    if not value:
        raise PublishError(label + '环境变量不存在或为空。')
    return value


def _password(password=None, password_env=None):
    if password is not None and password_env:
        raise PublishError('只能选择一种私钥密码来源。')
    if password_env:
        password = _environment(password_env, '私钥密码')
    if isinstance(password, str):
        password = password.encode('utf-8')
    if not isinstance(password, bytes) or not 1 <= len(password) <= 4096:
        raise PublishError('需要非空私钥密码；请使用密码环境变量或交互式 getpass 输入。')
    return password


def _private_key(*, private_key_file=None, private_key_env=None, password=None,
                 password_env=None, blocked=()):
    if bool(private_key_file) == bool(private_key_env):
        raise PublishError('必须明确选择外部私钥文件或私钥环境变量中的一种。')
    if private_key_file:
        path = _outside_project(private_key_file, *blocked)
        if not path.is_file() or path.stat().st_size > 16384 or path.stat().st_nlink != 1:
            raise PublishError('外部私钥文件不可读、过大或存在硬链接。')
        with frozen_file(path) as stream:
            pem = stream.read(16385)
    else:
        pem = _environment(private_key_env, '私钥').encode('utf-8')
    if len(pem) > 16384 or not pem.lstrip().startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'):
        raise PublishError('只接受外部加密 PKCS8 PEM 私钥，不能使用未加密私钥。')
    secret = _password(password, password_env)
    try:
        key = serialization.load_pem_private_key(pem, password=secret)
    except (ValueError, TypeError):
        raise PublishError('无法解密私钥，请检查加密 PKCS8 PEM 文件和密码。') from None
    if not isinstance(key, Ed25519PrivateKey):
        raise PublishError('发布格式 2 只接受 Ed25519 私钥。')
    return key


def _trusted_keys(path, release, output):
    path = _plain(path).resolve()
    if path.is_relative_to(release) or path.is_relative_to(output):
        raise PublishError('可信公钥必须由发布包和输出目录之外的独立文件提供。')
    value, _, _ = _read_document(path, 128 * 1024)
    if set(value) == {'trusted_keys'}:
        value = value['trusted_keys']
    return validate_trusted_keys(value)


def _sign(document, key, key_id):
    document = deepcopy(document)
    document.pop('signature', None)
    document['signature'] = {'algorithm': 'ed25519', 'key_id': key_id,
        'value': base64.b64encode(key.sign(canonical_bytes(document))).decode('ascii')}
    return document


def publish_release(release_dir, output_dir, *, package_url, published_at, key_id,
                    trusted_keys_file, acceptance_receipt, private_key_file=None,
                    private_key_env=None, password=None, password_env=None,
                    release_notes='', release_page='', minimum_app_version='1.9.0',
                    package_name=None):
    """Return verified artifacts. No server request, executable run or upload occurs."""
    release = _plain(release_dir).resolve()
    output = _plain(output_dir).resolve()
    if (not release.is_dir() or output.exists() or not output.parent.is_dir()
            or output == release or output.is_relative_to(release)
            or release.is_relative_to(output)):
        raise PublishError('需要已验收发布目录和独立、尚不存在的输出目录；不会覆盖现有目录。')
    _utc(published_at)
    _key_id(key_id)
    stamp = _read_stamp(release)
    executable_sha = _file_hash(release / 'MediaFlow.exe')
    receipt, receipt_sha = _acceptance(acceptance_receipt, stamp, executable_sha)
    keys = _trusted_keys(trusted_keys_file, release, output)
    if key_id not in keys or keys[key_id]['algorithm'] != 'ed25519':
        raise PublishError('独立可信公钥文件未包含所选 Ed25519 key_id。')
    key = _private_key(private_key_file=private_key_file, private_key_env=private_key_env,
        password=password, password_env=password_env, blocked=(release, output))
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode('ascii')
    if public != keys[key_id]['public_key']:
        raise PublishError('私钥与独立可信公钥不匹配，未生成发布文件。')
    name = package_name or f'MediaFlow-{stamp["app_version"]}-{stamp["build_id"]}.zip'
    if (not isinstance(name, str) or Path(name).name != name or not name.lower().endswith('.zip')
            or len(name) > 180 or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in name)
            or name.rstrip(' .') != name):
        raise PublishError('发布 ZIP 文件名必须是安全的单个 .zip 文件名。')
    safe_release_path(name)
    # Validate the real HTTPS target and format-2 metadata before producing ZIP bytes.
    preliminary = {'schema_version': 2, 'product': 'MediaFlow', 'version': stamp['app_version'],
        'build_id': stamp['build_id'], 'platform': 'windows', 'architecture': 'x86_64',
        'release_notes': release_notes, 'release_page': release_page,
        'minimum_app_version': minimum_app_version, 'data_migration': 'separate',
        'published_at': published_at, 'signing': {'algorithm': 'ed25519', 'key_id': key_id},
        'files': [{'path': 'MediaFlow.exe', 'size': 1, 'sha256': '0' * 64}],
        'package': {'format': 'zip', 'url': package_url, 'size': 1, 'sha256': '0' * 64}}
    from core.update_manifest import REQUIRED_FILES
    preliminary['files'] = [{'path': path, 'size': 1, 'sha256': '0' * 64} for path in REQUIRED_FILES]
    validate_manifest(_sign(preliminary, key, key_id), keys, require_signature=True)
    staging = Path(tempfile.mkdtemp(prefix='.mediaflow-publish-', dir=output.parent))
    try:
        result = create_package(release, staging / name, release_notes=release_notes,
            minimum_app_version=minimum_app_version, release_page=release_page,
            manifest_fields={'schema_version': 2, 'published_at': published_at,
                'signing': {'algorithm': 'ed25519', 'key_id': key_id}},
            sign_manifest=lambda manifest: _sign(manifest, key, key_id), trusted_keys=keys,
            expected_exe_sha256=executable_sha)
        outer = deepcopy(result['manifest'])
        outer.pop('signature')
        outer['package'] = {'format': 'zip', 'url': package_url, 'size': result['size'], 'sha256': result['sha256']}
        outer = _sign(outer, key, key_id)
        verified = validate_manifest(outer, keys, require_signature=True)
        if not verified['authenticated']:
            raise PublishError('独立可信公钥自验签未通过，未提交发布文件。')
        _save_new(staging / 'release.json', _json(outer))
        detached = {'schema_version': 1, 'kind': 'MediaFlow.release-signature', 'product': 'MediaFlow',
            'version': stamp['app_version'], 'build_id': stamp['build_id'],
            'canonicalization': 'utf8-json-sortkeys-compact-without-signature-v1',
            'manifest_sha256': verified['manifest_sha256'], 'signature': outer['signature']}
        _save_new(staging / 'signature.json', _json(detached))
        # A report or EXE changing during package production invalidates this release.
        _, final_receipt_sha = _acceptance(acceptance_receipt, stamp, executable_sha)
        if final_receipt_sha != receipt_sha or _file_hash(release / 'MediaFlow.exe') != executable_sha:
            raise PublishError('验收收据或实际 EXE 在发布期间发生变化，未提交发布文件。')
        if output.exists():
            raise PublishError('输出目录在发布期间出现，已停止且未覆盖。')
        os.rename(staging, output)
        return {'product': 'MediaFlow', 'version': stamp['app_version'], 'build_id': stamp['build_id'],
            'output_directory': str(output), 'package_path': str(output / name),
            'manifest_path': str(output / 'release.json'), 'signature_path': str(output / 'signature.json'),
            'metadata_path': str(output / Path(result['metadata_path']).name),
            'size': result['size'], 'sha256': result['sha256'], 'exe_sha256': executable_sha,
            'manifest_sha256': verified['manifest_sha256'], 'authenticated': True,
            'authentication': verified['authentication'], 'key_id': key_id,
            'acceptance_receipt_sha256': receipt_sha, 'acceptance_reports': len(receipt['reports']),
            'package_url': package_url, 'uploaded': False, 'manifest': outer}
    finally:
        # Only this operation's private staging directory is disposable.
        if staging.exists():
            _remove_private_staging(staging, output.parent, '.mediaflow-publish-')


def initialize_key_directory(directory, *, key_id, password=None, password_env=None):
    """Explicit operator action only; callers must preserve this external directory."""
    _key_id(key_id)
    destination = _outside_project(directory)
    if destination.exists() or not destination.parent.is_dir():
        raise PublishError('密钥目录必须为独立、尚不存在的外部目录，父目录须已存在。')
    secret = _password(password, password_env)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(secret))
    public = {'trusted_keys': {key_id: {'algorithm': 'ed25519', 'public_key': base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode('ascii')}}}
    validate_trusted_keys(public['trusted_keys'])
    staging = Path(tempfile.mkdtemp(prefix='.mediaflow-key-init-', dir=destination.parent))
    try:
        _save_new(staging / 'private-key.encrypted.pem', pem)
        _save_new(staging / 'trusted-public-keys.json', _json(public))
        os.chmod(staging / 'private-key.encrypted.pem', 0o600)
        if destination.exists():
            raise PublishError('外部密钥目录已存在，未覆盖。')
        os.rename(staging, destination)
    finally:
        if staging.exists():
            _remove_private_staging(staging, destination.parent, '.mediaflow-key-init-')
    return {'key_id': key_id, 'algorithm': 'ed25519',
        'private_key_file': str(destination / 'private-key.encrypted.pem'),
        'trusted_keys_file': str(destination / 'trusted-public-keys.json'),
        'encrypted': True, 'uploaded': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='MediaFlow 显式离线签名发布工具：不上传，不自动生成密钥')
    commands = parser.add_subparsers(dest='command', required=True)
    receipt = commands.add_parser('receipt', help='从已通过的真实 EXE 报告生成绑定收据，不运行测试')
    receipt.add_argument('--release-dir', required=True)
    receipt.add_argument('--report', action='append', required=True)
    receipt.add_argument('--output', required=True)
    publish = commands.add_parser('publish', help='使用外部加密私钥生成并自验签发布文件')
    publish.add_argument('--release-dir', required=True)
    publish.add_argument('--output-dir', required=True)
    publish.add_argument('--package-url', required=True)
    publish.add_argument('--published-at', required=True)
    publish.add_argument('--key-id', required=True)
    publish.add_argument('--trusted-keys-file', required=True)
    publish.add_argument('--acceptance-receipt', required=True)
    sources = publish.add_mutually_exclusive_group(required=True)
    sources.add_argument('--private-key-file')
    sources.add_argument('--private-key-env')
    publish.add_argument('--password-env')
    publish.add_argument('--notes', default='')
    publish.add_argument('--release-page', default='')
    publish.add_argument('--minimum-app-version', default='1.9.0')
    publish.add_argument('--package-name')
    key = commands.add_parser('init-key', help='仅用户明确调用时在外部目录创建加密 Ed25519 私钥')
    key.add_argument('--directory', required=True)
    key.add_argument('--key-id', required=True)
    key.add_argument('--password-env')
    args = parser.parse_args(argv)
    try:
        if args.command == 'receipt':
            result = create_acceptance_receipt(args.release_dir, args.report, args.output)
        else:
            password = None
            if not args.password_env:
                password = getpass.getpass('外部私钥密码（输入不回显）：')
                if args.command == 'init-key' and password != getpass.getpass('再次输入密码：'):
                    raise PublishError('两次密码输入不一致，未创建密钥。')
            if args.command == 'init-key':
                result = initialize_key_directory(args.directory, key_id=args.key_id,
                    password=password, password_env=args.password_env)
            else:
                result = publish_release(args.release_dir, args.output_dir, package_url=args.package_url,
                    published_at=args.published_at, key_id=args.key_id, trusted_keys_file=args.trusted_keys_file,
                    acceptance_receipt=args.acceptance_receipt, private_key_file=args.private_key_file,
                    private_key_env=args.private_key_env, password=password, password_env=args.password_env,
                    release_notes=args.notes, release_page=args.release_page,
                    minimum_app_version=args.minimum_app_version, package_name=args.package_name)
        print(json.dumps({key: value for key, value in result.items() if key != 'manifest'}, ensure_ascii=False, indent=2))
        return 0
    except (PublishError, PackageError, UpdateValidationError) as error:
        parser.exit(1, '发布未完成：' + str(error) + '\n')
    except (OSError, ValueError, TypeError):
        parser.exit(1, '发布未完成：无法安全读取、加密或提交发布文件，请检查输入、权限和磁盘空间。\n')


if __name__ == '__main__':
    raise SystemExit(main())
