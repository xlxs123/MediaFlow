"""Explicit publisher approval of a public source/key before building the EXE."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

if __package__ in ('', None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.update_channel import key_fingerprint, read_channel, validate_channel
from core.update_manifest import UpdateValidationError, validate_trusted_keys
from scripts.publish_release import PublishError, _read_document


def configure(source_url, trusted_keys_file, key_id, confirm_fingerprint, output,
              *, retain_current_keys=False):
    document, _, _ = _read_document(trusted_keys_file, 65536)
    if set(document) != {'trusted_keys'}:
        raise UpdateValidationError('公钥文件只允许 trusted_keys 字段；禁止私钥和其他配置。')
    keys = validate_trusted_keys(document['trusted_keys'])
    if set(keys) != {key_id}:
        raise UpdateValidationError('每次只确认一个明确的正式 key_id。')
    fingerprint = key_fingerprint(keys[key_id])
    if confirm_fingerprint.lower() != fingerprint:
        raise UpdateValidationError('公钥指纹与独立确认值不符，未修改正式渠道。')
    output = Path(output).absolute()
    if any(part.is_symlink() or part.is_junction() for part in (output, *output.parents)):
        raise UpdateValidationError('渠道输出路径不能使用符号链接或重解析目录。')
    if not output.parent.is_dir():
        raise UpdateValidationError('渠道输出的父目录不存在。')
    if output.exists():
        previous = read_channel(output)['trusted_keys']
        if key_id in previous and previous[key_id] != keys[key_id]:
            raise UpdateValidationError('密钥轮换必须使用新的 key_id。')
        if retain_current_keys:
            keys = dict(previous, **keys)
    channel = validate_channel({'schema_version': 1, 'product': 'MediaFlow', 'channel': 'stable',
        'source_url': source_url, 'trusted_keys': keys, 'key_state': 'initialized'})
    payload = (json.dumps(channel, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    temporary = output.with_name('.' + output.name + '.' + uuid.uuid4().hex + '.tmp')
    created = False
    try:
        with temporary.open('xb') as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
    return {'configured': True, 'key_state': 'initialized', 'key_id': key_id,
            'public_key_sha256': fingerprint, 'output': str(output),
            'requires_rebuild': True, 'online_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='只写入明确确认的正式公钥；不会生成私钥或访问服务器')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('configure')
    command.add_argument('--source-url', required=True)
    command.add_argument('--trusted-keys-file', required=True)
    command.add_argument('--key-id', required=True)
    command.add_argument('--confirm-fingerprint', required=True)
    command.add_argument('--output', required=True)
    command.add_argument('--retain-current-keys', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = configure(args.source_url, args.trusted_keys_file, args.key_id,
            args.confirm_fingerprint, args.output, retain_current_keys=args.retain_current_keys)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (UpdateValidationError, PublishError, OSError, ValueError) as error:
        parser.exit(1, '未配置正式渠道：' + str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
