"""真实离线 Qt 轨道控件，不调用下载器或外部进程。"""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from core.discovery_models import MediaCandidate
from core.request_context import RequestContext
from ui.track_selector import TrackSelector


def track(identity, language, *, subtitle=False, **extra):
    return {'id': identity, 'language': language, 'name': identity, 'codec': 'srt' if subtitle else 'aac',
        'channels': 0 if subtitle else 2, 'sample_rate': 0 if subtitle else 48000,
        'bitrate': 0 if subtitle else 128000, 'default': False, 'automatic': None,
        'format': 'srt' if subtitle else '', 'source': 'ytdlp', 'supported': True, 'reason': '', **extra}


def media(identity='one', **changes):
    values = {'audio': [track('audio-en', 'en', name='Original', default=True),
                       track('audio-ja', 'ja', name='日本語'),
                       track('audio-bad', 'fr', supported=False, reason='该音轨没有完整资源。')],
        'subtitles': [track('subtitle-zh', 'zh-Hans', subtitle=True, automatic=False, name='简体字幕'),
                      track('subtitle-auto', 'en', subtitle=True, automatic=True),
                      track('subtitle-bad', 'ja', subtitle=True, supported=False,
                            reason='分段字幕暂不支持独立提取。')], 'known': True}
    metadata = {'track_catalog': values, **changes.pop('metadata', {})}
    return MediaCandidate(identity, RequestContext('https://test.local/' + identity),
                          media_type='Direct', container='mp4', metadata=metadata, **changes)


class TrackSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = TrackSelector()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def choose_audio(self, identity):
        self.widget.audio_combo.setCurrentIndex(self.widget.audio_combo.findData(identity))

    def choose_mode(self, value):
        self.widget.mode_combo.setCurrentIndex(self.widget.mode_combo.findData(value))

    def choose_container(self, value):
        self.widget.container_combo.setCurrentIndex(self.widget.container_combo.findData(value))

    def subtitle(self, identity):
        return next(self.widget.subtitle_list.item(index) for index in range(self.widget.subtitle_list.count())
                    if self.widget.subtitle_list.item(index).data(Qt.ItemDataRole.UserRole) == identity)

    def test_none_is_clear_and_not_a_fake_track(self):
        self.assertIn('选择媒体后', self.widget.summary.text())
        self.assertFalse(self.widget.audio_combo.isEnabled())
        self.assertFalse(self.widget.mode_combo.isEnabled())
        with self.assertRaisesRegex(ValueError, '先选择'):
            self.widget.selection()

    def test_unknown_direct_catalog_keeps_original_and_explains_post_download_check(self):
        candidate = MediaCandidate('unknown', RequestContext('https://test.local/video'), media_type='Direct')
        self.widget.set_candidate(candidate)
        self.assertIn('下载后检查实际轨道', self.widget.summary.text())
        self.assertIn('未发现可用字幕', self.widget.summary.text())
        self.assertEqual(self.widget.selection(), {'audio_id': '', 'subtitle_ids': [],
            'subtitle_mode': 'none', 'container': 'original'})

    def test_default_audio_and_subtitles_off(self):
        self.widget.set_candidate(media())
        self.assertEqual(self.widget.selection(), {'audio_id': 'audio-en', 'subtitle_ids': [],
            'subtitle_mode': 'none', 'container': 'original'})
        self.assertIn('英语 (en)', self.widget.audio_combo.currentText())
        self.assertIn('2 声道', self.widget.audio_combo.currentText())
        self.assertIn('48 kHz', self.widget.audio_combo.currentText())
        self.assertIn('128 kb/s', self.widget.audio_combo.currentText())

    def test_switching_candidate_clears_even_colliding_previous_track_ids(self):
        self.widget.set_candidate(media('first'))
        self.choose_audio('audio-ja')
        self.subtitle('subtitle-zh').setCheckState(Qt.CheckState.Checked)
        self.choose_mode('mux')
        self.choose_container('mp4')
        self.widget.set_candidate(media('second'))
        self.assertEqual(self.widget.selection(), {'audio_id': 'audio-en', 'subtitle_ids': [],
            'subtitle_mode': 'none', 'container': 'original'})
        self.assertEqual(self.subtitle('subtitle-zh').checkState(), Qt.CheckState.Unchecked)

    def test_valid_saved_choice_for_this_catalog_is_used_without_mutating_candidate(self):
        selected = {'audio_id': 'audio-ja', 'subtitle_ids': ['subtitle-zh'],
                    'subtitle_mode': 'mux', 'container': 'mkv'}
        candidate = media(metadata={'track_selection': selected})
        before = deepcopy(candidate)
        self.widget.set_candidate(candidate)
        self.assertEqual(self.widget.selection(), selected)
        self.assertEqual(candidate, before)

    def test_saved_cross_media_or_unsupported_choice_falls_back_to_actual_defaults(self):
        for audio_id in ('another-media-id', 'audio-bad'):
            with self.subTest(audio_id=audio_id):
                self.widget.set_candidate(media(audio_id, metadata={'track_selection': {
                    'audio_id': audio_id, 'subtitle_ids': [], 'subtitle_mode': 'none', 'container': 'original'}}))
                self.assertEqual(self.widget.selection()['audio_id'], 'audio-en')

    def test_refresh_of_same_candidate_keeps_valid_manual_choices(self):
        candidate = media()
        self.widget.set_candidate(candidate)
        self.choose_audio('audio-ja')
        self.subtitle('subtitle-zh').setCheckState(Qt.CheckState.Checked)
        selected = self.widget.selection()
        self.widget.set_candidate(deepcopy(candidate))
        self.assertEqual(self.widget.selection(), selected)

    def test_declared_preference_is_applied_and_missing_audio_requires_manual_choice(self):
        self.widget.set_candidate(media(), {'audio_language': 'ja'})
        self.assertEqual(self.widget.selection()['audio_id'], 'audio-ja')
        self.widget.set_candidate(media('missing'), {'audio_language': 'de'})
        self.assertIn('德语 (de)', self.widget.summary.text())
        with self.assertRaisesRegex(ValueError, '音轨语言未找到'):
            self.widget.selection()
        self.choose_audio('audio-ja')
        self.assertEqual(self.widget.selection()['audio_id'], 'audio-ja')

    def test_missing_subtitle_language_is_not_replaced_silently(self):
        self.widget.set_candidate(media(), {'subtitle_mode': 'sidecar', 'subtitle_languages': ['fr']})
        self.assertIn('法语 (fr)', self.widget.summary.text())
        self.assertIn('未替换', self.widget.summary.text())
        self.assertEqual(self.subtitle('subtitle-zh').checkState(), Qt.CheckState.Unchecked)
        with self.assertRaisesRegex(ValueError, '字幕语言未找到'):
            self.widget.selection()
        self.subtitle('subtitle-zh').setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.widget.selection()['subtitle_ids'], ['subtitle-zh'])

    def test_explicit_language_preferences_use_real_supported_subtitles(self):
        self.widget.set_candidate(media(), {'subtitle_mode': 'sidecar', 'subtitle_languages': ['zh-Hans']})
        self.assertEqual(self.widget.selection()['subtitle_ids'], ['subtitle-zh'])
        self.assertIn('简体中文 (zh-Hans)', self.widget.summary.text())

    def test_missing_subtitle_does_not_replace_an_available_preferred_audio(self):
        self.widget.set_candidate(media(), {'audio_language': 'ja',
            'subtitle_mode': 'sidecar', 'subtitle_languages': ['fr']})
        self.assertEqual(self.widget.audio_combo.currentData(), 'audio-ja')
        with self.assertRaisesRegex(ValueError, '字幕语言未找到'):
            self.widget.selection()
        self.choose_mode('none')
        self.assertEqual(self.widget.selection()['audio_id'], 'audio-ja')

    def test_unsupported_items_show_reason_but_are_not_selectable(self):
        self.widget.set_candidate(media())
        index = self.widget.audio_combo.findData('audio-bad')
        self.assertFalse(self.widget.audio_combo.model().item(index).isEnabled())
        self.assertIn('没有完整资源', self.widget.audio_combo.itemText(index))
        item = self.subtitle('subtitle-bad')
        self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsEnabled)
        self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertIn('暂不支持', item.text())
        self.choose_audio('audio-bad')
        with self.assertRaisesRegex(ValueError, '没有完整资源'):
            self.widget.selection()

    def test_checking_subtitle_is_explicit_sidecar_and_none_clears_choices(self):
        self.widget.set_candidate(media())
        self.subtitle('subtitle-zh').setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.widget.selection()['subtitle_mode'], 'sidecar')
        self.choose_mode('none')
        self.assertEqual(self.widget.selection()['subtitle_ids'], [])

    def test_mp4_soft_subtitle_conversion_is_explained_without_video_transcode(self):
        self.widget.set_candidate(media())
        self.subtitle('subtitle-zh').setCheckState(Qt.CheckState.Checked)
        self.choose_mode('mux')
        self.choose_container('mp4')
        self.assertFalse(self.widget.conversion_hint.isHidden())
        self.assertIn('mov_text', self.widget.conversion_hint.text())
        self.assertIn('仅转换字幕', self.widget.conversion_hint.text())
        self.assertIn('视频保持原有编码', self.widget.conversion_hint.text())
        self.choose_container('mkv')
        self.assertTrue(self.widget.conversion_hint.isHidden())

    def test_original_name_language_and_automatic_flag_are_plain_text(self):
        values = {'known': True, 'audio': [track('one', 'xx-YY', name='<b>Original</b>', default=True)],
            'subtitles': [track('manual', 'zh-Hant', subtitle=True, automatic=False),
                          track('generated', 'en', subtitle=True, automatic=True),
                          track('unknown', '', subtitle=True, automatic=None)]}
        self.widget.set_candidate(media(metadata={'track_catalog': values}))
        self.assertIn('xx-YY', self.widget.audio_combo.currentText())
        self.assertIn('<b>Original</b>', self.widget.audio_combo.currentText())
        self.assertIn('<b>Original</b>', self.widget.summary.text())
        self.assertEqual(self.widget.summary.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.widget._tooltip.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('人工字幕', self.subtitle('manual').text())
        self.assertIn('自动字幕', self.subtitle('generated').text())
        self.assertIn('生成方式未知', self.subtitle('unknown').text())

    def test_selection_returns_detached_ids_only_and_emits_user_changes(self):
        spy = QSignalSpy(self.widget.changed)
        self.widget.set_candidate(media())
        self.assertEqual(spy.count(), 0)
        self.subtitle('subtitle-zh').setCheckState(Qt.CheckState.Checked)
        self.assertGreater(spy.count(), 0)
        first = self.widget.selection()
        first['subtitle_ids'].clear()
        self.assertEqual(self.widget.selection()['subtitle_ids'], ['subtitle-zh'])
        self.assertEqual(set(self.widget.selection()), {'audio_id', 'subtitle_ids', 'subtitle_mode', 'container'})

    def test_all_interactions_keep_candidate_payload_and_score_unchanged_without_network(self):
        candidate = media(confidenceScore=66)
        before, payload = deepcopy(candidate), deepcopy(candidate.download_payload())
        with patch('socket.create_connection', side_effect=AssertionError('禁止网络')):
            self.widget.set_candidate(candidate)
            self.choose_audio('audio-ja')
            self.subtitle('subtitle-zh').setCheckState(Qt.CheckState.Checked)
            self.choose_mode('mux')
            self.choose_container('mkv')
            self.widget.selection()
        self.assertEqual(candidate, before)
        self.assertEqual(candidate.download_payload(), payload)
        self.assertEqual(candidate.confidenceScore, 66)


    def test_missing_catalog_hides_unusable_track_operations(self):
        self.widget.set_candidate(media(metadata={'track_catalog': {'audio': [], 'subtitles': [], 'known': False}}))
        self.assertTrue(self.widget.audio_combo.isHidden())
        self.assertTrue(self.widget.mode_combo.isHidden())
        self.assertIn('下载后检查实际轨道', self.widget.summary.text())
        self.widget.set_candidate(media())
        self.assertFalse(self.widget.audio_combo.isHidden())
        self.assertFalse(self.widget.mode_combo.isHidden())


if __name__ == '__main__':
    unittest.main()
