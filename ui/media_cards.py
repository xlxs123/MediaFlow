"""轻量 Qt 卡片，仅负责展示与界面信号，不发请求或改变下载状态。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
                              QCheckBox, QComboBox, QProgressBar)

from ui.media_presentation import display_text, confidence_status, reason_text
from utils.security import Redactor


CARD_STYLES = '''
QFrame#mediaCard { background: white; border: 1px solid #e0e7ef; border-radius: 12px; }
QFrame#mediaCard[selected="true"] { background: #f0f9f8; border: 1px solid #15958c; }
QLabel#cardTitle { font-size: 14px; font-weight: 600; color: #172b42; }
QLabel#thumbnail { background: #e9eff5; color: #7d8fa4; border-radius: 8px; font-size: 12px; }
QLabel#cardMeta { color: #6c7c90; font-size: 12px; }
QLabel#confidence { border-radius: 6px; padding: 5px 9px; font-size: 12px; font-weight: 600; }
QLabel#confidence[tone="high"] { background: #def4ec; color: #167558; }
QLabel#confidence[tone="medium"] { background: #fff1d4; color: #946510; }
QLabel#confidence[tone="low"] { background: #f0f2f5; color: #768296; }
QLabel#emptyState { background: white; color: #64758b; border: 1px dashed #ccd9e5; border-radius: 12px; padding: 23px; font-size: 15px; }
QScrollArea#cardScroll { background: transparent; border: none; }
QScrollArea#cardScroll > QWidget > QWidget { background: transparent; }
QComboBox { background: white; border: 1px solid #dce4ed; border-radius: 6px; padding: 5px 8px; min-height: 20px; }
'''


def text_label(text='', name='cardMeta'):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


class CardFrame(QFrame):
    selected = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('mediaCard')
        self.setProperty('selected', False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, value):
        if self.property('selected') != bool(value):
            self.setProperty('selected', bool(value))
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.selected.emit()
        super().mousePressEvent(event)

    def thumbnail_area(self):
        label = text_label('▶\n暂无预览', 'thumbnail')
        label.setFixedSize(106, 72)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setToolTip('候选阶段不请求网站封面；下载完成并验证后生成本地视频封面。')
        return label


class CandidateCard(CardFrame):
    checked = Signal(bool)
    quality_changed = Signal(str)
    download_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(14)
        self.checked_box = QCheckBox()
        self.checked_box.setToolTip('加入批量下载选择')
        self.checked_box.toggled.connect(self.checked.emit)
        row.addWidget(self.checked_box)
        self.thumbnail = self.thumbnail_area()
        row.addWidget(self.thumbnail)
        content = QVBoxLayout()
        content.setSpacing(5)
        self.title_label = text_label(name='cardTitle')
        self.source_label = text_label()
        self.meta_label = text_label()
        self.state_label = text_label()
        self.reasons_label = text_label()
        for label in (self.title_label, self.source_label, self.meta_label, self.state_label, self.reasons_label):
            content.addWidget(label)
        row.addLayout(content, 1)
        actions = QVBoxLayout()
        actions.setSpacing(8)
        self.confidence_label = text_label(name='confidence')
        self.confidence_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.quality_combo = QComboBox()
        self.quality_combo.setMaximumWidth(210)
        self.quality_combo.currentIndexChanged.connect(self._quality_changed)
        self.download_button = QPushButton('下载')
        self.download_button.setObjectName('primary')
        self.download_button.clicked.connect(self.download_requested.emit)
        for widget in (self.confidence_label, self.quality_combo, self.download_button):
            actions.addWidget(widget)
        row.addLayout(actions)

    def _quality_changed(self, _):
        value = self.quality_combo.currentData()
        if value:
            self.quality_changed.emit(value)

    def update_display(self, columns, reasons, choices, candidate_id, checked, checkable, downloadable,
                       quality_summary='', quality_reasons=(), lifecycle_status='', unavailable_reasons=()):
        redactor = Redactor(urls=[str(columns[8])] if len(columns) > 8 else [])
        safe_text = lambda value: redactor.redact(display_text(value))
        title = safe_text(columns[1])
        self.title_label.setText(title[:180])
        self.title_label.setToolTip(title)
        self.source_label.setText('来源：' + safe_text(columns[7]))
        self.meta_label.setText('  ·  '.join((safe_text(columns[4]), '时长：' + safe_text(columns[3]),
                                              safe_text(columns[2]), safe_text(columns[5]))))
        if quality_summary:
            self.meta_label.setText(self.meta_label.text() + '\n' + safe_text(quality_summary))
        self.meta_label.setToolTip('\n'.join(safe_text(reason) for reason in quality_reasons))
        self.state_label.setText(safe_text(lifecycle_status) if lifecycle_status else '')
        self.state_label.setVisible(bool(lifecycle_status))
        blocked = '\n'.join(safe_text(reason) for reason in unavailable_reasons)
        self.state_label.setToolTip(blocked or '已确认完整媒体；低可信度可手动确认后下载')
        if blocked:
            self.state_label.setText(self.state_label.text() + '\n⚠ ' + blocked.splitlines()[0])
        self.download_button.setToolTip(blocked or ('确认媒体信息后下载' if '需确认' in lifecycle_status else '下载当前媒体'))
        rendered_reasons = []
        for value in reasons:
            text = display_text(value, '未检测到评分依据')
            prefix = text[:2] if text.startswith(('✓ ', '⚠ ')) else ''
            text = text[2:] if prefix else text
            if text.startswith('时长有效') and not prefix:
                prefix = '✓ '
            rendered_reasons.append(redactor.redact(prefix + reason_text(text)))
        self.reasons_label.setText('  ·  '.join(rendered_reasons[:3]))
        self.reasons_label.setToolTip('\n'.join(rendered_reasons))
        try:
            score = max(0, min(100, int(str(columns[6]).split('/', 1)[0])))
        except (ValueError, TypeError, OverflowError):
            score = 0
        status = confidence_status(score)
        self.confidence_label.setText(f'可信度 {score}/100 · {status}')
        self.confidence_label.setToolTip('\n'.join(rendered_reasons))
        tone = 'high' if score >= 80 else 'medium' if score >= 40 else 'low'
        if self.confidence_label.property('tone') != tone:
            self.confidence_label.setProperty('tone', tone)
            self.confidence_label.style().unpolish(self.confidence_label)
            self.confidence_label.style().polish(self.confidence_label)
        self.checked_box.blockSignals(True)
        self.checked_box.setChecked(checked)
        self.checked_box.setEnabled(checkable)
        self.checked_box.blockSignals(False)
        self.download_button.setEnabled(downloadable)
        self.quality_combo.blockSignals(True)
        if [(self.quality_combo.itemText(i), self.quality_combo.itemData(i))
                for i in range(self.quality_combo.count())] != choices:
            self.quality_combo.clear()
            for label, value in choices:
                self.quality_combo.addItem(label, value)
        self.quality_combo.setCurrentIndex(max(0, self.quality_combo.findData(candidate_id)))
        self.quality_combo.setVisible(len(choices) > 1)
        self.quality_combo.blockSignals(False)


class TaskCard(CardFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(16)
        self.thumbnail = self.thumbnail_area()
        row.addWidget(self.thumbnail)
        content = QVBoxLayout()
        header = QHBoxLayout()
        self.name_label = text_label(name='cardTitle')
        self.state_label = text_label()
        header.addWidget(self.name_label, 1)
        header.addWidget(self.state_label)
        content.addLayout(header)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        content.addWidget(self.progress_bar)
        self.speed_label = text_label()
        content.addWidget(self.speed_label)
        self.completion_label = text_label()
        self.completion_label.hide()
        content.addWidget(self.completion_label)
        row.addLayout(content, 1)

    def show_thumbnail_result(self, text, path=''):
        self.completion_label.setText(text)
        self.completion_label.setVisible(bool(text))
        if path:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                self.thumbnail.setPixmap(pixmap.scaled(self.thumbnail.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation))
                self.thumbnail.setToolTip(path)

    def update_display(self, values, state, percent):
        self.name_label.setText(values[0])
        self.state_label.setText(values[1])
        self.speed_label.setText(f'速度 {values[3]}  ·  分片 {values[4]}  ·  剩余 {values[5]}')
        if state == 'running' and type(percent) not in (int, float):
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(1000 if state == 'completed' else
                                      round(max(0, min(100, percent)) * 10) if type(percent) in (int, float) else 0)
        self.progress_bar.setFormat(values[2] if values[2] != '--' else '等待开始')
