"""Strict release metadata and independently trusted publisher signatures.

An embedded file hash proves consistency only. Trust keys must be configured
independently of the ZIP; this module never imports keys from release metadata.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import struct
import unicodedata
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version


MAX_PACKAGE_BYTES = 4 * 1024 ** 3
MAX_UNPACKED_BYTES = 12 * 1024 ** 3
MAX_FILES = 4096
REQUIRED_FILES = (
    'MediaFlow.exe',
    'tools/yt-dlp.exe',
    'tools/ffmpeg.exe',
    'tools/ffprobe.exe',
    'tools/MediaFlow.Recycle.exe',
    'tools/webview2/MediaFlow.WebView2Host.exe',
    'tools/webview2/WebView2Loader.dll',
    'tools/webview2/Microsoft.Web.WebView2.Core.dll',
    'build-info.json',
)

_ALGORITHM = 'rsa-pkcs1-sha256'
ED25519_ALGORITHM = 'ed25519'
_VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
                      r'(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?'
                      r'(?:\+[a-z0-9]+(?:[.-][a-z0-9]+)*)?\Z')
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z')
_HASH = re.compile(r'[a-fA-F0-9]{64}\Z')
_RESERVED = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])\Z', re.I)
_FIELDS = {'schema_version', 'product', 'version', 'build_id', 'platform',
           'architecture', 'release_notes', 'release_page', 'minimum_app_version',
           'data_migration', 'files'}
_TOOL_FILES = set(REQUIRED_FILES) | {
    'tools/FFmpeg-LICENSE', 'tools/FFmpeg-README.txt', 'tools/provenance.json',
    'tools/yt-dlp-LICENSE.txt', 'tools/yt-dlp-THIRD_PARTY_LICENSES.txt',
    'tools/webview2/LICENSE.txt', 'tools/webview2/NOTICE.txt',
    'tools/webview2/MediaFlow.WebView2Host.exe.config',
}
_DATA_NAMES = {'browserprofile', '.stream-downloads', 'config.json', 'tasks.json',
               'tasks.json.bak', 'history.sqlite3', 'batches.sqlite3',
               'data-manifest.json', 'migration-assistant.json', 'local state',
               'preferences', 'cookies', 'login data', 'trusted_keys.json',
               'trusted-keys.json'}
_SECRET_NAME = re.compile(r'(?:^|[._ -])(?:cookies?|passwords?|credentials?|secrets?|'
                          r'tokens?|private[._ -]?keys?)(?:$|[._ -])', re.I)


class UpdateValidationError(RuntimeError):
    pass


def parse_version(value: str) -> Version:
    """Use packaging ordering, accepting only the documented release syntax."""
    if not isinstance(value, str) or len(value) > 96 or not _VERSION.fullmatch(value):
        raise UpdateValidationError('版本号无效：需要三段数字，可附加 a/b/rc 或 local 标识。')
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise UpdateValidationError('版本号无法解析。') from exc


def canonical_bytes(value: dict) -> bytes:
    """The signature covers every manifest field except the signature itself."""
    if type(value) is not dict:
        raise UpdateValidationError('发布清单必须是 JSON 对象。')
    try:
        return json.dumps({key: item for key, item in value.items() if key != 'signature'},
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateValidationError('发布清单不是有效的 UTF-8 JSON。') from exc


def safe_release_path(value: str) -> str:
    """Reject paths that Windows might resolve outside or alias within a release."""
    if not isinstance(value, str) or not value or '\\' in value:
        raise UpdateValidationError('发布文件路径必须是非空相对 POSIX 路径。')
    try:
        encoded_length = len(value.encode('utf-16-le')) // 2
    except UnicodeError as exc:
        raise UpdateValidationError('发布文件路径包含无效 Unicode。') from exc
    if encoded_length > 240 or value.startswith('/') or value.endswith('/'):
        raise UpdateValidationError('发布文件路径过长或不是相对文件路径。')
    parts = value.split('/')
    for part in parts:
        if not part or part in ('.', '..') or part.startswith('.') or part.endswith((' ', '.')):
            raise UpdateValidationError('发布文件路径含有空段、隐藏段、上级目录或尾随空格/句点。')
        if any(char in '<>:"|?*' or unicodedata.category(char) in ('Cc', 'Cf', 'Cs')
               for char in part):
            raise UpdateValidationError('发布文件路径包含 Windows 非法字符或控制字符。')
        if _RESERVED.fullmatch(part.split('.', 1)[0].rstrip(' ')):
            raise UpdateValidationError('发布文件路径使用了 Windows 保留名称。')
    return value


def _allowed_release_file(path: str) -> None:
    parts = path.split('/')
    if any(part.casefold() in _DATA_NAMES or _SECRET_NAME.search(part) for part in parts):
        raise UpdateValidationError('发布包不得包含用户数据、浏览器配置、密钥或凭据文件。')
    if path in _TOOL_FILES or path in ('LICENSE', 'FILES.sha256', 'MediaFlow-Qt.cmd'):
        return
    extension = PurePosixPath(path).suffix.lower()
    if len(parts) == 1 and extension in ('.md', '.txt', '.png', '.jpg', '.jpeg', '.ico', '.svg', '.webp'):
        return
    if len(parts) == 1 and extension == '.json' and path != 'release.json':
        return
    if (parts[0] == 'licenses' and 2 <= len(parts) <= 5
            and (extension in ('.md', '.txt', '.html', '.json') or not extension
                 or parts[-1] in ('LICENSE.APACHE', 'LICENSE.BSD'))):
        return
    raise UpdateValidationError('发布文件不属于允许的应用、工具或说明文件结构。')


def _object(value, required: set, optional: set, label: str) -> None:
    if type(value) is not dict or set(value) - required - optional or required - set(value):
        raise UpdateValidationError(f'{label}字段缺失或包含不允许的字段。')


def _https(value, label: str, *, allow_empty=False) -> None:
    if allow_empty and value in ('', None):
        return
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(char.isspace() or ord(char) < 32 for char in value) or '\\' in value):
        raise UpdateValidationError(f'{label}必须是有效的 HTTPS 地址。')
    try:
        parsed = urlsplit(value)
        port = parsed.port
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                 and parsed.password is None and not parsed.fragment and port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise UpdateValidationError(f'{label}必须是无凭据的 HTTPS 地址。')


def _integer(value, maximum: int, label: str, *, minimum=0) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise UpdateValidationError(f'{label}超出允许范围或不是整数。')


def _hash(value, label: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise UpdateValidationError(f'{label}必须是 SHA-256 十六进制值。')


def _decode64(value, maximum: int, label: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 4 * ((maximum + 2) // 3):
        raise UpdateValidationError(f'{label}的 Base64 长度无效。')
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UpdateValidationError(f'{label}不是有效的 Base64。') from exc
    if len(decoded) > maximum or base64.b64encode(decoded).decode('ascii') != value:
        raise UpdateValidationError(f'{label}的 Base64 编码不规范。')
    return decoded


def validate_trusted_keys(value) -> dict:
    if type(value) is not dict or len(value) > 32:
        raise UpdateValidationError('可信公钥配置必须是最多 32 项的对象。')
    result = {}
    for key_id, key in value.items():
        if not isinstance(key_id, str) or not _IDENTIFIER.fullmatch(key_id):
            raise UpdateValidationError('可信公钥标识无效。')
        if isinstance(key, dict) and key.get('algorithm') == ED25519_ALGORITHM:
            _object(key, {'algorithm', 'public_key'}, set(), 'Ed25519 可信公钥')
            raw = _decode64(key['public_key'], 32, 'Ed25519 公钥')
            if len(raw) != 32:
                raise UpdateValidationError('Ed25519 公钥必须是 32 字节。')
            result[key_id] = dict(key)
            continue
        _object(key, {'algorithm', 'modulus', 'exponent'}, set(), '可信公钥')
        if key['algorithm'] != _ALGORITHM:
            raise UpdateValidationError('不支持的可信公钥算法。')
        modulus = _decode64(key['modulus'], 1024, 'RSA 模数')
        exponent = _decode64(key['exponent'], 8, 'RSA 指数')
        bits = int.from_bytes(modulus, 'big').bit_length()
        exp = int.from_bytes(exponent, 'big')
        if (not 2048 <= bits <= 8192 or bits % 8 or not modulus or not modulus[-1] & 1
                or modulus[0] == 0 or not exponent or exponent[0] == 0 or exp < 3 or not exp & 1):
            raise UpdateValidationError('RSA 公钥必须为 2048–8192 位，使用有效的奇数模数与指数。')
        result[key_id] = dict(key)
    return result


def _verify_ed25519(key: dict, signature: bytes, payload: bytes) -> None:
    if len(signature) != 64:
        raise UpdateValidationError('Ed25519 发布签名必须是 64 字节。')
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise UpdateValidationError('Ed25519 验签依赖不可用，请使用完整的 MediaFlow 发布文件夹。') from exc
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key['public_key'])).verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        raise UpdateValidationError('Ed25519 发布签名验证失败，清单内容或签名可能已被修改。') from exc
    except UnsupportedAlgorithm as exc:
        raise UpdateValidationError('当前加密运行库不支持 Ed25519，已停止更新。') from exc


class _PKCS1Padding(ctypes.Structure):
    _fields_ = [('pszAlgId', ctypes.c_wchar_p)]


def _verify_rsa(key: dict, signature: bytes, payload: bytes) -> None:
    """Verify with CNG; no private-key operations or handwritten RSA arithmetic.

    Native layouts and pre-hash semantics follow Microsoft bcrypt.h documentation:
    https://learn.microsoft.com/windows/win32/api/bcrypt/ns-bcrypt-bcrypt_rsakey_blob
    https://learn.microsoft.com/windows/win32/api/bcrypt/nf-bcrypt-bcryptverifysignature
    """
    if os.name != 'nt':
        raise UpdateValidationError('当前平台不支持 Windows 原生发布签名验证。')
    modulus = base64.b64decode(key['modulus'])
    exponent = base64.b64decode(key['exponent'])
    if len(signature) != len(modulus):
        raise UpdateValidationError('发布签名长度与可信公钥不符。')
    # BCRYPT_RSAKEY_BLOB is six native ULONGs, followed by big-endian E and N.
    blob = struct.pack('<6I', 0x31415352, len(modulus) * 8, len(exponent), len(modulus), 0, 0)
    blob += exponent + modulus
    handle = ctypes.c_void_p
    ulong = ctypes.c_uint32
    algorithm, public_key = handle(), handle()
    try:
        bcrypt = ctypes.WinDLL('bcrypt.dll', winmode=0x00000800)  # System32 only
        declarations = {
            'BCryptOpenAlgorithmProvider': [ctypes.POINTER(handle), ctypes.c_wchar_p, ctypes.c_wchar_p, ulong],
            'BCryptImportKeyPair': [handle, handle, ctypes.c_wchar_p, ctypes.POINTER(handle), ctypes.c_void_p, ulong, ulong],
            'BCryptVerifySignature': [handle, ctypes.c_void_p, ctypes.c_void_p, ulong, ctypes.c_void_p, ulong, ulong],
            'BCryptDestroyKey': [handle],
            'BCryptCloseAlgorithmProvider': [handle, ulong],
        }
        for name, arguments in declarations.items():
            function = getattr(bcrypt, name)
            function.argtypes = arguments
            function.restype = ctypes.c_int32
        if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), 'RSA', None, 0) != 0:
            raise UpdateValidationError('Windows 无法初始化 RSA 签名验证。')
        blob_buffer = ctypes.create_string_buffer(blob)
        if bcrypt.BCryptImportKeyPair(algorithm, None, 'RSAPUBLICBLOB', ctypes.byref(public_key),
                                     blob_buffer, len(blob), 0) != 0:
            raise UpdateValidationError('Windows 无法导入可信 RSA 公钥。')
        digest = hashlib.sha256(payload).digest()
        digest_buffer = ctypes.create_string_buffer(digest)
        signature_buffer = ctypes.create_string_buffer(signature)
        padding = _PKCS1Padding('SHA256')
        status = bcrypt.BCryptVerifySignature(public_key, ctypes.byref(padding), digest_buffer,
                                             len(digest), signature_buffer, len(signature), 0x2)
        if status != 0:
            raise UpdateValidationError('发布签名验证失败，清单内容或签名可能已被修改。')
    except (OSError, AttributeError) as exc:
        raise UpdateValidationError('Windows 原生签名验证不可用。') from exc
    finally:
        if public_key.value:
            bcrypt.BCryptDestroyKey(public_key)
        if algorithm.value:
            bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


def validate_manifest(value: dict, trusted_keys: dict | None = None,
                      require_signature=False) -> dict:
    schema = value.get('schema_version') if isinstance(value, dict) else None
    if type(schema) is not int or schema not in (1, 2):
        raise UpdateValidationError('不支持的发布清单版本。')
    _object(value, _FIELDS | ({'published_at', 'signing'} if schema == 2 else set()),
            {'signature', 'package'}, '发布清单')
    if schema == 2:
        published = value['published_at']
        if not isinstance(published, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', published):
            raise UpdateValidationError('发布时间必须是 UTC 秒精度格式 YYYY-MM-DDTHH:MM:SSZ。')
        try:
            datetime.strptime(published, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError as exc:
            raise UpdateValidationError('发布时间不是有效日期。') from exc
        _object(value['signing'], {'algorithm', 'key_id'}, set(), '签名身份')
        if (value['signing']['algorithm'] != ED25519_ALGORITHM
                or not isinstance(value['signing']['key_id'], str)
                or not _IDENTIFIER.fullmatch(value['signing']['key_id'])):
            raise UpdateValidationError('格式 2 清单需要有效的 Ed25519 签名身份。')
    if value['product'] != 'MediaFlow':
        raise UpdateValidationError('发布包产品标识不是 MediaFlow。')
    parse_version(value['version'])
    if not isinstance(value['build_id'], str) or not _IDENTIFIER.fullmatch(value['build_id']):
        raise UpdateValidationError('构建标识无效。')
    if value['platform'] != 'windows' or value['architecture'] != 'x86_64':
        raise UpdateValidationError('发布包不适用于 Windows x86_64 平台。')
    notes = value['release_notes']
    if (not isinstance(notes, str) or len(notes) > 16000
            or any(unicodedata.category(char) in ('Cs', 'Cf')
                   or (ord(char) < 32 and char not in '\n\r\t') for char in notes)):
        raise UpdateValidationError('发布说明必须是最多 16000 字符的有效纯文本。')
    _https(value['release_page'], '发布页面', allow_empty=True)
    minimum = value['minimum_app_version']
    if minimum not in ('', None):
        if parse_version(minimum) > parse_version(value['version']):
            raise UpdateValidationError('最低兼容版本不能高于发布版本。')
    if value['data_migration'] not in ('none', 'separate'):
        raise UpdateValidationError('数据迁移标记无效；迁移必须通过独立流程执行。')
    files = value['files']
    if type(files) is not list or not 1 <= len(files) <= MAX_FILES:
        raise UpdateValidationError('发布文件数量超出允许范围。')
    seen, spellings, total = set(), {}, 0
    for item in files:
        _object(item, {'path', 'size', 'sha256'}, set(), '发布文件')
        path = safe_release_path(item['path'])
        _allowed_release_file(path)
        normalized = unicodedata.normalize('NFC', path).casefold()
        if normalized in seen:
            raise UpdateValidationError('发布清单存在重复、大小写或 Unicode 冲突路径。')
        seen.add(normalized)
        parts = path.split('/')
        for index in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:index])
            normalized_prefix = unicodedata.normalize('NFC', prefix).casefold()
            if normalized_prefix in spellings and spellings[normalized_prefix] != prefix:
                raise UpdateValidationError('发布目录名称存在大小写或 Unicode 冲突。')
            spellings[normalized_prefix] = prefix
        _integer(item['size'], MAX_UNPACKED_BYTES, '发布文件大小')
        _hash(item['sha256'], '发布文件哈希')
        total += item['size']
        if total > MAX_UNPACKED_BYTES:
            raise UpdateValidationError('解压后数据总量超过允许上限。')
    paths = {item['path']: item for item in files}
    for path in paths:
        parts = path.split('/')
        if any('/'.join(parts[:index]) in paths for index in range(1, len(parts))):
            raise UpdateValidationError('发布清单的文件路径与目录路径冲突。')
    if any(path not in paths or not paths[path]['size'] for path in REQUIRED_FILES):
        raise UpdateValidationError('发布清单缺少必需的 EXE、工具或构建信息，或必需文件为空。')
    if 'package' in value:
        package = value['package']
        _object(package, {'format', 'url', 'size', 'sha256'}, set(), '在线发布包')
        if package['format'] != 'zip':
            raise UpdateValidationError('只支持 ZIP 发布包。')
        _https(package['url'], '发布包地址')
        _integer(package['size'], MAX_PACKAGE_BYTES, '发布包大小', minimum=1)
        _hash(package['sha256'], '发布包哈希')
    keys = validate_trusted_keys({} if trusted_keys is None else trusted_keys)
    payload = canonical_bytes(value)
    authenticated = False
    authentication = '本地发布包未经过发布者身份认证；包内哈希只能验证文件一致性。'
    if 'signature' in value:
        signature = value['signature']
        _object(signature, {'algorithm', 'key_id', 'value'}, set(), '发布签名')
        if (signature['algorithm'] not in (_ALGORITHM, ED25519_ALGORITHM) or not isinstance(signature['key_id'], str)
                or not _IDENTIFIER.fullmatch(signature['key_id'])):
            raise UpdateValidationError('发布签名算法或公钥标识无效。')
        if schema == 2 and {k: signature[k] for k in ('algorithm', 'key_id')} != value['signing']:
            raise UpdateValidationError('发布签名与已签署的密钥身份不一致。')
        key = keys.get(signature['key_id'])
        if key is None:
            raise UpdateValidationError('发布签名使用了未配置或不可信的公钥。')
        if key['algorithm'] != signature['algorithm']:
            raise UpdateValidationError('签名算法与可信公钥类型不一致。')
        signature_bytes = _decode64(signature['value'], 1024, '发布签名')
        if signature['algorithm'] == ED25519_ALGORITHM:
            _verify_ed25519(key, signature_bytes, payload)
        else:
            _verify_rsa(key, signature_bytes, payload)
        authenticated = True
        authentication = '已通过独立配置的可信公钥验证发布清单签名。'
    elif require_signature:
        raise UpdateValidationError('发布清单缺少可信发布者签名。')
    return {'manifest': json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False)),
            'authenticated': authenticated, 'authentication': authentication,
            'manifest_sha256': hashlib.sha256(payload).hexdigest()}
