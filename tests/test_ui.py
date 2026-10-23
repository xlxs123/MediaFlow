import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
from ui.main_window import MainWindow
from ui.dialogs import HeadersEditor, HeadersDialog, CurlDialog, DetectionDialog
from utils.config import ConfigStore
from core.yt_dlp_manager import ToolInfo


class FakeDownloadWorker(QObject):
    log = Signal(str)
    progress = Signal(dict)
    status = Signal(str)
    diagnostic = Signal(dict)
    completed = Signal(dict)
    finished = Signal()

    def __init__(self, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.running = False

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def request_stop(self):
        self.finish(cancelled=True)

    def finish(self, cancelled=False, error=''):
        if self.running:
            self.running = False
            self.completed.emit({'success': not cancelled and not error, 'cancelled': cancelled,
                                 'files': [] if cancelled or error else [str(Path(self.request.output_directory) / (self.request.filename + '.mp4'))],
                                 'error': error, 'diagnostic': {}})
            self.finished.emit()


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # 单元测试只检查控件；真正 yt-dlp/FFmpeg 检测由 GUI/EXE smoke 验收。
        self.tool_patches = [patch('ui.main_window.detect_yt_dlp', return_value=ToolInfo(error='unit test')),
                             patch('ui.main_window.detect_ffmpeg', return_value=ToolInfo(error='unit test'))]
        for tool_patch in self.tool_patches:
            tool_patch.start()
        self.window = MainWindow(ConfigStore(Path(self.temp.name) / 'settings.json'))

    def tearDown(self):
        deadline = time.monotonic() + 12
        self.window.queue.stop_all()
        self.app.processEvents()
        while (self.window.detector or self.window.queue.active_count or self.window.thumbnails.busy) and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.window.batch_controller.shutdown()
        self.window.close()
        deadline = time.monotonic() + 8
        while (not self.window.batch_controller.is_closed or not self.window.library_service.is_closed) and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertTrue(self.window.batch_controller.is_closed, '批次后台未在限时内结束')
        self.assertTrue(self.window.library_service.is_closed, '媒体库后台未在限时内结束')
        self.app.processEvents()
        self.temp.cleanup()
        for tool_patch in self.tool_patches:
            tool_patch.stop()

    def test_default_directory_and_controls(self):
        self.assertEqual(self.window.output_directory.text(), 'E:\\download')
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())
        self.assertFalse(self.window.advanced_toggle.isChecked())
        self.assertEqual(self.window.windowTitle(), 'MediaFlow')
        self.assertEqual(self.window.max_concurrent_videos.value(), 3)
        self.assertEqual(self.window.concurrent_fragments.value(), 4)

    def test_cmd_parse_and_sensitive_mask(self):
        text = 'curl --url ^"https://example.com/index.m3u8?sign=abc^&t=123^" -H ^"Origin: https://site.example^" -H ^"Cookie: secret=private-cookie^"'
        self.assertTrue(self.window.parse_text(text))
        self.assertEqual(self.window.url.text(), 'https://example.com/index.m3u8?sign=abc&t=123')
        self.assertEqual(self.window.origin.text(), 'https://site.example')
        self.assertEqual(self.window.custom_headers.cellWidget(0, 1).echoMode(), QLineEdit.EchoMode.Password)
        self.assertNotIn('private-cookie', self.window.log_view.toPlainText())
        self.assertNotIn('private-cookie', self.window.config_store.path.read_text(encoding='utf-8'))

    def test_bash_parse_and_fields_editable(self):
        self.assertTrue(self.window.parse_text("curl 'https://example.com/index.m3u8' -H 'user-agent: Actual Browser (Windows)'"))
        self.window.user_agent.setText('Edited Agent')
        self.assertEqual(self.window.collect_headers()['user-agent'], 'Edited Agent')

    def test_multiple_media_selection(self):
        with patch('ui.main_window.QInputDialog.getItem', return_value=('https://x/b.m3u8', True)) as choose:
            self.assertTrue(self.window.parse_text("curl 'https://x/a.m3u8' 'https://x/b.m3u8'"))
            choose.assert_called_once()
        self.assertEqual(self.window.url.text(), 'https://x/b.m3u8')

    def test_parser_errors_are_chinese(self):
        with patch('ui.main_window.QMessageBox.warning') as warning:
            self.assertFalse(self.window.parse_text('not curl'))
            self.assertIn('未检测到', warning.call_args.args[2])

    def test_invalid_input_is_reported_without_a_global_tool_gate(self):
        with patch('ui.main_window.QMessageBox.warning') as warning:
            self.window.start_download()
            self.assertIn('URL', warning.call_args.args[2])

    def test_url_keeps_existing_origin(self):
        self.window.origin.setText('https://existing.example')
        QApplication.clipboard().setText('https://x/a.m3u8?sign=abc&t=123')
        self.window.read_clipboard_field(self.window.url)
        self.assertEqual(self.window.origin.text(), 'https://existing.example')
        self.assertIn('&t=123', self.window.url.text())

    def test_signature_visibility(self):
        self.window.url.setText('https://x/a.m3u8?token=abc')
        self.assertFalse(self.window.signature_hint.isHidden())
        self.window.url.setText('https://x/a.m3u8')
        self.assertTrue(self.window.signature_hint.isHidden())

    def test_header_edit_delete_and_reveal(self):
        table = HeadersEditor({'Cookie': 'session=secret'})
        self.assertEqual(table.cellWidget(0, 1).echoMode(), QLineEdit.EchoMode.Password)
        table.set_reveal(True)
        self.assertEqual(table.cellWidget(0, 1).echoMode(), QLineEdit.EchoMode.Normal)
        table.setCurrentCell(0, 0)
        table.delete_selected()
        self.assertEqual(table.rowCount(), 0)

    def test_progress_unknown_and_partial_updates(self):
        self.window.update_progress({'fragment_count': 73})
        self.window.update_progress({'percent': 45, 'fragment_index': 34, 'speed': '2.4 MiB/s', 'eta': '00:18'})
        self.assertEqual(self.window.progress_bar.value(), 450)
        self.assertIn('34 / 73', self.window.progress_detail.text())
        self.window.update_progress({'fragment_index': None, 'fragment_count': None, 'percent': 100})
        self.assertIn('34 / 73', self.window.progress_detail.text())

    def test_logs_clear(self):
        self.window.append_log('Hello log')
        self.window.clear_log()
        self.assertEqual(self.window.log_view.toPlainText(), '')

    def test_brand_about_and_log_export_use_mediaflow_names(self):
        self.assertEqual(self.window.subtitle.text(), '智能媒体发现与下载工具')
        self.assertEqual(self.window.english_subtitle.text(), 'Intelligent Media Discovery & Download Tool')
        self.assertEqual(self.window.log_toggle.text(), 'MediaFlow 下载日志')
        with patch('ui.main_window.QMessageBox.exec'):
            self.window.show_about()
        dialog = self.window.findChildren(QMessageBox)[-1]
        self.assertEqual(dialog.windowTitle(), '关于 MediaFlow')
        self.assertIn('Intelligent Media Discovery & Download Tool', dialog.text())
        self.window.append_log('GET https://example.com/video?token=private-token')
        target = Path(self.temp.name) / 'MediaFlow.log'
        with patch('ui.main_window.QFileDialog.getSaveFileName', return_value=(str(target), '')) as choose:
            self.window.export_log()
        self.assertEqual(Path(choose.call_args.args[2]).name, 'MediaFlow.log')
        self.assertTrue(target.is_file())
        self.assertNotIn('private-token', target.read_text(encoding='utf-8'))

    def test_cover_failure_keeps_original_download_success_and_completion_event(self):
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        self.window.url.setText('https://api.example/video.mp4')
        events = []
        self.window.task_completed.connect(events.append)
        with patch('ui.main_window.DownloadWorker', side_effect=FakeDownloadWorker), patch(
                'ui.thumbnail_service.generate_thumbnail', return_value={
                    'status': 'failed', 'path': '', 'message': 'unit cover failure', 'attempts': [1, 3, 5]}):
            self.window.start_download()
            job = self.window.queue.jobs[0]
            worker = job.worker
            worker.running = False
            result = {'success': True, 'cancelled': False, 'error': '', 'diagnostic': {},
                      'files': [str(Path(self.temp.name) / 'video.mp4')],
                      'output_validation': [{'format_name': 'mov,mp4', 'video_streams': [{'index': 0}]}]}
            worker.completed.emit(result)
            worker.finished.emit()
            deadline = time.monotonic() + 5
            while (job.worker is not None or job.id not in self.window.thumbnails.results or
                   self.window.thumbnails.busy) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertFalse(self.window.thumbnails.busy)
            self.assertEqual(job.state, 'completed')
            self.assertEqual(job.result, result)
            self.assertEqual(events, [result])
            self.assertEqual(self.window.progress_bar.value(), 1000)
            card = self.window._task_cards[job.id]
            self.assertIn('✓ 下载完成', card.completion_label.text())
            self.assertIn('✓ 媒体验证通过', card.completion_label.text())
            self.assertIn('⚠ 缩略图生成失败', card.completion_label.text())
            self.assertTrue(card.thumbnail.pixmap().isNull())

    def test_raw_url_unified_parse(self):
        self.assertTrue(self.window.parse_text('https://api.example/endpoint?sign=a%2Fb&t=123'))
        self.assertIn('?sign=a%2Fb&t=123', self.window.url.text())

    def test_post_body_parsed_and_reset_on_url_change(self):
        self.assertTrue(self.window.parse_text("curl 'https://api.example/manifest' --data-raw 'auth=private-body'"))
        self.assertEqual(self.window._request_method, 'POST')
        self.assertEqual(self.window._request_body, b'auth=private-body')
        self.assertNotIn('private-body', self.window.parse_summary.text())
        self.assertNotIn('private-body', self.window.config_store.path.read_text(encoding='utf-8'))
        self.window.url.setText('https://api.example/next')
        self.assertEqual(self.window._request_method, 'GET')
        self.assertIsNone(self.window._request_body)

    def test_loading_same_url_as_plain_input_clears_previous_body(self):
        url = 'https://api.example/endpoint'
        self.window.parse_text(f"curl '{url}' --data-raw 'private-body'")
        self.assertTrue(self.window.parse_text('\"' + url + '\"'))
        self.assertEqual(self.window._request_method, 'GET')
        self.assertIsNone(self.window._request_body)

    def test_detection_panel_aes_and_http403_separate(self):
        self.window.update_diagnostic({'media_type': 'HLS', 'playlist_type': 'Media', 'encryption': 'AES-128',
                                       'drm': False, 'http_status': 200, 'classification': 'HLS_AES128',
                                       'keys': [{'method': 'AES-128', 'keyformat': 'identity', 'uri': 'key.dat', 'iv': '0x00'}]})
        dialog = DetectionDialog(self.window._diagnostic)
        self.assertIn('DRM：No', dialog.editor.toPlainText())
        self.assertIn('METHOD：AES-128', dialog.editor.toPlainText())
        self.window.update_diagnostic({'http_status': 403, 'drm': None, 'error_code': 'HTTP_403'})
        dialog = DetectionDialog(self.window._diagnostic)
        self.assertIn('DRM：Unknown', dialog.editor.toPlainText())
        self.assertIn('HTTP：403', dialog.editor.toPlainText())

    def test_segment_semantics_and_browser_metadata_in_detection_panel(self):
        self.window.update_diagnostic({'media_type': 'HLS', 'manifest_type': 'Media', 'playlist_type': 'VOD',
                                       'hls_playlist_type': 'VOD', 'encryption': 'AES-128', 'drm': False,
                                       'resources': [{'role': 'Media Segment', 'uri': 'abc000.png?token=private-fragment'}],
                                       'browser_metadata': {'sec-fetch-mode': 'cors'}, 'inherit_browser_metadata': False})
        dialog = DetectionDialog(self.window._diagnostic)
        detail = dialog.editor.toPlainText()
        self.assertIn('Manifest：Media', detail)
        self.assertIn('HLS 播放类型：VOD', detail)
        self.assertIn('Media Segment：abc000.png', detail)
        self.assertIn('sec-fetch-mode：cors', detail)
        self.assertIn('默认不发送', detail)
        self.assertNotIn('private-fragment', detail)

    def test_browser_metadata_compatibility_is_explicit_and_not_persisted(self):
        self.assertFalse(self.window.inherit_browser_metadata.isChecked())
        self.window.inherit_browser_metadata.setChecked(True)
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        self.window.url.setText('https://api.example/manifest')
        with patch('ui.main_window.DownloadWorker', side_effect=FakeDownloadWorker) as worker_type:
            self.window.start_download()
            self.assertTrue(worker_type.call_args.args[0].inherit_browser_metadata)
        self.assertNotIn('inherit_browser_metadata', self.window.config_store.path.read_text(encoding='utf-8'))

    def test_observed_cmd_bash_enable_range_without_origin_or_credentials(self):
        commands = ["curl 'https://cdn.example/index.m3u8' -H 'Referer: https://page.example/watch' -H 'User-Agent: Browser' -H 'Range: bytes=0-'",
                    'curl ^"https://cdn.example/index.m3u8^" -H ^"Referer: https://page.example/watch^" -H ^"User-Agent: Browser^" -H ^"Range: bytes=0-^"']
        for command in commands:
            self.assertTrue(self.window.parse_text(command))
            self.assertTrue(self.window.inherit_range.isChecked())
            self.assertEqual(self.window.collect_headers(), {'referer': 'https://page.example/watch',
                             'user-agent': 'Browser', 'range': 'bytes=0-'})
            self.assertIn('Range 继承：已开启', self.window.parse_summary.text())
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        with patch('ui.main_window.DownloadWorker', side_effect=FakeDownloadWorker) as worker_type:
            self.window.start_download()
            self.assertTrue(worker_type.call_args.args[0].inherit_range)
        self.assertNotIn('inherit_range', self.window.config_store.path.read_text(encoding='utf-8'))

    def test_partial_or_absent_range_does_not_auto_enable_inheritance(self):
        self.window.inherit_range.setChecked(True)
        for range_arg in ('', "-H 'Range: bytes=8-23'", "-H 'Range: bytes=-16'"):
            self.assertTrue(self.window.parse_text("curl 'https://cdn.example/index.m3u8' " + range_arg))
            self.assertFalse(self.window.inherit_range.isChecked())

    def test_range_isolation_can_be_selected_after_import(self):
        self.window.parse_text("curl 'https://cdn.example/index.m3u8' -H 'Range: bytes=0-'")
        self.window.inherit_range.setChecked(False)
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        with patch('ui.main_window.DownloadWorker', side_effect=FakeDownloadWorker) as worker_type:
            self.window.start_download()
            request = worker_type.call_args.args[0]
            self.assertFalse(request.inherit_range)
            self.assertNotIn('range', request.to_context().derive('enc.key').headers)

    def test_diagnostics_show_range_strategy_and_absent_origin(self):
        self.window.update_diagnostic({'media_type': 'HLS', 'drm': False, 'origin': '', 'request_range': 'bytes=0-',
                                       'inherit_range': True, 'header_categories': {'resource': ['range']}})
        dialog = DetectionDialog(self.window._diagnostic)
        detail = dialog.editor.toPlainText()
        self.assertIn('请求 Range：bytes=0-', detail)
        self.assertIn('Range 继承：开启', detail)
        self.assertIn('Origin：--', detail)
        self.assertIn('条件请求 / 正文头不继承', detail)

    def test_batch_urls_use_get_auto_names_and_snapshot_headers(self):
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        self.window.parse_text("curl 'https://api.example/a' -H 'Cookie: session=private-a' --data-raw 'auth=private-body'")
        self.window.filename.setText('shared name')
        jobs = self.window.add_batch_urls('https://api.example/a?sign=private-1\nhttps://api.example/b\n\nhttps://api.example/c')
        self.assertEqual(len(jobs), 3)
        self.assertEqual(self.window.queue_table.rowCount(), 3)
        self.assertTrue(self.window.queue.paused)
        self.assertEqual([job.name for job in jobs], ['video001', 'video002', 'video003'])
        self.window.set_headers({'Cookie': 'session=private-b'})
        for job in jobs:
            self.assertEqual(job.request.method, 'GET')
            self.assertIsNone(job.request.body)
            self.assertEqual(job.request.headers['cookie'], 'session=private-a')
            self.assertEqual(job.request.concurrent_fragments, 4)
        self.assertNotIn('private-', self.window.queue_table.item(0, 0).text())
        self.assertNotIn('private-', self.window.config_store.path.read_text(encoding='utf-8'))

    def test_bad_batch_is_atomic(self):
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        with patch('ui.main_window.QMessageBox.warning'):
            self.assertEqual(self.window.add_batch_urls('https://api.example/good\nnot-a-url'), [])
            self.assertEqual(self.window.add_batch_urls("curl 'https://api.example/a' -H 'Cookie: session=private'"), [])
        self.assertEqual(self.window.queue.jobs, [])

    def test_parallel_ui_keeps_input_editable_and_isolates_selection(self):
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        with patch('ui.main_window.DownloadWorker', side_effect=FakeDownloadWorker):
            self.window.parse_text("curl 'https://api.example/a' -H 'Cookie: session=private-a'")
            self.window.start_download()
            first = self.window.queue.jobs[0]
            self.window.parse_text("curl 'https://api.example/b' -H 'Cookie: session=private-b'")
            self.window.start_download()
            second = self.window.queue.jobs[1]
            self.assertEqual(self.window.queue.active_count, 2)
            self.assertTrue(self.window.start_button.isEnabled())
            self.assertTrue(self.window.url.isEnabled())
            first.worker.log.emit('A private-a masked')
            second.worker.log.emit('B private-b masked')
            first.worker.progress.emit({'percent': 25, 'fragment_index': 1, 'fragment_count': 4})
            second.worker.progress.emit({'percent': 50, 'fragment_index': 2, 'fragment_count': 4})
            self.app.processEvents()
            self.window.select_job(first.id)
            self.assertEqual(self.window.progress_bar.value(), 250)
            self.assertIn('A', self.window.log_view.toPlainText())
            self.assertNotIn('private-a', self.window.log_view.toPlainText())
            self.assertNotIn('B', self.window.log_view.toPlainText())
            self.window.select_job(second.id)
            self.assertEqual(self.window.progress_bar.value(), 500)
            self.assertNotIn('private-b', self.window.log_view.toPlainText())
            self.assertNotIn('A', self.window.log_view.toPlainText())
            self.window.stop_download()
            self.app.processEvents()
            self.assertEqual(second.state, 'cancelled')
            self.assertTrue(first.worker.isRunning())
            self.assertTrue(self.window.retry_button.isEnabled())
            self.window.pause_queue()
            self.window.retry_selected()
            self.assertEqual(second.state, 'queued')

    def test_fragment_setting_is_saved_but_only_changes_new_tasks(self):
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        self.window.url.setText('https://api.example/manifest')
        first = self.window.add_current_to_queue()
        self.window.concurrent_fragments.setValue(8)
        self.window.max_concurrent_videos.setValue(2)
        second = self.window.add_current_to_queue()
        self.assertEqual(first.request.concurrent_fragments, 4)
        self.assertEqual(second.request.concurrent_fragments, 8)
        self.assertEqual(second.name, 'video002')
        settings = self.window.config_store.load()
        self.assertEqual(settings['concurrent_fragments'], 8)
        self.assertEqual(settings['max_concurrent_videos'], 2)

    def test_completed_events_once_and_finished_selection_can_be_removed(self):
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        self.window.url.setText('https://api.example/a')
        events = []
        self.window.task_completed.connect(events.append)
        with patch('ui.main_window.DownloadWorker', side_effect=FakeDownloadWorker):
            self.window.start_download()
            job = self.window.queue.jobs[0]
            job.worker.finish(error='unit failure')
            self.app.processEvents()
            self.window.select_job(job.id)
            self.assertEqual(len(events), 1)
            self.assertTrue(self.window.full_error_button.isEnabled())
            self.window.remove_finished_jobs()
            self.assertEqual(self.window.queue_table.rowCount(), 0)
            self.assertIsNone(self.window.selected_job_id)
            self.assertIsNone(self.window.last_result)
            self.assertFalse(self.window.full_error_button.isEnabled())

    def test_selecting_waiting_job_clears_completed_percentage(self):
        self.window.yt_info = ToolInfo(path='tools/yt-dlp.exe', version='unit-test')
        self.window.url.setText('https://api.example/a')
        with patch('ui.main_window.DownloadWorker', side_effect=FakeDownloadWorker):
            self.window.start_download()
            first = self.window.queue.jobs[0]
            first.worker.progress.emit({'fragment_count': 4, 'fragment_index': 3})
            first.worker.finish()
            self.app.processEvents()
            self.assertEqual(self.window.queue_table.item(0, 4).text(), '4 / 4')
            self.assertEqual(self.window.progress_bar.value(), 1000)
            self.window.pause_queue()
            self.window.url.setText('https://api.example/b')
            self.window.add_current_to_queue()
            self.assertEqual(self.window.progress_bar.value(), 0)
            self.assertNotIn('100', self.window.progress_bar.text())


if __name__ == '__main__':
    unittest.main()
