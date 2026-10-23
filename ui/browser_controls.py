"""内嵌网页工具栏；导航与大屏交由浮窗接线，页面操作均可撤销。"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEngineScript
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox, QLabel

from core.page_tools import OVERLAY_SCAN_SCRIPT, OVERLAY_HIDE_SCRIPT, PAGE_UNDO_SCRIPT, PAGE_FIT_METRICS_SCRIPT


ZOOM_LEVELS = (50, 60, 67, 75, 80, 90, 100, 110, 125, 150)
FIT_LEVELS = (100, 90, 80, 75, 67, 60, 50)


class BrowserControls(QWidget):
    back_requested = Signal()
    forward_requested = Signal()
    reload_requested = Signal()
    large_mode_changed = Signal(bool)
    status_changed = Signal(str)

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self._page = None
        self._generation = 0
        self._zoom_percent = 100
        self._fit_serial = 0
        self._fit_running = False
        self._disposed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        self.back_button = QPushButton('←')
        self.back_button.setToolTip('后退')
        self.forward_button = QPushButton('→')
        self.forward_button.setToolTip('前进')
        self.reload_button = QPushButton('刷新')
        self.back_button.clicked.connect(lambda _=False: self.back_requested.emit())
        self.forward_button.clicked.connect(lambda _=False: self.forward_requested.emit())
        self.reload_button.clicked.connect(lambda _=False: self.reload_requested.emit())
        self.zoom_out_button = QPushButton('−')
        self.zoom_in_button = QPushButton('+')
        self.zoom_combo = QComboBox()
        for level in ZOOM_LEVELS:
            self.zoom_combo.addItem(f'{level}%', level)
        self.zoom_combo.setCurrentIndex(ZOOM_LEVELS.index(100))
        self.zoom_combo.activated.connect(lambda _: self.set_zoom(self.zoom_combo.currentData()))
        self.zoom_out_button.clicked.connect(self.zoom_out)
        self.zoom_in_button.clicked.connect(self.zoom_in)
        self.fit_button = QPushButton('适应窗口')
        self.fit_button.clicked.connect(self.fit_window)
        self.large_button = QPushButton('放大网页')
        self.large_button.setCheckable(True)
        self.large_button.toggled.connect(self._large_changed)
        for button in (self.back_button, self.forward_button):
            button.setMaximumWidth(40)
        for button in (self.zoom_out_button, self.zoom_in_button):
            button.setMaximumWidth(36)
        for widget in (self.back_button, self.forward_button, self.reload_button,
                       self.zoom_out_button, self.zoom_combo, self.zoom_in_button,
                       self.fit_button, self.large_button):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)
        tools = QHBoxLayout()
        self.overlay_button = QPushButton('处理遮挡')
        self.overlay_button.setToolTip('只高亮可能遮挡及其关闭按钮，不自动点击。')
        self.hide_overlay_button = QPushButton('隐藏遮挡层')
        self.hide_overlay_button.setToolTip('仅手动隐藏无可用关闭按钮的普通遮挡，保护播放器和权限层。')
        self.hide_overlay_button.setEnabled(False)
        self.undo_button = QPushButton('撤销页面修改')
        self.undo_button.setEnabled(False)
        self.overlay_button.clicked.connect(self.scan_overlay)
        self.hide_overlay_button.clicked.connect(self.hide_overlay)
        self.undo_button.clicked.connect(self.undo_page_changes)
        self.fit_floor = QComboBox()
        self.fit_floor.addItem('最低 50%', 50)
        self.fit_floor.addItem('最低 40%', 40)
        self.fit_floor.setToolTip('适应窗口的缩放下限，默认 50%。')
        self.fit_floor.currentIndexChanged.connect(self._floor_changed)
        for widget in (self.overlay_button, self.hide_overlay_button, self.undo_button, self.fit_floor):
            tools.addWidget(widget)
        tools.addStretch()
        layout.addLayout(tools)
        self._shortcuts = []
        for sequence, callback in [('Ctrl++', self.zoom_in), ('Ctrl+=', self.zoom_in),
                                   ('Ctrl+-', self.zoom_out), ('Ctrl+0', self.reset_zoom)]:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self.escape_shortcut = QShortcut(QKeySequence('Esc'), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.escape_shortcut.activated.connect(lambda: self.set_large_mode(False))
        self.escape_shortcut.setEnabled(False)
        self._fit_timeout = QTimer(self)
        self._fit_timeout.setSingleShot(True)
        self._fit_timeout.timeout.connect(self._fit_expired)
        self.destroyed.connect(lambda *_: setattr(self, '_disposed', True))
        if hasattr(view, 'loadStarted'):
            view.loadStarted.connect(self._navigation_started)
        if hasattr(view, 'loadFinished'):
            view.loadFinished.connect(lambda *_: self.refresh_page())
        if hasattr(view, 'urlChanged'):
            view.urlChanged.connect(lambda *_: self.refresh_page())
        if hasattr(view, 'browserShortcut'):
            view.browserShortcut.connect(self._native_shortcut)
        self.sync_history(False, False)
        self.refresh_page()

    def _native_shortcut(self, key):
        """Native child windows receive keyboard events outside Qt's shortcut map."""
        callback = {'zoom_in': self.zoom_in, 'zoom_out': self.zoom_out, 'zoom_reset': self.reset_zoom,
                    'escape': lambda: self.set_large_mode(False)}.get(key)
        if callback:
            callback()

    def sync_history(self, can_back, can_forward):
        self.back_button.setEnabled(bool(can_back))
        self.forward_button.setEnabled(bool(can_forward))

    def refresh_page(self):
        """page 可替换，view 固定；也可由宿主替换 page 后显式调用。"""
        if self._disposed:
            return None
        try:
            page = self.view.page()
        except RuntimeError:
            return None
        if page is not self._page:
            self._page = page
            self._generation += 1
            self._cancel_fit()
            self.hide_overlay_button.setEnabled(False)
            self.undo_button.setEnabled(False)
        self.view.setZoomFactor(self._zoom_percent / 100)
        return page

    def _navigation_started(self):
        self._generation += 1
        self._cancel_fit()
        self.hide_overlay_button.setEnabled(False)
        self.undo_button.setEnabled(False)
        self.refresh_page()

    def _evaluate(self, script, callback):
        page = self.refresh_page()
        if page is None:
            return
        generation = self._generation
        def received(value):
            if self._disposed or generation != self._generation:
                return
            try:
                if page is not self.view.page():
                    return
            except RuntimeError:
                return
            if isinstance(value, str) and len(value) <= 64 * 1024:
                try:
                    value = json.loads(value)
                except ValueError:
                    value = None
            if isinstance(value, dict):
                callback(value)
            else:
                self.status_changed.emit('页面辅助未能完成，请稍后重试。')
        page.runJavaScript(script, QWebEngineScript.ScriptWorldId.MainWorld, received)

    def set_zoom(self, percent):
        self._cancel_fit()
        minimum = int(self.fit_floor.currentData())
        levels = ((40,) + ZOOM_LEVELS) if minimum == 40 else ZOOM_LEVELS
        percent = max(minimum, min(150, int(percent)))
        self._apply_zoom(min(levels, key=lambda value: abs(value - percent)))

    def _apply_zoom(self, percent):
        self._zoom_percent = percent
        self.view.setZoomFactor(percent / 100)
        index = self.zoom_combo.findData(percent)
        if index < 0:
            self.zoom_combo.insertItem(0, f'{percent}%', percent)
            index = 0
        self.zoom_combo.setCurrentIndex(index)

    def _floor_changed(self, *_):
        self._cancel_fit()
        if self._zoom_percent < int(self.fit_floor.currentData()):
            self.set_zoom(self.fit_floor.currentData())

    def zoom_in(self):
        levels = ZOOM_LEVELS
        self.set_zoom(next((value for value in levels if value > self._zoom_percent), 150))

    def zoom_out(self):
        levels = ((40,) + ZOOM_LEVELS) if self.fit_floor.currentData() == 40 else ZOOM_LEVELS
        self.set_zoom(next((value for value in reversed(levels) if value < self._zoom_percent), levels[0]))

    def reset_zoom(self):
        self.set_zoom(100)

    def set_large_mode(self, enabled):
        self.large_button.setChecked(bool(enabled))

    def _large_changed(self, enabled):
        self.large_button.setText('退出大屏' if enabled else '放大网页')
        self.escape_shortcut.setEnabled(enabled)
        self.large_mode_changed.emit(enabled)

    def _cancel_fit(self):
        self._fit_serial += 1
        self._fit_running = False
        if hasattr(self, '_fit_timeout'):
            self._fit_timeout.stop()
        if hasattr(self, 'fit_button'):
            self.fit_button.setEnabled(True)

    def fit_window(self):
        self.refresh_page()
        self._cancel_fit()
        self._fit_running = True
        self.fit_button.setEnabled(False)
        self._fit_timeout.start(4000)
        levels = FIT_LEVELS + ((40,) if self.fit_floor.currentData() == 40 else ())
        self._fit_step(self._fit_serial, levels, 0)

    def _fit_step(self, token, levels, index):
        if self._disposed or token != self._fit_serial or not self._fit_running:
            return
        self._apply_zoom(levels[index])
        def measure():
            if self._disposed or token != self._fit_serial or not self._fit_running:
                return
            def measured(values):
                if token != self._fit_serial or not self._fit_running:
                    return
                if values.get('usable') or index == len(levels) - 1:
                    self._cancel_fit()
                    suffix = '' if values.get('usable') else '，仍有遮挡可使用“处理遮挡”或手动关闭'
                    self.status_changed.emit(f'网页缩放已调整为 {self._zoom_percent}%{suffix}。')
                else:
                    self._fit_step(token, levels, index + 1)
            self._evaluate(PAGE_FIT_METRICS_SCRIPT, measured)
        QTimer.singleShot(100, measure)

    def _fit_expired(self):
        self._cancel_fit()
        self.status_changed.emit('读取页面尺寸超时，可手动调整缩放。')

    def _tool_result(self, values):
        self.hide_overlay_button.setEnabled(bool(values.get('can_hide')))
        self.undo_button.setEnabled(bool(values.get('changed')))
        messages = {
            'none': '未发现覆盖超过 40% 的可处理遮挡层。',
            'close_found': '发现关闭按钮，已高亮并尝试滚动到可见位置；请手动点击。',
            'overlay_found': '检测到可能遮挡层；如需隐藏，请手动点击“隐藏遮挡层”。',
            'iframe': '遮挡包含 iframe，请缩放、放大窗口或手动关闭，不修改子页面。',
            'protected_layer': '检测到登录、付费或权限层，已保留，请按网站正常流程操作。',
            'player': '目标包含播放器，已保留。',
            'page_root': '不会隐藏页面根节点。',
            'close_available': '已有可用关闭按钮，请手动点击。',
            'hidden': '已临时隐藏遮挡层，可点击“撤销页面修改”恢复。',
            'restored': '已恢复页面原始样式和滚动位置。',
            'stale': '原遮挡已变化，请重新点击“处理遮挡”。',
            'limit': '页面修改记录已达到上限，请先撤销后重试。',
        }
        message = messages.get(values.get('code'), '页面辅助未能完成，请稍后重试。')
        if values.get('large'):
            message = '检测到大型遮挡（覆盖超过 60%）。' + message
        self.status_changed.emit(message)

    def scan_overlay(self):
        self._cancel_fit()
        self._evaluate(OVERLAY_SCAN_SCRIPT, self._tool_result)

    def hide_overlay(self):
        self._cancel_fit()
        self._evaluate(OVERLAY_HIDE_SCRIPT, self._tool_result)

    def undo_page_changes(self):
        self._cancel_fit()
        self._evaluate(PAGE_UNDO_SCRIPT, self._tool_result)
