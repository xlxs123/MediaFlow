"""现有设置和批量导入共用的小型文件组织区域。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QLineEdit, QWidget

from core.output_organization import directory_for, make_values, normalize_options, render_filename


class OutputOptions(QWidget):
    changed = Signal()

    def __init__(self, options=None, parent=None):
        super().__init__(parent)
        settings = normalize_options(options)
        self._root = ''
        self._values = make_values()
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        self.template_edit = QLineEdit(settings['filename_template'])
        self.template_edit.setPlaceholderText('留空沿用原命名；可使用 {site}_{title}_{resolution}_{date}.{ext}')
        self.template_edit.setMaxLength(512)
        self.template_edit.setToolTip('支持 title、site、date、time、resolution、quality、id、ext、media_type。真实扩展名由下载容器决定。')
        layout.addRow('文件命名模板', self.template_edit)
        self.classification_combo = QComboBox()
        for title, value in (('不分类（保持原目录）', 'none'), ('按媒体类型', 'type'),
                             ('按来源网站', 'site'), ('按下载日期', 'date'),
                             ('按媒体类型与网站', 'type_site'), ('自定义相对目录', 'custom')):
            self.classification_combo.addItem(title, value)
        self.classification_combo.setCurrentIndex(self.classification_combo.findData(settings['classification']))
        layout.addRow('自动分类', self.classification_combo)
        self.directory_edit = QLineEdit(settings['directory_template'])
        self.directory_edit.setMaxLength(512)
        self.directory_edit.setPlaceholderText('{media_type}/{site}/{date}，最多四层相对目录')
        layout.addRow('自定义分类目录', self.directory_edit)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addRow('当前输入预览', self.preview)
        self.template_edit.textChanged.connect(self._changed)
        self.directory_edit.textChanged.connect(self._changed)
        self.classification_combo.currentIndexChanged.connect(self._changed)
        self._changed()

    def options(self):
        return normalize_options({'filename_template': self.template_edit.text(),
            'classification': self.classification_combo.currentData(),
            'directory_template': self.directory_edit.text()})

    def set_context(self, root='', values=None):
        self._root = str(root)
        if values is not None:
            self._values = dict(values)
        self.update_preview()

    def validate(self):
        directory = directory_for(self._root or '.', self.options(), self._values)
        return str(directory / render_filename(self.template_edit.text(), self._values,
                                               self._values.get('ext') or 'mp4', directory))

    def update_preview(self):
        try:
            text = self.validate()
        except ValueError as error:
            text = str(error)
        self.preview.setText(text)
        self.preview.setToolTip(text)

    def _changed(self, *_):
        self.directory_edit.setEnabled(self.classification_combo.currentData() == 'custom')
        self.update_preview()
        self.changed.emit()
