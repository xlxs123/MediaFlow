"""独立数据迁移助手；仅在主窗口及其数据库服务关闭后使用。"""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget)


_LABELS = {'settings': '界面与下载设置', 'library': '媒体库', 'history': '下载历史',
           'organization': '命名与自动分类规则', 'batches': '批量任务与合集',
           'tasks': '未完成任务恢复记录', 'browser': '浏览器登录配置'}
_COUNT_LABELS = {**_LABELS, 'tasks_recoverable': '可迁移的暂停任务', 'tasks_incompatible': '需重新分析的任务',
    'collections': '合集', 'skipped': '已跳过', 'failed': '失败项目', 'conflicts': '冲突记录',
    'missing_files': '缺失或不可访问文件'}
_STATUS_LABELS = {'completed': '迁移完成', 'failed': '迁移失败', 'cancelled': '已取消',
    'rolled_back': '已回滚', 'rollback_partial': '已回滚，保留后续修改', 'preparing': '正在备份', 'running': '正在迁移'}


def _migration_engine(target_dir, target_profile):
    from core.data_migration import MigrationEngine
    return MigrationEngine(target_dir, target_profile=target_profile)


def known_migration_paths(target_dir):
    """仅检查固定的旧版数据目录，不遍历磁盘、用户媒体或浏览器凭据。"""
    appdata = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    candidates = [appdata / 'MediaFlow' / version for version in (
        '1.7.0', '1.6.0', '1.5.0', '1.4.0', '1.3.0', '1.2.2', '1.2.1', '1.2.0')]
    candidates.extend((appdata / 'MediaFlow', appdata / 'M3U8 Downloader', appdata / '流媒体下载器'))
    target = os.path.normcase(os.path.abspath(target_dir))
    return [path for path in candidates if os.path.normcase(os.path.abspath(path)) != target]


def should_offer_migration(target_dir):
    """首次启动只做少量存在性检查；真正发现、读取和预览由后台执行。"""
    target = Path(target_dir)
    try:
        if any((target / name).exists() for name in
               ('migration-assistant.json', 'config.json', 'history.sqlite3', 'tasks.json', 'batches.sqlite3')):
            return False
        return any((directory / name).is_file() for directory in known_migration_paths(target)
                   for name in ('config.json', 'history.sqlite3', 'tasks.json', 'batches.sqlite3'))
    except OSError:
        return False


def _state(target_dir, result):
    """独立、原子的助手提示标记，不承载媒体记录或访问上下文。"""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target,
                                         prefix='migration-assistant-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({'version': 1, 'handled': True, 'result': result,
                       'updated_at': datetime.now(timezone.utc).isoformat()}, handle)
        os.replace(temporary, target / 'migration-assistant.json')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _error_text(error):
    if type(error).__name__ in ('MigrationError', 'MigrationCancelled'):
        return str(error)
    if isinstance(error, PermissionError):
        return '无法访问迁移来源、目标或备份目录，请检查权限并关闭其他 MediaFlow 后重试。'
    if isinstance(error, FileNotFoundError):
        return '所选来源文件已移动或不存在，请重新发现并生成预览。'
    if isinstance(error, OSError):
        return '迁移文件操作未完成，请检查磁盘连接、剩余空间、文件占用和访问权限后重试。'
    return '迁移操作未完成，请重新发现来源并生成预览；旧版原始数据不会被界面操作删除。'


def _issue_text(value):
    if isinstance(value, dict):
        return ' · '.join(str(value[key]) for key in ('path', 'reason', 'message', 'status') if value.get(key))
    return str(value)


def _counts_text(counts):
    if not isinstance(counts, dict):
        return '暂无计数'
    values = []
    for key, value in list(counts.items())[:30]:
        if type(value) is int:
            values.append(f'{_COUNT_LABELS.get(key, key)} {value}')
        elif isinstance(value, dict):
            numbers = [f'{name} {number}' for name, number in list(value.items())[:12]
                       if type(number) is int]
            if numbers:
                values.append(f'{_COUNT_LABELS.get(key, key)}（' + '、'.join(numbers) + '）')
    return ' · '.join(values) or '没有可迁移项目'


