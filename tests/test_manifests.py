"""Protocol classification regressions; no network, key fetch or decryption."""

import base64
import struct
import unittest
import uuid

from core.errors import MediaCheckError, ProtectedMediaError
from core.manifests import KeyInfo, ManifestInfo, MediaResource, hls_lines, parse_attribute_list, parse_manifest


BASE = "https://cdn.example.com/final/playlist/index.m3u8?sign=private&t=42"
WIDEVINE = "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
PLAYREADY = "9a04f079-9840-4286-ab92-e65be0885f95"
FAIRPLAY = "94ce86fb-07ff-4f43-adb8-93d2fa968ca2"
USER_VOD = b'''#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=AES-128,URI="enc.key",
IV=0x00000000000000000000000000000000

#EXTINF:2.000000,
abc000.png
#EXTINF:2.000000,
abc001.png
'''


def box(kind, payload=b"", extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def pssh(system_id, version=0):
    payload = bytes([version, 0, 0, 0]) + uuid.UUID(system_id).bytes
    if version == 1:
        payload += struct.pack(">I", 0)
    payload += struct.pack(">I", 0)
    return box(b"pssh", payload)


def hls(key_tag="", extra=""):
    return ("#EXTM3U\n#EXT-X-VERSION:3\n" + key_tag + "\n" + extra + "\n#EXTINF:5,\nsegment01.ts\n#EXT-X-ENDLIST\n").encode()


class AttributeListTests(unittest.TestCase):
    def test_all_key_fields(self):
        attrs = parse_attribute_list('METHOD=AES-128,URI="key.dat",IV=0x01,KEYFORMAT="identity",KEYFORMATVERSIONS="1/2"')
        self.assertEqual(attrs, {"METHOD": "AES-128", "URI": "key.dat", "IV": "0x01", "KEYFORMAT": "identity", "KEYFORMATVERSIONS": "1/2"})

    def test_quoted_commas_are_part_of_value(self):
        self.assertEqual(parse_attribute_list('URI="key,a,b.dat",CODECS="avc1.4d401f,mp4a.40.2"'), {"URI": "key,a,b.dat", "CODECS": "avc1.4d401f,mp4a.40.2"})

    def test_spaces_case_normalization(self):
        self.assertEqual(parse_attribute_list(' method = AES-128 , uri = "key file.dat" '), {"METHOD": "AES-128", "URI": "key file.dat"})

    def test_url_query_equal_and_ampersand_preserved(self):
        value = "key.dat?sig=a==&t=1%2B2+3#fragment"
        self.assertEqual(parse_attribute_list('URI="' + value + '"')["URI"], value)

    def test_escaped_quote_and_backslash(self):
        self.assertEqual(parse_attribute_list(r'URI="one\"two\\three"')["URI"], 'one"two\\three')

    def test_user_comma_before_iv_line_break(self):
        text = '#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-KEY:METHOD=AES-128,URI="key.dat",\nIV=0x00000000000000000000000000000000'
        self.assertEqual(hls_lines(text)[-1], '#EXT-X-KEY:METHOD=AES-128,URI="key.dat",IV=0x00000000000000000000000000000000')

    def test_crlf_and_multiple_attribute_continuations(self):
        text = '#EXTM3U\r\n#EXT-X-KEY:METHOD=AES-128,\r\n URI="key.dat",\r\n KEYFORMAT="identity",\r\n IV=0x01\r\n'
        self.assertEqual(len(hls_lines(text)), 2)
        self.assertEqual(parse_attribute_list(hls_lines(text)[1].split(":", 1)[1])["IV"], "0x01")

    def test_key_tag_on_its_own_line_with_comma_separated_fields(self):
        text = '#EXTM3U\n#EXT-X-KEY:\nMETHOD=AES-128,\nURI="enc.key",\nIV=0x00\n#EXTINF:2,\nabc000.png'
        self.assertEqual(hls_lines(text), ["#EXTM3U", '#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x00', "#EXTINF:2,", "abc000.png"])

    def test_key_block_fields_without_physical_commas(self):
        text = '#EXTM3U\n#EXT-X-KEY:\nMETHOD=AES-128\nURI="enc.key"\nIV=0x00\nKEYFORMAT="identity"\nKEYFORMATVERSIONS="1/2"'
        lines = hls_lines(text)
        self.assertEqual(len(lines), 2)
        attrs = parse_attribute_list(lines[1].split(":", 1)[1])
        self.assertEqual(attrs, {"METHOD": "AES-128", "URI": "enc.key", "IV": "0x00", "KEYFORMAT": "identity", "KEYFORMATVERSIONS": "1/2"})

    def test_split_key_block_does_not_consume_real_resource_names(self):
        for name in ("IV=clip.ts", "URI=clip.png", "METHOD=clip.bin", "other=value"):
            with self.subTest(name=name):
                text = '#EXTM3U\n#EXT-X-KEY:\nMETHOD=AES-128\nURI="enc.key"\n' + name
                self.assertEqual(hls_lines(text)[-1], name)
                self.assertEqual(len(hls_lines(text)), 3)

    def test_split_key_block_does_not_consume_next_tag(self):
        text = '#EXTM3U\n#EXT-X-KEY:\nMETHOD=AES-128,\n#EXTINF:2,\nURI=actual-segment.png'
        self.assertEqual(hls_lines(text), ["#EXTM3U", "#EXT-X-KEY:METHOD=AES-128,", "#EXTINF:2,", "URI=actual-segment.png"])

    def test_inline_complete_key_does_not_start_unbounded_field_block(self):
        text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\nIV=clip.png'
        self.assertEqual(hls_lines(text)[-1], "IV=clip.png")

    def test_split_key_preserves_quoted_commas_and_query(self):
        text = '#EXTM3U\n#EXT-X-KEY:\nMETHOD=AES-128,\nURI="keys/a,b.key?auth=%2f+x&t=1",\nIV=0x01'
        attrs = parse_attribute_list(hls_lines(text)[1].split(":", 1)[1])
        self.assertEqual(attrs["URI"], "keys/a,b.key?auth=%2f+x&t=1")

    def test_no_joining_segment_uri_into_extinf(self):
        self.assertEqual(hls_lines("#EXTM3U\n#EXTINF:10,\nIV=clip.ts"), ["#EXTM3U", "#EXTINF:10,", "IV=clip.ts"])

    def test_no_joining_next_tag_after_attribute_comma(self):
        self.assertEqual(len(hls_lines('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,\n#EXTINF:10,\npart.ts')), 4)

    def test_empty_list(self):
        self.assertEqual(parse_attribute_list(" \t "), {})

    def test_malformed_lists_raise_typed_error(self):
        for value in ('METHOD', 'METHOD=', 'URI="incomplete', 'URI="key"extra', 'METHOD=AES-128,', 'METHOD=AES-128,METHOD=NONE', ',METHOD=NONE', 'BAD NAME=one', 'URI="one\ntwo"'):
            with self.subTest(value=value), self.assertRaises(MediaCheckError) as caught:
                parse_attribute_list(value)
            self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")
            self.assertNotIsInstance(caught.exception, ProtectedMediaError)


class HlsClassificationTests(unittest.TestCase):
    def test_exact_user_aes_sample_is_not_drm(self):
        data = b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-KEY:METHOD=AES-128,URI="key.dat",\nIV=0x00000000000000000000000000000000'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.media_type, "HLS")
        self.assertEqual(info.classification, "HLS_AES128")
        self.assertIs(info.drm, False)
        self.assertEqual(info.encryption, "AES-128")
        self.assertEqual(info.keys[0].method, "AES-128")
        self.assertEqual(info.keys[0].uri, "key.dat")
        self.assertEqual(info.keys[0].iv, "0x00000000000000000000000000000000")
        self.assertEqual(info.keys[0].resolved_uri, "https://cdn.example.com/final/playlist/key.dat")

    def test_plain_hls_without_keys(self):
        info = parse_manifest(hls(), BASE)
        self.assertEqual(info.classification, "CLEAR_HLS")
        self.assertEqual(info.encryption, "None")
        self.assertIs(info.drm, False)
        self.assertEqual(info.keys, [])

    def test_none_is_clear(self):
        info = parse_manifest(hls("#EXT-X-KEY:METHOD=NONE"), BASE)
        self.assertIs(info.drm, False)
        self.assertEqual(info.keys[0].classification, "CLEAR_HLS")
        self.assertEqual(info.keys[0].method, "NONE")

    def test_aes_identity_all_fields_parsed(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="key.dat?x=1&y=2",IV=0x0F,KEYFORMAT="identity",KEYFORMATVERSIONS="1/2"'), BASE)
        key = info.keys[0]
        self.assertEqual((key.method, key.keyformat, key.keyformatversions, key.iv), ("AES-128", "identity", "1/2", "0x0F"))
        self.assertIs(key.drm, False)
        self.assertEqual(key.resolved_uri, "https://cdn.example.com/final/playlist/key.dat?x=1&y=2")

    def test_aes_key_filename_is_not_drm_evidence(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="widevine/cenc/drm.key"'), BASE)
        self.assertEqual(info.classification, "HLS_AES128")
        self.assertIs(info.drm, False)

    def test_different_domain_does_not_change_classification(self):
        data = hls('#EXT-X-KEY:METHOD=AES-128,URI="key.dat"')
        for host in ("ordinary.example", "widevine.example", "fairplay.example", "unknown.invalid"):
            with self.subTest(host=host):
                self.assertIs(parse_manifest(data, "https://" + host + "/manifest").drm, False)

    def test_sample_aes_identity_is_not_automatically_protected(self):
        for method in ("SAMPLE-AES", "SAMPLE-AES-CTR", "SAMPLE-AES-CENC"):
            with self.subTest(method=method):
                info = parse_manifest(hls('#EXT-X-KEY:METHOD=' + method + ',URI="key.dat",KEYFORMAT="identity"'), BASE)
                self.assertEqual(info.classification, "HLS_SAMPLE_AES")
                self.assertIsNone(info.drm)

    def test_unknown_identity_algorithm_not_called_drm(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-256-GCM,URI="key.dat"'), BASE)
        self.assertEqual(info.classification, "HLS_ENCRYPTION_UNKNOWN")
        self.assertIsNone(info.drm)
        self.assertEqual(info.encryption, "AES-256-GCM")

    def test_fairplay_keyformat(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key67",KEYFORMAT="com.apple.streamingkeydelivery",KEYFORMATVERSIONS="1"'), BASE)
        self.assertEqual(info.classification, "DRM_FAIRPLAY")
        self.assertIs(info.drm, True)
        self.assertIs(info.keys[0].drm, True)

    def test_skd_is_explicit_fairplay_signal_even_without_keyformat(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key67"'), BASE)
        self.assertEqual(info.classification, "DRM_FAIRPLAY")
        self.assertIs(info.drm, True)

    def test_widevine_keyformat_and_uuid(self):
        for keyformat in ("com.widevine.alpha", "urn:uuid:" + WIDEVINE):
            with self.subTest(keyformat=keyformat):
                info = parse_manifest(hls('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="metadata.dat",KEYFORMAT="' + keyformat + '"'), BASE)
                self.assertEqual(info.classification, "DRM_WIDEVINE")
                self.assertIs(info.drm, True)

    def test_playready_keyformat_and_uuid(self):
        for keyformat in ("com.microsoft.playready", "urn:uuid:" + PLAYREADY):
            with self.subTest(keyformat=keyformat):
                info = parse_manifest(hls('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="metadata.dat",KEYFORMAT="' + keyformat + '"'), BASE)
                self.assertEqual(info.classification, "DRM_PLAYREADY")
                self.assertIs(info.drm, True)

    def test_fairplay_uuid(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="metadata.dat",KEYFORMAT="urn:uuid:' + FAIRPLAY + '"'), BASE)
        self.assertEqual(info.classification, "DRM_FAIRPLAY")

    def test_unknown_nonidentity_keyformat(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="metadata.dat",KEYFORMAT="custom.drm.system"'), BASE)
        self.assertEqual(info.classification, "DRM_UNKNOWN")
        self.assertIs(info.drm, True)
        self.assertTrue(any("非 identity" in evidence for evidence in info.evidence))

    def test_cenc_and_unknown_uuid_are_explicit_protection(self):
        for keyformat in ("cenc", "urn:uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
            with self.subTest(keyformat=keyformat):
                info = parse_manifest(hls('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="metadata.dat",KEYFORMAT="' + keyformat + '"'), BASE)
                self.assertIs(info.drm, True)
                self.assertEqual(info.classification, "DRM_UNKNOWN")

    def test_inline_pssh_classified_without_fetching(self):
        uri = "data:text/plain;base64," + base64.b64encode(pssh(WIDEVINE)).decode()
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="' + uri + '"'), BASE)
        self.assertEqual(info.classification, "DRM_WIDEVINE")
        self.assertIs(info.drm, True)

    def test_ordinary_inline_aes_key_is_not_pssh(self):
        uri = "data:application/octet-stream;base64," + base64.b64encode(b"a" * 16).decode()
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="' + uri + '"'), BASE)
        self.assertEqual(info.classification, "HLS_AES128")
        self.assertIs(info.drm, False)

    def test_aes_then_none_aggregate_remains_aes(self):
        data = hls('#EXT-X-KEY:METHOD=AES-128,URI="key.dat"', '#EXTINF:5,\nfirst.ts\n#EXT-X-KEY:METHOD=NONE')
        info = parse_manifest(data, BASE)
        self.assertEqual(len(info.keys), 2)
        self.assertEqual(info.classification, "HLS_AES128")
        self.assertEqual(info.encryption, "AES-128")
        self.assertIs(info.drm, False)

    def test_any_drm_key_makes_aggregate_protected(self):
        data = hls('#EXT-X-KEY:METHOD=AES-128,URI="key.dat"', '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXT-X-KEY:METHOD=NONE')
        info = parse_manifest(data, BASE)
        self.assertIs(info.drm, True)
        self.assertEqual(info.classification, "DRM_FAIRPLAY")
        self.assertIn("AES-128", info.encryption)
        self.assertIn("SAMPLE-AES", info.encryption)

    def test_key_rotation_keeps_all_keys(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="key1.dat",IV=0x01', '#EXT-X-KEY:METHOD=AES-128,URI="key2.dat",IV=0x02'), BASE)
        self.assertEqual([key.uri for key in info.keys], ["key1.dat", "key2.dat"])

    def test_relative_root_and_protocol_relative_key_uris(self):
        for uri, expected in (("../key.dat", "https://cdn.example.com/final/key.dat"), ("/keys/key.dat", "https://cdn.example.com/keys/key.dat"), ("//key.example.com/key.dat", "https://key.example.com/key.dat")):
            with self.subTest(uri=uri):
                key = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="' + uri + '"'), BASE).keys[0]
                self.assertEqual(key.resolved_uri, expected)
                self.assertEqual(key.uri, uri)

    def test_final_base_used_for_key_resolution(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="../key.dat?sign=a%2Fb&t=4"'), "https://redirect.example/new/location/current")
        self.assertEqual(info.keys[0].resolved_uri, "https://redirect.example/new/key.dat?sign=a%2Fb&t=4")

    def test_sensitive_uri_is_not_embedded_in_evidence(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="key.dat?token=private-cookie"'), BASE)
        self.assertFalse(any("private-cookie" in entry for entry in info.evidence))

    def test_content_hls_wins_over_suffix_and_mime(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="key.dat"'), "https://example.com/no-extension", "video/mp4")
        self.assertEqual(info.media_type, "HLS")

    def test_bom_and_leading_whitespace(self):
        info = parse_manifest(b"\xef\xbb\xbf\r\n\t" + hls(), BASE)
        self.assertEqual(info.media_type, "HLS")

    def test_key_without_method_or_uri_is_parse_error_not_drm(self):
        for tag in ('#EXT-X-KEY:URI="key.dat"', '#EXT-X-KEY:METHOD=AES-128', '#EXT-X-KEY', '#EXT-X-KEY:METHOD=NONE,URI="skd://key"'):
            with self.subTest(tag=tag), self.assertRaises(MediaCheckError) as caught:
                parse_manifest(hls(tag), BASE)
            self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")
            self.assertNotIsInstance(caught.exception, ProtectedMediaError)

    def test_bad_iv_or_versions_are_parse_error(self):
        for tail in ('IV=bad', 'IV=0x' + "A" * 33, 'KEYFORMATVERSIONS="0"', 'KEYFORMAT=""'):
            with self.subTest(tail=tail), self.assertRaises(MediaCheckError):
                parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="key.dat",' + tail), BASE)

    def test_invalid_key_host_is_typed_parse_error(self):
        with self.assertRaises(MediaCheckError) as caught:
            parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="https://[broken/key.dat"'), BASE)
        self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")


class HlsMasterTests(unittest.TestCase):
    def test_master_variant_children_from_final_base(self):
        data = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.4d401f,mp4a.40.2"\nlow/playlist\n#EXT-X-STREAM-INF:BANDWIDTH=1600000\n../high/index.m3u8?token=abc%2B&t=4\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.playlist_type, "Master")
        self.assertEqual(info.children, ["https://cdn.example.com/final/playlist/low/playlist", "https://cdn.example.com/final/high/index.m3u8?token=abc%2B&t=4"])

    def test_audio_subtitle_iframe_and_image_uri_attributes(self):
        data = b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="one",URI="audio/index"\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="cn",URI="subs/index"\n#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe/index"\n#EXT-X-IMAGE-STREAM-INF:BANDWIDTH=1,URI="image/index"\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.playlist_type, "Master")
        self.assertEqual(len(info.children), 4)

    def test_session_key_fields_and_classification(self):
        data = b'#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key.dat",KEYFORMAT="identity"\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvariant/index\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.classification, "HLS_AES128")
        self.assertIs(info.drm, False)
        self.assertEqual(info.keys[0].keyformat, "identity")

    def test_session_fairplay_is_protected(self):
        data = b'#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="skd://key",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvariant/index\n'
        self.assertEqual(parse_manifest(data, BASE).classification, "DRM_FAIRPLAY")

    def test_media_segments_map_and_parts_are_not_child_manifests(self):
        data = hls("", '#EXT-X-MAP:URI="init.mp4"\n#EXT-X-PART:DURATION=1,URI="part.m4s"\n#EXT-X-PRELOAD-HINT:TYPE=PART,URI="next.m4s"')
        info = parse_manifest(data, BASE)
        self.assertEqual(info.playlist_type, "Media")
        self.assertEqual(info.children, [])

    def test_rendition_report_resolves_sibling_manifest(self):
        info = parse_manifest(hls("", '#EXT-X-RENDITION-REPORT:URI="../another/playlist",LAST-MSN=10'), BASE)
        self.assertEqual(info.playlist_type, "Media")
        self.assertEqual(info.children, ["https://cdn.example.com/final/another/playlist"])

    def test_same_child_uri_deduplicated(self):
        data = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvariant\n#EXT-X-STREAM-INF:BANDWIDTH=2\nvariant\n'
        self.assertEqual(len(parse_manifest(data, BASE).children), 1)

    def test_missing_stream_child_is_parse_error(self):
        with self.assertRaises(MediaCheckError):
            parse_manifest(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n", BASE)

    def test_invalid_child_host_is_typed_parse_error(self):
        with self.assertRaises(MediaCheckError):
            parse_manifest(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://[broken/manifest\n", BASE)


class HlsResourceAndPlaylistTests(unittest.TestCase):
    def test_exact_user_vod_png_segments_without_endlist(self):
        info = parse_manifest(USER_VOD, BASE, "application/vnd.apple.mpegurl")
        self.assertEqual(info.media_type, "HLS")
        self.assertEqual(info.playlist_type, "Media")
        self.assertEqual(info.hls_playlist_type, "VOD")
        self.assertEqual(info.classification, "HLS_AES128")
        self.assertEqual(info.encryption, "AES-128")
        self.assertIs(info.drm, False)
        self.assertEqual(info.keys[0].uri, "enc.key")
        self.assertEqual(info.keys[0].iv, "0x00000000000000000000000000000000")
        self.assertEqual([resource.uri for resource in info.resources], ["abc000.png", "abc001.png"])
        self.assertEqual([resource.role for resource in info.resources], ["Media Segment", "Media Segment"])
        self.assertEqual([resource.duration for resource in info.resources], [2.0, 2.0])
        self.assertEqual([resource.resolved_uri for resource in info.resources], ["https://cdn.example.com/final/playlist/abc000.png", "https://cdn.example.com/final/playlist/abc001.png"])
        diagnostic = info.to_dict()
        self.assertEqual(diagnostic["protocol"], "HLS")
        self.assertEqual(diagnostic["manifest_type"], "Media")
        self.assertEqual(diagnostic["playlist_type"], "VOD")
        self.assertEqual(diagnostic["hls_playlist_type"], "VOD")
        self.assertEqual(len(diagnostic["resources"]), 2)

    def test_same_vod_with_endlist_keeps_identical_resource_semantics(self):
        original = parse_manifest(USER_VOD, BASE)
        bounded = parse_manifest(USER_VOD + b"#EXT-X-ENDLIST\n", BASE)
        self.assertEqual(original.to_dict(), bounded.to_dict())

    def test_all_segment_suffixes_and_no_suffix_have_same_role(self):
        names = ["video.ts", "video.m4s", "audio.aac", "fragment.mp4", "payload.bin", "payload.dat", "abc000.png", "no-extension", "arbitrary.weird", "playlist.m3u8", "file.JPEG"]
        for key in ("", '#EXT-X-KEY:METHOD=AES-128,URI="enc.key",KEYFORMAT="identity"'):
            with self.subTest(encrypted=bool(key)):
                data = ("#EXTM3U\n" + key + "\n" + "".join("#EXTINF:2.5,\n" + name + "\n" for name in names)).encode()
                info = parse_manifest(data, BASE)
                self.assertEqual([resource.uri for resource in info.resources], names)
                self.assertTrue(all(resource.role == "Media Segment" for resource in info.resources))
                self.assertTrue(all(resource.duration == 2.5 for resource in info.resources))
                self.assertEqual(info.children, [])
                self.assertIs(info.drm, False)

    def test_media_uri_without_extinf_is_still_media_resource(self):
        info = parse_manifest(b"#EXTM3U\nplain-name\nsecond.png\n", BASE)
        self.assertEqual([resource.role for resource in info.resources], ["Media Segment", "Media Segment"])
        self.assertEqual([resource.duration for resource in info.resources], [None, None])

    def test_master_variant_uri_never_becomes_media_segment(self):
        data = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.png\n#EXT-X-STREAM-INF:BANDWIDTH=2\nchild.ts\n#EXT-X-STREAM-INF:BANDWIDTH=3\nno-extension\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.playlist_type, "Master")
        self.assertEqual(len(info.children), 3)
        self.assertEqual(info.resources, [])
        self.assertEqual(info.to_dict()["manifest_type"], "Master")
        self.assertEqual(info.to_dict()["playlist_type"], "Master")

    def test_rendition_iframe_and_image_uris_remain_child_manifests(self):
        data = b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="one",URI="audio.aac"\n#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.bin"\n#EXT-X-IMAGE-STREAM-INF:BANDWIDTH=2,URI="image.png"\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.playlist_type, "Master")
        self.assertEqual(len(info.children), 3)
        self.assertEqual(info.resources, [])

    def test_structure_is_known_before_processing_bare_uris(self):
        data = b'#EXTM3U\nunassociated.png\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.dat\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.resources, [])
        self.assertEqual(info.children, ["https://cdn.example.com/final/playlist/child.dat"])

    def test_keys_remain_keys_even_with_segment_like_filename(self):
        info = parse_manifest(hls('#EXT-X-KEY:METHOD=AES-128,URI="key.png"'), BASE)
        self.assertEqual([key.uri for key in info.keys], ["key.png"])
        self.assertEqual([resource.uri for resource in info.resources], ["segment01.ts"])
        self.assertIs(info.drm, False)

    def test_event_is_explicitly_parsed_without_changing_internal_type(self):
        info = parse_manifest(b"#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:1,\nlive.bin\n", BASE)
        self.assertEqual(info.playlist_type, "Media")
        self.assertEqual(info.hls_playlist_type, "EVENT")
        self.assertEqual(info.to_dict()["playlist_type"], "EVENT")
        self.assertEqual(info.to_dict()["manifest_type"], "Media")

    def test_endlist_alone_does_not_invent_vod(self):
        info = parse_manifest(hls(), BASE)
        self.assertEqual(info.hls_playlist_type, "")
        self.assertEqual(info.to_dict()["playlist_type"], "Media")

    def test_playlist_type_whitespace_and_case_are_normalized(self):
        info = parse_manifest(b"#EXTM3U\n#EXT-X-PLAYLIST-TYPE: vod \n", BASE)
        self.assertEqual(info.hls_playlist_type, "VOD")

    def test_split_key_user_form_parses_all_fields_and_segments(self):
        data = b'#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-KEY:\nMETHOD=AES-128,\nURI="enc.key",\nIV=0x00000000000000000000000000000000\n#EXTINF:2.000000,\nabc000.png\n#EXTINF:2.000000,\nabc001.png\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.keys[0].method, "AES-128")
        self.assertEqual(info.keys[0].uri, "enc.key")
        self.assertEqual(info.keys[0].iv, "0x00000000000000000000000000000000")
        self.assertEqual(info.keys[0].keyformat, "identity")
        self.assertEqual(len(info.resources), 2)
        self.assertIs(info.drm, False)

    def test_split_key_without_physical_commas_is_supported(self):
        data = b'#EXTM3U\n#EXT-X-KEY:\nMETHOD=AES-128\nURI="enc.key"\nIV=0x01\nKEYFORMAT="identity"\nKEYFORMATVERSIONS="1/2"\n#EXTINF:2,\npart.bin\n'
        info = parse_manifest(data, BASE)
        key = info.keys[0]
        self.assertEqual((key.method, key.uri, key.iv, key.keyformat, key.keyformatversions), ("AES-128", "enc.key", "0x01", "identity", "1/2"))
        self.assertEqual(info.resources[0].uri, "part.bin")
        self.assertIs(info.drm, False)

    def test_split_session_key_keeps_master_roles(self):
        data = b'#EXTM3U\n#EXT-X-SESSION-KEY:\nMETHOD=AES-128,\nURI="enc.key"\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.png\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.keys[0].uri, "enc.key")
        self.assertEqual(info.playlist_type, "Master")
        self.assertEqual(info.resources, [])
        self.assertIs(info.drm, False)

    def test_split_key_resource_names_are_not_swallowed(self):
        for name in ("URI=part.png", "IV=clip.ts", "METHOD=part.bin"):
            with self.subTest(name=name):
                data = ('#EXTM3U\n#EXT-X-KEY:\nMETHOD=AES-128\nURI="enc.key"\n' + name + "\n").encode()
                info = parse_manifest(data, BASE)
                self.assertEqual(info.keys[0].uri, "enc.key")
                self.assertEqual(info.resources[0].uri, name)

    def test_map_part_preload_and_full_segment_have_distinct_roles(self):
        data = b'#EXTM3U\n#EXT-X-MAP:URI="init.png",BYTERANGE="100@0"\n#EXT-X-PART:DURATION=0.25,URI="part.dat",BYTERANGE="40@100"\n#EXT-X-PRELOAD-HINT:TYPE=PART,URI="next.bin",BYTERANGE-START=140,BYTERANGE-LENGTH=40\n#EXTINF:2,\nfull.no-suffix\n'
        info = parse_manifest(data, BASE)
        self.assertEqual([resource.role for resource in info.resources], ["Initialization Segment", "Partial Segment", "Preload Segment", "Media Segment"])
        self.assertEqual([resource.duration for resource in info.resources], [None, 0.25, None, 2.0])
        self.assertEqual([resource.byte_range for resource in info.resources], ["100@0", "40@100", "40@140", ""])
        self.assertEqual(info.children, [])

    def test_preload_open_range_and_implicit_zero_start(self):
        data = b'#EXTM3U\n#EXT-X-PRELOAD-HINT:TYPE=PART,URI="first",BYTERANGE-START=120\n#EXT-X-PRELOAD-HINT:TYPE=PART,URI="second",BYTERANGE-LENGTH=50\n#EXT-X-PRELOAD-HINT:TYPE=PART,URI="third"\n'
        info = parse_manifest(data, BASE)
        self.assertEqual([resource.byte_range for resource in info.resources], ["@120", "50@0", ""])

    def test_preload_map_is_not_misrepresented_as_segment(self):
        info = parse_manifest(b'#EXTM3U\n#EXT-X-PRELOAD-HINT:TYPE=MAP,URI="upcoming-init"\n', BASE)
        self.assertEqual(info.resources, [])

    def test_full_byte_ranges_preserve_implicit_offsets_and_duplicates(self):
        data = b'#EXTM3U\n#EXTINF:2,\n#EXT-X-BYTERANGE:100@0\npacked.bin\n#EXTINF:3,\n#EXT-X-BYTERANGE:100\npacked.bin\nplain-name\n'
        info = parse_manifest(data, BASE)
        self.assertEqual([resource.uri for resource in info.resources], ["packed.bin", "packed.bin", "plain-name"])
        self.assertEqual([resource.duration for resource in info.resources], [2.0, 3.0, None])
        self.assertEqual([resource.byte_range for resource in info.resources], ["100@0", "100", ""])

    def test_init_and_parts_do_not_consume_pending_full_segment_metadata(self):
        data = b'#EXTM3U\n#EXT-X-BYTERANGE:200@500\n#EXTINF:2,\n#EXT-X-MAP:URI="init",BYTERANGE="20@0"\n#EXT-X-PART:DURATION=0.5,URI="part",BYTERANGE="50@500"\npacked\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.resources[-1].role, "Media Segment")
        self.assertEqual(info.resources[-1].duration, 2.0)
        self.assertEqual(info.resources[-1].byte_range, "200@500")

    def test_comments_discontinuity_and_key_rotation_preserve_duration(self):
        data = b'#EXTM3U\n#EXTINF:1.25,Chapter one, detail\n# comment\n#EXT-X-DISCONTINUITY\n#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\nfirst.png\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\nsecond.dat\n'
        info = parse_manifest(data, BASE)
        self.assertEqual([resource.duration for resource in info.resources], [1.25, 2.0])
        self.assertEqual(info.classification, "HLS_AES128")
        self.assertIs(info.drm, False)

    def test_relative_and_signed_resource_queries_are_not_rewritten(self):
        names = ["../parts/a.png?auth=%2f+%2B&empty=&t=1#part", "/segments/b.dat?sign=A%3d&x=+", "//other.example/no-extension?token=%25+", "https://absolute.example/part?expires=3"]
        expected = ["https://cdn.example.com/final/parts/a.png?auth=%2f+%2B&empty=&t=1#part", "https://cdn.example.com/segments/b.dat?sign=A%3d&x=+", "https://other.example/no-extension?token=%25+", "https://absolute.example/part?expires=3"]
        info = parse_manifest(("#EXTM3U\n" + "\n".join(names)).encode(), BASE)
        self.assertEqual([resource.uri for resource in info.resources], names)
        self.assertEqual([resource.resolved_uri for resource in info.resources], expected)

    def test_resource_resolution_uses_supplied_final_manifest_url(self):
        info = parse_manifest(b"#EXTM3U\n#EXTINF:2,\nabc000.png?sign=%2f+t\n", "https://redirect.example/new/path/endpoint")
        self.assertEqual(info.resources[0].resolved_uri, "https://redirect.example/new/path/abc000.png?sign=%2f+t")

    def test_rendition_report_remains_manifest_child_of_media_playlist(self):
        data = b'#EXTM3U\n#EXT-X-RENDITION-REPORT:URI="sibling.png",LAST-MSN=1\n#EXTINF:2,\nsegment.png\n'
        info = parse_manifest(data, BASE)
        self.assertEqual(info.playlist_type, "Media")
        self.assertEqual(info.children, ["https://cdn.example.com/final/playlist/sibling.png"])
        self.assertEqual([resource.uri for resource in info.resources], ["segment.png"])

    def test_media_resource_dataclass_defaults(self):
        resource = MediaResource("part.bin", "https://example.com/part.bin")
        self.assertEqual(resource.role, "Media Segment")
        self.assertIsNone(resource.duration)
        self.assertEqual(resource.byte_range, "")

    def test_legacy_manifest_positional_fields_and_independent_new_lists(self):
        info = ManifestInfo("HLS", "Media", "AES-128", "HLS_AES128", False, [KeyInfo("AES-128")], ["child"], ["evidence"])
        other = ManifestInfo("HLS", "Media")
        self.assertEqual(info.evidence, ["evidence"])
        self.assertEqual(info.hls_playlist_type, "")
        info.resources.append(MediaResource("part", "https://example.com/part"))
        self.assertEqual(other.resources, [])

    def test_serialized_resources_are_copies_and_internal_type_stays_media(self):
        info = parse_manifest(USER_VOD, BASE)
        serialized = info.to_dict()
        serialized["resources"][0]["uri"] = "changed"
        serialized["resources"].clear()
        self.assertEqual(len(info.resources), 2)
        self.assertEqual(info.resources[0].uri, "abc000.png")
        self.assertEqual(info.playlist_type, "Media")

    def test_dash_and_direct_serialization_remain_compatible(self):
        dash = parse_manifest(b"<MPD/>", BASE)
        direct = parse_manifest(b"binary data", BASE, "video/mp4")
        self.assertEqual(dash.to_dict()["protocol"], "DASH")
        self.assertEqual(dash.to_dict()["manifest_type"], "MPD")
        self.assertEqual(dash.to_dict()["playlist_type"], "MPD")
        self.assertEqual(dash.resources, [])
        self.assertEqual(direct.to_dict()["protocol"], "Direct")
        self.assertEqual(direct.resources, [])

    def test_bad_resource_attributes_are_parse_errors_not_drm(self):
        bad_tags = [
            '#EXT-X-MAP:BYTERANGE="50@0"',
            '#EXT-X-PART:URI="part.bin"',
            '#EXT-X-PART:DURATION=1',
            '#EXT-X-PRELOAD-HINT:TYPE=PART',
            '#EXT-X-MAP:URI="https://[broken/init"',
            '#EXT-X-MAP:URI="init",BYTERANGE="0@0"',
            '#EXT-X-PART:URI="part",DURATION=1,BYTERANGE="bad"',
            '#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part",BYTERANGE-START=-1',
            '#EXT-X-PRELOAD-HINT:TYPE=PART,URI="part",BYTERANGE-LENGTH=0',
        ]
        for tag in bad_tags:
            with self.subTest(tag=tag), self.assertRaises(MediaCheckError) as caught:
                parse_manifest(("#EXTM3U\n" + tag + "\n").encode(), BASE)
            self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")
            self.assertNotIsInstance(caught.exception, ProtectedMediaError)

    def test_invalid_durations_and_dangling_metadata_are_typed_errors(self):
        for lines in ("#EXTINF:NaN,\npart", "#EXTINF:inf,\npart", "#EXTINF:-1,\npart", "#EXTINF:0,\npart", "#EXTINF:1e3,\npart", "#EXTINF:2,", "#EXTINF:2,\n#EXTINF:3,\npart", "#EXT-X-BYTERANGE:50@0", "#EXT-X-BYTERANGE:\npart", "#EXT-X-BYTERANGE:50@-1\npart"):
            with self.subTest(lines=lines), self.assertRaises(MediaCheckError) as caught:
                parse_manifest(("#EXTM3U\n" + lines).encode(), BASE)
            self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")

    def test_invalid_or_duplicate_playlist_types_are_parse_errors(self):
        for lines in ("#EXT-X-PLAYLIST-TYPE:LIVE", "#EXT-X-PLAYLIST-TYPE:", "#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-PLAYLIST-TYPE:EVENT"):
            with self.subTest(lines=lines), self.assertRaises(MediaCheckError) as caught:
                parse_manifest(("#EXTM3U\n" + lines).encode(), BASE)
            self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")

    def test_resource_uri_control_or_invalid_host_is_typed_error(self):
        for uri in ("part\tname.png", "https://[broken/part.png"):
            with self.subTest(uri=uri), self.assertRaises(MediaCheckError) as caught:
                parse_manifest(("#EXTM3U\n#EXTINF:2,\n" + uri).encode(), BASE)
            self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")


class DashTests(unittest.TestCase):
    def test_clear_mpd(self):
        info = parse_manifest(b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet /></Period></MPD>', "https://example.com/media")
        self.assertEqual(info.media_type, "DASH")
        self.assertEqual(info.playlist_type, "MPD")
        self.assertEqual(info.classification, "CLEAR_DASH")
        self.assertIs(info.drm, False)

    def test_namespace_prefixed_root(self):
        data = b'<?xml version="1.0"?><d:MPD xmlns:d="urn:mpeg:dash:schema:mpd:2011"><d:Period/></d:MPD>'
        self.assertEqual(parse_manifest(data, "https://example.com/file.mp4", "video/mp4").media_type, "DASH")

    def test_utf16_mpd(self):
        data = '<?xml version="1.0" encoding="UTF-16"?><MPD><Period /></MPD>'.encode("utf-16")
        self.assertEqual(parse_manifest(data, BASE, "application/dash+xml").media_type, "DASH")

    def test_widevine_contentprotection(self):
        data = ('<MPD><Period><ContentProtection schemeIdUri="urn:uuid:' + WIDEVINE + '" /></Period></MPD>').encode()
        info = parse_manifest(data, BASE)
        self.assertEqual(info.classification, "DRM_WIDEVINE")
        self.assertIs(info.drm, True)

    def test_playready_contentprotection(self):
        data = ('<MPD><Period><ContentProtection schemeIdUri="urn:uuid:' + PLAYREADY.upper() + '" /></Period></MPD>').encode()
        self.assertEqual(parse_manifest(data, BASE).classification, "DRM_PLAYREADY")

    def test_fairplay_contentprotection(self):
        data = ('<MPD><ContentProtection schemeIdUri="urn:uuid:' + FAIRPLAY + '" /></MPD>').encode()
        self.assertEqual(parse_manifest(data, BASE).classification, "DRM_FAIRPLAY")

    def test_cenc_contentprotection_is_protection_evidence(self):
        info = parse_manifest(b'<MPD><ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc" /></MPD>', BASE)
        self.assertEqual(info.classification, "DRM_UNKNOWN")
        self.assertEqual(info.encryption, "CENC")
        self.assertIs(info.drm, True)

    def test_unknown_uuid_and_unknown_contentprotection(self):
        for scheme in ("urn:uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "vendor.drm.system", ""):
            with self.subTest(scheme=scheme):
                info = parse_manifest(('<MPD><ContentProtection schemeIdUri="' + scheme + '" /></MPD>').encode(), BASE)
                self.assertEqual(info.classification, "DRM_UNKNOWN")
                self.assertIs(info.drm, True)

    def test_pssh_v0_and_v1_known_system(self):
        for version in (0, 1):
            encoded = base64.b64encode(pssh(WIDEVINE, version)).decode()
            data = ('<MPD xmlns:c="urn:mpeg:cenc:2013"><ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"><c:pssh>' + encoded + '</c:pssh></ContentProtection></MPD>').encode()
            with self.subTest(version=version):
                info = parse_manifest(data, BASE)
                self.assertEqual(info.classification, "DRM_WIDEVINE")
                self.assertEqual(len(info.keys), 1)

    def test_standalone_pssh_is_recognized(self):
        encoded = base64.b64encode(pssh(PLAYREADY)).decode()
        info = parse_manifest(('<MPD xmlns:c="urn:mpeg:cenc:2013"><c:pssh>' + encoded + '</c:pssh></MPD>').encode(), BASE)
        self.assertEqual(info.classification, "DRM_PLAYREADY")

    def test_pssh_invalid_base64_does_not_silently_become_clear(self):
        info = parse_manifest(b'<MPD xmlns:c="urn:mpeg:cenc:2013"><c:pssh>invalid!</c:pssh></MPD>', BASE)
        self.assertIs(info.drm, True)
        self.assertEqual(info.classification, "DRM_UNKNOWN")

    def test_playready_pro_namespace(self):
        info = parse_manifest(b'<MPD xmlns:p="urn:microsoft:playready"><ContentProtection><p:pro>metadata</p:pro></ContentProtection></MPD>', BASE)
        self.assertEqual(info.classification, "DRM_PLAYREADY")

    def test_multiple_drm_preserves_each_descriptor(self):
        data = ('<MPD><ContentProtection schemeIdUri="urn:uuid:' + WIDEVINE + '"/><ContentProtection schemeIdUri="urn:uuid:' + PLAYREADY + '"/></MPD>').encode()
        info = parse_manifest(data, BASE)
        self.assertEqual([key.classification for key in info.keys], ["DRM_WIDEVINE", "DRM_PLAYREADY"])
        self.assertIs(info.drm, True)

    def test_unused_cenc_namespace_is_not_drm(self):
        info = parse_manifest(b'<MPD xmlns:cenc="urn:mpeg:cenc:2013"><Period /></MPD>', BASE)
        self.assertIs(info.drm, False)

    def test_default_kid_is_explicit_cenc_evidence(self):
        info = parse_manifest(b'<MPD xmlns:cenc="urn:mpeg:cenc:2013"><AdaptationSet cenc:default_KID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" /></MPD>', BASE)
        self.assertIs(info.drm, True)

    def test_xml_invalid_is_media_parse_error_not_protected(self):
        for data in (b'<MPD><Period></MPD>', b'<MPD', b'<html><body>403 Forbidden</body></html>', b'<MPDX/>'):
            with self.subTest(data=data), self.assertRaises(MediaCheckError) as caught:
                parse_manifest(data, BASE, "application/dash+xml")
            self.assertEqual(caught.exception.code, "MANIFEST_PARSE_ERROR")
            self.assertNotIsInstance(caught.exception, ProtectedMediaError)

    def test_entities_rejected_without_network_access(self):
        data = b'<!DOCTYPE MPD [<!ENTITY secret SYSTEM "file:///secret">]><MPD>&secret;</MPD>'
        with self.assertRaises(MediaCheckError):
            parse_manifest(data, BASE)


class DirectAndContentTests(unittest.TestCase):
    def test_real_mp4_magic_without_extension(self):
        info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0isom") + box(b"moov"), "https://example.com/download")
        self.assertEqual(info.media_type, "Direct")
        self.assertIs(info.drm, False)

    def test_direct_actual_content_type(self):
        info = parse_manifest(b"binary media payload", "https://example.com/download", "video/mp4; charset=binary")
        self.assertEqual(info.media_type, "Direct")
        self.assertIs(info.drm, False)

    def test_no_moov_not_assumed_protected(self):
        info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0isom") + box(b"mdat", b"some data"), BASE, "application/octet-stream")
        self.assertIs(info.drm, False)
        self.assertTrue(any("未包含 moov" in entry for entry in info.evidence))

    def test_mp4_structural_pssh_known_drm(self):
        for system, classification in ((WIDEVINE, "DRM_WIDEVINE"), (PLAYREADY, "DRM_PLAYREADY"), (FAIRPLAY, "DRM_FAIRPLAY")):
            with self.subTest(system=system):
                info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0") + box(b"moov", pssh(system)), BASE)
                self.assertIs(info.drm, True)
                self.assertEqual(info.classification, classification)

    def test_pssh_text_inside_mdat_is_not_drm(self):
        info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0") + box(b"mdat", b"pssh sinf encv enca " + pssh(WIDEVINE)), BASE)
        self.assertIs(info.drm, False)

    def test_sinf_structure_is_protection_evidence(self):
        payload = box(b"moov", box(b"trak", box(b"sinf", box(b"schm", b"\0" * 4 + b"cbcs"))))
        info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0") + payload, BASE)
        self.assertIs(info.drm, True)
        self.assertEqual(info.classification, "DRM_UNKNOWN")

    def test_encrypted_sample_entries(self):
        for sample_type in (b"encv", b"enca"):
            stsd = box(b"stsd", b"\0" * 4 + struct.pack(">I", 1) + box(sample_type, b"\0" * 78))
            payload = box(b"moov", box(b"trak", box(b"mdia", box(b"minf", box(b"stbl", stsd)))))
            with self.subTest(sample_type=sample_type):
                info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0") + payload, BASE)
                self.assertIs(info.drm, True)

    def test_extended_bmff_box_size(self):
        info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0", extended=True) + box(b"moov", pssh(WIDEVINE), extended=True), BASE)
        self.assertEqual(info.classification, "DRM_WIDEVINE")

    def test_truncated_direct_prefix_does_not_assume_drm(self):
        info = parse_manifest(box(b"ftyp", b"isom\0\0\0\0") + struct.pack(">I4s", 50000, b"mdat") + b"partial", BASE)
        self.assertIs(info.drm, False)

    def test_media_magic_webm_ogg_flv_and_ts(self):
        for data in (b"\x1aE\xdf\xa3rest", b"OggSpayload", b"FLVpayload", bytes([0x47]) + bytes(187) + bytes([0x47]) + bytes(187)):
            with self.subTest(data=data[:8]):
                self.assertEqual(parse_manifest(data, "https://example.com/no-extension").media_type, "Direct")

    def test_suffix_does_not_allow_unknown_content(self):
        for url in ("https://example.com/index.m3u8", "https://example.com/manifest.mpd", "https://example.com/video.mp4"):
            with self.subTest(url=url), self.assertRaises(MediaCheckError):
                parse_manifest(b"not media content", url, "text/plain")

    def test_html_rejected_even_with_mp4_mime(self):
        with self.assertRaises(MediaCheckError) as caught:
            parse_manifest(b"<html><body>Sign in</body></html>", BASE, "video/mp4")
        self.assertNotIsInstance(caught.exception, ProtectedMediaError)

    def test_hls_mime_without_hls_content_is_invalid(self):
        with self.assertRaises(MediaCheckError):
            parse_manifest(b"error response", BASE, "application/vnd.apple.mpegurl")

    def test_empty_bad_type_and_invalid_utf8(self):
        for data in (b"", None, "#EXTM3U", b"#EXTM3U\n\xff\xff"):
            with self.subTest(data=data), self.assertRaises(MediaCheckError):
                parse_manifest(data, BASE)

    def test_dataclass_serialization_and_independent_defaults(self):
        one, two = ManifestInfo("HLS"), ManifestInfo("HLS")
        one.keys.append(KeyInfo("AES-128"))
        self.assertEqual(two.keys, [])
        self.assertEqual(one.to_dict()["keys"][0]["keyformat"], "identity")

    def test_http_403_text_is_not_reported_as_drm(self):
        with self.assertRaises(MediaCheckError) as caught:
            parse_manifest(b"403 Forbidden", BASE)
        self.assertNotIsInstance(caught.exception, ProtectedMediaError)
        self.assertNotEqual(caught.exception.code, "PROTECTED_MEDIA")


if __name__ == "__main__":
    unittest.main()
