"""DASH 描述、HTTP Range 与候选生命周期回归；不访问第三方网站。"""
from dataclasses import replace
import json
import struct
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from core.discovery_models import NetworkObservation
from core.media_discovery import MediaDiscoveryService
from core.media_guard import check_media
from core.request_context import RequestContext


def box(kind, payload=b''):
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


MP4 = box(b'ftyp', b'isom\0\0\0\0') + box(b'moov') + box(b'mdat', b'video')
INIT = box(b'ftyp', b'isom\0\0\0\0') + box(b'moov')


def dash_json(videos=2, audios=2, padding=0):
    def track(index, role):
        video = role == 'video'
        result = {'id': 80 + index if video else 30216 + index,
            'baseUrl': f'https://cdn.example/{role}-{index}.m4s?sign=a%2Fb+t&expires=9999999999',
            'backupUrl': [f'https://backup.example/{role}-{index}.m4s?token=backup'],
            'bandwidth': 800000 * (index + 1) if video else 64000 * (index + 1),
            'codecs': 'avc1.640028' if video else 'mp4a.40.2',
            'mimeType': 'video/mp4' if video else 'audio/mp4',
            'SegmentBase': {'Initialization': '0-999', 'indexRange': '1000-1299'}}
        if video:
            result.update(width=1280 if index == 0 else 1920,
                          height=720 if index == 0 else 1080, frameRate='30000/1001')
        return result
    return json.dumps({'data': {'timelength': 97000, 'dash': {'duration': 97,
        'video': [track(i, 'video') for i in range(videos)],
        'audio': [track(i, 'audio') for i in range(audios)]}}, 'padding': 'x' * padding}).encode()


class DashDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.service = MediaDiscoveryService()
        self.context = RequestContext('https://api.example/x/player/playurl?cid=123&sign=keep',
            headers={'referer': 'https://page.example/watch', 'user-agent': 'Observed Browser',
                     'origin': 'https://page.example', 'cookie': 'session=private',
                     'authorization': 'Bearer private', 'range': 'bytes=0-'})

    def describe(self, body=None, request_id='api', **overrides):
        observation = NetworkObservation(request_id, self.context, 200, 'application/json',
            body if body is not None else dash_json(), frame_id='main', resource_type='XHR',
            body_complete=True, context_complete=True)
        self.service.consume_network(replace(observation, **overrides))
        return self.service.ranked(include_low=True, limit=128)

    def media(self, url, *, status=206, body=INIT, complete=True, length=None, headers=None):
        observation = NetworkObservation('range:' + str(self.service._sequence),
            self.context.derive(url), status, 'video/mp4', body, frame_id='main',
            content_length=length, body_complete=complete, response_headers=headers or {})
        self.service.consume_network(observation)
        return self.service.get(self.service._urls.get(url))

    def test_description_creates_paired_variants_with_exact_signed_urls(self):
        candidates = self.describe()
        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertEqual(candidate.media_type, 'DASH')
            self.assertTrue(candidate.downloadable)
            self.assertTrue(candidate.validated)
            self.assertIs(candidate.drm, False)
            self.assertEqual(candidate.audio_track.id, '30217')
            self.assertIn('sign=a%2Fb+t&expires=9999999999', candidate.video_url)
            self.assertIn('sign=a%2Fb+t&expires=9999999999', candidate.audio_url)
            self.assertNotIn('range', candidate.request_context.headers)
            for name in ('referer', 'user-agent', 'origin', 'cookie', 'authorization'):
                self.assertEqual(candidate.request_context.headers[name], self.context.headers[name])

    def test_embedded_mpd_contains_only_the_selected_video_and_audio(self):
        for candidate in self.describe():
            root = ET.fromstring(candidate.manifest_text)
            reps = root.findall('.//{*}Representation')
            self.assertEqual(len(reps), 2)
            self.assertEqual({r.find('{*}BaseURL').text for r in reps},
                             {candidate.video_url, candidate.audio_url})
            for rep in reps:
                self.assertEqual(rep.find('{*}SegmentBase').get('indexRange'), '1000-1299')
                self.assertEqual(rep.find('.//{*}Initialization').get('range'), '0-999')
            with patch('core.media_guard.read_media', side_effect=AssertionError('must not refetch API')):
                inspection = check_media(candidate.request_context, **{
                    'manifest_text': candidate.manifest_text, 'manifest_url': candidate.manifest_url,
                    'manifest_origin': candidate.manifest_origin, 'expected_media_type': 'DASH'})
            self.assertTrue(inspection.root.use_cached_manifest)
            self.assertEqual(inspection.root.info.media_type, 'DASH')
            self.assertEqual(candidate.download_payload()['manifest_text'], candidate.manifest_text)

    def test_large_json_over_old_two_mib_limit_reaches_parser(self):
        self.assertEqual(len(self.describe(dash_json(padding=3 * 1024 * 1024))), 2)
        self.service = MediaDiscoveryService(max_body_bytes=1024 * 1024)
        self.assertEqual(self.describe(dash_json(padding=3 * 1024 * 1024)), [])

    def test_unfinished_json_is_not_promoted(self):
        self.assertEqual(self.describe(body_complete=False), [])

    def test_api_counts_are_not_duplicated_by_body_extra_info_updates(self):
        self.describe()
        self.describe(request_id='api-later')
        values = self.service.discovery_diagnostic()['statistics']
        self.assertEqual(values['dash_descriptions'], 1)
        self.assertEqual(values['dash_video_tracks'], 2)
        self.assertEqual(values['dash_audio_tracks'], 2)

    def test_json_upgrades_existing_segment_with_same_identity(self):
        url = json.loads(dash_json())['data']['dash']['video'][0]['baseUrl']
        first = self.media(url)
        self.assertEqual(first.media_type, 'Segment')
        identifier = first.id
        self.describe()
        self.assertEqual(self.service.get(identifier).media_type, 'DASH')
        self.assertTrue(self.service.get(identifier).downloadable)

    def test_late_track_ranges_and_http_failure_do_not_downgrade_dash(self):
        candidates = self.describe()
        before = {c.id: (c.media_type, c.video_url, c.audio_url, c.manifest_text) for c in candidates}
        for candidate in candidates:
            for url in (candidate.video_url, candidate.audio_url, *candidate.video_track.backup_urls):
                self.media(url)
                self.media(url, status=403, body=None, complete=False)
        after = self.service.ranked(include_low=True, limit=128)
        self.assertEqual(len(after), len(before))
        for candidate in after:
            self.assertEqual((candidate.media_type, candidate.video_url, candidate.audio_url, candidate.manifest_text), before[candidate.id])
            self.assertTrue(candidate.downloadable)

    def test_late_bare_track_probe_cannot_replace_paired_manifest(self):
        candidate = self.describe()[0]
        self.service.apply_probe(candidate.id, {'media_type': 'Direct', 'http_status': 403,
            'error_code': 'HTTP_403', 'probe_error': 'late track probe'})
        current = self.service.get(candidate.id)
        self.assertEqual(current.media_type, 'DASH')
        self.assertTrue(current.downloadable)
        self.service.apply_probe(candidate.id, {'drm': True, 'classification': 'DRM_WIDEVINE'})
        self.assertFalse(current.downloadable)
        self.assertTrue(current.drm)

    def test_range_initialization_with_complete_moov_stays_segment(self):
        candidate = self.media('https://cdn.example/init.mp4', length=len(INIT),
            headers={'Content-Range': f'bytes 0-{len(INIT)-1}/9000000'})
        self.assertEqual(candidate.media_type, 'Segment')
        self.assertFalse(candidate.downloadable)
        self.assertIsNone(candidate.drm)

    def test_owned_track_pssh_blocks_pair_and_survives_later_clear_json(self):
        candidate = self.describe()[0]
        pssh = box(b'pssh', b'\0\0\0\0' + bytes.fromhex('edef8ba979d64acea3c827dcd51d21ed') + b'\0\0\0\0')
        body = box(b'ftyp', b'isom\0\0\0\0') + box(b'moov', pssh)
        self.media(candidate.video_url, body=body, length=len(body))
        current = self.service.get(candidate.id)
        self.assertTrue(current.drm)
        self.assertFalse(current.downloadable)
        self.assertEqual(current.classification, 'DRM_WIDEVINE')
        self.describe(request_id='clear-later')
        self.assertTrue(current.drm)
        self.assertEqual(current.classification, 'DRM_WIDEVINE')
        self.assertFalse(current.downloadable)

    def test_full_non_segment_206_requires_matching_resource_lengths(self):
        candidate = self.media('https://cdn.example/whole.mp4', body=MP4, length=len(MP4),
            headers={'Content-Range': f'bytes 0-{len(MP4)-1}/{len(MP4)}'})
        self.assertEqual(candidate.media_type, 'Direct')
        self.assertTrue(candidate.downloadable)
        for suffix, length, range_value in (('missing', None, f'bytes 0-{len(MP4)-1}/{len(MP4)}'),
            ('short', len(MP4), f'bytes 0-{len(MP4)-1}/{len(MP4)+100}'),
            ('wrong', len(MP4)+1, f'bytes 0-{len(MP4)-1}/{len(MP4)}')):
            candidate = self.media(f'https://cdn.example/{suffix}.mp4', body=MP4, length=length,
                headers={'Content-Range': range_value})
            self.assertEqual(candidate.media_type, 'Segment')

    def test_206_segment_mode_never_promotes_direct_even_if_range_is_whole(self):
        candidate = self.media('https://cdn.example/piece.m4s', body=MP4, length=len(MP4),
            headers={'Content-Range': f'bytes 0-{len(MP4)-1}/{len(MP4)}'})
        self.assertEqual(candidate.media_type, 'Segment')
        self.assertFalse(candidate.downloadable)

    def test_direct_player_cannot_promote_a_partial_initialization_range(self):
        url = 'https://cdn.example/opaque'
        self.media(url, body=INIT, length=len(INIT),
            headers={'Content-Range': f'bytes 0-{len(INIT)-1}/10000000',
                     'Content-Length': str(len(INIT))})
        self.service.consume_dom({'frame_id': 'main', 'page_url': 'https://page.example/watch',
            'videos': [{'src': url, 'current_src': url, 'ready_state': 4, 'duration': 60,
                        'width': 1920, 'height': 1080, 'visible': True, 'paused': False}]})
        candidate = self.service.get(self.service._urls[url])
        self.assertEqual(candidate.media_type, 'Segment')
        self.assertFalse(candidate.downloadable)

    def test_resolver_transport_retains_typed_dash_pair(self):
        resolved = self.describe()[0]
        service = MediaDiscoveryService()
        service.consume_resolver(resolved)
        candidate = service.ranked(include_low=True)[0]
        self.assertEqual(candidate.video_track, resolved.video_track)
        self.assertEqual(candidate.audio_track, resolved.audio_track)
        self.assertTrue(candidate.downloadable)

    def test_complete_200_direct_survives_later_partial_range(self):
        url = 'https://cdn.example/whole.m4s'
        first = self.media(url, status=200, body=MP4, length=len(MP4))
        self.assertEqual(first.media_type, 'Direct')
        second = self.media(url, body=INIT, length=len(INIT),
            headers={'Content-Range': f'bytes 0-{len(INIT)-1}/{len(MP4)}'})
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.media_type, 'Direct')
        self.assertTrue(second.downloadable)

    def test_multiple_qualities_share_player_but_keep_declared_dimensions(self):
        self.describe()
        self.service.consume_dom({'frame_id': 'main', 'page_url': 'https://page.example/watch',
            'videos': [{'tag': 'VIDEO', 'src': 'blob:https://page.example/player',
                'current_src': 'blob:https://page.example/player', 'ready_state': 4,
                'width': 1920, 'height': 1080, 'duration': 97, 'visible': True,
                'area': 1920 * 1080, 'paused': False, 'is_main': True}]})
        candidates = self.service.ranked(include_low=True, limit=128)
        self.assertEqual({c.resolution for c in candidates}, {(1280, 720), (1920, 1080)})
        self.assertTrue(all(c.confidence >= 80 for c in candidates))

    def test_diagnostics_do_not_expose_track_signatures_or_session_secrets(self):
        candidate = self.describe()[0]
        safe = json.dumps(candidate.safe_diagnostic())
        for secret in ('session=private', 'Bearer private', 'a%2Fb+t', 'token=backup'):
            self.assertNotIn(secret, safe)

    def test_clear_preserves_configured_capture_limit(self):
        service = MediaDiscoveryService(max_body_bytes=32 * 1024 * 1024)
        service.clear()
        self.assertEqual(service.max_body_bytes, 32 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
