from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import zlib

from core.errors import MediaCheckError
from core import rou_segment_decoder
from core.rou_segment_decoder import (MAX_SEGMENT_DECODED_SIZE, MAX_SEGMENT_TRANSPORT_SIZE,
                                     PNG_SIGNATURE, decode_media_segment)


IHDR = struct.pack('>IIBBBBB', 320, 192, 8, 2, 0, 0, 0)
HLS = b'#EXTM3U\n#EXTINF:2,\nvideo.png\n#EXT-X-ENDLIST\n'


def chunk(kind, payload=b''):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)


def png(payload=None, flag=0, *, header=IHDR, before=b'', after=b''):
    encoded = chunk(b'roUd', bytes([flag]) + payload) if payload is not None else b''
    return PNG_SIGNATURE + chunk(b'IHDR', header) + before + encoded + after + chunk(b'IEND')


def raw_deflate(data):
    compressor = zlib.compressobj(wbits=-15)
    return compressor.compress(data) + compressor.flush()


def ts_packets(count=2, packet_size=188):
    base = b'\x47\x40\x00\x10' + bytes(184)
    if packet_size == 192:
        packet = bytes(4) + base
    elif packet_size == 204:
        packet = base + bytes(16)
    else:
        packet = base
    return packet * count


def box(kind, payload=b'', *, extended=False, to_end=False):
    if extended:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', 0 if to_end else len(payload) + 8, kind) + payload


FMP4 = box(b'moof', box(b'mfhd', bytes(8))) + box(b'mdat', b'fragment media payload')
INIT_MP4 = box(b'ftyp', b'isom\x00\x00\x00\x00isom') + box(b'moov', box(b'trak'))


def aac_frame(payload=b'\x11\x22\x33', crc=False):
    length = (9 if crc else 7) + len(payload)
    header = bytes((0xff, 0xf0 if crc else 0xf1, 0x4c, 0x80 | ((length >> 11) & 3),
                    (length >> 3) & 255, ((length & 7) << 5) | 0x1f, 0xfc))
    return header + (bytes(2) if crc else b'') + payload


