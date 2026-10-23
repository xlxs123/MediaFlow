"""独立媒体查找浮窗；确定导入输入，卡片下载复用主窗口原下载操作。"""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
import time
from urllib.parse import urlsplit, parse_qsl

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
                              QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
                              QAbstractItemView, QSplitter, QWidget, QCheckBox, QPlainTextEdit, QComboBox, QMessageBox,
                              QScrollArea, QFrame, QTabWidget)

from core.browser_observer import BrowserNetworkObserver, capture_body_limit, capture_total_limit
from core.curl_parser import has_temporary_signature
from core.discovery_worker import DiscoveryWorker
from core.dom_scanner import DOM_OBSERVER_SCRIPT
from core.request_context import validate_url
from core.resolver_models import PageContext, ResolverRequest
from core.config_media import config_media_urls
from core.dash_json import parse_dash_description
from core.media_groups import MediaGroup, group_media, ordered_candidates, available_candidate
from core.media_quality import QUALITY_PREFERENCES, update_quality, quality_candidates, recommend_candidate
from core.media_details import enrich_media_details
from core.media_discovery import is_verified_resolver
from ui.dialogs import ResolverTracePanel, format_diagnostic, format_resolver_chain, manifest_label
from ui.media_cards import CandidateCard, CARD_STYLES
from ui.media_details_panel import MediaDetailsPanel, bitrate_text, size_text
from ui.media_presentation import (display_text, duration_text, confidence_status,
                                   confidence_reasons, encryption_text, diagnostic_text, reason_text, candidate_state_text)
from utils.security import Redactor
from utils.build_info import runtime_summary


def page_media_identity(url):
    parsed = urlsplit(url)
    identifiers = tuple(sorted((k,v) for k,v in parse_qsl(parsed.query)
                              if k in ('modal_id', 'video_id', 'item_id', 'aweme_id')))
    return parsed.scheme, parsed.netloc, parsed.path, identifiers, parsed.fragment


def candidate_details(candidate):
    values = candidate.safe_diagnostic()
    manifest = values.get('manifest') or {}
    values.update(keys=manifest.get('keys', []), resources=manifest.get('resources', []),
                  evidence=manifest.get('evidence', []),
                  hls_playlist_type=candidate.playlist_type,
                  stage='自动查找 / 已验证 Resolver' if is_verified_resolver(candidate) else '自动查找 / 浏览器实际请求')
    lines = [f'清晰度：{display_text(values["resolution"])} · {display_text(values["dimensions"])}',
             f'来源：{display_text(values["source_type"] if is_verified_resolver(candidate) else " / ".join(values["sources"]) or values["source_type"])}',
             f'播放器类型：{values.get("player_type") or "普通媒体 / 未知"}',
             f'Content-Type：{values["content_type"] or "--"}',
             f'时长：{duration_text(candidate.duration or candidate.metadata.get("detail_duration"))}',
             f'Codecs：{values["codecs"] or "--"}',
             f'帧率：{values["frame_rate"] or "--"}',
             f'视频编码：{display_text(candidate.videoCodec)}',
             f'音频编码：{display_text(candidate.audioCodec)}',
             f'视频码率：{bitrate_text(candidate.videoBitrate)}',
             f'音频码率：{bitrate_text(candidate.audioBitrate)}',
             f'文件大小：{size_text(candidate.estimatedSize, candidate.metadata.get("size_is_estimate") is not False)}',
             f'容器：{display_text(candidate.container)}',
             f'上下文：{"完整" if candidate.context_complete else "未捕获到可复现的实际请求"}',
             '协议解析：已验证' if is_verified_resolver(candidate) else '协议解析：浏览器实际请求 / 内容检测',
             f'可信度：{candidate.confidence}/100（真实性规则评分，不是下载成功证明）',
             f'状态：{confidence_status(candidate.confidence)}',
             f'候选流程：{candidate_state_text(candidate)}',
             '不可下载原因：' + ('；'.join(candidate.unavailable_reasons) or '无'),
             '请求 Header：' + (', '.join(sorted(candidate.request_context.headers)) or '未观察到'),
             'Cookie：' + ('已捕获，仅保存在本地内存' if candidate.request_context.cookies else '未观察到'),
             f'质量评分：{candidate.qualityScore}/100',
             f'综合评分：{values.get("combined_score", "--")}（质量 × 0.6 + 可信度 × 0.4；仅同一视频比较）',
             f'推荐：{values.get("quality_recommendation") or "待分析"}',
             f'广告特征：{"有，请核对是否为目标视频" if candidate.is_ad else "未观察到明确特征"}']
    if has_temporary_signature(candidate.url):
        lines.append('临时媒体地址：签名完整保存在内存中，建议及时下载。')
    lines.extend(['', '诊断原因：', *confidence_reasons(candidate), '', '真实性评分原因：'])
    lines.extend(f'{"✓" if item.get("points", 0) >= 0 else "⚠"} {reason_text(item.get("reason", ""))} ({item.get("points", 0):+d})'
                 for item in values['confidenceDetails'])
    if values.get('probe_error'):
        lines.extend(['', '检测提示：' + values['probe_error']])
    if values.get('quality_details'):
        lines.extend(['', '质量评分原因：'])
        lines.extend(f'{"✓" if item.get("points", 0) >= 0 else "⚠"} {item.get("reason", "")} ({item.get("points", 0):+d})'
                     for item in values['quality_details'])
    lines.extend(['', format_diagnostic(values)])
    return Redactor(candidate.request_context.headers, [candidate.url, candidate.final_url]).redact(diagnostic_text('\n'.join(lines)))


class PrivatePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, *_):
        # 网页 console 可能打印请求凭据；不把网页日志传入下载日志或终端。
        pass


