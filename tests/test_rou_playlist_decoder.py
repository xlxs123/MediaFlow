import gzip
import struct
import unittest
import zlib

from core.errors import MediaCheckError
from core.manifests import parse_manifest
from core.rou_playlist_decoder import (
    DEFAULT_MAX_SIZE, MAX_CHUNKS, MAX_PNG_SIZE, PNG_SIGNATURE, RouPlaylistDecoder,
)


MEDIA = b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nseg.png\n#EXT-X-ENDLIST\n'
MASTER = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlow/index.m3u8\n'
IHDR = struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0)


def chunk(kind, payload=b''):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)


def png(payload=None, flag=0, before=b'', after=b''):
    middle = b'' if payload is None else chunk(b'roUd', bytes([flag]) + payload)
    return PNG_SIGNATURE + chunk(b'IHDR', IHDR) + before + middle + after + chunk(b'IEND')


def raw_deflate(data):
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


class RouPlaylistDecoderTests(unittest.TestCase):
    def assert_code(self, code, data, **kwargs):
        with self.assertRaises(MediaCheckError) as raised:
            RouPlaylistDecoder.decode(data, **kwargs)
        self.assertEqual(raised.exception.code, code)

    def test_plain_hls_passes_through_unchanged(self):
        self.assertEqual(RouPlaylistDecoder.decode(MEDIA), MEDIA)

    def test_non_png_or_hls_has_specific_error(self):
        for data in (b'<MPD/>', b'\x00\x00\x00\x18ftypisom', b'\xff\xd8\xff', b'not media', b''):
            with self.subTest(data=data):
                self.assert_code('ROU_NOT_PNG_OR_HLS', data)

    def test_instance_and_bytes_like_inputs(self):
        decoder = RouPlaylistDecoder()
        for data in (png(MEDIA), bytearray(png(MEDIA)), memoryview(png(MEDIA))):
            with self.subTest(input_type=type(data).__name__):
                self.assertEqual(decoder.decode(data), MEDIA)

    def test_plain_ro_ud_playlist(self):
        self.assertEqual(RouPlaylistDecoder.decode(png(MEDIA)), MEDIA)

    def test_zlib_ro_ud_playlist(self):
        self.assertEqual(RouPlaylistDecoder.decode(png(zlib.compress(MEDIA), flag=1)), MEDIA)

    def test_raw_deflate_ro_ud_playlist(self):
        self.assertEqual(RouPlaylistDecoder.decode(png(raw_deflate(MEDIA), flag=1)), MEDIA)

    def test_flag_is_compression_bit_not_exact_integer(self):
        self.assertEqual(RouPlaylistDecoder.decode(png(MEDIA, flag=2)), MEDIA)
        self.assertEqual(RouPlaylistDecoder.decode(png(zlib.compress(MEDIA), flag=3)), MEDIA)

    def test_bom_and_initial_whitespace_are_normalized(self):
        self.assertEqual(RouPlaylistDecoder.decode(png(b'\xef\xbb\xbf\n \t' + MEDIA)), MEDIA)

    def test_headless_extinf_fragment_is_rejected(self):
        fragment = b'# comment\n  #EXTINF:2,\nseg.png\n'
        self.assert_code('ROU_PLAYLIST_INVALID', png(fragment))

    def test_headless_master_fragment_is_rejected(self):
        fragment = MASTER.split(b'\n', 1)[1]
        self.assert_code('ROU_PLAYLIST_INVALID', png(fragment))

    def test_aes128_multiline_key_and_png_segments_are_protocol_data(self):
        playlist = (b'#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n'
                    b'#EXT-X-KEY:METHOD=AES-128,URI="enc.key",\nIV=0x00000000000000000000000000000000\n'
                    b'#EXTINF:2,\nabc000.png\n#EXTINF:2,\nabc001.png\n')
        info = parse_manifest(RouPlaylistDecoder.decode(png(zlib.compress(playlist), flag=1)), 'https://cdn.test/index.png')
        self.assertEqual(info.classification, 'HLS_AES128')
        self.assertFalse(info.drm)
        self.assertEqual([resource.role for resource in info.resources], ['Media Segment', 'Media Segment'])

    def test_ordinary_png_has_specific_chunk_not_found_error(self):
        ordinary = png(before=chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00\xff')))
        self.assert_code('ROU_CHUNK_NOT_FOUND', ordinary)

    def test_non_playlist_ro_ud_is_not_trusted(self):
        for payload in (b'not a playlist', b'{"url":"https://cdn.test/x"}', b'<MPD/>', b'<html>#EXTINF:2,</html>'):
            with self.subTest(payload=payload):
                self.assert_code('ROU_PLAYLIST_INVALID', png(payload))

    def test_invalid_utf8_in_ro_ud(self):
        self.assert_code('ROU_PLAYLIST_INVALID', png(MEDIA + b'\xff'))

    def test_invalid_hls_attributes_are_reported_as_playlist_error(self):
        malformed = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="unterminated\n#EXTINF:2,\nseg.png\n'
        self.assert_code('ROU_PLAYLIST_INVALID', png(malformed))

    def test_incomplete_extinf_does_not_pass_marker_check(self):
        self.assert_code('ROU_PLAYLIST_INVALID', png(b'#EXTINF:2,\n'))

    def test_incomplete_master_does_not_pass_marker_check(self):
        self.assert_code('ROU_PLAYLIST_INVALID', png(b'#EXT-X-STREAM-INF:BANDWIDTH=100\n'))

    def test_empty_ro_ud_payload(self):
        self.assert_code('ROU_PAYLOAD_INVALID', png(b''))

    def test_missing_ro_ud_flag(self):
        data = PNG_SIGNATURE + chunk(b'IHDR', IHDR) + chunk(b'roUd') + chunk(b'IEND')
        self.assert_code('ROU_PAYLOAD_INVALID', data)

    def test_truncated_or_wrong_png_signature(self):
        for data in (b'\x89PNG', b'\x89PNG\r\n', b'\x89PNGwrong'):
            with self.subTest(data=data):
                self.assert_code('ROU_PNG_INVALID', data)

    def test_chunk_header_is_bounded(self):
        self.assert_code('ROU_PNG_INVALID', PNG_SIGNATURE + b'\x00\x00\x00')

    def test_chunk_payload_and_crc_are_bounded(self):
        data = png(MEDIA)
        for data in (data[:-1], data[:-5], data[:30], PNG_SIGNATURE + struct.pack('>I4s', 100, b'IHDR') + b'x' * 13):
            with self.subTest(size=len(data)):
                self.assert_code('ROU_PNG_INVALID', data)

    def test_integer_length_overflow_does_not_allocate(self):
        data = PNG_SIGNATURE + chunk(b'IHDR', IHDR) + struct.pack('>I4s', 0xffffffff, b'roUd') + b'\0' * 4
        self.assert_code('ROU_PNG_INVALID', data)

    def test_crc_mismatch_in_ro_ud(self):
        invalid = bytearray(chunk(b'roUd', b'\0' + MEDIA))
        invalid[-1] ^= 1
        self.assert_code('ROU_PNG_INVALID', PNG_SIGNATURE + chunk(b'IHDR', IHDR) + invalid + chunk(b'IEND'))

    def test_crc_after_ro_ud_is_still_checked(self):
        invalid = bytearray(chunk(b'tEXt', b'comment'))
        invalid[-1] ^= 1
        self.assert_code('ROU_PNG_INVALID', png(MEDIA, after=invalid))

    def test_crc_in_ihdr_is_checked(self):
        invalid = bytearray(chunk(b'IHDR', IHDR))
        invalid[-1] ^= 1
        self.assert_code('ROU_PNG_INVALID', PNG_SIGNATURE + invalid + chunk(b'roUd', b'\0' + MEDIA) + chunk(b'IEND'))

    def test_chunk_type_letters_and_reserved_bit(self):
        for kind in (b'bad1', b'rodu', b'\xffoUd'):
            with self.subTest(kind=kind):
                self.assert_code('ROU_PNG_INVALID', png(MEDIA, before=chunk(kind)))

    def test_ihdr_must_be_first(self):
        self.assert_code('ROU_PNG_INVALID', PNG_SIGNATURE + chunk(b'roUd', b'\0' + MEDIA) + chunk(b'IEND'))

    def test_ihdr_length_is_validated(self):
        self.assert_code('ROU_PNG_INVALID', PNG_SIGNATURE + chunk(b'IHDR', IHDR[:-1]) + chunk(b'IEND'))

    def test_ihdr_attributes_are_validated(self):
        for values in ((0, 1, 8, 6, 0, 0, 0), (1, 1, 3, 6, 0, 0, 0),
                       (1, 1, 8, 6, 1, 0, 0), (1, 1, 8, 6, 0, 1, 0), (1, 1, 8, 6, 0, 0, 2)):
            with self.subTest(values=values):
                data = PNG_SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', *values)) + chunk(b'IEND')
                self.assert_code('ROU_PNG_INVALID', data)

    def test_duplicate_ihdr_is_rejected(self):
        self.assert_code('ROU_PNG_INVALID', png(MEDIA, before=chunk(b'IHDR', IHDR)))

    def test_duplicate_ro_ud_is_rejected(self):
        self.assert_code('ROU_PNG_INVALID', png(MEDIA, after=chunk(b'roUd', b'\0' + MEDIA)))

    def test_iend_must_be_present_empty_and_terminal(self):
        for data in (png(MEDIA)[:-12], png(MEDIA) + b'junk', png(MEDIA)[:-12] + chunk(b'IEND', b'x')):
            with self.subTest(size=len(data)):
                self.assert_code('ROU_PNG_INVALID', data)

    def test_chunk_count_limit(self):
        self.assert_code('ROU_PNG_INVALID', png(MEDIA, before=chunk(b'tEXt') * MAX_CHUNKS))

    def test_total_png_input_size_limit(self):
        self.assert_code('ROU_PLAYLIST_TOO_LARGE', PNG_SIGNATURE + bytes(MAX_PNG_SIZE))

    def test_corrupted_deflate(self):
        self.assert_code('ROU_DEFLATE_FAILED', png(b'invalid zip', flag=1))

    def test_truncated_deflate(self):
        self.assert_code('ROU_DEFLATE_FAILED', png(zlib.compress(MEDIA)[:-2], flag=1))

    def test_trailing_compressed_bytes_and_members_are_rejected(self):
        for payload in (zlib.compress(MEDIA) + b'junk', zlib.compress(MEDIA) + zlib.compress(MEDIA)):
            with self.subTest(size=len(payload)):
                self.assert_code('ROU_DEFLATE_FAILED', png(payload, flag=1))

    def test_gzip_is_not_deflate_wrapper(self):
        self.assert_code('ROU_DEFLATE_FAILED', png(gzip.compress(MEDIA), flag=1))

    def test_zip_bomb_cannot_exceed_output_limit(self):
        bomb = zlib.compress(b'#EXTM3U\n' + b'#' * (DEFAULT_MAX_SIZE + 1000))
        self.assertLess(len(bomb), 4096)
        self.assert_code('ROU_PLAYLIST_TOO_LARGE', png(bomb, flag=1))

    def test_raw_zip_bomb_has_the_same_output_limit(self):
        self.assert_code('ROU_PLAYLIST_TOO_LARGE', png(raw_deflate(MEDIA * 200), flag=1), max_size=128)

    def test_output_limit_boundary_and_normalization_overflow(self):
        self.assertEqual(RouPlaylistDecoder.decode(png(MEDIA), max_size=len(MEDIA)), MEDIA)
        self.assertEqual(RouPlaylistDecoder.decode(png(zlib.compress(MEDIA), flag=1), max_size=len(MEDIA)), MEDIA)
        self.assert_code('ROU_PLAYLIST_TOO_LARGE', png(MEDIA), max_size=len(MEDIA) - 1)
        fragment = MEDIA.split(b'\n', 1)[1]
        self.assert_code('ROU_PLAYLIST_INVALID', png(fragment), max_size=len(fragment))

    def test_non_png_output_limit(self):
        self.assert_code('ROU_PLAYLIST_TOO_LARGE', MEDIA, max_size=10)

    def test_decoder_exposes_text_and_protocol_trace(self):
        result = RouPlaylistDecoder.decode_with_trace(png(zlib.compress(MEDIA), flag=3))
        self.assertEqual(result.text, MEDIA.decode())
        self.assertEqual(result.trace()['flag'], '0x03')
        self.assertEqual(result.trace()['compression'], 'zlib')
        self.assertEqual(result.trace()['container'], 'PNG')
        self.assertEqual(RouPlaylistDecoder.decode_text(MEDIA), MEDIA.decode())

    def test_header_alone_and_bare_uri_do_not_validate(self):
        for payload in (b'#EXTM3U\n', b'#EXTM3UX\n#EXTINF:2,\ns.png\n', b'#EXTM3U\ns.png\n'):
            self.assert_code('ROU_PLAYLIST_INVALID', png(payload))

    def test_invalid_parameters_return_classified_errors(self):
        for max_size in (0, -1, True, 1.5, None, MAX_PNG_SIZE + 1, 2**100):
            with self.subTest(max_size=max_size):
                self.assert_code('ROU_PLAYLIST_INVALID', MEDIA, max_size=max_size)
        for data in (None, 'a playlist', 1, ['bytes']):
            with self.subTest(input_type=type(data).__name__):
                self.assert_code('ROU_PLAYLIST_INVALID', data)


if __name__ == '__main__':
    unittest.main()
