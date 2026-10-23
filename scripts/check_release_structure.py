"""Read-only CI gate for a complete portable MediaFlow build.

This checks files, identity, public-text hygiene and dependency provenance. It
does not sign, publish, deploy, execute the EXE or claim media-download acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if __package__ in ('', None):
    sys.path.insert(0, str(ROOT))

from core.update_manifest import (REQUIRED_FILES, UpdateValidationError,
    _allowed_release_file, parse_version)
from scripts.package_update import PackageError, _inventory, _plain, _read_stamp


REQUIRED_DOCUMENTS = (
    'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'MediaFlow-Qt.cmd',
    'MEDIAFLOW_DASH_FIX.md', 'MEDIAFLOW_BROWSER_LAYER.md', 'MEDIAFLOW_WEBVIEW2.md',
    'MEDIAFLOW_BROWSER_ENVIRONMENT.md', 'MEDIAFLOW_DETAILS_QUALITY.md',
    'MEDIAFLOW_BRAND_THUMBNAILS.md', 'PROTOCOL_REFACTOR.md', 'MEDIA_DISCOVERY.md',
    'MEDIAFLOW_CONFIDENCE.md', 'RESOLVER_UPGRADE.md', 'ROU_CURRENT_PROTOCOL.md',
    'ROU_PIPELINE_FIX.md', 'BATCH_DOWNLOAD.md', 'MEDIAFLOW_DASH_RELEASE_FIX.md',
    'MEDIAFLOW_RECOVERY.md', 'MEDIAFLOW_DOUYIN_FIX.md', 'MEDIAFLOW_TOOLS.md',
    'MEDIAFLOW_LIBRARY.md', 'MEDIAFLOW_BATCH_IMPORT.md', 'MEDIAFLOW_TRACKS.md',
    'MEDIAFLOW_ORGANIZATION.md', 'MEDIAFLOW_STORAGE.md', 'MEDIAFLOW_MIGRATION.md',
    'MIGRATION_AUDIT.md', 'MEDIAFLOW_UPDATE.md', 'PUBLISHING.md',
    'PRODUCTION_RELEASE.md', 'UPDATE_AUDIT.md',
)
REQUIRED_TOOL_NOTICES = (
    'tools/FFmpeg-LICENSE', 'tools/FFmpeg-README.txt', 'tools/yt-dlp-LICENSE.txt',
    'tools/yt-dlp-THIRD_PARTY_LICENSES.txt', 'tools/provenance.json',
    'tools/webview2/LICENSE.txt', 'tools/webview2/NOTICE.txt',
    'tools/webview2/MediaFlow.WebView2Host.exe.config',
)
LICENSE_PACKAGES = ('pyside6-', 'pyside6_addons-', 'pyside6_essentials-', 'shiboken6-',
                    'cryptography-', 'cffi-', 'pycparser-')
FEATURES = ('dash', 'track', 'organization', 'storage', 'migration', 'update')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_BUILD = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z')
_PRIVATE_PEM = re.compile(r'-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----')
_PERSONAL_PATH = re.compile(r'(?i)(?:[a-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+|'
                            r'[a-z]:[\\/]+create[ _]program(?:-codex)?[\\/]+|/home/[^/\s]+/)')
_AUTH_HEADER = re.compile(r'(?im)^\s*(?:Cookie|Authorization|Proxy-Authorization)\s*:\s*\S+')
_CREDENTIAL_FIELD = re.compile(r'(?i)\A(?:cookie|cookies|authorization|password|passwd|'
    r'access[_-]?token|api[_-]?key|secret|private[_-]?key|request[_-]?headers)\Z')
_PUBLIC_TEXT = {'.md', '.txt', '.json', '.html', '.cmd', '.config'}


class ReleaseStructureError(RuntimeError):
    pass


def _unique(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ReleaseStructureError('JSON 中存在重复字段。')
        value[name] = item
    return value


def _document(path):
    try:
        value = json.loads(path.read_text('utf-8-sig'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise ReleaseStructureError('发布 JSON 必须为有效 UTF-8，且不能包含重复字段或非有限数字。') from None
    if type(value) is not dict:
        raise ReleaseStructureError('发布 JSON 顶层必须为对象。')
    return value


def _public_fields(value):
    if isinstance(value, dict):
        for name, item in value.items():
            if _CREDENTIAL_FIELD.fullmatch(name):
                raise ReleaseStructureError('公开 JSON 包含凭据或授权字段，停止交付。')
            _public_fields(item)
    elif isinstance(value, list):
        for item in value:
            _public_fields(item)


def _text_hygiene(path, relative):
    if path.suffix.lower() not in _PUBLIC_TEXT and not path.name.startswith(('LICENSE', 'FFmpeg-LICENSE')):
        return
    try:
        content = path.read_text('utf-8-sig')
    except UnicodeError:
        raise ReleaseStructureError('公开文本不是可检查的 UTF-8：' + relative) from None
    if _PRIVATE_PEM.search(content) or _PERSONAL_PATH.search(content) or _AUTH_HEADER.search(content):
        raise ReleaseStructureError('公开文本含私钥标记、个人绝对路径或授权头：' + relative)
    if path.suffix.lower() == '.json':
        _public_fields(_document(path))


def _pe_header(path, relative):
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b'MZ':
            raise ReleaseStructureError('必需程序或 DLL 缺少 Windows PE 文件头：' + relative)
        offset = struct.unpack_from('<I', header, 60)[0]
        if not 64 <= offset <= min(path.stat().st_size - 24, 1024 * 1024):
            raise ReleaseStructureError('Windows PE 头偏移无效：' + relative)
        stream.seek(offset)
        signature = stream.read(24)
        if len(signature) != 24 or signature[:4] != b'PE\0\0':
            raise ReleaseStructureError('Windows PE 签名或 COFF 头无效：' + relative)
        machine = struct.unpack_from('<H', signature, 4)[0]
        if machine not in (0x14C, 0x8664) or relative == 'MediaFlow.exe' and machine != 0x8664:
            raise ReleaseStructureError('程序架构不受支持，MediaFlow 必须为 Windows x64：' + relative)


def _identity(stamp, expected_version):
    parse_version(expected_version)
    if stamp.get('app_version') != expected_version or stamp.get('packaged') is not True:
        raise ReleaseStructureError('发布身份中的版本或 packaged 标记不符合预期。')
    if not _BUILD.fullmatch(stamp.get('build_id', '')) or not _SHA.fullmatch(stamp.get('source_sha256', '')):
        raise ReleaseStructureError('发布构建标识或源码 SHA-256 无效。')
    if any(stamp.get(name + '_module_loaded') is not True for name in FEATURES):
        raise ReleaseStructureError('发布身份缺少下载、音轨、整理、存储、迁移或更新模块。')
    if 'executable' in stamp or 'runtime_root' in stamp:
        raise ReleaseStructureError('公开 build-info.json 不得包含运行时绝对路径字段。')
    channel = stamp.get('update_channel')
    if (not isinstance(channel, dict) or channel.get('key_state') not in ('pending', 'initialized')
            or not _SHA.fullmatch(channel.get('source_sha256', ''))
            or not isinstance(channel.get('key_fingerprints'), dict)
            or any(not _SHA.fullmatch(value) for value in channel['key_fingerprints'].values())):
        raise ReleaseStructureError('正式渠道状态或公钥指纹无效；不能交付 invalid 渠道。')
    if channel['key_state'] == 'pending' and (channel['key_fingerprints'] or
            channel['source_sha256'] != hashlib.sha256(b'').hexdigest()):
        raise ReleaseStructureError('待初始化渠道不能冒充已配置更新源或正式公钥。')
    if channel['key_state'] == 'initialized' and not channel['key_fingerprints']:
        raise ReleaseStructureError('已初始化渠道缺少独立可信公钥指纹。')


def check_release(release_dir, expected_version, *, runtime_identity=None):
    root = _plain(release_dir).resolve()
    if not root.is_dir():
        raise ReleaseStructureError('需要完整的发布文件夹。')
    before = _inventory(root)
    required = (set(REQUIRED_FILES) | set(REQUIRED_DOCUMENTS) | set(REQUIRED_TOOL_NOTICES)
                | {'licenses/QtWebEngine/provenance.json'})
    if missing := sorted(required - set(before)):
        raise ReleaseStructureError('发布目录缺少必需文件：' + ', '.join(missing))
    if any(not before[name][0] for name in before):
        raise ReleaseStructureError('发布目录包含空文件。')
    hashes = {}
    for name in before:
        _allowed_release_file(name)
        if '/' not in name and name not in required and name != 'FILES.sha256':
            raise ReleaseStructureError('发布根目录包含未声明的附加文件：' + name)
        path = root / name
        _text_hygiene(path, name)
        if path.suffix.lower() in ('.exe', '.dll'):
            _pe_header(path, name)
        with path.open('rb') as stream:
            hashes[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    stamp = _read_stamp(root)
    _identity(stamp, expected_version)
    runtime_verified = False
    if runtime_identity is not None:
        actual = _document(_plain(runtime_identity))
        expected = {name: actual[name] for name in actual if name not in ('executable', 'runtime_root')}
        if expected != stamp or Path(actual.get('executable', '')).resolve() != root / 'MediaFlow.exe':
            raise ReleaseStructureError('实际 EXE 身份报告与脱敏发布身份不一致。')
        runtime_verified = True
    for prefix in LICENSE_PACKAGES:
        names = [name for name in before if name.lower().startswith('licenses/' + prefix)
                 and '.dist-info/' in name.lower()]
        if not names:
            raise ReleaseStructureError('缺少运行依赖许可：' + prefix.rstrip('-'))
        if prefix == 'cryptography-' and not all(any(name.endswith('/' + leaf) for name in names)
                for leaf in ('LICENSE', 'LICENSE.APACHE', 'LICENSE.BSD')):
            raise ReleaseStructureError('cryptography 许可文件不完整。')
    notices = _document(root / 'licenses/QtWebEngine/provenance.json')
    pages = notices.get('files')
    if not isinstance(pages, list) or not 10 <= len(pages) <= 200:
        raise ReleaseStructureError('QtWebEngine/Chromium 第三方许可列表不完整。')
    seen = set()
    for page in pages:
        if (not isinstance(page, dict) or not isinstance(page.get('file'), str)
                or Path(page['file']).name != page['file'] or '\\' in page['file']
                or page['file'] in seen):
            raise ReleaseStructureError('Qt 第三方许可记录包含重复或无效路径。')
        seen.add(page['file'])
        name = 'licenses/QtWebEngine/' + page['file']
        if name not in before or hashes[name] != page.get('sha256') or before[name][0] != page.get('size'):
            raise ReleaseStructureError('Qt 第三方许可正文与记录的大小或 SHA-256 不符。')
    provenance = _document(root / 'tools/provenance.json')
    for tool in ('yt-dlp.exe', 'ffmpeg.exe', 'ffprobe.exe'):
        record = provenance.get(tool, {})
        source = urlsplit(record.get('source', ''))
        if (source.scheme != 'https' or not source.hostname or source.username or source.password
                or hashes['tools/' + tool] != record.get('sha256')):
            raise ReleaseStructureError('工具来源或实际 SHA-256 不符合构建来源记录：' + tool)
    if _inventory(root) != before:
        raise ReleaseStructureError('发布目录在检查期间发生变化。')
    return {'schema_version': 1, 'product': 'MediaFlow', 'passed': True,
        'version': stamp['app_version'], 'build_id': stamp['build_id'],
        'source_sha256': stamp['source_sha256'], 'files': len(before),
        'total_bytes': sum(item[0] for item in before.values()), 'sha256': hashes,
        'runtime_identity_verified': runtime_verified, 'native_exe_executed': False,
        'signed': False, 'published': False, 'scope': 'portable release structure and public-data hygiene'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', required=True)
    parser.add_argument('--expected-version', required=True)
    parser.add_argument('--runtime-identity', required=True, help='实际 EXE --build-info 生成的私有报告')
    parser.add_argument('--report', required=True, help='发布目录之外的新文件，不能覆盖')
    options = parser.parse_args(argv)
    try:
        output = _plain(options.report).resolve()
        release = _plain(options.release_dir).resolve()
        if output.exists() or not output.parent.is_dir() or output.is_relative_to(release):
            raise ReleaseStructureError('结构报告必须是发布目录外的新文件，父目录须已存在。')
        result = check_release(release, options.expected_version, runtime_identity=options.runtime_identity)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        print(json.dumps({name: value for name, value in result.items() if name != 'sha256'}, ensure_ascii=False))
        return 0
    except (ReleaseStructureError, PackageError, UpdateValidationError) as error:
        print('发布结构检查失败：' + str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print('发布结构检查失败：文件不可读、权限不足或结构无效。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
