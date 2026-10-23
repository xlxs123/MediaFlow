from __future__ import annotations

import os
import re
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                              QFormLayout, QLabel, QLineEdit, QPushButton, QGroupBox, QToolButton,
                              QFileDialog, QPlainTextEdit, QProgressBar, QMessageBox, QInputDialog,
                              QDialog, QScrollArea, QFrame, QCheckBox, QSpinBox, QTabWidget,
                              QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QComboBox, QLayout)

from core.curl_parser import parse_curl, parse_input, CurlParseError, has_temporary_signature
from core.downloader import DownloadWorker, DownloadRequest, build_arguments, friendly_error
from core.download_queue import DownloadQueue
from core.task_store import TaskStore
from core.download_manager import DownloadManager
from core.yt_dlp_manager import detect_yt_dlp, detect_ffmpeg, detect_ffprobe, ToolInfo
from core.request_context import RequestContext
from core.media_quality import QUALITY_PREFERENCES
from ui.dialogs import CurlDialog, HeadersDialog, HeadersEditor, ErrorDialog, DetectionDialog
from ui.media_cards import CARD_STYLES, TaskCard
from ui.library_service import LibraryService
from ui.media_library import MediaLibrary
from ui.storage_service import StorageService
from ui.storage_panel import StoragePanel
from core.storage_safety import queue_snapshot
from ui.batch_import import BatchImportDialog
from ui.batch_panel import BatchPanel
from core.batch_controller import BatchController
from core.task_store import utc_now
from ui.thumbnail_service import ThumbnailService
from utils.branding import APP_NAME, APP_VERSION, SUBTITLE, ENGLISH_SUBTITLE, LOG_NAME
from utils.build_info import runtime_summary, runtime_identity
from utils.config import ConfigStore
from utils.security import normalize_headers, is_sensitive_header, Redactor, redact_data

COMMON_FIELDS = ('origin', 'referer', 'user-agent', 'accept', 'accept-language')

STYLES = '''
QMainWindow, QDialog { background: #f3f6fa; }
QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; color: #243347; }
QLineEdit, QPlainTextEdit, QTableWidget { background: white; border: 1px solid #d7dee7; border-radius: 5px; padding: 6px; selection-background-color: #2878d8; }
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #2878d8; }
QPushButton { background: white; border: 1px solid #dce4ed; border-radius: 8px; padding: 8px 14px; }
QPushButton:hover { background: #eaf2fd; border-color: #8bb5e8; }
QPushButton:disabled { color: #9aa6b5; background: #edf0f4; }
QPushButton#primary { background: #11867e; color: white; border-color: #11867e; font-weight: 600; }
QPushButton#primary:hover { background: #0c716b; }
QPushButton#primary:disabled { background: #a6bddf; border-color: #a6bddf; }
QGroupBox { background: white; border: 1px solid #e0e7ef; border-radius: 12px; margin-top: 12px; padding: 16px; }
QGroupBox::title { subcontrol-origin: margin; left: 15px; padding: 0 6px; }
QLabel#title { font-size: 29px; font-weight: 700; color: #173449; }
QLabel#muted { color: #718097; font-size: 12px; }
QLabel#warning { color: #9b660a; background: #fff6de; border: 1px solid #ead9ad; border-radius: 5px; padding: 6px; }
QLabel#result { background: #ebf5ef; border: 1px solid #cee5d6; border-radius: 5px; padding: 8px; }
QToolButton { border: none; background: transparent; font-weight: 600; padding: 5px; }
QProgressBar { border: none; border-radius: 6px; background: #e9eff5; text-align: center; min-height: 18px; }
QProgressBar::chunk { background: #17a196; border-radius: 6px; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { background: #e8eef5; color: #64758b; border: none; border-radius: 7px; padding: 9px 20px; margin: 0 6px 8px 0; }
QTabBar::tab:selected { background: #173449; color: white; }
QScrollBar:vertical { background: transparent; width: 9px; }
QScrollBar::handle:vertical { background: #cad5e1; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
''' + CARD_STYLES


class ToolDetectionWorker(QThread):
    detected = Signal(object, object)
    tool_detected = Signal(str, object)

    def __init__(self, preferred='', parent=None):
        super().__init__(parent)
        self.settings = dict(preferred) if isinstance(preferred, dict) else {'yt_dlp_path': preferred}
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        functions = {'yt-dlp': detect_yt_dlp, 'ffmpeg': detect_ffmpeg, 'ffprobe': detect_ffprobe}
        results = {}
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='MediaFlow-tools') as pool:
            futures = {}
            for name, function in functions.items():
                key = name.replace('-', '_')
                futures[pool.submit(function, self.settings.get(key + '_path', ''),
                    mode=self.settings.get(key + '_mode', 'auto'), cancel_event=self.cancel_event)] = name
            for future in as_completed(futures):
                name = futures[future]
                try:
                    info = future.result()
                except Exception:
                    info = ToolInfo(error=f'{name} 检测未完成，请重新检测。', status='version_failed')
                results[name] = info
                self.tool_detected.emit(name, info)
        self.detected.emit(results['yt-dlp'], results['ffmpeg'])


