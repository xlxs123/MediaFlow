"""工具栏交互回归；--page-tools-smoke 另行执行真实 Chromium DOM 验收。"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtWebEngineCore import QWebEngineScript

from core.page_tools import OVERLAY_SCAN_SCRIPT, OVERLAY_HIDE_SCRIPT, PAGE_UNDO_SCRIPT, PAGE_FIT_METRICS_SCRIPT
from ui.browser_controls import BrowserControls, ZOOM_LEVELS


class FakePage(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = []
        self.answers = []
        self.measurements = []

    def runJavaScript(self, script, world, callback):
        self.calls.append((script, world, callback))
        self.measurements.append(getattr(self.parent(), 'factor', None))
        if self.answers:
            callback(self.answers.pop(0))


class FakeView(QWidget):
    loadStarted = Signal()
    loadFinished = Signal(bool)
    urlChanged = Signal(QUrl)
    browserShortcut = Signal(str)

    def __init__(self):
        super().__init__()
        self.current_page = FakePage(self)
        self.factor = 1.0
        self.zooms = []

    def page(self):
        return self.current_page

    def setZoomFactor(self, factor):
        self.factor = factor
        self.zooms.append(factor)

    def zoomFactor(self):
        return self.factor


class BrowserControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = FakeView()
        self.controls = BrowserControls(self.view)
        self.status = []
        self.controls.status_changed.connect(self.status.append)

    def tearDown(self):
        self.controls.deleteLater()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def wait(self, predicate, seconds=3):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate())

    def test_native_child_shortcuts_update_same_zoom_and_large_mode(self):
        self.view.browserShortcut.emit('zoom_in')
        self.assertGreater(self.view.factor, 1)
        self.view.browserShortcut.emit('zoom_out')
        self.assertEqual(self.view.factor, 1)
        self.controls.set_zoom(75)
        self.view.browserShortcut.emit('zoom_reset')
        self.assertEqual(self.view.factor, 1)
        self.controls.set_large_mode(True)
        self.view.browserShortcut.emit('escape')
        self.assertFalse(self.controls.large_button.isChecked())
        self.view.browserShortcut.emit('untrusted-page-command')
        self.assertEqual(self.view.factor, 1)

    def test_history_buttons_and_signals_do_not_navigate_themselves(self):
        seen = []
        self.controls.back_requested.connect(lambda: seen.append('back'))
        self.controls.forward_requested.connect(lambda: seen.append('forward'))
        self.controls.reload_requested.connect(lambda: seen.append('reload'))
        self.assertFalse(self.controls.back_button.isEnabled())
        self.controls.sync_history(True, False)
        self.controls.back_button.click()
        self.controls.forward_button.click()
        self.controls.reload_button.click()
        self.assertEqual(seen, ['back', 'reload'])
        self.controls.sync_history(True, True)
        self.controls.forward_button.click()
        self.assertEqual(seen[-1], 'forward')
        self.assertFalse(self.view.page().calls)

    def test_prescribed_zoom_levels_and_default(self):
        self.assertEqual(tuple(self.controls.zoom_combo.itemData(i) for i in range(self.controls.zoom_combo.count())), ZOOM_LEVELS)
        self.assertEqual(self.view.zoomFactor(), 1)
        self.assertEqual(self.controls.zoom_combo.currentText(), '100%')

    def test_zoom_steps_and_clamps(self):
        self.controls.zoom_out()
        self.assertEqual(self.view.zoomFactor(), 0.9)
        self.controls.zoom_in()
        self.assertEqual(self.view.zoomFactor(), 1)
        self.controls.set_zoom(500)
        self.assertEqual(self.view.zoomFactor(), 1.5)
        self.controls.set_zoom(1)
        self.assertEqual(self.view.zoomFactor(), 0.5)
        self.controls.reset_zoom()
        self.assertEqual(self.view.zoomFactor(), 1)

    def test_optional_fit_floor_40_percent(self):
        self.controls.fit_floor.setCurrentIndex(1)
        self.controls.set_zoom(40)
        self.assertEqual(self.view.zoomFactor(), 0.4)
        self.assertEqual(self.controls.zoom_combo.currentText(), '40%')
        self.controls.fit_floor.setCurrentIndex(0)
        self.assertEqual(self.view.zoomFactor(), 0.5)

    def test_ctrl_shortcuts_apply_zoom(self):
        self.controls._shortcuts[0].activated.emit()
        self.assertEqual(self.view.zoomFactor(), 1.1)
        self.controls._shortcuts[2].activated.emit()
        self.assertEqual(self.view.zoomFactor(), 1)
        self.controls.set_zoom(67)
        self.controls._shortcuts[3].activated.emit()
        self.assertEqual(self.view.zoomFactor(), 1)

    def test_large_mode_and_escape_only_exit_large_mode(self):
        changes = []
        self.controls.large_mode_changed.connect(changes.append)
        self.controls.large_button.click()
        self.assertTrue(self.controls.escape_shortcut.isEnabled())
        self.assertEqual(self.controls.large_button.text(), '退出大屏')
        self.controls.escape_shortcut.activated.emit()
        self.assertEqual(changes, [True, False])
        self.assertFalse(self.controls.escape_shortcut.isEnabled())
        self.assertEqual(self.controls.large_button.text(), '放大网页')

    def test_page_replacement_keeps_zoom_and_resets_page_tool_state(self):
        self.controls.set_zoom(67)
        self.controls.hide_overlay_button.setEnabled(True)
        self.controls.undo_button.setEnabled(True)
        self.view.current_page = FakePage(self.view)
        self.controls.refresh_page()
        self.assertEqual(self.view.zoomFactor(), 0.67)
        self.assertFalse(self.controls.hide_overlay_button.isEnabled())
        self.assertFalse(self.controls.undo_button.isEnabled())

    def test_overlay_and_undo_commands_use_mainworld_and_actual_current_page(self):
        self.view.current_page = FakePage(self.view)
        page = self.view.page()
        self.controls.scan_overlay()
        self.controls.hide_overlay()
        self.controls.undo_page_changes()
        self.assertEqual([call[0] for call in page.calls], [OVERLAY_SCAN_SCRIPT, OVERLAY_HIDE_SCRIPT, PAGE_UNDO_SCRIPT])
        self.assertTrue(all(call[1] == QWebEngineScript.ScriptWorldId.MainWorld for call in page.calls))

    def test_scan_does_not_automatically_hide_or_click(self):
        self.view.page().answers.append({'code': 'overlay_found', 'can_hide': True, 'changed': True, 'large': True})
        self.controls.overlay_button.click()
        self.assertEqual(len(self.view.page().calls), 1)
        self.assertEqual(self.view.page().calls[0][0], OVERLAY_SCAN_SCRIPT)
        self.assertTrue(self.controls.hide_overlay_button.isEnabled())
        self.assertTrue(self.controls.undo_button.isEnabled())
        self.assertIn('超过 60%', self.status[-1])

    def test_close_button_result_invites_manual_click_not_hiding(self):
        self.view.page().answers.append({'code': 'close_found', 'can_hide': False, 'changed': True})
        self.controls.scan_overlay()
        self.assertFalse(self.controls.hide_overlay_button.isEnabled())
        self.assertIn('手动点击', self.status[-1])

    def test_protected_layer_and_iframe_hide_remain_disabled(self):
        for code in ('player', 'protected_layer', 'iframe'):
            self.view.page().answers.append({'code': code, 'can_hide': False, 'changed': True})
            self.controls.scan_overlay()
            self.assertFalse(self.controls.hide_overlay_button.isEnabled())

    def test_old_page_script_callback_cannot_modify_toolbar(self):
        old = self.view.page()
        self.controls.scan_overlay()
        self.view.current_page = FakePage(self.view)
        old.calls[-1][2]({'code': 'overlay_found', 'can_hide': True, 'changed': True})
        self.assertFalse(self.controls.hide_overlay_button.isEnabled())
        self.assertFalse(self.status)

    def test_old_document_script_callback_after_navigation_is_ignored(self):
        page = self.view.page()
        self.controls.scan_overlay()
        self.view.loadStarted.emit()
        page.calls[-1][2]({'code': 'overlay_found', 'can_hide': True, 'changed': True})
        self.assertFalse(self.controls.undo_button.isEnabled())
        self.assertFalse(self.status)

    def test_fit_walks_levels_until_page_is_usable(self):
        self.view.page().answers.extend([{'usable': False}, {'usable': False}, {'usable': True}])
        self.controls.fit_window()
        self.wait(lambda: not self.controls._fit_running)
        self.assertEqual(self.view.page().measurements, [1.0, 0.9, 0.8])
        self.assertEqual(self.view.zoomFactor(), 0.8)
        self.assertEqual(len(self.view.page().calls), 3)
        self.assertTrue(self.controls.fit_button.isEnabled())

    def test_fit_stops_at_50_and_can_be_configured_to_40(self):
        self.view.page().answers.extend([{'usable': False}] * 8)
        self.controls.fit_window()
        self.wait(lambda: not self.controls._fit_running)
        self.assertEqual(self.view.zoomFactor(), 0.5)
        self.view.page().answers.clear()
        self.controls.fit_floor.setCurrentIndex(1)
        self.view.page().answers.extend([{'usable': False}] * 8)
        self.controls.fit_window()
        self.wait(lambda: not self.controls._fit_running)
        self.assertEqual(self.view.zoomFactor(), 0.4)

    def test_manual_zoom_cancels_inflight_fit(self):
        self.controls.fit_window()
        self.wait(lambda: bool(self.view.page().calls))
        callback = self.view.page().calls[-1][2]
        self.controls.set_zoom(125)
        callback({'usable': False})
        self.assertEqual(self.view.zoomFactor(), 1.25)
        self.assertFalse(self.controls._fit_running)

    def test_fit_timeout_reenables_controls(self):
        self.controls.fit_window()
        self.controls._fit_expired()
        self.assertTrue(self.controls.fit_button.isEnabled())
        self.assertFalse(self.controls._fit_running)
        self.assertIn('超时', self.status[-1])

    def test_unexpected_script_result_never_leaks_raw_webpage_values(self):
        self.view.page().answers.append({'code': 'Cookie: secret Authorization: secret', 'message': 'secret'})
        self.controls.scan_overlay()
        self.assertNotIn('secret', self.status[-1])

    def test_qt_json_string_result_is_decoded(self):
        self.view.page().answers.append(json.dumps({'code': 'overlay_found', 'can_hide': True, 'changed': True}))
        self.controls.scan_overlay()
        self.assertTrue(self.controls.hide_overlay_button.isEnabled())

    def test_scripts_have_no_network_or_programmatic_clicks(self):
        scripts = '\n'.join((OVERLAY_SCAN_SCRIPT, OVERLAY_HIDE_SCRIPT, PAGE_UNDO_SCRIPT, PAGE_FIT_METRICS_SCRIPT))
        for forbidden in ('.click(', 'dispatchEvent', 'fetch(', 'XMLHttpRequest', 'contentWindow', 'requestMediaKeySystemAccess'):
            self.assertNotIn(forbidden, scripts)


def run_page_tools_smoke(native=False):
    """独立进程运行，避免在普通单测中启动 Chromium。"""
    from core.browser_runtime import prepare_browser_runtime
    os.environ['QT_QPA_PLATFORM'] = 'windows' if native else 'offscreen'
    existing = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = existing + ' --disable-gpu'
    prepare_browser_runtime()
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)

    class RealPageToolsTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.profile = QWebEngineProfile()
            cls.view = QWebEngineView()
            cls.page = QWebEnginePage(cls.profile)
            cls.view.setPage(cls.page)
            cls.view.resize(900, 650)
            cls.view.showMinimized() if native else cls.view.show()

        @classmethod
        def tearDownClass(cls):
            cls.view.close()
            cls.view.deleteLater()
            cls.page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.profile.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        def js(self, script):
            results = []
            commands = (OVERLAY_SCAN_SCRIPT, OVERLAY_HIDE_SCRIPT, PAGE_UNDO_SCRIPT, PAGE_FIT_METRICS_SCRIPT)
            wire = script if script in commands else 'JSON.stringify((' + script + '))'
            self.page.runJavaScript(wire, QWebEngineScript.ScriptWorldId.MainWorld, results.append)
            end = time.monotonic() + 5
            while not results and time.monotonic() < end:
                app.processEvents()
                time.sleep(0.005)
            self.assertTrue(results, 'Chromium JS callback timeout')
            return json.loads(results[0]) if results[0] else None

        def load(self, body, root_style=''):
            done = []
            nonce = str(time.time_ns())
            def complete(success):
                done.append(success)
            self.page.loadFinished.connect(complete)
            self.view.setZoomFactor(1)
            self.page.setHtml('<!doctype html><html data-tools-fixture="' + nonce + '" style="' + root_style + '"><head><style>body{margin:0}button{min-width:30px;min-height:30px}</style></head><body>' + body + '</body></html>', QUrl('about:blank'))
            end = time.monotonic() + 5
            while not done and time.monotonic() < end:
                app.processEvents()
                time.sleep(0.005)
            self.page.loadFinished.disconnect(complete)
            self.assertTrue(done and done[-1], 'Fixture load failed')
            self.assertEqual(self.js('document.documentElement.dataset.toolsFixture'), nonce)
            QTest.qWait(20)

        def overlay(self, content='', extra=''):
            return '<div id="cover" style="position:fixed;inset:0;z-index:100;background:#ccc;' + extra + '">' + content + '</div>'

        def test_highlight_close_without_click_or_hide_and_undo_style(self):
            self.load(self.overlay('<button id="close" aria-label="Close ad" style="color:red" onclick="window.clicked=1">×</button>'))
            result = self.js(OVERLAY_SCAN_SCRIPT)
            self.assertEqual(result['code'], 'close_found')
            self.assertFalse(result['can_hide'])
            self.assertGreater(result['coverage'], 0.6)
            self.assertIsNone(self.js('window.clicked'))
            self.assertEqual(self.js("getComputedStyle(document.getElementById('cover')).display"), 'block')
            self.assertEqual(self.js(OVERLAY_HIDE_SCRIPT)['code'], 'close_available')
            self.js(PAGE_UNDO_SCRIPT)
            self.assertEqual(self.js("document.getElementById('close').getAttribute('style')"), 'color:red')

        def test_manual_hide_and_exact_style_and_overflow_restore(self):
            self.load(self.overlay('<p>Advertisement</p>'), 'overflow:hidden;color:blue')
            before = self.js("[document.getElementById('cover').getAttribute('style'),document.documentElement.getAttribute('style')]")
            scan = self.js(OVERLAY_SCAN_SCRIPT)
            self.assertTrue(scan['can_hide'])
            self.assertEqual(self.js(OVERLAY_HIDE_SCRIPT)['code'], 'hidden')
            self.assertEqual(self.js("getComputedStyle(document.getElementById('cover')).display"), 'none')
            self.js(PAGE_UNDO_SCRIPT)
            after = self.js("[document.getElementById('cover').getAttribute('style'),document.documentElement.getAttribute('style')]")
            self.assertEqual(after, before)

        def test_video_audio_canvas_and_player_ancestors_are_not_hidden(self):
            for content in ('<video></video>', '<audio></audio>', '<canvas></canvas>', '<div data-player></div>', '<div class="mainPlayerContainer"></div>'):
                self.load(self.overlay(content))
                self.assertFalse(self.js(OVERLAY_SCAN_SCRIPT)['can_hide'])
                self.assertNotEqual(self.js(OVERLAY_HIDE_SCRIPT)['code'], 'hidden')

        def test_login_payment_permission_layers_are_preserved(self):
            for content in ('<form><input type=password></form>', '<p>Subscribe to watch this premium content</p>', '<p>请登录后购买会员</p>', '<p>Permission required</p>'):
                self.load(self.overlay(content))
                self.assertEqual(self.js(OVERLAY_SCAN_SCRIPT)['code'], 'protected_layer')
                self.assertEqual(self.js(OVERLAY_HIDE_SCRIPT)['code'], 'protected_layer')

        def test_iframe_overlay_is_not_read_or_hidden(self):
            self.load(self.overlay('<iframe srcdoc="<video></video>" sandbox></iframe>'))
            result = self.js(OVERLAY_SCAN_SCRIPT)
            self.assertEqual(result['code'], 'iframe')
            self.assertFalse(result['can_hide'])
            self.assertEqual(self.js(OVERLAY_HIDE_SCRIPT)['code'], 'iframe')

        def test_overlay_that_gains_video_after_scan_is_rechecked(self):
            self.load(self.overlay('<p>Advertising</p>'))
            self.assertTrue(self.js(OVERLAY_SCAN_SCRIPT)['can_hide'])
            self.js("document.getElementById('cover').appendChild(document.createElement('video'))")
            self.assertEqual(self.js(OVERLAY_HIDE_SCRIPT)['code'], 'player')

        def test_under_40_percent_is_not_treated_as_large_overlay(self):
            self.load('<div style="position:fixed;width:100%;height:20%;z-index:100;background:gray">Banner</div>')
            self.assertEqual(self.js(OVERLAY_SCAN_SCRIPT)['code'], 'none')

        def test_repeat_scan_then_undo_does_not_restore_highlight(self):
            self.load(self.overlay('<p>Advertising</p>'))
            style = self.js("document.getElementById('cover').getAttribute('style')")
            self.js(OVERLAY_SCAN_SCRIPT)
            self.js(OVERLAY_SCAN_SCRIPT)
            self.js(PAGE_UNDO_SCRIPT)
            self.assertEqual(self.js("document.getElementById('cover').getAttribute('style')"), style)

        def test_undo_removes_originally_missing_style_attribute(self):
            self.load(self.overlay('<button id="close">Close</button>'))
            self.assertIsNone(self.js("document.getElementById('close').getAttribute('style')"))
            self.js(OVERLAY_SCAN_SCRIPT)
            self.js(PAGE_UNDO_SCRIPT)
            self.assertIsNone(self.js("document.getElementById('close').getAttribute('style')"))

        def test_undo_restores_window_and_nested_scroll(self):
            self.load('<div style="height:2500px"><div id="scroll" style="position:fixed;inset:0;z-index:100;overflow:auto">'
                      '<div style="height:1800px"><button id="close" style="margin-top:1500px">Close</button></div></div></div>')
            self.js('window.scrollTo({top:100,behavior:"instant"})')
            before = self.js('[scrollY,document.getElementById("scroll").scrollTop]')
            self.js(OVERLAY_SCAN_SCRIPT)
            self.assertGreater(self.js('document.getElementById("scroll").scrollTop'), 0)
            self.js(PAGE_UNDO_SCRIPT)
            self.assertEqual(self.js('[scrollY,document.getElementById("scroll").scrollTop]'), before)

        def test_fit_metrics_recognize_horizontal_overflow(self):
            self.load('<div style="width:1600px;height:200px">Wide page</div>')
            result = self.js(PAGE_FIT_METRICS_SCRIPT)
            self.assertGreater(result['horizontal_overflow'], 100)
            self.assertFalse(result['usable'])

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestLoader().loadTestsFromTestCase(RealPageToolsTests))
    report = {'passed': result.wasSuccessful(), 'page_tools_smoke': result.wasSuccessful(),
              'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'native': native}
    if '--report-file' in sys.argv:
        from pathlib import Path
        destination = Path(sys.argv[sys.argv.index('--report-file') + 1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    if '--page-tools-smoke' in sys.argv:
        raise SystemExit(run_page_tools_smoke('--native' in sys.argv))
    unittest.main()
