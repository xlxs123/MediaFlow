"""Publisher-only release gates; never changes client trust or contacts a server."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.update_channel import key_fingerprint, read_channel
from core.update_manifest import UpdateValidationError, parse_version, validate_manifest
from core.update_transport import TransportError, https_url
from scripts.package_update import PackageError, _plain, _read_stamp
from scripts.publish_release import (PublishError, _file_hash, _outside_project,
    _private_key, _read_document, _trusted_keys)
from utils.branding import APP_VERSION


class ReleaseGateError(RuntimeError):
    pass


def public_url(value):
    """Reject obvious test targets; actual public access is verified after upload."""
    https_url(value)
    host = urlsplit(value).hostname.lower().rstrip('.')
    if (host == 'localhost' or any(host == domain or host.endswith('.' + domain)
            for domain in ('example.com', 'example.org', 'example.net'))
            or host.endswith(('.localhost', '.invalid', '.test', '.example', '.local'))):
        raise ReleaseGateError('正式发布地址不能使用 localhost、示例地址或测试域名。')
    import ipaddress
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if '.' not in host:
            raise ReleaseGateError('正式发布地址需要公开 HTTPS 主机名。') from None
    else:
        if not address.is_global:
            raise ReleaseGateError('正式发布地址不能使用私有、保留或本地 IP。')
    return value


def _separate_paths(release, output, verification):
    paths = [Path(_plain(path)).resolve() for path in (release, output, verification)]
    for index, left in enumerate(paths):
        for right in paths[index + 1:]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ReleaseGateError('发布目录、签名资产目录和验证目录必须互相独立。')
    for path in paths[1:]:
        if path.exists() or not path.parent.is_dir():
            raise ReleaseGateError('资产和验证目录必须尚不存在，且父目录已经存在；不会覆盖。')
    return paths


def preflight(*, expected_version, channel_file, trusted_keys_file, key_id,
              package_url, release_dir, output_dir, verification_dir,
              private_key_file=None, private_key_env=None, password_env=None,
              existing=False, reports=(), project_root=ROOT):
    version = parse_version(expected_version)
    if version.is_prerelease or version.local or expected_version != APP_VERSION:
        raise ReleaseGateError('正式稳定版号必须与当前源码 APP_VERSION 完全一致，不能是预发布版。')
    channel = read_channel(channel_file)
    if channel['key_state'] != 'initialized':
        raise ReleaseGateError('正式渠道仍待初始化；请先确认真实 HTTPS 地址和正式公钥，未构建或签名。')
    embedded = read_channel(Path(project_root) / 'assets/update-channel.json')
    if channel != embedded:
        raise ReleaseGateError('所选渠道与即将嵌入客户端的正式渠道不一致，未构建。')
    public_url(channel['source_url'])
    public_url(package_url)
    # The current client intentionally follows same-origin HTTPS redirects only.
    # A real hosting service must pass verify_hosted_release after uploading.
    release, output, verification = _separate_paths(release_dir, output_dir, verification_dir)
    if not existing and release.exists():
        raise ReleaseGateError('新构建目录已经存在，请选择独立的新目录；不会复用或覆盖。')
    if existing and (not release.is_dir() or not reports):
        raise ReleaseGateError('复用构建必须提供现存发布目录及至少一份实际 EXE 验收报告。')
    if not existing and reports:
        raise ReleaseGateError('新构建不能沿用旧报告；pipeline 将运行新 EXE 生成报告。')
    keys = _trusted_keys(trusted_keys_file, release, output)
    if key_id not in keys or keys[key_id]['algorithm'] != 'ed25519':
        raise ReleaseGateError('独立可信公钥文件中不存在所选 Ed25519 key_id。')
    if channel['trusted_keys'].get(key_id) != keys[key_id]:
        raise ReleaseGateError('发布签名公钥与客户端内置公钥不同，未构建或签名。')
    for path in reports:
        path = Path(_plain(path)).resolve()
        if not path.is_file():
            raise ReleaseGateError('提供的实际 EXE 验收报告不存在。')
    if not password_env or not os.environ.get(password_env):
        raise ReleaseGateError('私钥密码环境变量不存在或为空；不会在参数或日志中传递密码。')
    key = _private_key(private_key_file=private_key_file, private_key_env=private_key_env,
        password_env=password_env, blocked=(release, output, verification))
    from cryptography.hazmat.primitives import serialization
    import base64
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode('ascii')
    if public != keys[key_id]['public_key']:
        raise ReleaseGateError('加密私钥与确认的客户端公钥不匹配，未构建。')
    if existing:
        stamp = _read_stamp(release)
        if stamp['app_version'] != expected_version:
            raise ReleaseGateError('已验收发布目录的版本与要求版本不符。')
    return {'passed': True, 'version': expected_version, 'key_id': key_id,
        'key_fingerprint': key_fingerprint(keys[key_id]), 'existing_release': bool(existing),
        'channel_source_sha256': hashlib.sha256(channel['source_url'].encode('utf-8')).hexdigest(),
        'uploaded': False, 'public_verification': 'pending'}


def check_runtime(*, identity_file, release_dir, channel_file, expected_version):
    identity, _, _ = _read_document(identity_file)
    release = Path(_plain(release_dir)).resolve()
    stamp = _read_stamp(release)
    channel = read_channel(channel_file)
    fingerprints = {name: key_fingerprint(key) for name, key in channel['trusted_keys'].items()}
    actual = identity.get('update_channel', {})
    if (identity.get('packaged') is not True or identity.get('app_version') != expected_version
            or stamp.get('app_version') != expected_version
            or identity.get('build_id') != stamp.get('build_id')
            or identity.get('source_sha256') != stamp.get('source_sha256')
            or Path(identity.get('executable', '')).resolve() != release / 'MediaFlow.exe'
            or actual.get('key_state') != 'initialized'
            or actual.get('source_sha256') != hashlib.sha256(channel['source_url'].encode('utf-8')).hexdigest()
            or actual.get('key_fingerprints') != fingerprints):
        raise ReleaseGateError('实际启动的 EXE 版本、构建或内置渠道与批准配置不一致。')
    return {'passed': True, 'version': expected_version,
            'exe_sha256': _file_hash(release / 'MediaFlow.exe'), 'build_id': stamp['build_id']}


def check_published(*, output_dir, trusted_keys_file, expected_version, package_url):
    output = Path(_plain(output_dir)).resolve()
    document, _, _ = _read_document(output / 'release.json')
    # Independent trust is deliberately outside both release assets and ZIP.
    keys = _trusted_keys(trusted_keys_file, output, output)
    verified = validate_manifest(document, keys, require_signature=True)
    if document['version'] != expected_version or document['package']['url'] != package_url:
        raise ReleaseGateError('最终清单的版本或发布资产地址不一致。')
    archives = list(output.glob('*.zip'))
    if len(archives) != 1:
        raise ReleaseGateError('最终资产目录必须包含唯一的发布 ZIP。')
    package = archives[0]
    digest = _file_hash(package)
    if digest != document['package']['sha256'] or package.stat().st_size != document['package']['size']:
        raise ReleaseGateError('最终 ZIP 的大小或 SHA-256 与已验签的发布清单不一致。')
    return {'passed': True, 'version': expected_version, 'authenticated': verified['authenticated'],
        'package_size': package.stat().st_size, 'package_sha256': digest,
        'uploaded': False, 'public_verification': 'pending'}


def key_destination(directory):
    """The wrapper creates this vault with a restricted ACL before init-key."""
    destination = _outside_project(directory)
    if destination.exists() or not destination.parent.is_dir():
        raise ReleaseGateError('密钥保险目录必须是外部尚不存在的新目录，父目录须存在。')
    return {'passed': True, 'vault': str(destination)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    pre = sub.add_parser('preflight')
    for name in ('expected-version', 'channel-file', 'trusted-keys-file', 'key-id',
                 'package-url', 'release-dir', 'output-dir', 'verification-dir', 'password-env'):
        pre.add_argument('--' + name, required=True)
    source = pre.add_mutually_exclusive_group(required=True)
    source.add_argument('--private-key-file')
    source.add_argument('--private-key-env')
    pre.add_argument('--existing', action='store_true')
    pre.add_argument('--report', dest='reports', action='append', default=[])
    runtime = sub.add_parser('runtime')
    for name in ('identity-file', 'release-dir', 'channel-file', 'expected-version'):
        runtime.add_argument('--' + name, required=True)
    published = sub.add_parser('published')
    for name in ('output-dir', 'trusted-keys-file', 'expected-version', 'package-url'):
        published.add_argument('--' + name, required=True)
    key = sub.add_parser('key-destination')
    key.add_argument('--directory', required=True)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    try:
        result = {'preflight': preflight, 'runtime': check_runtime,
                  'published': check_published, 'key-destination': key_destination}[command](**args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ReleaseGateError, UpdateValidationError, TransportError, PackageError, PublishError) as error:
        parser.exit(1, '发布门禁未通过：' + str(error) + '\n')
    except (OSError, ValueError, TypeError):
        parser.exit(1, '发布门禁未通过：输入、访问权限或文件结构无效。\n')


if __name__ == '__main__':
    raise SystemExit(main())
