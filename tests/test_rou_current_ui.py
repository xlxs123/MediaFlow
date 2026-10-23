"""确定性 Resolver 与异步发现、分组和 Qt 界面的实际协作回归。

界面单测仅构造普通 Qt widgets；浏览器生命周期用替身，不启动 Chromium。
"""
from copy import deepcopy
import json
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import zlib

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTableWidget

from core.discovery_models import MediaCandidate, NetworkObservation
from core.discovery_worker import DiscoveryWorker
from core.media_discovery import MAX_CANDIDATES, MediaDiscoveryService, is_verified_resolver
from core.request_context import RequestContext
from core.resolver_models import PageContext, ResolveResult, ResolverRequest
from core.site_resolvers import ResolverRegistry, RouVideoResolver
from tests.test_rou_current_protocol import API, INDEX, PAGE, context, observation, playlist
from tests.test_rou_playlist_decoder import MEDIA, png
from ui.dialogs import DetectionDialog, ResolverTracePanel, format_resolver_trace
from ui.discovery_dialog import DiscoveryDialog, candidate_details


THUMB_API = API + '?kind=thumbs'
THUMB_INDEX = 'https://track-cdn.example/index.m3u8?auth=THUMB-PRIVATE'


def thumbnail_observation(body=MEDIA):
    chain = ({'from': THUMB_API, 'to': THUMB_INDEX, 'location': THUMB_INDEX, 'status': 302},)
    context = RequestContext(THUMB_INDEX, original_url=THUMB_API, redirect_chain=chain)
    return NetworkObservation('thumbnail-request', context, 200, 'application/vnd.apple.mpegurl', body,
                              redirects=list(chain), body_complete=True)


def verified(body=MEDIA, **kwargs):
    result = RouVideoResolver(legacy_fallback=False).resolve(context(observation(
        png(zlib.compress(body), 1), headers={'Referer': PAGE, 'User-Agent': 'Fixture Browser',
                                            'Cookie': 'session=PRIVATE', 'Authorization': 'Bearer SECRET'}, **kwargs)))
    if not result.success:
        raise AssertionError(result.diagnostics)
    return result.candidates[0]


def direct(name='generic', url='https://other.example/full.mp4', **kwargs):
    fields = {'media_type': 'Direct', 'confirmed': True, 'context_complete': True,
              'drm': False, 'encryption': 'None', 'score': 99, 'sources': {'DOM', 'MSE', 'Network'},
              'source_type': 'DOM / MSE / Network', 'duration': 860.333, 'resolution': (3840, 2160)}
    fields.update(kwargs)
    return MediaCandidate(name, RequestContext(url), **fields)


class VerifiedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.service = MediaDiscoveryService()
        self.resolved = verified()

    def candidate(self):
        return next(candidate for candidate in self.service.ranked(include_low=True) if is_verified_resolver(candidate))

    def assert_authoritative(self, candidate):
        self.assertEqual(candidate.request_context, self.resolved.request_context)
        self.assertEqual((candidate.source_type, candidate.sources, candidate.score), ('RouVideoResolver', {'Resolver'}, 100))
        self.assertEqual((candidate.media_type, candidate.manifest_type, candidate.playlist_type), ('HLS', 'Media', 'VOD'))
        self.assertEqual((candidate.encryption, candidate.classification, candidate.drm), ('None', 'CLEAR_HLS', False))
        self.assertTrue(candidate.context_complete and candidate.confirmed)
        self.assertEqual(candidate.redirects, self.resolved.redirects)
        self.assertEqual(candidate.metadata['resolver_trace'], self.resolved.metadata['resolver_trace'])

    def test_resolver_result_keeps_protocol_source_context_and_100_score(self):
        self.service.consume_resolver(self.resolved)
        self.assert_authoritative(self.candidate())

    def test_delayed_network_extra_info_and_http403_cannot_replace_verified_context(self):
        self.service.consume_resolver(self.resolved)
        for url in (INDEX, API):
            wrong = RequestContext(url, headers={'User-Agent': 'Incomplete browser'})
            self.service.consume_network(NetworkObservation('late-' + url, wrong, 403, 'video/mp4',
                b'error response', frame_id='unrelated', body_complete=True, context_complete=False))
        self.assert_authoritative(self.candidate())
        self.assertEqual(len(self.service._network), 2)  # 诊断记录仍保留。

    def test_delayed_png_response_does_not_drop_verified_hls_as_an_image(self):
        self.service.consume_resolver(self.resolved)
        self.service.consume_network(NetworkObservation('png', RequestContext(INDEX), 200, 'image/png',
                                                       png(), body_complete=True))
        self.assert_authoritative(self.candidate())

    def test_dom_direct_and_unrelated_mse_drm_do_not_contaminate_resolver_identity(self):
        self.service.consume_resolver(self.resolved)
        for source in (INDEX, 'blob:' + PAGE):
            self.service.consume_dom({'page_url': PAGE, 'page_title': 'Wrong title', 'frame_id': self.resolved.frame_id,
                'videos': [{'current_src': source, 'title': 'Wrong title', 'video_id': 'wrong', 'duration': 9000,
                            'width': 3840, 'height': 2160, 'paused': False, 'media_keys': 'com.widevine.alpha'}]})
        actual = self.candidate()
        self.assert_authoritative(actual)
        self.assertEqual(actual.metadata['page_title'], 'Fixture title')
        self.assertNotIn('eme_evidence', actual.metadata)

    def test_resolver_replaces_existing_generic_guess_and_its_stale_probe_error(self):
        self.service.consume_network(NetworkObservation('guess', self.resolved.request_context, 200, 'video/mp4',
                                                       body_complete=False))
        generic = self.service.ranked(include_low=True)[0]
        self.service.apply_probe(generic.id, {'error_code': 'HTTP_403', 'http_status': 403, 'probe_error': 'Stale'})
        self.service.consume_resolver(self.resolved)
        self.assert_authoritative(self.candidate())
        self.assertNotIn('probe_error', self.candidate().metadata)

    def test_delayed_probe_neither_changes_type_nor_erases_real_drm(self):
        self.service.consume_resolver(self.resolved)
        identifier = self.candidate().id
        self.service.apply_probe(identifier, {'media_type': 'Direct', 'drm': None, 'http_status': 403,
                                              'error_code': 'HTTP_403', 'probe_error': 'late failure'})
        self.service.consume_network(NetworkObservation('probe', self.resolved.request_context, 200,
            'application/dash+xml', b'<MPD/>', resource_type='Probe', body_complete=True))
        self.assert_authoritative(self.candidate())
        body = MEDIA.replace(b'#EXTINF:', b'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://content",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:')
        protected = verified(body)
        self.service.consume_resolver(protected)
        self.service.apply_probe(identifier, {'media_type': 'Direct', 'drm': False, 'encryption': 'None'})
        actual = self.candidate()
        self.assertEqual((actual.drm, actual.classification), (True, 'DRM_FAIRPLAY'))
        self.assertFalse(actual.downloadable)

    def test_verified_segment_roles_and_arbitrary_png_names_are_preserved(self):
        self.resolved = verified(playlist(26, 259.493, 'c'))
        self.service.consume_resolver(self.resolved)
        resources = self.resolved.metadata['manifest']['resources']
        for index, resource in enumerate(resources[:3]):
            self.service.consume_network(NetworkObservation(str(index), RequestContext(resource['resolved_uri']),
                                                           206, 'image/png', b'not-an-image', body_complete=True))
        self.assertEqual(len(self.service.candidates), 1)
        self.assertEqual(self.candidate().metadata['segment_requests'], 3)
        self.assertTrue(all(resource['role'] == 'Media Segment' for resource in resources))
        self.assertEqual(self.candidate().metadata['segment_count'], 26)

    def test_verified_result_survives_bounded_candidate_noise(self):
        self.service.consume_resolver(self.resolved)
        for index in range(MAX_CANDIDATES + 2):
            self.service.consume_network(NetworkObservation(str(index), RequestContext(f'https://other.example/{index}.mp4'),
                                                           200, 'video/mp4'))
        self.assertLessEqual(len(self.service.candidates), MAX_CANDIDATES)
        self.assertEqual(self.service.ranked(include_low=True)[0].source_type, 'RouVideoResolver')
        self.assert_authoritative(self.candidate())

    def test_new_verified_signed_url_replaces_old_context_with_stable_candidate_id(self):
        self.service.consume_resolver(self.resolved)
        identifier = self.candidate().id
        replacement = verified(location=INDEX.replace('A%2FB%2BC', 'NEW-SIGNATURE'))
        self.service.consume_resolver(replacement)
        actual = self.candidate()
        self.assertEqual(actual.id, identifier)
        self.assertEqual(actual.url, replacement.url)
        self.assertEqual(actual.request_context, replacement.request_context)
        self.assertEqual(actual.metadata['resolver_trace'], replacement.metadata['resolver_trace'])

    def test_lower_priority_legacy_result_cannot_replace_already_verified_current_route(self):
        self.service.consume_resolver(self.resolved)
        legacy = deepcopy(self.resolved)
        legacy.metadata.update(resolver_verified=False, resolver_route='legacy', resolver_confidence=90)
        legacy.request_context = RequestContext(INDEX, headers={'User-Agent': 'legacy'})
        legacy.media_type, legacy.encryption, legacy.drm = 'Direct', 'Unknown', None
        self.service.consume_resolver(legacy)
        self.assert_authoritative(self.candidate())

    def test_generic_fallback_and_nonverified_legacy_resolver_keep_existing_rules(self):
        self.service.consume_network(NetworkObservation('unknown', RequestContext('https://unknown.example/no-extension'),
                                                       200, 'text/plain', MEDIA, body_complete=True))
        generic = self.service.ranked(include_low=True)[0]
        self.assertEqual(generic.media_type, 'HLS')
        self.assertTrue(generic.downloadable)
        self.assertLessEqual(generic.score, 99)
        legacy = deepcopy(self.resolved)
        legacy.metadata.pop('resolver_verified')
        legacy.metadata['resolver_route'] = 'legacy'
        legacy.metadata['resolver_confidence'] = 90
        self.service.consume_resolver(legacy)
        actual = next(candidate for candidate in self.service.ranked(include_low=True) if candidate.url == legacy.url)
        self.assertFalse(is_verified_resolver(actual))
        self.assertIn('Network', actual.sources)
        self.assertLessEqual(actual.score, 99)

    def test_confirmed_thumbnail_role_removes_existing_master_children_and_blocks_replays(self):
        thumb = thumbnail_observation(b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000\nchild/index.m3u8\n')
        self.service.consume_network(thumb)
        self.assertEqual(len(self.service.candidates), 2)
        self.service.exclude_resource_role([THUMB_API, THUMB_INDEX], 'THUMBNAIL_TRACK', thumb.request_id)
        self.assertFalse(self.service.candidates)
        self.service.consume_network(thumb)
        self.service.consume_network(NetworkObservation('child', RequestContext('https://track-cdn.example/child/index.m3u8'),
                                                       200, 'application/vnd.apple.mpegurl', MEDIA, body_complete=True))
        self.service.consume_dom({'page_url': PAGE, 'videos': [{'src': THUMB_INDEX, 'paused': False, 'duration': 60}]})
        self.assertFalse(self.service.candidates)
        refreshed_url = THUMB_INDEX.replace('THUMB-PRIVATE', 'FRESH-THUMB')
        refreshed = thumbnail_observation()
        refreshed.request_id = 'refreshed-thumbnail'
        refreshed.context = RequestContext(refreshed_url, original_url=THUMB_API)
        self.service.consume_network(refreshed)
        self.service.consume_dom({'page_url': PAGE, 'videos': [{'src': refreshed_url, 'paused': False, 'duration': 60}]})
        self.assertFalse(self.service.candidates)
        self.assertIn(thumb.request_id, self.service._network)

    def test_role_exclusion_is_protocol_agnostic_and_does_not_remove_verified_main(self):
        self.service.consume_resolver(self.resolved)
        self.service.exclude_resource_role([INDEX], 'Thumbnail', 'non-site-specific')
        self.assert_authoritative(self.candidate())
        self.service.consume_network(NetworkObservation('later', RequestContext(INDEX), 200, 'video/mp4'))
        self.assert_authoritative(self.candidate())


class DialogHarness(DiscoveryDialog):
    """执行生产界面方法，隔离浏览器和后台下载；不抢桌面焦点。"""
    def __init__(self, parent=None, shared=False):
        QDialog.__init__(self, parent)
        self._closing = self._worker_done = self._page_gone = self._profile_gone = self._shutdown_emitted = False
        self._pages_alive = 1
        self._owns_profile = not shared
        self._candidates, self._groups = [], []
        self._group_checks, self._group_choices = {}, {}
        self._selected_id, self._user_selected = None, False
        self._batch_pending = self._batch_enqueued = False
        self._resolver_succeeded = self._resolver_verified = self._showing_verified = False
        self._resolver_diagnostics, self._fetch_queue = [], []
        self._generic_diagnostic, self._network_history, self._last_dom = {}, {}, {}
        self._fetch_tried, self._fetch_inflight, self._resolver_epoch = set(), None, 1
        self._excluded_resource_urls = set()
        self._listening, self._player_type = True, ''
        self.listen_timer, self.resolver_timer = QTimer(self), QTimer(self)
        self.worker = MagicMock(generation=4)
        self.page = MagicMock()
        self.page.url.return_value = QUrl(PAGE)
        self.profile, self.view = MagicMock(), MagicMock()
        self.observer = MagicMock(_enabled=True)
        self.observer.fetch_url.return_value = True
        self.page_url, self.include_low = QLineEdit(PAGE, self), QCheckBox(self)
        self.table = QTableWidget(0, 9, self)
        self.summary_label, self.player_label, self.status_label = QLabel(self), QLabel(self), QLabel(self)
        self.details = QPlainTextEdit(self)
        from ui.track_selector import TrackSelector
        self.track_selector = TrackSelector(self)
        self.trace = ResolverTracePanel(self)
        self.confirm_button, self.download_all_button = QPushButton(self), QPushButton(self)
        self.find_button, self.deep_find_button = QPushButton(self), QPushButton(self)
        self.all_qualities, self.auto_fallback = QCheckBox(self), QCheckBox(self)


class CurrentRouUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = DialogHarness()
        self.candidate = verified(playlist(106, 860.333, 'a'))
        self.candidate.id = 'verified-main'

    def tearDown(self):
        self.dialog._closing = True  # Harness 不持有真实浏览器/线程。
        self.dialog.deleteLater()
        self.app.processEvents()

    def display(self, *extra):
        self.dialog._update_candidates([*extra, self.candidate], 'BLOB_MSE', self.dialog.worker.generation)

    def test_default_ui_keeps_generic_but_only_selects_verified_main_movie(self):
        generic = direct()
        self.display(generic)
        self.assertEqual(self.dialog.table.rowCount(), 2)
        self.assertEqual({c.id for c in self.dialog._visible_candidates()}, {self.candidate.id, generic.id})
        self.assertEqual(self.dialog.selected_candidate().id, self.candidate.id)
        self.assertEqual(self.dialog.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertEqual(self.dialog.table.item(0, 6).text(), f'{self.candidate.confidence}/100 · 推荐')
        self.assertEqual(self.dialog.table.item(0, 7).text(), 'RouVideoResolver')
        self.assertTrue(self.dialog.confirm_button.isEnabled())

    def test_advanced_generic_diagnostic_cannot_replace_same_video_group_and_is_not_preselected(self):
        guess = direct(url=INDEX, metadata={'video_id': self.candidate.metadata['video_id'],
                       'resolver_id': 'rou.video', 'page_url': PAGE, 'title': 'Wrong title', 'discovered_at': time.time() + 10})
        self.dialog.include_low.setChecked(True)
        self.display(guess)
        self.assertEqual(self.dialog.table.rowCount(), 2)
        selected = [group for group in self.dialog._groups if group.selected]
        self.assertEqual([group.selected_candidate.id for group in selected], [self.candidate.id])
        self.assertEqual(self.dialog.selected_candidate().source_type, 'RouVideoResolver')
        self.assertTrue(any(group.id.startswith('generic-') and not group.selected for group in self.dialog._groups))

    def test_full_summary_and_collapsible_trace_are_redacted(self):
        self.display(direct())
        summary = self.dialog.summary_label.text()
        for label in ('Fixture title', 'HLS', 'Media Playlist', 'VOD', '加密：无', 'DRM：否', '106', '14分20秒',
                      'RouVideoResolver', '已验证', '/api/hls → 302 → PNG(roUd) → zlib → HLS'):
            self.assertIn(label, summary)
        self.assertFalse(self.dialog.trace.toggle.isChecked())
        self.assertTrue(self.dialog.trace.editor.isHidden())
        self.dialog.trace.toggle.setChecked(True)
        self.assertFalse(self.dialog.trace.editor.isHidden())
        trace = json.loads(self.dialog.trace.editor.toPlainText())
        for field in ('video_id', 'api', 'api_status', 'redirect', 'location', 'index_url', 'index_http',
                      'container', 'roud', 'flag', 'compression', 'playlist', 'manifest_type', 'playlist_type',
                      'segment_count', 'duration', 'encryption', 'drm'):
            self.assertIn(field, trace)
        texts = '\n'.join((summary, self.dialog.details.toPlainText(), self.dialog.trace.editor.toPlainText(),
                           self.dialog.table.item(0, 8).text()))
        for secret in ('PRIVATE', 'SECRET', 'A%2FB%2BC', 'auth=actual-', 'v=6'):
            self.assertNotIn(secret, texts)
        self.assertIn('A%2FB%2BC', self.dialog.selected_candidate().url)
        self.assertEqual(self.dialog.selected_candidate().request_context.headers['cookie'], 'session=PRIVATE')

    def test_detection_dialog_has_same_collapsible_trace_and_verified_details(self):
        dialog = DetectionDialog(self.candidate.safe_diagnostic())
        self.assertIn('状态：已验证', dialog.editor.toPlainText())
        self.assertIn('Manifest：Media Playlist', dialog.editor.toPlainText())
        self.assertIn('HLS 播放类型：VOD', dialog.editor.toPlainText())
        self.assertTrue(dialog.trace.editor.isHidden())
        dialog.trace.toggle.setChecked(True)
        self.assertFalse(dialog.trace.editor.isHidden())
        dialog.deleteLater()

    def test_trace_redacts_credentials_in_nested_headers_and_signed_urls(self):
        values = {'trace': {'api_status': 302, 'index_url': INDEX,
                           'request': {'Cookie': 'session=PRIVATE', 'Authorization': 'Bearer SECRET'}}}
        text = format_resolver_trace(values)
        self.assertEqual(json.loads(text)['request'], {'Cookie': '[present]', 'Authorization': '[present]'})
        for secret in ('PRIVATE', 'SECRET', 'A%2FB%2BC'):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)['api_status'], 302)

    def test_browser_fetch_receives_exact_observed_resolver_headers(self):
        headers = {'Authorization': 'Bearer synthetic', 'Range': 'bytes=0-', 'X-Test': 'fixture'}
        request = ResolverRequest(API, metadata={'frame_id': 'main'}, headers=headers)
        self.dialog._fetch_queue = [(request, True)]
        self.dialog._next_fetch()
        self.dialog.observer.fetch_url.assert_called_once()
        args, kwargs = self.dialog.observer.fetch_url.call_args
        self.assertEqual(args[0], API)
        self.assertEqual(kwargs, {'frame_id': 'main', 'headers': headers})

    def test_verified_success_stops_queued_generic_fetches_and_stale_failure_does_not_replace_trace(self):
        success = ResolveResult(True, [self.candidate], [{'code': 'ROU_RESOLVER_TRACE',
            'trace': self.candidate.safe_diagnostic()['resolver_trace']}])
        self.dialog._fetch_queue = [(ResolverRequest('https://other.example/guess.mp4'), False)]
        self.dialog._resolver_result(success, 4)
        self.assertTrue(self.dialog._resolver_verified)
        self.assertFalse(self.dialog._fetch_queue)
        self.dialog._resolver_result(ResolveResult(False, diagnostics=[{'error_code': 'ROU_API_REQUEST_FAILED'}],
                                                  requests=[ResolverRequest(API)]), 4)
        self.assertEqual(self.dialog._resolver_diagnostics, success.diagnostics)
        legacy = deepcopy(self.candidate)
        legacy.metadata['resolver_verified'] = False
        self.dialog._resolver_result(ResolveResult(True, [legacy], [{'code': 'LEGACY'}]), 4)
        self.assertEqual(self.dialog._resolver_diagnostics, success.diagnostics)
        self.dialog.observer.fetch_url.assert_not_called()

    def test_resolver_failure_shows_trace_but_preserves_generic_fallback(self):
        failure = ResolveResult(False, diagnostics=[{'error_code': 'ROU_API_REQUEST_FAILED',
            'trace': {'api': API, 'api_status': 403, 'error_code': 'HTTP_403'}}])
        self.dialog._resolver_result(failure, 4)
        self.assertEqual(json.loads(self.dialog.trace.editor.toPlainText())['api_status'], 403)
        self.dialog._update_candidates([direct()], 'HTML5_VIDEO', 4)
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertTrue(self.dialog.confirm_button.isEnabled())
        self.assertEqual(self.dialog.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertEqual(json.loads(self.dialog.trace.editor.toPlainText())['api_status'], 403)

    def test_thumbnail_exclusion_immediately_removes_stale_ui_and_batch_candidates(self):
        thumb = direct('thumb', THUMB_INDEX)
        thumb.request_context = thumbnail_observation().context
        fallback = direct('fallback', 'https://other.example/ordinary.mp4')
        self.dialog._update_candidates([thumb, fallback], '', 4)
        self.assertEqual(self.dialog.table.rowCount(), 2)
        self.dialog._fetch_queue = [(ResolverRequest(THUMB_INDEX), False)]
        result = ResolveResult(False, diagnostics=[{'classification': 'THUMBNAIL_TRACK',
            'request_id': 'thumbnail-request', 'excluded_urls': [THUMB_API, THUMB_INDEX]}])
        self.dialog._resolver_result(result, 4)
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertEqual(self.dialog.selected_candidate().id, 'fallback')
        self.assertFalse(self.dialog._fetch_queue)
        self.dialog._update_candidates([thumb, fallback], '', 4)
        self.assertEqual(self.dialog.table.rowCount(), 1)
        outputs = []
        self.dialog.batch_requested.connect(lambda *args: outputs.append(args))
        self.dialog._batch_pending = True
        self.dialog._batch_ready([thumb, fallback], 4)
        self.assertEqual([group.selected_candidate.id for group in outputs[-1][0] if group.selected], ['fallback'])
        text = self.dialog.trace.editor.toPlainText()
        self.assertIn('THUMBNAIL_TRACK', text)
        self.assertNotIn('THUMB-PRIVATE', text)

    def test_download_all_reuses_media_groups_and_emits_only_default_verified_movie(self):
        outputs = []
        self.dialog.batch_requested.connect(lambda *args: outputs.append(args))
        self.display(direct())
        self.dialog._batch_pending = True
        self.dialog._batch_ready([direct(), self.candidate], 4)
        groups, options = outputs[0]
        self.assertEqual([group.selected_candidate.source_type for group in groups if group.selected], ['RouVideoResolver'])
        self.assertEqual(options, {'all_qualities': False, 'auto_fallback': False})
        self.assertEqual(groups[0].selected_candidate.request_context, self.candidate.request_context)
        self.assertEqual(groups[0].title, 'Fixture title')

    def test_real_drm_verified_source_is_shown_but_cannot_be_selected_for_download(self):
        self.candidate = verified(MEDIA.replace(b'#EXTINF:',
            b'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://protected",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:'))
        self.display(direct())
        self.assertEqual(self.dialog.table.rowCount(), 2)
        self.assertIn('DRM：是', self.dialog.table.item(0, 5).text())
        self.assertEqual(self.dialog.table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertFalse(self.dialog.confirm_button.isEnabled())
        self.assertFalse(self.dialog.download_all_button.isEnabled())
        self.assertIn('100/100', candidate_details(self.candidate))

    def test_retaining_profile_transfers_only_profile_ownership_to_parent(self):
        parent = QDialog()
        self.dialog.setParent(parent)
        profile = self.dialog.retain_session_profile()
        self.assertIs(profile, self.dialog.profile)
        profile.setParent.assert_called_once_with(parent)
        self.assertFalse(self.dialog._owns_profile)
        self.assertIs(self.dialog.retain_session_profile(), profile)
        profile.setParent.assert_called_once()
        self.dialog.setParent(None)
        parent.deleteLater()

    def test_shared_profile_shutdown_releases_pages_and_worker_without_deleting_session(self):
        self.dialog._owns_profile = False
        outputs = []
        self.dialog.shutdown_complete.connect(lambda: outputs.append(True))
        self.dialog.shutdown()
        self.dialog.profile.destroyed.connect.assert_not_called()
        self.assertTrue(self.dialog._profile_gone)
        self.dialog._worker_finished()
        self.assertFalse(outputs)  # 本窗口 Page 也必须完成释放。
        self.dialog._page_destroyed()
        self.dialog.profile.deleteLater.assert_not_called()
        self.dialog.page.deleteLater.assert_called_once()
        self.dialog.worker.cancel.assert_called_once()
        self.assertEqual(outputs, [True])

    def test_owned_profile_shutdown_still_deletes_it_after_last_page(self):
        outputs = []
        self.dialog.shutdown_complete.connect(lambda: outputs.append(True))
        self.dialog.shutdown()
        self.dialog._worker_finished()
        self.dialog.profile.deleteLater.assert_not_called()
        self.dialog._page_destroyed()
        self.dialog.profile.deleteLater.assert_called_once()
        self.assertFalse(outputs)
        self.dialog._profile_destroyed()
        self.assertEqual(outputs, [True])


class VerifiedWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.worker = None
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        if self.worker:
            self.worker.cancel()
            self.wait_for(lambda: not self.worker.isRunning())
            self.worker.deleteLater()
            self.app.processEvents()

    def wait_for(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(condition())

    def test_verified_resolver_avoids_redundant_generic_http_probe_and_freezes_for_batch(self):
        candidate = verified()
        registry = MagicMock()
        registry.resolve.return_value = ResolveResult(True, [candidate])
        outputs = []
        self.worker = DiscoveryWorker(probe=True, resolver_registry=registry)
        self.worker.batch_ready.connect(lambda *args: outputs.append(args))
        with patch.object(DiscoveryWorker, '_probe') as probe:
            self.worker.start()
            self.worker.submit('page', PageContext(PAGE))
            self.worker.freeze()
            self.wait_for(lambda: bool(outputs))
            probe.assert_not_called()
        self.assertEqual(outputs[-1][0][0].score, 100)
        self.assertEqual(outputs[-1][0][0].source_type, 'RouVideoResolver')

    def test_inflight_generic_probe_completed_after_resolver_cannot_override_final_batch(self):
        candidate = verified()
        started = threading.Event()
        outputs, batch = [], []

        def probe(*_):
            started.set()
            self.release.wait(3)
            return {'media_type': 'Direct', 'error_code': 'HTTP_403', 'http_status': 403, 'probe_error': 'late'}, None

        self.worker = DiscoveryWorker(probe=True)
        self.worker.candidates_changed.connect(lambda *args: outputs.append(args))
        self.worker.batch_ready.connect(lambda *args: batch.append(args))
        with patch.object(DiscoveryWorker, '_probe', side_effect=probe):
            self.worker.start()
            self.worker.submit('network', NetworkObservation('generic', candidate.request_context, 200,
                'application/vnd.apple.mpegurl', MEDIA, body_complete=True))
            self.wait_for(started.is_set)
            self.worker.submit('resolver', candidate)
            self.wait_for(lambda: any(any(is_verified_resolver(c) for c in values[0]) for values in outputs))
            self.release.set()
            self.worker.freeze()
            self.wait_for(lambda: bool(batch))
        actual = batch[-1][0][0]
        self.assertEqual((actual.media_type, actual.http_status, actual.score), ('HLS', 200, 100))
        self.assertNotIn('probe_error', actual.metadata)
        self.assertEqual(actual.request_context, candidate.request_context)

    def test_generic_network_after_verified_success_is_observed_without_new_http_probe(self):
        outputs = []
        self.worker = DiscoveryWorker(probe=True)
        self.worker.batch_ready.connect(lambda *args: outputs.append(args))
        with patch.object(DiscoveryWorker, '_probe') as probe:
            self.worker.start()
            self.worker.submit('resolver', verified())
            self.worker.submit('network', NetworkObservation('other', RequestContext('https://other.example/full.mp4'),
                                                           200, 'video/mp4'))
            self.worker.freeze()
            self.wait_for(lambda: bool(outputs))
            probe.assert_not_called()
        candidates = outputs[-1][0]
        self.assertEqual(candidates[0].score, 100)
        self.assertTrue(any(candidate.media_type == 'Direct' for candidate in candidates))

    def test_network_then_failed_page_resolver_excludes_thumbnail_hls_and_keeps_generic_fallback(self):
        thumb = thumbnail_observation()
        failed_main = NetworkObservation('main-api', RequestContext(API), 403)
        fallback_url = 'https://ordinary-cdn.example/full.m3u8'
        fallback = NetworkObservation('fallback', RequestContext(fallback_url), 200,
                                      'application/vnd.apple.mpegurl', MEDIA, body_complete=True)
        registry = ResolverRegistry([RouVideoResolver(legacy_fallback=False)])
        result = registry.resolve(PageContext(PAGE, observations=(thumb, failed_main, fallback)))
        self.assertFalse(result.success)
        excluded = next(item for item in result.diagnostics if item.get('classification') == 'THUMBNAIL_TRACK')
        self.assertEqual(excluded['request_id'], thumb.request_id)
        self.assertTrue({THUMB_API, THUMB_INDEX} <= set(excluded['excluded_urls']))
        outputs, snapshots = [], []
        self.worker = DiscoveryWorker(probe=False, resolver_registry=registry)
        self.worker.resolver_result.connect(lambda *args: outputs.append(args))
        self.worker.batch_ready.connect(lambda *args: snapshots.append(args))
        self.worker.start()
        for observed in (thumb, failed_main, fallback):
            self.worker.submit('network', observed)
        self.worker.submit('page', PageContext(PAGE, observations=(thumb, failed_main, fallback)))
        self.worker.freeze()
        self.wait_for(lambda: bool(snapshots and outputs))
        self.assertFalse(outputs[-1][0].success)
        candidates = snapshots[-1][0]
        self.assertEqual([candidate.url for candidate in candidates], [fallback_url])
        self.assertTrue(candidates[0].downloadable)


if __name__ == '__main__':
    unittest.main()
