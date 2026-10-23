"""Build-time, independently approved public trust. Never reads remote trust data."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sys

from core.update_manifest import UpdateValidationError, validate_trusted_keys
from core.update_transport import TransportError, https_url


FIELDS = {'schema_version', 'product', 'channel', 'source_url', 'trusted_keys', 'key_state'}


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise UpdateValidationError('正式渠道配置包含重复字段。')
        result[name] = value
    return result


def validate_channel(value):
    if (type(value) is not dict or set(value) != FIELDS or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['product'] != 'MediaFlow'
            or value['channel'] != 'stable' or value['key_state'] not in ('pending', 'initialized')
            or not isinstance(value['source_url'], str)):
        raise UpdateValidationError('正式渠道配置结构或版本无效。')
    keys = validate_trusted_keys(value['trusted_keys'])
    if any(key['algorithm'] != 'ed25519' for key in keys.values()):
        raise UpdateValidationError('正式渠道只能固定 Ed25519 公钥。')
    if value['key_state'] == 'pending':
        if keys or value['source_url']:
            raise UpdateValidationError('待初始化渠道不能包含未确认的更新源或公钥。')
    else:
        if not keys or not value['source_url']:
            raise UpdateValidationError('正式渠道需要真实 HTTPS 地址和已确认的公钥。')
        try:
            https_url(value['source_url'])
        except TransportError:
            raise UpdateValidationError('正式渠道地址必须为有效的 HTTPS 地址。') from None
    return dict(value, trusted_keys=keys)


def read_channel(path):
    path = Path(path).absolute()
    try:
        if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
            raise UpdateValidationError('正式渠道配置不能使用重解析路径。')
        if not path.is_file() or path.stat().st_size > 65536:
            raise UpdateValidationError('正式渠道配置缺失或超过大小限制。')
        value = json.loads(path.read_text('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return validate_channel(value)
    except (OSError, UnicodeError, ValueError, TypeError):
        raise UpdateValidationError('正式渠道配置不可读或 JSON 格式无效。') from None


def load_channel():
    # Frozen applications trust their embedded resource, never a neighbouring
    # download, user config, current working directory, or unsigned manifest.
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    return read_channel(root / 'assets/update-channel.json')


def key_fingerprint(key):
    if key.get('algorithm') != 'ed25519':
        raise UpdateValidationError('公钥指纹仅支持 Ed25519。')
    return hashlib.sha256(base64.b64decode(key['public_key'], validate=True)).hexdigest()


def merge_keys(channel, saved_keys):
    result = dict(validate_trusted_keys(saved_keys))
    for name, key in channel['trusted_keys'].items():
        if name in result and result[name] != key:
            raise UpdateValidationError('用户配置中的 key_id 与内置正式公钥冲突；未替换可信根，请重新核对公钥。')
        result[name] = dict(key)
    return validate_trusted_keys(result)
