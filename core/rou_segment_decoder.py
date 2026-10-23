"""按 PNG/roUd 容器解包媒体分片；不读取 URL、密钥或执行媒体解密。"""
from __future__ import annotations

import re
import struct
import zlib

from core.errors import MediaCheckError


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
MAX_SEGMENT_TRANSPORT_SIZE = 64 * 1024 * 1024
MAX_SEGMENT_DECODED_SIZE = 32 * 1024 * 1024
MAX_PNG_CHUNKS = 4096
MAX_BMFF_BOXES = 4096
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}


def _error(message, code='ROU_SEGMENT_INVALID'):
    return MediaCheckError(message, code)


def _unpack_png(data):
    offset, count = len(PNG_SIGNATURE), 0
    encoded = None
    saw_header = saw_end = False
    view = memoryview(data)
    while offset < len(data):
        count += 1
        if count > MAX_PNG_CHUNKS or len(data) - offset < 12:
            raise _error('分片 PNG Chunk 数量过多或结构不完整。')
        length, kind = struct.unpack_from('>I4s', data, offset)
        if length > MAX_SEGMENT_TRANSPORT_SIZE or length > len(data) - offset - 12:
            raise _error('分片 PNG Chunk 长度超出容器边界。')
        if not re.fullmatch(rb'[A-Za-z]{4}', kind) or not 65 <= kind[2] <= 90:
            raise _error('分片 PNG Chunk Type 不符合标准。')
        if kind[0] < 97 and kind not in (b'IHDR', b'IDAT', b'PLTE', b'IEND'):
            raise _error('分片 PNG 包含未知关键 Chunk。')
        start = offset + 8
        end = start + length
        payload = view[start:end]
        expected_crc = struct.unpack_from('>I', data, end)[0]
        if expected_crc != zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff:
            raise _error('分片 PNG Chunk CRC 校验失败。')
        offset = end + 4
        if not saw_header:
            if kind != b'IHDR' or length != 13:
                raise _error('分片 PNG 缺少首个合法 IHDR。')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
            if (not 0 < width < 2**31 or not 0 < height < 2**31 or depth not in _PNG_DEPTHS.get(color, ())
                    or compression or filtering or interlace not in (0, 1)):
                raise _error('分片 PNG IHDR 属性无效。')
            saw_header = True
        elif kind == b'IHDR':
            raise _error('分片 PNG 包含重复 IHDR。')
        if kind == b'roUd':
            if encoded is not None:
                raise _error('分片 PNG 包含重复 roUd。')
            encoded = payload
        elif kind == b'IEND':
            if length or offset != len(data):
                raise _error('分片 PNG IEND 无效或含额外数据。')
            saw_end = True
            break
    if not saw_header or not saw_end:
        raise _error('分片 PNG 缺少完整 IHDR 或 IEND。')
    if encoded is None:
        raise _error('分片实际为普通 PNG，没有 roUd 媒体载荷。', 'OUTPUT_NOT_VIDEO')
    if len(encoded) < 2:
        raise _error('分片 roUd 缺少标志字节或媒体载荷。')
    flag = encoded[0]
    if flag not in (0, 1):
        raise _error('分片 roUd 使用未知标志，无法可靠解包。')
    return flag, encoded[1:]


def _inflate(payload):
    for window in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        decoder = zlib.decompressobj(window)
        try:
            result = decoder.decompress(payload, MAX_SEGMENT_DECODED_SIZE + 1)
        except zlib.error:
            continue
        if len(result) > MAX_SEGMENT_DECODED_SIZE or decoder.unconsumed_tail:
            raise _error('分片 roUd 解压超过 32 MiB 上限。', 'ROU_SEGMENT_TOO_LARGE')
        if not decoder.eof or decoder.unused_data:
            raise _error('分片 roUd 压缩数据不完整或含额外数据。', 'ROU_SEGMENT_DEFLATE_FAILED')
        return result
    raise _error('分片 roUd 不是有效 zlib/raw deflate 数据。', 'ROU_SEGMENT_DEFLATE_FAILED')


def _is_ts(data):
    # 真实样本存在仅 376 bytes 的 PAT/PMT 小片；不能要求五个 TS 包。
    for packet_size, sync_offset in ((188, 0), (192, 4), (204, 0)):
        if len(data) < packet_size * 2 or len(data) % packet_size:
            continue
        if all(data[start + sync_offset] == 0x47 and data[start + sync_offset + 3] & 0x30
               for start in range(0, len(data), packet_size)):
            return True
    return False


