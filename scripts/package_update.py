"""Build a local update ZIP from a verified complete release folder.

Default calls remain unsigned. An explicit publisher-supplied hook may sign the
manifest; this module does not load or create keys, upload, or run executables.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import zipfile

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data_migration import frozen_file
from core.update_manifest import (MAX_FILES, MAX_PACKAGE_BYTES, MAX_UNPACKED_BYTES,
    UpdateValidationError, canonical_bytes, validate_manifest)


class PackageError(RuntimeError):
    pass


def _plain(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        info = part.lstat() if part.exists() or part.is_symlink() else None
        if info and (stat.S_ISLNK(info.st_mode)
                     or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise PackageError('发布路径含链接或重解析点，已停止打包。')
    return path


def _inventory(root):
    files = {}
    total = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in names:
            child = _plain(parent / name)
            relative = child.relative_to(root).as_posix()
            parts = relative.split('/')
            if not (relative in ('tools', 'tools/webview2')
                    or (parts[0] == 'licenses' and len(parts) <= 4)):
                raise PackageError('发布目录包含不允许的目录：' + relative)
        for name in filenames:
            path = _plain(parent / name)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PackageError('发布目录包含特殊文件或硬链接。')
            relative = path.relative_to(root).as_posix()
            files[relative] = (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)
            total += info.st_size
            if len(files) > MAX_FILES or total > MAX_UNPACKED_BYTES:
                raise PackageError('发布文件数量或展开大小超过更新器安全上限。')
    return dict(sorted(files.items()))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError('构建信息包含重复 JSON 字段。')
        result[key] = value
    return result


def _read_stamp(root):
    path = root / 'build-info.json'
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise PackageError('完整发布目录缺少有效 build-info.json。')
    try:
        with frozen_file(path) as stream:
            stamp = json.loads(stream.read().decode('utf-8'), object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        raise PackageError('构建信息不是有效 UTF-8 JSON。') from error
    if (not isinstance(stamp, dict) or stamp.get('packaged') is not True
            or not isinstance(stamp.get('source_sha256'), str)
            or len(stamp['source_sha256']) != 64
            or any(char not in '0123456789abcdefABCDEF' for char in stamp['source_sha256'])
            or not isinstance(stamp.get('app_version'), str)
            or not isinstance(stamp.get('build_id'), str)):
        raise PackageError('构建信息必须来自实际打包版本，并包含版本、构建标识和源码 SHA-256。')
    return stamp


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def _publish(temporary, destination):
    # Same-volume hard-link creation is atomic and never overwrites an existing path.
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise PackageError('输出文件已存在，未覆盖：' + str(destination)) from None
    except OSError as error:
        raise PackageError('输出目录不支持安全提交，未替换任何已有文件：' + type(error).__name__) from None
    temporary.unlink()


def create_package(release_dir, output_zip, *, release_notes='',
                   minimum_app_version='1.8.0', release_page='', manifest_fields=None,
                   sign_manifest=None, trusted_keys=None, expected_exe_sha256='') -> dict:
    """Return package_path/metadata_path, ZIP size/SHA, and the validated manifest.

    The external ``<output>.metadata.json`` is an inventory receipt, not a trust
    root. Defaults preserve format 1; the optional signing hook may add only the
    signature to the fully hashed manifest. No private key enters this helper.
    """
    root = _plain(release_dir).resolve()
    output = _plain(output_zip).resolve()
    if not root.is_dir():
        raise PackageError('发布目录不存在或不可访问。')
    if output.suffix.lower() != '.zip':
        raise PackageError('更新包输出必须使用 .zip 扩展名。')
    if output.is_relative_to(root):
        raise PackageError('更新 ZIP 必须输出到发布目录之外，原发布目录保持只读。')
    metadata_path = output.with_name(output.name + '.metadata.json')
    _plain(metadata_path)
    if output.exists() or metadata_path.exists():
        raise PackageError('ZIP 或外部元数据输出已存在，未覆盖。')
    if not output.parent.is_dir():
        raise PackageError('输出父目录不存在，请先创建独立的发布包目录。')
    inventory = _inventory(root)
    stamp = _read_stamp(root)
    manifest = {'schema_version': 1, 'product': 'MediaFlow', 'version': stamp['app_version'],
                'build_id': stamp['build_id'], 'platform': 'windows', 'architecture': 'x86_64',
                'release_notes': release_notes, 'release_page': release_page,
                'minimum_app_version': minimum_app_version, 'data_migration': 'separate',
                'files': [{'path': path, 'size': info[0], 'sha256': '0' * 64}
                          for path, info in inventory.items()]}
    if manifest_fields is not None:
        if type(manifest_fields) is not dict or set(manifest_fields) - {'schema_version', 'published_at', 'signing'}:
            raise PackageError('仅允许显式添加发布清单格式、发布时间和签名身份。')
        manifest.update(deepcopy(manifest_fields))
    if (not isinstance(expected_exe_sha256, str) or expected_exe_sha256 and
            (len(expected_exe_sha256) != 64 or any(c not in '0123456789abcdefABCDEF' for c in expected_exe_sha256))):
        raise PackageError('验收 EXE SHA-256 格式无效。')
    # Reuse exactly the client's allowlist before opening arbitrary release files.
    validate_manifest(manifest, trusted_keys)
    total = sum(info[0] for info in inventory.values())
    if shutil.disk_usage(output.parent).free < total + 1024 * 1024:
        raise PackageError('输出磁盘空间不足，不能安全生成完整更新包。')
    temporary_files = []
    try:
        with ExitStack() as sources:
            handles = {path: sources.enter_context(frozen_file(root / path)) for path in inventory}
            for item in manifest['files']:
                stream = handles[item['path']]
                digest = hashlib.sha256()
                size = 0
                while block := stream.read(1024 * 1024):
                    size += len(block)
                    digest.update(block)
                if size != item['size']:
                    raise PackageError('打包期间发布文件发生变化，请重新生成完整发布目录。')
                item['sha256'] = digest.hexdigest()
                stream.seek(0)
            executable = next(item for item in manifest['files'] if item['path'] == 'MediaFlow.exe')
            if expected_exe_sha256 and executable['sha256'] != expected_exe_sha256.lower():
                raise PackageError('实际打包 EXE 与已通过验收的 EXE SHA-256 不一致。')
            if sign_manifest is not None:
                signed = sign_manifest(deepcopy(manifest))
                if type(signed) is not dict or canonical_bytes(signed) != canonical_bytes(manifest):
                    raise PackageError('签名钩子修改了发布身份或文件清单，已停止打包。')
                verified = validate_manifest(signed, trusted_keys, require_signature=True)
                manifest = verified['manifest']
            else:
                verified = validate_manifest(manifest, trusted_keys)
            if _inventory(root) != inventory:
                raise PackageError('打包期间发布目录内容发生变化。')
            descriptor, name = tempfile.mkstemp(prefix='.mediaflow-package-', suffix='.tmp', dir=output.parent)
            temporary = Path(name)
            temporary_files.append(temporary)
            with os.fdopen(descriptor, 'w+b') as destination:
                with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED,
                                     compresslevel=6, allowZip64=True) as archive:
                    archive.writestr('release.json', _bytes(manifest))
                    for item in manifest['files']:
                        info = zipfile.ZipInfo(item['path'])
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = (stat.S_IFREG | 0o644) << 16
                        digest = hashlib.sha256()
                        with archive.open(info, 'w', force_zip64=True) as writer:
                            while block := handles[item['path']].read(1024 * 1024):
                                digest.update(block)
                                writer.write(block)
                        if digest.hexdigest() != item['sha256']:
                            raise PackageError('打包读取结果与发布清单不一致，未提交更新包。')
                        member = archive.getinfo(item['path'])
                        if (member.file_size > 1024 * 1024
                                and member.file_size > max(member.compress_size, 1) * 1000):
                            raise PackageError('发布文件压缩比超过更新器允许上限。')
                destination.flush()
                os.fsync(destination.fileno())
                package_size = destination.tell()
                if not 0 < package_size <= MAX_PACKAGE_BYTES:
                    raise PackageError('更新 ZIP 大小超过 4 GiB 安全上限。')
                destination.seek(0)
                package_sha = hashlib.file_digest(destination, 'sha256').hexdigest()
            if _inventory(root) != inventory:
                raise PackageError('打包期间发布目录内容发生变化，未提交更新包。')
        metadata = {'schema_version': 1, 'kind': 'MediaFlow.update-package-metadata',
                    'product': 'MediaFlow', 'version': manifest['version'],
                    'build_id': manifest['build_id'], 'format': 'zip', 'filename': output.name,
                    'size': package_size, 'sha256': package_sha,
                    'manifest_sha256': verified['manifest_sha256'], 'authenticated': verified['authenticated'],
                    'authentication': verified['authentication']}
        descriptor, name = tempfile.mkstemp(prefix='.mediaflow-package-metadata-', suffix='.tmp', dir=output.parent)
        metadata_temp = Path(name)
        temporary_files.append(metadata_temp)
        with os.fdopen(descriptor, 'wb') as writer:
            writer.write(_bytes(metadata))
            writer.flush()
            os.fsync(writer.fileno())
        _publish(temporary, output)
        _publish(metadata_temp, metadata_path)
        return {**metadata, 'package_path': str(output), 'metadata_path': str(metadata_path),
                'manifest': manifest}
    finally:
        for temporary in temporary_files:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='生成 MediaFlow 本地更新 ZIP（不签名、不联网）')
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--notes', default='')
    parser.add_argument('--minimum-app-version', default='1.8.0')
    parser.add_argument('--release-page', default='')
    args = parser.parse_args(argv)
    try:
        result = create_package(args.release_dir, args.output, release_notes=args.notes,
            minimum_app_version=args.minimum_app_version, release_page=args.release_page)
    except (OSError, RuntimeError, UpdateValidationError) as error:
        parser.exit(1, '打包失败：' + str(error) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'manifest'},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
