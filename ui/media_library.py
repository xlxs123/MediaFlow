"""分页媒体库与下载历史，复用主窗口样式。"""
from pathlib import Path
import math
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QUrl, QSize, QItemSelectionModel
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QMessageBox, QFileDialog, QDialog, QPlainTextEdit)

from core.media_tracks import language_name


def _plain(text=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _bytes(value):
    value = max(0, value or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value} B'
        value /= 1024


def _duration(value):
    if not value or value <= 0:
        return '未知时长'
    minutes, seconds = divmod(round(value), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


def _status(item):
    if item.get('task_state') != 'completed':
        return {'failed': '下载失败', 'cancelled': '已取消'}.get(item.get('task_state'), '未完成')
    state = item.get('file_state')
    if state in ('missing', 'inaccessible'):
        return {'missing': '文件已移动或丢失', 'inaccessible': '文件不可访问'}[state]
    subtitle_check = (item.get('validation') or {}).get('subtitle_check') or {}
    if item.get('validation_state') == 'valid' and subtitle_check.get('status') == 'not_checked':
        return '音视频已验证 · 字幕待完整检查'
    return {'valid': '文件存在 · 已验证', 'failed': '媒体验证失败',
            'unverified': '尚未完成验证'}.get(item.get('validation_state'), '尚未完成验证')


def _track_number(value, divisor=1):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return '未知'
    return f'{value / divisor:g}'


def track_details_text(validation):
    """只格式化后台已取得的摘要；展示详情不访问媒体、字幕或磁盘。"""
    validation = validation if isinstance(validation, dict) else {}
    decode = validation.get('full_decode')
    videos = validation.get('video_streams') or []
    audios = validation.get('audio_streams') or []
    complete = isinstance(decode, dict) and decode.get('status') == 'passed'
    if complete:
        counts = decode.get('streams_read')
        complete = type(counts) is int and counts >= len(videos) + len(audios)
    read_status = '完整读取通过' if complete else '尚未完成完整读取'
    subtitle_check = validation.get('subtitle_check')
    subtitle_check = subtitle_check if isinstance(subtitle_check, dict) else {}
    checked = {item.get('index'): item for item in subtitle_check.get('streams', [])
               if isinstance(item, dict)}
    sections = []
    for role, label, values in (('video', '视频轨', videos), ('audio', '音轨', audios),
                                ('subtitle', '内嵌字幕轨', validation.get('subtitle_streams') or [])):
        lines = []
        for number, stream in enumerate(values, 1):
            if not isinstance(stream, dict):
                continue
            index = stream.get('index')
            index_text = str(index) if type(index) is int and index >= 0 else '未知'
            title = str(stream.get('title') or '')
            parts = [f'{label} {number}（流索引 {index_text}）', language_name(stream.get('language')),
                     '编码：' + str(stream.get('codec_name') or '未知')]
            if title:
                parts.append('名称：' + title)
            if role == 'audio':
                parts.extend(['声道：' + _track_number(stream.get('channels')),
                    '布局：' + str(stream.get('channel_layout') or '未知'),
                    '采样率：' + (_track_number(stream.get('sample_rate'), 1000) + ' kHz'
                        if stream.get('sample_rate') else '未知'),
                    '码率：' + (_track_number(stream.get('bit_rate'), 1000) + ' kb/s'
                        if stream.get('bit_rate') else '未知')])
            if type(stream.get('default')) is bool:
                parts.append('默认：' + ('是' if stream['default'] else '否'))
            if type(stream.get('forced')) is bool:
                parts.append('强制：' + ('是' if stream['forced'] else '否'))
            if role == 'subtitle':
                state = checked.get(index, {})
                if state.get('status') == 'passed' and state.get('method') == 'text_decode':
                    parts.append('读取：文本字幕完整解码通过')
                elif state.get('status') == 'failed':
                    parts.append('读取：字幕校验失败')
                elif state.get('method') == 'packet_read':
                    parts.append('读取：仅完成数据包读取，尚未完整解码')
                else:
                    parts.append('读取：尚未完成字幕解码检查')
                if state.get('reason'):
                    parts.append('说明：' + str(state['reason']))
            else:
                parts.append('读取：' + read_status)
            lines.append(' · '.join(parts))
        if lines:
            sections.append('\n'.join(lines))
        elif role == 'subtitle':
            sections.append('内嵌字幕轨：未检测到内嵌字幕。' if 'subtitle_streams' in validation
                            else '内嵌字幕轨：旧记录尚未采集，可重新验证媒体获取实际轨道。')
    sidecars = []
    for number, item in enumerate(validation.get('subtitle_files') or [], 1):
        if not isinstance(item, dict):
            continue
        status = {'verified': '已验证', 'missing': '已移动或丢失',
                  'changed': '内容已变化，待重新验证',
                  'inaccessible': '无法访问', 'unverified': '尚未验证'}.get(item.get('status'), '尚未验证')
        sidecars.append(f'独立字幕 {number}：{language_name(item.get("language"))} · '
            f'格式：{str(item.get("format") or "未知").upper()} · 最近状态：{status}\n'
            '路径：' + str(item.get('path') or '未保存'))
    sections.append('\n'.join(sidecars) if sidecars else '独立字幕文件：未保存相关记录。')
    check_label = {'passed': '全部文本字幕轨完整解码通过', 'failed': '字幕校验失败',
                   'not_checked': '部分字幕尚未完成完整解码', 'none': '没有内嵌字幕轨'}.get(
                       subtitle_check.get('status'), '尚未执行独立字幕检查')
    sections.append('音视频读取：' + read_status + '\n字幕读取：' + check_label)
    return '\n\n'.join(sections)


def _container_label(item):
    formats = (item.get('container') or '').split(',')
    extension = Path(item.get('path', '')).suffix.lower().lstrip('.')
    if extension and (extension in formats or extension in ('m4a', 'm4v') and 'mp4' in formats
                      or extension == 'mkv' and 'matroska' in formats):
        return extension.upper()
    return (formats[0] or '未知格式').upper()


def _date(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, AttributeError):
        return ''


class MediaLibrary(QWidget):
    PAGE_SIZE = 50

    def __init__(self, service, tools_provider=None, parent=None):
        super().__init__(parent)
        self.service = service
        self.tools_provider = tools_provider or (lambda: {})
        self.items = []
        self.offset = 0
        self.total = 0
        self.generation = 0
        self._loading = False
        self._active = False
        self._dirty = True
        self._validating = False
        self._placeholders = {}
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self.refresh)
        self._build()
        service.page_ready.connect(self._page_ready)
        service.images_ready.connect(self._images_ready)
        service.changed.connect(self._changed)
        service.message.connect(self._message)
        service.action_ready.connect(self._action_ready)
        service.validation_changed.connect(self._validation_status)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 6)
        layout.setSpacing(10)
        filters = QHBoxLayout()
        self.view_mode = QComboBox()
        self.view_mode.addItem('媒体库', 'library')
        self.view_mode.addItem('全部下载历史', 'history')
        filters.addWidget(self.view_mode)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索文件名…')
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
        self.media_type = QComboBox()
        for label, value in (('所有类型', ''), ('视频', 'video'), ('音频', 'audio'), ('未知类型', 'unknown')):
            self.media_type.addItem(label, value)
        filters.addWidget(self.media_type)
        self.source_site = QComboBox()
        self.source_site.setMaximumWidth(180)
        self.source_site.addItem('所有来源', '')
        filters.addWidget(self.source_site)
        self.sort = QComboBox()
        for label, value in (('下载时间 ↓', 'date'), ('文件大小 ↓', 'size'),
                             ('媒体时长 ↓', 'duration'), ('分辨率 ↓', 'resolution')):
            self.sort.addItem(label, value)
        filters.addWidget(self.sort)
        self.reset_button = QPushButton('清空筛选')
        self.reset_button.clicked.connect(self.reset_filters)
        filters.addWidget(self.reset_button)
        layout.addLayout(filters)
        self.summary = _plain('下载完成的媒体会自动显示在这里。')
        self.summary.setObjectName('muted')
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 7)
        self.table.setObjectName('libraryTable')
        self.table.setHorizontalHeaderLabels(['预览', '文件名 / 来源', '类型 / 格式', '大小 / 时长',
                                             '分辨率', '文件 / 任务状态', '下载日期'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setIconSize(QSize(96, 54))
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(76)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for index, width in ((0, 108), (2, 100), (3, 125), (4, 90), (5, 160), (6, 160)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda *_: self.run_action('open_file'))
        layout.addWidget(self.table, 1)
        self.empty = _plain('暂无媒体。完成下载后将自动加入媒体库；失败和取消的任务可在“全部下载历史”中查看。')
        self.empty.setObjectName('emptyState')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)
        pages = QHBoxLayout()
        self.refresh_button = QPushButton('刷新文件状态')
        self.refresh_button.clicked.connect(self.refresh)
        pages.addWidget(self.refresh_button)
        pages.addStretch()
        self.previous = QPushButton('上一页')
        self.next = QPushButton('下一页')
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next.clicked.connect(lambda: self._move(1))
        self.page_label = QLabel('第 1 页')
        pages.addWidget(self.previous)
        pages.addWidget(self.page_label)
        pages.addWidget(self.next)
        layout.addLayout(pages)
        actions = QHBoxLayout()
        self.buttons = {}
        for title, command in (('打开文件', 'open_file'), ('所在文件夹', 'open_folder'),
                               ('重新验证', 'validate'), ('来源网页', 'source'), ('详细信息', 'details')):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, cmd=command: self.run_action(cmd))
            actions.addWidget(button)
            self.buttons[command] = button
        self.cancel_validation = QPushButton('取消验证')
        self.cancel_validation.clicked.connect(self.service.cancel_validation)
        self.cancel_validation.hide()
        actions.addWidget(self.cancel_validation)
        actions.addStretch()
        layout.addLayout(actions)
        record_actions = QHBoxLayout()
        self.organize_button = QPushButton('整理所选媒体…')
        self.organize_button.setToolTip('可按 Ctrl / Shift 多选本页记录，先预览路径并确认，再整理文件与关联字幕。')
        self.organize_button.clicked.connect(self.organize_selected)
        record_actions.addWidget(self.organize_button)
        self.relocate_button = QPushButton('重新定位文件')
        self.relocate_button.clicked.connect(lambda: self.run_action('relocate'))
        record_actions.addWidget(self.relocate_button)
        self.remove_button = QPushButton('仅移除历史记录')
        self.remove_button.setToolTip('保留磁盘上的媒体与缩略图。')
        self.remove_button.clicked.connect(lambda: self.run_action('remove'))
        record_actions.addWidget(self.remove_button)
        self.delete_button = QPushButton('移入回收站…')
        self.delete_button.setToolTip('必须预览并确认完整路径；失败不会永久删除，历史记录保留。')
        self.delete_button.clicked.connect(lambda: self.run_action('delete_preview'))
        record_actions.addWidget(self.delete_button)
        record_actions.addStretch()
        layout.addLayout(record_actions)
        self.notice = _plain('每页最多 50 条记录。文件状态在后台检查，移除历史记录不会删除文件。')
        self.notice.setObjectName('muted')
        layout.addWidget(self.notice)
        for combo in (self.view_mode, self.media_type, self.source_site, self.sort):
            combo.currentIndexChanged.connect(self._filter_changed)
        self.search.textChanged.connect(self._filter_changed)
        self._selection_changed()

    def set_active(self, active):
        self._active = active
        if active and self._dirty:
            self.refresh()

    def _filter_changed(self, *_):
        self.offset = 0
        self._debounce.start()

    def _changed(self):
        self._dirty = True
        if self._active:
            self._debounce.start()

    def refresh(self):
        self._dirty = False
        self._loading = True
        self.summary.setText('正在后台加载历史并检查本页文件状态…')
        self.generation = self.service.request_page(view=self.view_mode.currentData(),
            search=self.search.text().strip(), media_type=self.media_type.currentData(),
            source_site=self.source_site.currentData(), sort=self.sort.currentData(),
            offset=self.offset, limit=self.PAGE_SIZE)

    def reset_filters(self):
        for widget in (self.search, self.media_type, self.source_site, self.sort):
            widget.blockSignals(True)
        self.search.clear()
        self.media_type.setCurrentIndex(0)
        self.source_site.setCurrentIndex(0)
        self.sort.setCurrentIndex(0)
        for widget in (self.search, self.media_type, self.source_site, self.sort):
            widget.blockSignals(False)
        self.offset = 0
        self.refresh()

    def _move(self, delta):
        self.offset = max(0, self.offset + delta * self.PAGE_SIZE)
        self.refresh()

    def selected_entry(self):
        row = self.table.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def selected_entries(self):
        return [self.items[index.row()] for index in self.table.selectionModel().selectedRows()
                if 0 <= index.row() < len(self.items)]

    def organize_selected(self):
        entries = self.selected_entries()
        if not entries:
            return
        from ui.organize_dialog import OrganizeDialog
        dialog = OrganizeDialog(self.service, entries, self)
        dialog.exec()
        dialog.deleteLater()

    def _page_ready(self, generation, page):
        if generation != self.generation:
            return
        previous_ids = {item['id'] for item in self.selected_entries()}
        self._loading = False
        self.total = page['total']
        if self.offset and self.offset >= self.total:
            self.offset = max(0, ((self.total - 1) // self.PAGE_SIZE) * self.PAGE_SIZE)
            self.refresh()
            return
        source = self.source_site.currentData()
        self.source_site.blockSignals(True)
        self.source_site.clear()
        self.source_site.addItem('所有来源', '')
        for site in page.get('sources', []):
            self.source_site.addItem(site, site)
        self.source_site.setCurrentIndex(max(0, self.source_site.findData(source)))
        self.source_site.blockSignals(False)
        self.items = page['items']
        self.table.setRowCount(len(self.items))
        selected = []
        self.table.blockSignals(True)
        for row, item in enumerate(self.items):
            if item['id'] in previous_ids:
                selected.append(row)
            filename = Path(item['path']).name if item.get('path') else item.get('title', '')
            values = ('', filename + '\n' + (item.get('source_site') or '来源未知'),
                {'video': '视频', 'audio': '音频'}.get(item.get('media_type'), '待识别') + '\n' +
                    _container_label(item),
                _bytes(item.get('size')) + '\n' + _duration(item.get('duration')),
                f"{item['width']} × {item['height']}" if item.get('width') and item.get('height') else '—',
                _status(item), _date(item.get('finished_at')))
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(item.get('path') or item.get('title', '') if column == 1 else
                    '\n'.join(str(item.get(key, '') or '') for key in (
                        'error', 'validation_reason', 'file_message')) if column == 5 else str(value))
                if column == 0:
                    cell.setIcon(self._placeholder(item.get('media_type', 'unknown')))
                    cell.setToolTip('本地缩略图；没有封面时显示文件类型。')
                self.table.setItem(row, column, cell)
        if selected:
            for row in selected:
                self.table.selectionModel().select(self.table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
            self.table.setCurrentCell(selected[0], 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        elif self.items:
            self.table.selectRow(0)
        self.table.blockSignals(False)
        self.empty.setVisible(not self.items)
        self.table.setVisible(bool(self.items))
        self.empty.setText('没有符合筛选条件的记录，点击“清空筛选”可恢复列表。' if
            self.search.text() or self.media_type.currentData() or self.source_site.currentData() else
            '暂无媒体。完成下载后会自动加入；失败和取消的任务请切换到“全部下载历史”。')
        self.summary.setText(f'共 {self.total} 条记录 · 本页 {len(self.items)} 条 · 文件状态已在后台检查')
        pages = max(1, math.ceil(self.total / self.PAGE_SIZE))
        self.page_label.setText(f'第 {self.offset // self.PAGE_SIZE + 1} / {pages} 页')
        self.previous.setEnabled(self.offset > 0)
        self.next.setEnabled(self.offset + self.PAGE_SIZE < self.total)
        self._selection_changed()

    def _placeholder(self, kind):
        if kind not in self._placeholders:
            pixmap = QPixmap(96, 54)
            pixmap.fill(QColor('#e9eff5'))
            painter = QPainter(pixmap)
            painter.setPen(QColor('#61758a'))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter,
                {'video': '▶ 视频', 'audio': '♪ 音频'}.get(kind, '媒体文件'))
            painter.end()
            self._placeholders[kind] = QIcon(pixmap)
        return self._placeholders[kind]

    def _images_ready(self, generation, images):
        if generation != self.generation:
            return
        for row, item in enumerate(self.items):
            image = images.get(item['id'])
            if image is not None:
                self.table.item(row, 0).setIcon(QIcon(QPixmap.fromImage(image)))

    def _selection_changed(self):
        entry = self.selected_entry()
        for command, button in self.buttons.items():
            button.setEnabled(bool(entry) and (command != 'validate' or not self._validating))
        self.remove_button.setEnabled(bool(entry))
        self.relocate_button.setEnabled(bool(entry and entry.get('path')))
        self.delete_button.setEnabled(bool(entry and entry.get('path')))
        self.organize_button.setEnabled(bool(self.selected_entries()) and not self._validating)

    def _message(self, message):
        self.notice.setText(message)
        self._loading = False
        if '存储' in message or '数据库' in message:
            self.summary.setText(message)

    def _validation_status(self, message):
        self._validating = self.service.validator is not None
        self.cancel_validation.setVisible(self._validating)
        self.notice.setText(message)
        self._selection_changed()

    def run_action(self, command):
        entry = self.selected_entry()
        if not entry:
            return
        if command == 'validate':
            if not entry.get('path'):
                self._message('该任务没有完成的媒体文件，无法验证。')
                return
            self.service.validate(entry['id'], **self.tools_provider())
        elif command == 'source':
            url = entry.get('source_url')
            if not url:
                self._message('未保存可安全打开的来源网页，请重新分析来源。')
            elif not QDesktopServices.openUrl(QUrl(url)):
                self._message('无法打开来源网页，请重新分析来源。')
            else:
                self._message('已打开来源网页；如内容已失效，请重新分析来源。')
        elif command == 'remove':
            self.service.action('remove', entry['id'])
            self._message('已请求移除历史记录，磁盘文件与缩略图会保留。')
        elif command == 'relocate':
            filename, _ = QFileDialog.getOpenFileName(self, '重新定位媒体文件', entry.get('path', ''), '媒体文件 (*.*)')
            if filename:
                self.service.action('relocate', entry['id'], path=filename)
        else:
            self.service.action(command, entry['id'])

    def _action_ready(self, command, entry):
        if command.startswith('organize_'):
            return
        if not entry:
            self._message('该历史记录已不存在，请刷新列表。')
            return
        if command in ('open_file', 'open_folder'):
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(entry['open_path'])):
                self._message('Windows 无法打开此文件或文件夹，请检查访问权限。')
        elif command == 'delete_preview':
            plan = entry.get('delete_plan', {})
            if not plan.get('token') or plan.get('errors'):
                self._message('\n'.join(plan.get('errors', [])) or '无法安全生成清理预览，请重新扫描。')
                return
            dialog = QMessageBox(self)
            dialog.setWindowTitle('确认移入系统回收站')
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setText('将以下 1 个文件移入系统回收站？\n\n文件名：' + Path(entry['path']).name +
                           '\n完整路径：' + entry['path'] + '\n文件大小：' + _bytes(plan.get('bytes', 0)) +
                           '\n\n历史记录和关联字幕仍会保留。回收站仍占空间，由您清空后才真正释放容量。'
                           '\n如果系统不能安全回收，将取消操作，不会永久删除。')
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            if dialog.exec() == QMessageBox.StandardButton.Yes:
                self.service.action('delete_file', entry['id'], token=plan['token'], confirmed=True)
        elif command == 'details':
            self._show_details(entry)

    def _show_details(self, entry):
        rows = [('标题', entry.get('title')), ('文件路径', entry.get('path')),
                ('任务 ID', f"{entry.get('queue_id', '')} / {entry.get('task_key', '')}"),
                ('来源网站', entry.get('source_site')), ('来源网页', entry.get('source_url') or '未保存或包含敏感参数'),
                ('状态', _status(entry)), ('文件大小', _bytes(entry.get('size'))),
                ('媒体时长', _duration(entry.get('duration'))),
                ('视频分辨率', f"{entry.get('width', 0)} × {entry.get('height', 0)}"),
                ('容器格式', entry.get('container')), ('视频编码', entry.get('video_codec')),
                ('音频编码', entry.get('audio_codec')), ('缩略图路径', entry.get('thumbnail_path')),
                ('开始时间', entry.get('started_at')), ('结束时间', entry.get('finished_at')),
                ('取消时间', entry.get('cancelled_at')), ('已完成进度',
                    str((entry.get('progress') or {}).get('percent', '未知')) + '%'),
                ('校验结果', entry.get('validation_reason')), ('失败原因', entry.get('error'))]
        validation = entry.get('validation') or {}
        rows.append(('实际轨道与独立字幕', track_details_text(validation)))
        dialog = QDialog(self)
        dialog.setWindowTitle('媒体详细信息')
        dialog.resize(750, 600)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText('\n\n'.join(f'{key}：{value or "—"}' for key, value in rows))
        layout.addWidget(text)
        done = QPushButton('关闭')
        done.clicked.connect(dialog.accept)
        layout.addWidget(done)
        dialog.exec()