def _is_bmff(data):
    if len(data) < 8 or data[4:8] not in (b'ftyp', b'styp', b'moov', b'moof', b'sidx'):
        return False
    offset = count = 0
    saw_moov = saw_moof = saw_media = False
    while offset < len(data):
        count += 1
        if count > MAX_BMFF_BOXES or len(data) - offset < 8:
            return False
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                return False
            size = struct.unpack_from('>Q', data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or size > len(data) - offset:
            return False
        if kind in (b'ftyp', b'styp') and size - header < 8:
            return False
        saw_moov |= kind == b'moov'
        saw_moof |= kind == b'moof'
        saw_media |= kind == b'mdat' and size > header
        offset += size
    return saw_moov or saw_moof and saw_media


def _is_aac(data):
    offset = 0
    # HLS packed audio 可以包含合法 ID3 时间戳头。
    if data.startswith(b'ID3'):
        if len(data) < 10 or any(value & 0x80 for value in data[6:10]):
            return False
        tag_size = sum(value << shift for value, shift in zip(data[6:10], (21, 14, 7, 0)))
        offset = 10 + tag_size + (10 if data[5] & 0x10 else 0)
        if offset >= len(data):
            return False
    saw_frame = False
    while offset < len(data):
        if len(data) - offset < 7 or data[offset] != 0xff or data[offset + 1] & 0xf6 != 0xf0:
            return False
        if (data[offset + 2] >> 2) & 0xf > 12:
            return False
        header_size = 7 if data[offset + 1] & 1 else 9
        frame_size = ((data[offset + 3] & 3) << 11) | (data[offset + 4] << 3) | (data[offset + 5] >> 5)
        if frame_size < header_size or frame_size > len(data) - offset:
            return False
        offset += frame_size
        saw_frame = True
    return saw_frame


def _obvious_non_media(data):
    prefix = data.lstrip(b'\xef\xbb\xbf \t\r\n')
    return (prefix.startswith((b'#EXTM3U', b'<MPD', b'<?xml', b'<html', b'<!DOCTYPE'))
            or data.startswith((PNG_SIGNATURE, b'\xff\xd8\xff', b'GIF87a', b'GIF89a'))
            or len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP')


def _validate_encrypted(data):
    if not data or len(data) % 16 or _obvious_non_media(data):
        raise _error('AES-128 分片载荷不是合法块对齐密文，或实际为清单/图片。', 'OUTPUT_NOT_VIDEO')


def decode_media_segment(data, encrypted=False) -> bytes:
    """非 PNG 原媒体透传；roUd 清媒体验证类型，AES-128 密文交由已有下载器解密。"""
    if not isinstance(data, (bytes, bytearray, memoryview)) or type(encrypted) is not bool:
        raise _error('分片输入必须是 bytes，encrypted 必须是明确布尔值。')
    size = data.nbytes if isinstance(data, memoryview) else len(data)
    if size > MAX_SEGMENT_TRANSPORT_SIZE:
        raise _error('分片运输数据超过 64 MiB 上限。', 'ROU_SEGMENT_TOO_LARGE')
    if not size:
        raise _error('分片响应为空，不能作为媒体。', 'OUTPUT_NOT_VIDEO')
    data = bytes(data)
    if not data.startswith(PNG_SIGNATURE):
        if data.startswith(b'\x89PNG') or len(data) < len(PNG_SIGNATURE) and PNG_SIGNATURE.startswith(data):
            raise _error('分片 PNG 签名不完整或不正确。')
        if len(data) > MAX_SEGMENT_DECODED_SIZE:
            raise _error('分片媒体超过 32 MiB 上限。', 'ROU_SEGMENT_TOO_LARGE')
        if encrypted:
            _validate_encrypted(data)
        return data
    flag, encoded = _unpack_png(data)
    if flag == 0:
        if len(encoded) > MAX_SEGMENT_DECODED_SIZE:
            raise _error('分片 roUd 媒体超过 32 MiB 上限。', 'ROU_SEGMENT_TOO_LARGE')
        payload = bytes(encoded)
    else:
        payload = _inflate(encoded)
    if not payload or _obvious_non_media(payload):
        raise _error('roUd 内容是清单/图片或空数据，不能作为媒体分片。', 'OUTPUT_NOT_VIDEO')
    if encrypted:
        _validate_encrypted(payload)
    elif not (_is_ts(payload) or _is_bmff(payload) or _is_aac(payload)):
        raise _error('roUd 载荷未识别为 MPEG-TS、ISO-BMFF 或 AAC 媒体。', 'OUTPUT_NOT_VIDEO')
    return payload
