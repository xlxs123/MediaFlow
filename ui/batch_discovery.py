"""复用 DiscoveryDialog 的每页采集；串行释放 CDP target 后再交接下一页。"""
from copy import deepcopy
import time

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QComboBox,
                              QDialogButtonBox)

from core.batch_controller import automatic_choice
from core.media_groups import available_candidate
from core.media_quality import quality_candidates
from ui.discovery_dialog import DiscoveryDialog
from utils.security import Redactor


class BatchCandidateDialog(QDialog):
    """每个来源选择一个完整媒体，复用现有的可信度确认。"""

    def __init__(self, groups, parent=None, track_preferences=None):
        super().__init__(parent)
        self.setWindowTitle('选择此网址的正片和画质')
        self.resize(650, 200)
        self.candidate_id = ''
        self.track_selection = None
        self._choices = []
        layout = QVBoxLayout(self)
        label = QLabel('请选择本网址要下载的完整媒体。孤立分片和访问验证失败的候选无法加入下载。')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.choices = QComboBox()
        self.choices.setMinimumContentsLength(30)
        self.choices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for group in groups:
            for candidate in quality_candidates(group):
                if not available_candidate(candidate) or candidate.id in {value.id for value in self._choices}:
                    continue
                redactor = Redactor(candidate.request_context.headers, [candidate.url, candidate.final_url])
                title = redactor.redact(group.title or candidate.metadata.get('title') or '未命名媒体')
                self.choices.addItem(f'{title[:100]} · {candidate.resolution_label} · {candidate.media_type} · 可信度 {candidate.confidence}/100', candidate.id)
                self._choices.append(candidate)
        layout.addWidget(self.choices)
        from ui.track_selector import TrackSelector
        self.track_selector = TrackSelector()
        self.track_selector.setMinimumHeight(210)
        layout.addWidget(self.track_selector)
        def show_tracks():
            candidate = next((v for v in self._choices if v.id == self.choices.currentData()), None)
            self.track_selector.set_candidate(candidate, track_preferences)
        self.choices.currentIndexChanged.connect(show_tracks)
        show_tracks()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('确认并加入下载')
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self._choices))
        self.buttons.accepted.connect(self._confirm)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _confirm(self):
        candidate = next((value for value in self._choices if value.id == self.choices.currentData()), None)
        if candidate and available_candidate(candidate) and DiscoveryDialog._confirm_medium(self, [candidate]):
            try:
                from core.media_tracks import apply_selection
                selection = self.track_selector.selection()
                apply_selection(candidate, selection)
            except ValueError as error:
                self.track_selector.summary.setText(str(error))
                return
            self.candidate_id = candidate.id
            self.track_selection = selection
            self.accept()