class DiscoveryDialog(QDialog):
    download_now = Signal()
    media_selected = Signal(object)
    batch_requested = Signal(object, dict)
    shutdown_complete = Signal()

    def __init__(self, parent=None, page_url='', listen_seconds=60, resolver_registry=None, session_profile=None, browser_backend=''):
        super().__init__(parent)
        self.setWindowTitle('MediaFlow · 媒体发现')
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(1120, 840)
        self.setMinimumSize(860, 640)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
        self._closing = False
        self._page_gone = self._profile_gone = False
        self._pages_alive = 0
        self._selected_id = None
        self._user_selected = False
        self._candidates = []
        self._groups = []
        self._group_checks = {}
        self._group_choices = {}
        self._quality_options = {}
        self._quality_preference = getattr(parent, 'settings', {}).get('quality_preference', 'auto')
        body_limit_mib = getattr(parent, 'settings', {}).get('browser_body_limit_mib', 16)
        self._max_body_bytes = capture_body_limit(body_limit_mib * 1024 * 1024 if type(body_limit_mib) is int else None)
        self._max_history_bytes = capture_total_limit(self._max_body_bytes)
        self._batch_pending = self._batch_enqueued = False
        self._player_type = ''
        self._pending_url = ''
        self._listening = False
        self.listen_seconds = listen_seconds
        self._started_at = 0
        self._page_loaded = False
        self._document_ready = False
        self._navigation_dom_after = 0.0
        self._worker_done = False
        self._shutdown_emitted = False
        self._network_history = OrderedDict()
        self._last_dom = {}
        self._resolver_diagnostics = []
        self._generic_diagnostic = {}
        self._fetch_queue = []
        self._fetch_tried = set()
        self._excluded_resource_urls = set()
        self._fetch_inflight = None
        self._resolver_epoch = 0
        self._resolver_succeeded = False
        self._resolver_verified = False
        self._showing_verified = False
        self._owns_profile = session_profile is None
        self._fast_ready = False
        self._navigation_urls = []
        self._history_index = -1
        self._history_target_index = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        address_row = QHBoxLayout()
        address_row.addWidget(QLabel('页面地址'))
        self.page_url = QLineEdit(page_url)
        self.page_url.setPlaceholderText('粘贴视频网页的 http:// 或 https:// 地址')
        self.page_url.returnPressed.connect(self.find)
        address_row.addWidget(self.page_url, 1)
        layout.addLayout(address_row)
        self.status_label = QLabel('等待查找')
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.build_status_label = QLabel(runtime_summary() + f' · 响应采集上限：{self._max_body_bytes // (1024 * 1024)} MiB')
        self.build_status_label.setObjectName('muted')
        self.build_status_label.setWordWrap(True)
        layout.addWidget(self.build_status_label)
        hint = QLabel('在下方网页播放或拖动视频可帮助识别正片。候选中的签名和凭据已隐藏。')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        splitter = QSplitter(Qt.Orientation.Vertical)
        from ui.webview2_browser import select_browser_backend, WebView2View, WebView2Profile
        self.browser_backend = select_browser_backend(session_profile, browser_backend)
        if self.browser_backend == 'webview2':
            self.view = WebView2View()
            if session_profile is None and hasattr(parent, 'webview2_profile'):
                self.profile = parent.webview2_profile()
                self._owns_profile = False
            else:
                self.profile = WebView2Profile(self) if session_profile is None else session_profile
            self.profile.host.set_capture_body_limit(self._max_body_bytes)
        else:
            self.view = QWebEngineView()
            # Qt 兼容模式保留原有 off-the-record 行为。
            self.profile = QWebEngineProfile(self) if session_profile is None else session_profile
            if self._owns_profile:
                self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
                self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            from core.browser_runtime import configure_browser_profile
            configure_browser_profile(self.profile)
        self.page = self._new_page()
        self.view.setPage(self.page)
        from ui.browser_controls import BrowserControls
        self.browser_controls = BrowserControls(self.view, self)
        self.browser_controls.back_requested.connect(lambda: self._navigate_history(-1))
        self.browser_controls.forward_requested.connect(lambda: self._navigate_history(1))
        self.browser_controls.reload_requested.connect(self.deep_find)
        self.browser_controls.large_mode_changed.connect(self._large_mode)
        self.browser_controls.status_changed.connect(self.status_label.setText)
        layout.insertWidget(0, self.browser_controls)
        self.splitter = splitter
        splitter.addWidget(self.view)
        result_panel = QWidget()
        self.result_panel = result_panel
        result_layout = QVBoxLayout(result_panel)
        result_layout.setContentsMargins(0, 5, 0, 0)
        result_header = QHBoxLayout()
        group_label = QLabel('独立视频')
        group_label.setToolTip('同一视频的清晰度归为一组；质量推荐只在组内比较。')
        result_header.addWidget(group_label)
        result_header.addWidget(QLabel('下载质量'))
        self.quality_preference_combo = QComboBox()
        for value, label in QUALITY_PREFERENCES:
            self.quality_preference_combo.addItem(label, value)
        self.quality_preference_combo.setCurrentIndex(max(0, self.quality_preference_combo.findData(self._quality_preference)))
        self.quality_preference_combo.currentIndexChanged.connect(self._quality_preference_changed)
        result_header.addWidget(self.quality_preference_combo)
        self.select_all_button = QPushButton('全选')
        self.select_none_button = QPushButton('全不选')
        self.select_all_button.clicked.connect(lambda: self._check_groups(True))
        self.select_none_button.clicked.connect(lambda: self._check_groups(False))
        result_header.addWidget(self.select_all_button)
        result_header.addWidget(self.select_none_button)
        result_header.addStretch()
        self.include_low = QCheckBox('低可信度（0）· 展开查看')
        self.include_low.setToolTip('低可信度表示证据不足；候选仍然保留，可展开查看诊断。')
        self.include_low.toggled.connect(self._render_candidates)
        result_layout.addLayout(result_header)
        self.discovery_stats_label = QLabel('页面请求：0 · 媒体请求：0 · 候选：0 · 可下载：0')
        self.discovery_stats_label.setObjectName('muted')
        self.discovery_stats_label.setWordWrap(True)
        self.discovery_stats_label.setTextFormat(Qt.TextFormat.PlainText)
        result_layout.addWidget(self.discovery_stats_label)
        # 保留原选择适配器和回归接口，可见部分改为轻量卡片。
        self.table = QTableWidget(0, 9, result_panel)
        self.table.setHorizontalHeaderLabels(['下载', '视频标题', '清晰度', '时长', '类型', '加密 / DRM', '可信度 / 状态', '来源', '媒体地址'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(8, 200)
        self.table.itemSelectionChanged.connect(lambda: self._selection_changed(user=True))
        self.table.cellClicked.connect(self._user_selection_changed)
        self.table.itemActivated.connect(lambda _: self._user_selection_changed())
        self.table.itemChanged.connect(self._group_checked)
        self.table.hide()
        self._candidate_cards = {}
        self.candidate_card_scroll = QScrollArea()
        self.candidate_card_scroll.setObjectName('cardScroll')
        self.candidate_card_scroll.setWidgetResizable(True)
        self.candidate_card_scroll.setFrameShape(QFrame.Shape.NoFrame)
        card_content = QWidget()
        self.candidate_card_layout = QVBoxLayout(card_content)
        self.candidate_card_layout.setContentsMargins(0, 0, 9, 0)
        self.candidate_card_layout.setSpacing(10)
        self.candidate_empty_state = QLabel('播放网页中的视频后，媒体候选会显示在这里')
        self.candidate_empty_state.setObjectName('emptyState')
        self.candidate_empty_state.setWordWrap(True)
        self.candidate_empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.candidate_card_layout.addWidget(self.candidate_empty_state)
        self.candidate_card_layout.addWidget(self.include_low)
        self.include_low.hide()
        self.candidate_card_layout.addStretch()
        self.candidate_card_scroll.setWidget(card_content)
        result_layout.addWidget(self.candidate_card_scroll, 3)
        self.setStyleSheet(self.styleSheet() + CARD_STYLES)
        self.summary_label = QLabel('媒体类型：未知  ·  清晰度：未知  ·  加密：未知  ·  DRM：未知')
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        result_layout.addWidget(self.summary_label)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.setMaximumHeight(190)
        self.media_details = MediaDetailsPanel()
        self.detail_tabs.addTab(self.media_details, '媒体详情')
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText('选择候选后显示检测详情、请求上下文与评分原因。')
        self.details.setMaximumBlockCount(2000)
        self.details.setMaximumHeight(110)
        self.detail_tabs.addTab(self.details, '检测详情')
        from ui.track_selector import TrackSelector
        self.track_selector = TrackSelector()
        self.detail_tabs.addTab(self.track_selector, '音轨与字幕')
        result_layout.addWidget(self.detail_tabs, 1)
        self.trace = ResolverTracePanel(self)
        result_layout.addWidget(self.trace)
        splitter.addWidget(result_panel)
        splitter.setSizes([220, 510])
        layout.addWidget(splitter, 1)
        batch_options = QHBoxLayout()
        self.auto_fallback = QCheckBox('失败时自动使用下一可用清晰度')
        self.all_qualities = QCheckBox('下载同一视频的所有清晰度')
        self.auto_fallback.setToolTip('默认关闭；开启后才自动降级。降级使用独立暂存文件，避免拼接不同质量的分片。')
        batch_options.addWidget(self.auto_fallback)
        batch_options.addWidget(self.all_qualities)
        batch_options.addStretch()
        layout.addLayout(batch_options)
        actions = QHBoxLayout()
        self.player_label = QLabel('播放器类型：未知')
        self.player_label.setTextFormat(Qt.TextFormat.PlainText)
        actions.addWidget(self.player_label)
        actions.addStretch()
        self.find_button = QPushButton('查找')
        self.find_button.clicked.connect(self.find)
        self.deep_find_button = QPushButton('深度查找')
        self.deep_find_button.clicked.connect(self.deep_find)
        self.browser_environment = None
        self.environment_button = QPushButton('浏览器诊断')
        self.environment_button.clicked.connect(self._show_browser_environment)
        self.confirm_button = QPushButton('确定')
        self.confirm_button.setObjectName('primary')
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.confirm)
        self.download_all_button = QPushButton('下载全部')
        self.download_all_button.setEnabled(False)
        self.download_all_button.clicked.connect(self.download_all)
        self.cancel_button = QPushButton('取消')
        self.cancel_button.clicked.connect(self.reject)
        for button in (self.find_button, self.deep_find_button, self.environment_button,
                       self.download_all_button, self.confirm_button, self.cancel_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        if self.browser_backend == 'webview2':
            from core.webview2_observer import WebView2NetworkObserver
            self.observer = WebView2NetworkObserver(self.page, self, max_body_bytes=self._max_body_bytes)
        else:
            self.observer = BrowserNetworkObserver(self.page, self, max_body_bytes=self._max_body_bytes)
        self.observer.ready.connect(self._observer_ready)
        self.observer.observation.connect(self._network)
        if hasattr(self.observer, 'document_ready'):
            self.observer.document_ready.connect(self._document_loaded)
        self.observer.dom.connect(self._dom)
        self.observer.error.connect(self._observer_error)
        self.observer.status.connect(self._observer_status)
        self.worker = DiscoveryWorker(parent or self, resolver_registry=resolver_registry, max_body_bytes=self._max_body_bytes)
        self.worker.candidates_changed.connect(self._update_candidates)
        self.worker.batch_ready.connect(self._batch_ready)
        self.worker.resolver_result.connect(self._resolver_result)
        self.worker.diagnostics_changed.connect(self._generic_details)
        self.worker.status.connect(self._observer_status)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()
        self.listen_timer = QTimer(self)
        self.listen_timer.setSingleShot(True)
        self.listen_timer.timeout.connect(self._listen_ended)
        self.resolver_timer = QTimer(self)
        self.resolver_timer.setSingleShot(True)
        self.resolver_timer.setInterval(180)
        self.resolver_timer.timeout.connect(self._resolve_page)

    def _large_mode(self, enabled):
        self.result_panel.setVisible(not enabled)

    def _show_browser_environment(self):
        if self._closing:
            return
        if self.browser_environment is None:
            from ui.browser_environment import BrowserEnvironmentDialog
            self.browser_environment = BrowserEnvironmentDialog(self)
            self.browser_environment.idle.connect(self._finish_shutdown)
        self.browser_environment.show()
        self.browser_environment.refresh()

    def retain_session_profile(self):
        """由主窗口为已验证任务保留会话；不写入用户持久配置。"""
        if self._closing:
            return None
        if self._owns_profile:
            self.profile.setParent(self.parent())
            self._owns_profile = False
        return self.profile

    def _url_changed(self, url):
        if not self._closing:
            target = url.toString()
            previous = self._navigation_urls[self._history_index] if self._history_index >= 0 else ''
            changed_work = bool(previous and page_media_identity(previous) != page_media_identity(target))
            self.page_url.setText(target)
            if self._history_target_index is not None:
                self._history_index = self._history_target_index
                self._navigation_urls[self._history_index] = target
                self._history_target_index = None
            elif self._history_index < 0 or self._navigation_urls[self._history_index] != target:
                self._navigation_urls = self._navigation_urls[:self._history_index + 1] + [target]
                self._history_index = len(self._navigation_urls) - 1
            self.browser_controls.sync_history(self._history_index > 0,
                                                self._history_index + 1 < len(self._navigation_urls))
            if changed_work and (self._document_ready or self._page_loaded or self._candidates
                                 or self._network_history or self._last_dom):
                # history.pushState 的作品切换没有 loadStarted，仍须隔离旧候选和在途回调。
                self._navigation_dom_after = time.time()
                self._reset_analysis()
                self.observer.reset()
                if self._listening:
                    self.observer.scan_dom()
                    self.listen_timer.start(max(1, int(self.listen_seconds * 1000)))
                else:
                    self._page_loaded = self._document_ready = False

    def _navigate_history(self, delta):
        index = self._history_index + delta
        if 0 <= index < len(self._navigation_urls):
            self._history_target_index = index
            self.page_url.setText(self._navigation_urls[index])
            self.deep_find()

    def _navigation_started(self):
        if self._closing:
            return
        self._document_ready = False
        self._navigation_dom_after = time.time()
        if self._listening and self._page_loaded:
            self._reset_analysis()
            self.observer.reset()
            self._page_loaded = False
            self.listen_timer.start(max(1, int(self.listen_seconds * 1000)))

    def _new_page(self):
        if self.browser_backend == 'webview2':
            from ui.webview2_browser import WebView2Page
            page = WebView2Page(self.profile, self)
            page.backendError.connect(lambda message, page=page: self._page_callback(page, self._observer_error, message))
        else:
            page = PrivatePage(self.profile, self)
        self._pages_alive += 1
        script = QWebEngineScript()
        script.setName('media-discovery-observer')
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(DOM_OBSERVER_SCRIPT)
        page.scripts().insert(script)
        page.loadFinished.connect(lambda success, page=page: self._page_callback(page, self._load_finished, success))
        page.loadStarted.connect(lambda page=page: self._page_callback(page, self._navigation_started))
        page.urlChanged.connect(lambda url, page=page: self._page_callback(page, self._url_changed, url))
        page.renderProcessTerminated.connect(lambda *args, page=page: self._page_callback(page, self._renderer_failed, *args))
        page.fullScreenRequested.connect(lambda request: request.reject())
        page.destroyed.connect(self._page_destroyed)
        return page

    def _page_callback(self, page, callback, *args):
        if page is self.page:
            callback(*args)

    def _replace_page(self):
        previous = self.page
        previous.triggerAction(QWebEnginePage.WebAction.Stop)
        self.page = self._new_page()
        self.view.setPage(self.page)
        self.observer.set_page(self.page)
        previous.deleteLater()

    def find(self):
        if self._closing:
            return
        target = self.page_url.text().strip()
        if not self._page_loaded or self.page.url().toString() != target:
            return self.deep_find()
        # Quick discovery observes the current document. Never reload it.
        self._reset_analysis(keep_history=True)
        self._listening = True
        self._started_at = time.monotonic()
        self.listen_timer.start(max(1, int(self.listen_seconds * 1000)))
        self.status_label.setText('快速查找当前页面：解析器 → DOM → Performance → 已有网络记录')
        self._fast_ready = True
        if not self.observer._active:
            self.observer.start()
        self._resolve_page()
        self.worker.submit('dom', self._last_dom)
        for observation in self._network_history.values():
            self.worker.submit('network', deepcopy(observation))
        self.observer.scan_dom()

    def _reset_analysis(self, keep_history=False):
        self.resolver_timer.stop()
        self._resolver_epoch += 1
        self._resolver_succeeded = False
        self._resolver_verified = False
        self._showing_verified = False
        self._fetch_queue.clear()
        self._fetch_tried.clear()
        self._excluded_resource_urls.clear()
        self._fetch_inflight = None
        self._resolver_diagnostics.clear()
        self._generic_diagnostic.clear()
        if not keep_history:
            self._network_history.clear()
            self._last_dom = {}
        self._candidates = []
        self._groups.clear()
        self._group_checks.clear()
        self._group_choices.clear()
        self._batch_pending = self._batch_enqueued = False
        self._selected_id = None
        self._user_selected = False
        self._player_type = ''
        self.worker.reset()
        self._render_candidates()

    def deep_find(self):
        if self._closing:
            return
        try:
            validate_url(self.page_url.text().strip())
        except ValueError as error:
            self.status_label.setText(str(error))
            return
        self.listen_timer.stop()
        self._listening = False
        self._page_loaded = False
        self._pending_url = self.page_url.text().strip()
        self._document_ready = False
        self._navigation_dom_after = time.time()
        self._fast_ready = False
        self._reset_analysis()
        self.observer.stop()
        # 全新的 page / CDP target 隔离旧文档、iframe、仍在途的网络和取消事件。
        # Profile 保留，页面内登录产生的会话 Cookie 仍可在下一轮正常使用。
        self._replace_page()
        self.observer.reset()
        self._render_candidates()
        self._listening = True
        self._page_loaded = False
        self._started_at = time.monotonic()
        self.status_label.setText('正在监听网页媒体请求……')
        self.observer.start()

    def _observer_ready(self):
        if self._closing or not self._listening:
            return
        self.listen_timer.start(max(1, int(self.listen_seconds * 1000)))
        if self._fast_ready:
            self._fast_ready = False
            self.observer.scan_dom()
            self._next_fetch()
            return
        # Network.enable 先完成，首次导航的重定向和早期播放器请求不会漏掉。
        self.page.load(QUrl(self._pending_url))
        self.observer.scan_dom()

    def _load_finished(self, success):
        if self._closing or not self._listening:
            return
        self._page_loaded = True
        self._document_ready = True
        self.observer.scan_dom()
        self._resolve_page()
        if not success and not self._candidates:
            self.status_label.setText('网页加载未完成，可检查地址或在网页内登录后再次查找。')

    def _document_loaded(self):
        if (self._listening and not self._closing
                and self.page.url().scheme() in ('http', 'https')):
            self._document_ready = True
            self.observer.scan_dom()

    def _renderer_failed(self, *_):
        if not self._closing:
            self.observer.stop()
            self._listening = False
            self.listen_timer.stop()
            self.status_label.setText('内嵌浏览器已停止，请点击“查找”重新加载网页。')

    def _network(self, observation):
        if self._listening and not self._closing:
            self._network_history[observation.request_id] = deepcopy(observation)
            self._network_history.move_to_end(observation.request_id)
            while (len(self._network_history) > 512 or
                   sum(len(obs.body or b'') for obs in self._network_history.values()) > self._max_history_bytes):
                self._network_history.popitem(last=False)
            self.worker.submit('network', observation)
            if not self.resolver_timer.isActive():
                self.resolver_timer.start()
            if (not self._resolver_verified and observation.body_complete and observation.resource_type in ('Fetch', 'XHR')
                    and 200 <= observation.status < 300):
                # 完整 DASH 描述交由 worker 生成视频/音频组合；不能再把轨道裸 URL
                # 或 codecs、SegmentBase 的字符串泛扫为独立下载目标。
                dash = parse_dash_description(observation.body, observation.context.final_url, max_bytes=self._max_body_bytes)
                for url in ([] if dash else config_media_urls(observation.body, observation.context.final_url,
                                                              observation.content_type, max_bytes=self._max_body_bytes)):
                    self._queue_fetch(ResolverRequest(url, metadata={'frame_id': observation.frame_id}), resolver=False)

    def _dom(self, snapshot):
        if (self._listening and not self._closing and (self._page_loaded or self._document_ready)
                and snapshot.get('timestamp', 0) >= self._navigation_dom_after):
            if snapshot.get('page_url') == self.page.url().toString():
                self._last_dom = deepcopy(snapshot)
                self._resolve_page()
                # Performance is only a hint. An actual browser response must confirm it.
                known = {obs.context.url for obs in self._network_history.values()}
                for resource in ([] if self._resolver_verified else snapshot.get('performance_resources', [])[-128:]):
                    url = resource.get('url', '')
                    if url not in known and resource.get('type') in ('video', 'audio', 'fetch', 'xmlhttprequest'):
                        if any(hint in url.split('?', 1)[0].lower() for hint in ('.m3u8', '.mpd', '.mp4', '/manifest', '/stream')):
                            self._queue_fetch(ResolverRequest(url), resolver=False)
            self.worker.submit('dom', snapshot)

    def _resolve_page(self):
        if self._closing or not self._listening or not self._page_loaded:
            return
        snapshot = deepcopy(self._last_dom)
        snapshot['resolver_attempted_urls'] = list(self._fetch_tried)
        snapshot['resolver_inflight_url'] = self._fetch_inflight.url if self._fetch_inflight else ''
        self.worker.submit('page', PageContext(self.page.url().toString(),
                           self._last_dom.get('next_data'), tuple(deepcopy(list(self._network_history.values()))),
                           snapshot))

    def _resolver_result(self, result, generation):
        if self._closing or generation != self.worker.generation:
            return
        excluded = {url for diagnostic in result.diagnostics
                    for url in diagnostic.get('excluded_urls', ()) if isinstance(url, str)}
        if excluded:
            self._excluded_resource_urls.update(excluded)
            self._fetch_queue = [(request, resolver) for request, resolver in self._fetch_queue if request.url not in excluded]
            self._candidates = [candidate for candidate in self._candidates if self._candidate_allowed(candidate)]
            self._render_candidates()
        if self._resolver_verified and not any(is_verified_resolver(candidate) for candidate in result.candidates):
            return
        self._resolver_diagnostics = deepcopy(result.diagnostics)
        if result.success:
            self._resolver_succeeded = True
            self._resolver_verified = self._resolver_verified or any(is_verified_resolver(candidate) for candidate in result.candidates)
            self._fetch_queue = [] if self._resolver_verified else [(request, is_resolver)
                                for request, is_resolver in self._fetch_queue if not is_resolver]
        else:
            for request in result.requests:
                self._queue_fetch(request, resolver=True)
        self._next_fetch()
        if not self.selected_candidate() or not self.selected_candidate().metadata.get('resolver_trace'):
            self.trace.set_values(self._resolver_trace_values())

    def _resolver_trace_values(self, values=None):
        values = deepcopy(values or next((item for item in reversed(self._resolver_diagnostics) if item.get('trace')), {}))
        excluded = [{'role': item.get('classification') or item.get('resource_role'),
                     'request_id': item.get('request_id'), 'urls': item['excluded_urls']}
                    for item in self._resolver_diagnostics if item.get('excluded_urls')]
        if excluded:
            key = 'resolver_trace' if values.get('resolver_trace') else 'trace'
            values[key] = {**values.get(key, {}), 'excluded_resources': excluded}
        return values

    def _generic_details(self, values, generation):
        if not self._closing and generation == self.worker.generation:
            self._generic_diagnostic = deepcopy(values)
            self._update_discovery_stats()

    def _queue_fetch(self, request, resolver=False):
        if (self._closing or not self._listening or len(self._fetch_tried) >= 48
                or self._resolver_verified and not resolver
                or request.url in self._excluded_resource_urls
                or request.url in self._fetch_tried or any(item[0].url == request.url for item in self._fetch_queue)):
            return
        if resolver:
            self._fetch_queue.insert(0, (request, True))
        else:
            self._fetch_queue.append((request, False))
        self._next_fetch()

    def _next_fetch(self):
        if self._closing or not self._listening or self._fetch_inflight or not self.observer._enabled:
            return
        if not self._fetch_queue:
            return
        request, is_resolver = self._fetch_queue.pop(0)
        if is_resolver and self._resolver_succeeded:
            return self._next_fetch()
        self._fetch_tried.add(request.url)
        self._fetch_inflight = request
        epoch = self._resolver_epoch
        def completed(value, success):
            if self._closing or epoch != self._resolver_epoch:
                return
            self._fetch_inflight = None
            self.resolver_timer.start()
            QTimer.singleShot(250, self._next_fetch)
        if not self.observer.fetch_url(request.url, completed, frame_id=request.metadata.get('frame_id', ''),
                                       headers=request.headers):
            self._fetch_inflight = None

    def _observer_status(self, text):
        if not self._closing and self._listening and not self._candidates:
            self.status_label.setText(text)

    def _observer_error(self, text):
        if not self._closing:
            self.status_label.setText(text)

    def _update_candidates(self, candidates, player_type, generation):
        if self._closing or generation != self.worker.generation:
            return
        self._candidates, self._player_type = [candidate for candidate in candidates if self._candidate_allowed(candidate)], player_type
        self._filtered_candidate_count = len(candidates) - len(self._candidates)
        self._render_candidates()
        if self._batch_pending:
            return
        count = len(self._candidates)
        if count:
            suffix = '，仍在监听播放 / 拖动请求' if self._listening else ''
            if self.selected_candidate() and available_candidate(self.selected_candidate()):
                score = self.selected_candidate().confidence
                self.status_label.setText(f'{confidence_status(score)} · 可信度 {score}/100：' + ('已选择候选，请核对视频信息' if score >= 70 else '请确认是否为当前正片') + suffix)
            else:
                low = sum(candidate.confidence < 40 for candidate in self._candidates)
                self.status_label.setText(f'找到 {count} 个候选' + (f'（其中 {low} 个低可信度，默认折叠，可展开查看）' if low else '')
                                          + f'，请在列表确认当前正片{suffix}')
        elif self._listening:
            self.status_label.setText('正在分析候选资源……请在网页播放视频。')

    def discovery_counts(self):
        stats = getattr(self, '_generic_diagnostic', {}).get('statistics', {})
        observer = getattr(self, 'observer', None)
        network = observer.network_diagnostics() if observer and hasattr(observer, 'network_diagnostics') else {}
        counts = network.get('counts', {}) if isinstance(network, dict) else {}
        candidates = self._candidates
        return {'page_requests': counts.get('Network.requestWillBeSent', stats.get('page_requests', 0)),
                'media_requests': counts.get('media_requests', stats.get('media_requests', 0)),
                'candidates': len(candidates), 'selectable': len(candidates),
                'downloadable': sum(available_candidate(candidate) for candidate in candidates),
                'recommended': sum(candidate.confidence >= 80 for candidate in candidates),
                'confirmation': sum(40 <= candidate.confidence < 80 for candidate in candidates),
                'low_confidence': sum(candidate.confidence < 40 for candidate in candidates),
                'filtered': stats.get('filtered', 0) + getattr(self, '_filtered_candidate_count', 0)}

    def _update_discovery_stats(self):
        if not hasattr(self, 'discovery_stats_label'):
            return
        values = self.discovery_counts()
        self.discovery_stats_label.setText(
            ' · '.join(f'{label}：{values[key]}' for key, label in (
                ('page_requests', '页面请求'), ('media_requests', '媒体请求'), ('candidates', '候选'),
                ('selectable', '可查看选择'), ('downloadable', '可下载'), ('recommended', '推荐'),
                ('confirmation', '需确认'), ('low_confidence', '低可信度'), ('filtered', '过滤资源'))))
        stats = getattr(self, '_generic_diagnostic', {}).get('statistics', {})
        reasons = stats.get('filtered_reasons', {})
        dash = (f'DASH 描述：{stats.get("dash_descriptions", 0)}'
                f'（页面内嵌：{stats.get("dash_inline_descriptions", 0)}）'
                f' · 视频轨：{stats.get("dash_video_tracks", 0)} · 音频轨：{stats.get("dash_audio_tracks", 0)}')
        if stats.get('dash_descriptions'):
            self.discovery_stats_label.setText(self.discovery_stats_label.text() + '\n' + dash)
        self.discovery_stats_label.setToolTip('过滤资源不作为独立视频；已关联分片交给对应清单。\n'
            + dash + '\n' + '\n'.join(f'{reason}：{count}' for reason, count in reasons.items()))

    def _visible_candidates(self):
        return [candidate for candidate in self._candidates
                if self.include_low.isChecked() or candidate.confidence >= 40]

    def _candidate_allowed(self, candidate):
        return is_verified_resolver(candidate) or not ({candidate.url, candidate.final_url, candidate.original_url}
                                                     & self._excluded_resource_urls)

    def _render_candidates(self, *_):
        previous_groups = self._groups
        for candidate in self._candidates:
            enrich_media_details(candidate)
            update_quality(candidate)
        verified = [candidate for candidate in self._candidates if is_verified_resolver(candidate)]
        page = self.page.url().toString() or self.page_url.text().strip()
        title = self._last_dom.get('page_title', '')
        if verified and not self._showing_verified:
            self._group_checks.clear()
            self._group_choices.clear()
            self._user_selected = False
        self._showing_verified = bool(verified)
        self._groups = group_media(verified or self._candidates, page_title=title, source_page=page)
        if verified:
            # 通用诊断独立分组，避免同 URL 或同 videoId 的较新猜测在
            # MediaGroup 的清晰度/去重过程中替换已验证正片。
            generic = group_media([candidate for candidate in self._candidates if not is_verified_resolver(candidate)],
                                  page_title=title, source_page=page)
            for group in generic:
                group.id = 'generic-' + group.id
                group.selected = False
            self._groups.extend(generic)
        self._quality_options = {}
        preference = getattr(self, '_quality_preference', 'auto')
        preference_label = dict(QUALITY_PREFERENCES).get(preference, '自动推荐')
        for group in self._groups:
            choices = [candidate for candidate in quality_candidates(group) if available_candidate(candidate)]
            self._quality_options[group.id] = choices
            recommended = recommend_candidate(choices, preference)
            for option in choices:
                option.metadata['quality_recommended'] = bool(recommended and option.id == recommended.id)
                option.metadata['quality_recommendation'] = (
                    f'推荐 · {preference_label}' + ('（需确认）' if option.confidence < 70 else '')
                    if option.metadata['quality_recommended'] else '其他可用版本')
            # 推荐只决定默认选择；没有推荐证据的完整媒体仍可由用户手动选择。
            group.selected_candidate = recommended or next(iter(choices), None)
            if recommended is None or recommended.confidence < 70:
                group.selected = False
        self._migrate_group_state(previous_groups)
        self._groups.sort(key=lambda group: (not group.is_ad, group.selected_candidate is not None,
                                            group.confidence), reverse=True)
        selection_groups = [group for group in self._groups if not verified or not group.id.startswith('generic-')]
        current_groups = [group for group in selection_groups if any(
            candidate.metadata.get('confidence_evidence', {}).get('current_player') for candidate in group.candidates)]
        observed_groups = [group for group in selection_groups if any(
            candidate.metadata.get('confidence_evidence', {}).get('associated') for candidate in group.candidates)]
        if current_groups or len(observed_groups) > 1:
            unique_current = bool(current_groups and (len(current_groups) == 1 or
                                  current_groups[0].confidence > current_groups[1].confidence))
            for group in self._groups:
                group.selected = bool(unique_current and group.id == current_groups[0].id and group.selected)
        for group in self._groups:
            if group.id in self._group_checks:
                group.selected = bool(self._group_checks[group.id] and group.selected_candidate)
            choice = self._group_choices.get(group.id)
            candidate = next((c for c in self._choices_for_group(group) if c.id == choice), None)
            if candidate:
                group.selected_candidate = candidate
                group.confidence = candidate.confidence
        groups = self._groups[:]
        visible = [g.selected_candidate or g.candidates[0] for g in groups]
        # 质量选择器之外保留诊断行；低分版本额外放入折叠区，仍可手动选择。
        represented = {candidate.id for candidate in visible}
        represented.update(candidate.id for group in groups for candidate in self._choices_for_group(group)
                           if candidate.confidence >= 40)
        for candidate in self._candidates:
            if candidate.id not in represented:
                owner = next((group for group in self._groups
                    if any(choice.id == candidate.id for choice in self._choices_for_group(group))), None)
                groups.append(MediaGroup(owner.id if owner else 'display-' + candidate.id,
                    display_text(candidate.metadata.get('title') or candidate.metadata.get('page_title'), '未检测到标题'),
                    [candidate], candidate if owner and available_candidate(candidate) else None,
                    duration=candidate.duration, confidence=candidate.confidence,
                    selected=False, is_ad=candidate.is_ad))
                visible.append(candidate)
                represented.add(candidate.id)
        selectable = [(group, candidate) for group, candidate in zip(groups, visible)
                      if available_candidate(candidate) and candidate.confidence >= 70
                      and group.selected_candidate is not None
                      and (not verified or is_verified_resolver(candidate))]
        if not self._user_selected or not any(c.id == self._selected_id and
                (c.confidence >= 40 or self.include_low.isChecked()) for c in visible):
            unique_best = bool(selectable and (len(selectable) == 1 or selectable[0][0].confidence > selectable[1][0].confidence))
            self._selected_id = selectable[0][1].id if unique_best else None
        rows = sorted(zip(groups, visible), key=lambda item: (
            0 if item[1].confidence >= 80 else 1 if item[1].confidence >= 40 else 2,
            -item[1].confidence))
        groups, visible = [row[0] for row in rows], [row[1] for row in rows]
        self.table.blockSignals(True)
        self.table.setRowCount(len(visible))
        self.table.clearSelection()
        for row, (group, candidate) in enumerate(zip(groups, visible)):
            drm = {True: '是', False: '否', None: '未知'}.get(candidate.drm, '未知')
            label = f'{encryption_text(candidate.encryption)} / DRM：{drm}'
            if candidate.drm is True:
                label = '受保护媒体 / DRM：是'
            duration = group.duration or candidate.duration
            duration_label = duration_text(duration)
            title = Redactor(candidate.request_context.headers, [candidate.url]).redact(display_text(group.title, '未检测到标题'))
            if group.is_preview:
                title += '（预览媒体）'
            columns = ['', title, display_text(candidate.resolution_label), duration_label,
                       display_text(candidate.media_type), label, f'{candidate.confidence}/100 · {confidence_status(candidate.confidence)}',
                       display_text(candidate.source_type if is_verified_resolver(candidate) else ' / '.join(sorted(candidate.sources)) or candidate.source_type),
                       Redactor(candidate.request_context.headers, [candidate.url]).redact(candidate.final_url)]
            for column, value in enumerate(columns):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, candidate.id)
                item.setData(Qt.ItemDataRole.UserRole + 1, group.id)
                if column == 0:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    if group.selected_candidate is None or group.is_ad:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                    item.setCheckState(Qt.CheckState.Checked if group.selected else Qt.CheckState.Unchecked)
                self.table.setItem(row, column, item)
            self.table.setRowHidden(row, candidate.confidence < 40 and not self.include_low.isChecked())
            choices = self._choices_for_group(group)
            self.table.removeCellWidget(row, 2)
            if len(choices) > 1:
                combo = QComboBox()
                for source in choices:
                    combo.addItem(self._quality_label(source), source.id)
                combo.setCurrentIndex(max(0, combo.findData(candidate.id)))
                combo.currentIndexChanged.connect(lambda _, g=group.id, widget=combo: self._choose_quality(g, widget.currentData()))
                self.table.setCellWidget(row, 2, combo)
            if candidate.id == self._selected_id:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        if hasattr(self, '_candidate_cards'):
            self._render_candidate_cards(groups, visible)
        self.player_label.setText('播放器类型：' + ('Blob / MSE' if self._player_type == 'BLOB_MSE' else display_text(self._player_type)))
        self._selection_changed()
        self._update_batch_button()
        self._update_discovery_stats()

    def _render_candidate_cards(self, groups, candidates):
        visible_ids = {candidate.id for candidate in candidates}
        for candidate_id in list(self._candidate_cards):
            if candidate_id not in visible_ids:
                card = self._candidate_cards.pop(candidate_id)
                self.candidate_card_layout.removeWidget(card)
                card.deleteLater()
        self.candidate_empty_state.setVisible(not candidates)
        if not hasattr(self, '_confidence_headers'):
            self._confidence_headers = [QLabel(), QLabel()]
            for header in self._confidence_headers:
                header.setObjectName('cardTitle')
                header.setTextFormat(Qt.TextFormat.PlainText)
        counts = [sum(candidate.confidence >= 80 for candidate in candidates),
                  sum(40 <= candidate.confidence < 80 for candidate in candidates),
                  sum(candidate.confidence < 40 for candidate in candidates)]
        self._confidence_headers[0].setText(f'推荐（{counts[0]}）')
        self._confidence_headers[1].setText(f'可确认（{counts[1]}）')
        self.include_low.setText(f'低可信度（{counts[2]}）· ' + ('收起' if self.include_low.isChecked() else '展开查看'))
        self.include_low.setVisible(bool(counts[2]))
        for tier, header in enumerate(self._confidence_headers):
            header.setVisible(bool(counts[tier]))
        for row, (group, candidate) in enumerate(zip(groups, candidates)):
            if candidate.id not in self._candidate_cards:
                card = CandidateCard()
                card.selected.connect(lambda current=candidate.id: self._select_card(current))
                card.checked.connect(lambda checked, current=candidate.id: self._check_card(current, checked))
                card.quality_changed.connect(lambda current, widget=card: self._choose_quality(widget.property('group_id'), current))
                card.download_requested.connect(lambda current=candidate.id: self._download_card(current))
                self._candidate_cards[candidate.id] = card
            card = self._candidate_cards[candidate.id]
            card.setProperty('group_id', group.id)
            columns = [self.table.item(row, column).text() for column in range(9)]
            choices = [(self._quality_label(source), source.id) for source in self._choices_for_group(group)]
            redactor = Redactor(candidate.request_context.headers, [candidate.url, candidate.final_url])
            reasons = [redactor.redact(reason) for reason in confidence_reasons(candidate)]
            card.update_display(columns, reasons or candidate.confidenceReasons, choices, candidate.id,
                                group.selected, group.selected_candidate is not None and not group.is_ad,
                                available_candidate(candidate),
                                quality_summary=f'{display_text(candidate.videoCodec, "编码未知")} · 质量 {candidate.qualityScore}/100 · '
                                                + display_text(candidate.metadata.get('quality_recommendation'), '未推荐'),
                                quality_reasons=[redactor.redact(item.get('reason', '')) for item in candidate.metadata.get('quality_details', [])],
                                lifecycle_status=candidate_state_text(candidate),
                                unavailable_reasons=[redactor.redact(reason) for reason in candidate.unavailable_reasons])
            card.set_selected(candidate.id == self._selected_id)
            card.setVisible(candidate.confidence >= 40 or self.include_low.isChecked())
        # 新建卡片也按分组顺序插入；收起只改变可见性，不删除候选和卡片。
        position = 0
        for tier, header in enumerate([*self._confidence_headers, self.include_low]):
            self.candidate_card_layout.insertWidget(position, header)
            position += 1
            for candidate in candidates:
                if (0 if candidate.confidence >= 80 else 1 if candidate.confidence >= 40 else 2) == tier:
                    self.candidate_card_layout.insertWidget(position, self._candidate_cards[candidate.id])
                    position += 1

    def _select_card(self, candidate_id):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == candidate_id:
                self.table.selectRow(row)
                self._user_selection_changed()
                return

    def _check_card(self, candidate_id, checked):
        if checked:
            owner = next((group for group in self._groups
                if any(choice.id == candidate_id for choice in self._choices_for_group(group))), None)
            if owner and (not owner.selected_candidate or owner.selected_candidate.id != candidate_id):
                self._choose_quality(owner.id, candidate_id)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.data(Qt.ItemDataRole.UserRole) == candidate_id:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                return

    def _download_card(self, candidate_id):
        self._select_card(candidate_id)
        self.confirm()
        if self.result() == QDialog.DialogCode.Accepted:
            self.download_now.emit()

    def _group_checked(self, item):
        if item.column() == 0:
            self._group_checks[item.data(Qt.ItemDataRole.UserRole + 1)] = item.checkState() == Qt.CheckState.Checked
            self._update_batch_button()

    def _choose_quality(self, group_id, candidate_id):
        if not any(group.id == group_id and any(candidate.id == candidate_id for candidate in self._choices_for_group(group))
                   for group in self._groups):
            return
        self._group_choices[group_id] = candidate_id
        self._selected_id, self._user_selected = candidate_id, True
        self._render_candidates()

    def _choices_for_group(self, group):
        return getattr(self, '_quality_options', {}).get(group.id, ordered_candidates(group))

    def _migrate_group_state(self, previous_groups):
        # 浏览器后补 video_id 会改变分组 ID；候选身份不变时保留用户操作。
        # 仅迁移一对一关系，分组拆分或合并时重新确认，避免勾选其他视频。
        matches = {}
        for group in self._groups:
            ids = {candidate.id for candidate in group.candidates}
            matches[group.id] = [previous for previous in previous_groups
                if group.id.startswith('generic-') == previous.id.startswith('generic-')
                and (group.id == previous.id or ids.intersection(candidate.id for candidate in previous.candidates))]
        old_checks, old_choices = self._group_checks, self._group_choices
        self._group_checks, self._group_choices = {}, {}
        for group in self._groups:
            previous = matches[group.id]
            one_to_one = len(previous) == 1 and sum(
                any(item.id == previous[0].id for item in values) for values in matches.values()) == 1
            if not one_to_one:
                if any(item.id in old_checks or item.id in old_choices for item in previous):
                    self._group_checks[group.id] = False
                continue
            previous_id = previous[0].id
            if previous_id in old_checks:
                self._group_checks[group.id] = old_checks[previous_id]
            choice = old_choices.get(previous_id)
            if any(candidate.id == choice for candidate in self._choices_for_group(group)):
                self._group_choices[group.id] = choice

    @staticmethod
    def _quality_label(candidate):
        return ('★ 推荐 · ' if candidate.metadata.get('quality_recommended') else '') + (
            f'{display_text(candidate.resolution_label)} · {display_text(candidate.videoCodec or candidate.media_type)}'
            f' · 可信度{candidate.confidence}（{confidence_status(candidate.confidence)}） · 质量{candidate.qualityScore}')

    def _quality_preference_changed(self, *_):
        self._quality_preference = self.quality_preference_combo.currentData()
        self._group_choices.clear()
        self._selected_id, self._user_selected = None, False
        parent = self.parent()
        if parent is not None and hasattr(parent, 'download_quality'):
            parent.download_quality.setCurrentIndex(parent.download_quality.findData(self._quality_preference))
        self._render_candidates()

    def _check_groups(self, checked):
        for group in self._groups:
            self._group_checks[group.id] = bool(checked and group.selected_candidate and not group.is_ad)
        self._render_candidates()

    def _update_batch_button(self):
        count = sum(bool(self._group_checks.get(g.id, g.selected) and g.selected_candidate and not g.is_ad) for g in self._groups)
        self.download_all_button.setEnabled(bool(count and not self._batch_pending and not self._batch_enqueued))
        self.download_all_button.setToolTip(f'下载勾选的 {count} 个独立视频；默认每个视频仅一份。')

    def download_all(self):
        if self._closing or self._batch_pending or self._batch_enqueued:
            return
        self._batch_pending = True
        self._listening = False
        self.listen_timer.stop()
        self.resolver_timer.stop()
        self._resolver_epoch += 1
        self._fetch_queue.clear()
        self.observer.stop()
        self.find_button.setEnabled(False)
        self.deep_find_button.setEnabled(False)
        self.status_label.setText('正在完成已收集候选的检测、去重和分组…')
        self._update_batch_button()
        self.worker.freeze()

    def _batch_ready(self, candidates, generation):
        if self._closing or generation != self.worker.generation or not self._batch_pending:
            return
        self._candidates = [candidate for candidate in candidates if self._candidate_allowed(candidate)]
        self._render_candidates()
        for group in self._groups:
            group.selected = bool(self._group_checks.get(group.id, group.selected) and group.selected_candidate and not group.is_ad)
        selected = [group.selected_candidate for group in self._groups if group.selected]
        if self.all_qualities.isChecked():
            selected = [candidate for group in self._groups if group.selected for candidate in ordered_candidates(group)]
        if not self._confirm_medium(selected):
            self.batch_result(False, '已取消：请先确认要下载的正片。')
            return
        self.batch_requested.emit(deepcopy(self._groups), {'all_qualities': self.all_qualities.isChecked(),
                                                         'auto_fallback': self.auto_fallback.isChecked()})

    def batch_result(self, accepted, message):
        self._batch_pending, self._batch_enqueued = False, bool(accepted)
        self.find_button.setEnabled(True)
        self.deep_find_button.setEnabled(True)
        self.status_label.setText(message)
        self._update_batch_button()

    def selected_candidate(self):
        for choices in getattr(self, '_quality_options', {}).values():
            candidate = next((item for item in choices if item.id == self._selected_id), None)
            if candidate is not None:
                return candidate
        return next((candidate for candidate in self._candidates if candidate.id == self._selected_id), None)

    def _user_selection_changed(self, *_):
        self._user_selected = True
        self._selection_changed()

    def _selection_changed(self, user=False):
        if user:
            self._user_selected = True
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self._selected_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        candidate = self.selected_candidate()
        if hasattr(self, 'track_selector'):
            self.track_selector.set_candidate(candidate)
        for candidate_id, card in getattr(self, '_candidate_cards', {}).items():
            card.set_selected(candidate_id == self._selected_id)
        self.confirm_button.setEnabled(bool(candidate and available_candidate(candidate)))
        self.confirm_button.setToolTip('；'.join(candidate.safe_diagnostic()['unavailable_reasons']) if candidate and not available_candidate(candidate)
                                      else '证据不足的候选需手动确认；低可信度不会单独禁止下载。')
        if not candidate:
            self.details.clear()
            if hasattr(self, 'media_details'):
                self.media_details.clear()
            self.summary_label.setText('媒体类型：未知  ·  清晰度：未知  ·  加密：未知  ·  DRM：未知')
            self.trace.set_values(self._resolver_trace_values())
            return
        drm = {True: '是', False: '否', None: '未知'}.get(candidate.drm, '未知')
        if is_verified_resolver(candidate):
            values = candidate.safe_diagnostic()
            duration = candidate.duration
            duration_label = duration_text(duration)
            self.summary_label.setText(f'视频标题：{display_text(values.get("page_title") or values.get("title"))}'
                f'  ·  {display_text(candidate.media_type)}  ·  {display_text(manifest_label(candidate.manifest_type))}  ·  {display_text(candidate.playlist_type)}'
                f'  ·  加密：{encryption_text(candidate.encryption)}  ·  DRM：{drm}  ·  Segment：{display_text(values.get("segment_count"))}'
                f'  ·  时长：{duration_label}  ·  来源：{values["source_type"]}  ·  已验证\n'
                f'解析：{format_resolver_chain(values)}')
        else:
            self.summary_label.setText(f'媒体类型：{display_text(candidate.media_type)}  ·  清晰度：{display_text(candidate.resolution_label)}'
                                       f'  ·  加密：{encryption_text(candidate.encryption)}  ·  DRM：{drm}')
        self.details.setPlainText(candidate_details(candidate))
        if hasattr(self, 'media_details'):
            self.media_details.show_candidate(candidate)
        trace_values = candidate.safe_diagnostic()
        if not trace_values.get('resolver_trace'):
            trace_values = self._resolver_trace_values() or trace_values
        self.trace.set_values(self._resolver_trace_values(trace_values))

    def _listen_ended(self):
        self._listening = False
        self.observer.stop()
        self.resolver_timer.stop()
        count = len(self._candidates)
        low = sum(candidate.confidence < 40 for candidate in self._candidates)
        self.status_label.setText(f'找到 {count} 个候选媒体源（低可信度 {low} 个，可展开查看）；可点击“查找”继续监听。' if count
                                  else self._empty_diagnostic())
        self._update_discovery_stats()

    def _empty_diagnostic(self):
        if self._generic_diagnostic.get('error_code'):
            return self._generic_diagnostic['error_code'] + '：' + self._generic_diagnostic['message']
        if self._candidates and all(candidate.is_ad for candidate in self._candidates):
            return 'ONLY_AD_MEDIA_FOUND：仅发现广告媒体，请播放正片后继续查找。'
        if self._candidates and all(candidate.confidence < 40 for candidate in self._candidates):
            return '已发现低可信度媒体，候选保留在折叠区；可展开查看诊断，播放正片有助于补齐证据。'
        if self._resolver_diagnostics:
            code = self._resolver_diagnostics[-1].get('error_code') or self._resolver_diagnostics[-1].get('code', '')
            if code in ('NO_SITE_RESOLVER', 'UNSUPPORTED_SITE'):
                code = ''
            if code:
                return f'{code}：站点解析未找到有效媒体；可以浏览到播放页或使用深度查找。'
        return ('MANIFEST_MISSED：已发现 Blob/MSE 播放器，但尚未捕获清单，请使用深度查找。'
                if self._player_type == 'BLOB_MSE' else 'PLAYER_NOT_STARTED：尚未发现可下载媒体，请先播放视频，再快速查找或深度查找。')

    def confirm(self):
        candidate = self.selected_candidate()
        if not candidate or not available_candidate(candidate):
            reason = '；'.join(candidate.unavailable_reasons) if candidate else '请先选择媒体候选'
            self.status_label.setText(Redactor(candidate.request_context.headers, [candidate.url]).redact(reason) if candidate else reason)
            return
        if not self._confirm_medium([candidate]):
            return
        try:
            from core.media_tracks import apply_selection
            candidate = apply_selection(candidate, self.track_selector.selection())
        except ValueError as error:
            self.status_label.setText(str(error))
            self.detail_tabs.setCurrentWidget(self.track_selector)
            return
        self.media_selected.emit(deepcopy(candidate))
        self.setResult(QDialog.DialogCode.Accepted)
        self.close()

    def _confirm_medium(self, candidates):
        medium = [candidate for candidate in candidates if candidate.requires_confirmation]
        if not medium:
            return True
        message = '\n'.join(Redactor(candidate.request_context.headers, [candidate.url]).redact(
            f'可信度 {candidate.confidence}/100：' + '、'.join(confidence_reasons(candidate)[:4])) for candidate in medium[:5])
        answer = QMessageBox.question(self, '确认当前正片',
            message + '\n\n这些媒体尚缺少充分的正片关联证据。确认是你要下载的视频后继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def reject(self):
        self.setResult(QDialog.DialogCode.Rejected)
        self.close()

    def closeEvent(self, event):
        self.shutdown()
        event.accept()

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        if environment := getattr(self, 'browser_environment', None):
            environment.shutdown()
        self._listening = False
        self.listen_timer.stop()
        self.resolver_timer.stop()
        self._resolver_epoch += 1
        self._fetch_queue.clear()
        self._network_history.clear()
        self.observer.stop()
        self.worker.cancel()
        self.view.stop()
        self.view.hide()
        self.view.deleteLater()
        self._profile_gone = not self._owns_profile
        if self._owns_profile:
            self.profile.destroyed.connect(self._profile_destroyed)
        self.page.deleteLater()
        self.hide()
        self._finish_shutdown()

    def _worker_finished(self):
        self._worker_done = True
        self.worker.deleteLater()
        self._finish_shutdown()

    def _page_destroyed(self, *_):
        self._pages_alive = max(0, self._pages_alive - 1)
        self._page_gone = self._pages_alive == 0
        if self._closing and self._page_gone and self._owns_profile:
            self.profile.deleteLater()
        self._finish_shutdown()

    def _profile_destroyed(self, *_):
        self._profile_gone = True
        self._finish_shutdown()

    def _finish_shutdown(self):
        environment = getattr(self, 'browser_environment', None)
        if environment is not None and environment.busy:
            return
        if (self._closing and self._worker_done and self._page_gone and self._profile_gone
                and not self._shutdown_emitted):
            self._shutdown_emitted = True
            self._candidates.clear()
            self.shutdown_complete.emit()
