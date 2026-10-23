"""Download and verify hosted release bytes without deployment or execution.

Production verification uses system TLS trust and rejects local/private hosts.
An explicitly selected local test can inject a verified test CA through the API;
its report can never be mistaken for public online acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import queue
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import zipfile

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data_migration import frozen_file
from core.update_manager import UpdateManager, UpdateError, UpdateCancelled, _json_bytes
from core.update_manifest import UpdateValidationError, parse_version, validate_trusted_keys
from core.update_transport import https_url, TransportError
from scripts.package_update import PackageError, _inventory, _plain, _read_stamp
from scripts.publish_release import (PublishError, _acceptance, _file_hash,
                                    _read_document, _save_new)


class HostedReleaseError(PublishError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def _report_url(value):
    """No query credentials or fragments belong in a persistent report."""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ''
        if ':' in hostname:
            hostname = '[' + hostname + ']'
        authority = hostname + (':' + str(parsed.port) if parsed.port is not None else '')
        return urlunsplit((parsed.scheme, authority, parsed.path, '', ''))
    except (ValueError, TypeError):
        return ''


def _cancel(event):
    if event is not None and event.is_set():
        raise UpdateCancelled('发布源验收已取消，未部署或启动任何应用。')


def _error_message(error):
    # OSError may embed a filename, a request URL or arbitrary remote data in
    # str(error). Keep its useful category without serializing that raw value.
    if isinstance(error, OSError):
        return f'文件或网络操作无法完成：{type(error).__name__}；请检查路径、访问权限及网络状态。'
    if isinstance(error, (ValueError, zipfile.BadZipFile)):
        return f'参数、JSON 或发布 ZIP 格式无效：{type(error).__name__}。'
    message = str(error)
    message = re.sub(r'https?://[^\s\'\"<>]+', '[HTTPS 地址已隐藏]', message, flags=re.IGNORECASE)
    message = re.sub(r'(?:[A-Za-z]:[\\/]|\\\\)[^\r\n;；，。\'\"<>]*', '[文件路径已隐藏]', message)
    return message[:1024]


def _release_inventory(release):
    inventory = _inventory(release)
    # release.json belongs to the update wrapper, not the accepted application.
    inventory.pop('release.json', None)
    return inventory


def _match_release_inventory(release, entries, cancel, progress, stage):
    """Hash every accepted EXE, tool and resource, not only its build identity."""
    inventory = _release_inventory(release)
    if set(inventory) != set(entries):
        raise PublishError('实际托管清单的完整文件列表与已验收发布目录不一致。')
    total = sum(value[0] for value in inventory.values())
    completed, verified = 0, []
    for name, metadata in inventory.items():
        _cancel(cancel)
        entry = entries[name]
        if metadata[0] != entry['size']:
            raise PublishError('实际托管清单的工具或资源大小与已验收完整发布目录不一致。')
        digest = hashlib.sha256()
        with frozen_file(release / name) as source:
            while block := source.read(1024 * 1024):
                _cancel(cancel)
                digest.update(block)
                completed += len(block)
                if progress:
                    progress({'stage':stage, 'message':'正在逐文件核对已验收发布目录',
                              'completed':completed, 'total':total})
        value = digest.hexdigest()
        if value != entry['sha256'].lower():
            raise PublishError('实际托管清单的工具或资源内容与已验收完整发布目录不一致。')
        verified.append({'path':name, 'size':metadata[0], 'sha256':value})
    if _release_inventory(release) != inventory:
        raise PublishError('验收期间本地完整发布目录发生变化，请重新验收。')
    serialized = json.dumps(verified, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {'files_count':len(verified), 'inventory_sha256':hashlib.sha256(serialized).hexdigest()}


def _public_host(url, cancel, timeout=5):
    parsed = urlsplit(https_url(url))
    hostname = parsed.hostname.rstrip('.').lower()
    if (hostname in ('localhost', 'example.com', 'example.net', 'example.org')
            or hostname.endswith(('.localhost', '.local', '.internal', '.test', '.invalid', '.example',
                                  '.example.com', '.example.net', '.example.org'))
            or '.' not in hostname and ':' not in hostname):
        raise PublishError('公网验收拒绝本地或测试地址；本地测试必须明确选择 local-test。')
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise PublishError('公网验收拒绝回环、私有、保留或不可公开路由的地址。')
        return [str(literal)]

    # DNS preflight is bounded too. A stopped resolver thread can only resolve a
    # hostname; it cannot write files, download bytes, or start other processes.
    responses = queue.Queue(maxsize=1)
    def resolve():
        try:
            values = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            responses.put((values, None))
        except OSError as error:
            responses.put((None, type(error).__name__))
    threading.Thread(target=resolve, daemon=True).start()
    deadline = time.monotonic() + timeout
    while True:
        _cancel(cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PublishError('公网发布源 DNS 预检超时，未执行下载。')
        try:
            values, error = responses.get(timeout=min(.05, remaining))
            break
        except queue.Empty:
            pass
    if error or not values:
        raise PublishError('公网发布源 DNS 预检失败，请检查真实托管地址。')
    try:
        addresses = sorted({ipaddress.ip_address(value[4][0]) for value in values}, key=str)
    except (ValueError, IndexError):
        raise PublishError('公网发布源 DNS 返回无效地址。') from None
    if not addresses or any(not value.is_global for value in addresses):
        raise PublishError('公网验收拒绝解析到私有、回环或保留地址的发布源。')
    return [str(value) for value in addresses]


def verify_hosted_release(source_url, *, release_dir, trusted_keys_file,
                          acceptance_receipt=None, local_test=False,
                          ssl_context=None, cancel=None, progress=None,
                          network_timeouts=None):
    """Verify real HTTPS manifest/ZIP bytes against an accepted local release.

    ``acceptance_receipt`` revalidates existing native reports when provided.
    The production CLI always requires one. The receipt is a local acceptance
    gate, not an independently unforgeable test attestation. No EXE is run here.
    Failure raises :class:`HostedReleaseError` carrying a sanitized report.
    """
    report = {'schema_version':1, 'kind':'MediaFlow.hosted-release-verification',
        'checked_at':datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'passed':False, 'scope':'local_https_integration' if local_test else 'public_https',
        'public_online_verified':False, 'source_url':_report_url(source_url),
        'package_url':'', 'signature_verified':False, 'package_integrity_verified':False,
        'release_matches_expected':False, 'acceptance_receipt_verified':False,
        'deployed':False, 'executable_started':False, 'temporary_files_removed':False,
        'stage':'configuration'}
    try:
        if type(local_test) is not bool:
            raise PublishError('local_test 必须明确为布尔值。')
        source_url = https_url(source_url)
        if ssl_context is not None and not local_test:
            raise PublishError('公网验收必须使用系统 TLS 信任；测试 SSLContext 仅限显式 local-test。')
        if ssl_context is not None and (ssl_context.verify_mode != ssl.CERT_REQUIRED
                                       or not ssl_context.check_hostname):
            raise PublishError('本地测试也必须校验证书和主机名。')
        _cancel(cancel)
        release = _plain(release_dir).resolve()
        stamp = _read_stamp(release)
        parse_version(stamp['app_version'])
        exe_sha = _file_hash(release / 'MediaFlow.exe')
        build_sha = _file_hash(release / 'build-info.json')
        key_path = _plain(trusted_keys_file).resolve()
        if key_path.is_relative_to(release):
            raise PublishError('可信公钥必须由发布包之外的独立配置提供。')
        key_document, _, _ = _read_document(key_path)
        keys = validate_trusted_keys(key_document.get('trusted_keys', key_document))
        if not keys:
            raise PublishError('尚未配置独立可信公钥，不能验收发布者身份。')
        if acceptance_receipt is not None:
            _acceptance(acceptance_receipt, stamp, exe_sha)
            report['acceptance_receipt_verified'] = True
        report['expected'] = {'version':stamp['app_version'], 'build_id':stamp['build_id'],
            'source_sha256':stamp['source_sha256'].lower(), 'exe_sha256':exe_sha}

        with tempfile.TemporaryDirectory(prefix='MediaFlow-hosted-verify-') as name:
            temporary = Path(name)
            manager = UpdateManager(temporary / 'updates', release, stamp,
                temporary / 'never-opened-user-data', ssl_context=ssl_context,
                health_data_root=temporary / 'never-used-health', network_timeouts=network_timeouts)
            report['stage'] = 'source_preflight'
            if not local_test:
                report['source_public_addresses'] = _public_host(source_url, cancel)
            manager.save_settings({'source_url':source_url, 'trusted_keys':keys})

            def emit(value):
                report['stage'] = value['stage']
                if progress:
                    progress(value)

            report['stage'] = 'manifest_download'
            checked = manager.check_updates(cancel=cancel, progress=emit)
            manifest = checked['manifest']
            report.update(signature_verified=checked['authenticated'],
                manifest_sha256=checked['manifest_sha256'], update_status=checked['status'],
                actual={'version':manifest['version'], 'build_id':manifest['build_id']},
                signing_key_id=manifest['signature']['key_id'],
                package_url=_report_url(manifest['package']['url']))
            report['stage'] = 'release_identity'
            entries = {entry['path']:entry for entry in manifest['files']}
            if (manifest['version'] != stamp['app_version'] or manifest['build_id'] != stamp['build_id']):
                raise PublishError('实际托管清单的版本或构建与已验收发布目录不一致。')
            if entries['MediaFlow.exe']['sha256'].lower() != exe_sha:
                raise PublishError('实际托管清单的 MediaFlow.exe SHA-256 与已验收 EXE 不一致。')
            if entries['build-info.json']['sha256'].lower() != build_sha:
                raise PublishError('实际托管清单的构建信息与已验收发布目录不一致。')
            report['stage'] = 'accepted_inventory'
            matched_inventory = _match_release_inventory(release, entries, cancel, emit, 'accepted_inventory')
            report['expected'].update(matched_inventory)
            if not local_test:
                report['package_public_addresses'] = _public_host(manifest['package']['url'], cancel)
            prepared = manager.download_update(manifest, cancel=cancel, progress=emit)
            report.update(package_integrity_verified=True,
                package_size=prepared['package_size'], package_sha256=prepared['package_sha256'])
            cache = manager.state_dir / 'downloads' / (prepared['id'] + '.zip')
            report['stage'] = 'packaged_identity'
            with frozen_file(cache) as reader, zipfile.ZipFile(reader) as archive:
                zipped_stamp = _json_bytes(archive.read('build-info.json'))
            if any(zipped_stamp.get(key) != stamp.get(key) for key in (
                    'app_version', 'build_id', 'source_sha256', 'packaged')):
                raise PublishError('实际托管 ZIP 中的构建标识与已验收版本不一致。')
            if (_read_stamp(release) != stamp or _file_hash(release / 'MediaFlow.exe') != exe_sha
                    or _file_hash(release / 'build-info.json') != build_sha):
                raise PublishError('验收期间本地发布目录发生变化，请重新验收。')
            report['stage'] = 'final_release_inventory'
            if _match_release_inventory(release, entries, cancel, emit, 'final_release_inventory') != matched_inventory:
                raise PublishError('验收期间本地完整发布目录发生变化，请重新验收。')
            if acceptance_receipt is not None:
                _acceptance(acceptance_receipt, stamp, exe_sha)
            _cancel(cancel)
            report['release_matches_expected'] = True
            report['stage'] = 'temporary_cleanup'
        report.update(passed=True, public_online_verified=not local_test,
            temporary_files_removed=True, stage='verified',
            message=('本地 HTTPS 集成验证通过；未进行真实公网验收。' if local_test else
                     '实际公网清单、签名和发布包验证通过；本工具未执行部署或启动。'))
        return report
    except (PublishError, PackageError, UpdateError, UpdateValidationError, TransportError,
            OSError, ValueError, zipfile.BadZipFile) as error:
        message = _error_message(error)
        report.update(error=message, error_type=type(error).__name__, cancelled=isinstance(error, UpdateCancelled),
            temporary_files_removed='temporary' not in locals() or not temporary.exists())
        raise HostedReleaseError(message, report) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='验证实际 HTTPS 发布资产；不部署、不启动 EXE。')
    parser.add_argument('--manifest-url', required=True)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--trusted-keys', required=True)
    parser.add_argument('--acceptance-receipt', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--local-test', action='store_true', help='仅标记本地集成测试，仍强制系统 TLS 校验。')
    args = parser.parse_args(argv)
    try:
        output = _plain(args.report).resolve()
        release = _plain(args.release_dir).resolve()
        if output.is_relative_to(release) or output.exists() or not output.parent.is_dir():
            raise PublishError('报告须位于发布目录之外的现有目录中，且不能覆盖已有文件。')
        try:
            report = verify_hosted_release(args.manifest_url, release_dir=release,
                trusted_keys_file=args.trusted_keys, acceptance_receipt=args.acceptance_receipt,
                local_test=args.local_test)
        except HostedReleaseError as error:
            report = error.report
        _save_new(output, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report['passed'] else 1
    except (PublishError, PackageError, OSError) as error:
        print(json.dumps({'passed':False, 'error':_error_message(error),
                          'error_type':type(error).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
