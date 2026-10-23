"""真实验收入口的参数、只读计数和选择边界；不访问第三方网站。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch

from tests.live_release_check import (CaptureEvidence, PAGES, _capture_checkpoint,
    _capture_dialog_issue, _code_fingerprint, _eligible_candidates,
    _instrument_body_reads, _public_url, _safe_text, _select_candidate,
    _safe_frames, _valid_av_outputs, parse_arguments, run_live_release_check)


def candidate(identity='candidate-1', *, protocol='DASH', downloadable=True,
              paired=True, confidence=90, ad=False, source_type='Network',
              validated=False, manifest_origin='', manifest_text='', metadata=None):
    return SimpleNamespace(id=identity, media_type=protocol, downloadable=downloadable,
        is_ad=ad, video_track=object() if paired else None,
        audio_track=object() if paired else None, confidence=confidence,
        source_type=source_type, validated=validated, manifest_origin=manifest_origin,
        manifest_text=manifest_text, metadata=metadata or {})


class LiveReleaseArgumentsTests(unittest.TestCase):
    def test_paused_player_uses_native_click(self):
        from tests.live_release_check import _activate_player
        observer = Mock()
        self.assertFalse(_activate_player(observer, {'paused': True, 'progress': False, 'x': 120, 'y': 80}))
        self.assertEqual(observer._send.call_count, 2)
        self.assertEqual(observer._send.call_args_list[0].args[1]['type'], 'mousePressed')
        self.assertEqual(observer._send.call_args_list[1].args[1]['type'], 'mouseReleased')

    def test_progressing_player_is_not_paused_by_test(self):
        from tests.live_release_check import _activate_player
        observer = Mock()
        self.assertTrue(_activate_player(observer, {'paused': False, 'progress': True}))
        observer._send.assert_not_called()

    def test_invalid_player_coordinates_do_not_send_input(self):
        from tests.live_release_check import _activate_player
        observer = Mock()
        for point in ((-1, 20), (float('nan'), 20), (20000, 20), ('1', 20)):
            self.assertFalse(_activate_player(observer, {'paused': True, 'x': point[0], 'y': point[1]}))
        observer._send.assert_not_called()

    def test_same_entry_supports_source_and_frozen_argv(self):
        for entry in ('main.py', 'MediaFlow.exe'):
            args = parse_arguments([entry, '--live-release-check', 'evidence', '--case', 'B'])
            self.assertEqual(args.case, 'B')
            self.assertFalse(args.deep_find)
            self.assertFalse(args.discover_only)
            self.assertEqual(args.capture_seconds, 40)

    def test_explicit_comparison_flags(self):
        args = parse_arguments(['--live-release-check', 'evidence', '--case', 'A',
            '--deep-find', '--discover-only', '--capture-seconds', '20', '--download-timeout', '120'])
        self.assertTrue(args.deep_find)
        self.assertTrue(args.discover_only)
        self.assertEqual((args.capture_seconds, args.download_timeout), (20, 120))

    def test_visible_browser_is_explicit_test_option(self):
        args = parse_arguments(['--live-release-check', 'evidence', '--case', 'douyin', '--visible-browser'])
        self.assertTrue(args.visible_browser)

    def test_rou_preroll_can_be_observed_without_clicking_ad(self):
        args = parse_arguments(['--live-release-check', 'evidence', '--case', 'rou',
            '--rou-url', 'https://rou.video/v/cmuw7m0kr000cs6fz3ceohcua', '--no-auto-play'])
        self.assertTrue(args.no_auto_play)
        self.assertEqual(args.rou_url, 'https://rou.video/v/cmuw7m0kr000cs6fz3ceohcua')

    def test_rou_override_rejects_unrelated_or_query_urls(self):
        for url in ('https://other.example/v/video',
                    'https://rou.video/v/video?token=secret', 'http://rou.video/v/video'):
            with self.subTest(url=url), patch('sys.stderr'), self.assertRaises(SystemExit):
                parse_arguments(['--live-release-check', 'evidence', '--case', 'rou', '--rou-url', url])

    def test_invalid_times_rejected(self):
        for value in ('0', '-1', 'nan', '4.2', '3601'):
            with self.subTest(value=value), patch('sys.stderr'), self.assertRaises(SystemExit):
                parse_arguments(['--live-release-check', 'evidence', '--capture-seconds', value])

    def test_bilibili_cases_use_supplied_bare_video_urls(self):
        self.assertEqual(PAGES['B'], 'https://www.bilibili.com/video/BV1fKeg6mEhi')
        self.assertEqual(PAGES['A'], 'https://www.bilibili.com/video/BV1CjaQ6NE42')
        self.assertNotIn('spm_id', PAGES['A'] + PAGES['B'])

    def test_module_fingerprint_ignores_packaging_path_and_tracks_nested_constants(self):
        source = 'def parse():\n    return frozenset(("video", "audio")), 16\n'
        first = compile(source, 'D:/source/core/parser.py', 'exec')
        frozen = compile(source, 'core/parser.py', 'exec')
        changed = compile(source.replace(', 16', ', 2'), 'core/parser.py', 'exec')
        self.assertEqual(_code_fingerprint(first), _code_fingerprint(frozen))
        self.assertNotEqual(_code_fingerprint(first), _code_fingerprint(changed))

    def test_entry_writes_runtime_and_real_runner_result_sequentially(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Mock()
            result = {'case': 'B', 'success': False, 'failure': '真实响应未发现'}
            with patch('tests.live_release_check.runtime_identity', return_value={'packaged': True}), \
                 patch('tests.live_release_check._run_case', return_value=result) as run:
                code = run_live_release_check(app, ['MediaFlow.exe', '--live-release-check', directory, '--case', 'B'])
            self.assertEqual(code, 1)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[1], 'B')
            report = json.loads((Path(directory) / 'results.json').read_text(encoding='utf-8'))
            self.assertFalse(report['success'])
            self.assertEqual(report['acceptance_mode'], 'real_download')
            app.setQuitOnLastWindowClosed.assert_called_once_with(False)


class LiveReleaseEvidenceTests(unittest.TestCase):
    def event(self, evidence, method, request_id='one', **params):
        evidence.cdp({'method': method, 'params': {'requestId': request_id, **params}})

    def test_counts_response_mime_without_media_suffix_or_stored_url(self):
        evidence = CaptureEvidence()
        self.event(evidence, 'Network.requestWillBeSent', type='Fetch',
                   request={'url': 'https://cdn.example/opaque?token=secret', 'method': 'GET'})
        self.event(evidence, 'Network.responseReceived', type='Fetch',
                   response={'status': 206, 'mimeType': 'application/octet-stream',
                             'headers': {'Content-Type': 'video/mp4'}})
        counts = evidence.summary()
        self.assertEqual(counts['page_requests'], 1)
        self.assertEqual(counts['media_responses'], 1)
        self.assertEqual(counts['range_206_responses'], 1)
        self.assertNotIn('secret', repr(evidence.__dict__))
        self.assertNotIn('cdn.example', repr(evidence.__dict__))

    def test_playurl_body_success_and_failure_are_separate(self):
        evidence = CaptureEvidence()
        self.event(evidence, 'Network.requestWillBeSent', request={'url': 'https://api.example/x/player/playurl'})
        self.event(evidence, 'Network.responseReceived', response={'status': 200, 'mimeType': 'application/json'})
        evidence.body_result(('', 'one'), {'body': '{"data":{"dash":{}}}'}, True)
        evidence.body_result(('', 'two'), {'error': {'message': 'CDP_COMMAND_FAILED'}}, False)
        counts = evidence.summary()
        self.assertEqual(counts['playurl_requests'], 1)
        self.assertEqual(counts['playurl_responses'], 1)
        self.assertEqual(counts['playurl_http_success'], 1)
        self.assertEqual(counts['playurl_cdp_bodies'], 1)
        self.assertEqual((counts['cdp_body_success'], counts['cdp_body_failed']), (1, 1))

    def test_json_observation_count_is_not_multiplied_by_repeated_events(self):
        evidence = CaptureEvidence()
        observation = SimpleNamespace(request_id='one', body=b'{"data":{"dash":{}}}',
            body_complete=True, context=SimpleNamespace(url='https://api.example/playurl?token=secret'))
        evidence.observation(observation)
        evidence.observation(observation)
        self.assertEqual(evidence.summary()['playurl_json_success'], 1)
        self.assertEqual(evidence.summary()['responses_with_body_seen'], 1)
        self.assertNotIn('secret', repr(evidence.__dict__))

    def test_transport_wrapper_preserves_callback_and_does_not_add_reads(self):
        sent = []

        def original(method, params=None, session='', callback=None):
            sent.append((method, params, session, callback))
            return True

        observer = SimpleNamespace(_send=original)
        evidence = CaptureEvidence()
        self.assertIs(_instrument_body_reads(observer, evidence), original)
        callback = Mock()
        self.assertTrue(observer._send('Network.getResponseBody', {'requestId': 'one'}, 'worker', callback))
        self.assertEqual(len(sent), 1)
        response = {'body': 'unchanged', 'base64Encoded': False}
        sent[0][3](response, True)
        callback.assert_called_once_with(response, True)
        self.assertEqual(evidence.summary()['cdp_body_requested'], 1)
        self.assertEqual(evidence.summary()['cdp_body_success'], 1)

    def test_non_body_commands_keep_original_callback(self):
        callback = Mock()
        original = Mock(return_value=False)
        observer = SimpleNamespace(_send=original)
        evidence = CaptureEvidence()
        _instrument_body_reads(observer, evidence)
        self.assertFalse(observer._send('Network.enable', {'maxTotalBufferSize': 16000000}, '', callback))
        original.assert_called_once_with('Network.enable', {'maxTotalBufferSize': 16000000}, '', callback)
        self.assertEqual(evidence.summary()['cdp_body_requested'], 0)

    def test_candidates_count_pairs_without_promoting_segments(self):
        evidence = CaptureEvidence()
        values = [candidate(), candidate('segment', protocol='Segment', paired=False, downloadable=False)]
        evidence.candidates(values)
        evidence.candidates(values)
        self.assertEqual(len(evidence.timeline), 1)
        self.assertEqual(evidence.timeline[0]['dash_pairs'], 1)
        self.assertEqual(evidence.timeline[0]['downloadable'], 1)
        self.assertEqual(evidence.timeline[0]['segments'], 1)


class LiveReleaseSelectionTests(unittest.TestCase):
    def test_bilibili_requires_complete_pair_and_preserves_low_confidence_media(self):
        pair = candidate(confidence=15)
        segment = candidate('seg', protocol='Segment', paired=False, downloadable=False)
        isolated = candidate('video-only', paired=False)
        direct = candidate('direct', protocol='Direct', paired=False)
        self.assertEqual(_eligible_candidates('B', [pair, segment, isolated, direct]), [pair])
        self.assertEqual(_eligible_candidates('douyin', [pair, segment, direct]), [pair, direct])

    def test_rou_checks_user_target_verified_playlist_instead_of_higher_score_direct(self):
        direct = candidate('page-video', protocol='Direct', paired=False, confidence=100)
        target = candidate('target', protocol='HLS', paired=False, confidence=40,
            source_type='RouVideoResolver', validated=True, manifest_origin='embedded:png/roUd',
            manifest_text='#EXTM3U\n#EXTINF:8,\nsegment.png\n',
            metadata={'video_id': 'cm5nlwtpm0000jpseusy9ni66'})
        self.assertEqual(_eligible_candidates('rou', [direct, target]), [target])
        # 这一条是固定验收样本的目标约束，不改变其它站点的候选选择策略。
        self.assertEqual(_eligible_candidates('douyin', [direct, target]), [direct, target])

    def test_rou_rejects_other_video_id_unvalidated_or_non_decoded_playlist(self):
        values = dict(protocol='HLS', paired=False, source_type='RouVideoResolver', validated=True,
            manifest_origin='embedded:png/roUd', manifest_text='#EXTM3U\n#EXTINF:2,\nsegment.png\n',
            metadata={'video_id': 'cm5nlwtpm0000jpseusy9ni66'})
        for changed in ({'metadata': {'video_id': 'another-video'}}, {'validated': False},
                        {'source_type': 'Network'}, {'manifest_origin': 'captured:hls'},
                        {'manifest_text': ''}, {'downloadable': False}):
            with self.subTest(changed=changed):
                self.assertEqual(_eligible_candidates('rou', [candidate(**{**values, **changed})]), [])

    def test_select_uses_group_quality_and_card_path_without_writing_internal_selection(self):
        pair = candidate(confidence=15)
        group = SimpleNamespace(id='group')
        dialog = SimpleNamespace(_groups=[group], include_low=Mock(),
            _choices_for_group=Mock(return_value=[pair]), _choose_quality=Mock(),
            _select_card=Mock(), selected_candidate=Mock(return_value=pair),
            confirm_button=Mock())
        dialog.confirm_button.isEnabled.return_value = True
        self.assertTrue(_select_candidate(dialog, pair))
        dialog.include_low.setChecked.assert_called_once_with(True)
        dialog._choose_quality.assert_called_once_with('group', pair.id)
        dialog._select_card.assert_called_once_with(pair.id)
        self.assertFalse(hasattr(dialog, '_selected_id'))

    def test_disabled_confirmation_is_not_overridden(self):
        pair = candidate()
        dialog = SimpleNamespace(_groups=[], _select_card=Mock(),
            selected_candidate=Mock(return_value=pair), confirm_button=Mock())
        dialog.confirm_button.isEnabled.return_value = False
        self.assertFalse(_select_candidate(dialog, pair))
        dialog.confirm_button.setEnabled.assert_not_called()

    def test_output_requires_real_mp4_family_video_and_audio(self):
        valid = {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2',
                 'video_streams': [{'codec_name': 'h264'}], 'audio_streams': [{'codec_name': 'aac'}]}
        self.assertTrue(_valid_av_outputs([valid]))
        self.assertFalse(_valid_av_outputs([]))
        self.assertFalse(_valid_av_outputs([{**valid, 'format_name': 'png_pipe'}]))
        self.assertFalse(_valid_av_outputs([{**valid, 'audio_streams': []}]))
        self.assertFalse(_valid_av_outputs([{**valid, 'video_streams': []}]))

    def test_report_urls_and_failure_text_do_not_keep_credentials_or_query(self):
        url = 'https://user:password@cdn.example/video?unexpected-secret=private#fragment'
        self.assertEqual(_public_url(url), 'https://cdn.example/video')
        safe = _safe_text('Download failed: ' + url)
        self.assertNotIn('password', safe)
        self.assertNotIn('private', safe)
        self.assertNotIn('unexpected-secret', safe)


class LiveReleaseLifecycleTests(unittest.TestCase):
    def test_destroyed_dialog_preserves_previous_counts_without_qobject_calls(self):
        from PySide6.QtCore import QObject
        from shiboken6 import delete
        dialog = QObject()
        delete(dialog)
        evidence = CaptureEvidence()
        evidence.counts['page_requests'] = 291
        latest = {'discovery': {'candidates': 6, 'downloadable': 6},
                  'dash': {'video_tracks': 6, 'audio_tracks': 3, 'paired_candidates': 6},
                  'last_valid_snapshot_second': 19.5}
        result = {}
        with self.assertRaisesRegex(RuntimeError, 'DISCOVERY_CLOSED_DURING_CAPTURE'):
            _capture_checkpoint(dialog, evidence, result, latest)
        self.assertEqual(result['failure_code'], 'DISCOVERY_CLOSED_DURING_CAPTURE')
        self.assertEqual(result['unexpected_capture_close']['reason'], 'dialog_destroyed')
        self.assertEqual(result['discovery']['downloadable'], 6)
        self.assertEqual(result['dash']['audio_tracks'], 3)
        self.assertEqual(result['capture']['page_requests'], 291)
        latest['discovery']['downloadable'] = 0
        self.assertEqual(result['discovery']['downloadable'], 6)

    def test_closing_dialog_does_not_read_cleared_candidates_or_timer(self):
        from PySide6.QtCore import QObject
        dialog = QObject()
        dialog._closing = True
        self.assertEqual(_capture_dialog_issue(dialog), 'dialog_closing')
        result, latest = {}, {'discovery': {'candidates': 6}}
        with patch('tests.live_release_check._discovery_snapshot') as snapshot, \
             self.assertRaisesRegex(RuntimeError, 'DISCOVERY_CLOSED_DURING_CAPTURE'):
            _capture_checkpoint(dialog, CaptureEvidence(), result, latest)
        snapshot.assert_not_called()
        self.assertEqual(result['discovery']['candidates'], 6)

    def test_live_checkpoint_keeps_independent_recent_snapshot(self):
        from PySide6.QtCore import QObject
        dialog = QObject()
        dialog._closing = False
        latest = {'old': 'removed'}
        with patch('tests.live_release_check._discovery_snapshot', return_value={
                'discovery': {'candidates': 6}, 'dash': {'paired_candidates': 6}}):
            _capture_checkpoint(dialog, CaptureEvidence(), {}, latest)
        self.assertNotIn('old', latest)
        self.assertEqual(latest['dash']['paired_candidates'], 6)
        self.assertGreaterEqual(latest['last_valid_snapshot_second'], 0)

    def test_safe_traceback_does_not_include_source_lines_or_locals(self):
        frames = [traceback.FrameSummary('live_release_check.py', 20, 'capture',
                                        line='Cookie: private-test-value')]
        saved = _safe_frames(frames)
        self.assertEqual(saved, [{'file': 'live_release_check.py', 'line': 20, 'function': 'capture'}])
        self.assertNotIn('private-test-value', repr(saved))


if __name__ == '__main__':
    unittest.main()
