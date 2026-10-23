"""Independent, confirmed application deployment. Never migrates user data."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import uuid
import zipfile

from core.data_migration import DataDirectoryLease, MigrationError, frozen_file
from core.process_tree import ProcessTree
from core.update_transport import (TransportError, TransportCancelled, fetch as fetch_https, https_url)
from core.update_channel import load_channel, merge_keys
from core.update_manifest import (UpdateValidationError, canonical_bytes, parse_version,
    safe_release_path, validate_manifest, validate_trusted_keys,
    MAX_PACKAGE_BYTES, MAX_UNPACKED_BYTES, MAX_FILES)
from utils.build_info import runtime_identity
from utils.config import ConfigStore


class UpdateError(RuntimeError):
    pass


class UpdateCancelled(UpdateError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _cancel(event):
    if event is not None and event.is_set():
        raise UpdateCancelled('更新操作已取消；旧版本和用户数据保留。')


def _progress(callback, stage, message, completed=0, total=0):
    if callback:
        callback({'stage':stage, 'message':message, 'completed':completed, 'total':total})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError('JSON 包含重复字段。')
        result[key] = value
    return result


def _json_bytes(data):
    if len(data) > 2 * 1024 * 1024:
        raise UpdateError('发布清单或更新记录过大。')
    try:
        result = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise UpdateError('JSON 格式无效。') from None


def _regular_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise UpdateError('更新路径包含符号链接或重解析目录，已停止。')
    return path


def _read_json(path):
    path = _regular_path(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise UpdateError('更新配置或记录过大。')
    return _json_bytes(path.read_bytes())


def _atomic_json(path, value):
    path = _regular_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _sha(stream, cancel=None, progress=None, stage='verify', total=0):
    digest = hashlib.sha256()
    done = 0
    while block := stream.read(1024 * 1024):
        _cancel(cancel)
        digest.update(block)
        done += len(block)
        _progress(progress, stage, '正在核对文件 SHA-256', done, total)
    return digest.hexdigest()


def _https(value):
    try:
        return https_url(value)
    except TransportError as error:
        raise UpdateError(str(error)) from None


def _release_payload(document):
    return {key:value for key,value in document.items() if key not in ('signature','package')}


class UpdateManager:
    def __init__(self, state_dir=None, release_root=None, current_identity=None,
                 current_data_dir=None, ssl_context=None, health_data_root=None, launch_env=None, network_timeouts=None):
        local = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
        # Format-2/Ed25519 state must not make the preserved 1.9 RSA-only UI
        # unable to read its settings or operation records after a rollback.
        self.state_dir = _regular_path(state_dir or local / 'MediaFlow/Updates-v2').resolve()
        self.release_root = _regular_path(release_root or (
            Path(sys.executable).parent if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parents[1])).resolve()
        self.current_identity = dict(current_identity or runtime_identity())
        self.current_data_dir = _regular_path(current_data_dir or ConfigStore().path.parent).resolve()
        if (self.state_dir == self.release_root or self.state_dir.is_relative_to(self.release_root)
                or (self.release_root.is_relative_to(self.state_dir)
                    and self.release_root.parent != self.state_dir / 'versions')
                or self.state_dir == self.current_data_dir
                or self.current_data_dir.is_relative_to(self.state_dir)):
            # Private UI fixtures may put Updates next to their config, but never inside a release.
            raise UpdateError('更新状态目录必须与应用发布目录及实际用户数据目录分离。')
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.network_timeouts = {'connect':4, 'check':25, 'download':1800}
        if network_timeouts is not None:
            if (not isinstance(network_timeouts, dict) or set(network_timeouts) - set(self.network_timeouts)
                    or any(type(v) not in (int, float) or not 0.1 <= v <= (30 if k == 'connect' else 1800)
                           for k, v in network_timeouts.items())):
                raise UpdateError('更新网络超时配置无效。')
            self.network_timeouts.update(network_timeouts)
        self.health_data_root = Path(health_data_root).resolve() if health_data_root else None
        self.launch_env = dict(launch_env) if launch_env is not None else None
        self._lock = threading.Lock()
        self._launched = []

    @contextmanager
    def _operation(self):
        if not self._lock.acquire(blocking=False):
            raise UpdateError('已有更新操作正在执行，请等待或取消。')
        try:
            _regular_path(self.state_dir)
            with DataDirectoryLease(self.state_dir):
                yield
        except MigrationError:
            raise UpdateError('更新目录正被使用，或文件无法安全读取。请等待其他更新操作结束。') from None
        except UpdateValidationError as error:
            raise UpdateError(str(error)) from None
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            if isinstance(error, UpdateError):
                raise
            raise UpdateError(f'更新操作无法完成：{type(error).__name__}；请检查文件、访问权限与磁盘空间。') from None
        finally:
            self._lock.release()

    def load_settings(self):
        channel = load_channel()
        result = {'source_url':channel['source_url'], 'startup_check':False,
                  'trusted_keys':channel['trusted_keys'], 'channel_state':channel['key_state'],
                  'last_checked':'', 'last_status':'未检查'}
        path = self.state_dir / 'settings.json'
        if path.exists():
            values = _read_json(path)
            if 'source_url' in values:
                result['source_url'] = _https(values['source_url']) if values['source_url'] else ''
            result['startup_check'] = values.get('startup_check') is True
            result['trusted_keys'] = merge_keys(channel, values.get('trusted_keys', {}))
            for key in ('last_checked','last_status'):
                if isinstance(values.get(key), str):
                    result[key] = values[key][:512]
        return result

    def save_settings(self, values):
        with self._operation():
            current = self.load_settings()
            source = values.get('source_url', current['source_url']).strip()
            current['source_url'] = _https(source) if source else ''
            current['startup_check'] = values.get('startup_check', current['startup_check']) is True
            current['trusted_keys'] = merge_keys(load_channel(), values.get('trusted_keys', current['trusted_keys']))
            _atomic_json(self.state_dir / 'settings.json', current)
            return current

    def _fetch(self, url, *, cancel=None, progress=None, destination=None, size=None, limit=None):
        try:
            return fetch_https(url, context=self.ssl_context, cancel=cancel, progress=progress,
                destination=destination, size=size, limit=limit or 2*1024*1024,
                connect_timeout=self.network_timeouts['connect'],
                total_timeout=self.network_timeouts['download' if destination is not None else 'check'])
        except TransportCancelled as error:
            raise UpdateCancelled(str(error)) from None
        except TransportError as error:
            raise UpdateError(str(error)) from None

    def check_updates(self, cancel=None, progress=None):
        with self._operation():
            settings = self.load_settings()
            if not settings['source_url']:
                return {'status':'not_configured','message':'未配置在线更新源，可从本地导入发布 ZIP。',
                        'last_checked':settings['last_checked']}
            settings['last_checked'] = _now()
            try:
                value = _json_bytes(self._fetch(settings['source_url'], cancel=cancel, progress=progress))
                _progress(progress, 'signature', '正在验证发布清单数字签名')
                verified = validate_manifest(value, settings['trusted_keys'], require_signature=True)
                _progress(progress, 'signature_verified', '发布清单数字签名已通过验证', 1, 1)
                if not value.get('package'):
                    raise UpdateError('在线清单缺少发布包地址、大小和摘要。')
                current, latest = parse_version(self.current_identity['app_version']), parse_version(value['version'])
                status = 'update_available' if latest > current else 'older_available' if latest < current else (
                    'up_to_date' if value['build_id'] == self.current_identity['build_id'] else 'different_build')
                message = {'update_available':'发现可用新版本。','older_available':'更新源提供的是较旧版本，安装需要降级确认。',
                           'up_to_date':'已核对可信清单，当前版本与构建一致。','different_build':'版本号相同，但构建不同，请查看发布说明。'}[status]
                settings['last_status'] = message
                return dict(verified, status=status, message=message, last_checked=settings['last_checked'])
            except (UpdateError, UpdateValidationError) as error:
                settings['last_status'] = str(error)[:512]
                raise
            finally:
                _atomic_json(self.state_dir / 'settings.json', settings)

    def _space(self, needed):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.state_dir).free < needed + 64 * 1024 * 1024:
            raise UpdateError('更新目录所在磁盘空间不足；请腾出空间后重试。')

    def _record_path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise UpdateError('更新操作标识无效。')
        return self.state_dir / 'operations' / identifier / 'record.json'

    def _target(self, manifest):
        return self.state_dir / 'versions' / ('MediaFlow-' + manifest['version'] + '-' + manifest['build_id'])

    def _data_directory(self, version):
        if self.health_data_root:
            return self.health_data_root / version
        return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'MediaFlow' / version

    def _store(self, record):
        record['updated_at'] = _now()
        _atomic_json(self._record_path(record['id']), record)

    def result(self, identifier):
        record = _read_json(self._record_path(identifier))
        verified = validate_manifest(record['manifest'], self.load_settings()['trusted_keys'],
                                     require_signature=self._requires_publisher())
        if record.get('id') != identifier:
            raise UpdateError('更新记录身份不一致。')
        record['target_path'] = str(self._target(verified['manifest']))
        record.update(authenticated=verified['authenticated'], authentication=verified['authentication'])
        return record

    def _requires_publisher(self):
        # The normal 2.x UI always uses the executable's real identity. Historical
        # 1.x callers/fixtures retain the legacy import contract; a ZIP cannot
        # change this locally established application identity.
        return parse_version(self.current_identity['app_version']) >= parse_version('2.0.0')

    def _discard_cache(self, cache):
        """Only uncommitted UUID files created by this operation may be removed."""
        cache = _regular_path(cache)
        if (cache.parent != self.state_dir / 'downloads'
                or not re.fullmatch(r'[0-9a-f]{32}\.zip', cache.name)):
            raise UpdateError('临时更新文件超出专属缓存范围，未清理。')
        cache.unlink(missing_ok=True)

    def _discard_staging(self, staging, identifier):
        staging = _regular_path(staging)
        if (staging.parent != self.state_dir / 'staging'
                or not re.fullmatch(re.escape(identifier) + r'-[0-9a-f]{32}', staging.name)):
            raise UpdateError('临时更新目录超出专属暂存范围，未清理。')
        if staging.exists():
            for child in staging.rglob('*'):
                _regular_path(child)
            shutil.rmtree(staging)

    def _inspect(self, stream, settings, cancel, progress, outer=None):
        stream.seek(0)
        archive = zipfile.ZipFile(stream)
        infos = archive.infolist()
        if not infos or len(infos) > MAX_FILES * 2:
            raise UpdateError('ZIP 文件数量无效或超过安全上限。')
        by_name, case_names, directories = {}, set(), set()
        total = 0
        for info in infos:
            _cancel(cancel)
            original = info.orig_filename
            if original != info.filename or info.flag_bits & 1:
                raise UpdateError('ZIP 包含异常文件名或加密成员。')
            name = safe_release_path(original[:-1] if info.is_dir() else original)
            folded = unicodedata.normalize('NFC', name).casefold()
            if folded in case_names:
                raise UpdateError('ZIP 含重复文件名、大小写冲突或重复目录。')
            case_names.add(folded)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or info.external_attr & 0x400:
                raise UpdateError('ZIP 包含链接或特殊文件，禁止部署。')
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise UpdateError('ZIP 使用了不支持的压缩方式。')
            if info.is_dir():
                directories.add(name)
                continue
            if stat.S_ISDIR(mode):
                raise UpdateError('ZIP 文件类型与名称不一致。')
            if info.file_size > MAX_UNPACKED_BYTES or (info.file_size > 1024 * 1024 and
                    info.file_size > max(info.compress_size, 1) * 1000):
                raise UpdateError('ZIP 展开大小或压缩比异常。')
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateError('ZIP 展开总大小超过安全上限。')
            by_name[name] = info
        if 'release.json' not in by_name or by_name['release.json'].file_size > 2 * 1024 * 1024:
            raise UpdateError('ZIP 根目录缺少合法的 release.json 发布清单。')
        inner = _json_bytes(archive.read(by_name['release.json']))
        verified = validate_manifest(inner, settings['trusted_keys'],
                                     require_signature=not outer and self._requires_publisher())
        if outer:
            authenticated = validate_manifest(outer, settings['trusted_keys'], require_signature=True)
            if canonical_bytes(_release_payload(inner)) != canonical_bytes(_release_payload(outer)):
                raise UpdateError('下载包内部清单与可信在线清单不一致。')
            verified = authenticated
        manifest = verified['manifest']
        expected = {entry['path']:entry for entry in manifest['files']}
        if set(by_name) != set(expected) | {'release.json'}:
            raise UpdateError('ZIP 实际文件与发布清单不一致，存在缺失或未声明文件。')
        parents = {parent.as_posix() for name in expected for parent in Path(name).parents if str(parent) != '.'}
        if not directories.issubset(parents):
            raise UpdateError('ZIP 含未声明文件的目录。')
        if any(name.casefold() in {item.casefold() for item in by_name} for name in parents):
            raise UpdateError('ZIP 文件与目录名称冲突。')
        done = 0
        total = sum(entry['size'] for entry in expected.values())
        for name, entry in expected.items():
            if by_name[name].file_size != entry['size']:
                raise UpdateError('ZIP 内文件大小与清单不一致：' + name)
            with archive.open(by_name[name]) as source:
                digest = hashlib.sha256()
                while block := source.read(1024 * 1024):
                    _cancel(cancel)
                    digest.update(block)
                    done += len(block)
                    _progress(progress, 'verify', '完整校验发布文件', done, total)
            if digest.hexdigest() != entry['sha256'].lower():
                raise UpdateError('发布文件 SHA-256 不匹配：' + name)
        return archive, by_name, verified

    def _prepared(self, identifier, cache, digest, size, verified):
        manifest = verified['manifest']
        minimum = manifest.get('minimum_app_version')
        minimum_ok = not minimum or parse_version(self.current_identity['app_version']) >= parse_version(minimum)
        if not minimum_ok:
            raise UpdateError('当前版本低于发布清单要求的最低兼容版本，请先使用受支持的中间版本。')
        origin_exe = self.release_root / 'MediaFlow.exe'
        origin_sha = ''
        if origin_exe.is_file():
            with frozen_file(origin_exe) as source:
                origin_sha = _sha(source)
        record = {'id':identifier, 'state':'prepared','created_at':_now(),
            'version':manifest['version'],'build_id':manifest['build_id'],
            'release_notes':manifest['release_notes'],'release_page':manifest.get('release_page',''),
            'package_size':size,'package_sha256':digest,
            'unpacked_size':sum(entry['size'] for entry in manifest['files']),
            'files_count':len(manifest['files']), 'authenticated':verified['authenticated'],
            'authentication':verified['authentication'],'manifest':manifest,
            'target_path':str(self._target(manifest)),'current_path':str(self.release_root),
            'origin_sha256':origin_sha,'origin_version':self.current_identity['app_version'],
            'data_directory':str(self._data_directory(manifest['version'])),
            'migration_state':'未执行数据迁移；由新版本迁移助手单独预览并确认。',
            'is_downgrade':parse_version(manifest['version']) < parse_version(self.current_identity['app_version']),
            'minimum_compatible':minimum_ok,'message':'更新包完整性校验通过，等待部署确认。'}
        self._store(record)
        return record

    def prepare_local(self, path, cancel=None, progress=None):
        with self._operation():
            source = _regular_path(path)
            if source.suffix.lower() != '.zip' or not source.is_file():
                raise UpdateError('请选择可读取的本地发布 ZIP 文件。')
            size = source.stat().st_size
            if not 0 < size <= MAX_PACKAGE_BYTES:
                raise UpdateError('更新包大小无效或超过 4 GiB 上限。')
            self._space(size)
            identifier = uuid.uuid4().hex
            cache = self.state_dir / 'downloads' / (identifier + '.zip')
            cache.parent.mkdir(parents=True, exist_ok=True)
            _regular_path(cache)
            digest = hashlib.sha256()
            cache_created = False
            try:
                with frozen_file(source) as reader, cache.open('xb') as writer:
                    cache_created = True
                    done = 0
                    while block := reader.read(1024 * 1024):
                        _cancel(cancel)
                        writer.write(block); digest.update(block); done += len(block)
                        _progress(progress, 'copy', '正在缓存本地发布包', done, size)
                    writer.flush(); os.fsync(writer.fileno())
                    if done != size:
                        raise UpdateError('本地发布包在读取期间发生变化。')
                with frozen_file(cache) as stream:
                    if _sha(stream, cancel, progress, stage='package_hash', total=size) != digest.hexdigest():
                        raise UpdateError('缓存的本地发布包发生变化。')
                    archive, _, verified = self._inspect(stream, self.load_settings(), cancel, progress)
                    archive.close()
                return self._prepared(identifier, cache, digest.hexdigest(), size, verified)
            except Exception:
                if cache_created:
                    self._discard_cache(cache)
                raise

    def download_update(self, manifest, cancel=None, progress=None):
        with self._operation():
            settings = self.load_settings()
            validate_manifest(manifest, settings['trusted_keys'], require_signature=True)
            package = manifest.get('package')
            if not package:
                raise UpdateError('在线清单没有发布包信息。')
            minimum = manifest.get('minimum_app_version')
            if minimum and parse_version(self.current_identity['app_version']) < parse_version(minimum):
                raise UpdateError('当前版本低于发布清单要求的最低兼容版本，请先使用受支持的中间版本。')
            self._space(package['size'])
            identifier = uuid.uuid4().hex
            cache = self.state_dir / 'downloads' / (identifier + '.zip')
            cache.parent.mkdir(parents=True, exist_ok=True)
            _regular_path(cache)
            cache_created = False
            try:
                _progress(progress, 'signature_verified', '可信发布清单已验证，正在下载更新包', 1, 1)
                with cache.open('xb') as writer:
                    cache_created = True
                    size, digest = self._fetch(package['url'], destination=writer, size=package['size'],
                        limit=MAX_PACKAGE_BYTES, cancel=cancel, progress=progress)
                    writer.flush(); os.fsync(writer.fileno())
                if size != package['size'] or digest != package['sha256'].lower():
                    raise UpdateError('下载的 ZIP 大小或 SHA-256 与可信在线清单不一致。')
                with frozen_file(cache) as stream:
                    if (os.fstat(stream.fileno()).st_size != size
                            or _sha(stream, cancel, progress, stage='package_hash', total=size) != digest):
                        raise UpdateError('下载后的更新包缓存发生变化，已停止安装。')
                    archive, _, verified = self._inspect(stream, settings, cancel, progress, outer=manifest)
                    archive.close()
                _progress(progress, 'integrity_verified', '更新包完整性和结构校验通过，尚未部署', 1, 1)
                return self._prepared(identifier, cache, digest, size, verified)
            except Exception:
                if cache_created:
                    self._discard_cache(cache)
                raise

    def _validate_tree(self, target, manifest, cancel=None, progress=None):
        target = _regular_path(target)
        if not target.is_dir():
            raise UpdateError('部署目录不存在。')
        expected = {entry['path']:entry for entry in manifest['files']}
        actual = set()
        for path in target.rglob('*'):
            _regular_path(path)
            if path.is_file():
                actual.add(path.relative_to(target).as_posix())
        if actual != set(expected) | {'release.json'}:
            raise UpdateError('已部署版本文件结构发生变化，请重新检查；现有目录保留。')
        inner = _read_json(target / 'release.json')
        if canonical_bytes(_release_payload(inner)) != canonical_bytes(_release_payload(manifest)):
            raise UpdateError('已部署版本清单不一致。')
        for index, (name, item) in enumerate(expected.items()):
            path = target / name
            if path.stat().st_size != item['size']:
                raise UpdateError('已部署文件大小不一致：' + name)
            with frozen_file(path) as stream:
                if _sha(stream, cancel) != item['sha256'].lower():
                    raise UpdateError('已部署文件摘要不一致：' + name)
            _progress(progress, 'verify_deployment','核对独立版本文件',index + 1,len(expected))
        identity = _read_json(target / 'build-info.json')
        if identity.get('app_version') != manifest['version'] or identity.get('build_id') != manifest['build_id']:
            raise UpdateError('发布文件中的构建标识与清单不符。')

    def deploy(self, identifier, confirmed=False, allow_unsigned=False, allow_downgrade=False,
               cancel=None, progress=None):
        with self._operation():
            if not confirmed:
                raise UpdateError('请检查部署预览并确认安装。')
            record = self.result(identifier)
            manifest = record['manifest']
            verified = validate_manifest(manifest, self.load_settings()['trusted_keys'],
                                         require_signature=self._requires_publisher())
            if not verified['authenticated'] and not allow_unsigned:
                raise UpdateError('本地包未通过发布者身份认证；必须明确确认信任来源后才能部署。')
            if record['is_downgrade'] and not allow_downgrade:
                raise UpdateError('这是降级安装；请单独确认，旧版本数据不会自动降级。')
            target = _regular_path(self._target(manifest))
            if target.exists():
                if record['state'] in ('deployed','healthy','health_failed','deploying','interrupted'):
                    self._validate_tree(target, manifest, cancel, progress)
                    if record['state'] in ('deploying','interrupted'):
                        record.update(state='deployed', message='已核对先前完成的独立部署。')
                        self._store(record)
                    return record
                raise UpdateError('目标版本目录已经存在，已停止部署；不会覆盖已有目录。')
            self._space(record['unpacked_size'])
            cache = _regular_path(self.state_dir / 'downloads' / (identifier + '.zip'))
            if not cache.is_file() or cache.stat().st_size != record['package_size']:
                raise UpdateError('已缓存的更新包缺失或大小变化，请重新导入。')
            staging = _regular_path(self.state_dir / 'staging' / (identifier + '-' + uuid.uuid4().hex))
            record.update(state='deploying',staging_path=str(staging),message='正在暂存独立版本。')
            self._store(record)
            committed = False
            staging_created = False
            try:
                with frozen_file(cache) as stream:
                    if _sha(stream, cancel, progress, total=record['package_size']) != record['package_sha256']:
                        raise UpdateError('导入后更新包发生变化，请重新导入并检查来源。')
                    archive, members, checked = self._inspect(stream, self.load_settings(), cancel, progress,
                        outer=manifest if manifest.get('package') else None)
                    if canonical_bytes(checked['manifest']) != canonical_bytes(manifest):
                        raise UpdateError('更新包清单发生变化，必须重新预览。')
                    staging.mkdir(parents=True, exist_ok=False)
                    staging_created = True
                    done = 0
                    extraction_total = sum(info.file_size for info in members.values())
                    for name, info in members.items():
                        _cancel(cancel)
                        destination = _regular_path(staging / name)
                        if not destination.is_relative_to(staging):
                            raise UpdateError('更新路径超出暂存目录。')
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info) as source, destination.open('xb') as output:
                            while block := source.read(1024 * 1024):
                                _cancel(cancel)
                                output.write(block); done += len(block)
                                _progress(progress,'extract','正在展开独立版本',done,extraction_total)
                            output.flush(); os.fsync(output.fileno())
                    archive.close()
                self._validate_tree(staging, manifest, cancel, progress)
                _cancel(cancel)
                target.parent.mkdir(parents=True, exist_ok=True)
                _regular_path(target)
                if target.exists():
                    raise UpdateError('目标目录在部署期间出现，已保留暂存结果并停止。')
                # Windows rename refuses an existing destination; no REPLACE_EXISTING flag.
                os.rename(staging, target)
                committed = True
                record.update(state='deployed',message='已部署到独立目录，尚未启动；数据迁移未执行。')
                self._store(record)
                return record
            except Exception as error:
                if isinstance(error, UpdateCancelled) and not committed and staging_created:
                    self._discard_staging(staging, identifier)
                record.update(state='deploying' if committed else (
                                  'cancelled' if isinstance(error, UpdateCancelled) else 'failed'),
                              message='版本目录已提交，但记录未完成保存；下次打开时将重新核对。' if committed
                              else str(error)[:1024] if isinstance(error,(UpdateError,UpdateValidationError))
                              else '部署中断，暂存数据保留供诊断；旧版本仍可使用。')
                try:
                    self._store(record)
                except OSError:
                    # The durable pre-commit journal still permits recovery after storage errors.
                    pass
                raise

    def list_versions(self):
        directory = self.state_dir / 'operations'
        if not directory.exists():
            return []
        results = []
        for path in sorted(directory.glob('*/record.json'), key=lambda item:item.stat().st_mtime, reverse=True)[:1000]:
            try:
                results.append(self.result(path.parent.name))
            except (UpdateError, UpdateValidationError, OSError) as error:
                results.append({'id':path.parent.name,'state':'failed','message':str(error),
                                'version':'未知','build_id':'','target_path':''})
        return results

    def recover_interrupted(self):
        with self._operation():
            changed = []
            for record in self.list_versions():
                if record['state'] != 'deploying':
                    continue
                try:
                    target = self._target(record['manifest'])
                    if target.exists():
                        self._validate_tree(target, record['manifest'])
                        record.update(state='deployed',message='已验证上次中断前完成的部署；尚未启动。')
                    else:
                        record.update(state='interrupted',message='上次部署中断；暂存文件保留，可重新确认后重试。')
                except (UpdateError, OSError, UpdateValidationError):
                    record.update(state='interrupted',message='上次部署不完整或存在冲突；未覆盖目标，请查看目录后重新导入。')
                self._store(record); changed.append(record)
            return changed

    def check_health(self, identifier, confirmed=False, cancel=None, progress=None):
        with self._operation():
            if not confirmed:
                raise UpdateError('健康检查需要执行新版本 EXE，请明确确认。')
            record = self.result(identifier)
            if record['state'] not in ('deployed','healthy','health_failed'):
                raise UpdateError('版本尚未完成部署。')
            self._validate_tree(self._target(record['manifest']), record['manifest'], cancel, progress)
            directory = self._record_path(identifier).parent
            report = directory / ('health-' + uuid.uuid4().hex + '.json')
            command = [str(self._target(record['manifest']) / 'MediaFlow.exe'), '--update-health', str(report),
                       '--expected-version',record['version'],'--expected-build',record['build_id'],
                       '--health-data-directory',str(self._data_directory(record['version']))]
            process = tree = None
            try:
                _cancel(cancel)
                _progress(progress,'health','正在实际启动新版本执行有限健康检查')
                process = subprocess.Popen(command, cwd=str(self._target(record['manifest'])),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=self._child_environment())
                tree = ProcessTree(process)
                deadline = time.monotonic() + 90
                while process.poll() is None:
                    _cancel(cancel)
                    if time.monotonic() > deadline:
                        raise UpdateError('新版本启动健康检查超时，失败版本保留，可打开旧版本。')
                    time.sleep(0.05)
                health = _read_json(report) if report.is_file() else {'passed':False,'message':'新版本未返回健康检查报告。'}
                identity = health.get('identity', {})
                passed = bool(process.returncode == 0 and health.get('passed') is True
                    and identity.get('app_version') == record['version']
                    and identity.get('build_id') == record['build_id'] and identity.get('packaged') is True)
                health['passed'] = passed
                if not passed and not health.get('message'):
                    health['message'] = '新版本健康检查失败或运行身份不一致，旧版本保留。'
            except (OSError, UpdateError) as error:
                if process is not None and process.poll() is None:
                    tree.terminate() if tree is not None else process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
                health = {'passed':False,'message':str(error) if isinstance(error,UpdateError)
                          else '新版本进程启动失败，请检查文件是否损坏或被系统拦截。'}
                record.update(state='health_failed',health=health,message=health['message'])
                self._store(record)
                if isinstance(error,UpdateCancelled):
                    raise
                return health
            finally:
                if tree is not None:
                    tree.close()
            record.update(state='healthy' if passed else 'health_failed',health=health,
                          message='新版本有限启动检查通过，等待打开确认。' if passed else health['message'])
            self._store(record)
            return health

    def _child_environment(self):
        environment = dict(os.environ if self.launch_env is None else self.launch_env)
        # Each application instance must own its onefile extraction lifetime.
        environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        return environment

    def _launch(self, executable):
        executable = _regular_path(executable)
        if executable.name != 'MediaFlow.exe' or not executable.is_file():
            raise UpdateError('登记的 MediaFlow.exe 已移动或无法访问。')
        try:
            process = subprocess.Popen([str(executable)], cwd=str(executable.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0), env=self._child_environment())
        except OSError:
            raise UpdateError('Windows 未能启动该版本，请检查文件或安全软件提示。') from None
        self._launched.append(process)
        return {'started':True,'pid':process.pid,'path':str(executable),
                'message':'已发送独立启动请求；当前版本保留。数据迁移需在新版本中单独确认。'}

    def launch_new(self, identifier, confirmed=False):
        with self._operation():
            if not confirmed:
                raise UpdateError('请确认打开新版本。')
            record = self.result(identifier)
            if record['state'] != 'healthy' or not record.get('health',{}).get('passed'):
                raise UpdateError('请先通过新版本启动健康检查；失败时可打开旧版本。')
            target = self._target(record['manifest'])
            self._validate_tree(target, record['manifest'])
            return self._launch(target / 'MediaFlow.exe')

    def launch_previous(self, identifier, confirmed=False):
        with self._operation():
            if not confirmed:
                raise UpdateError('请确认打开登记的旧版本。')
            record = self.result(identifier)
            executable = _regular_path(Path(record['current_path']) / 'MediaFlow.exe')
            if not record.get('origin_sha256') or not executable.is_file():
                raise UpdateError('旧版本 EXE 不可用，请从保留的原发布目录打开。')
            with frozen_file(executable) as source:
                if _sha(source) != record['origin_sha256']:
                    raise UpdateError('旧版本 EXE 与登记摘要不一致，已停止自动打开。')
            return self._launch(executable)