class MainWindow(QMainWindow):
    task_completed = Signal(dict)

    def __init__(self, config_store=None):
        super().__init__()
        self.config_store = config_store or ConfigStore()
        self.settings = self.config_store.load()
        self.setWindowTitle(APP_NAME)
        self.resize(self.settings['window_width'], self.settings['window_height'])
        self.setMinimumSize(780, 630)
        self.setStyleSheet(STYLES)
        self.yt_info = ToolInfo()
        self.ff_info = ToolInfo()
        self.probe_info = ToolInfo()
        self._tool_last_errors = {}
        self.detector = None
        self.worker = None
        self.last_result = None
        self.last_error = ''
        self._close_after_download = False
        self._migration_requested = False
        self._update_dialog = None
        self._close_after_update = False
        self._update_startup_allowed = True
        self._progress_state = {}
        self._redactor = Redactor()
        self._request_method = 'GET'
        self._request_body = None
        self._diagnostic = {}
        self._discovery_context = None
        self._discovery_metadata = {}
        self._discovery_payload = {}
        self._refresh_job_id = None
        self._discovery_dialog = None
        from ui.webview2_browser import browser_profile_path
        self.browser_profile_directory = browser_profile_path(self.config_store)
        self._webview2_profile = None
        self._clearing_browser_data = False
        self._close_after_browser = False
        self._resolver_sessions = {}
        self._resolver_session_registries = {}
        self._refresh_dialogs = set()
        self._close_after_discovery = False
        self.selected_job_id = None
        self._job_rows = {}
        self._task_cards = {}
        self._close_after_thumbnails = False
        self._job_started_at = {}
        self._relocation_workers = set()
        self._close_after_relocation = False
        self.library_service = LibraryService(self.config_store.path.with_name('history.sqlite3'), self)
        self.library_service.closed.connect(self.close)
        self.library_service.message.connect(self.on_library_message)
        self.thumbnails = ThumbnailService(self)
        self.thumbnails.completed.connect(self.on_thumbnail_completed)
        self.thumbnails.idle.connect(self.on_thumbnails_idle)
        self.queue = DownloadQueue(self.settings['max_concurrent_videos'], self,
                                   worker_factory=lambda request, parent: DownloadWorker(request, parent),
                                   max_requests=self.settings['max_concurrent_requests'],
                                   task_store=TaskStore(self.config_store.path.with_name('tasks.json')))
        self.download_manager = DownloadManager(self.queue, self)
        self.storage_service = StorageService(self.config_store.path.with_name('history.sqlite3'),
                                              self._storage_protection, self)
        self.storage_service.closed.connect(self.close)
        self.storage_service.cleanup_guard_changed.connect(self.queue.set_storage_cleanup_guard)
        self.storage_service.changed.connect(self.library_service.changed)
        self.storage_service.message.connect(self.on_library_message)
        self.library_service.protection_provider = self._storage_protection
        self.library_service.cleanup_guard = self.queue.set_storage_cleanup_guard
        self.library_service.mutation_blocked = lambda: bool(self.queue._storage_cleanup_guards)
        self.download_manager.quality_fallback_required.connect(self.offer_quality_fallback)
        self.download_manager.refresh_required.connect(self.auto_refresh_media)
        self._auto_refresh_ids = []
        self._auto_refresh_job_id = None
        self._auto_refresh_dialog = None
        self._auto_refresh_generation = None
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setSingleShot(True)
        self._auto_refresh_timer.setInterval(25000)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_done)
        self._build_ui()
        self.queue.changed.connect(self.on_job_changed)
        self.queue.log.connect(self.on_job_log)
        self.queue.completed.connect(self.on_job_completed)
        self.queue.idle.connect(self.on_queue_idle)
        self.queue.persistence_changed.connect(self.on_persistence_changed)
        restored = self.queue.restore_tasks()
        for job in restored:
            self.library_service.record_job(job, importing=True)
        if restored:
            self.status_label.setText(f'已恢复 {len(restored)} 个任务；未完成任务已暂停，选择任务后点击“继续”。')
        from ui.batch_discovery import SerialBatchDiscovery
        self.batch_driver = SerialBatchDiscovery(self, can_acquire=self._batch_browser_available)
        self.batch_controller = BatchController(self.queue, self.batch_driver,
            self.config_store.path.with_name('batches.sqlite3'), self._batch_template, self)
        self.batch_controller.changed.connect(self._update_batch_panel)
        self.batch_controller.message.connect(self._batch_message)
        self.batch_controller.history_event.connect(self.library_service.record_snapshot)
        self.batch_controller.closed.connect(self.close)
        self.batch_panel.import_requested.connect(self.show_batch_dialog)
        self.batch_panel.item_action.connect(self.on_batch_item_action)
        self.batch_panel.batch_action.connect(self.on_batch_action)
        self.library_service.paths_moved.connect(self.on_library_paths_moved)
        QTimer.singleShot(0, self.library_service.replay_organization_paths)
        QTimer.singleShot(0, self.detect_tools)
        QTimer.singleShot(0, self._startup_update_check)
        if self.config_store.error:
            self.append_log(self.config_store.error)

    def on_persistence_changed(self, message):
        if message:
            self.append_log(message)
            self.status_label.setText(message)

    def _storage_protection(self):
        snapshot = queue_snapshot(self.queue)
        if self.library_service._organization_active or self._relocation_workers or self.library_service._validation_pending:
            snapshot['error'] = '媒体文件正在整理或同步恢复路径，请等待完成后再清理。'
        validator = self.library_service.validator
        if validator:
            snapshot['jobs'].append({'state': 'verifying', 'directories': [],
                                     'paths': [validator.entry['path']]})
        return snapshot

    def _build_ui(self):
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)
        title_row = QHBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName('title')
        title_row.addWidget(title)
        title_row.addStretch()
        note = QLabel(f'v{APP_VERSION} · HLS · DASH · Direct')
        note.setToolTip(runtime_summary() + '\n运行文件：' + runtime_identity()['executable'])
        note.setObjectName('muted')
        title_row.addWidget(note)
        self.migration_button = QPushButton('数据迁移')
        self.migration_button.setToolTip('先安全关闭当前数据服务，再打开旧版数据迁移、报告与回滚助手。')
        self.migration_button.clicked.connect(self.request_data_migration)
        title_row.addWidget(self.migration_button)
        self.update_button = QPushButton('更新与版本管理')
        self.update_button.setToolTip('检查更新、导入本地发布 ZIP，并安全部署到独立版本目录。')
        self.update_button.clicked.connect(self.show_update_dialog)
        title_row.addWidget(self.update_button)
        self.about_button = QPushButton('关于')
        self.about_button.clicked.connect(self.show_about)
        title_row.addWidget(self.about_button)
        layout.addLayout(title_row)
        self.subtitle = QLabel(SUBTITLE)
        self.subtitle.setObjectName('muted')
        layout.addWidget(self.subtitle)
        self.english_subtitle = QLabel(ENGLISH_SUBTITLE)
        self.english_subtitle.setObjectName('muted')
        layout.addWidget(self.english_subtitle)

        self.settings_toggle = QToolButton()
        self.settings_toggle.setText('工具与下载设置')
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.settings_toggle.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(self.settings_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.settings_card = QGroupBox('工具与并发')
        settings_layout = QVBoxLayout(self.settings_card)
        settings_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setWidget(self.settings_card)
        self.settings_scroll.setFixedHeight(280)
        self.settings_scroll.hide()
        self.settings_toggle.toggled.connect(self.settings_scroll.setVisible)
        self.settings_toggle.toggled.connect(lambda shown: self.settings_toggle.setArrowType(
            Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow))
        layout.addWidget(self.settings_scroll)

        tool_row = QHBoxLayout()
        self.tool_status = QLabel('工具尚未检测')
        self.tool_status.setWordWrap(True)
        self.tool_choice = QComboBox()
        for title, name in (('yt-dlp', 'yt-dlp'), ('FFmpeg', 'ffmpeg'), ('ffprobe', 'ffprobe')):
            self.tool_choice.addItem(title, name)
        self.select_tool_button = QPushButton('选择程序…')
        self.select_tool_button.clicked.connect(lambda: self.select_tool(self.tool_choice.currentData()))
        self.detect_button = QPushButton('自动重新检测')
        self.detect_button.clicked.connect(self.detect_tools)
        self.reset_tool_button = QPushButton('恢复自动查找')
        self.reset_tool_button.clicked.connect(self.reset_tool_path)
        self.open_tool_button = QPushButton('打开所在目录')
        self.open_tool_button.clicked.connect(self.open_tool_directory)
        tool_row.addWidget(self.tool_choice)
        tool_row.addWidget(self.select_tool_button)
        tool_row.addWidget(self.reset_tool_button)
        tool_row.addWidget(self.open_tool_button)
        tool_row.addStretch()
        tool_row.addWidget(self.detect_button)
        settings_layout.addWidget(self.tool_status)
        self.tool_table = QTableWidget(3, 5)
        self.tool_table.setHorizontalHeaderLabels(['工具', '当前路径', '检测状态', '版本', '最近错误 / 恢复说明'])
        self.tool_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tool_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tool_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tool_table.verticalHeader().hide()
        self.tool_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tool_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tool_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tool_table.setFixedHeight(128)
        self.tool_table.cellClicked.connect(lambda row, _: self.tool_choice.setCurrentIndex(row))
        settings_layout.addWidget(self.tool_table)
        settings_layout.addLayout(tool_row)

        concurrency_row = QHBoxLayout()
        concurrency_row.addWidget(QLabel('同时下载视频'))
        self.max_concurrent_videos = QSpinBox()
        self.max_concurrent_videos.setRange(1, 10)
        self.max_concurrent_videos.setValue(self.settings['max_concurrent_videos'])
        self.max_concurrent_videos.setSuffix(' 个')
        self.max_concurrent_videos.setToolTip('最多同时运行的任务数。降低时，当前任务继续，下一个空位遵守新上限。')
        concurrency_row.addWidget(self.max_concurrent_videos)
        concurrency_row.addSpacing(18)
        concurrency_row.addWidget(QLabel('每个视频分片并发'))
        self.concurrent_fragments = QSpinBox()
        self.concurrent_fragments.setRange(1, 16)
        self.concurrent_fragments.setValue(self.settings['concurrent_fragments'])
        self.concurrent_fragments.setSuffix(' 个')
        self.concurrent_fragments.setToolTip('用于新加入的 HLS / DASH 任务。实际速度取决于带宽和服务器限速。')
        concurrency_row.addWidget(self.concurrent_fragments)
        concurrency_row.addWidget(QLabel('分片总尝试'))
        self.segment_max_attempts = QSpinBox()
        self.segment_max_attempts.setRange(1, 10)
        self.segment_max_attempts.setValue(self.settings['segment_max_attempts'])
        self.segment_max_attempts.setSuffix(' 次')
        self.segment_max_attempts.setToolTip('包含首次请求；默认3次，失败后等待1秒、3秒再重试。')
        concurrency_row.addWidget(self.segment_max_attempts)
        concurrency_row.addWidget(QLabel('全局请求上限'))
        self.max_concurrent_requests = QSpinBox()
        self.max_concurrent_requests.setRange(1, 64)
        self.max_concurrent_requests.setValue(self.settings['max_concurrent_requests'])
        self.max_concurrent_requests.setToolTip('全部视频的远端 HTTP 请求合计上限，包括清单、Key 和分片；默认12。')
        concurrency_row.addWidget(self.max_concurrent_requests)
        concurrency_row.addStretch()
        settings_layout.addLayout(concurrency_row)

        browser_row = QHBoxLayout()
        browser_row.addWidget(QLabel('Browser Profile'))
        self.browser_profile_location = QLineEdit(str(self.browser_profile_directory))
        self.browser_profile_location.setReadOnly(True)
        self.browser_profile_location.setToolTip('WebView2 的本地 Cookie、网站存储和缓存目录；关闭查找窗口后保留，不上传或同步。')
        browser_row.addWidget(self.browser_profile_location, 1)
        self.clear_browser_button = QPushButton('清除浏览器数据')
        self.clear_browser_button.clicked.connect(self.clear_browser_data)
        browser_row.addWidget(self.clear_browser_button)
        settings_layout.addLayout(browser_row)

        capture_row = QHBoxLayout()
        capture_row.addWidget(QLabel('浏览器单条响应采集上限'))
        self.browser_body_limit = QSpinBox()
        self.browser_body_limit.setRange(1, 128)
        self.browser_body_limit.setSuffix(' MiB')
        self.browser_body_limit.setValue(self.settings.get('browser_body_limit_mib', 16))
        self.browser_body_limit.setToolTip('用于新打开的自动查找窗口；默认16 MiB。较大媒体描述可适当调高，会增加内存占用，采集总内存仍有上限。')
        capture_row.addWidget(self.browser_body_limit)
        capture_row.addStretch()
        settings_layout.addLayout(capture_row)

        scroll = QScrollArea()
        self.form_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_widget = QWidget()
        form_layout = QVBoxLayout(form_widget)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        scroll.setWidget(form_widget)
        self.input_tabs = QTabWidget()
        self.input_tabs.addTab(scroll, '媒体输入')
        queue_page = QWidget()
        queue_layout = QVBoxLayout(queue_page)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(12)
        queue_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.queue_table = QTableWidget(0, 6, queue_page)
        self.queue_table.setHorizontalHeaderLabels(['任务 / 文件名', '状态', '进度', '速度', '分片', '剩余'])
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_table.verticalHeader().hide()
        self.queue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.queue_table.itemSelectionChanged.connect(self.on_job_selection_changed)
        # 隐藏原表格，仅复用其选中行事件；任务状态仍由原队列驱动。
        self.queue_table.hide()
        self.task_scroll = QScrollArea()
        self.task_scroll.setObjectName('cardScroll')
        self.task_scroll.setWidgetResizable(True)
        self.task_scroll.setFrameShape(QFrame.Shape.NoFrame)
        task_content = QWidget()
        self.task_card_layout = QVBoxLayout(task_content)
        self.task_card_layout.setContentsMargins(0, 0, 9, 0)
        self.task_card_layout.setSpacing(10)
        self.task_card_layout.addStretch()
        self.task_scroll.setWidget(task_content)
        self.task_scroll.hide()
        queue_layout.addWidget(self.task_scroll, 1)
        queue_actions = QHBoxLayout()
        self.start_queue_button = QPushButton('启动队列')
        self.start_queue_button.clicked.connect(self.start_queue)
        self.pause_queue_button = QPushButton('暂停队列')
        self.pause_queue_button.setToolTip('暂停派发新任务，当前下载继续。')
        self.pause_queue_button.clicked.connect(self.pause_queue)
        self.retry_button = QPushButton('重试所选')
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.retry_selected)
        self.update_link_button = QPushButton('更新链接并继续')
        self.update_link_button.clicked.connect(self.update_selected_link)
        self.refresh_media_button = QPushButton('刷新媒体地址并继续')
        self.refresh_media_button.clicked.connect(self.refresh_selected_media)
        self.remove_finished_button = QPushButton('移除已结束')
        self.remove_finished_button.clicked.connect(self.remove_finished_jobs)
        for button in (self.start_queue_button, self.pause_queue_button, self.retry_button,
                       self.update_link_button, self.refresh_media_button, self.remove_finished_button):
            queue_actions.addWidget(button)
        queue_actions.addStretch()
        queue_layout.addLayout(queue_actions)
        task_actions = QHBoxLayout()
        self.pause_job_button = QPushButton('暂停所选')
        self.pause_job_button.clicked.connect(self.pause_selected)
        self.resume_job_button = QPushButton('继续所选')
        self.resume_job_button.clicked.connect(self.resume_selected)
        self.cancel_job_button = QPushButton('取消所选')
        self.cancel_job_button.clicked.connect(self.stop_download)
        self.task_directory_button = QPushButton('打开任务目录')
        self.task_directory_button.clicked.connect(self.open_task_directory)
        self.retry_failed_button = QPushButton('重试失败任务')
        self.retry_failed_button.clicked.connect(self.download_manager.retry_failed)
        for button in (self.pause_job_button, self.resume_job_button, self.cancel_job_button,
                       self.task_directory_button, self.retry_failed_button):
            task_actions.addWidget(button)
        task_actions.addStretch()
        queue_layout.addLayout(task_actions)
        self.queue_summary = QLabel('队列已暂停 · 运行 0 · 等待 0 · 已结束 0')
        queue_layout.addWidget(self.queue_summary)
        self.input_tabs.addTab(queue_page, '任务队列 (0)')
        self.media_library = MediaLibrary(self.library_service, tools_provider=lambda: {
            'ffmpeg_path': self.ff_info.path if self.ff_info.found else '',
            'ffprobe_path': self.probe_info.path if self.probe_info.found else ''}, parent=self)
        self.input_tabs.addTab(self.media_library, '媒体库与历史')
        self.batch_panel = BatchPanel(self)
        self.input_tabs.addTab(self.batch_panel, '批次任务')
        self.storage_panel = StoragePanel(self.storage_service, self)
        self.input_tabs.addTab(self.storage_panel, '去重与空间')
        layout.addWidget(self.input_tabs, 3)

        request_box = QGroupBox('媒体和输出')
        request_layout = QVBoxLayout(request_box)
        curl_row = QHBoxLayout()
        self.parse_clipboard_button = QPushButton('从剪贴板解析 URL / cURL')
        self.parse_clipboard_button.setObjectName('primary')
        self.parse_clipboard_button.clicked.connect(self.parse_clipboard)
        self.paste_curl_button = QPushButton('粘贴 URL / cURL')
        self.paste_curl_button.clicked.connect(self.paste_curl)
        self.headers_button = QPushButton('查看全部 Headers')
        self.headers_button.clicked.connect(self.edit_headers)
        self.discovery_button = QPushButton('自动查找')
        self.discovery_button.clicked.connect(self.open_discovery)
        curl_row.addWidget(self.parse_clipboard_button)
        curl_row.addWidget(self.paste_curl_button)
        curl_row.addWidget(self.discovery_button)
        curl_row.addStretch()
        curl_row.addWidget(self.headers_button)
        request_layout.addLayout(curl_row)

        form = QFormLayout()
        form.setSpacing(8)
        self.origin = QLineEdit()
        self.origin.setPlaceholderText('https://example.com（可留空）')
        self.referer = QLineEdit()
        self.referer.setPlaceholderText('https://example.com/video.html（可留空）')
        self.url = QLineEdit()
        self.url.setPlaceholderText('请输入媒体URL，或使用自动查找。')
        self.url.textChanged.connect(self.update_signature_hint)
        self.url.textChanged.connect(self.reset_request_payload)
        self.output_directory = QLineEdit(self.settings['output_directory'])
        self.filename = QLineEdit()
        self.filename.setPlaceholderText('留空自动编号；例如 video01，同名任务自动区分')
        for label, field in (('媒体网址', self.url), ('Origin', self.origin), ('Referer', self.referer)):
            row = QHBoxLayout()
            row.addWidget(field, 1)
            button = QPushButton('从剪贴板读取')
            button.clicked.connect(lambda checked=False, target=field: self.read_clipboard_field(target))
            row.addWidget(button)
            form.addRow(label, row)
        row = QHBoxLayout()
        row.addWidget(self.output_directory, 1)
        self.browse_button = QPushButton('浏览')
        self.browse_button.clicked.connect(self.browse_output)
        row.addWidget(self.browse_button)
        form.addRow('输出目录', row)
        form.addRow('文件名', self.filename)
        from ui.output_options import OutputOptions
        self.output_options = OutputOptions(self.settings.get('organization'), self)
        settings_layout.addWidget(self.output_options)
        self.filename.textChanged.connect(self._update_output_preview)
        self.url.textChanged.connect(self._update_output_preview)
        self.output_directory.textChanged.connect(self._update_output_preview)
        self._update_output_preview()
        self.download_quality = QComboBox()
        for value, label in QUALITY_PREFERENCES:
            self.download_quality.addItem(label, value)
        self.download_quality.setCurrentIndex(max(0, self.download_quality.findData(self.settings['quality_preference'])))
        self.download_quality.setToolTip('用于自动发现的同一视频版本；先确认正片真实性，再比较质量。单个直链按原媒体下载。')
        form.addRow('下载质量', self.download_quality)
        request_layout.addLayout(form)
        self.signature_hint = QLabel('检测到可能具有时效性的媒体链接，建议获取后立即下载。')
        self.signature_hint.setObjectName('warning')
        self.signature_hint.setWordWrap(True)
        self.signature_hint.hide()
        layout.addWidget(self.signature_hint)
        self.parse_summary = QLabel('')
        self.parse_summary.setObjectName('result')
        self.parse_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.parse_summary.setWordWrap(True)
        self.parse_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.parse_summary.hide()
        request_layout.addWidget(self.parse_summary)
        form_layout.addWidget(request_box)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText('高级设置')
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.toggled.connect(self.toggle_advanced)
        form_layout.addWidget(self.advanced_toggle)
        self.advanced_box = QGroupBox('请求头')
        advanced_layout = QVBoxLayout(self.advanced_box)
        advanced_form = QFormLayout()
        self.user_agent = QLineEdit(self.settings['user_agent'])
        self.user_agent.setPlaceholderText('留空使用应用默认 User-Agent')
        self.accept = QLineEdit()
        self.accept.setPlaceholderText('例如 */*（可留空）')
        self.accept_language = QLineEdit()
        self.accept_language.setPlaceholderText('例如 zh-CN,zh;q=0.9（可留空）')
        advanced_form.addRow('User-Agent', self.user_agent)
        advanced_form.addRow('Accept', self.accept)
        advanced_form.addRow('Accept-Language', self.accept_language)
        advanced_layout.addLayout(advanced_form)
        self.inherit_range = QCheckBox('Key / 分片 / 子清单继承原 Range')
        self.inherit_range.setToolTip('导入 cURL 的 bytes=0- 时自动开启；可关闭恢复隔离。分片自身的字节范围优先，条件请求头仍不继承。')
        advanced_layout.addWidget(self.inherit_range)
        self.inherit_browser_metadata = QCheckBox('派生请求也发送浏览器元数据（兼容选项）')
        self.inherit_browser_metadata.setToolTip('默认关闭。只有服务端要求时启用 Sec-Fetch-* / sec-ch-ua*；Range 使用独立的继承开关。')
        advanced_layout.addWidget(self.inherit_browser_metadata)
        self.custom_headers = HeadersEditor()
        advanced_layout.addWidget(self.custom_headers)
        controls = QHBoxLayout()
        add = QPushButton('添加')
        remove = QPushButton('删除')
        add.clicked.connect(lambda: self.custom_headers.add_header())
        remove.clicked.connect(self.custom_headers.delete_selected)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addStretch()
        tip = QLabel('敏感头仅在当前会话使用，不保存到配置。')
        tip.setObjectName('muted')
        controls.addWidget(tip)
        advanced_layout.addLayout(controls)
        form_layout.addWidget(self.advanced_box)
        self.toggle_advanced(self.settings['advanced_expanded'])
        self.advanced_toggle.setChecked(self.settings['advanced_expanded'])
        form_layout.addStretch()

        action_row = QHBoxLayout()
        self.start_button = QPushButton('开始下载')
        self.start_button.setObjectName('primary')
        self.start_button.setMinimumHeight(40)
        self.start_button.clicked.connect(self.start_download)
        self.add_queue_button = QPushButton('加入队列')
        self.add_queue_button.clicked.connect(self.add_current_to_queue)
        self.batch_button = QPushButton('批量添加 URL')
        self.batch_button.clicked.connect(self.show_batch_dialog)
        self.stop_button = QPushButton('停止所选')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_download)
        self.stop_all_button = QPushButton('停止全部')
        self.stop_all_button.setEnabled(False)
        self.stop_all_button.clicked.connect(self.stop_all_downloads)
        self.open_directory_button = QPushButton('打开下载目录')
        self.open_directory_button.clicked.connect(self.open_output_directory)
        action_row.addWidget(self.start_button, 1)
        action_row.addWidget(self.add_queue_button)
        action_row.addWidget(self.batch_button)
        action_row.addWidget(self.stop_button)
        action_row.addWidget(self.stop_all_button)
        action_row.addWidget(self.open_directory_button)
        self.download_actions = QWidget()
        self.download_actions.setLayout(action_row)
        self.download_actions.layout().setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.download_actions)
        self.status_label = QLabel('准备就绪。')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat('0%')
        layout.addWidget(self.progress_bar)
        self.progress_detail = QLabel('分片：-- / --    速度：--    已下载：--    剩余：--')
        self.progress_detail.setObjectName('muted')
        layout.addWidget(self.progress_detail)

        completion = QHBoxLayout()
        self.result_label = QLabel('')
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_file_button = QPushButton('打开文件')
        self.open_folder_button = QPushButton('打开文件夹')
        self.open_file_button.clicked.connect(self.open_result_file)
        self.open_folder_button.clicked.connect(self.open_result_folder)
        self.open_file_button.hide()
        self.open_folder_button.hide()
        completion.addWidget(self.result_label, 1)
        completion.addWidget(self.open_file_button)
        completion.addWidget(self.open_folder_button)
        self.download_completion = QWidget()
        self.download_completion.setLayout(completion)
        self.download_completion.layout().setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.download_completion)

        log_row = QHBoxLayout()
        self.log_toggle = QToolButton()
        self.log_toggle.setText(APP_NAME + ' 下载日志')
        self.log_toggle.setCheckable(True)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log_toggle.setArrowType(Qt.ArrowType.RightArrow)
        log_row.addWidget(self.log_toggle)
        log_row.addStretch()
        self.detection_button = QPushButton('检测详情')
        self.detection_button.clicked.connect(self.show_detection)
        log_row.addWidget(self.detection_button)
        self.full_error_button = QPushButton('查看完整错误')
        self.full_error_button.setEnabled(False)
        self.full_error_button.clicked.connect(self.show_full_error)
        copy_log = QPushButton('复制日志')
        copy_log.clicked.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))
        self.export_log_button = QPushButton('导出日志')
        self.export_log_button.clicked.connect(self.export_log)
        clear_log = QPushButton('清空日志')
        clear_log.clicked.connect(self.clear_log)
        log_row.addWidget(self.full_error_button)
        log_row.addWidget(copy_log)
        log_row.addWidget(self.export_log_button)
        log_row.addWidget(clear_log)
        layout.addLayout(log_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(100)
        self.log_view.setMaximumHeight(160)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFont('Consolas', 10))
        layout.addWidget(self.log_view)
        self.log_view.hide()
        self.log_toggle.toggled.connect(self.log_view.setVisible)
        self.log_toggle.toggled.connect(lambda shown: self.log_toggle.setArrowType(
            Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow))
        footer = QLabel('支持普通 HLS AES-128 和明文 HLS / DASH。仅下载有权保存的媒体；明确 DRM 会停止。')
        footer.setObjectName('muted')
        footer.setWordWrap(True)
        layout.addWidget(footer)
        self.request_controls = [request_box, self.advanced_toggle, self.advanced_box, self.select_tool_button,
                                 self.detect_button, self.browse_button]
        self.max_concurrent_videos.valueChanged.connect(self.change_video_limit)
        self.max_concurrent_requests.valueChanged.connect(self.change_request_limit)
        self.concurrent_fragments.valueChanged.connect(lambda _: self.save_settings())
        self.segment_max_attempts.valueChanged.connect(lambda _: self.save_settings())
        self.browser_body_limit.valueChanged.connect(lambda _: self.save_settings())
        self.download_quality.currentIndexChanged.connect(lambda _: self.save_settings())
        self.input_tabs.currentChanged.connect(self.on_main_tab_changed)

    def on_main_tab_changed(self, index):
        library = self.input_tabs.widget(index) is self.media_library
        self.media_library.set_active(library)
        managed = library or self.input_tabs.widget(index) in (self.batch_panel, self.storage_panel)
        self.download_actions.setVisible(not managed)
        self.download_completion.setVisible(not managed)
        self.progress_bar.setVisible(not managed)
        self.progress_detail.setVisible(not managed)

    def on_library_message(self, message):
        self.append_log('媒体库：' + message)
        if '存储' in message or '数据库' in message:
            self.status_label.setText('媒体库：' + message)

    def toggle_advanced(self, checked):
        self.advanced_box.setVisible(checked)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def detect_tools(self):
        if self.detector is not None:
            return
        self.detect_button.setEnabled(False)
        self.select_tool_button.setEnabled(False)
        self.reset_tool_button.setEnabled(False)
        for name in ('yt-dlp', 'ffmpeg', 'ffprobe'):
            key = name.replace('-', '_')
            self.on_tool_detected(name, ToolInfo(path=self.settings.get(key + '_path', ''), status='checking'))
        self.detector = ToolDetectionWorker(self.settings, self)
        self.detector.tool_detected.connect(self.on_tool_detected)
        self.detector.detected.connect(self.on_tools_detected)
        self.detector.finished.connect(self.on_detection_finished)
        self.detector.start()

    def on_tool_detected(self, name, info):
        attribute = {'yt-dlp': 'yt_info', 'ffmpeg': 'ff_info', 'ffprobe': 'probe_info'}[name]
        setattr(self, attribute, info)
        row = ('yt-dlp', 'ffmpeg', 'ffprobe').index(name)
        if info.error or info.warning:
            self._tool_last_errors[name] = info.error or info.warning
        values = (name, info.path or '自动查找', info.label, info.version or '—',
                  self._tool_last_errors.get(name, '—'))
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            self.tool_table.setItem(row, column, item)
        infos = (('yt-dlp', self.yt_info), ('FFmpeg', self.ff_info), ('ffprobe', self.probe_info))
        self.tool_status.setText('  |  '.join(f'{title}：{item.label}' for title, item in infos))
        self.tool_status.setToolTip('\n'.join(item.error or item.warning or item.path for _, item in infos))

    def on_tools_detected(self, yt_info, ff_info):
        self.on_tool_detected('yt-dlp', yt_info)
        self.on_tool_detected('ffmpeg', ff_info)
        for name, info in (('yt_dlp', yt_info), ('ffmpeg', ff_info), ('ffprobe', self.probe_info)):
            if info.found and info.source != 'custom':
                # Auto mode stores no release-specific absolute path, so moving the
                # entire folder does not pin the old installation.
                self.settings[name + '_path'] = ''
                self.settings[name + '_mode'] = 'auto'
        for job in self.queue.jobs:
            if job.worker is None and job.state != 'completed':
                if yt_info.found:
                    job.request.yt_dlp_path = yt_info.path
                if ff_info.found:
                    job.request.ffmpeg_path = ff_info.path
                if self.probe_info.found:
                    job.request.ffprobe_path = self.probe_info.path
        self.queue.save_tasks()
        self.save_settings()

    def on_detection_finished(self):
        self.detect_button.setEnabled(not self.is_downloading())
        self.select_tool_button.setEnabled(not self.is_downloading())
        self.reset_tool_button.setEnabled(not self.is_downloading())
        if self.detector:
            self.detector.deleteLater()
        self.detector = None

    def select_yt_dlp(self):
        self.select_tool('yt-dlp')

    def select_tool(self, name):
        path, _ = QFileDialog.getOpenFileName(self, f'选择 {name}.exe', '', 'Windows 程序 (*.exe)')
        if path:
            key = name.replace('-', '_')
            self.settings[key + '_path'] = path
            self.settings[key + '_mode'] = 'custom'
            self.save_settings()
            self.detect_tools()

    def reset_tool_path(self):
        key = self.tool_choice.currentData().replace('-', '_')
        self.settings[key + '_path'] = ''
        self.settings[key + '_mode'] = 'auto'
        self.save_settings()
        self.detect_tools()

    def open_tool_directory(self):
        info = {'yt-dlp': self.yt_info, 'ffmpeg': self.ff_info, 'ffprobe': self.probe_info}[self.tool_choice.currentData()]
        if info.path and Path(info.path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(info.path).parent)))
        else:
            from core.tool_manager import APP_DIR
            directory = APP_DIR / 'tools'
            if directory.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def read_clipboard_field(self, field):
        text = QApplication.clipboard().text().strip()
        if field is self.url and text.lower().startswith(('curl ', 'curl.exe ', 'curl\n', 'curl\r')):
            self.parse_text(text)
            return
        if not text:
            QMessageBox.information(self, '剪贴板为空', '请先复制媒体地址或请求头。')
            return
        field.setText(text)

    def parse_clipboard(self):
        self.parse_text(QApplication.clipboard().text())

    def paste_curl(self):
        dialog = CurlDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            text = dialog.editor.toPlainText()
            dialog.editor.clear()
            self.parse_text(text)
        dialog.editor.clear()
        dialog.deleteLater()

    def parse_text(self, text) -> bool:
        try:
            candidate = text.strip().removeprefix('\ufeff').strip()
            if candidate.lower().startswith(('http://', 'https://', '\"http://', '\"https://', "'http://", "'https://")):
                parsed_context = parse_input(text)
                context = RequestContext(parsed_context.url, headers=self.collect_headers())
                self.reset_request_payload()
                self.url.setText(context.url)
                self._redactor = Redactor(context.headers, [context.url])
                self.parse_summary.setText('已载入完整 URL；开始下载时自动检测 HLS / DASH / Direct。')
                self.parse_summary.show()
                self.status_label.setText('媒体 URL 已载入，可以开始下载。')
                return True
            parsed = parse_curl(text)
            selected = parsed.url
            if not selected:
                selected, accepted = QInputDialog.getItem(self, '选择媒体地址',
                                                          '发现多个媒体 URL，请选择一个：', parsed.media_urls, 0, False)
                if not accepted:
                    return False
            self.reset_request_payload()
            self.set_headers(parsed.headers)
            self.inherit_range.setChecked(bool(re.fullmatch(r'bytes\s*=\s*0\s*-\s*',
                                                           parsed.headers.get('range', ''), re.I)))
            self.url.setText(selected)
            self._request_method = parsed.method
            self._request_body = parsed.body
            self._redactor = Redactor(parsed.headers, parsed.urls)
            sensitive = sum(is_sensitive_header(name) for name in parsed.headers)
            # 地址只显示在输入框；预览隐藏签名并且不会复制进日志。
            safe_url = Redactor().redact(selected)
            preview = (f'解析成功（{parsed.shell_format.upper()}）  ·  Headers：{len(parsed.headers)} 个\n'
                       f'HTTP Method：{parsed.method}  ·  Body：{len(parsed.body or b"")} bytes（不显示正文）\n'
                       f'媒体地址：{safe_url}\nOrigin：{parsed.origin or "--"}\n'
                       f'Referer：{Redactor().redact(parsed.referer) or "--"}\n'
                       f'User-Agent：{parsed.user_agent or "--"}')
            if sensitive:
                preview += f'\n已载入 {sensitive} 个敏感 Header（********），只在当前会话内使用。'
            if parsed.headers.get('range'):
                preview += '\nRange 继承：' + ('已开启（完整 bytes=0-，可在高级设置关闭）' if self.inherit_range.isChecked()
                                             else '已隔离；如服务端要求，可在高级设置开启')
            self.parse_summary.setText(preview)
            self.parse_summary.show()
            self.status_label.setText('解析成功，可直接开始下载，也可修改字段或查看全部 Headers。')
            self.append_log(f'cURL 解析成功：{parsed.shell_format.upper()}，{len(parsed.headers)} 个请求头。')
            for warning in parsed.warnings:
                self.append_log(warning)
            self.save_settings()
            return True
        except (CurlParseError, ValueError) as error:
            # 不把原始 cURL 或含凭据的错误文本写入普通日志。
            safe_error = Redactor().redact(str(error))
            self.last_error = safe_error
            self.full_error_button.setEnabled(True)
            QMessageBox.warning(self, '解析 cURL 失败', safe_error)
            return False

    def collect_headers(self):
        headers = self.custom_headers.headers()
        for name, value in (('origin', self.origin.text()), ('referer', self.referer.text()),
                            ('user-agent', self.user_agent.text()), ('accept', self.accept.text()),
                            ('accept-language', self.accept_language.text())):
            if value.strip():
                headers[name] = value.strip()
            else:
                headers.pop(name, None)
        return normalize_headers(headers)

    def set_headers(self, headers):
        headers = normalize_headers(headers)
        for name, edit in (('origin', self.origin), ('referer', self.referer), ('user-agent', self.user_agent),
                           ('accept', self.accept), ('accept-language', self.accept_language)):
            edit.setText(headers.get(name, ''))
        self.custom_headers.load_headers({key: value for key, value in headers.items() if key not in COMMON_FIELDS})

    def edit_headers(self):
        try:
            dialog = HeadersDialog(self.collect_headers(), self)
        except ValueError as error:
            QMessageBox.warning(self, '请求头需要修改', str(error))
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_headers(dialog.table.headers())
            self.status_label.setText('请求头已更新，可开始下载。')
        dialog.deleteLater()

    def update_signature_hint(self):
        self.signature_hint.setVisible(has_temporary_signature(self.url.text()))

    def reset_request_payload(self):
        self._request_method = 'GET'
        self._request_body = None
        self._discovery_context = None
        self._discovery_metadata = {}
        self._discovery_payload = {}

    def open_discovery(self, *_, page_url='', refresh_job_id=None, activate=True):
        if getattr(self, 'batch_driver', None) and self.batch_driver.busy:
            self.status_label.setText('批次正在使用浏览器。可在“批次任务”暂停分析后再打开手动媒体发现。')
            return None
        if self._clearing_browser_data:
            self.status_label.setText('正在清除浏览器数据，请稍后打开媒体发现。')
            return None
        self._refresh_job_id = refresh_job_id
        if self._discovery_dialog:
            if not self._discovery_dialog._closing and activate:
                self._discovery_dialog.show()
                self._discovery_dialog.raise_()
                self._discovery_dialog.activateWindow()
            return self._discovery_dialog
        from ui.discovery_dialog import DiscoveryDialog
        initial = page_url or self.url.text().strip()
        if not initial.lower().startswith(('https://', 'http://')):
            initial = ''
        job = self.queue.get(refresh_job_id) if refresh_job_id is not None else None
        session_id = job.request.media_metadata.get('browser_session_id') if job else None
        profile = self._resolver_sessions.get(session_id)
        dialog = DiscoveryDialog(self, initial, session_profile=profile,
                                 resolver_registry=self._resolver_session_registries.get(session_id))
        self._discovery_dialog = dialog
        dialog.media_selected.connect(self.import_discovered_media)
        if refresh_job_id is None:
            dialog.download_now.connect(self.start_download)
        dialog.batch_requested.connect(self.download_discovered_batch)
        dialog.worker.candidates_changed.connect(self._auto_refresh_candidates)
        dialog.shutdown_complete.connect(lambda: self._discovery_closed(dialog))
        if self.isMinimized():
            dialog.showMinimized()
        elif activate:
            dialog.show()
        return dialog

    def webview2_profile(self):
        """One environment per window, shared by discovery and refresh pages."""
        from ui.webview2_browser import WebView2Profile
        profile = self._webview2_profile
        if profile is None or profile.host._finished_once:
            if profile is not None:
                profile.deleteLater()
            profile = WebView2Profile(self, user_data_folder=self.browser_profile_directory)
            self._webview2_profile = profile
            profile.host.closed.connect(self._browser_host_closed)
        return profile

    def _browser_host_closed(self):
        if self._close_after_browser:
            QTimer.singleShot(0, self.close)

    def clear_browser_data(self):
        if self._clearing_browser_data:
            return
        if (self._discovery_dialog or self._refresh_dialogs or self.is_downloading()
                or getattr(self, 'batch_driver', None) and self.batch_driver.busy):
            QMessageBox.information(self, '浏览器数据正在使用', '请先关闭媒体发现窗口并等待下载和刷新任务结束，再清除浏览器数据。')
            return
        answer = QMessageBox.question(self, '清除浏览器数据',
            '将清除 MediaFlow 本地浏览器的 Cookie、网站存储和缓存，并退出已登录的网站。\n是否继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from ui.webview2_browser import select_browser_backend
        if select_browser_backend() != 'webview2':
            QMessageBox.information(self, 'WebView2 未启用', '请在 Windows WebView2 模式下管理持久浏览器数据。')
            return
        self._clearing_browser_data = True
        self.clear_browser_button.setEnabled(False)
        self.status_label.setText('正在清除本地浏览器数据…')
        self.webview2_profile().clear_browsing_data(self._browser_data_cleared)

    def _browser_data_cleared(self, result, ok):
        self._clearing_browser_data = False
        self.clear_browser_button.setEnabled(True)
        self.status_label.setText('浏览器数据已清除，下次访问需要重新登录。' if ok and result is True
            else '浏览器数据未清除，请关闭使用此 Profile 的窗口后重试。')
        if self._close_after_browser:
            QTimer.singleShot(0, self.close)

    def _discovery_closed(self, dialog):
        if self._discovery_dialog is dialog:
            self._discovery_dialog = None
        dialog.deleteLater()
        if self._auto_refresh_job_id is not None and self._auto_refresh_dialog is dialog:
            self._auto_refresh_done()
        if self._close_after_discovery:
            QTimer.singleShot(0, self.close)

    def _remember_resolver_session(self, candidate, dialog=None):
        """只在已验证的候选/任务生命周期中持有既有浏览器 Profile 引用。"""
        dialog = dialog or self._discovery_dialog
        if not candidate.metadata.get('resolver_verified') or not dialog or dialog._closing:
            return
        profile = dialog.retain_session_profile()
        session_id = next((key for key, value in self._resolver_sessions.items() if value is profile), None)
        if session_id is None:
            session_id = uuid.uuid4().hex
            self._resolver_sessions[session_id] = profile
            self._resolver_session_registries[session_id] = dialog.worker.resolver_registry
        candidate.metadata['browser_session_id'] = session_id

    def import_discovered_media(self, candidate):
        if not candidate.downloadable:
            return
        try:
            from core.media_tracks import apply_selection
            previous = self.queue.get(self._refresh_job_id) if self._refresh_job_id is not None else None
            choice = previous.request.media_metadata.get('track_selection') if previous else None
            candidate = apply_selection(candidate, choice)
        except ValueError as error:
            self.status_label.setText(str(error))
            return
        self._remember_resolver_session(candidate)
        context = deepcopy(candidate.request_context)
        self.set_headers(context.headers)
        self.url.setText(context.url)
        self._request_method, self._request_body = context.method, context.body
        self._discovery_context = context
        self._discovery_metadata = deepcopy(candidate.metadata)
        from core.output_organization import make_values
        from core.download_manager import output_extension
        self._discovery_metadata['organization_values'] = make_values(
            candidate.metadata.get('title') or candidate.metadata.get('page_title') or '',
            source_url=candidate.metadata.get('page_url') or candidate.metadata.get('source_page') or context.referer,
            site=candidate.metadata.get('source_site', ''), media_id=candidate.metadata.get('video_id', ''),
            resolution=candidate.resolution, quality=candidate.resolution_label, extension=output_extension(candidate))
        self._update_output_preview()
        self._discovery_payload = candidate.download_payload()
        self.inherit_range.setChecked(context.inherit_range)
        self.inherit_browser_metadata.setChecked(context.inherit_browser_metadata)
        self._redactor = Redactor(context.headers, [context.url, context.original_url, context.final_url])
        values = candidate.safe_diagnostic()
        manifest = values.get('manifest') or {}
        values.update(keys=manifest.get('keys', []), resources=manifest.get('resources', []),
                      evidence=manifest.get('evidence', []), hls_playlist_type=candidate.playlist_type,
                      stage='自动查找 / 待下载时复检')
        self._diagnostic = values
        self.parse_summary.setText(f'自动查找已导入：{candidate.media_type} · {candidate.resolution_label}'
                                   f' · {candidate.encryption}\nHTTP Method：{context.method}'
                                   f' · Headers：{len(context.headers)} 个（凭据仅在内存使用）\n'
                                   f'媒体地址：{self._redactor.redact(context.url)}')
        self.parse_summary.show()
        self.input_tabs.setCurrentIndex(0)
        if self._refresh_job_id is not None:
            job_id, self._refresh_job_id = self._refresh_job_id, None
            try:
                self.queue.retry_with_context(job_id, context, candidate.metadata, candidate.download_payload())
                self.queue.start()
                self.select_job(job_id)
                self.status_label.setText('已更新同一任务的媒体地址，保留部分文件并核对视频身份后继续。')
            except ValueError as error:
                self.status_label.setText(str(error))
        else:
            self.status_label.setText('已导入媒体及其实际请求上下文，可加入队列或开始下载。')

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, '选择输出目录', self.output_directory.text())
        if path:
            self.output_directory.setText(path)
            self.save_settings()

    def download_discovered_batch(self, groups, options):
        dialog = self._discovery_dialog
        try:
            for group in groups:
                for item in group.candidates:
                    self._remember_resolver_session(item, dialog)
                if group.selected_candidate is not None:
                    self._remember_resolver_session(group.selected_candidate, dialog)
            candidate = next((g.selected_candidate for g in groups if g.selected and g.selected_candidate), None)
            if candidate is None:
                raise ValueError('请勾选至少一个已确认可下载的视频。')
            # 只借用工具和输出配置；每个媒体仍使用自己的实际浏览器上下文。
            template = self._collect_request(candidate.url, '')
            jobs = self.download_manager.add_batch(groups, template, **options)
        except (ValueError, OSError) as error:
            if dialog:
                dialog.batch_result(False, Redactor().redact(str(error)))
            return []
        if dialog:
            dialog.batch_result(True, f'已将 {len(jobs)} 个任务加入队列并启动；请在主窗口任务队列查看进度。')
        self.select_job(jobs[0].id)
        self.input_tabs.setCurrentIndex(1)
        self.save_settings()
        return jobs

    def offer_quality_fallback(self, job_id, current, next_label):
        def ask():
            job = self.queue.get(job_id)
            if job is None or job.state != 'failed' or job.worker is not None:
                return
            answer = QMessageBox.question(self, '选择备用清晰度',
                f'{job.name}：{current} 下载失败，是否改用 {next_label}？\n原质量的未完成文件会保留；新质量使用独立暂存文件。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.download_manager.use_next_quality(job_id)
        QTimer.singleShot(0, ask)

    def pause_selected(self):
        if self.selected_job_id is not None:
            self.queue.pause_job(self.selected_job_id)

    def resume_selected(self):
        if self.selected_job_id is not None:
            try:
                self.queue.resume_job(self.selected_job_id)
                self.queue.start()
            except ValueError as error:
                self.status_label.setText(str(error))

    def open_task_directory(self):
        job = self.queue.get(self.selected_job_id)
        if job:
            from core.output_organization import final_directory
            self._open_path(str(final_directory(job.request)))

    def auto_refresh_media(self, job_id):
        job = self.queue.get(job_id)
        if job and job.request.media_metadata.get('batch_item_id'):
            job.status = '媒体链接需要更新；请在批次任务中重试，重新分析原始播放页。'
            self.queue.changed.emit(job.id)
            return
        if job_id not in self._auto_refresh_ids:
            self._auto_refresh_ids.append(job_id)
        QTimer.singleShot(0, self._start_auto_refresh)

    def _start_auto_refresh(self):
        if self._auto_refresh_job_id is not None or not self._auto_refresh_ids or self._close_after_download:
            return
        if getattr(self, 'batch_driver', None) and self.batch_driver.busy:
            QTimer.singleShot(1000, self._start_auto_refresh)
            return
        job = self.queue.get(self._auto_refresh_ids.pop(0))
        if job is None or job.state != 'waiting_context':
            QTimer.singleShot(0, self._start_auto_refresh)
            return
        dialog = self._discovery_dialog
        session_id = job.request.media_metadata.get('browser_session_id')
        profile = self._resolver_sessions.get(session_id)
        if job.request.media_metadata.get('resolver_verified') and profile is not None:
            # 隔离的隐藏 Page 使用任务原来的合法 Profile，允许用户继续操作现有浮窗。
            from ui.discovery_dialog import DiscoveryDialog
            self._auto_refresh_job_id = job.id
            dialog = DiscoveryDialog(self, job.request.media_metadata['page_url'], session_profile=profile,
                                     resolver_registry=self._resolver_session_registries.get(session_id))
            self._refresh_dialogs.add(dialog)
            dialog.worker.candidates_changed.connect(self._auto_refresh_candidates)
            dialog.shutdown_complete.connect(lambda current=dialog: self._refresh_dialog_closed(current))
            job.status = '签名可能已失效，使用原浏览器会话重新请求媒体 API…'
            self.queue.changed.emit(job.id)
            self._auto_refresh_dialog = dialog
            self._auto_refresh_timer.start()
            dialog.deep_find()
            self._auto_refresh_generation = dialog.worker.generation
            return
        if dialog and (dialog._closing or dialog._listening or dialog._batch_pending):
            job.status = '地址可能已过期；自动查找正在使用，请点击“刷新媒体地址并继续”'
            self.queue.changed.emit(job.id)
            QTimer.singleShot(0, self._start_auto_refresh)
            return
        self._auto_refresh_job_id = job.id
        dialog = self.open_discovery(page_url=job.request.media_metadata['page_url'], activate=False)
        dialog.page_url.setText(job.request.media_metadata['page_url'])
        job.status = '地址可能已过期，正在重新解析原播放页…'
        self.queue.changed.emit(job.id)
        self._auto_refresh_timer.start()
        dialog.deep_find()
        self._auto_refresh_dialog = dialog
        self._auto_refresh_generation = dialog.worker.generation

    def _auto_refresh_candidates(self, candidates, _player, generation):
        job = self.queue.get(self._auto_refresh_job_id)
        dialog = self._auto_refresh_dialog
        if (not job or not dialog or dialog._closing
                or generation != self._auto_refresh_generation or generation != dialog.worker.generation
                or job.worker is not None or job.state != 'waiting_context'):
            return
        identity = job.request.media_metadata
        matched = next((candidate for candidate in candidates if candidate.downloadable
                        and candidate.metadata.get('resolver_id') == identity.get('resolver_id')
                        and candidate.metadata.get('video_id') == identity.get('video_id')
                        and candidate.metadata.get('page_url') == identity.get('page_url')
                        and (not identity.get('resolver_verified') or
                             candidate.metadata.get('resolver_verified') is True)
                        and (candidate.url != job.request.url or candidate.metadata.get('discovered_at', 0)
                             > identity.get('discovered_at', 0))), None)
        if matched:
            try:
                if job.request.media_metadata.get('track_selection'):
                    from core.media_tracks import apply_selection
                    matched = apply_selection(matched, job.request.media_metadata['track_selection'])
                self._remember_resolver_session(matched, dialog)
                self.queue.retry_with_context(job.id, matched.request_context, matched.metadata, matched.download_payload())
                self.queue.start()
            except ValueError as error:
                job.status = str(error)
            self._auto_refresh_done()

    def _auto_refresh_done(self):
        self._auto_refresh_timer.stop()
        job = self.queue.get(self._auto_refresh_job_id)
        dialog, generation = self._auto_refresh_dialog, self._auto_refresh_generation
        self._auto_refresh_job_id = None
        self._auto_refresh_dialog = None
        self._auto_refresh_generation = None
        if job and job.state == 'waiting_context':
            job.status = '媒体地址可能已过期，自动刷新未完成；请点击“刷新媒体地址并继续”'
            self.queue.changed.emit(job.id)
        if dialog in self._refresh_dialogs:
            dialog.shutdown()
        elif (dialog and dialog is self._discovery_dialog and not dialog._closing
                and dialog.worker.generation == generation):
            dialog._listen_ended()
        QTimer.singleShot(0, self._start_auto_refresh)

    def _refresh_dialog_closed(self, dialog):
        self._refresh_dialogs.discard(dialog)
        dialog.deleteLater()
        if self._auto_refresh_dialog is dialog:
            self._auto_refresh_done()
        if self._close_after_discovery:
            QTimer.singleShot(0, self.close)

    def is_downloading(self):
        return self.queue.active_count > 0

    def _update_output_preview(self, *_):
        if not hasattr(self, 'output_options'):
            return
        from core.output_organization import make_values, site_name
        metadata = self._discovery_metadata
        values = make_values(self.filename.text() or metadata.get('title', ''),
            source_url=metadata.get('page_url', ''), site=metadata.get('source_site') or site_name(source_url=self.url.text()),
            media_id=metadata.get('video_id', ''), resolution=metadata.get('height', 0),
            quality=metadata.get('quality_label', ''), extension=metadata.get('track_output_extension') or 'mp4')
        if isinstance(metadata.get('organization_values'), dict):
            values = metadata['organization_values']
        self.output_options.set_context(self.output_directory.text(), values)

    def _collect_request(self, url=None, filename=None):
        self.output_options.validate()
        request = DownloadRequest(yt_dlp_path=self.yt_info.path if self.yt_info.found else '',
                                  url=self.url.text().strip() if url is None else url,
                                  output_directory=self.output_directory.text().strip(),
                                  filename=self.filename.text() if filename is None else filename,
                                  headers=self.collect_headers(), ffmpeg_path=self.ff_info.path if self.ff_info.found else None,
                                  method=self._request_method if url is None else 'GET',
                                  body=self._request_body if url is None else None,
                                  inherit_browser_metadata=self.inherit_browser_metadata.isChecked(),
                                  inherit_range=self.inherit_range.isChecked(),
                                  concurrent_fragments=self.concurrent_fragments.value(),
                                  context_snapshot=self._discovery_context if url is None else None,
                                  segment_max_attempts=self.segment_max_attempts.value(),
                                  format_selector=self._discovery_metadata.get('format_selector', '') if url is None else '',
                                  media_metadata=deepcopy(self._discovery_metadata) if url is None else {},
                                  output_extension=self._discovery_metadata.get('track_output_extension', '') if url is None else '',
                                  ffprobe_path=self.probe_info.path if self.probe_info.found else '',
                                  organization=self.output_options.options(),
                                  **(deepcopy(self._discovery_payload) if url is None else {}))
        if request.organization.get('filename_template'):
            from core.output_organization import make_values, site_name
            request.media_metadata.setdefault('organization_values', make_values(request.filename or request.media_metadata.get('title', ''),
                source_url=request.media_metadata.get('page_url', ''),
                site=request.media_metadata.get('source_site') or site_name(source_url=request.url),
                extension=request.output_extension or 'mp4'))
            request.filename = ''
        build_arguments(request, require_tools=False)
        return request

    def add_current_to_queue(self):
        try:
            if self.url.text().strip().lower().startswith(('curl ', 'curl.exe ')):
                if not self.parse_text(self.url.text()):
                    return
            job = self.queue.add(self._collect_request())
        except ValueError as error:
            QMessageBox.warning(self, '请检查输入', str(error))
            return
        self.select_job(job.id)
        self.save_settings()
        return job

    def start_download(self):
        job = self.add_current_to_queue()
        if job:
            self.start_queue()
            self.input_tabs.setCurrentIndex(1)

    def show_batch_dialog(self):
        defaults = dict(self.settings.get('batch_import', {}))
        defaults.update(output_directory=self.output_directory.text(),
                        organization=self.output_options.options(),
                        max_concurrent=self.max_concurrent_videos.value(),
                        quality=self.download_quality.currentData())
        dialog = BatchImportDialog(self, existing=self.batch_controller.existing_urls(), defaults=defaults)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.submit_batch_urls(dialog.result_urls, dialog.options)
            except ValueError as error:
                QMessageBox.warning(self, '批量输入需要修改', Redactor().redact(str(error)))
        dialog.deleteLater()

    def submit_batch_urls(self, urls, options):
        batch_id = self.batch_controller.create(urls, options)
        settings = self.batch_controller.batches[batch_id]['options']
        self.max_concurrent_videos.setValue(settings['max_concurrent'])
        self.output_directory.setText(settings['output_directory'])
        self.settings['batch_import'] = dict(settings)
        self.save_settings()
        self.input_tabs.setCurrentWidget(self.batch_panel)
        self._update_batch_panel()
        return batch_id

    def _batch_template(self, options):
        # 批次不能继承媒体输入栏中另一个网址的 Cookie、Range 或正文。
        return DownloadRequest(yt_dlp_path=self.yt_info.path if self.yt_info.found else '',
            url='https://example.invalid/', output_directory=options['output_directory'],
            ffmpeg_path=self.ff_info.path if self.ff_info.found else '',
            ffprobe_path=self.probe_info.path if self.probe_info.found else '',
            concurrent_fragments=self.concurrent_fragments.value(),
            segment_max_attempts=self.segment_max_attempts.value())

    def on_batch_action(self, action, batch_id):
        if action == 'open_collection':
            batch = self.batch_controller.batches.get(batch_id)
            if batch:
                from core.output_organization import check_collection
                options = batch['options']
                try:
                    directory = check_collection(options['output_directory'], options.get('collection_directory', ''), batch_id)
                    self._open_path(str(directory))
                except ValueError as error:
                    self._batch_message(str(error))
            return
        self.batch_controller.batch_action(action, batch_id)

    def _batch_browser_available(self):
        if self.queue._storage_cleanup_guards:
            return False
        return not (self._discovery_dialog or self._refresh_dialogs or self._clearing_browser_data
                    or self._auto_refresh_ids or self._auto_refresh_job_id is not None
                    or self._close_after_download or self._close_after_browser)

    def _batch_message(self, message):
        self.append_log('批次：' + message)
        self.status_label.setText('批次：' + message)

    def _update_batch_panel(self):
        self.batch_panel.set_snapshot(*self.batch_controller.snapshot())

    def on_library_paths_moved(self, mappings):
        from core.output_relocation import apply_completed_paths, RelocationJournalWorker
        affected = apply_completed_paths(self.queue, mappings)
        self.batch_controller.relocate_completed_paths(mappings)
        for job in affected:
            self.batch_controller._job_changed(job.id)
        if affected:
            worker = RelocationJournalWorker([job.request for job in affected], self)
            self._relocation_workers.add(worker)
            worker.notice.connect(self.on_library_message)
            worker.finished.connect(lambda current=worker: self._relocation_finished(current))
            worker.start()

    def _relocation_finished(self, worker):
        self._relocation_workers.discard(worker)
        worker.deleteLater()
        if self._close_after_relocation and not self._relocation_workers:
            self.close()

    def on_batch_item_action(self, action, item_id):
        item = self.batch_controller.items.get(item_id)
        if not item:
            return
        if action == 'select':
            groups = self.batch_controller.candidate_groups(item_id)
            if not groups:
                self.batch_controller.item_action('select', item_id)
                return
            from ui.batch_discovery import BatchCandidateDialog
            preferences = self.batch_controller.batches[item['batch_id']]['options'].get('track_preferences')
            dialog = BatchCandidateDialog(groups, self, track_preferences=preferences)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.batch_controller.select_candidate(item_id, dialog.candidate_id, confirmed=True,
                                                      track_selection=dialog.track_selection)
            dialog.deleteLater()
        elif action == 'show':
            if self.batch_controller.active_item_id == item_id:
                self.batch_driver.show_page()
            else:
                job = self.batch_controller._job(item)
                if job:
                    self.select_job(job.id)
                    self.input_tabs.setCurrentIndex(1)
                else:
                    text = next((row for row in self.batch_controller.snapshot()[1] if row['id'] == item_id), {})
                    box = QMessageBox(self)
                    box.setWindowTitle('批次任务详情')
                    box.setTextFormat(Qt.TextFormat.PlainText)
                    box.setText(str(text.get('title', '')))
                    box.setInformativeText(str(text.get('source_display', '')) + '\n' + str(text.get('error') or '尚未加入下载队列。'))
                    box.exec()
        else:
            self.batch_controller.item_action(action, item_id)

    def add_batch_urls(self, text):
        try:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines:
                raise ValueError('请粘贴至少一个 URL，每行一个。')
            if len(lines) > 100:
                raise ValueError('每次最多添加 100 个 URL。')
            # 全部验证后再入队，防止一半成功、一半失败且未经选择开始下载。
            requests = []
            for number, line in enumerate(lines, 1):
                if not line.strip('\"\'').lower().startswith(('https://', 'http://')):
                    raise ValueError(f'第 {number} 行需要单个 URL；完整 cURL 请在媒体输入中解析后加入队列。')
                requests.append(self._collect_request(parse_input(line).url, ''))
            jobs = [self.queue.add(request) for request in requests]
        except (ValueError, CurlParseError) as error:
            QMessageBox.warning(self, '批量输入需要修改', Redactor().redact(str(error)))
            return []
        self.select_job(jobs[0].id)
        self.input_tabs.setCurrentIndex(1)
        self.save_settings()
        return jobs

    def start_queue(self):
        self.queue.start()
        self.update_queue_controls()

    def pause_queue(self):
        self.queue.pause()
        self.update_queue_controls()

    def change_video_limit(self, value):
        self.queue.set_max_concurrent(value)
        self.update_queue_controls()
        self.save_settings()

    def change_request_limit(self, value):
        self.queue.concurrency.set_limit(value)
        self.save_settings()

    def stop_download(self):
        if self.selected_job_id is not None:
            self.queue.cancel(self.selected_job_id)
        self.update_queue_controls()

    def stop_all_downloads(self):
        self.queue.stop_all()
        self.update_queue_controls()

    def retry_selected(self):
        if self.selected_job_id is not None:
            try:
                self.queue.retry(self.selected_job_id)
            except ValueError as error:
                QMessageBox.warning(self, '暂时无法重试', str(error))
        self.update_queue_controls()

    def update_selected_link(self):
        if self.selected_job_id is None:
            return
        try:
            if self.url.text().strip().lower().startswith(('curl ', 'curl.exe ')):
                if not self.parse_text(self.url.text()):
                    return
            request = self._collect_request()
            self.queue.retry_with_context(self.selected_job_id, request.to_context(),
                                          self._discovery_metadata or None,
                                          self._discovery_payload or None)
            self.queue.start()
        except ValueError as error:
            QMessageBox.warning(self, '无法更新链接', str(error))

    def refresh_selected_media(self):
        job = self.queue.get(self.selected_job_id)
        if job and job.state in ('failed', 'cancelled', 'waiting_context', 'paused') and job.worker is None:
            page_url = job.request.media_metadata.get('page_url', '')
            if page_url:
                dialog = self.open_discovery(page_url=page_url, refresh_job_id=job.id)
                if dialog is not None:
                    dialog.page_url.setText(page_url)
                    dialog.deep_find()

    def remove_finished_jobs(self):
        self.queue.remove_finished()
        existing = {job.id for job in self.queue.jobs}
        self.queue_table.blockSignals(True)
        for job_id, row in sorted(self._job_rows.items(), key=lambda item: item[1], reverse=True):
            if job_id not in existing:
                self.queue_table.removeRow(row)
        self._job_rows = {self.queue_table.item(row, 0).data(Qt.ItemDataRole.UserRole): row
                          for row in range(self.queue_table.rowCount())}
        self.queue_table.blockSignals(False)
        if self.selected_job_id not in existing:
            self.selected_job_id = None
            self.worker = None
            self.last_result = None
            self.last_error = ''
            self._diagnostic = {}
            self._progress_state = {}
            self.log_view.clear()
            self.result_label.clear()
            self.open_file_button.hide()
            self.open_folder_button.hide()
            self.full_error_button.setEnabled(False)
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat('%p%')
            self.update_progress({})
            self.status_label.setText('准备就绪。')
            if self.queue.jobs:
                self.select_job(self.queue.jobs[0].id)
        self.update_queue_controls()

    def select_job(self, job_id):
        row = self._job_rows.get(job_id)
        if row is not None:
            self.queue_table.selectRow(row)
            if self.selected_job_id != job_id:
                self.on_job_selection_changed()

    def on_job_selection_changed(self):
        selected = self.queue_table.selectionModel().selectedRows()
        if not selected:
            return
        self.selected_job_id = self.queue_table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        job = self.queue.get(self.selected_job_id)
        if job:
            self.log_view.setPlainText('\n'.join(job.logs))
            self.present_selected_job(job)
        self.update_queue_controls()

    def on_job_changed(self, job_id):
        job = self.queue.get(job_id)
        if job and job.state == 'running' and job_id not in self._job_started_at:
            self._job_started_at[job_id] = utc_now()
        if not job:
            return
        row = self._job_rows.get(job_id)
        if row is None:
            row = self.queue_table.rowCount()
            self._job_rows[job_id] = row
            self.queue_table.insertRow(row)
            for column in range(6):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, job_id)
                self.queue_table.setItem(row, column, item)
        states = {'queued': '等待', 'running': '下载中', 'stopping': '正在停止',
                  'completed': '完成', 'failed': '失败', 'cancelled': '已停止',
                  'paused': '已暂停', 'waiting_context': '地址可能已过期 / 待刷新'}
        percent = job.progress.get('percent')
        progress_text = f'{max(0, min(100, percent)):.1f}%' if type(percent) in (int, float) else '--'
        if job.state == 'completed':
            progress_text = '100%'
        fragment_index = job.progress.get('fragment_index')
        if job.state == 'completed' and job.progress.get('fragment_count') is not None:
            fragment_index = job.progress['fragment_count']
        name_label = job.name + (' · ' + job.media_candidate.resolution_label if job.media_candidate else '')
        values = [name_label, states.get(job.state, job.state), progress_text,
                  str(job.progress.get('speed') or '--'),
                  f'{fragment_index if fragment_index is not None else "--"} / {job.progress.get("fragment_count") or "--"}',
                  str(job.progress.get('eta') or '--')]
        recovery = job.progress.get('recovery_state')
        if job.state == 'running':
            labels = {'downloading': '正在下载', 'waiting_network': '等待网络', 'retrying': '正在重试',
                      'recovering': '正在恢复', 'resuming': '正在续传', 'restarting': '重新下载',
                      'merging': '正在合并', 'verifying': '正在验证'}
            values[1] = labels.get(recovery, '正在下载')
            if recovery in ('waiting_network', 'retrying'):
                values[1] += f' · 第 {job.progress.get("retry_attempt", 0)} 次 · '
                values[1] += f'{job.progress.get("retry_countdown", 0):.0f} 秒'
        for column, value in enumerate(values):
            self.queue_table.item(row, column).setText(value)
        self.queue_table.item(row, 1).setToolTip(job.status)
        if job_id not in self._task_cards:
            card = TaskCard()
            card.selected.connect(lambda current=job_id: self.select_job(current))
            self._task_cards[job_id] = card
            self.task_card_layout.insertWidget(self.task_card_layout.count() - 1, card)
        self._task_cards[job_id].update_display(values, job.state, percent)
        self._task_cards[job_id].setToolTip(job.status + '\n' +
            str(job.progress.get('last_error') or job.progress.get('recovery_reason') or ''))
        if self.selected_job_id == job_id:
            self.present_selected_job(job)
        self.update_queue_controls()

    def present_selected_job(self, job):
        # 队列已使用本任务的上下文脱敏；新输入不能重写其他任务的日志或诊断。
        self.worker = job.worker
        self._diagnostic = job.diagnostic.copy()
        if not self._diagnostic:
            redactor = Redactor(job.request.headers, [job.request.url])
            self._diagnostic = redact_data({**job.request.to_context().summary(), 'drm': None,
                                           'stage': '等待检测'}, redactor)
        self._progress_state = {}
        self.last_result = None
        self.last_error = ''
        self.full_error_button.setEnabled(False)
        self.result_label.clear()
        self.open_file_button.hide()
        self.open_folder_button.hide()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat('%p%')
        if job.state in ('running', 'stopping') and job.progress.get('percent') is None:
            self.progress_bar.setRange(0, 0)
        self.update_progress(job.progress)
        self.status_label.setText(f'{job.name}：{job.status}')
        if job.result:
            self.on_download_completed(job.result, emit_event=False)
            self.show_thumbnail_status(job.id, job.result)
        if job.state in ('paused', 'waiting_context'):
            self.status_label.setText(f'{job.name}：{job.status}')

    def update_queue_controls(self):
        jobs = self.queue.jobs
        if hasattr(self, '_task_cards'):
            existing_ids = {job.id for job in jobs}
            for job_id in list(self._task_cards):
                card = self._task_cards[job_id]
                if job_id not in existing_ids:
                    self.task_card_layout.removeWidget(card)
                    card.deleteLater()
                    del self._task_cards[job_id]
                else:
                    card.set_selected(job_id == self.selected_job_id)
            self.task_scroll.setVisible(bool(jobs))
        waiting = sum(job.state == 'queued' for job in jobs)
        ended = sum(job.state in ('completed', 'failed', 'cancelled') for job in jobs)
        self.queue_summary.setText(f'队列{"已暂停" if self.queue.paused else "运行中"} · 运行 {self.queue.active_count}'
                                   f' · 等待 {waiting} · 完成 {sum(j.state == "completed" for j in jobs)}'
                                   f' · 失败 {sum(j.state == "failed" for j in jobs)}'
                                   f' · 暂停/待刷新 {sum(j.state in ("paused", "waiting_context") for j in jobs)} · 共 {len(jobs)} 个')
        self.input_tabs.setTabText(1, f'任务队列 ({len(jobs)})')
        selected = self.queue.get(self.selected_job_id) if self.selected_job_id is not None else None
        self.stop_button.setEnabled(bool(selected and selected.state in ('queued', 'running', 'paused', 'waiting_context')))
        self.pause_job_button.setEnabled(bool(selected and selected.state in ('queued', 'running')))
        self.resume_job_button.setEnabled(bool(selected and selected.state == 'paused' and selected.worker is None))
        self.cancel_job_button.setEnabled(self.stop_button.isEnabled())
        self.task_directory_button.setEnabled(bool(selected))
        self.retry_failed_button.setEnabled(any(j.state == 'failed' and j.worker is None for j in jobs))
        self.retry_button.setEnabled(bool(selected and selected.state in ('failed', 'cancelled', 'waiting_context', 'paused') and selected.worker is None))
        self.update_link_button.setEnabled(self.retry_button.isEnabled())
        self.refresh_media_button.setEnabled(bool(self.retry_button.isEnabled() and selected.request.media_metadata.get('page_url')))
        self.stop_all_button.setEnabled(bool(waiting or self.queue.active_count))
        self.start_queue_button.setEnabled(bool(waiting))
        self.pause_queue_button.setEnabled(not self.queue.paused)
        self.remove_finished_button.setEnabled(any(job.worker is None and job.state in ('completed', 'failed', 'cancelled') for job in jobs))
        detecting = self.detector is not None
        self.select_tool_button.setEnabled(not self.is_downloading() and not detecting)
        self.detect_button.setEnabled(not self.is_downloading() and not detecting)
        self.reset_tool_button.setEnabled(not self.is_downloading() and not detecting)

    def on_job_log(self, job_id, text):
        if self.selected_job_id == job_id:
            self.log_view.appendPlainText(text)

    def on_job_completed(self, job_id, result):
        job = self.queue.get(job_id)
        if job:
            self.library_service.record_job(job, self._job_started_at.get(job_id, job.created_at))
        if result.get('success'):
            self.thumbnails.submit(job_id, result.get('files', []), result.get('output_validation'),
                                   self.ff_info.path if self.ff_info.found else '')
        if self.selected_job_id == job_id:
            self.on_download_completed(result, emit_event=False)
        self.show_thumbnail_status(job_id, result)
        self.task_completed.emit(result)

    def show_thumbnail_status(self, job_id, download_result):
        if not download_result.get('success'):
            return
        text = '✓ 下载完成'
        result = self.thumbnails.results.get(job_id)
        if download_result.get('output_validation'):
            text += '  ·  ✓ 媒体验证通过'
        if result:
            labels = {'pending': '正在生成本地缩略图…', 'generated': '✓ 缩略图生成成功',
                      'failed': '⚠ 缩略图生成失败', 'skipped': '已跳过缩略图（无有效视频流）',
                      'cancelled': '缩略图生成已停止'}
            text += '  ·  ' + labels[result['status']]
        path = next((item['path'] for item in result.get('items', [])
                     if item.get('path') and item.get('status') in ('generated', 'existing')), '') if result else ''
        card = self._task_cards.get(job_id)
        if card:
            card.show_thumbnail_result(text, path)
            card.completion_label.setToolTip('\n'.join(item.get('message', '') for item in result.get('items', []))
                                             if result else '')
        if self.selected_job_id == job_id:
            self.status_label.setText(text)

    def on_thumbnail_completed(self, job_id, result):
        self.library_service.update_thumbnails(job_id, result)
        job = self.queue.get(job_id)
        if job and job.result:
            self.show_thumbnail_status(job_id, job.result)

    def on_thumbnails_idle(self):
        if self._close_after_thumbnails:
            QTimer.singleShot(0, self.close)

    def on_queue_idle(self):
        self.update_queue_controls()
        if self._close_after_download:
            self.close()

    def update_progress(self, values):
        for key, value in values.items():
            # finished 输出可能不带分片字段，保留此前已报告的已知计数。
            if key in ('fragment_count', 'fragment_index') and value is None:
                continue
            self._progress_state[key] = value
        state = self._progress_state
        percent = state.get('percent')
        if isinstance(percent, (float, int)):
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(round(percent * 10))
            self.progress_bar.setFormat(f'{percent:.1f}%')
        detail = (f'分片：{state.get("fragment_index") or "--"} / {state.get("fragment_count") or "--"}    '
                  f'速度：{state.get("speed", "--")}    已下载：{state.get("downloaded", "--")} / {state.get("total", "--")}    '
                  f'剩余：{state.get("eta", "--")}')
        self.progress_detail.setText(detail)

    def on_download_completed(self, result, emit_event=True):
        self.last_result = result
        if result.get('diagnostic'):
            self._diagnostic = result['diagnostic'].copy()
        self.progress_bar.setRange(0, 1000)
        if result['success']:
            if self._progress_state.get('fragment_count'):
                self.update_progress({'fragment_index': self._progress_state['fragment_count']})
            self.progress_bar.setValue(1000)
            self.progress_bar.setFormat('100%')
            self.status_label.setText('下载完成')
            self.result_label.setText('文件：' + '\n'.join(result['files']))
            self.open_file_button.show()
            self.open_folder_button.show()
        elif result['cancelled']:
            self.status_label.setText('下载已停止，可重新开始续传。')
            if self.progress_bar.value() < 0:
                self.progress_bar.setValue(0)
            self.progress_bar.setFormat('%p%')
        else:
            self.last_error = result['error']
            self.full_error_button.setEnabled(True)
            self.status_label.setText('下载失败：' + friendly_error(self.last_error))
            self.progress_bar.setFormat('%p%')
        if emit_event:
            self.task_completed.emit(result)

    def append_log(self, text):
        self.log_view.appendPlainText(self._redactor.redact(text))

    def clear_log(self):
        self.log_view.clear()
        job = self.queue.get(self.selected_job_id) if self.selected_job_id is not None else None
        if job:
            job.logs.clear()

    def show_full_error(self):
        ErrorDialog(self.last_error or '暂无错误。', self).exec()

    def show_about(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle('关于 ' + APP_NAME)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f'{APP_NAME} {APP_VERSION}\n{SUBTITLE}\n{ENGLISH_SUBTITLE}\n\n'
                       f'{runtime_summary()}\n运行文件：{runtime_identity()["executable"]}\n\n'
                       f'设置：{self.config_store.path}\n日志导出名称：{LOG_NAME}\n'
                       '下载完成并通过媒体验证后生成本地 JPEG 封面。')
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.exec()

    def export_log(self):
        filename, _ = QFileDialog.getSaveFileName(self, '导出 ' + APP_NAME + ' 日志',
                                                str(Path.home() / LOG_NAME), '日志文件 (*.log);;文本文件 (*.txt)')
        if filename:
            try:
                Path(filename).write_text(Redactor().redact(self.log_view.toPlainText()), encoding='utf-8')
            except OSError:
                QMessageBox.warning(self, '无法导出日志', '请检查目标文件夹是否存在且可以写入。')

    def update_diagnostic(self, values):
        self._diagnostic = redact_data(values, self._redactor)

    def show_detection(self):
        DetectionDialog(self._diagnostic, self).exec()

    def _open_path(self, path):
        if not Path(path).exists():
            QMessageBox.information(self, '路径不存在', '该路径尚未创建或文件已被移动。开始下载时会自动创建输出目录。')
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve()))):
            QMessageBox.warning(self, '无法打开', 'Windows 未能打开此路径，请在资源管理器中手动查看。')

    def open_output_directory(self):
        self._open_path(self.output_directory.text())

    def open_result_file(self):
        if self.last_result and self.last_result['files']:
            self._open_path(self.last_result['files'][0])

    def open_result_folder(self):
        if self.last_result and self.last_result['files']:
            self._open_path(Path(self.last_result['files'][0]).parent)

    def save_settings(self):
        self.settings.update(output_directory=self.output_directory.text(), user_agent=self.user_agent.text(),
                             window_width=self.width(), window_height=self.height(),
                             advanced_expanded=self.advanced_toggle.isChecked(),
                             max_concurrent_videos=self.max_concurrent_videos.value(),
                             concurrent_fragments=self.concurrent_fragments.value(),
                             segment_max_attempts=self.segment_max_attempts.value(),
                             max_concurrent_requests=self.max_concurrent_requests.value(),
                             browser_body_limit_mib=self.browser_body_limit.value(),
                             organization=self.output_options.options(),
                             quality_preference=self.download_quality.currentData())
        self.config_store.save(self.settings)

    def _create_update_dialog(self):
        if self._update_dialog is not None and not self._update_dialog.is_closed:
            return self._update_dialog
        from core.update_manager import UpdateManager
        from ui.update_dialog import UpdateDialog
        config_path = self.config_store.path.resolve()
        if config_path != ConfigStore().path.resolve():
            # Private test/config roots keep all update state isolated from real user data.
            factory = lambda: UpdateManager(state_dir=config_path.parent / 'Updates',
                current_data_dir=config_path.parent, health_data_root=config_path.parent / 'UpdateHealth')
        else:
            factory = UpdateManager
        dialog = UpdateDialog(factory, self)
        self._update_dialog = dialog
        dialog.message.connect(self.append_log)
        dialog.shutdown_complete.connect(lambda: self._update_dialog_closed(dialog))
        return dialog

    def show_update_dialog(self):
        dialog = self._create_update_dialog()
        dialog.show()
        return dialog

    def _startup_update_check(self):
        if (self._close_after_update or self._migration_requested or not self._update_startup_allowed
                or not self.isVisible()):
            return
        config_path = self.config_store.path.resolve()
        state = (config_path.parent / 'Updates' if config_path != ConfigStore().path.resolve()
                 else Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'MediaFlow/Updates-v2')
        if (state / 'settings.json').is_file():
            self._create_update_dialog().check_on_startup()

    def _update_dialog_closed(self, dialog):
        if self._update_dialog is dialog:
            self._update_dialog = None
        dialog.deleteLater()
        if self._close_after_update:
            QTimer.singleShot(0, self.close)

    def request_data_migration(self):
        if self._migration_requested:
            return
        answer = QMessageBox.question(self, '打开数据迁移助手',
            '将暂停并保存未完成任务，安全关闭浏览器、媒体库与其他数据服务后打开迁移助手。'
            '助手关闭后重新加载当前版本数据。是否继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._migration_requested = True
        self.setEnabled(False)
        self.status_label.setText('正在安全停止当前数据服务，随后打开数据迁移助手…')
        self.close()

    def closeEvent(self, event):
        self._update_startup_allowed = False
        if self._relocation_workers:
            self._close_after_relocation = True
            self.status_label.setText('正在保存整理后的恢复路径，随后退出…')
            event.ignore()
            return
        if self.is_downloading():
            answer = QMessageBox.StandardButton.Yes if self._migration_requested else QMessageBox.question(self, '下载任务仍在运行', '是否暂停并保存未完成任务后退出？下次启动可继续。',
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._close_after_download = True
            self.batch_controller.shutdown()
            self.queue.shutdown()
            event.ignore()
            return
        if self._update_dialog is not None and not self._update_dialog.is_closed:
            self._close_after_update = True
            self.status_label.setText('正在取消更新操作并等待安全停止，随后退出…')
            self._update_dialog.shutdown()
            event.ignore()
            return
        if not self.storage_service.is_closed:
            self.status_label.setText('正在停止空间扫描或等待安全回收完成，随后退出…')
            self.storage_service.shutdown()
            event.ignore()
            return
        if not self.batch_controller.is_closed:
            self.status_label.setText('正在保存批次任务，随后退出…')
            self.batch_controller.shutdown()
            event.ignore()
            return
        if self.detector and self.detector.isRunning():
            self.status_label.setText('正在停止工具检测，随后退出…')
            self._close_after_download = True
            self.detector.cancel()
            self.detector.finished.connect(self.close)
            event.ignore()
            return
        if self._discovery_dialog:
            self._close_after_discovery = True
            self._discovery_dialog.shutdown()
            event.ignore()
            return
        if self._refresh_dialogs:
            self._close_after_discovery = True
            self._auto_refresh_timer.stop()
            self._auto_refresh_ids.clear()
            for dialog in list(self._refresh_dialogs):
                dialog.shutdown()
            event.ignore()
            return
        if self.thumbnails.busy:
            self._close_after_thumbnails = True
            self.thumbnails.cancel()
            self.status_label.setText('正在停止封面生成，随后退出…')
            event.ignore()
            return
        profile = self._webview2_profile
        if self._clearing_browser_data:
            self._close_after_browser = True
            event.ignore()
            return
        if profile is not None and not profile.host._finished_once:
            self._close_after_browser = True
            profile.host.shutdown()
            event.ignore()
            return
        if not self.library_service.is_closed:
            self.status_label.setText('正在保存下载历史，随后退出…')
            self.library_service.shutdown()
            event.ignore()
            return
        self.save_settings()
        self.queue.shutdown()
        self.set_headers({})
        self.url.clear()
        for profile in self._resolver_sessions.values():
            profile.deleteLater()
        self._resolver_sessions.clear()
        self._resolver_session_registries.clear()
        super().closeEvent(event)
