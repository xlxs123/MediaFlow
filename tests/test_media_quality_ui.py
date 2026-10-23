"""质量推荐的 Qt 选择、偏好迁移和实际下载参数连接回归。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QComboBox
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from core.discovery_models import MediaCandidate, NetworkObservation
from core.download_manager import DownloadManager
from core.downloader import DownloadRequest, build_arguments
from core.media_discovery import MediaDiscoveryService
from core.media_quality import QUALITY_PREFERENCES
from core.request_context import RequestContext
from core.yt_dlp_manager import ToolInfo
from tests.test_mediaflow_ui import media
from tests.test_rou_current_ui import DialogHarness
from ui.main_window import MainWindow
from utils.config import ConfigStore


def version(height, **fields):
    metadata = {'video_id': 'one-real-video', 'page_url': 'https://page.example/watch',
                'title': '同一正片'}
    metadata.update(fields.pop('metadata', {}))
    return media(f'version-{height}', current=True, height=height, metadata=metadata,
                 videoCodec='h264', audioCodec='aac', **fields)


class QualityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = DialogHarness()

    def tearDown(self):
        self.dialog._closing = True
        self.dialog.deleteLater()
        self.app.processEvents()

    def display(self, *values):
        self.dialog._update_candidates(list(values), 'HTML5_VIDEO', self.dialog.worker.generation)

    def enable_cards(self):
        host = QWidget(self.dialog)
        self.dialog.candidate_card_layout = QVBoxLayout(host)
        self.dialog.candidate_empty_state = QLabel(host)
        self.dialog._candidate_cards = {}
        self.dialog.table.itemChanged.connect(self.dialog._group_checked)

    @staticmethod
    def unidentified_hls(height):
        candidate = version(height)
        candidate.media_type, candidate.manifest_type = 'HLS', 'Media Playlist'
        candidate.metadata.pop('video_id')
        candidate.metadata['parent_manifest'] = 'https://cdn.example/master.m3u8'
        return candidate

    def test_recommends_high_quality_within_one_identity_and_shows_scores(self):
        self.display(version(480), version(1080), version(720))
        self.assertEqual(len(self.dialog._groups), 1)
        self.assertEqual(self.dialog.selected_candidate().height, 1080)
        combo = self.dialog.table.cellWidget(0, 2)
        self.assertEqual(combo.count(), 3)
        recommended = [combo.itemText(i) for i in range(combo.count()) if '★ 推荐' in combo.itemText(i)]
        self.assertEqual(len(recommended), 1)
        self.assertIn('1080p', recommended[0])
        self.assertIn('质量45', recommended[0])
        self.assertIn('质量评分：45/100', self.dialog.details.toPlainText())

    def test_quality_never_promotes_independent_preload_above_current_movie(self):
        main = media('movie', current=True, height=1080, videoCodec='h264', audioCodec='aac')
        preload = media('next', height=2160, videoCodec='av1', audioCodec='aac')
        for preference, _ in QUALITY_PREFERENCES:
            self.dialog._quality_preference = preference
            self.display(preload, main)
            self.assertEqual(self.dialog.selected_candidate().id, main.id, preference)
            self.assertEqual([g.selected_candidate.id for g in self.dialog._groups if g.selected], [main.id])

    def test_equal_independent_players_remain_ambiguous_despite_quality_difference(self):
        self.display(media('first', current=True, height=480), media('second', current=True, height=2160))
        self.assertIsNone(self.dialog.selected_candidate())
        self.assertFalse(any(group.selected for group in self.dialog._groups))

    def test_manual_version_persists_after_new_discovery_updates(self):
        low, high = version(720), version(1080)
        self.display(low, high)
        group = self.dialog._groups[0]
        self.dialog._choose_quality(group.id, low.id)
        self.display(version(2160), low, high)
        self.assertEqual(self.dialog.selected_candidate().id, low.id)
        self.assertIn(low.id, self.dialog._group_choices.values())

    def test_reused_card_switches_quality_after_browser_adds_video_identity(self):
        self.enable_cards()
        low, high = self.unidentified_hls(720), self.unidentified_hls(1080)
        self.display(low, high)
        previous_id = self.dialog._groups[0].id
        card = self.dialog._candidate_cards[high.id]
        for candidate in (low, high):
            candidate.metadata['video_id'] = 'late-browser-video'
        self.display(low, high)
        group_id = self.dialog._groups[0].id
        self.assertNotEqual(group_id, previous_id)
        self.assertIs(self.dialog._candidate_cards[high.id], card)
        card.quality_combo.setCurrentIndex(card.quality_combo.findData(low.id))
        self.assertEqual(self.dialog.selected_candidate().id, low.id)
        self.assertEqual(self.dialog._group_choices, {group_id: low.id})

    def test_manual_quality_and_unchecked_card_survive_late_video_identity(self):
        self.enable_cards()
        low, high = self.unidentified_hls(720), self.unidentified_hls(1080)
        self.display(low, high)
        card = self.dialog._candidate_cards[high.id]
        card.quality_combo.setCurrentIndex(card.quality_combo.findData(low.id))
        self.dialog._candidate_cards[low.id].checked_box.setChecked(False)
        previous_id = self.dialog._groups[0].id
        self.assertFalse(self.dialog._group_checks[previous_id])
        for candidate in (low, high):
            candidate.metadata['video_id'] = 'late-browser-video'
        self.display(low, high)
        group_id = self.dialog._groups[0].id
        self.assertNotEqual(group_id, previous_id)
        self.assertEqual(self.dialog.selected_candidate().id, low.id)
        self.assertEqual(self.dialog._group_choices, {group_id: low.id})
        self.assertEqual(self.dialog._group_checks, {group_id: False})
        self.assertFalse(self.dialog._groups[0].selected)
        self.assertFalse(self.dialog._candidate_cards[low.id].checked_box.isChecked())
        self.assertFalse(self.dialog.download_all_button.isEnabled())

    def test_group_split_and_merge_require_rechecking_instead_of_copying_user_choices(self):
        low, high = self.unidentified_hls(720), self.unidentified_hls(1080)
        self.display(low, high)
        group_id = self.dialog._groups[0].id
        self.dialog._group_checks[group_id] = True
        self.dialog._choose_quality(group_id, low.id)
        low.metadata['video_id'], high.metadata['video_id'] = 'movie-a', 'movie-b'
        self.display(low, high)
        self.assertEqual(len(self.dialog._groups), 2)
        self.assertEqual(self.dialog._group_choices, {})
        self.assertFalse(any(group.selected for group in self.dialog._groups))
        for group in self.dialog._groups:
            self.dialog._group_checks[group.id] = True
        low.metadata['video_id'] = high.metadata['video_id'] = 'merged-movie'
        self.display(low, high)
        self.assertEqual(len(self.dialog._groups), 1)
        self.assertFalse(self.dialog._groups[0].selected)
        self.assertFalse(self.dialog.download_all_button.isEnabled())

    def test_smallest_unknown_size_is_not_treated_as_zero(self):
        self.dialog._quality_preference = 'smallest'
        known = version(1080, estimatedSize=10_000_000)
        unknown = version(480)
        self.display(unknown, known)
        self.assertEqual(self.dialog.selected_candidate().id, known.id)

    def test_quality_does_not_upgrade_69_to_automatic_selection(self):
        high = version(2160)
        high.confidenceScore = 69
        self.display(high)
        self.assertIsNone(self.dialog.selected_candidate())
        self.assertFalse(any(group.selected for group in self.dialog._groups))

    def test_dash_choice_keeps_context_and_reaches_existing_download_selector(self):
        service = MediaDiscoveryService()
        context = RequestContext('https://cdn.example/opaque?token=A%2FB', headers={'Referer': 'https://page.example/watch',
            'User-Agent': 'Fixture Browser', 'Authorization': 'Bearer synthetic'})
        body = b'''<MPD type="static" mediaPresentationDuration="PT19S"><Period>
          <AdaptationSet mimeType="video/mp4" codecs="avc1.640028" frameRate="25">
            <Representation id="v1080" width="1920" height="1080" bandwidth="4000000"/>
            <Representation id="v720" width="1280" height="720" bandwidth="1800000"/>
          </AdaptationSet><AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2">
            <Representation id="a" bandwidth="128000"/>
          </AdaptationSet></Period></MPD>'''
        service.consume_network(NetworkObservation('mpd', context, 200, 'application/dash+xml', body, body_complete=True))
        root = service.ranked(include_low=True)[0]
        root.confidenceScore = 98
        root.confirmed = True
        root.drm = False
        payload = deepcopy(root.download_payload())
        self.display(root)
        group = self.dialog._groups[0]
        choices = self.dialog._choices_for_group(group)
        self.assertEqual({candidate.height for candidate in choices}, {720, 1080})
        low = next(candidate for candidate in choices if candidate.height == 720)
        self.dialog._choose_quality(group.id, low.id)
        selected = self.dialog.selected_candidate()
        self.assertEqual(selected.height, 720)
        self.assertEqual(selected.request_context, context)
        self.assertEqual(selected.download_payload(), payload)
        self.assertIn('720', selected.metadata['format_selector'])
        with tempfile.TemporaryDirectory() as folder:
            template = DownloadRequest('yt-dlp.exe', root.url, folder, ffmpeg_path='ffmpeg.exe')
            request = DownloadManager._request(selected, group, template)
            self.assertEqual(request.format_selector, selected.metadata['format_selector'])
            self.assertIn(request.format_selector, build_arguments(request))
            with patch.object(MainWindow, 'detect_tools'):
                window = MainWindow(ConfigStore(Path(folder) / 'settings.json'))
            try:
                window.yt_info, window.ff_info = ToolInfo('yt-dlp.exe', 'test'), ToolInfo('ffmpeg.exe', 'test')
                window.output_directory.setText(folder)
                window.import_discovered_media(selected)
                request = window._collect_request()
                self.assertEqual(request.format_selector, selected.metadata['format_selector'])
                self.assertEqual(request.to_context(), context)
                self.assertEqual(request.download_protocol, 'DASH')
            finally:
                window.batch_controller.shutdown()
                window.close()
                deadline = time.monotonic() + 8
                while (not window.batch_controller.is_closed or not window.library_service.is_closed) and time.monotonic() < deadline:
                    self.app.processEvents()
                self.assertTrue(window.batch_controller.is_closed, '批次后台未在限时内结束')
                self.assertTrue(window.library_service.is_closed, '媒体库后台未在限时内结束')
                window.deleteLater()
                self.app.processEvents()

    def test_preference_changes_keep_checked_videos_and_clear_manual_version_override(self):
        self.display(version(720), version(1080))
        group = self.dialog._groups[0]
        self.dialog._group_checks[group.id] = True
        self.dialog._choose_quality(group.id, 'version-720')
        self.dialog.quality_preference_combo = QComboBox()
        self.dialog.quality_preference_combo.addItem('最高画质', 'highest')
        self.dialog._quality_preference_changed()
        self.assertEqual(self.dialog._group_choices, {})
        self.assertTrue(self.dialog._group_checks[group.id])
        self.assertEqual(self.dialog.selected_candidate().height, 1080)


class QualityPreferenceTests(unittest.TestCase):
    def test_old_config_retains_values_and_defaults_to_auto(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'output_directory': 'D:\\saved', 'max_concurrent_videos': 3}), encoding='utf-8')
            original = path.read_bytes()
            loaded = ConfigStore(path).load()
            self.assertEqual(loaded['quality_preference'], 'auto')
            self.assertEqual(loaded['output_directory'], 'D:\\saved')
            self.assertEqual(path.read_bytes(), original)

    def test_all_preferences_roundtrip_and_invalid_values_default(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder) / 'settings.json')
            for preference, _ in QUALITY_PREFERENCES:
                self.assertTrue(store.save({'quality_preference': preference}))
                self.assertEqual(store.load()['quality_preference'], preference)
            for invalid in ('unlimited', '', None, {}, []):
                self.assertTrue(store.save({'quality_preference': invalid}))
                self.assertEqual(store.load()['quality_preference'], 'auto')


if __name__ == '__main__':
    unittest.main()
