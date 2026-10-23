from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from core.discovery_models import MediaCandidate, NetworkObservation
from core.media_chain_diagnostics import MediaChainTrace, safe_url, summarize_body, MAX_BODY
from core.request_context import RequestContext


class MediaChainDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.trace = MediaChainTrace('B', 'https://example.test/video/1?tracking=private', 'Segment only')

    def request(self, url='https://example.test/x/player/playurl?sign=private', request_id='1', session='', kind='Fetch'):
        message = {'method': 'Network.requestWillBeSent', 'params': {'requestId': request_id,
            'type': kind, 'frameId': 'frame', 'timestamp': 12, 'wallTime': 123,
            'request': {'url': url, 'method': 'GET', 'headers': {'Cookie': 'secret-cookie'}},
            'initiator': {'type': 'script', 'stack': {'callFrames': [
                {'url': 'https://example.test/player.js?token=secret', 'lineNumber': 2}]}}}}
        if session:
            message['sessionId'] = session
        self.trace.record_cdp(message)
        return url

    def response(self, request_id='1', session='', mime='application/json', size='100'):
        message = {'method': 'Network.responseReceived', 'params': {'requestId': request_id,
            'type': 'Fetch', 'response': {'status': 200, 'mimeType': mime,
                'headers': {'Content-Type': mime, 'Content-Length': size, 'Set-Cookie': 'secret-cookie'}}}}
        if session:
            message['sessionId'] = session
        self.trace.record_cdp(message)

    def test_urls_redact_query_credentials_and_fragment(self):
        value = safe_url('https://user:password@example.test/p?sign=secret&expires=123#credential')
        self.assertEqual(value, 'https://example.test/p?sign=********&expires=********')

    def test_request_and_response_fields_no_mutation_or_secret_storage(self):
        url = self.request()
        self.response()
        self.trace.record_cdp({'method': 'Network.dataReceived', 'params': {'requestId': '1', 'dataLength': 150}})
        self.trace.record_cdp({'method': 'Network.loadingFinished', 'params': {'requestId': '1', 'encodedDataLength': 80}})
        row = self.trace.snapshot()['requests'][0]
        self.assertEqual((row['method'], row['content_type'], row['content_length'],
                          row['decoded_data_bytes'], row['encoded_transfer_bytes']), ('GET', 'application/json', 100, 150, 80))
        self.assertEqual(row['focus_matches'], ['playurl', 'x/player'])
        self.assertNotIn('secret', json.dumps(row))
        self.assertNotIn('private', json.dumps(row))

    def test_observer_first_qt_delivery_does_not_duplicate_request(self):
        url = 'https://example.test/1.mp4'
        observation = NetworkObservation('1', RequestContext(url), resource_type='Media')
        self.trace.record_observation(observation)
        self.request(url, kind='Media')
        snapshot = self.trace.snapshot()
        self.assertEqual(snapshot['counts']['requests'], 1)
        self.assertTrue(snapshot['requests'][0]['observed_by_discovery'])
        self.assertEqual(snapshot['unmatched_observer_snapshots'], 0)

    def test_sessions_with_same_request_id_remain_distinct(self):
        self.request(session='worker1')
        self.request(session='worker2')
        self.response(session='worker1', size='10')
        self.response(session='worker2', size='20')
        rows = self.trace.snapshot()['requests']
        self.assertEqual([r['content_length'] for r in rows], [10, 20])
        self.assertEqual([r['request_id'] for r in rows], ['worker1:1', 'worker2:1'])

    def test_redirect_hops_are_retained(self):
        self.request('https://example.test/begin')
        self.trace.record_cdp({'method': 'Network.requestWillBeSent', 'params': {'requestId': '1',
            'type': 'Fetch', 'request': {'url': 'https://cdn.test/master', 'method': 'GET', 'headers': {}},
            'redirectResponse': {'status': 302, 'headers': {'Content-Type': 'text/plain'}}}})
        rows = self.trace.snapshot()['requests']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['status'], 302)
        self.assertEqual(rows[0]['redirect_to'], 'https://cdn.test/master')

    def test_json_dash_is_description_not_mpd(self):
        body = json.dumps({'data': {'dash': {'video': [{'baseUrl': 'https://cdn.test/video.m4s?sign=secret',
            'SegmentBase': {'Initialization': '0-1000', 'indexRange': '1001-2000'}}],
            'audio': [{'base_url': 'https://cdn.test/audio.m4s'}]}, 'token': 'private-account-value'}}).encode()
        summary = summarize_body(body)
        self.assertEqual(summary['kind'], 'JSON_DASH')
        self.assertEqual(summary['track_counts'], {'video': 1, 'audio': 1})
        self.assertIn('segmentbase', summary['fields'])
        self.assertEqual(len(summary['references']), 2)
        self.assertNotIn('private-account-value', json.dumps(summary))
        self.assertNotIn('sign=secret', json.dumps(summary))

    def test_hls_and_xml_mpd_are_distinct(self):
        self.assertEqual(summarize_body(b'#EXTM3U\n#EXTINF:2,\na.png')['kind'], 'HLS')
        self.assertEqual(summarize_body(b'<?xml version="1.0"?>\n<MPD><Representation /></MPD>')['kind'], 'DASH_XML')
        self.assertEqual(summarize_body(b'<dash:MPD xmlns:dash="x"/>')['kind'], 'DASH_XML')

    def test_json_durl_and_explicit_manifest_reference(self):
        self.assertEqual(summarize_body(b'{"durl":[{"url":"https://cdn.test/v.flv"}]}')['kind'], 'JSON_DURL')
        self.assertEqual(summarize_body(b'{"m3u8_url":"https://cdn.test/v"}')['kind'], 'JSON_MEDIA_DESCRIPTION')

    def test_body_limits_and_json_recursion_are_bounded(self):
        self.assertEqual(summarize_body(b'a' * (MAX_BODY + 1))['kind'], 'BODY_NOT_AVAILABLE')
        summary = summarize_body(b'[' * 2000 + b']' * 2000)
        self.assertIn(summary['kind'], ('OTHER', 'JSON_OTHER'))
        if summary['kind'] == 'JSON_OTHER':
            self.assertTrue(summary['summary_truncated'])

    def test_diagnostic_read_does_not_mark_original_observer_as_read(self):
        self.request()
        self.trace.diagnostic_body('', '1', {'body': '{"dash":{"video":[]}}'}, True)
        row = self.trace.snapshot()['requests'][0]
        self.assertFalse(row['body_seen_by_discovery'])
        self.assertEqual(row['body_summaries']['diagnostic_read']['kind'], 'JSON_DASH')
        self.assertEqual(row['diagnostic_body_status'], 'READ_ONLY_NOT_SUBMITTED_TO_DISCOVERY')

    def test_existing_observer_json_and_segment_candidate_are_correlated(self):
        api = self.request()
        segment = 'https://cdn.test/video.m4s?sign=private'
        body = json.dumps({'dash': {'video': [{'baseUrl': segment}]}}).encode()
        observation = NetworkObservation('1', RequestContext(api), status=200, content_type='application/json',
                                         body=body, body_complete=True, resource_type='Fetch')
        before = deepcopy(observation)
        self.trace.record_observation(observation)
        self.request(segment, request_id='2', kind='Media')
        candidate = MediaCandidate('segment', RequestContext(segment), media_type='Segment', confirmed=True,
                                   metadata={'fragment_only': True}, confidenceScore=100)
        candidate_before = deepcopy(candidate)
        self.trace.record_candidates([candidate])
        snapshot = self.trace.snapshot()
        self.assertEqual(snapshot['descriptions'][0]['references'][0]['observed_request_ids'], ['R0002'])
        self.assertEqual(snapshot['descriptions'][0]['references'][0]['candidate_ids'], ['segment'])
        self.assertEqual(snapshot['current_observation'], 'SEGMENT_ONLY')
        self.assertEqual(observation, before)
        self.assertEqual(candidate, candidate_before)

    def test_low_confidence_complete_media_eligibility_is_not_changed(self):
        self.request('https://cdn.test/video.mp4', kind='Media')
        candidate = MediaCandidate('direct', RequestContext('https://cdn.test/video.mp4'), media_type='Direct',
                                   confirmed=True, confidenceScore=0, drm=False)
        self.trace.record_candidates([candidate])
        snapshot = self.trace.snapshot()
        self.assertEqual(snapshot['counts']['downloadable'], 1)
        self.assertEqual(candidate.confidence, 0)
        self.assertTrue(candidate.downloadable)

    def test_extension_is_only_weak_diagnostic_hint(self):
        self.request('https://cdn.test/video.m4s', kind='Media')
        row = self.trace.snapshot()['requests'][0]
        self.assertIsNone(row['is_segment'])
        self.assertEqual(row['segment_evidence'], '后缀弱提示')

    def test_inline_description_is_diagnostic_only(self):
        self.trace.record_inline({'playinfo_json': '{"dash":{"video":[]}}'})
        snapshot = self.trace.snapshot()
        self.assertTrue(snapshot['description_presence']['JSON_DASH'])
        self.assertEqual(snapshot['counts']['candidates'], 0)
        self.assertFalse(snapshot['inline_descriptions'][0]['submitted_to_discovery'])

    def test_partial_response_and_candidate_state_reversal_are_logged(self):
        url = self.request('https://cdn.test/video.m4s')
        context = RequestContext(url, headers={'Range': 'bytes=0-1023'})
        self.trace.record_observation(NetworkObservation('1', context, status=206,
            body=b'\x00\x00\x00\x10ftypisom', body_complete=True,
            response_headers={'content-range': 'bytes 0-1023/999999'}))
        candidate = MediaCandidate('x', context, media_type='Direct', confirmed=True, drm=False,
                                   metadata={'last_request_id': '1'}, confidenceScore=100)
        self.trace.record_candidates([candidate])
        candidate.media_type = 'Segment'
        candidate.metadata.update(fragment_only=True, last_request_id='2')
        self.trace.record_candidates([candidate])
        result = self.trace.snapshot()
        self.assertEqual(result['requests'][0]['request_range'], 'bytes=0-1023')
        self.assertEqual(result['requests'][0]['response_content_range'], 'bytes 0-1023/999999')
        self.assertTrue(result['requests'][0]['is_partial_response'])
        self.assertTrue(result['requests'][0]['observer_body_complete_flag'])
        transition = result['candidate_transitions'][0]
        self.assertTrue(transition['before']['downloadable'])
        self.assertFalse(transition['after']['downloadable'])
        self.assertEqual(transition['after']['last_request_id'], '2')

    def test_jsonl_files_contain_required_fields_and_no_credentials(self):
        self.request()
        self.response(mime='video/mp4')
        with tempfile.TemporaryDirectory() as directory:
            self.trace.write(directory)
            for name in ('trace.json', 'fetch-xhr.jsonl', 'media.jsonl', 'focus.jsonl'):
                text = (Path(directory) / name).read_text(encoding='utf-8')
                self.assertNotIn('secret-cookie', text)
                self.assertNotIn('sign=private', text)
            row = json.loads((Path(directory) / 'media.jsonl').read_text(encoding='utf-8'))
            for field in ('url', 'method', 'content_type', 'initiator', 'is_segment', 'content_length'):
                self.assertIn(field, row)


if __name__ == '__main__':
    unittest.main()
