import json
import os
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEngineScript, QWebEngineSettings
from PySide6.QtWidgets import QApplication, QWidget

from core.browser_observer import BrowserNetworkObserver
from core.browser_runtime import chrome_compatible_user_agent, configure_browser_profile
from tests.test_browser_observer import FakePage, FakeSocket, request, response


class BrowserRuntimeTests(unittest.TestCase):
    def test_user_agent_retains_real_chromium_and_platform(self):
        value = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) QtWebEngine/6.11.2 Chrome/140.0.0.0 Safari/537.36'
        result = chrome_compatible_user_agent(value)
        self.assertNotIn('QtWebEngine', result)
        self.assertIn('Chrome/140.0.0.0', result)
        self.assertIn('Windows NT 10.0; Win64; x64', result)
        self.assertEqual(chrome_compatible_user_agent(result), result)

    def test_standard_user_agent_is_unchanged(self):
        value = 'Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36'
        self.assertEqual(chrome_compatible_user_agent(value), value)

    def test_profile_settings_enable_storage_and_desktop_playback_only(self):
        profile = Mock()
        profile.httpUserAgent.return_value = 'QtWebEngine/6.11.2 Chrome/140.0.0.0'
        configure_browser_profile(profile)
        attributes = profile.settings.return_value.setAttribute.call_args_list
        self.assertEqual(len(attributes), 3)
        values = {call.args[0]: call.args[1] for call in attributes}
        self.assertTrue(values[QWebEngineSettings.WebAttribute.JavascriptEnabled])
        self.assertTrue(values[QWebEngineSettings.WebAttribute.LocalStorageEnabled])
        self.assertFalse(values[QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture])
        profile.setPersistentCookiesPolicy.assert_not_called()


class BrowserDiagnosticObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.observer = BrowserNetworkObserver(FakePage())
        self.observer._active = self.observer._enabled = True
        self.observer._socket = FakeSocket(parent=self.observer)

    def tearDown(self):
        self.observer.stop()
        self.observer.deleteLater()
        self.app.processEvents()

    def event(self, method, params):
        self.observer._receive_text(json.dumps({'method': method, 'params': params}))

    def test_counts_fetch_xhr_media_without_credentials_or_urls(self):
        for kind in ('Fetch', 'XHR', 'Media'):
            self.event('Network.requestWillBeSent', {**request(request_id=kind), 'type': kind})
            self.event('Network.responseReceived', {**response(request_id=kind), 'type': kind})
        report = self.observer.network_diagnostics()
        self.assertEqual(report['counts']['Network.requestWillBeSent'], 3)
        for kind in ('Fetch', 'XHR', 'Media'):
            self.assertEqual(report['counts']['Network.responseReceived:' + kind], 1)
        self.assertNotIn('https', json.dumps(report))
        report['counts'].clear()
        self.assertTrue(self.observer.network_diagnostics()['counts'])

    def test_counts_reset_with_navigation_round(self):
        self.event('Network.requestWillBeSent', request())
        self.observer.reset()
        self.assertEqual(self.observer.network_diagnostics()['counts'], {})
        stale = {**request(request_id='stale'), 'wallTime': time.time() - 100}
        self.event('Network.requestWillBeSent', stale)
        self.assertEqual(self.observer.network_diagnostics()['counts'], {})

    def test_loading_failure_is_visible(self):
        self.event('Network.loadingFailed', {'requestId': 'failed', 'type': 'Media'})
        self.assertEqual(self.observer.network_diagnostics()['counts']['Network.loadingFailed:Media'], 1)

    def test_probe_awaits_promise_and_normalizes_page_values(self):
        reports = []
        self.assertTrue(self.observer.probe_capabilities(reports.append))
        command = self.observer._socket.messages[-1]
        self.assertEqual(command['method'], 'Runtime.evaluate')
        self.assertTrue(command['params']['awaitPromise'])
        self.assertTrue(command['params']['returnByValue'])
        self.observer._receive_text(json.dumps({'id': command['id'], 'result': {'result': {'value': {
            'video_tag': True, 'cookie': {'value': 'secret'}, 'arbitrary': 'secret'}}}}))
        self.assertTrue(reports[0]['video_tag'])
        self.assertNotIn('secret', json.dumps(reports))

    def test_probe_error_remains_unknown_without_raw_exception(self):
        reports = []
        self.observer.probe_capabilities(reports.append)
        command = self.observer._socket.messages[-1]
        self.observer._receive_text(json.dumps({'id': command['id'], 'result': {'exceptionDetails': {'text': 'secret'}}}))
        self.assertEqual(reports, [None])

    def test_probe_requires_enabled_connection(self):
        self.observer._enabled = False
        self.assertFalse(self.observer.probe_capabilities(lambda *_: None))
        self.assertFalse(self.observer._socket.messages)


class BrowserEnvironmentPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.browser_environment import BrowserEnvironmentDialog
        self.owner = QWidget()
        self.owner._closing = False
        self.owner._candidates = []
        self.owner.profile = Mock()
        self.owner.profile.httpUserAgent.return_value = 'Chrome/140.0.0.0'
        self.owner.page = Mock()
        self.owner.page.url.return_value = QUrl('https://player.example.test/watch')
        self.owner.observer = Mock()
        self.owner.observer.network_diagnostics.return_value = {
            'backend': 'Chrome DevTools Protocol', 'connected': False, 'network_enabled': False, 'counts': {}}
        self.owner.observer.probe_capabilities.return_value = False
        self.panel = BrowserEnvironmentDialog(self.owner)

    def tearDown(self):
        self.panel.shutdown()
        self.owner.deleteLater()
        self.app.processEvents()

    def test_diagnostic_does_not_read_system_cookies_on_creation(self):
        self.assertIsNone(self.panel._worker)
        self.assertIn('Chromium', self.panel.output.toPlainText())
        self.assertIn('默认不读取', self.panel.cookie_status.text())

    def test_pre_capture_probe_uses_isolated_world(self):
        self.panel.refresh()
        args = self.owner.page.runJavaScript.call_args.args
        self.assertEqual(args[1], QWebEngineScript.ScriptWorldId.ApplicationWorld)
        self.assertIn('mediaflow_diagnostic_', args[0])
        self.assertIn('MediaSource', args[0])

    def test_cold_native_page_waits_for_controller_before_starting_probe(self):
        class ColdPage(QObject):
            ready = Signal()
            is_ready = False
            runJavaScript = Mock()
        page = ColdPage()
        self.owner.page = page
        self.panel.refresh()
        page.runJavaScript.assert_not_called()
        self.assertFalse(self.panel._probe_poll.isActive())
        self.assertEqual(self.panel._probe_timeout.interval(), 30000)
        page.is_ready = True
        page.ready.emit()
        page.runJavaScript.assert_called_once()
        self.assertEqual(self.panel._probe_timeout.interval(), 6000)

    def test_probe_timeout_releases_button_and_ignores_late_success(self):
        self.panel.refresh()
        generation = self.panel._generation
        self.panel._probe_timeout.stop()
        self.panel._probe_timeout.timeout.emit()
        self.assertTrue(self.panel.refresh_button.isEnabled())
        self.assertIsNone(self.panel._report['video_tag'])
        self.panel._probe_done({'video_tag': True}, generation)
        self.assertIsNone(self.panel._report['video_tag'])

    def test_stale_probe_callback_does_not_replace_current_page_diagnostic(self):
        self.panel.refresh()
        previous = self.panel._generation
        self.panel.refresh()
        self.panel._probe_done({'video_tag': False}, previous)
        self.assertIsNone(self.panel._report)
        self.panel._probe_done({'video_tag': True, 'can_play_type': {'h264_mp4': ''}}, self.panel._generation)
        self.assertTrue(self.panel._report['video_tag'])
        self.assertIn('修改 User-Agent 不会补充', self.panel.output.toPlainText())

    def test_cookie_import_requires_loaded_http_page(self):
        self.owner.page.url.return_value = QUrl('about:blank')
        self.panel.import_browser()
        self.assertIsNone(self.panel._worker)
        self.assertIn('先打开', self.panel.cookie_status.text())

    def test_missing_tool_has_file_or_embedded_login_fallback(self):
        self.panel.import_browser()
        self.assertIsNone(self.panel._worker)
        self.assertIn('未检测到 yt-dlp', self.panel.cookie_status.text())

    def test_shutdown_cancels_import_without_destroying_running_thread(self):
        worker = Mock()
        worker.cancel_event = Mock()
        self.panel._worker = worker
        self.panel.shutdown()
        worker.cancel_event.set.assert_called_once()
        self.assertTrue(self.panel.busy)
        worker.deleteLater.assert_not_called()
        self.panel._worker_finished()
        self.assertFalse(self.panel.busy)


if __name__ == '__main__':
    unittest.main()