class SerialBatchDiscovery(QObject):
    finished = Signal(str, object, str)

    def __init__(self, parent, can_acquire=None):
        super().__init__(parent)
        self.owner = parent
        self.can_acquire = can_acquire or (lambda: True)
        self.dialog = None
        self.item_id = ''
        self._url = self._preference = ''
        self._deadline = self._freeze_at = 0.0
        self._stable_choice, self._stable_at = '', 0.0
        self._committed_url = ''
        self._payload, self._error = {}, ''
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._poll)

    @property
    def busy(self):
        return self.dialog is not None

    def can_start(self):
        return not self.busy and self.can_acquire()

    def start(self, item_id, url, options):
        if not self.can_start():
            raise ValueError('浏览器正在使用，请稍候再分析。')
        self.item_id, self._url = item_id, url
        self._preference = options.get('quality', 'auto')
        seconds = options.get('discovery_seconds', 60)
        self._deadline = time.monotonic() + seconds + 30
        self._freeze_at = self._stable_at = 0.0
        self._stable_choice = ''
        self._committed_url = ''
        self._payload, self._error = {}, ''
        dialog = DiscoveryDialog(self.owner, page_url=url, listen_seconds=seconds)
        self.dialog = dialog
        dialog.setWindowTitle('批次正在分析 · MediaFlow')
        dialog.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog._quality_preference = self._preference
        dialog.page_url.setReadOnly(True)
        # 这里每条来源只交接一个正片；多媒体/全部画质仍保留在原手动发现入口。
        dialog.download_all_button.hide()
        dialog.worker.batch_ready.connect(lambda candidates, generation, current=dialog:
                                         self._frozen(current, candidates, generation))
        dialog.media_selected.connect(lambda candidate, current=dialog: self._manual_selected(current, candidate))
        dialog.shutdown_complete.connect(lambda current=dialog: self._released(current))
        dialog.showMinimized()
        dialog.deep_find()
        self._timer.start()

    def _poll(self):
        dialog = self.dialog
        if not dialog:
            return
        now = time.monotonic()
        if dialog._closing:
            return
        current_url = dialog.page.url().toString()
        if dialog._document_ready and not self._committed_url:
            self._committed_url = current_url
        if self._committed_url and current_url != self._committed_url:
            self._error = '分析期间页面已切换，未自动选择新页面的媒体；请返回原始网址后重试。'
            dialog.shutdown()
            return
        if self._freeze_at:
            if now - self._freeze_at > 20:
                self._error = '候选检测超时，已停止此页；可稍后重试。'
                dialog.shutdown()
            return
        choice = automatic_choice(dialog._groups, self._url, self._preference)
        identity = (choice[0].id + ':' + choice[1].id) if choice else ''
        if identity != self._stable_choice:
            self._stable_choice, self._stable_at = identity, now
        # 等候 DOM 与当前播放器关联稳定；没有身份依据时始终保留人工选择。
        settled = identity and now - self._stable_at >= 2 and dialog._document_ready
        if settled or not dialog._listening or now >= self._deadline:
            self._freeze_at = now
            dialog._listening = False
            dialog.listen_timer.stop()
            dialog.resolver_timer.stop()
            dialog._resolver_epoch += 1
            dialog._fetch_queue.clear()
            dialog.observer.stop()
            dialog.find_button.setEnabled(False)
            dialog.deep_find_button.setEnabled(False)
            dialog.worker.freeze()

    def _frozen(self, dialog, candidates, generation):
        if dialog is not self.dialog or dialog._closing or generation != dialog.worker.generation or not self._freeze_at:
            return
        if self._committed_url and dialog.page.url().toString() != self._committed_url:
            self._error = '分析期间页面已切换，未交接其他页面的候选；请重试原始网址。'
            self._payload = {}
            dialog.shutdown()
            return
        dialog._candidates = [candidate for candidate in candidates if dialog._candidate_allowed(candidate)]
        dialog._render_candidates()
        groups = deepcopy(dialog._groups)
        for group in groups:
            for candidate in group.candidates:
                self.owner._remember_resolver_session(candidate, dialog)
            if group.selected_candidate is not None:
                self.owner._remember_resolver_session(group.selected_candidate, dialog)
        self._payload = {'groups': groups}
        if not groups:
            self._error = dialog._empty_diagnostic()
        dialog.shutdown()

    def _manual_selected(self, dialog, candidate):
        # 用户在“查看分析页”中选择时也只缓存候选，由批次统一执行资格检查。
        if dialog is not self.dialog or dialog._closing:
            return
        if self._committed_url and dialog.page.url().toString() != self._committed_url:
            self._error = '分析期间页面已切换，请重试原始网址后选择正片。'
            self._payload = {}
            dialog.shutdown()
            return
        self.owner._remember_resolver_session(candidate, dialog)
        groups = deepcopy(dialog._groups)
        self._payload = {'groups': groups, 'manual_candidate': candidate.id}

    def _released(self, dialog):
        if dialog is not self.dialog:
            return
        self._timer.stop()
        item_id, payload = self.item_id, self._payload
        error = self._error or ('' if payload else '分析页已关闭；可重试此网址。')
        self.dialog, self.item_id = None, ''
        self._payload = {}
        dialog.deleteLater()
        # 只有 shutdown_complete 才释放串行浏览器租约。
        self.finished.emit(item_id, payload, error)

    def cancel(self):
        if self.dialog:
            self._error = '媒体发现已暂停或取消。'
            self._payload = {}
            self.dialog.shutdown()

    def show_page(self):
        if self.dialog and not self.dialog._closing:
            self.dialog.showNormal()
            self.dialog.raise_()
            self.dialog.activateWindow()