def _plain(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _cell(text, data=None):
    item = QTableWidgetItem(str(text))
    item.setToolTip(str(text))
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    return item


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().hide()
    return table


class _MigrationWorker(QObject):
    done = Signal(str, object)
    failed = Signal(str, str, bool)
    progress = Signal(object)
    stopped = Signal()

    def __init__(self, target_dir, target_profile):
        super().__init__()
        self.target_dir = Path(target_dir)
        self.target_profile = target_profile
        self.cancel_event = threading.Event()
        self._pending = deque()
        self._condition = threading.Condition()
        self._closing = False
        self._dismiss_result = None
        self._engine = None
        self._recovered = False
        self._recovery_reports = []
        self._thread = threading.Thread(target=self._run, name='MediaFlowMigration', daemon=True)
        self._thread.start()

    def submit(self, operation, payload):
        with self._condition:
            if self._closing:
                return False
            self.cancel_event.clear()
            self._pending.append((operation, payload))
            self._condition.notify()
        return True

    def shutdown(self, dismiss_result=None):
        with self._condition:
            self._closing = True
            if dismiss_result is not None:
                self._dismiss_result = dismiss_result
            self.cancel_event.set()
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closing)
                if not self._pending:
                    break
                operation, payload = self._pending.popleft()
            try:
                if operation == 'dismiss':
                    _state(self.target_dir, payload['result'])
                    value = True
                else:
                    if self._engine is None:
                        self._engine = _migration_engine(self.target_dir, self.target_profile)
                    if not self._recovered:
                        self.progress.emit({'stage': 'recovery', 'current': 0, 'total': 0,
                                            'message': '正在检查并恢复上次中断的迁移事务…'})
                        self._recovery_reports = self._engine.recover_interrupted()
                        self._recovered = True
                    previous = [0.0, '']

                    def report(progress):
                        now = time.monotonic()
                        stage = progress.get('stage', '')
                        if stage != previous[1] or now - previous[0] >= .05 or progress.get('current') == progress.get('total'):
                            previous[:] = [now, stage]
                            self.progress.emit(dict(progress))

                    if operation == 'discover':
                        value = {'sources': self._engine.discover(payload.get('paths')),
                                 'runs': self._engine.list_runs(), 'recovery': self._recovery_reports}
                    elif operation == 'recover':
                        value = self._recovery_reports
                    elif operation == 'preview':
                        value = self._engine.preview(payload['source'], selections=payload['selections'])
                    elif operation == 'apply':
                        value = self._engine.apply(payload['plan'], confirmed=True,
                            browser_confirmed=payload['browser_confirmed'], cancel_event=self.cancel_event,
                            progress=report)
                    elif operation == 'rollback':
                        value = self._engine.rollback(payload['id'], confirmed=True,
                            cancel_event=self.cancel_event, progress=report)
                    elif operation == 'runs':
                        value = self._engine.list_runs()
                    else:
                        raise ValueError('invalid migration operation')
                self.done.emit(operation, value)
            except Exception as error:
                self.failed.emit(operation, _error_text(error), type(error).__name__ == 'MigrationCancelled')
        if self._engine is not None:
            closer = getattr(self._engine, 'close', None)
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        if self._dismiss_result is not None:
            try:
                _state(self.target_dir, self._dismiss_result)
            except Exception as error:
                self.failed.emit('dismiss', _error_text(error), False)
        self.stopped.emit()


