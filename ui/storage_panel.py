"""媒体库重复检测与空间分析；默认只分析，清理必须预览并明确确认。"""
import ntpath
import os
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)


_ROLE = Qt.ItemDataRole.UserRole
_TYPES = {'video': '视频', 'audio': '音频', 'subtitle': '字幕', 'other': '其他',
          'unknown': '其他'}
_STATES = {'exists': '存在', 'missing': '已移动或丢失', 'inaccessible': '不可访问',
           'changed': '扫描期间变化', 'skipped': '已跳过'}


def _bytes(value):
    number = max(0, int(value or 0))
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if number < 1024 or unit == 'TiB':
            return f'{number:.2f} {unit}' if unit != 'B' else f'{number:.0f} B'
        number /= 1024


def _key(path):
    return ntpath.normcase(ntpath.normpath(str(path)))


def _plain(label):
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _cell(value, data=None):
    item = QTableWidgetItem(str(value))
    item.setToolTip(str(value))
    if data is not None:
        item.setData(_ROLE, data)
    return item


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    table.verticalHeader().hide()
    return table


class _CleanupDialog(QDialog):
    PAGE_SIZE = 100

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.page = 0
        self.applying = False
        self.setWindowTitle('确认移入系统回收站')
        self.resize(980, 560)
        layout = QVBoxLayout(self)
        self.summary = _plain(QLabel(
            f'即将移入系统回收站：{plan.get("count", 0)} 个文件 · '
            f'逻辑文件容量 {_bytes(plan.get("bytes"))}。请逐项核对完整路径。\n'
            '每组至少保留一份已验证副本；不会永久删除。回收站仍占用磁盘，实际释放空间需要您自行清空回收站。'))
        layout.addWidget(self.summary)
        self.table = _table(['文件名', '完整路径', '文件大小', '关联媒体库记录'])
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 480)
        layout.addWidget(self.table, 1)
        paging = QHBoxLayout()
        self.previous_button = QPushButton('上一页')
        self.next_button = QPushButton('下一页')
        self.page_label = QLabel()
        self.previous_button.clicked.connect(lambda: self._page(-1))
        self.next_button.clicked.connect(lambda: self._page(1))
        paging.addWidget(self.previous_button)
        paging.addWidget(self.page_label, 1)
        paging.addWidget(self.next_button)
        layout.addLayout(paging)
        self.errors = QPlainTextEdit()
        self.errors.setReadOnly(True)
        self.errors.setMaximumHeight(85)
        self.errors.setPlainText('\n'.join(str(value) for value in plan.get('errors', [])))
        self.errors.setVisible(bool(plan.get('errors')))
        layout.addWidget(self.errors)
        self.paths_preview = QPlainTextEdit()
        self.paths_preview.setReadOnly(True)
        self.paths_preview.setMinimumHeight(65)
        self.paths_preview.setMaximumHeight(105)
        self.paths_preview.setToolTip('完整路径可以选择和复制，不使用省略号。')
        layout.addWidget(self.paths_preview)
        self.confirm_check = QCheckBox('我已核对文件名、完整路径和保留副本，确认仅将以上文件移入系统回收站。')
        self.confirm_check.setChecked(False)
        self.confirm_check.setEnabled(bool(plan.get('token') and plan.get('count') and not plan.get('errors')))
        layout.addWidget(self.confirm_check)
        buttons = QHBoxLayout()
        self.close_button = QPushButton('取消')
        self.close_button.clicked.connect(self.reject)
        self.apply_button = QPushButton('确认移入回收站')
        self.apply_button.setEnabled(False)
        self.confirm_check.toggled.connect(lambda checked: self.apply_button.setEnabled(
            checked and self.confirm_check.isEnabled() and not self.applying))
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        self._render()

    def _page(self, difference):
        pages = max(1, (len(self.plan.get('rows', [])) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(pages - 1, self.page + difference))
        self._render()

    def _render(self):
        rows = self.plan.get('rows', [])
        start = self.page * self.PAGE_SIZE
        visible = rows[start:start + self.PAGE_SIZE]
        self.paths_preview.setPlainText('\n'.join(str(item.get('path', '')) for item in visible))
        self.table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            values = (item.get('name') or ntpath.basename(item.get('path', '')), item.get('path', ''),
                      _bytes(item.get('size')), len(item.get('entry_ids', [])))
            for column, value in enumerate(values):
                self.table.setItem(row, column, _cell(value))
        total_pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_label.setText(f'第 {self.page + 1} / {total_pages} 页 · 共 {len(rows)} 个完整路径')
        self.previous_button.setEnabled(self.page > 0 and not self.applying)
        self.next_button.setEnabled(self.page + 1 < total_pages and not self.applying)

    def set_applying(self, applying):
        self.applying = applying
        self.close_button.setEnabled(not applying)
        self.confirm_check.setEnabled(False)
        self.apply_button.setEnabled(False)
        if applying:
            self.summary.setText('正在后台重新检查文件完整哈希、保留副本和下载任务状态，并逐项移入回收站。请等待当前操作安全结束。')
        self._render()

    def reject(self):
        if not self.applying:
            super().reject()

    def closeEvent(self, event):
        if self.applying:
            event.ignore()
        else:
            super().closeEvent(event)


class StoragePanel(QWidget):
    FILE_PAGE_SIZE = 100
    GROUP_PAGE_SIZE = 30
    GROUP_FILE_PAGE_SIZE = 50
    SUMMARY_PAGE_SIZE = 100

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.result = None
        self.last_preview = None
        self.last_cleanup = None
        self.confirmation_dialog = None
        self._selected_paths = set()
        self._groups = []
        self._group_page = 0
        self._group_file_pages = {}
        self._files_page = 0
        self._summary_page = 0
        self._scope_dirty = False
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._refresh_files)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.content_scroll.setWidget(content)
        outer.addWidget(self.content_scroll)
        layout.addWidget(_plain(QLabel('默认只分析媒体库已知文件和已登记字幕。完整 SHA-256 一致才确认重复；扫描和辅助保留策略不会删除文件。')))
        scope = QHBoxLayout()
        scope.addWidget(QLabel('扫描范围：媒体库已知文件'))
        scope.addStretch()
        self.add_directory_button = QPushButton('添加额外扫描目录…')
        self.remove_directory_button = QPushButton('移除所选目录')
        self.add_directory_button.clicked.connect(self._browse_directory)
        self.remove_directory_button.clicked.connect(self._remove_directory)
        scope.addWidget(self.add_directory_button)
        scope.addWidget(self.remove_directory_button)
        layout.addLayout(scope)
        self.extra_directories = QListWidget()
        self.extra_directories.setMaximumHeight(65)
        self.extra_directories.hide()
        self.extra_directories.setToolTip('默认没有额外目录；只扫描您明确添加的目录。不会自动扫描整个系统盘、浏览器登录配置或凭据目录。')
        layout.addWidget(self.extra_directories)
        controls = QHBoxLayout()
        self.scan_button = QPushButton('开始扫描')
        self.cancel_button = QPushButton('取消扫描')
        self.cancel_button.setEnabled(False)
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button.clicked.connect(self.service.cancel)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label = _plain(QLabel('尚未扫描'))
        controls.addWidget(self.scan_button)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.progress_bar, 1)
        layout.addLayout(controls)
        layout.addWidget(self.progress_label)
        self.tabs = QTabWidget()
        self._build_duplicates()
        self._build_space()
        layout.addWidget(self.tabs, 1)
        self.status = _plain(QLabel('没有扫描结果。请先完成扫描，再检查重复组与空间分布。'))
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.issues = QPlainTextEdit()
        self.issues.setReadOnly(True)
        self.issues.setMaximumHeight(85)
        self.issues.hide()
        layout.addWidget(self.issues)
        service.progress.connect(self._progress)
        service.scan_ready.connect(self._scan_ready)
        service.preview_ready.connect(self._preview_ready)
        service.cleanup_ready.connect(self._cleanup_ready)
        service.message.connect(self._message)
        service.busy_changed.connect(self._busy_changed)
        if service.result:
            self._scan_ready(service.result)

    def _build_duplicates(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.duplicate_summary = _plain(QLabel('未发现已确认重复组。勾选代表“待回收”，默认全部保留。'))
        layout.addWidget(self.duplicate_summary)
        strategy = QHBoxLayout()
        self.keep_strategy = QComboBox()
        self.keep_strategy.addItem('优先保留修改时间较早的文件', 'oldest')
        self.keep_strategy.addItem('优先保留路径较短的文件', 'shortest')
        self.keep_strategy.addItem('优先保留指定目录中的文件', 'directory')
        self.keep_directory = QLineEdit()
        self.keep_directory.setPlaceholderText('指定优先保留目录（只辅助预选）')
        self.keep_directory.setEnabled(False)
        self.keep_strategy.currentIndexChanged.connect(lambda: self.keep_directory.setEnabled(
            self.keep_strategy.currentData() == 'directory' and not self.service.busy))
        self.apply_strategy_button = QPushButton('仅预选待回收副本')
        self.apply_strategy_button.clicked.connect(self._apply_strategy)
        self.clear_selection_button = QPushButton('取消全部勾选')
        self.clear_selection_button.clicked.connect(lambda: self.set_cleanup_paths([]))
        strategy.addWidget(self.keep_strategy)
        strategy.addWidget(self.keep_directory, 1)
        strategy.addWidget(self.apply_strategy_button)
        strategy.addWidget(self.clear_selection_button)
        layout.addLayout(strategy)
        self.duplicates_tree = QTreeWidget()
        self.duplicates_tree.setMinimumHeight(180)
        self.duplicates_tree.setColumnCount(7)
        self.duplicates_tree.setHeaderLabels(['重复组 / 文件名（勾选待回收）', '完整路径', '大小', '格式', '修改时间', '磁盘', '媒体库关联'])
        self.duplicates_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.duplicates_tree.setColumnWidth(0, 300)
        self.duplicates_tree.setColumnWidth(1, 380)
        self.duplicates_tree.itemChanged.connect(self._selection_changed)
        self.duplicates_tree.itemDoubleClicked.connect(self._group_page_clicked)
        layout.addWidget(self.duplicates_tree, 1)
        paging = QHBoxLayout()
        self.groups_previous = QPushButton('上一组页')
        self.groups_next = QPushButton('下一组页')
        self.groups_page_label = QLabel('0 组')
        self.groups_previous.clicked.connect(lambda: self._change_group_page(-1))
        self.groups_next.clicked.connect(lambda: self._change_group_page(1))
        paging.addWidget(self.groups_previous)
        paging.addWidget(self.groups_page_label, 1)
        paging.addWidget(self.groups_next)
        layout.addLayout(paging)
        actions = QHBoxLayout()
        self.keep_selected_button = QPushButton('保留所选文件，预选本组其余副本')
        self.keep_selected_button.clicked.connect(self._keep_selected)
        self.open_folder_button = QPushButton('打开所选文件所在目录')
        self.open_folder_button.clicked.connect(self._open_folder)
        self.cleanup_button = QPushButton('预览所选文件的回收操作…')
        self.cleanup_button.setEnabled(False)
        self.cleanup_button.clicked.connect(self.request_cleanup_preview)
        actions.addWidget(self.keep_selected_button)
        actions.addWidget(self.open_folder_button)
        actions.addStretch()
        actions.addWidget(self.cleanup_button)
        layout.addLayout(actions)
        self.tabs.addTab(page, '重复检测')

    def _build_space(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.space_summary = _plain(QLabel('容量来自实际存在且可访问的文件；同一路径和硬链接只计一份，缺失文件不计容量。'))
        layout.addWidget(self.space_summary)
        summary_controls = QHBoxLayout()
        self.summary_dimension = QComboBox()
        for label, key in (('按媒体类型', 'by_type'), ('按来源网站', 'by_site'), ('按下载日期', 'by_date'), ('按文件夹', 'by_folder')):
            self.summary_dimension.addItem(label, key)
        self.summary_dimension.currentIndexChanged.connect(self._summary_changed)
        self.summary_previous = QPushButton('上一统计页')
        self.summary_next = QPushButton('下一统计页')
        self.summary_page_label = QLabel()
        self.summary_previous.clicked.connect(lambda: self._change_summary_page(-1))
        self.summary_next.clicked.connect(lambda: self._change_summary_page(1))
        for widget in (self.summary_dimension, self.summary_previous, self.summary_page_label, self.summary_next):
            summary_controls.addWidget(widget)
        summary_controls.addStretch()
        layout.addLayout(summary_controls)
        self.summary_table = _table(['分类', '实际文件数', '实际容量'])
        self.summary_table.setMaximumHeight(145)
        self.summary_table.setColumnWidth(0, 400)
        layout.addWidget(self.summary_table)
        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('搜索文件名或完整路径')
        self.search_edit.setClearButtonEnabled(True)
        self.type_filter = QComboBox()
        self.site_filter = QComboBox()
        self.date_filter = QComboBox()
        self.folder_filter = QComboBox()
        self.minimum_size = QDoubleSpinBox()
        self.minimum_size.setRange(0, 1_000_000_000)
        self.minimum_size.setSuffix(' MiB 以上')
        self.minimum_size.setDecimals(2)
        self.sort_combo = QComboBox()
        for label, key in (('大文件优先', 'size_desc'), ('小文件优先', 'size_asc'), ('文件名', 'name'), ('所在目录', 'folder')):
            self.sort_combo.addItem(label, key)
        self.clear_filters_button = QPushButton('清空筛选')
        self.clear_filters_button.clicked.connect(self.clear_filters)
        filters.addWidget(self.search_edit, 2)
        for widget in (self.type_filter, self.site_filter, self.date_filter, self.folder_filter,
                       self.minimum_size, self.sort_combo, self.clear_filters_button):
            filters.addWidget(widget)
        layout.addLayout(filters)
        for combo in (self.type_filter, self.site_filter, self.date_filter, self.folder_filter, self.sort_combo):
            combo.currentIndexChanged.connect(self._schedule_filter)
            combo.setMaximumWidth(180)
        self.minimum_size.valueChanged.connect(self._schedule_filter)
        self.search_edit.textChanged.connect(self._schedule_filter)
        self.files_table = _table(['文件名', '完整路径', '实际大小', '类型', '来源网站', '下载日期', '状态 / 保护原因'])
        self.files_table.setMinimumHeight(180)
        self.files_table.setColumnWidth(0, 190)
        self.files_table.setColumnWidth(1, 380)
        self.files_table.itemDoubleClicked.connect(lambda *_: self._open_folder())
        layout.addWidget(self.files_table, 1)
        paging = QHBoxLayout()
        self.files_previous = QPushButton('上一页')
        self.files_next = QPushButton('下一页')
        self.files_page_label = QLabel('没有文件')
        self.files_previous.clicked.connect(lambda: self._change_file_page(-1))
        self.files_next.clicked.connect(lambda: self._change_file_page(1))
        self.space_open_folder_button = QPushButton('打开所选文件所在目录')
        self.space_open_folder_button.clicked.connect(self._open_folder)
        paging.addWidget(self.files_previous)
        paging.addWidget(self.files_page_label, 1)
        paging.addWidget(self.files_next)
        paging.addWidget(self.space_open_folder_button)
        layout.addLayout(paging)
        self.tabs.addTab(page, '空间分析')
        self._populate_filters()

    def extra_dirs(self):
        return tuple(self.extra_directories.item(index).text() for index in range(self.extra_directories.count()))

    def add_directory(self, path):
        text = str(path).strip()
        if text and _key(text) not in {_key(value) for value in self.extra_dirs()}:
            self.extra_directories.addItem(text)
            self.extra_directories.item(self.extra_directories.count() - 1).setToolTip(text)
            self._scope_changed()

    def _browse_directory(self):
        path = QFileDialog.getExistingDirectory(self, '明确选择额外扫描目录')
        if path:
            self.add_directory(path)

    def _remove_directory(self):
        row = self.extra_directories.currentRow()
        if row >= 0:
            self.extra_directories.takeItem(row)
            self._scope_changed()

    def _scope_changed(self):
        self.extra_directories.setVisible(self.extra_directories.count() > 0)
        self._scope_dirty = True
        self.set_cleanup_paths([])
        self.status.setText('扫描范围已修改，请重新扫描。未添加的目录不会扫描。')
        self._update_cleanup_button()

    def start_scan(self):
        if self.service.start_scan(self.extra_dirs()):
            self.last_preview = None
            self._scope_dirty = False
            self.set_cleanup_paths([])
            self.progress_label.setText('正在后台枚举媒体库文件；尚未完成枚举，不显示估算百分比。')

    def _busy_changed(self, busy):
        for widget in (self.scan_button, self.add_directory_button, self.remove_directory_button,
                       self.extra_directories, self.apply_strategy_button, self.clear_selection_button,
                       self.keep_selected_button, self.keep_strategy):
            widget.setEnabled(not busy)
        self.keep_directory.setEnabled(not busy and self.keep_strategy.currentData() == 'directory')
        self.duplicates_tree.setEnabled(not busy)
        self.cancel_button.setEnabled(busy and self.service.operation == 'scan')
        self._update_cleanup_button()

    def _progress(self, value):
        phase = value.get('phase')
        done = int(value.get('bytes_done') or 0)
        total = int(value.get('bytes_total') or 0)
        if phase == 'hash' and total:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(min(1000, int(done * 1000 / total)))
            text = f'完整哈希：已核对 {_bytes(done)} / {_bytes(total)}（有效缓存另计）'
        elif phase == 'done':
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100 if not value.get('cancelled') else 0)
            text = value.get('message') or '扫描处理已结束。'
        else:
            self.progress_bar.setRange(0, 0)
            text = f'正在枚举：已检查 {value.get("files_done", 0)} 个文件'
        if value.get('message') and phase != 'done':
            text += ' · ' + str(value['message'])
        self.progress_label.setText(text)
        self.progress_label.setToolTip(str(value.get('path', '')))

    def _scan_ready(self, result):
        self._filter_timer.stop()
        self.result = result or {}
        self._groups = list(self.result.get('groups', []))
        self._group_page = self._files_page = self._summary_page = 0
        self._group_file_pages.clear()
        self._selected_paths.clear()
        stats = self.result.get('stats', {})
        complete = self.result.get('complete') and not self.result.get('cancelled')
        self.scan_button.setText('重新扫描')
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100 if complete else 0)
        self.progress_label.setText(
            f'扫描完成 · 完整哈希 {self.result.get("hashed_files", 0)} 个文件 · '
            f'有效缓存 {self.result.get("cache_hits", 0)} 个' if complete else
            '扫描已取消或未完成，当前结果不能用于清理。')
        self.duplicate_summary.setText(
            f'完整 SHA-256 确认 {len(self._groups)} 组 / {stats.get("duplicate_files", 0)} 个重复文件；'
            f'理论冗余容量 {_bytes(stats.get("reclaimable_bytes"))}。默认全部保留；勾选表示待回收。')
        counts = ' · '.join(f'{_TYPES.get(key, key)} {value.get("count", 0)} 个 / {_bytes(value.get("bytes"))}'
                            for key, value in (stats.get('by_type') or {}).items())
        self.space_summary.setText(
            f'实际可访问文件 {stats.get("total_files", 0)} 个 · 容量 {_bytes(stats.get("total_bytes"))} · '
            f'缺失 {stats.get("missing_files", 0)} · 不可访问 {stats.get("inaccessible_files", 0)}\n'
            + (counts + '\n' if counts else '') + '容量基于本次文件系统检查；每个物理文件只计一份。重复容量是保留一份后的理论值。')
        self._render_groups()
        self._populate_filters()
        self._render_summary()
        self._refresh_files()
        issues = self.result.get('issues', [])
        self.issues.setPlainText('\n'.join(f'{item.get("path", "")}：{item.get("reason", "")}' for item in issues[:100])
                                 + (f'\n另有 {len(issues) - 100} 项；请缩小范围重新扫描。' if len(issues) > 100 else ''))
        self.issues.setVisible(bool(issues))
        self._update_selection_status()

    def _render_groups(self):
        blocker = QSignalBlocker(self.duplicates_tree)
        self.duplicates_tree.clear()
        pages = max(1, (len(self._groups) + self.GROUP_PAGE_SIZE - 1) // self.GROUP_PAGE_SIZE)
        self._group_page = min(self._group_page, pages - 1)
        start = self._group_page * self.GROUP_PAGE_SIZE
        for group in self._groups[start:start + self.GROUP_PAGE_SIZE]:
            files = group.get('files', [])
            root = QTreeWidgetItem([f'{len(files)} 份相同内容 · 理论冗余 {_bytes(group.get("reclaimable_bytes"))}', '',
                                    _bytes(group.get('size')), '', '', '', ''])
            root.setData(0, _ROLE, {'kind': 'group', 'id': group.get('id')})
            root.setToolTip(0, '完整 SHA-256：' + str(group.get('sha256', group.get('id', ''))))
            self.duplicates_tree.addTopLevelItem(root)
            file_pages = max(1, (len(files) + self.GROUP_FILE_PAGE_SIZE - 1) // self.GROUP_FILE_PAGE_SIZE)
            page = min(self._group_file_pages.get(group.get('id'), 0), file_pages - 1)
            self._group_file_pages[group.get('id')] = page
            if page:
                previous = QTreeWidgetItem(['双击：本组上一文件页'])
                previous.setData(0, _ROLE, {'kind': 'page', 'id': group.get('id'), 'delta': -1})
                root.addChild(previous)
            offset = page * self.GROUP_FILE_PAGE_SIZE
            for item in files[offset:offset + self.GROUP_FILE_PAGE_SIZE]:
                path = item.get('path', '')
                leaf = QTreeWidgetItem([item.get('name') or ntpath.basename(path), path, _bytes(item.get('size')),
                                       str(item.get('format', '')), str(item.get('mtime', '')), str(item.get('disk', '')),
                                       f'{len(item.get("entry_ids", []))} 条记录' if item.get('entry_ids') else '库外文件'])
                leaf.setData(0, _ROLE, {'kind': 'file', 'path': path, 'group': group.get('id')})
                leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                leaf.setCheckState(0, Qt.CheckState.Checked if _key(path) in self._selected_paths else Qt.CheckState.Unchecked)
                for column in range(leaf.columnCount()):
                    leaf.setToolTip(column, path + '\n' + f'关联记录：{item.get("entry_ids", [])}'
                                    + ('\n别名：' + '\n'.join(item.get('aliases', [])) if item.get('aliases') else ''))
                root.addChild(leaf)
            if page + 1 < file_pages:
                next_page = QTreeWidgetItem([f'双击：本组下一文件页（当前 {page + 1}/{file_pages}）'])
                next_page.setData(0, _ROLE, {'kind': 'page', 'id': group.get('id'), 'delta': 1})
                root.addChild(next_page)
            root.setExpanded(True)
        self.groups_page_label.setText(f'第 {self._group_page + 1} / {pages} 页 · 共 {len(self._groups)} 组；每组每页最多 {self.GROUP_FILE_PAGE_SIZE} 个文件')
        self.groups_previous.setEnabled(self._group_page > 0)
        self.groups_next.setEnabled(self._group_page + 1 < pages)
        del blocker

    def _group_page_clicked(self, item, _column):
        data = item.data(0, _ROLE) or {}
        if data.get('kind') == 'page':
            self._group_file_pages[data['id']] = max(0, self._group_file_pages.get(data['id'], 0) + data['delta'])
            self._render_groups()

    def _change_group_page(self, difference):
        pages = max(1, (len(self._groups) + self.GROUP_PAGE_SIZE - 1) // self.GROUP_PAGE_SIZE)
        self._group_page = max(0, min(pages - 1, self._group_page + difference))
        self._render_groups()

    def _selection_changed(self, item, column):
        data = item.data(0, _ROLE) or {}
        if column != 0 or data.get('kind') != 'file':
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._selected_paths.add(_key(data['path']))
        else:
            self._selected_paths.discard(_key(data['path']))
        self.last_preview = None
        self._update_selection_status()

    def selected_cleanup_paths(self):
        return [item['path'] for group in self._groups for item in group.get('files', [])
                if _key(item.get('path', '')) in self._selected_paths]

    def set_cleanup_paths(self, paths):
        known = {_key(item.get('path', '')) for group in self._groups for item in group.get('files', [])}
        self._selected_paths = {_key(path) for path in paths} & known
        self.last_preview = None
        self._render_groups()
        self._update_selection_status()

    def _update_selection_status(self):
        selected = [item for group in self._groups for item in group.get('files', [])
                    if _key(item.get('path', '')) in self._selected_paths]
        invalid = sum(bool(group.get('files')) and all(_key(item.get('path', '')) in self._selected_paths
                      for item in group['files']) for group in self._groups)
        self.status.setText(
            f'已明确选择 {len(selected)} 个待回收副本 · 预计逻辑冗余容量 {_bytes(sum(item.get("size", 0) for item in selected))}。'
            + (f'有 {invalid} 组未保留任何副本，必须取消至少一份勾选。' if invalid else
               '先生成预览，核对完整路径后才能确认移入回收站。')
            if self.result else '没有扫描结果。请先完成扫描，再检查重复组与空间分布。')
        self._update_cleanup_button()

    def _update_cleanup_button(self):
        valid = bool(self.result and self.result.get('complete') and not self.result.get('cancelled'))
        self.cleanup_button.setEnabled(valid and not self._scope_dirty and bool(self._selected_paths) and not self.service.busy)

    def _apply_strategy(self):
        if not self.result or not self.result.get('complete') or self.service.busy:
            return
        mode = self.keep_strategy.currentData()
        directory = _key(self.keep_directory.text().strip())
        if mode == 'directory' and not self.keep_directory.text().strip():
            self.status.setText('请先输入优先保留目录；此策略只改变勾选，不执行清理。')
            return
        selected = []
        for group in self._groups:
            files = group.get('files', [])
            if not files:
                continue
            if mode == 'oldest':
                keep = min(files, key=lambda item: (item.get('mtime_ns', 0), len(item['path']), _key(item['path'])))
            elif mode == 'shortest':
                keep = min(files, key=lambda item: (len(item['path']), _key(item['path'])))
            else:
                matches = [item for item in files if _key(item['path']).startswith(directory.rstrip('\\/') + '\\')]
                keep = min(matches or files, key=lambda item: (len(item['path']), _key(item['path'])))
            selected.extend(item['path'] for item in files if item is not keep)
        self.set_cleanup_paths(selected)

    def _keep_selected(self):
        item = self.duplicates_tree.currentItem()
        data = item.data(0, _ROLE) if item else None
        if not data or data.get('kind') != 'file':
            self.status.setText('请先在重复组中选中要保留的具体文件。')
            return
        group = next((value for value in self._groups if value.get('id') == data.get('group')), None)
        if group:
            selected = set(self._selected_paths)
            for value in group['files']:
                if _key(value['path']) == _key(data['path']):
                    selected.discard(_key(value['path']))
                else:
                    selected.add(_key(value['path']))
            self.set_cleanup_paths(selected)

    def request_cleanup_preview(self):
        if self._scope_dirty:
            self.status.setText('扫描范围已变化，请重新完成扫描后再生成清理预览。')
            return False
        self.last_preview = None
        accepted = self.service.preview_cleanup(self.selected_cleanup_paths())
        if accepted:
            self.status.setText('正在后台重新验证所选副本、保留文件和任务占用状态；尚未移动任何文件。')
        return accepted

    def _preview_ready(self, value):
        self.last_preview = value
        if self.confirmation_dialog:
            self.confirmation_dialog.reject()
            self.confirmation_dialog.deleteLater()
        self.confirmation_dialog = _CleanupDialog(value, self)
        self.confirmation_dialog.apply_button.clicked.connect(lambda: self.confirm_cleanup(confirmed=True))
        if self.window().isMinimized():
            self.confirmation_dialog.showMinimized()
        else:
            self.confirmation_dialog.show()
        self.status.setText('请在清理预览中核对完整路径并明确确认。' if value.get('count') else '没有可安全回收的文件，请检查具体原因后重新扫描。')

    def confirm_cleanup(self, confirmed=False):
        dialog = self.confirmation_dialog
        if (confirmed is not True or not dialog or not dialog.isVisible()
                or not dialog.confirm_check.isChecked() or not dialog.confirm_check.isEnabled()
                or dialog.applying or self.service.busy or not self.last_preview
                or not self.last_preview.get('token') or self.last_preview.get('errors')):
            self.status.setText('尚未最终确认，文件没有改变。请在预览中核对完整路径并勾选确认框。')
            return False
        if self.service.apply_cleanup(self.last_preview['token'], confirmed=True):
            dialog.set_applying(True)
            return True
        return False

    def _cleanup_ready(self, value):
        self.last_cleanup = value
        if self.confirmation_dialog:
            self.confirmation_dialog.set_applying(False)
            self.confirmation_dialog.accept()
        self._selected_paths.clear()
        self.last_preview = None
        errors = value.get('errors', [])
        self.status.setText(f'已移入回收站 {value.get("completed", 0)} 个 · 未完成 {value.get("failed", 0)} 个。'
                            + ('\n' + '\n'.join(str(item) for item in errors[:10]) if errors else '')
                            + '\n正在重新扫描以更新重复组和空间统计；历史记录会保留。')
        if errors:
            self.issues.setPlainText('\n'.join(str(item) for item in errors[:100]))
            self.issues.show()
        QTimer.singleShot(0, self.start_scan)

    def _populate_filters(self):
        stats = (self.result or {}).get('stats', {})
        for combo, title, dimension in ((self.type_filter, '全部类型', 'by_type'),
                (self.site_filter, '全部网站', 'by_site'), (self.date_filter, '全部日期', 'by_date'),
                (self.folder_filter, '全部目录', 'by_folder')):
            blocker = QSignalBlocker(combo)
            combo.clear()
            combo.addItem(title, '')
            for value in sorted((stats.get(dimension) or {}), key=str)[:2000]:
                combo.addItem(_TYPES.get(value, value) if dimension == 'by_type' else str(value), str(value))
                combo.setItemData(combo.count() - 1, str(value), Qt.ItemDataRole.ToolTipRole)
            if len(stats.get(dimension) or {}) > 2000:
                combo.setToolTip('筛选项显示前 2000 项；其余目录或来源可通过完整路径搜索定位。')
            del blocker

    def _schedule_filter(self, *_):
        self._files_page = 0
        self._filter_timer.start()

    def clear_filters(self):
        self.search_edit.clear()
        for combo in (self.type_filter, self.site_filter, self.date_filter, self.folder_filter):
            combo.setCurrentIndex(0)
        self.minimum_size.setValue(0)
        self.sort_combo.setCurrentIndex(0)
        self._files_page = 0
        self._filter_timer.stop()
        self._refresh_files()

    def _filtered_files(self):
        search = self.search_edit.text().strip().casefold()
        type_value, site, date, folder = (combo.currentData() for combo in
            (self.type_filter, self.site_filter, self.date_filter, self.folder_filter))
        minimum = self.minimum_size.value() * 1024 * 1024
        files = []
        for item in (self.result or {}).get('files', []):
            path = item.get('path', '')
            sites = item.get('source_sites') or ['未知来源']
            dates = item.get('download_dates') or ['未知日期']
            if (search and search not in (path + ' ' + item.get('name', '')).casefold()
                    or type_value and item.get('media_type') != type_value
                    or site and site not in sites and not (site == '多个来源' and len(sites) > 1)
                    or date and date not in dates and not (date == '多个日期' and len(dates) > 1)
                    or folder and _key(ntpath.dirname(path)) != _key(folder)
                    or int(item.get('size') or 0) < minimum):
                continue
            files.append(item)
        mode = self.sort_combo.currentData()
        if mode.startswith('size'):
            files.sort(key=lambda item: (int(item.get('size') or 0), _key(item.get('path', ''))), reverse=mode == 'size_desc')
        else:
            files.sort(key=lambda item: (_key(ntpath.dirname(item.get('path', ''))) if mode == 'folder'
                                       else str(item.get('name', '')).casefold(), _key(item.get('path', ''))))
        return files

    def _refresh_files(self):
        files = self._filtered_files()
        pages = max(1, (len(files) + self.FILE_PAGE_SIZE - 1) // self.FILE_PAGE_SIZE)
        self._files_page = min(self._files_page, pages - 1)
        start = self._files_page * self.FILE_PAGE_SIZE
        visible = files[start:start + self.FILE_PAGE_SIZE]
        self.files_table.setRowCount(len(visible))
        for row, item in enumerate(visible):
            reason = _STATES.get(item.get('status'), item.get('status', '未知状态'))
            if item.get('protected_reason'):
                reason += ' · 禁止清理：' + str(item['protected_reason'])
            values = (item.get('name', ''), item.get('path', ''), _bytes(item.get('size')),
                      _TYPES.get(item.get('media_type'), '其他'), '、'.join(item.get('source_sites', [])) or '未知来源',
                      '、'.join(item.get('download_dates', [])) or '未知日期', reason)
            for column, value in enumerate(values):
                self.files_table.setItem(row, column, _cell(value, item.get('path') if column == 0 else None))
        self.files_page_label.setText(f'第 {self._files_page + 1} / {pages} 页 · {len(files)} 个文件 · 每页最多 {self.FILE_PAGE_SIZE} 行')
        self.files_previous.setEnabled(self._files_page > 0)
        self.files_next.setEnabled(self._files_page + 1 < pages)

    def _change_file_page(self, difference):
        self._files_page = max(0, self._files_page + difference)
        self._refresh_files()

    def _summary_changed(self):
        self._summary_page = 0
        self._render_summary()

    def _change_summary_page(self, difference):
        self._summary_page = max(0, self._summary_page + difference)
        self._render_summary()

    def _render_summary(self):
        dimension = self.summary_dimension.currentData()
        values = ((self.result or {}).get('stats') or {}).get(dimension) or {}
        groups = sorted(values.items(), key=lambda item: (-int(item[1].get('bytes', 0)), str(item[0])))
        pages = max(1, (len(groups) + self.SUMMARY_PAGE_SIZE - 1) // self.SUMMARY_PAGE_SIZE)
        self._summary_page = min(self._summary_page, pages - 1)
        start = self._summary_page * self.SUMMARY_PAGE_SIZE
        visible = groups[start:start + self.SUMMARY_PAGE_SIZE]
        self.summary_table.setRowCount(len(visible))
        for row, (label, item) in enumerate(visible):
            for column, value in enumerate((_TYPES.get(label, label) if dimension == 'by_type' else label,
                                             item.get('count', 0), _bytes(item.get('bytes')))):
                self.summary_table.setItem(row, column, _cell(value))
        self.summary_page_label.setText(f'第 {self._summary_page + 1} / {pages} 页 · {len(groups)} 类')
        self.summary_previous.setEnabled(self._summary_page > 0)
        self.summary_next.setEnabled(self._summary_page + 1 < pages)

    def _open_folder(self):
        path = ''
        if self.tabs.currentIndex() == 0:
            item = self.duplicates_tree.currentItem()
            data = item.data(0, _ROLE) if item else None
            if data and data.get('kind') == 'file':
                path = data['path']
        else:
            row = self.files_table.currentRow()
            item = self.files_table.item(row, 0) if row >= 0 else None
            path = item.data(_ROLE) if item else ''
        if not path:
            self.status.setText('请先选择一个具体文件。')
            return
        folder = str(Path(path).parent)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(folder)):
            self.status.setText('无法打开文件所在目录，请检查磁盘是否连接、目录是否被移动及访问权限。')

    def _message(self, text):
        self.status.setText(text)
