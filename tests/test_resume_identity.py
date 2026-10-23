from dataclasses import replace
import unittest

from core.errors import MediaCheckError
from core.media_guard import MediaInspection, MediaSnapshot
from core.manifests import parse_manifest
from core.request_context import RequestContext
from core.resume_identity import capture_identity, validate_resume


def inspection(url='https://cdn.test/clip/index?token=old', segment='segment.png', duration=2, seq=0):
    data = f'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:{seq}\n#EXTINF:{duration},\n{segment}\n#EXT-X-ENDLIST\n'.encode()
    context = RequestContext(url)
    snap = MediaSnapshot(context, data, 'application/vnd.apple.mpegurl', parse_manifest(data, url))
    return MediaInspection(snap, {url: snap})


class ResumeIdentityTests(unittest.TestCase):
    def test_new_signature_matches_same_complete_layout(self):
        old = capture_identity(inspection())
        new = capture_identity(inspection('https://cdn.test/clip/index?token=new'))
        self.assertTrue(validate_resume(old, new))

    def test_different_clip_rejected(self):
        with self.assertRaises(MediaCheckError) as caught:
            validate_resume(capture_identity(inspection()), capture_identity(inspection('https://cdn.test/other/index?token=new')))
        self.assertEqual(caught.exception.code, 'RESUME_MISMATCH')

    def test_changed_fragment_layout_rejected(self):
        for fresh in (inspection(segment='other.png'), inspection(duration=3), inspection(seq=1)):
            with self.assertRaises(MediaCheckError):
                validate_resume(capture_identity(inspection()), capture_identity(fresh))

    def test_unsigned_asset_query_not_discarded(self):
        with self.assertRaises(MediaCheckError):
            validate_resume(capture_identity(inspection('https://cdn.test/index?video=1&token=x')),
                            capture_identity(inspection('https://cdn.test/index?video=2&token=y')))

    def test_resolver_can_refresh_cdn_origin_but_not_video_id(self):
        metadata = {'resolver_id': 'rou.video', 'video_id': 'one'}
        old = capture_identity(inspection(), metadata)
        new = capture_identity(inspection('https://fresh-cdn.test/clip/index?token=new'), metadata)
        self.assertTrue(validate_resume(old, new))
        with self.assertRaises(MediaCheckError):
            validate_resume(old, capture_identity(inspection(), dict(metadata, video_id='two')))

    def test_raw_query_not_present_in_proof(self):
        self.assertNotIn('token=old', repr(capture_identity(inspection())))

    def test_verified_api_identity_allows_new_cdn_and_random_directory(self):
        api = 'https://rou.video/api/hls/same-video'
        metadata = {'resolver_id':'rou.video', 'video_id':'same-video', 'resolver_verified':True,
                    'resolver_route':'api_redirect', 'original_api_url':api}
        first = inspection('https://first.example/hls/same-video/old-random/index.png?v=6&auth=old')
        second = inspection('https://second.example/elsewhere/new-random/index.png?v=6&auth=new')
        first.root.context = replace(first.root.context, original_url=api)
        second.root.context = replace(second.root.context, original_url=api)
        self.assertTrue(validate_resume(capture_identity(first,metadata),capture_identity(second,metadata)))

    def test_verified_route_still_rejects_resource_or_media_version_change(self):
        api = 'https://rou.video/api/hls/same-video'
        metadata = {'resolver_id':'rou.video', 'video_id':'same-video', 'resolver_verified':True,
                    'resolver_route':'api_redirect', 'original_api_url':api}
        old = inspection('https://first.example/old/index.png?v=6&auth=old')
        old.root.context = replace(old.root.context, original_url=api)
        for new in (inspection('https://second.example/new/index.png?v=7&auth=new'),
                    inspection('https://second.example/new/index.png?v=6&auth=new', segment='different.png')):
            new.root.context = replace(new.root.context, original_url=api)
            with self.assertRaises(MediaCheckError):
                validate_resume(capture_identity(old,metadata),capture_identity(new,metadata))