class RouSegmentDecoderTests(unittest.TestCase):
    def assert_code(self, code, data, **kwargs):
        with self.assertRaises(MediaCheckError) as error:
            decode_media_segment(data, **kwargs)
        self.assertEqual(error.exception.code, code)

    def test_non_png_media_passes_through_unchanged(self):
        for data in (ts_packets(), FMP4, INIT_MP4, aac_frame(), b'other original media bytes'):
            with self.subTest(prefix=data[:8]):
                self.assertEqual(decode_media_segment(data), data)

    def test_non_png_encrypted_media_passes_through_unchanged(self):
        ciphertext = bytes(range(32))
        self.assertEqual(decode_media_segment(ciphertext, encrypted=True), ciphertext)

    def test_bytes_like_input_returns_bytes_without_mutation(self):
        source = png(ts_packets())
        for data in (source, bytearray(source), memoryview(source)):
            with self.subTest(type=type(data).__name__):
                self.assertIsInstance(decode_media_segment(data), bytes)
                self.assertEqual(decode_media_segment(data), ts_packets())
                self.assertEqual(bytes(data), source)

    def test_png_flag_zero_contains_binary_ts_not_playlist(self):
        self.assertEqual(decode_media_segment(png(ts_packets())), ts_packets())

    def test_real_376_byte_two_packet_case_is_supported(self):
        data = ts_packets()
        self.assertEqual(len(data), 376)
        self.assertEqual(decode_media_segment(png(data)), data)

    def test_ts_packet_sizes_supported(self):
        for packet_size in (188, 192, 204):
            with self.subTest(packet_size=packet_size):
                data = ts_packets(3, packet_size)
                self.assertEqual(decode_media_segment(png(data)), data)

    def test_ts_sync_and_packet_alignment_are_required(self):
        for payload in (ts_packets(1), ts_packets()[:-1], ts_packets() + b'x', b'\x47' + bytes(187) + ts_packets(1)):
            with self.subTest(size=len(payload)):
                self.assert_code('OUTPUT_NOT_VIDEO', png(payload))

    def test_ts_reserved_adaptation_control_is_not_media(self):
        self.assert_code('OUTPUT_NOT_VIDEO', png((b'\x47\x40\x00\x00' + bytes(184)) * 2))

    def test_compressed_ts_zlib_is_supported(self):
        self.assertEqual(decode_media_segment(png(zlib.compress(ts_packets(4)), flag=1)), ts_packets(4))

    def test_compressed_ts_raw_deflate_is_supported(self):
        self.assertEqual(decode_media_segment(png(raw_deflate(ts_packets(4)), flag=1)), ts_packets(4))

    def test_fragmented_mp4_is_supported(self):
        self.assertEqual(decode_media_segment(png(FMP4)), FMP4)

    def test_mp4_initialization_is_supported(self):
        self.assertEqual(decode_media_segment(png(INIT_MP4)), INIT_MP4)

    def test_fmp4_with_styp_and_sidx_is_supported(self):
        data = box(b'styp', b'msis\x00\x00\x00\x00msis') + box(b'sidx', bytes(12)) + FMP4
        self.assertEqual(decode_media_segment(png(data)), data)

    def test_extended_bmff_box_and_last_zero_size_box_are_supported(self):
        data = box(b'moof', box(b'mfhd', bytes(8)), extended=True) + box(b'mdat', b'payload', to_end=True)
        self.assertEqual(decode_media_segment(png(data)), data)

    def test_bmff_truncation_or_invalid_size_is_rejected(self):
        for payload in (FMP4[:-1], struct.pack('>I4s', 7, b'moof') + b'data',
                        struct.pack('>I4s', 1, b'moof') + b'x', box(b'ftyp') + box(b'moov')):
            with self.subTest(prefix=payload[:8]):
                self.assert_code('OUTPUT_NOT_VIDEO', png(payload))

    def test_bmff_only_moof_or_mdat_cannot_pass(self):
        for payload in (box(b'moof'), box(b'mdat', b'payload'), box(b'ftyp', b'isom0000') + box(b'mdat', b'payload')):
            self.assert_code('OUTPUT_NOT_VIDEO', png(payload))

    def test_bmff_box_count_is_bounded(self):
        data = box(b'styp', b'msis0000') + box(b'free') * 5 + FMP4
        with mock.patch.object(rou_segment_decoder, 'MAX_BMFF_BOXES', 4):
            self.assert_code('OUTPUT_NOT_VIDEO', png(data))

    def test_adts_aac_frames_are_supported(self):
        for payload in (aac_frame(), aac_frame() * 3, aac_frame(crc=True) * 2):
            with self.subTest(size=len(payload)):
                self.assertEqual(decode_media_segment(png(payload)), payload)

    def test_adts_aac_with_id3_timestamp_header_is_supported(self):
        payload = b'ID3\x04\x00\x00\x00\x00\x00\x04time' + aac_frame() * 2
        self.assertEqual(decode_media_segment(png(payload)), payload)

    def test_truncated_or_reserved_aac_is_not_media(self):
        invalid_freq = bytearray(aac_frame())
        invalid_freq[2] = (invalid_freq[2] & 0xc3) | (15 << 2)
        for data in (aac_frame()[:-1], aac_frame() + b'x', bytes(invalid_freq), b'ID3\x04\x00\x00\x80\x00\x00\x00' + aac_frame()):
            self.assert_code('OUTPUT_NOT_VIDEO', png(data))

    def test_png_dimensions_do_not_determine_media_role(self):
        for width, height in ((1, 1), (320, 192), (864, 864), (1920, 1080)):
            with self.subTest(dimensions=(width, height)):
                header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
                self.assertEqual(decode_media_segment(png(ts_packets(), header=header)), ts_packets())

    def test_ordinary_aes128_ciphertext_is_returned_without_decrypting(self):
        ciphertext = b'\xa9\x7c\x10\x3d' * 16
        self.assertEqual(decode_media_segment(png(ciphertext), encrypted=True), ciphertext)

    def test_compressed_ciphertext_is_returned_for_existing_aes128_downloader(self):
        ciphertext = b'\xa9\x7c\x10\x3d' * 16
        self.assertEqual(decode_media_segment(png(zlib.compress(ciphertext), flag=1), encrypted=True), ciphertext)

    def test_ciphertext_requires_explicit_encrypted_context(self):
        self.assert_code('OUTPUT_NOT_VIDEO', png(b'\xa9\x7c\x10\x3d' * 16))

    def test_ciphertext_requires_full_aes_blocks(self):
        for ciphertext in (b'', b'x' * 15, b'x' * 17, b'x' * 31):
            with self.subTest(size=len(ciphertext)):
                expected = 'ROU_SEGMENT_INVALID' if not ciphertext else 'OUTPUT_NOT_VIDEO'
                self.assert_code(expected, png(ciphertext), encrypted=True)
        self.assert_code('OUTPUT_NOT_VIDEO', b'x' * 17, encrypted=True)

    def test_manifest_payload_is_never_segment(self):
        for text in (HLS, b'\xef\xbb\xbf \n' + HLS, b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"/>',
                     b'<?xml version="1.0"?><MPD/>', b'<html>not video</html>'):
            with self.subTest(prefix=text[:16]):
                self.assert_code('OUTPUT_NOT_VIDEO', png(text))

    def test_block_aligned_manifest_still_is_not_ciphertext(self):
        text = HLS + b' ' * ((-len(HLS)) % 16)
        self.assert_code('OUTPUT_NOT_VIDEO', png(text), encrypted=True)

    def test_image_payloads_cannot_be_nested_media(self):
        for payload in (png(), b'\xff\xd8\xff\xe0JPEG', b'GIF89a' + bytes(32), b'RIFF\x00\x00\x00\x00WEBP' + bytes(12)):
            with self.subTest(prefix=payload[:8]):
                self.assert_code('OUTPUT_NOT_VIDEO', png(payload))

    def test_block_aligned_image_payload_is_not_ciphertext(self):
        image = png()
        image += b'\0' * ((-len(image)) % 16)
        self.assert_code('OUTPUT_NOT_VIDEO', png(image), encrypted=True)

    def test_ordinary_png_without_roud_is_rejected(self):
        self.assert_code('OUTPUT_NOT_VIDEO', png(before=chunk(b'IDAT', zlib.compress(b'\x00\x80\xa0\xc0'))))

    def test_unknown_clear_payload_is_rejected(self):
        for payload in (b'not media', bytes(32), b'{"url":"https://example.test/video"}'):
            self.assert_code('OUTPUT_NOT_VIDEO', png(payload))

    def test_ancillary_png_chunks_are_allowed(self):
        data = png(ts_packets(), before=chunk(b'tEXt', b'Test\x00image shell'), after=chunk(b'pHYs', bytes(9)))
        self.assertEqual(decode_media_segment(data), ts_packets())

    def test_unknown_compression_flags_are_rejected(self):
        for flag in (2, 3, 127, 255):
            with self.subTest(flag=flag):
                self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), flag=flag))

    def test_unknown_critical_png_chunk_is_rejected(self):
        self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), before=chunk(b'BLAH')))

    def test_duplicate_roud_is_rejected(self):
        self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), after=chunk(b'roUd', b'\x00' + ts_packets())))

    def test_duplicate_header_is_rejected(self):
        self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), after=chunk(b'IHDR', IHDR)))

    def test_missing_or_noninitial_ihdr_is_rejected(self):
        data = PNG_SIGNATURE + chunk(b'roUd', b'\0' + ts_packets()) + chunk(b'IEND')
        self.assert_code('ROU_SEGMENT_INVALID', data)
        self.assert_code('ROU_SEGMENT_INVALID', PNG_SIGNATURE + chunk(b'IDAT') + png(ts_packets())[8:])

    def test_bad_ihdr_dimensions_and_parameters_are_rejected(self):
        for fields in ((0, 1, 8, 2, 0, 0, 0), (1, 0, 8, 2, 0, 0, 0), (2**31, 1, 8, 2, 0, 0, 0),
                       (1, 1, 4, 2, 0, 0, 0), (1, 1, 8, 7, 0, 0, 0), (1, 1, 8, 2, 1, 0, 0),
                       (1, 1, 8, 2, 0, 1, 0), (1, 1, 8, 2, 0, 0, 2)):
            with self.subTest(fields=fields):
                self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), header=struct.pack('>IIBBBBB', *fields)))

    def test_bad_ihdr_size_is_rejected(self):
        self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), header=IHDR[:-1]))

    def test_crc_is_checked_for_every_chunk(self):
        data = png(ts_packets(), before=chunk(b'IDAT', zlib.compress(b'pixels')))
        offset = 8
        while offset < len(data):
            length = struct.unpack_from('>I', data, offset)[0]
            modified = bytearray(data)
            modified[offset + 8 + length] ^= 1
            self.assert_code('ROU_SEGMENT_INVALID', modified)
            offset += length + 12

    def test_chunk_length_cannot_exceed_available_bytes(self):
        data = bytearray(png(ts_packets()))
        struct.pack_into('>I', data, 33, 0xffffffff)
        self.assert_code('ROU_SEGMENT_INVALID', data)

    def test_bad_reserved_chunk_type_is_rejected(self):
        self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), before=chunk(b'xxxd')))
        self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), before=chunk(b'x1Xd')))

    def test_truncated_container_and_signature_are_rejected(self):
        for length in (1, 4, 7, 8, 20, 32, len(png(ts_packets())) - 1):
            with self.subTest(length=length):
                self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets())[:length])
        self.assert_code('ROU_SEGMENT_INVALID', b'\x89PNG\r\nwrong')

    def test_iend_is_required_and_must_be_empty(self):
        self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets())[:-12])
        data = png(ts_packets())[:-12] + chunk(b'IEND', b'x')
        self.assert_code('ROU_SEGMENT_INVALID', data)

    def test_trailing_bytes_and_multiple_pngs_are_rejected(self):
        for data in (png(ts_packets()) + b'x', png(ts_packets()) * 2):
            self.assert_code('ROU_SEGMENT_INVALID', data)

    def test_empty_ro_ud_and_flag_without_payload_are_rejected(self):
        self.assert_code('ROU_SEGMENT_INVALID', png(b''))
        data = PNG_SIGNATURE + chunk(b'IHDR', IHDR) + chunk(b'roUd') + chunk(b'IEND')
        self.assert_code('ROU_SEGMENT_INVALID', data)

    def test_chunk_count_is_bounded(self):
        with mock.patch.object(rou_segment_decoder, 'MAX_PNG_CHUNKS', 4):
            self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets(), before=chunk(b'tEXt') * 4))

    def test_transport_size_is_bounded_before_copy(self):
        with mock.patch.object(rou_segment_decoder, 'MAX_SEGMENT_TRANSPORT_SIZE', 32):
            self.assert_code('ROU_SEGMENT_TOO_LARGE', memoryview(png(ts_packets())))

    def test_decoded_size_is_bounded_for_uncompressed_and_raw_media(self):
        with mock.patch.object(rou_segment_decoder, 'MAX_SEGMENT_DECODED_SIZE', 375):
            self.assert_code('ROU_SEGMENT_TOO_LARGE', png(ts_packets()))
            self.assert_code('ROU_SEGMENT_TOO_LARGE', ts_packets())

    def test_zlib_and_raw_deflate_bombs_are_bounded(self):
        for compressed in (zlib.compress(ts_packets(100)), raw_deflate(ts_packets(100))):
            with mock.patch.object(rou_segment_decoder, 'MAX_SEGMENT_DECODED_SIZE', 1024):
                self.assert_code('ROU_SEGMENT_TOO_LARGE', png(compressed, flag=1))

    def test_corrupt_or_truncated_compression_is_rejected(self):
        for compressed in (b'not compressed', zlib.compress(ts_packets())[:-1], raw_deflate(ts_packets())[:-1]):
            self.assert_code('ROU_SEGMENT_DEFLATE_FAILED', png(compressed, flag=1))

    def test_compressed_trailing_or_multiple_streams_are_rejected(self):
        for compressed in (zlib.compress(ts_packets()) + b'x', zlib.compress(ts_packets()) * 2,
                           raw_deflate(ts_packets()) + b'x'):
            self.assert_code('ROU_SEGMENT_DEFLATE_FAILED', png(compressed, flag=1))

    def test_bad_input_and_ambiguous_encrypted_flag_are_rejected(self):
        for data in ('text', None, 42):
            self.assert_code('ROU_SEGMENT_INVALID', data)
        for encrypted in ('true', 1, None):
            self.assert_code('ROU_SEGMENT_INVALID', png(ts_packets()), encrypted=encrypted)
        self.assert_code('OUTPUT_NOT_VIDEO', b'')

    def test_public_size_limits(self):
        self.assertEqual(MAX_SEGMENT_TRANSPORT_SIZE, 64 * 1024 * 1024)
        self.assertEqual(MAX_SEGMENT_DECODED_SIZE, 32 * 1024 * 1024)


class RealRouSegmentMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.ffmpeg = cls.root / 'tools/ffmpeg.exe'
        if not cls.ffmpeg.is_file():
            raise unittest.SkipTest('实际媒体回归需要 ffmpeg。')

    def assert_real_media(self, options, extension):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ('real.' + extension)
            result = subprocess.run([str(self.ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', *options,
                                    '-threads', '1', str(path)], shell=False, capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            media = path.read_bytes()
            wrapper = png(media, header=struct.pack('>IIBBBBB', 864, 864, 8, 2, 0, 0, 0))
            self.assertEqual(decode_media_segment(wrapper), media)
            self.assertEqual(decode_media_segment(png(zlib.compress(media), flag=1)), media)

    def test_real_mpegts_bytes_are_extracted(self):
        self.assert_real_media(['-i', 'color=c=blue:s=48x32:r=5:d=0.4', '-c:v', 'mpeg2video', '-f', 'mpegts'], 'ts')

    def test_real_fragmented_mp4_bytes_are_extracted(self):
        self.assert_real_media(['-i', 'color=c=blue:s=48x32:r=5:d=0.4', '-c:v', 'mpeg4',
                                '-movflags', 'frag_keyframe+empty_moov', '-f', 'mp4'], 'mp4')

    def test_real_adts_aac_bytes_are_extracted(self):
        self.assert_real_media(['-i', 'sine=frequency=880:sample_rate=48000:duration=0.2', '-c:a', 'aac', '-f', 'adts'], 'aac')


if __name__ == '__main__':
    unittest.main()
