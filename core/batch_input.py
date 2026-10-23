"""批量来源网址的有界、离线导入；不改写查询参数或作品身份。"""
from __future__ import annotations

import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit

from core.request_context import validate_url


MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_URLS = 500
MAX_PREVIEW_ROWS = 5000


class BatchInputError(ValueError):
    pass


def _validate_source_url(url):
    validate_url(url)
    if any(character.isspace() for character in url) or '\\' in url or '\x7f' in url:
        raise BatchInputError('网址包含空白或不支持的字符，请每行仅填写一个完整网址。')
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host or parsed.port == 0:
        raise BatchInputError('网址的主机名或端口无效。')
    if ':' in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise BatchInputError('网址的主机名无效。') from None
    else:
        try:
            ascii_host = host.encode('idna').decode('ascii').rstrip('.')
        except UnicodeError:
            raise BatchInputError('网址的主机名无效。') from None
        if (not ascii_host or len(ascii_host) > 253 or
                any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                    for label in ascii_host.split('.'))):
            raise BatchInputError('网址的主机名无效。')


def parse_urls(text, existing=()):
    result = {'urls': [], 'rows': [], 'valid': 0, 'duplicates': 0,
              'invalid': 0, 'empty': 0, 'error': ''}
    if not isinstance(text, str):
        result['error'] = '批量导入内容必须是文本。'
        return result
    # 先检查字符数，避免为显然过大的粘贴内容再分配 UTF-8 副本。
    if len(text) > MAX_INPUT_BYTES or len(text.encode('utf-8', errors='replace')) > MAX_INPUT_BYTES:
        result['error'] = '批量导入文本不能超过 2 MiB，请分批导入。'
        return result
    seen = {str(value).strip() for value in existing if isinstance(value, str)}
    for line_number, raw in enumerate(text.splitlines(), 1):
        url = raw.strip()
        if not url:
            result['empty'] += 1
            continue
        try:
            _validate_source_url(url)
        except (ValueError, RuntimeError):
            result['invalid'] += 1
            status, reason = 'invalid', '需要完整的 HTTP(S) 网址，每行仅填写一个；不能包含凭据或空白。'
        else:
            if url in seen:
                result['duplicates'] += 1
                status, reason = 'duplicate', '与本次输入或现有任务的网址完全相同。'
            else:
                seen.add(url)
                result['valid'] += 1
                if result['valid'] <= MAX_URLS:
                    result['urls'].append(url)
                status, reason = 'valid', ''
        if len(result['rows']) < MAX_PREVIEW_ROWS:
            result['rows'].append({'line': line_number, 'url': url, 'status': status, 'reason': reason})
    if result['valid'] > MAX_URLS:
        result['error'] = f'本次有 {result["valid"]} 个有效不同网址，每批最多 500 个；请分批导入，当前未导入任何网址。'
        result['urls'] = []
    return result


def read_url_file(path):
    """严格解码大小受限的文本，读入工作应由界面后台任务执行。"""
    try:
        with Path(path).expanduser().open('rb') as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except (OSError, ValueError):
        raise BatchInputError('无法读取网址文本文件，请检查文件是否存在以及访问权限。') from None
    if len(raw) > MAX_INPUT_BYTES:
        raise BatchInputError('网址文本文件不能超过 2 MiB，请分批导入。')
    encodings = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')
    text = None
    for encoding in encodings:
        try:
            text = raw.decode(encoding, errors='strict')
            break
        except UnicodeError:
            continue
    if text is None:
        raise BatchInputError('文本编码无法识别，请另存为 UTF-8、带 BOM 的 UTF-16 或 GB18030 文本。')
    if any(ord(character) < 32 and character not in '\r\n\t' for character in text) or '\x7f' in text:
        raise BatchInputError('文件包含二进制或控制字符，请导入每行一个网址的 TXT 文本。')
    if len(text.encode('utf-8')) > MAX_INPUT_BYTES:
        raise BatchInputError('网址文本解码后超过 2 MiB，请分批导入。')
    return text
