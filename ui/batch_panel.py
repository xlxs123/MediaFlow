"""批次元数据分页视图，所有任务操作都由外部控制器执行。"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
    QPushButton, QTableWidgetItem, QVBoxLayout, QWidget)

from ui.batch_import import PlainTable, plain_label


STATE_LABELS = {
    'waiting_discovery': '等待发现', 'analyzing': '正在分析',
    'waiting_selection': '等待用户选择', 'queued': '等待下载',
    'downloading': '正在下载', 'merging': '正在合并', 'verifying': '正在验证',
    'completed': '已完成', 'paused': '已暂停', 'cancelled': '已取消',
    'failed': '下载失败', 'waiting_context': '需要重新分析',
}
_ALIASES = {'waiting_download': 'queued', 'running': 'downloading',
            'waiting_choice': 'waiting_selection', 'pending': 'waiting_discovery',
            'discovering': 'analyzing', 'validating': 'verifying'}
_TERMINAL = {'completed', 'failed', 'cancelled'}


def _state(item):
    state = item.get('state', '')
    return _ALIASES.get(state, state)


def _size(value):
    try:
        size = float(value)
        if not math.isfinite(size) or size < 0:
            return '—'
    except (ValueError, TypeError, OverflowError):
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{int(size)} B' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


def _progress(value):
    try:
        percent = float(value)
        if math.isfinite(percent) and 0 <= percent <= 100:
            return f'{percent:.1f}%'
    except (ValueError, TypeError, OverflowError):
        pass
    return '—'


class BatchPanel(QWidget):
    PAGE_SIZE = 50
    import_requested = Signal()
    item_action = Signal(str, str)
    batch_action = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.batches = []
        self.items = []
        self.page_items = []
        self.offset = 0
        self._filtered = []
        self._build()
        self._render()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 6)
        layout.setSpacing(10)
        filters = QHBoxLayout()
        self.import_button = QPushButton('批量添加…')
        self.import_button.setObjectName('primary')
        self.import_button.clicked.connect(self.import_requested)
        filters.addWidget(self.import_button)
        self.batch_filter = QComboBox()
        self.batch_filter.setMinimumWidth(160)
        self.batch_filter.setMaximumWidth(300)
        self.batch_filter.addItem('所有批次', '')
        filters.addWidget(self.batch_filter)
        self.state_filter = QComboBox()
        self.state_filter.addItem('所有状态', '')
        for state, label in STATE_LABELS.items():
            self.state_filter.addItem(label, state)
        filters.addWidget(self.state_filter)
        self.failed_button = QPushButton('查看失败任务')
        self.failed_button.clicked.connect(self.show_failed)
        filters.addWidget(self.failed_button)
        self.reset_button = QPushButton('清空筛选')
        self.reset_button.clicked.connect(self.reset_filters)
        filters.addWidget(self.reset_button)
        filters.addStretch()
        layout.addLayout(filters)
        self.summary = plain_label()
        layout.addWidget(self.summary)
        self.table = PlainTable(0, 10)
        self.table.setObjectName('batchTable')
        self.table.setHorizontalHeaderLabels(['标题', '来源网站', '任务状态', '候选',
            '下载进度', '大小', '速度', '最近原因', '输出路径', '所属合集'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((260, 140, 135, 65, 105, 100, 110, 290, 300, 160)):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda *_: self._emit_item('show'))
        layout.addWidget(self.table, 1)
        self.empty = plain_label('暂无批量任务。点击“批量添加…”粘贴网址或导入 TXT，然后确认开始分析。')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName('emptyState')
        layout.addWidget(self.empty)
        pages = QHBoxLayout()
        self.selection_notice = plain_label('选择任务可查看详情；需要确认的候选会等待您选择。')
        pages.addWidget(self.selection_notice, 1)
        self.previous = QPushButton('上一页')
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next = QPushButton('下一页')
        self.next.clicked.connect(lambda: self._move(1))
        self.page_label = plain_label()
        pages.addWidget(self.previous)
        pages.addWidget(self.page_label)
        pages.addWidget(self.next)
        layout.addLayout(pages)
        actions = QHBoxLayout()
        self.buttons = {}
        for label, action in (('选择候选…', 'select'), ('暂停任务', 'pause'),
            ('继续任务', 'resume'), ('重试任务', 'retry'), ('取消任务', 'cancel'), ('查看详情', 'show')):
            button = QPushButton(label)
            if action == 'select':
                button.setObjectName('primary')
            button.clicked.connect(lambda checked=False, value=action: self._emit_item(value))
            self.buttons[action] = button
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.batch_summary = plain_label()
        layout.addWidget(self.batch_summary)
        batch_actions = QHBoxLayout()
        self.batch_buttons = {}
        for label, action in (('暂停整个批次', 'pause'), ('继续整个批次', 'resume'),
                              ('取消整个批次', 'cancel'), ('重试本批失败任务', 'retry_failed'),
                              ('打开合集文件夹', 'open_collection')):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, value=action: self._emit_batch(value))
            self.batch_buttons[action] = button
            batch_actions.addWidget(button)
        batch_actions.addStretch()
        layout.addLayout(batch_actions)
        self.batch_filter.currentIndexChanged.connect(self._filter_changed)
        self.state_filter.currentIndexChanged.connect(self._filter_changed)

    def set_snapshot(self, batches, items):
        selected = self.selected_item()
        selected_id = selected.get('id') if selected else ''
        batch_id = self.batch_filter.currentData()
        self.batches = [dict(item) for item in batches]
        self.items = [dict(item) for item in items]
        self.batch_filter.blockSignals(True)
        self.batch_filter.clear()
        self.batch_filter.addItem('所有批次', '')
        for batch in self.batches:
            title = str(batch.get('title') or '未命名批次')
            self.batch_filter.addItem(title[:70], str(batch.get('id', '')))
        self.batch_filter.setCurrentIndex(max(0, self.batch_filter.findData(batch_id)))
        self.batch_filter.blockSignals(False)
        self._render(selected_id)

    def selected_item(self):
        row = self.table.currentRow()
        return self.page_items[row] if 0 <= row < len(self.page_items) else None

    def select_item(self, item_id):
        item_id = str(item_id)
        index = next((index for index, item in enumerate(self._filtered)
                      if str(item.get('id', '')) == item_id), None)
        if index is None:
            return False
        self.offset = (index // self.PAGE_SIZE) * self.PAGE_SIZE
        self._render(item_id)
        return True

    def _filter_changed(self, *_):
        self.offset = 0
        self._render()

    def _move(self, delta):
        self.offset = max(0, self.offset + delta * self.PAGE_SIZE)
        self._render()

    def reset_filters(self):
        self.batch_filter.blockSignals(True)
        self.state_filter.blockSignals(True)
        self.batch_filter.setCurrentIndex(0)
        self.state_filter.setCurrentIndex(0)
        self.batch_filter.blockSignals(False)
        self.state_filter.blockSignals(False)
        self._filter_changed()

    def show_failed(self):
        self.state_filter.setCurrentIndex(self.state_filter.findData('failed'))

    def _render(self, selected_id=''):
        batch_id, state = self.batch_filter.currentData(), self.state_filter.currentData()
        self._filtered = [item for item in self.items
            if (not batch_id or str(item.get('batch_id', '')) == batch_id)
            and (not state or _state(item) == state)]
        total = len(self._filtered)
        self.offset = min(self.offset, max(0, ((total - 1) // self.PAGE_SIZE) * self.PAGE_SIZE))
        self.page_items = self._filtered[self.offset:self.offset + self.PAGE_SIZE]
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(self.page_items))
        for row, item in enumerate(self.page_items):
            state = _state(item)
            title = str(item.get('title') or f"第 {item.get('ordinal', row + 1)} 条网址")
            speed = str(item.get('speed') or '—') if state == 'downloading' else '—'
            batch = next((b for b in self.batches if b.get('id') == item.get('batch_id')), {})
            collection = batch.get('options', {}).get('collection_name', '')
            values = (title, str(item.get('source_site') or '—'), STATE_LABELS.get(state, '未知状态'),
                str(item.get('candidate_count') or 0), _progress(item.get('percent')),
                _size(item.get('size')), speed, str(item.get('error') or ''),
                str(item.get('output_path') or '—'), collection or '—')
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value + ('\n所属合集：' + collection if collection else ''))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, str(item.get('id', '')))
                self.table.setItem(row, column, cell)
            if selected_id and str(item.get('id', '')) == str(selected_id):
                self.table.setCurrentCell(row, 0)
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self.empty.setVisible(total == 0)
        self.empty.setText('没有符合筛选条件的任务。可点击“清空筛选”恢复完整列表。' if self.items else
            '暂无批量任务。点击“批量添加…”粘贴网址或导入 TXT，然后确认开始分析。')
        self.summary.setText(f'共 {len(self.batches)} 个批次 · {len(self.items)} 个任务 · 当前筛选 {total} 个任务')
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + self.PAGE_SIZE < total)
        pages = max(1, math.ceil(total / self.PAGE_SIZE))
        self.page_label.setText(f'第 {self.offset // self.PAGE_SIZE + 1} / {pages} 页')
        self._selection_changed()

    def _current_batch(self):
        batch_id = self.batch_filter.currentData()
        item = self.selected_item()
        if not batch_id and item:
            batch_id = str(item.get('batch_id') or '')
        return next((batch for batch in self.batches if str(batch.get('id')) == batch_id), None)

    def _selection_changed(self):
        item = self.selected_item()
        state = _state(item) if item else ''
        enabled = {'pause': bool(item) and state not in _TERMINAL | {'paused'},
            'resume': state == 'paused', 'retry': state in {'failed', 'cancelled', 'waiting_context'},
            'cancel': bool(item) and state not in _TERMINAL, 'select': state == 'waiting_selection',
            'show': bool(item)}
        for action, button in self.buttons.items():
            button.setEnabled(enabled[action])
        self.selection_notice.setText('此任务需要确认媒体或画质，请点击“选择候选…”。' if state == 'waiting_selection'
            else '选择任务可查看详情；需要确认的候选会等待您选择。')
        batch = self._current_batch()
        members = [item for item in self.items if batch and str(item.get('batch_id')) == str(batch.get('id'))]
        counts = {}
        for member in members:
            member_state = _state(member)
            counts[member_state] = counts.get(member_state, 0) + 1
        if batch:
            details = ' · '.join(f'{STATE_LABELS.get(key, "其他")} {count}' for key, count in counts.items())
            paused = ' · 整批已暂停' if batch.get('paused') else ''
            collection = batch.get('options', {}).get('collection_name', '')
            collection_label = ' · 所属合集：' + collection if collection else ''
            self.batch_summary.setText(f"当前批次：{batch.get('title') or '未命名批次'}{paused}{collection_label} · 共 {len(members)} 项 · " + (details or '暂无任务'))
        else:
            self.batch_summary.setText('选择一个批次或其中的任务，可管理整个批次。')
        active = any(_state(member) not in _TERMINAL for member in members)
        self.batch_buttons['pause'].setEnabled(bool(batch and active and not batch.get('paused')))
        self.batch_buttons['resume'].setEnabled(bool(batch and (batch.get('paused') or counts.get('paused'))))
        self.batch_buttons['cancel'].setEnabled(bool(batch and active))
        self.batch_buttons['retry_failed'].setEnabled(bool(batch and counts.get('failed')))
        self.batch_buttons['open_collection'].setEnabled(bool(batch and batch.get('options', {}).get('collection_directory')))

    def _emit_item(self, action):
        item = self.selected_item()
        if item and self.buttons[action].isEnabled():
            self.item_action.emit(action, str(item['id']))

    def _emit_batch(self, action):
        batch = self._current_batch()
        if batch and self.batch_buttons[action].isEnabled():
            self.batch_action.emit(action, str(batch['id']))
