"""批量网址预览；确认前不创建任务，也不触发媒体发现。"""
from __future__ import annotations

import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.batch_input import parse_urls, read_url_file
from core.media_quality import QUALITY_PREFERENCES


_READERS = set()
_VISIBLE_QUERY = {'modal_id', 'aweme_id', 'item_id', 'bvid', 'aid', 'cid', 'vid',
                  'video_id', 'v', 'p', 'id', 'showTab', 'from_tab_name'}


def source_display(url):
    """仅供展示；不把签名、凭据或未知查询值放进表格或悬停提示。"""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname or ''
        if not host:
            return '无效网址'
        if ':' in host:
            host = f'[{host}]'
        if parts.port:
            host += f':{parts.port}'
        query = [(key[:64], value if key in _VISIBLE_QUERY else '•••')
                 for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ''))[:4096]
    except (TypeError, ValueError):
        return '无效网址'


def plain_label(text='', parent=None):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class PlainTable(QTableWidget):
    """Qt 的普通悬停提示可能识别 HTML；这里始终用纯文本标签。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tip = QLabel(self, Qt.WindowType.ToolTip)
        self._tip.setTextFormat(Qt.TextFormat.PlainText)
        self._tip.setWordWrap(True)
        self._tip.setMaximumWidth(720)
        self._tip.setMargin(8)
        self._tip.setStyleSheet('background: #fffce8; color: #263547; border: 1px solid #d7dee7;')

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip:
            item = self.itemAt(event.pos())
            text = item.toolTip() if item else ''
            if text:
                self._tip.setText(text)
                self._tip.adjustSize()
                self._tip.move(event.globalPos())
                self._tip.show()
                QTimer.singleShot(12000, self._tip.hide)
            else:
                self._tip.hide()
            return True
        if event.type() in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress):
            self._tip.hide()
        return super().viewportEvent(event)


class _FileReader(QObject):
    finished = Signal(str, str)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.cancelled = threading.Event()
        self.finished.connect(self._release)
        _READERS.add(self)

    def start(self):
        threading.Thread(target=self._run, name='batch-txt-import', daemon=True).start()

    def _run(self):
        text, error = '', ''
        try:
            if not self.cancelled.is_set():
                text = read_url_file(self.path)
        except Exception as exc:
            error = str(exc)[:1000] or '无法读取文本文件，请检查文件权限和编码。'
        if self.cancelled.is_set():
            text, error = '', ''
        self.finished.emit(text, error)

    @Slot(str, str)
    def _release(self, *_):
        _READERS.discard(self)
        self.deleteLater()


class BatchImportDialog(QDialog):
    def __init__(self, parent=None, existing=(), defaults=None):
        super().__init__(parent)
        self.setWindowTitle('批量添加网址')
        self.resize(900, 760)
        self.existing = tuple(existing)
        self.result_urls = []
        self.options = {}
        self._preview = {}
        self._reader = None
        self._reading = False
        self._closing = False
        self._read_error = ''
        self._defaults = dict(defaults or {})
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self.refresh_preview)
        self._build()
        self.refresh_preview()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label('每行一个网址。预览后确认加入，再逐条分析；不会自动遍历账号作品。'))
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText('https://example.com/video-a\nhttps://example.com/video-b')
        self.editor.setMaximumHeight(155)
        self.editor.textChanged.connect(self._text_changed)
        layout.addWidget(self.editor)
        file_row = QHBoxLayout()
        self.file_button = QPushButton('导入 TXT…')
        self.file_button.clicked.connect(self.choose_file)
        self.remove_button = QPushButton('移除选中网址')
        self.remove_button.clicked.connect(self.remove_selected)
        file_row.addWidget(self.file_button)
        file_row.addWidget(self.remove_button)
        file_row.addStretch()
        layout.addLayout(file_row)
        self.summary = plain_label()
        layout.addWidget(self.summary)
        self.table = PlainTable(0, 4)
        self.table.setHorizontalHeaderLabels(['原文行号', '待导入网址', '校验结果', '原因'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for index, width in ((0, 80), (2, 100), (3, 230)):
            self.table.setColumnWidth(index, width)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        form = QFormLayout()
        output_row = QHBoxLayout()
        self.output_directory = QLineEdit(str(self._defaults.get('output_directory', '')))
        output_row.addWidget(self.output_directory, 1)
        browse = QPushButton('选择目录…')
        browse.clicked.connect(self.choose_output)
        output_row.addWidget(browse)
        form.addRow('默认输出目录', output_row)
        self.collection_name = QLineEdit()
        self.collection_name.setMaxLength(100)
        self.collection_name.setPlaceholderText('可选；每次导入创建独立合集，同名自动编号')
        form.addRow('批量合集名称', self.collection_name)
        from ui.output_options import OutputOptions
        self.output_options = OutputOptions(self._defaults.get('organization'), self)
        self.output_options.set_context(self.output_directory.text())
        self.output_directory.textChanged.connect(lambda text: self.output_options.set_context(text))
        form.addRow(self.output_options)
        self.quality = QComboBox()
        for value, label in QUALITY_PREFERENCES:
            self.quality.addItem(label, value)
        self.quality.setCurrentIndex(max(0, self.quality.findData(self._defaults.get('quality', 'auto'))))
        form.addRow('默认画质策略', self.quality)
        from core.media_tracks import LANGUAGES, normalize_preferences
        preferences = normalize_preferences(self._defaults.get('track_preferences'))
        self.audio_language = QComboBox()
        for code, label in LANGUAGES:
            self.audio_language.addItem(label + (f' ({code})' if code else ''), code)
        self.audio_language.setCurrentIndex(max(0, self.audio_language.findData(preferences['audio_language'])))
        form.addRow('偏好音轨语言', self.audio_language)
        self.subtitle_languages = QLineEdit(', '.join(preferences['subtitle_languages']))
        self.subtitle_languages.setPlaceholderText('语言代码，用逗号分隔，如 zh-Hans, en；无对应语言时等待确认')
        form.addRow('偏好字幕语言', self.subtitle_languages)
        self.subtitle_mode = QComboBox()
        for label, code in (('不额外下载字幕', 'none'), ('独立字幕文件', 'sidecar'), ('封装字幕（不烧录）', 'mux')):
            self.subtitle_mode.addItem(label, code)
        self.subtitle_mode.setCurrentIndex(max(0, self.subtitle_mode.findData(preferences['subtitle_mode'])))
        form.addRow('字幕处理', self.subtitle_mode)
        self.track_container = QComboBox()
        for label, code in (('沿用原有输出格式', 'original'), ('MP4', 'mp4'), ('MKV', 'mkv'), ('WebM', 'webm')):
            self.track_container.addItem(label, code)
        self.track_container.setCurrentIndex(max(0, self.track_container.findData(preferences['container'])))
        form.addRow('轨道封装容器', self.track_container)
        self.naming = QComboBox()
        for label, value in (('媒体标题', 'title'), ('序号 + 媒体标题', 'index_title'),
                             ('日期 + 媒体标题', 'date_title')):
            self.naming.addItem(label, value)
        self.naming.setCurrentIndex(max(0, self.naming.findData(self._defaults.get('naming', 'index_title'))))
        form.addRow('文件命名', self.naming)
        self.naming.setToolTip('模板留空时使用此规则；已填写自定义模板时，以模板命名。')
        self.output_options.changed.connect(lambda: self.naming.setEnabled(not bool(self.output_options.options()['filename_template'])))
        self.naming.setEnabled(not bool(self.output_options.options()['filename_template']))
        row = QHBoxLayout()
        self.max_concurrent = QSpinBox()
        self.max_concurrent.setRange(1, 10)
        self.max_concurrent.setValue(int(self._defaults.get('max_concurrent', 3)))
        row.addWidget(self.max_concurrent)
        row.addWidget(plain_label('个同时下载；页面分析始终逐条进行'))
        row.addStretch()
        form.addRow('最大并发下载', row)
        row = QHBoxLayout()
        self.retry_limit = QSpinBox()
        self.retry_limit.setRange(0, 3)
        self.retry_limit.setValue(int(self._defaults.get('retry_limit', 0)))
        row.addWidget(self.retry_limit)
        row.addWidget(plain_label('次；只对符合现有恢复条件的任务生效'))
        row.addStretch()
        form.addRow('失败自动恢复', row)
        self.discovery_seconds = QSpinBox()
        self.discovery_seconds.setRange(5, 120)
        self.discovery_seconds.setValue(int(self._defaults.get('discovery_seconds', 60)))
        self.discovery_seconds.setSuffix(' 秒')
        form.addRow('单条分析最长等待', self.discovery_seconds)
        options_content = QWidget()
        options_content.setLayout(form)
        self.options_scroll = QScrollArea()
        self.options_scroll.setWidgetResizable(True)
        self.options_scroll.setWidget(options_content)
        self.options_scroll.setMinimumHeight(190)
        self.options_scroll.setMaximumHeight(360)
        layout.addWidget(self.options_scroll)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.confirm_button = QPushButton('加入并开始分析')
        self.confirm_button.setObjectName('primary')
        self.confirm_button.clicked.connect(self.accept)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _text_changed(self):
        self._read_error = ''
        self.confirm_button.setEnabled(False)
        self._debounce.start()

    def refresh_preview(self):
        self._debounce.stop()
        try:
            self._preview = parse_urls(self.editor.toPlainText(), self.existing)
        except Exception:
            self._preview = {'urls': [], 'rows': [], 'valid': 0, 'duplicates': 0,
                             'invalid': 0, 'empty': 0, 'error': '网址预览失败，请检查输入内容。'}
        rows = self._preview.get('rows', [])
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(rows))
        labels = {'valid': '可导入', 'duplicate': '重复', 'invalid': '无效', 'empty': '空行'}
        for row_index, row in enumerate(rows):
            display = source_display(row.get('url', ''))
            values = (str(row.get('line', '')), display,
                      labels.get(row.get('status'), '无效'), str(row.get('reason') or ''))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row_index, column, item)
        error = self._read_error or self._preview.get('error', '')
        prefix = '正在后台读取 TXT… ' if self._reading else ''
        self.summary.setText(prefix + f"有效 {self._preview.get('valid', 0)} 条 · "
            f"重复 {self._preview.get('duplicates', 0)} 条 · 无效 {self._preview.get('invalid', 0)} 条 · "
            f"忽略空行 {self._preview.get('empty', 0)} 条" + (f'\n{error}' if error else ''))
        self.confirm_button.setEnabled(bool(self._preview.get('urls')) and not error and not self._reading)
        self._selection_changed()

    def _selection_changed(self):
        self.remove_button.setEnabled(bool(self.table.selectionModel().selectedRows()) and not self._reading)

    def remove_selected(self):
        selected = {index.row() for index in self.table.selectionModel().selectedRows()}
        rows = self._preview.get('rows', [])
        remove_lines = {rows[index]['line'] for index in selected if index < len(rows)}
        if remove_lines:
            lines = self.editor.toPlainText().splitlines()
            self.editor.setPlainText('\n'.join(line for number, line in enumerate(lines, 1)
                                               if number not in remove_lines))
            self.refresh_preview()

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入网址文本', '', '文本文件 (*.txt);;所有文件 (*)')
        if path:
            self.load_file(path)

    def load_file(self, path):
        if self._reading or self._closing:
            return False
        self._read_error = ''
        self._reading = True
        self.file_button.setEnabled(False)
        self.editor.setEnabled(False)
        self._reader = _FileReader(path)
        self._reader.finished.connect(self._file_loaded)
        self._reader.start()
        self.refresh_preview()
        return True

    @Slot(str, str)
    def _file_loaded(self, text, error):
        self._reader = None
        if self._closing:
            return
        self._reading = False
        self.file_button.setEnabled(True)
        self.editor.setEnabled(True)
        if text and not error:
            current = self.editor.toPlainText()
            self.editor.setPlainText(current + ('\n' if current else '') + text)
        self._read_error = error
        self.refresh_preview()

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, '默认输出目录', self.output_directory.text())
        if path:
            self.output_directory.setText(path)

    def accept(self):
        try:
            self.output_options.validate()
        except ValueError as error:
            self.summary.setText(str(error))
            return
        self.refresh_preview()
        if not self.confirm_button.isEnabled():
            return
        from core.media_tracks import language_code
        invalid_languages = [value.strip() for value in self.subtitle_languages.text().replace('，', ',').split(',')
                             if value.strip() and not language_code(value.strip())]
        if invalid_languages:
            self.summary.setText('字幕语言代码无效：' + ', '.join(invalid_languages)[:160] + '。请使用 en、ja、zh-Hans 等代码。')
            return
        self.result_urls = list(self._preview['urls'])
        self.options = {'output_directory': self.output_directory.text().strip(),
                        'quality': self.quality.currentData(), 'naming': self.naming.currentData(),
                        'max_concurrent': self.max_concurrent.value(), 'retry_limit': self.retry_limit.value(),
                        'discovery_seconds': self.discovery_seconds.value()}
        from core.media_tracks import normalize_preferences
        preferences = normalize_preferences({
            'audio_language': self.audio_language.currentData(),
            'subtitle_languages': self.subtitle_languages.text().replace('，', ',').split(','),
            'subtitle_mode': self.subtitle_mode.currentData(), 'container': self.track_container.currentData()})
        if preferences != normalize_preferences():
            self.options['track_preferences'] = preferences
        from core.output_organization import DEFAULTS
        if self.output_options.options() != DEFAULTS:
            self.options['organization'] = self.output_options.options()
        if self.collection_name.text().strip():
            self.options['collection_name'] = self.collection_name.text().strip()
        super().accept()

    def done(self, result):
        self._closing = True
        self._debounce.stop()
        if self._reader is not None:
            self._reader.cancelled.set()
        super().done(result)
