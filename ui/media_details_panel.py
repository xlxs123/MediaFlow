"""下载前的媒体详情展示；只读取候选，不请求或推测媒体参数。"""
import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QGridLayout, QLabel

from ui.media_presentation import (display_text, duration_text, confidence_status,
                                   confidence_diagnostics, confidence_reasons, candidate_state_text)
from utils.security import Redactor


def positive(value):
    try:
        return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def size_text(value, estimated=True):
    size = positive(value)
    if not size:
        return '未知'
    unit = 'B'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            break
        size /= 1024
    return ('约 ' if estimated else '') + f'{size:.1f} {unit}'


def bitrate_text(value):
    number = positive(value)
    return f'{number / 1000000:.2f} Mb/s' if number and number >= 1000000 else f'{number / 1000:.0f} kb/s' if number else '未知'


class MediaDetailsPanel(QWidget):
    FIELDS = (('title', '标题'), ('source', '来源'), ('type', '类型'), ('container', '容器'),
              ('duration', '时长'), ('resolution', '分辨率'), ('fps', '帧率'),
              ('videoCodec', '视频编码'), ('audioCodec', '音频编码'), ('size', '文件大小'),
              ('videoBitrate', '视频码率'), ('audioBitrate', '音频码率'), ('confidence', '可信度'),
              ('quality', '质量评分'), ('recommendation', '推荐'), ('state', '候选状态'), ('diagnostics', '诊断原因'))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = {}
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(8, 5, 8, 5)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(4)
        for index, (key, label) in enumerate(self.FIELDS):
            name = QLabel(label + '：')
            name.setObjectName('muted')
            value = QLabel('未知')
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            row, column = divmod(index, 3)
            self.grid.addWidget(name, row, column * 2)
            self.grid.addWidget(value, row, column * 2 + 1)
            self.grid.setColumnStretch(column * 2 + 1, 1)
            self.values[key] = value
        self.clear()

    def clear(self):
        for value in self.values.values():
            value.setText('未知')
            value.setToolTip('未采集到可靠信息')

    def show_candidate(self, candidate):
        redactor = Redactor(candidate.request_context.headers, [candidate.url, candidate.final_url])
        metadata = candidate.metadata
        duration = positive(candidate.duration) or positive(metadata.get('detail_duration'))
        width, height = candidate.width or candidate.resolution[0], candidate.height or candidate.resolution[1]
        values = {
            'title': display_text(metadata.get('title'), display_text(metadata.get('page_title'))),
            'source': display_text(candidate.source_type), 'type': display_text(candidate.media_type),
            'container': display_text(candidate.container),
            'duration': duration_text(duration),
            'resolution': f'{width} × {height}' if positive(width) and positive(height) else '未知',
            'fps': f'{candidate.fps:.3g} fps' if positive(candidate.fps) else '未知',
            'videoCodec': display_text(candidate.videoCodec),
            'audioCodec': display_text(candidate.audioCodec, '存在音轨，编码未知' if metadata.get('has_audio') else '未知'),
            'videoBitrate': bitrate_text(candidate.videoBitrate), 'audioBitrate': bitrate_text(candidate.audioBitrate),
            'size': size_text(candidate.estimatedSize, metadata.get('size_is_estimate') is not False),
            'confidence': f'{candidate.confidence}/100 · {confidence_status(candidate.confidence)}',
            'quality': f'{candidate.qualityScore}/100' if metadata.get('quality_details') is not None else '待分析',
            'recommendation': display_text(metadata.get('quality_recommendation'), '待分析'),
            'state': candidate_state_text(candidate),
            'diagnostics': '\n'.join(candidate.unavailable_reasons + confidence_diagnostics(candidate)) or '无额外诊断提示',
        }
        for key, value in values.items():
            value = display_text(value)
            self.values[key].setText(redactor.redact(value)[:240])
            source = display_text((metadata.get('detail_sources') or {}).get(key), '')
            if key == 'confidence':
                source = '\n'.join(confidence_reasons(candidate))
            self.values[key].setToolTip(redactor.redact(value + ('\n依据：' + source if source else '')))
