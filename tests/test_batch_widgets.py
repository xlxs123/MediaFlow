"""使用真实 Qt 控件验证批量预览与元数据视图；不发起网络下载。"""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog

from ui.batch_import import BatchImportDialog, source_display
from ui.batch_panel import BatchPanel


def _item(identity, batch='batch-one', state='waiting_discovery', **extra):
    return {'id': identity, 'batch_id': batch, 'ordinal': 1, 'title': identity,
            'source_site': 'example.com', 'state': state, 'candidate_count': 0,
            'percent': None, 'size': None, 'speed': None, 'error': '',
            'output_path': '', **extra}


class BatchWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            if isinstance(widget, QDialog):
                widget.reject()
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def dialog(self, **kwargs):
        dialog = BatchImportDialog(**kwargs)
        self.widgets.append(dialog)
        return dialog

    def panel(self, items=(), batches=None):
        panel = BatchPanel()
        self.widgets.append(panel)
        panel.set_snapshot(batches or [{'id': 'batch-one', 'title': '测试批次', 'paused': False}], items)
        return panel

    def pump_until(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail('后台控件操作未在限时内完成')

    def test_paste_only_previews_without_accepting_or_submitting(self):
        dialog = self.dialog()
        accepted = QSignalSpy(dialog.accepted)
        dialog.editor.setPlainText(' https://example.com/one\n\nhttps://example.com/two ')
        QTest.qWait(190)
        self.app.processEvents()
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertEqual(dialog.result_urls, [])
        self.assertEqual(dialog.options, {})
        self.assertEqual(accepted.count(), 0)
        self.assertIn('有效 2 条', dialog.summary.text())
        self.assertTrue(dialog.confirm_button.isEnabled())

    def test_exact_duplicate_invalid_and_existing_counts(self):
        dialog = self.dialog(existing=['https://example.com/existing'])
        dialog.editor.setPlainText('\nhttps://example.com/one\nhttps://example.com/one\n'
            'ftp://example.com/not-media\nhttps://example.com/existing\nnot a URL\n')
        dialog.refresh_preview()
        self.assertEqual(dialog._preview['urls'], ['https://example.com/one'])
        self.assertEqual(dialog._preview['duplicates'], 2)
        self.assertEqual(dialog._preview['invalid'], 2)
        self.assertGreaterEqual(dialog._preview['empty'], 1)
        self.assertIn('重复 2 条', dialog.summary.text())

    def test_confirmation_preserves_distinct_work_ids_and_complete_query(self):
        one = 'https://www.douyin.com/user/self?modal_id=7526879737634950451&token=secret-one'
        two = 'https://www.douyin.com/user/self?modal_id=7657489271422549747&token=secret-two'
        dialog = self.dialog(defaults={'output_directory': 'G:/media', 'quality': 'balanced',
            'naming': 'date_title', 'max_concurrent': 4, 'retry_limit': 2, 'discovery_seconds': 30})
        dialog.editor.setPlainText(one + '\n' + two)
        accepted = QSignalSpy(dialog.accepted)
        dialog.accept()
        self.assertEqual(accepted.count(), 1)
        self.assertEqual(dialog.result_urls, [one, two])
        self.assertEqual(dialog.options, {'output_directory': 'G:/media', 'quality': 'balanced',
            'naming': 'date_title', 'max_concurrent': 4, 'retry_limit': 2, 'discovery_seconds': 30})

    def test_invalid_empty_or_global_error_cannot_accept(self):
        dialog = self.dialog()
        accepted = QSignalSpy(dialog.accepted)
        for text in ('', 'not a URL', '\n\n'):
            dialog.editor.setPlainText(text)
            dialog.accept()
            self.assertFalse(dialog.confirm_button.isEnabled())
        with patch('ui.batch_import.parse_urls', return_value={
                'urls': ['https://example.com/one'], 'rows': [], 'valid': 1,
                'error': '输入数量超出允许范围'}):
            dialog.accept()
            self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertEqual(accepted.count(), 0)
        self.assertEqual(dialog.result_urls, [])

    def test_selected_preview_rows_removed_from_source_text(self):
        dialog = self.dialog()
        dialog.editor.setPlainText('https://example.com/one\n\nhttps://example.com/two\nhttps://example.com/three')
        dialog.refresh_preview()
        dialog.table.selectRow(1)
        dialog.remove_button.click()
        self.assertEqual(dialog._preview['urls'], ['https://example.com/one', 'https://example.com/three'])
        self.assertNotIn('https://example.com/two', dialog.editor.toPlainText())
        self.assertFalse(dialog.remove_button.isEnabled())

    def test_sensitive_query_and_credentials_not_in_preview_or_tooltips(self):
        url = 'https://user:password@example.com/watch?modal_id=42&token=secret-value&signature=private-value#private-fragment'
        safe = source_display(url)
        for secret in ('password', 'secret-value', 'private-value', 'private-fragment'):
            self.assertNotIn(secret, safe)
        self.assertIn('modal_id=42', safe)
        dialog = self.dialog()
        # Backend rejects credentials; use a legal signed URL to inspect the normal preview.
        url = 'https://example.com/watch?modal_id=42&token=secret-value&signature=private-value'
        dialog.editor.setPlainText(url)
        dialog.refresh_preview()
        for row in range(dialog.table.rowCount()):
            for column in range(dialog.table.columnCount()):
                cell = dialog.table.item(row, column)
                self.assertNotIn('secret-value', cell.text() + cell.toolTip())
                self.assertNotIn('private-value', cell.text() + cell.toolTip())

    def test_txt_import_is_background_and_does_not_accept(self):
        dialog = self.dialog()
        accepted = QSignalSpy(dialog.accepted)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'urls.txt'
            path.write_text('\ufeffhttps://example.com/one\nhttps://example.com/two', encoding='utf-8')
            self.assertTrue(dialog.load_file(str(path)))
            self.assertFalse(dialog.confirm_button.isEnabled())
            self.assertFalse(dialog.load_file(str(path)))
            self.pump_until(lambda: not dialog._reading)
        self.assertEqual(dialog._preview['valid'], 2)
        self.assertEqual(dialog.result_urls, [])
        self.assertEqual(accepted.count(), 0)

    def test_txt_read_failure_is_clear_and_prevents_submission(self):
        dialog = self.dialog()
        dialog.editor.setPlainText('https://example.com/one')
        with patch('ui.batch_import.read_url_file', side_effect=ValueError('TXT 文件超过 2 MiB，请拆分文件。')):
            dialog.load_file('unused.txt')
            self.pump_until(lambda: not dialog._reading)
        self.assertIn('超过 2 MiB', dialog.summary.text())
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.editor.appendPlainText('https://example.com/two')
        dialog.refresh_preview()
        self.assertTrue(dialog.confirm_button.isEnabled())

    def test_closing_during_slow_txt_read_keeps_ui_responsive(self):
        dialog = self.dialog()
        release = threading.Event()
        finished = threading.Event()

        def read(_):
            release.wait(2)
            finished.set()
            return 'https://example.com/one'

        ticks = []
        timer = QTimer(dialog)
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(1))
        with patch('ui.batch_import.read_url_file', side_effect=read):
            dialog.load_file('slow.txt')
            timer.start()
            QTest.qWait(45)
            dialog.reject()
            self.assertGreaterEqual(len(ticks), 2)
            self.assertEqual(dialog.result_urls, [])
            release.set()
            self.pump_until(finished.is_set)
            self.app.processEvents()

    def test_configurable_limits_match_existing_download_capabilities(self):
        dialog = self.dialog()
        self.assertEqual((dialog.max_concurrent.minimum(), dialog.max_concurrent.maximum()), (1, 10))
        self.assertEqual((dialog.retry_limit.minimum(), dialog.retry_limit.maximum()), (0, 3))
        self.assertEqual((dialog.discovery_seconds.minimum(), dialog.discovery_seconds.maximum()), (5, 120))
        self.assertEqual(dialog.discovery_seconds.value(), 60)

    def test_state_and_batch_filtering_then_reset(self):
        panel = self.panel([_item('one', state='failed'), _item('two', batch='batch-two'),
            _item('three', state='completed')], batches=[{'id': 'batch-one', 'title': '批次甲'},
                                                      {'id': 'batch-two', 'title': '批次乙'}])
        panel.batch_filter.setCurrentIndex(panel.batch_filter.findData('batch-one'))
        self.assertEqual(panel.table.rowCount(), 2)
        panel.failed_button.click()
        self.assertEqual([item['id'] for item in panel.page_items], ['one'])
        panel.reset_button.click()
        self.assertEqual(panel.table.rowCount(), 3)

    def test_item_and_batch_actions_have_separate_targets_and_signals(self):
        panel = self.panel([_item('task-one', state='downloading')])
        item_spy, batch_spy = QSignalSpy(panel.item_action), QSignalSpy(panel.batch_action)
        self.assertTrue(panel.select_item('task-one'))
        panel.buttons['pause'].click()
        self.assertEqual(item_spy.at(0), ['pause', 'task-one'])
        self.assertEqual(batch_spy.count(), 0)
        panel.batch_buttons['pause'].click()
        self.assertEqual(batch_spy.at(0), ['pause', 'batch-one'])
        self.assertEqual(item_spy.count(), 1)

    def test_missing_progress_and_speed_are_never_invented(self):
        panel = self.panel([_item('one', state='analyzing', speed='old 12 MB/s'),
                            _item('two', state='downloading', percent=42.5, speed='1.2 MB/s', size=2048)])
        self.assertEqual([panel.table.item(0, column).text() for column in (4, 5, 6)], ['—', '—', '—'])
        self.assertEqual(panel.table.item(1, 4).text(), '42.5%')
        self.assertEqual(panel.table.item(1, 5).text(), '2.0 KB')
        self.assertEqual(panel.table.item(1, 6).text(), '1.2 MB/s')

    def test_many_records_only_create_one_page_of_table_items(self):
        panel = self.panel([_item(f'task-{number}') for number in range(2345)])
        self.assertEqual(panel.table.rowCount(), 50)
        self.assertEqual(len(panel.page_items), 50)
        panel.next.click()
        self.assertEqual(panel.page_items[0]['id'], 'task-50')
        self.assertTrue(panel.select_item('task-2344'))
        self.assertEqual(panel.table.rowCount(), 45)
        self.assertEqual(panel.selected_item()['id'], 'task-2344')
        self.assertFalse(panel.next.isEnabled())

    def test_long_names_and_error_text_stay_plain_with_complete_tooltips(self):
        title = '<img src=x onerror=alert(1)>' + '很长的媒体文件名称' * 30
        panel = self.panel([_item('one', title=title, error='<b>真实错误</b>', output_path='G:/完整路径/媒体.mp4')])
        self.assertEqual(panel.table.item(0, 0).text(), title)
        self.assertEqual(panel.table.item(0, 0).toolTip(), title)
        self.assertEqual(panel.table.textElideMode(), Qt.TextElideMode.ElideMiddle)
        self.assertEqual(panel.table._tip.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(panel.table.item(0, 7).text(), '<b>真实错误</b>')

    def test_candidate_confirmation_button_waits_for_user_and_is_distinct(self):
        panel = self.panel([_item('one', state='waiting_selection', candidate_count=3)])
        spy = QSignalSpy(panel.item_action)
        panel.select_item('one')
        self.assertTrue(panel.buttons['select'].isEnabled())
        self.assertFalse(panel.buttons['resume'].isEnabled())
        self.assertIn('需要确认', panel.selection_notice.text())
        self.assertEqual(spy.count(), 0)
        panel.buttons['select'].click()
        self.assertEqual(spy.at(0), ['select', 'one'])

    def test_batch_summary_includes_mixed_states_and_pause_flag(self):
        panel = self.panel([_item('one', state='completed'), _item('two', state='failed'),
            _item('three', state='paused')], batches=[{'id': 'batch-one', 'title': '混合批次', 'paused': True}])
        panel.batch_filter.setCurrentIndex(1)
        self.assertIn('已完成 1', panel.batch_summary.text())
        self.assertIn('下载失败 1', panel.batch_summary.text())
        self.assertIn('已暂停 1', panel.batch_summary.text())
        self.assertIn('整批已暂停', panel.batch_summary.text())
        self.assertFalse(panel.batch_buttons['pause'].isEnabled())
        self.assertTrue(panel.batch_buttons['resume'].isEnabled())
        self.assertTrue(panel.batch_buttons['retry_failed'].isEnabled())

    def test_snapshot_refresh_preserves_selection_without_emitting_actions(self):
        panel = self.panel([_item('one'), _item('two')])
        panel.select_item('two')
        item_spy, batch_spy = QSignalSpy(panel.item_action), QSignalSpy(panel.batch_action)
        panel.set_snapshot(panel.batches, [_item('one'), _item('two', state='downloading')])
        self.assertEqual(panel.selected_item()['id'], 'two')
        self.assertEqual(item_spy.count(), 0)
        self.assertEqual(batch_spy.count(), 0)

    def test_empty_state_and_buttons_do_not_offer_invalid_operations(self):
        panel = self.panel()
        self.assertFalse(panel.empty.isHidden())
        self.assertFalse(any(button.isEnabled() for button in panel.buttons.values()))
        self.assertFalse(any(button.isEnabled() for button in panel.batch_buttons.values()))
        self.assertFalse(panel.select_item('does-not-exist'))
        spy = QSignalSpy(panel.import_requested)
        panel.import_button.click()
        self.assertEqual(spy.count(), 1)


if __name__ == '__main__':
    unittest.main()