class MigrationDialog(QDialog):
    """单次只执行一项操作，迁移写入前始终要求完整预览和明确确认。"""
    PAGE_SIZE = 100

    def __init__(self, target_dir, target_profile=None, parent=None, source_paths=None, first_run=False):
        super().__init__(parent)
        from ui.main_window import STYLES
        self.setStyleSheet(STYLES)
        self.target_dir = Path(target_dir)
        self.target_profile = target_profile
        self.source_paths = tuple(str(path) for path in source_paths) if source_paths is not None else None
        self.first_run = bool(first_run)
        self.sources = []
        self.plan = None
        self.last_report = None
        self.worker_busy = False
        self.operation = ''
        self.is_closed = False
        self._closing = False
        self._exit_result = QDialog.DialogCode.Rejected
        self._preview_page = 0
        self._preview_revision = 0
        self._options_revision = 0
        self._operation_revision = 0
        self._worker = _MigrationWorker(self.target_dir, target_profile)
        self._worker.done.connect(self._completed)
        self._worker.failed.connect(self._failed)
        self._worker.progress.connect(self._progress)
        self._worker.stopped.connect(self._stopped)
        self.setWindowTitle('MediaFlow · 数据迁移助手')
        self.resize(1060, 800)
        self.setMinimumSize(780, 620)
        layout = QVBoxLayout(self)
        layout.addWidget(_plain('将您明确选择的旧版数据导入当前版本。旧版源码、发布文件和原始数据保留；媒体文件不会因导入历史而重新下载。'))
        target = _plain(f'当前目标数据目录：{self.target_dir}')
        target.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        target.setToolTip(str(target_profile or '浏览器配置不迁移，除非单独选择并明确确认。'))
        layout.addWidget(target)
        discovery = QHBoxLayout()
        self.discover_button = QPushButton('重新发现旧版数据')
        self.discover_button.clicked.connect(self.discover_sources)
        self.choose_source_button = QPushButton('指定旧版数据目录…')
        self.choose_source_button.clicked.connect(self._choose_source)
        discovery.addWidget(self.discover_button)
        discovery.addWidget(self.choose_source_button)
        discovery.addStretch()
        layout.addLayout(discovery)
        self.sources_table = _table(['版本', '旧版数据目录', '可用数据', '记录数量', '发现状态'])
        self.sources_table.setColumnWidth(0, 85)
        self.sources_table.setColumnWidth(1, 355)
        self.sources_table.setMaximumHeight(155)
        self.sources_table.itemSelectionChanged.connect(self._source_changed)
        layout.addWidget(self.sources_table)
        choices = QHBoxLayout()
        self.selections = {}
        for index, (key, label) in enumerate(_LABELS.items()):
            if index == 4:
                choices.addStretch()
                layout.addLayout(choices)
                choices = QHBoxLayout()
            check = QCheckBox(label)
            check.setChecked(key in ('settings', 'library', 'history'))
            check.toggled.connect(self._invalidate)
            self.selections[key] = check
            choices.addWidget(check)
        choices.addStretch()
        layout.addLayout(choices)
        policy = QHBoxLayout()
        policy.addWidget(QLabel('设置冲突策略'))
        self.settings_policy = QComboBox()
        self.settings_policy.addItem('保留新版已有设置', 'keep')
        self.settings_policy.addItem('采用所选旧版设置', 'source')
        self.settings_policy.currentIndexChanged.connect(self._invalidate)
        policy.addWidget(self.settings_policy)
        policy.addStretch()
        self.preview_button = QPushButton('生成迁移预览')
        self.preview_button.clicked.connect(self.request_preview)
        policy.addWidget(self.preview_button)
        layout.addLayout(policy)
        self.browser_confirm_check = QCheckBox('我已关闭其他 MediaFlow 及其浏览器，确认迁移所选旧版浏览器登录配置；其可用性取决于本机与账号。')
        self.browser_confirm_check.setChecked(False)
        self.browser_confirm_check.setVisible(False)
        self.browser_confirm_check.toggled.connect(self._update_actions)
        layout.addWidget(self.browser_confirm_check)
        self.tabs = QTabWidget()
        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        self.preview_summary = _plain('请选择一个来源，检查迁移范围后生成预览。未确认之前不会写入当前历史或任务数据库。')
        preview_layout.addWidget(self.preview_summary)
        self.preview_table = _table(['类别', '旧版项目 / 原始路径', '新版目标路径', '处理方式', '数量 / 原因'])
        self.preview_table.setColumnWidth(0, 115)
        self.preview_table.setColumnWidth(1, 290)
        self.preview_table.setColumnWidth(2, 290)
        preview_layout.addWidget(self.preview_table, 1)
        self.settings_preview = QPlainTextEdit()
        self.settings_preview.setReadOnly(True)
        self.settings_preview.setMaximumHeight(100)
        self.settings_preview.setPlaceholderText('这里显示本次实际采用的设置与修正后的命名、分类规则。')
        preview_layout.addWidget(self.settings_preview)
        pages = QHBoxLayout()
        self.previous_button = QPushButton('上一页')
        self.next_button = QPushButton('下一页')
        self.page_label = QLabel()
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        self.next_button.clicked.connect(lambda: self._change_page(1))
        pages.addWidget(self.previous_button)
        pages.addWidget(self.page_label, 1)
        pages.addWidget(self.next_button)
        preview_layout.addLayout(pages)
        self.tabs.addTab(preview_page, '迁移预览')
        reports = QWidget()
        reports_layout = QVBoxLayout(reports)
        self.report_summary = _plain('尚未执行迁移。完成或失败后可查看脱敏报告；需要重试时应重新生成预览。')
        reports_layout.addWidget(self.report_summary)
        runs = QHBoxLayout()
        self.runs_combo = QComboBox()
        self.runs_combo.currentIndexChanged.connect(self._run_changed)
        self.refresh_runs_button = QPushButton('刷新迁移记录')
        self.refresh_runs_button.clicked.connect(self.refresh_runs)
        self.open_report_button = QPushButton('打开最近报告')
        self.open_report_button.clicked.connect(self.open_report)
        runs.addWidget(self.runs_combo, 1)
        runs.addWidget(self.refresh_runs_button)
        runs.addWidget(self.open_report_button)
        reports_layout.addLayout(runs)
        self.report_details = QPlainTextEdit()
        self.report_details.setReadOnly(True)
        reports_layout.addWidget(self.report_details, 1)
        self.rollback_confirm_check = QCheckBox('我已检查所选迁移记录，确认将其回滚；如文件或数据已变化，需按报告处理冲突。')
        self.rollback_confirm_check.toggled.connect(self._update_actions)
        reports_layout.addWidget(self.rollback_confirm_check)
        self.rollback_button = QPushButton('回滚所选迁移')
        self.rollback_button.clicked.connect(self.request_rollback)
        reports_layout.addWidget(self.rollback_button)
        self.tabs.addTab(reports, '报告与回滚')
        layout.addWidget(self.tabs, 1)
        self.issues = QPlainTextEdit()
        self.issues.setReadOnly(True)
        self.issues.setMaximumHeight(85)
        self.issues.hide()
        layout.addWidget(self.issues)
        self.confirm_check = QCheckBox('我已核对所选来源、范围、目标路径和冲突策略，确认执行本次预览。')
        self.confirm_check.setChecked(False)
        self.confirm_check.toggled.connect(self._update_actions)
        layout.addWidget(self.confirm_check)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.status = _plain('正在准备后台发现旧版数据…')
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton('取消当前操作')
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.apply_button = QPushButton('确认迁移所选数据')
        self.apply_button.setObjectName('primary')
        self.apply_button.clicked.connect(self.apply_plan)
        self.close_button = QPushButton('暂时跳过' if first_run else '关闭助手并返回')
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._render_preview()
        self._update_actions()
        QTimer.singleShot(0, self.discover_sources)

    def _submit(self, operation, payload):
        if self.worker_busy or self._closing or self.is_closed:
            return False
        if not self._worker.submit(operation, payload):
            return False
        self.worker_busy = True
        self.operation = operation
        self._operation_revision = self._options_revision
        self.progress_bar.setRange(0, 0)
        self._set_busy(True)
        return True

    def _set_busy(self, busy):
        for widget in (self.discover_button, self.choose_source_button, self.sources_table,
                       self.settings_policy, self.preview_button, self.refresh_runs_button, self.runs_combo):
            widget.setEnabled(not busy and not self._closing)
        for check in self.selections.values():
            check.setEnabled(not busy and not self._closing)
        self.close_button.setEnabled(not self._closing)
        self.cancel_button.setEnabled(busy and self.operation in ('apply', 'rollback') and not self._closing)
        self.confirm_check.setEnabled(not busy and bool(self.plan) and not self._closing)
        self.browser_confirm_check.setEnabled(not busy and not self._closing)
        self.rollback_confirm_check.setEnabled(not busy and not self._closing)
        self._update_actions()

    def selected_source(self):
        row = self.sources_table.currentRow()
        if row < 0 or row >= len(self.sources):
            return None
        return self.sources[row]

    def selected_options(self):
        return {**{key: check.isChecked() for key, check in self.selections.items()},
                'settings_policy': self.settings_policy.currentData()}

    def _source_changed(self):
        self._invalidate()
        source = self.selected_source()
        if source:
            self._show_issues(source.get('issues', []))
            self.status.setText(f'已选择旧版 {source.get("version", "未知版本")}：{source.get("path", "")}。请生成预览。')

    def _invalidate(self, *_):
        self._options_revision += 1
        self.plan = None
        self.confirm_check.setChecked(False)
        browser = self.selections['browser'].isChecked()
        self.browser_confirm_check.setVisible(browser)
        self.browser_confirm_check.setChecked(False)
        self.confirm_check.setEnabled(False)
        self._preview_page = 0
        self._render_preview()
        self._update_actions()

    def _update_actions(self, *_):
        idle = not self.worker_busy and not self._closing and not self.is_closed
        valid = bool(self.plan and self._preview_revision == self._options_revision)
        browser_ok = not self.selections['browser'].isChecked() or self.browser_confirm_check.isChecked()
        self.apply_button.setEnabled(idle and valid and self.confirm_check.isChecked() and browser_ok)
        self.rollback_button.setEnabled(idle and bool(self.runs_combo.currentData()) and self.rollback_confirm_check.isChecked())
        self.preview_button.setEnabled(idle and self.selected_source() is not None
                                       and any(check.isChecked() for check in self.selections.values()))
        self.open_report_button.setEnabled(bool(self.last_report and self.last_report.get('report_path')))
        self.settings_policy.setEnabled(idle and self.selections['settings'].isChecked())

    def _choose_source(self):
        path = QFileDialog.getExistingDirectory(self, '选择旧版 MediaFlow 数据目录')
        if path:
            self.source_paths = (path,)
            self.discover_sources()

    def discover_sources(self):
        if self.worker_busy or self._closing or self.is_closed:
            return False
        self._invalidate()
        if self._submit('discover', {'paths': self.source_paths}):
            self.status.setText('正在后台检查旧版数据目录和迁移记录，不读取媒体内容或自动开始下载…')
            return True
        return False

    def request_preview(self):
        source = self.selected_source()
        if not source or self.worker_busy:
            return False
        self._invalidate()
        if self._submit('preview', {'source': source['path'], 'selections': self.selected_options()}):
            self.status.setText('正在后台检查来源、记录关联和目标冲突；尚未写入迁移数据…')
            return True
        return False

    def apply_plan(self):
        if (not self.plan or self.worker_busy or self._preview_revision != self._options_revision
                or not self.confirm_check.isChecked() or not self.confirm_check.isEnabled()):
            self.status.setText('尚未最终确认。请先生成当前范围的预览，再核对完整路径并勾选确认框。')
            return False
        browser = self.selections['browser'].isChecked()
        if browser and not self.browser_confirm_check.isChecked():
            self.status.setText('浏览器配置需要单独确认。请先关闭其他 MediaFlow 和浏览器，再勾选浏览器确认框。')
            return False
        plan = self.plan
        if self._submit('apply', {'plan': plan, 'browser_confirmed': bool(browser and self.browser_confirm_check.isChecked())}):
            self.status.setText('正在后台备份并迁移已确认的数据；请等待安全完成，期间不会运行下载任务。')
            return True
        return False

    def refresh_runs(self):
        return self._submit('runs', {})

    def _run_changed(self):
        self.rollback_confirm_check.setChecked(False)
        self._update_actions()

    def request_rollback(self):
        run_id = self.runs_combo.currentData()
        if not run_id or not self.rollback_confirm_check.isChecked() or self.worker_busy:
            self.status.setText('请先选择迁移记录并明确勾选回滚确认。')
            return False
        self.plan = None
        self.confirm_check.setChecked(False)
        if self._submit('rollback', {'id': run_id}):
            self.status.setText('正在后台检查并回滚所选迁移记录；如目标已变化，将保留冲突并写入报告。')
            return True
        return False

    def cancel_operation(self):
        if self.worker_busy:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status.setText('已请求取消。正在等待当前文件或事务安全结束，请勿强制关闭软件。')

    def _show_issues(self, issues):
        values = list(issues or [])
        self.issues.setPlainText('\n'.join(_issue_text(item) for item in values[:100])
            + (f'\n另有 {len(values) - 100} 项，请查看迁移报告。' if len(values) > 100 else ''))
        self.issues.setVisible(bool(values))

    @Slot(str, object)
    def _completed(self, operation, value):
        self.worker_busy = False
        self.operation = ''
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        if operation == 'dismiss':
            self._worker.shutdown()
            return
        if operation == 'discover':
            self.sources = list(value.get('sources', []))
            self._populate_sources()
            self._populate_runs(value.get('runs', []))
            if value.get('recovery'):
                self.last_report = value['recovery'][-1]
                self._render_report(self.last_report)
            self.status.setText(f'找到 {len(self.sources)} 个旧版数据来源。请选择一个来源并生成预览。' if self.sources
                                else '没有找到可迁移的旧版数据。可手动指定旧版数据目录，也可关闭助手继续使用。')
        elif operation == 'preview':
            if self._operation_revision == self._options_revision:
                self.plan = value
                self._preview_revision = self._options_revision
                self.confirm_check.setChecked(False)
                self.browser_confirm_check.setChecked(False)
                self.confirm_check.setEnabled(True)
                self._render_preview()
                self._show_issues(value.get('issues', []))
                self.tabs.setCurrentIndex(0)
                self.status.setText('预览已生成。请核对来源、目标路径、范围和冲突处理，再明确确认。')
            else:
                self.status.setText('迁移选项已经变化，请重新生成预览。')
        elif operation in ('apply', 'rollback'):
            self.plan = None
            self.confirm_check.setChecked(False)
            self.browser_confirm_check.setChecked(False)
            self.rollback_confirm_check.setChecked(False)
            self.last_report = value
            self._render_report(value)
            self.tabs.setCurrentIndex(1)
            if not self._closing:
                QTimer.singleShot(0, self.refresh_runs)
        elif operation == 'runs':
            self._populate_runs(value)
        self._set_busy(False)
        if self._closing:
            self._worker.shutdown()

    @Slot(str, str, bool)
    def _failed(self, operation, message, cancelled):
        self.worker_busy = False
        self.operation = ''
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        if operation != 'runs':
            self.plan = None
            self.confirm_check.setChecked(False)
            self.confirm_check.setEnabled(False)
        self._show_issues([message])
        self.status.setText(('操作已安全取消。' if cancelled else '操作未完成。') + message
                            + (' 可重新发现来源并生成预览后重试。' if operation != 'dismiss' else ''))
        self._set_busy(False)
        if self._closing or operation == 'dismiss':
            self._worker.shutdown()

    def _populate_sources(self):
        blocker = QSignalBlocker(self.sources_table)
        self.sources_table.setRowCount(len(self.sources))
        self.sources_table.clearSelection()
        self.sources_table.setCurrentCell(-1, -1)
        for row, source in enumerate(self.sources):
            types = source.get('types') or []
            if isinstance(types, dict):
                types = [key for key, value in types.items() if value]
            values = (source.get('version') or '未知版本', source.get('path', ''),
                      '、'.join(_LABELS.get(key, str(key)) for key in types),
                      _counts_text(source.get('counts')), '有提示，需检查' if source.get('issues') else '可预览')
            for column, text in enumerate(values):
                self.sources_table.setItem(row, column, _cell(text, source['path'] if column == 0 else None))
        del blocker
        if len(self.sources) == 1:
            self.sources_table.selectRow(0)

    def _populate_runs(self, runs):
        blocker = QSignalBlocker(self.runs_combo)
        self.runs_combo.clear()
        self.runs_combo.addItem('选择一条迁移记录', '')
        for item in list(runs or [])[:2000]:
            if isinstance(item, dict) and item.get('id'):
                self.runs_combo.addItem(f'{_STATUS_LABELS.get(item.get("status"), item.get("status", "未知状态"))} · {item["id"]}', str(item['id']))
            elif isinstance(item, str):
                self.runs_combo.addItem(item, item)
        if self.last_report and self.last_report.get('id'):
            index = self.runs_combo.findData(str(self.last_report['id']))
            if index >= 0:
                self.runs_combo.setCurrentIndex(index)
        del blocker
        self._run_changed()

    def _render_preview(self):
        plan = self.plan or {}
        rows = plan.get('items') or []
        if isinstance(rows, dict):
            choice = rows
            rows = []
            filenames = {'settings': 'config.json', 'organization': 'config.json',
                         'library': 'history.sqlite3', 'history': 'history.sqlite3',
                         'batches': 'batches.sqlite3', 'tasks': 'tasks.json'}
            source = self.selected_source() or {}
            for key in _LABELS:
                filename = filenames.get(key)
                old = str(Path(str(plan.get('source', ''))) / filename) if filename else source.get('profile_path', '')
                new = str(Path(str(plan.get('target', self.target_dir))) / filename) if filename else str(self.target_profile or '')
                count = (plan.get('counts') or {}).get(key)
                if key == 'tasks':
                    count = (plan.get('counts') or {}).get('tasks_recoverable', 0)
                rows.append({'kind': key, 'source': old, 'target': new,
                             'action': '按当前预览导入' if choice.get(key) else '未选中，不导入',
                             'count': count if count is not None else ''})
        pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._preview_page = max(0, min(self._preview_page, pages - 1))
        start = self._preview_page * self.PAGE_SIZE
        visible = rows[start:start + self.PAGE_SIZE]
        self.preview_table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            if isinstance(item, dict):
                kind = item.get('type') or item.get('kind') or item.get('category', '')
                values = (_LABELS.get(kind, kind), item.get('source_path') or item.get('source') or item.get('name', ''),
                          item.get('target_path') or item.get('target', ''), item.get('action') or item.get('status', ''),
                          item.get('reason') or item.get('count', ''))
            else:
                values = ('', str(item), '', '', '')
            for column, text in enumerate(values):
                self.preview_table.setItem(row, column, _cell(text))
        self.page_label.setText(f'第 {self._preview_page + 1} / {pages} 页 · 共 {len(rows)} 项 · 每页最多 {self.PAGE_SIZE} 项')
        self.previous_button.setEnabled(self._preview_page > 0)
        self.next_button.setEnabled(self._preview_page + 1 < pages)
        if plan:
            self.settings_preview.setPlainText(json.dumps(plan.get('settings_preview', {}), ensure_ascii=False, indent=2))
            self.preview_summary.setText(f'来源：{plan.get("source", "")}（{plan.get("version", "未知版本")}）\n'
                f'目标：{plan.get("target", self.target_dir)}\n'
                f'{_counts_text(plan.get("counts"))} · 待检查文件 {plan.get("files_to_check", 0)} · '
                f'预计迁移数据 {int(plan.get("estimated_bytes", 0) or 0):,} 字节。媒体文件不会因迁移历史而重复下载。')
        else:
            self.settings_preview.clear()
            self.preview_summary.setText('请选择一个来源，检查迁移范围后生成预览。未确认之前不会写入当前历史或任务数据库。')

    def _change_page(self, difference):
        self._preview_page += difference
        self._render_preview()

    def _render_report(self, report):
        status = _STATUS_LABELS.get(report.get('status'), report.get('status', '未知状态'))
        self.report_summary.setText(f'本次状态：{status} · {_counts_text(report.get("counts"))}\n报告：{report.get("report_path", "暂无报告路径")}')
        issues = report.get('issues', [])
        self.report_details.setPlainText('\n'.join(_issue_text(item) for item in issues[:500])
            or '报告未列出异常。可关闭助手重新加载数据，或选中迁移记录查看回滚操作。')
        self._show_issues(issues)
        self.status.setText('迁移操作结束。请查看报告；数据将在关闭助手后重新加载。')

    @Slot(object)
    def _progress(self, progress):
        total = int(progress.get('total') or 0)
        current = int(progress.get('current') or 0)
        if total > 0:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, max(0, current * 1000 // total)))
        else:
            self.progress_bar.setRange(0, 0)
        self.status.setText(str(progress.get('message') or '正在后台处理迁移数据…')
                            + (f'（{current}/{total}）' if total else ''))

    def open_report(self):
        path = self.last_report.get('report_path') if self.last_report else ''
        if path and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.status.setText('无法打开报告，请从上方完整路径检查文件是否存在及访问权限。')

    def _request_close(self, result):
        if self.is_closed:
            QDialog.done(self, result)
            return
        if self._closing:
            return
        self._closing = True
        self._exit_result = result
        self._worker.shutdown('completed' if self.last_report else 'skipped')
        if self.worker_busy:
            self._set_busy(True)
            self.status.setText('正在等待当前操作安全结束，随后关闭助手。请勿强制结束进程。')
        else:
            self._set_busy(True)

    def reject(self):
        self._request_close(QDialog.DialogCode.Rejected)

    def accept(self):
        self._request_close(QDialog.DialogCode.Accepted)

    def done(self, result):
        self._request_close(result)

    @Slot()
    def _stopped(self):
        self.is_closed = True
        self.worker_busy = False
        self.operation = ''
        QDialog.done(self, self._exit_result)

    def closeEvent(self, event):
        if self.is_closed:
            super().closeEvent(event)
        else:
            event.ignore()
            self.reject()


class MigrationRecoveryDialog(QDialog):
    """有本地迁移事务时，先在后台恢复，再允许主窗口打开数据库。"""
    def __init__(self, target_dir, target_profile=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('MediaFlow · 检查迁移恢复')
        self.resize(640, 320)
        self.passed = False
        self.is_closed = False
        self._closing = False
        self.reports = []
        layout = QVBoxLayout(self)
        self.status = _plain('正在后台检查上次迁移事务；检查完成前不会打开媒体库或恢复下载任务。')
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        layout.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.hide()
        layout.addWidget(self.details, 1)
        actions = QHBoxLayout()
        self.retry_button = QPushButton('重新检查')
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self._retry)
        self.continue_button = QPushButton('已核对报告，继续启动')
        self.continue_button.hide()
        self.continue_button.clicked.connect(self._continue)
        self.close_button = QPushButton('退出软件')
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.retry_button)
        actions.addStretch()
        actions.addWidget(self.continue_button)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self._worker = _MigrationWorker(target_dir, target_profile)
        self._worker.done.connect(self._done)
        self._worker.failed.connect(self._failed)
        self._worker.stopped.connect(self._stopped)
        QTimer.singleShot(0, self._retry)

    def _retry(self):
        if self._closing or self.is_closed:
            return
        self.retry_button.setEnabled(False)
        self.details.hide()
        self.progress.setRange(0, 0)
        self._worker.submit('recover', {})

    @Slot(str, object)
    def _done(self, operation, value):
        if operation != 'recover' or self._closing:
            return
        self.reports = list(value or [])
        attention = [report for report in self.reports if report.get('status') not in ('rolled_back', 'completed')]
        if attention:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.status.setText('中断事务已完成恢复检查，部分已变化的数据已保留。请核对本地报告后再继续启动。')
            self.details.setPlainText('\n'.join(
                str(report.get('report_path', '')) + '\n' + '\n'.join(_issue_text(item) for item in report.get('issues', [])[:30])
                for report in attention[:30]))
            self.details.show()
            self.continue_button.show()
        else:
            self._continue()

    @Slot(str, str, bool)
    def _failed(self, _operation, message, _cancelled):
        if self._closing:
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText('迁移恢复检查未完成，当前数据服务尚未打开。' + message)
        self.retry_button.setEnabled(True)

    def _continue(self):
        if not self._closing:
            self.passed = True
            self._closing = True
            self._worker.shutdown()

    def reject(self):
        if self.is_closed:
            QDialog.reject(self)
        elif not self._closing:
            self._closing = True
            self.passed = False
            self.close_button.setEnabled(False)
            self.status.setText('正在等待迁移恢复检查安全结束，随后退出…')
            self._worker.shutdown()

    @Slot()
    def _stopped(self):
        self.is_closed = True
        QDialog.done(self, QDialog.DialogCode.Accepted if self.passed else QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        if self.is_closed:
            super().closeEvent(event)
        else:
            event.ignore()
            self.reject()
