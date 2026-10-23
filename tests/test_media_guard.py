import threading
import unittest
from core.media_guard import (inspect_manifest, validate_url, check_media, ProtectedMediaError,
                              MediaCheckError, MediaCheckCancelled)


class MediaGuardTests(unittest.TestCase):
    def test_plain_hls(self):
        self.assertEqual(inspect_manifest(b'#EXTM3U\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST', 'http://example.com/a.m3u8'), [])

    def test_standard_encryption_is_not_drm(self):
        for method in ('AES-128', 'SAMPLE-AES', 'SAMPLE-AES-CTR'):
            self.assertEqual(inspect_manifest(f'#EXTM3U\n#EXT-X-KEY:METHOD={method},URI="key"'.encode(), 'https://x/a.m3u8'), [])

    def test_none_allowed(self):
        inspect_manifest(b'#EXTM3U\n#EXT-X-KEY:METHOD=NONE', 'https://x/a.m3u8')

    def test_session_encryption_refused(self):
        with self.assertRaises(ProtectedMediaError):
            inspect_manifest(b'#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="skd://key"', 'https://x/a.m3u8')

    def test_user_aes128_multiline_iv_is_allowed(self):
        self.assertEqual(inspect_manifest(b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-KEY:METHOD=AES-128,URI="key.dat",\n'
                                          b'IV=0x00000000000000000000000000000000',
                                          'https://x/index?sign=xx&t=1'), [])

    def test_true_drm_systems_refused(self):
        for keyformat in ('com.apple.streamingkeydelivery', 'com.widevine', 'com.microsoft.playready'):
            with self.subTest(keyformat=keyformat), self.assertRaises(ProtectedMediaError) as error:
                inspect_manifest(f'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.dat",KEYFORMAT="{keyformat}"'.encode(), 'https://x/index')
            self.assertEqual(error.exception.code, 'PROTECTED_MEDIA')

    def test_master_with_audio(self):
        children = inspect_manifest(b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100\nvideo.m3u8?sign=abc&t=123', 'https://x/folder/master.m3u8')
        self.assertIn('https://x/folder/video.m3u8?sign=abc&t=123', children)
        self.assertIn('https://x/folder/audio.m3u8', children)

    def test_drm_mpd(self):
        with self.assertRaises(ProtectedMediaError):
            inspect_manifest(b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><ContentProtection/></Period></MPD>', 'https://x/a.mpd')

    def test_invalid_manifest_and_page(self):
        for data, url in ((b'Expired', 'https://x/a.m3u8'), (b'<html>login</html>', 'https://x/index.html')):
            with self.assertRaises(MediaCheckError):
                inspect_manifest(data, url)

    def test_scheme_and_credentials(self):
        for url in ('file:///x/a.m3u8', 'ftp://x/a.m3u8', 'https://user:pass@x/a.m3u8', 'https://x/a\n.m3u8'):
            with self.assertRaises(MediaCheckError):
                validate_url(url)

    def test_early_cancel(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(MediaCheckCancelled):
            check_media('http://localhost/a.m3u8', {}, event)

    def test_encrypted_mp4_metadata(self):
        def box(tag, content=b''):
            return (8 + len(content)).to_bytes(4, 'big') + tag + content
        for tag in (b'pssh', b'sinf', b'encv', b'enca'):
            with self.assertRaises(ProtectedMediaError):
                inspect_manifest(box(b'ftyp', b'isom') + box(b'moov', box(tag, b'encrypted')),
                                 'https://x/a.mp4', 'video/mp4')

    def test_plain_mp4_metadata(self):
        data = b'\x00\x00\x00\x0cftypisom\x00\x00\x00\x08moov'
        self.assertEqual(inspect_manifest(data, 'https://x/a.mp4', 'video/mp4'), [])


if __name__ == '__main__':
    unittest.main()
