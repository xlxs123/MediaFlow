from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                              QDialogButtonBox, QTableWidget, QHeaderView, QLineEdit, QCheckBox, QMessageBox)
from PySide6.QtWidgets import QWidget
from utils.security import Redactor, is_sensitive_header, normalize_headers, redact_data


class CurlDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('粘贴 URL / cURL')
        self.resize(820, 480)
        layout = QVBoxLayout(self)
        label = QLabel('粘贴完整媒体 URL，或 Chrome DevTools 的 Copy as cURL（CMD / Bash）。会自动解析 method、headers 和内联 body。')
        label.setWordWrap(True)
        layout.addWidget(label)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("curl 'https://cdn.example.com/index.m3u8' …")
        layout.addWidget(self.editor)
        warning = QLabel('cURL 可能包含 Cookie 等凭据，只在当前会话内使用。')
        warning.setObjectName('muted')
        layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        parse = buttons.addButton('解析', QDialogButtonBox.ButtonRole.AcceptRole)
        parse.setObjectName('primary')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def closeEvent(self, event):
        self.editor.clear()
        super().closeEvent(event)


class HeadersEditor(QTableWidget):
    def __init__(self, headers=None, parent=None):
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(['Header Name', 'Value'])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setMinimumHeight(120)
        self.reveal_sensitive = False
        self.load_headers(headers or {})

    def add_header(self, name='', value=''):
        row = self.rowCount()
        self.insertRow(row)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText('例如 X-Custom-Header')
        value_edit = QLineEdit(value)
        value_edit.setPlaceholderText('请求头的值')
        self.setCellWidget(row, 0, name_edit)
        self.setCellWidget(row, 1, value_edit)
        name_edit.textChanged.connect(lambda _: self._mask_value(name_edit, value_edit))
        self._mask_value(name_edit, value_edit)

    def _mask_value(self, name_edit, value_edit):
        sensitive = is_sensitive_header(name_edit.text())
        value_edit.setEchoMode(QLineEdit.EchoMode.Password if sensitive and not self.reveal_sensitive else QLineEdit.EchoMode.Normal)
        value_edit.setToolTip('敏感值只保留在当前进程内' if sensitive else '')

    def set_reveal(self, reveal):
        self.reveal_sensitive = reveal
        for row in range(self.rowCount()):
            self._mask_value(self.cellWidget(row, 0), self.cellWidget(row, 1))

    def load_headers(self, headers):
        self.setRowCount(0)
        for name, value in headers.items():
            self.add_header(name, value)

    def delete_selected(self):
        rows = {index.row() for index in self.selectedIndexes()}
        if not rows and self.currentRow() >= 0:
            rows.add(self.currentRow())
        for row in sorted(rows, reverse=True):
            self.removeRow(row)

    def headers(self):
        headers = {}
        for row in range(self.rowCount()):
            name, value = self.cellWidget(row, 0).text().strip(), self.cellWidget(row, 1).text()
            if not name and not value.strip():
                continue
            if name.lower() in headers:
                raise ValueError(f'请求头 {name} 重复，请合并成一行。')
            headers[name.lower()] = value
        return normalize_headers(headers)


class HeadersDialog(QDialog):
    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.setWindowTitle('查看和修改全部 Headers')
        self.resize(840, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('名称大小写不敏感；空值不会发送。Cookie / Authorization 等敏感头默认隐藏。'))
        self.table = HeadersEditor(headers)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton('添加')
        remove = QPushButton('删除')
        add.clicked.connect(lambda: self.table.add_header())
        remove.clicked.connect(self.table.delete_selected)
        reveal = QCheckBox('显示敏感值')
        reveal.toggled.connect(self.table.set_reveal)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        row.addWidget(reveal)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('应用修改')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self):
        try:
            self.table.headers()
        except ValueError as error:
            QMessageBox.warning(self, '请求头需要修改', str(error))
            return
        self.accept()


