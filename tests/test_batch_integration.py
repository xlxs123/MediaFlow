"""主窗口接线、独立请求上下文和既有下载历史的集成边界。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from dataclasses import replace
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from core.media_groups import group_media
from tests.test_batch_controller import SerialDriver, media
from tests.test_download_queue import WorkerFactory
from ui.main_window import MainWindow
from utils.config import ConfigStore


class BatchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate, timeout=6):
        end = time.monotonic() + timeout
        while not predicate():
            self.app.processEvents()
            time.sleep(.002)
            if time.monotonic() >= end:
                self.fail('主窗口批次后台操作超时。')
        self.app.processEvents()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = ConfigStore(self.root / 'config.json')
        self.driver = SerialDriver()
        self.patches = [patch.object(MainWindow, 'detect_tools'),
                        patch('ui.batch_discovery.SerialBatchDiscovery', return_value=self.driver)]
        for current in self.patches:
            current.start()
        self.window = MainWindow(self.config)
        self.factory = WorkerFactory()
        self.window.queue._worker_factory = self.factory
        self.wait(lambda: self.window.batch_controller.ready)

    def tearDown(self):
        self.window.batch_controller.shutdown()
        for worker in tuple(self.window.queue._workers):
            worker.finish(success=False, cancelled=True)
        self.wait(lambda: self.window.batch_controller.is_closed)
        self.window.library_service.shutdown()
        self.wait(lambda: self.window.library_service.is_closed)
        self.window.close()
        for current in reversed(self.patches):
            current.stop()
        self.temp.cleanup()

    def submit(self, url='https://pages.example.test/first'):
        return self.window.submit_batch_urls([url], {'output_directory': str(self.root / 'media'),
            'naming': 'index_title', 'quality': 'highest', 'max_concurrent': 2,
            'retry_limit': 1, 'discovery_seconds': 5})

    def test_new_entry_uses_own_candidates_and_keeps_legacy_tabs(self):
        self.window.set_headers({'cookie': 'old-input-cookie', 'authorization': 'old-input-auth'})
        self.submit()
        self.assertIs(self.window.input_tabs.currentWidget(), self.window.batch_panel)
        self.assertIs(self.window.input_tabs.widget(2), self.window.media_library)
        self.assertEqual(self.window.queue.max_concurrent, 2)
        self.wait(lambda: self.driver.busy)
        candidate = media('first')
        candidate.request_context = replace(candidate.request_context,
            headers={'referer': 'https://pages.example.test/first'})
        self.driver.finish(group_media([candidate]))
        self.wait(lambda: bool(self.window.queue.jobs))
        job = self.window.queue.jobs[0]
        self.assertNotIn('cookie', job.request.headers)
        self.assertNotIn('authorization', job.request.headers)
        self.assertEqual(job.request.headers['referer'], 'https://pages.example.test/first')
        self.assertTrue(job.request.filename.startswith('001_'))
        self.assertEqual(job.request.recovery_max_attempts, 2)

    def test_batch_browser_lease_prevents_manual_dialog_from_stealing_page(self):
        self.submit()
        self.wait(lambda: self.driver.busy)
        with patch('ui.discovery_dialog.DiscoveryDialog') as create:
            self.assertIsNone(self.window.open_discovery(page_url='https://other.example.test/'))
            create.assert_not_called()
        self.assertIn('批次正在使用浏览器', self.window.status_label.text())

    def test_discovery_failure_enters_existing_history_with_safe_source(self):
        source = 'https://pages.example.test/first?token=not-for-history'
        self.submit(source)
        self.wait(lambda: self.driver.busy)
        self.driver.finish(error='此页未发现完整媒体')
        service = self.window.library_service
        pages = []
        service.page_ready.connect(lambda _, page: pages.append(page))
        service.request_page(view='history')
        self.wait(lambda: bool(pages) and pages[-1]['total'] == 1)
        entry = pages[-1]['items'][0]
        self.assertEqual(entry['task_state'], 'failed')
        self.assertIn('未发现完整媒体', entry['error'])
        self.assertNotIn('not-for-history', entry['source_url'])
        self.assertEqual(len(self.window.queue.jobs), 0)

    def test_batch_preferences_persist_without_source_or_secrets(self):
        self.submit('https://pages.example.test/first?token=private-source')
        restored = self.config.load()
        self.assertEqual(restored['batch_import']['naming'], 'index_title')
        self.assertEqual(restored['batch_import']['retry_limit'], 1)
        self.assertEqual(restored['max_concurrent_videos'], 2)
        self.assertNotIn('private-source', self.config.path.read_text(encoding='utf-8'))
        values = dict(restored, batch_import={'url': 'https://private.test', 'cookie': 'secret',
                                             'max_concurrent': 99, 'retry_limit': True})
        self.config.save(values)
        self.assertEqual(self.config.load()['batch_import'], {})


if __name__ == '__main__':
    unittest.main()
