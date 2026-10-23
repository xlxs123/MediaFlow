"""独立实现 Rou PNG ancillary chunk 的有界解包，不处理图像或密钥。"""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass

from core.errors import MediaCheckError
from core.manifests import parse_manifest


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
DEFAULT_MAX_SIZE = 2 * 1024 * 1024
MAX_PNG_SIZE = 16 * 1024 * 1024
MAX_CHUNK_SIZE = 8 * 1024 * 1024
MAX_CHUNKS = 4096
MAX_ROUD_SIZE = 2 * 1024 * 1024 + 1


class RouPlaylistError(MediaCheckError):
    """Rou 容器/清单错误，保留明确的协议错误码。"""


@dataclass(frozen=True)
class DecodedRouPlaylist:
    data: bytes
    container: str
    flag: int | None = None
    compression: str = 'none'
    chunks: tuple[str, ...] = ()

    @property
    def text(self):
        return self.data.decode('utf-8')

    def trace(self):
        return {'container': self.container, 'roud': 'FOUND' if self.container == 'PNG' else 'NOT_REQUIRED',
                'flag': f'0x{self.flag:02X}' if self.flag is not None else '',
                'compression': self.compression, 'playlist': 'VALID', 'chunks': list(self.chunks)}


def _error(code, message):
    return RouPlaylistError(message, code)