class ErrorDialog(QDialog):
    def __init__(self, error, parent=None):
        super().__init__(parent)
        self.setWindowTitle('完整错误（敏感信息已隐藏）')
        self.resize(840, 480)
        layout = QVBoxLayout(self)
        editor = QPlainTextEdit(error)
        editor.setReadOnly(True)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def manifest_label(value):
    return {'Media': 'Media Playlist', 'Master': 'Master Playlist'}.get(value, value or '--')


def format_resolver_chain(values):
    trace = values.get('resolver_trace') or values.get('trace') or {}
    if not trace:
        return ''
    parts = ['/api/hls'] if trace.get('api') else []
    if trace.get('api_status'):
        parts.append(str(trace['api_status']))
    container = trace.get('container')
    if container:
        parts.append('PNG(roUd)' if container == 'PNG' and trace.get('roud') == 'FOUND' else str(container))
    compression = trace.get('compression')
    if compression and str(compression).lower() not in ('none', 'identity'):
        parts.append(str(compression))
    if trace.get('playlist') == 'VALID':
        parts.append('HLS')
    return ' → '.join(parts)


def format_resolver_trace(values):
    trace = values.get('resolver_trace') or values.get('trace') or {}
    redactor = Redactor(values.get('headers', {}), [values.get('original_url', ''),
                         values.get('final_url', ''), values.get('manifest_url', '')])

    def safe(item):
        if isinstance(item, dict):
            return {key: '[present]' if is_sensitive_header(str(key)) and value else safe(value)
                    for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [safe(value) for value in item]
        return item

    return json.dumps(redact_data(safe(trace), redactor), ensure_ascii=False, indent=2) if trace else ''


class ResolverTracePanel(QWidget):
    """完整协议 Trace 默认折叠，内容始终来自脱敏诊断。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = QPushButton('Resolver Trace（展开）')
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self._toggle)
        layout.addWidget(self.toggle)
        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setMaximumBlockCount(4000)
        self.editor.setMaximumHeight(220)
        self.editor.hide()
        layout.addWidget(self.editor)
        self.hide()

    def _toggle(self, expanded):
        self.toggle.setText('Resolver Trace（收起）' if expanded else 'Resolver Trace（展开）')
        self.editor.setVisible(expanded)

    def set_values(self, values):
        text = format_resolver_trace(values)
        self.editor.setPlainText(text)
        self.setVisible(bool(text))


def format_diagnostic(values):
    drm = {True: 'Yes', False: 'No', None: 'Unknown'}.get(values.get('drm'), 'Unknown')
    pairs = [('媒体类型', values.get('media_type')), ('Manifest', manifest_label(values.get('manifest_type') or values.get('playlist_type'))),
             ('HLS 播放类型', values.get('hls_playlist_type') or values.get('playlist_type')),
             ('Encryption', values.get('encryption')), ('DRM', drm), ('Classification', values.get('classification')),
             ('HTTP', values.get('http_status')), ('错误分类', values.get('error_code')),
             ('检测阶段', values.get('stage')), ('HTTP Method', values.get('method')),
             ('请求 Range', values.get('request_range')),
             ('输入 Method', values.get('input_method')),
             ('Origin', values.get('origin')), ('Referer', values.get('referer')),
             ('User-Agent', values.get('user_agent')), ('原始 URL', values.get('original_url')),
             ('最终 URL', values.get('final_url')), ('Manifest 最终 URL', values.get('manifest_final_url'))]
    for name, key in (('Resolver', 'resolver_id'), ('视频标识', 'video_id'), ('播放页', 'page_url'),
                      ('URL 解析策略', 'url_resolution_policy'), ('清单解码器', 'playlist_decoder'),
                      ('临时签名', 'signed_url'), ('发现时间', 'discovered_at'), ('到期线索', 'expires_hint'),
                      ('异常类型', 'exception_type'), ('异常详情', 'exception_message'),
                      ('失败分片', 'segment_index'), ('分片总数', 'segment_total'),
                      ('当前尝试', 'attempt'), ('总尝试次数', 'max_attempts')):
        if key in values:
            pairs.append((name, values[key]))
    if values.get('resolver_verified') is True:
        pairs.extend([('解析方式', values.get('source_type') or values.get('resolver_id')),
                      ('状态', '已验证'), ('置信度', '100/100'),
                      ('视频标题', values.get('page_title') or values.get('title') or '--'),
                      ('Segment', values.get('segment_count')), ('时长（秒）', values.get('duration')),
                      ('解析链', format_resolver_chain(values))])
    lines = [f'{name}：{value if value is not None and value != "" else "--"}' for name, value in pairs]
    if values.get('response_headers'):
        lines += ['', '响应信息：']
        lines += [f'{name}：{value}' for name, value in values['response_headers'].items()]
    if values.get('timing'):
        lines += ['Timing：' + str(values['timing'])]
    if values.get('missing_context'):
        lines += ['', '当前未提供的请求字段：' + ', '.join(values['missing_context']),
                  '这些是检查线索，具体是否必需由服务器决定；403 不证明 DRM。']
    if values.get('possible_causes'):
        lines += ['待检查原因（尚未确诊）：' + ', '.join(values['possible_causes'])]
    if values.get('resource_domains'):
        lines += ['资源域名：' + ', '.join(values['resource_domains'])]
    if values.get('resource_domain_changed'):
        lines += ['失败的派生资源与 Manifest 使用不同域名，请核对该资源的认证上下文。']
    if values.get('resource_role'):
        lines += ['失败资源角色：' + values['resource_role']]
    if values.get('header_categories'):
        lines += ['', '请求头分类（仅显示名称）：']
        for category, label in (('session', '会话上下文'), ('origin', '显式 Origin'),
                                ('resource', '原资源专用'), ('browser', '浏览器元数据'), ('custom', '自定义请求头')):
            lines.append(label + '：' + (', '.join(values['header_categories'].get(category, [])) or '--'))
        lines += ['条件请求 / 正文头不继承到派生资源。']
    if 'inherit_range' in values:
        lines += ['Range 继承：' + ('开启（Key / 分片 / 子清单；分片自身范围优先）' if values['inherit_range']
                                  else '关闭（原 Range 隔离；分片自身范围可用）')]
    if values.get('browser_metadata'):
        lines += ['', '原始浏览器元数据（已保存）：']
        lines += [f'{name}：{value}' for name, value in values['browser_metadata'].items()]
        lines += ['派生请求兼容选项：' + ('开启' if values.get('inherit_browser_metadata') else '关闭（默认不发送）')]
    if values.get('dropped_headers'):
        lines += ['该派生请求未继承：' + ', '.join(values['dropped_headers'])]
    resources = values.get('resources', [])
    if resources:
        lines += ['', f'Manifest 声明资源：{len(resources)} 个（最多显示 20 个）']
        lines += [f'{item.get("role")}：{item.get("uri")}' for item in resources[:20]]
    for index, key in enumerate(values.get('keys', []), 1):
        lines += ['', f'Key / Session Key {index}']
        for label, field in (('METHOD', 'method'), ('KEYFORMAT', 'keyformat'), ('KEYFORMATVERSIONS', 'keyformatversions'),
                             ('Key URI', 'uri'), ('Resolved Key URI', 'resolved_uri'), ('IV', 'iv')):
            lines.append(f'{label}：{key.get(field) or "--"}')
    if values.get('evidence'):
        lines += ['', '协议证据：'] + list(values['evidence'])
    for redirect in values.get('redirects', []):
        lines += ['', f'重定向 {redirect.get("status")}：{redirect.get("from")} → {redirect.get("to")}']
    lines += ['', 'Cookie / Authorization / request body 仅在内存内使用；此面板和复制内容均已脱敏。']
    return '\n'.join(lines)


class DetectionDialog(QDialog):
    def __init__(self, values, parent=None):
        super().__init__(parent)
        self.setWindowTitle('检测详情（敏感信息已隐藏）')
        self.resize(860, 620)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit(format_diagnostic(values))
        self.editor.setReadOnly(True)
        layout.addWidget(self.editor)
        self.trace = ResolverTracePanel(self)
        self.trace.set_values(values)
        layout.addWidget(self.trace)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
