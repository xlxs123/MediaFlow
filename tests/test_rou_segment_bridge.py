"""PNG 分片二进制解包、媒体 Range 和 AES/DRM 边界的桥接回归。"""
import io
import re
import struct
import unittest
import zlib

from core.errors import MediaCheckError
from core.http_client import ContextHttpClient, HttpResponse
from core.media_bridge import MediaBridge
from core.media_guard import check_media
from core.request_context import RequestContext
from tests.test_rou_playlist_decoder import png, chunk


TS = (b'\x47\x40\x00\x10' + b'\0' * 184) * 4
CLEAR = '#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nopaque.png\n#EXT-X-ENDLIST\n'


class RemoteStream(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Type': 'image/png', 'Content-Length': str(len(data)), 'Accept-Ranges': 'bytes'}


class RouSegmentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = None
        self.client = ContextHttpClient()
        self.hits = []

    def tearDown(self):
        if self.bridge:
            self.bridge.close()

    def start(self, payload, text=CLEAR, *, embedded=True, inherit_range=True):
        context = RequestContext('https://media.example/index.png?auth=preserved',
            headers={'Referer': 'https://page.example/watch', 'User-Agent': 'Regression Browser', 'Range': 'bytes=0-'},
            inherit_range=inherit_range)
        inspection = check_media(context, manifest_text=text, manifest_url=context.url,
            manifest_origin='embedded:png/roUd' if embedded else 'captured:hls', expected_media_type='HLS')
        self.bridge = MediaBridge(inspection).start()

        def remote(context, overrides=None):
            self.hits.append((context, overrides))
            return HttpResponse(context, RemoteStream(payload), [])

        self.bridge.client.open = remote
        with self.client.open(RequestContext(self.bridge.url)) as response:
            manifest = response.read().decode()
        return re.findall(r'^http[^\n]+', manifest, re.M)

    def request(self, url, *, byte_range=None, method='GET'):
        headers = {'Range': byte_range} if byte_range else {}
        with self.client.open(RequestContext(url, method=method, headers=headers)) as response:
            return response.status, response.read(), dict(response.stream.headers)

    def test_png_segment_roud_payload_is_forwarded_instead_of_transport(self):
        transport = png(TS, 0)
        segment, = self.start(transport)
        status, body, headers = self.request(segment)
        self.assertEqual(status, 200)
        self.assertEqual(body, TS)
        self.assertFalse(body.startswith(b'\x89PNG'))
        self.assertEqual(int(headers['Content-Length']), len(TS))
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        context, overrides = self.hits[0]
        self.assertEqual(overrides['range'], 'bytes=0-')
        self.assertNotIn('origin', context.headers)
        self.assertEqual(context.referer, 'https://page.example/watch')

    def test_zlib_wrapper_and_unknown_extension_are_decoded_by_manifest_role(self):
        segment, = self.start(png(zlib.compress(TS), 1), CLEAR.replace('opaque.png', 'no-extension'))
        self.assertEqual(self.request(segment)[1], TS)

    def test_partial_range_targets_decoded_bytes_and_remote_reads_full_png(self):
        segment, = self.start(png(TS, 0))
        status, body, headers = self.request(segment, byte_range='bytes=8-23')
        self.assertEqual(status, 206)
        self.assertEqual(body, TS[8:24])
        self.assertEqual(headers['Content-Range'], f'bytes 8-23/{len(TS)}')
        self.assertEqual(self.hits[0][1]['range'], 'bytes=0-')

    def test_suffix_range_uses_decoded_media_length(self):
        segment, = self.start(png(TS, 0))
        status, body, headers = self.request(segment, byte_range='bytes=-16')
        self.assertEqual(status, 206)
        self.assertEqual(body, TS[-16:])
        self.assertEqual(headers['Content-Length'], '16')

    def test_range_isolation_does_not_send_manifest_range_to_transport(self):
        segment, = self.start(png(TS, 0), inherit_range=False)
        self.assertEqual(self.request(segment, byte_range='bytes=8-23')[1], TS[8:24])
        self.assertIsNone(self.hits[0][1]['range'])

    def test_head_reports_decoded_length_without_png_body(self):
        segment, = self.start(png(TS, 0))
        status, body, headers = self.request(segment, method='HEAD')
        self.assertEqual(status, 200)
        self.assertEqual(body, b'')
        self.assertEqual(int(headers['Content-Length']), len(TS))

    def test_raw_ts_with_image_mime_remains_valid_and_range_is_sliced(self):
        segment, = self.start(TS)
        self.assertEqual(self.request(segment, byte_range='bytes=8-23')[1], TS[8:24])

    def test_png_without_roud_is_rejected_before_forwarding(self):
        segment, = self.start(png(None))
        with self.assertRaises(MediaCheckError):
            self.request(segment)
        self.assertEqual(self.bridge.last_error.code, 'OUTPUT_NOT_VIDEO')

    def test_ciphertext_wrapper_is_forwarded_only_after_explicit_aes128_key(self):
        cipher = bytes(range(64))
        text = CLEAR.replace('#EXTINF:', '#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x' + '0' * 32 + '\n#EXTINF:')
        segment, = self.start(png(cipher, 0), text)
        self.assertEqual(self.request(segment)[1], cipher)

    def test_method_none_resets_ciphertext_permission(self):
        cipher = bytes(range(64))
        text = CLEAR.replace('#EXTINF:', '#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:')
        segment, = self.start(png(cipher, 0), text)
        with self.assertRaises(MediaCheckError):
            self.request(segment)
        self.assertEqual(self.bridge.last_error.code, 'OUTPUT_NOT_VIDEO')

    def test_roud_adts_audio_does_not_go_through_manifest_text_parser(self):
        audio = (b'\xff\xf1\x50\x80\x01\x3f\xfc' + b'\0' * 2) * 2
        segment, = self.start(png(audio, 0))
        self.assertEqual(self.request(segment)[1], audio)

    def test_raw_ciphertext_range_does_not_go_through_manifest_text_parser(self):
        cipher = bytes(range(64))
        text = CLEAR.replace('#EXTINF:', '#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n#EXTINF:')
        segment, = self.start(cipher, text)
        self.assertEqual(self.request(segment, byte_range='bytes=8-23')[1], cipher[8:24])

    def test_aes128_label_cannot_hide_explicit_pssh_in_wrapped_bmff(self):
        from tests.test_manifests import box, pssh, WIDEVINE
        media = box(b'ftyp', b'isom\0\0\0\0isom') + box(b'moov', pssh(WIDEVINE)) + box(b'free', b'\0' * 12)
        self.assertEqual(len(media) % 16, 0)
        text = CLEAR.replace('#EXTINF:', '#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n#EXTINF:')
        segment, = self.start(png(media, 0), text)
        with self.assertRaises(MediaCheckError):
            self.request(segment)
        self.assertEqual(self.bridge.last_error.code, 'PROTECTED_MEDIA')

    def test_png_roud_bmff_pssh_is_still_protected_media(self):
        system = bytes.fromhex('edef8ba979d64acea3c827dcd51d21ed')
        pssh_data = b'\0' * 4 + system + struct.pack('>I', 0)
        box = struct.pack('>I4s', len(pssh_data) + 8, b'pssh') + pssh_data
        ftyp = struct.pack('>I4s', 20, b'ftyp') + b'isom\0\0\0\0isom'
        segment, = self.start(png(ftyp + struct.pack('>I4s', 8, b'moov') + box, 0))
        with self.assertRaises(MediaCheckError):
            self.request(segment)
        self.assertEqual(self.bridge.last_error.code, 'PROTECTED_MEDIA')


if __name__ == '__main__':
    unittest.main()