class RouPlaylistDecoder:
    @staticmethod
    def decode(data, max_size=DEFAULT_MAX_SIZE) -> bytes:
        # 已有 HLS 下载/复检接口使用 bytes，保持兼容；文本和 trace 使用下面的接口。
        return RouPlaylistDecoder.decode_with_trace(data, max_size).data

    @staticmethod
    def decode_text(data, max_size=DEFAULT_MAX_SIZE) -> str:
        return RouPlaylistDecoder.decode_with_trace(data, max_size).text

    @staticmethod
    def decode_with_trace(data, max_size=DEFAULT_MAX_SIZE) -> DecodedRouPlaylist:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise _error('ROU_PLAYLIST_INVALID', '媒体清单必须是字节数据。')
        if type(max_size) is not int or not 0 < max_size <= MAX_PNG_SIZE:
            raise _error('ROU_PLAYLIST_INVALID', '清单大小上限必须是 1 到 16 MiB 范围的整数。')
        size = data.nbytes if isinstance(data, memoryview) else len(data)
        if size > MAX_PNG_SIZE:
            raise _error('ROU_PLAYLIST_TOO_LARGE', 'Rou 输入超过 16 MiB 大小上限。')
        data = bytes(data)
        if not data.startswith(PNG_SIGNATURE):
            if data.startswith(b'\x89PNG'):
                raise _error('ROU_PNG_INVALID', 'PNG 文件签名不完整或不正确。')
            if not data.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'#EXTM3U'):
                raise _error('ROU_NOT_PNG_OR_HLS', '响应既不是 Rou PNG 容器，也不是普通 HLS。')
            return DecodedRouPlaylist(RouPlaylistDecoder._validate_playlist(data, max_size), 'HLS')
        offset, count = len(PNG_SIGNATURE), 0
        saw_header = False
        saw_end = False
        encoded = None
        chunks = []
        while offset < len(data):
            count += 1
            if count > MAX_CHUNKS or len(data) - offset < 12:
                raise _error('ROU_PNG_INVALID', 'PNG Chunk 数量过多或头部不完整。')
            length, kind = struct.unpack_from('>I4s', data, offset)
            if length > MAX_CHUNK_SIZE or length > len(data) - offset - 12:
                raise _error('ROU_PNG_INVALID', 'PNG Chunk 长度超出边界或大小上限。')
            if kind == b'roUd' and length > MAX_ROUD_SIZE:
                raise _error('ROU_PLAYLIST_TOO_LARGE', 'roUd 输入超过 2 MiB 大小上限。')
            if not re.fullmatch(rb'[A-Za-z]{4}', kind) or not 65 <= kind[2] <= 90:
                raise _error('ROU_PNG_INVALID', 'PNG Chunk Type 不符合标准格式。')
            start = offset + 8
            end = start + length
            payload = data[start:end]
            expected_crc = struct.unpack_from('>I', data, end)[0]
            actual_crc = zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff
            if expected_crc != actual_crc:
                raise _error('ROU_PNG_INVALID', 'PNG Chunk CRC 校验失败。')
            offset = end + 4
            chunks.append(kind.decode('ascii'))
            if not saw_header:
                if kind != b'IHDR' or length != 13:
                    raise _error('ROU_PNG_INVALID', 'PNG 缺少首个合法 IHDR。')
                width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
                depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
                if (not 0 < width < 2**31 or not 0 < height < 2**31 or depth not in depths.get(color, ())
                        or compression or filtering or interlace not in (0, 1)):
                    raise _error('ROU_PNG_INVALID', 'PNG IHDR 属性无效。')
                saw_header = True
            elif kind == b'IHDR':
                raise _error('ROU_PNG_INVALID', 'PNG 包含重复 IHDR。')
            if kind == b'roUd':
                if encoded is not None:
                    raise _error('ROU_PNG_INVALID', 'PNG 包含重复 roUd，无法确定清单。')
                encoded = payload
            elif kind == b'IEND':
                if length or offset != len(data):
                    raise _error('ROU_PNG_INVALID', 'PNG IEND 不正确或后面存在额外数据。')
                saw_end = True
                break
        if not saw_header or not saw_end:
            raise _error('ROU_PNG_INVALID', 'PNG 缺少完整 IHDR 或 IEND。')
        if encoded is None:
            raise _error('ROU_CHUNK_NOT_FOUND', '普通 PNG 未包含 roUd 清单 Chunk。')
        if not encoded:
            raise _error('ROU_PAYLOAD_INVALID', 'roUd 缺少标志字节或清单内容。')
        flag, payload = encoded[0], encoded[1:]
        if not payload:
            raise _error('ROU_PAYLOAD_INVALID', 'roUd 缺少清单内容。')
        compression = 'none'
        if flag & 1:
            payload, compression = RouPlaylistDecoder._inflate(payload, max_size)
        elif len(payload) > max_size:
            raise _error('ROU_PLAYLIST_TOO_LARGE', 'roUd 清单超过大小上限。')
        return DecodedRouPlaylist(RouPlaylistDecoder._validate_playlist(payload, max_size),
                                  'PNG', flag, compression, tuple(chunks))

    @staticmethod
    def _inflate(payload, max_size):
        for window in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            inflater = zlib.decompressobj(window)
            try:
                result = inflater.decompress(payload, max_size + 1)
            except zlib.error:
                continue
            if len(result) > max_size or inflater.unconsumed_tail:
                raise _error('ROU_PLAYLIST_TOO_LARGE', 'roUd 解压超过清单大小上限。')
            if not inflater.eof or inflater.unused_data:
                raise _error('ROU_DEFLATE_FAILED', 'roUd deflate 数据不完整或包含额外压缩数据。')
            return result, 'zlib' if window > 0 else 'deflate'
        raise _error('ROU_DEFLATE_FAILED', 'roUd deflate 数据无法解压。')

    @staticmethod
    def _validate_playlist(payload, max_size):
        if len(payload) > max_size:
            raise _error('ROU_PLAYLIST_TOO_LARGE', '媒体清单超过大小上限。')
        if not payload:
            raise _error('ROU_PLAYLIST_INVALID', '媒体清单为空。')
        try:
            text = payload.decode('utf-8-sig').lstrip(' \t\r\n')
        except UnicodeDecodeError:
            raise _error('ROU_PLAYLIST_INVALID', 'roUd 清单不是有效 UTF-8 文本。') from None
        lines = [line.strip() for line in text.splitlines()]
        if not lines or lines[0] != '#EXTM3U':
            raise _error('ROU_PLAYLIST_INVALID', '清单缺少合法 #EXTM3U 头部。')
        result = text.encode('utf-8')
        if len(result) > max_size:
            raise _error('ROU_PLAYLIST_TOO_LARGE', '规范化后的清单超过大小上限。')
        try:
            info = parse_manifest(result, 'https://playlist.invalid/base/index.m3u8')
        except MediaCheckError:
            raise _error('ROU_PLAYLIST_INVALID', 'roUd 中的 HLS 标签或属性不完整。') from None
        master = any(line.startswith('#EXT-X-STREAM-INF:') for line in lines) and bool(info.children)
        media = any(line.startswith('#EXTINF:') for line in lines) and any(
            item.role == 'Media Segment' and item.duration is not None for item in info.resources)
        if info.media_type != 'HLS' or not (master or media):
            raise _error('ROU_PLAYLIST_INVALID', 'roUd 内容不是有效 HLS 清单。')
        return result
