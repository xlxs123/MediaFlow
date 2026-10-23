"""仅预览和明确确认；文件检查、复制、路径提交全部交给媒体库后台服务。"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout)


class OrganizeDialog(QDialog):
    def __init__(self, service, entries, parent=None):
        super().__init__(parent)
        self.service = service
        self.entry_ids = [entry['id'] for entry in entries]
        self.plan = None
        self._waiting_preview = False
        self._applying = False
        self.setWindowTitle('整理所选媒体 · 先预览再确认')
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        source = QLabel(f'仅整理当前选中的 {len(self.entry_ids)} 条记录；未选文件不会移动。')
        source.setWordWrap(True)
        source.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(source)
        form = QFormLayout()
        roots = sorted({str(Path(entry['path']).parent) for entry in entries if entry.get('path')})
        original = QLabel('\n'.join(roots) or '没有已完成的本地文件')
        original.setTextFormat(Qt.TextFormat.PlainText)
        original.setWordWrap(True)
        original.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow('原文件目录', original)
        row = QHBoxLayout()
        self.root_edit = QLineEdit(roots[0] if roots else '')
        row.addWidget(self.root_edit, 1)
        self.browse_button = QPushButton('选择输出目录')
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)
        form.addRow('目标根目录', row)
        self.classification = QComboBox()
        for title, value in (('不分类（只重命名或移动到根目录）', 'none'), ('按媒体类型', 'type'),
                             ('按来源网站', 'site'), ('按下载日期', 'date'),
                             ('按媒体类型与网站', 'type_site'), ('自定义相对目录', 'custom')):
            self.classification.addItem(title, value)
        form.addRow('目录分类', self.classification)
        self.directory_template = QLineEdit()
        self.directory_template.setPlaceholderText('{media_type}/{site}/{date}，最多四层相对目录')
        self.directory_template.setMaxLength(512)
        self.directory_template.setEnabled(False)
        form.addRow('自定义分类目录', self.directory_template)
        self.filename_template = QLineEdit()
        self.filename_template.setPlaceholderText('留空保留原文件名；例如 {site}_{title}_{date}.{ext}')
        self.filename_template.setMaxLength(512)
        self.filename_template.setToolTip('支持 title、site、date、time、resolution、quality、id、ext、media_type；真实扩展名保持不变。')
        form.addRow('命名模板', self.filename_template)
        self.move_thumbnails = QCheckBox('同步整理已登记的同目录同名封面（不移动未知缓存）')
        form.addRow('', self.move_thumbnails)
        layout.addLayout(form)
        self.preview_button = QPushButton('生成整理预览')
        self.preview_button.clicked.connect(self.request_preview)
        layout.addWidget(self.preview_button)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['关联文件', '原始路径', '新目标路径', '冲突 / 创建目录', '状态与原因'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        layout.addWidget(self.table, 1)
        self.status = QLabel('生成预览不会移动文件；原件保留到写入校验和数据库提交成功。')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.confirm_check = QCheckBox('我已检查以上原始路径和新目标路径，确认整理这些文件。')
        self.confirm_check.setEnabled(False)
        self.confirm_check.toggled.connect(self._update_apply)
        layout.addWidget(self.confirm_check)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.apply_button = QPushButton('确认整理所选文件')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_plan)
        buttons.addWidget(self.apply_button)
        self.close_button = QPushButton('关闭')
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        for field in (self.root_edit, self.directory_template, self.filename_template):
            field.textChanged.connect(self._invalidate)
        self.classification.currentIndexChanged.connect(self._invalidate)
        self.move_thumbnails.toggled.connect(self._invalidate)
        self.service.action_ready.connect(self._result)
        self._connected = True
        self.finished.connect(self._disconnect)

    def _disconnect(self, *_):
        if not self._connected:
            return
        self._connected = False
        try:
            self.service.action_ready.disconnect(self._result)
        except (RuntimeError, TypeError):
            pass

    def options(self):
        return {'classification': self.classification.currentData(),
                'directory_template': self.directory_template.text(),
                'filename_template': self.filename_template.text(),
                'move_thumbnails': self.move_thumbnails.isChecked()}

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, '选择整理后的根目录', self.root_edit.text())
        if path:
            self.root_edit.setText(path)

    def _invalidate(self, *_):
        self.plan = None
        self.confirm_check.setChecked(False)
        self.confirm_check.setEnabled(False)
        self.directory_template.setEnabled(self.classification.currentData() == 'custom' and not self._waiting_preview)
        self._update_apply()
        if self.table.rowCount():
            self.status.setText('设置已变化，请重新生成预览。')

    def _busy(self, busy):
        for widget in (self.root_edit, self.browse_button, self.classification, self.filename_template,
                       self.move_thumbnails, self.preview_button):
            widget.setEnabled(not busy)
        self.directory_template.setEnabled(not busy and self.classification.currentData() == 'custom')
        self.close_button.setEnabled(not self._applying)

    def request_preview(self):
        self._invalidate()
        self._waiting_preview = True
        self._busy(True)
        self.status.setText('正在后台检查所选记录、关联字幕和文件身份，不会移动文件…')
        self.service.preview_organization(self.entry_ids, self.root_edit.text().strip(), self.options())

    def _update_apply(self, *_):
        self.apply_button.setEnabled(bool(self.plan and self.plan.get('ready_count'))
                                     and self.confirm_check.isChecked() and not self._applying)

    def apply_plan(self):
        if not self.plan or not self.confirm_check.isChecked() or self._applying:
            return
        self._applying = True
        self._busy(True)
        self.confirm_check.setEnabled(False)
        self._update_apply()
        self.status.setText('正在后台整理并校验；文件写入及数据库提交成功后才移除原件…')
        self.service.apply_organization(self.plan['token'], confirmed=True)

    def _result(self, command, value):
        if command == 'organize_preview' and self._waiting_preview:
            self._waiting_preview = False
            self._busy(False)
            if value.get('error'):
                self.status.setText(value['error'])
                return
            self.plan = value
            rows = value.get('rows', [])
            self.table.setRowCount(len(rows))
            for row, item in enumerate(rows):
                directories = item.get('mkdirs', [])
                changes = ('重名，使用新名称\n' if item.get('conflict') else '无名称冲突\n')
                changes += '创建：' + '\n'.join(directories) if directories else '无需创建目录'
                state = {'ready': '待确认', 'skipped': '跳过', 'unchanged': '不变'}.get(item.get('status'), '')
                values = ({'media': '媒体', 'subtitle': '关联字幕', 'thumbnail': '本地封面'}.get(item.get('kind'), ''),
                          item.get('old', ''), item.get('new', ''), changes, state + '\n' + item.get('reason', ''))
                for column, text in enumerate(values):
                    cell = QTableWidgetItem(text)
                    cell.setToolTip(text)
                    self.table.setItem(row, column, cell)
            self.status.setText(f'已选 {value["selected_count"]} 条 · 可整理 {value["ready_count"]} 条 · '
                                f'跳过 {value["skipped_count"]} 条 · 不变 {value["unchanged_count"]} 条。'
                                '请核对完整路径；关联字幕与媒体一组提交。')
            self.confirm_check.setEnabled(bool(value.get('ready_count')))
            self._update_apply()
        elif command == 'organize_apply' and self._applying:
            self._applying = False
            self.plan = None
            self.confirm_check.setChecked(False)
            self.confirm_check.setEnabled(False)
            self._busy(False)
            self._update_apply()
            reasons = [item['reason'] for item in value.get('errors', [])] + value.get('warnings', [])
            if value.get('error'):
                reasons.insert(0, value['error'])
            self.status.setText(f'整理完成 {value.get("completed", 0)} 条 · 失败 {value.get("failed", 0)} 条。'
                                + ('\n' + '\n'.join(reasons) if reasons else '媒体库已同步实际最终路径。'))

    def reject(self):
        if self._applying:
            self.status.setText('整理仍在后台执行，请等待本组提交或回滚完成后关闭。')
            return
        super().reject()
