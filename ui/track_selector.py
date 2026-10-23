"""只展示已获得的轨道目录；选择不更改候选和下载资格。"""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QScrollArea, QVBoxLayout, QWidget)

from core.media_tracks import (MAX_TRACKS, catalog, default_selection, language_name,
                               normalize_preferences, same_language, validate_selection)
from utils.security import Redactor


def _label(text='', parent=None):
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _positive(value):
    return value if type(value) in (int, float) and 0 < value < 2 ** 53 else 0


def _selection(value):
    if not isinstance(value, dict):
        raise ValueError('轨道选择格式无效。')
    audio, subtitles = value.get('audio_id', ''), value.get('subtitle_ids', [])
    if (not isinstance(audio, str) or not isinstance(subtitles, list)
            or any(not isinstance(identifier, str) for identifier in subtitles)):
        raise ValueError('轨道选择标识无效。')
    return {'audio_id': audio, 'subtitle_ids': list(subtitles),
            'subtitle_mode': value.get('subtitle_mode', 'none'),
            'container': value.get('container', 'original')}


class TrackSelector(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('trackSelector')
        self._candidate = None
        self._identity = None
        self._values = {'audio': [], 'subtitles': [], 'known': False}
        self._preferences = normalize_preferences()
        self._updating = False
        self._audio_needs_choice = self._subtitle_needs_choice = False
        self._notices = []
        self._redactor = Redactor()
        self._build()
        self.set_candidate(None)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(6)
        form = QFormLayout()
        self._form = form
        form.setSpacing(6)
        self.audio_combo = QComboBox()
        self.audio_combo.setObjectName('trackAudioCombo')
        self.audio_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.audio_combo.setMinimumContentsLength(22)
        form.addRow('音轨', self.audio_combo)
        self.subtitle_list = QListWidget()
        self.subtitle_list.setObjectName('trackSubtitleList')
        self.subtitle_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.subtitle_list.setMinimumHeight(48)
        self.subtitle_list.setMaximumHeight(92)
        self.subtitle_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.subtitle_list.setWordWrap(False)
        form.addRow('字幕', self.subtitle_list)
        output = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName('trackSubtitleMode')
        for name, value in (('不下载字幕', 'none'), ('保存独立字幕文件', 'sidecar'), ('封装为软字幕', 'mux')):
            self.mode_combo.addItem(name, value)
        output.addWidget(self.mode_combo, 1)
        output.addWidget(_label('容器'))
        self.container_combo = QComboBox()
        self.container_combo.setObjectName('trackContainer')
        for name, value in (('保持原有容器', 'original'), ('MP4', 'mp4'), ('MKV', 'mkv'), ('WebM', 'webm')):
            self.container_combo.addItem(name, value)
        output.addWidget(self.container_combo)
        form.addRow('字幕输出', output)
        self._output_label = form.labelForField(output)
        layout.addLayout(form)
        self.conversion_hint = _label('MP4 软字幕会将文本字幕转换为 mov_text；仅转换字幕，视频保持原有编码。')
        self.conversion_hint.setObjectName('muted')
        layout.addWidget(self.conversion_hint)
        self.summary = _label()
        self.summary.setObjectName('muted')
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        layout.addStretch()
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll)
        self._tooltip = _label('', self)
        self._tooltip.setWindowFlags(Qt.WindowType.ToolTip)
        self._tooltip.setMaximumWidth(720)
        self._tooltip.setMargin(8)
        self._tooltip.setStyleSheet('background: #fffce8; color: #263547; border: 1px solid #d7dee7;')
        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.timeout.connect(self._tooltip.hide)
        self._tooltip_views = {self.subtitle_list.viewport(): self.subtitle_list,
                               self.audio_combo.view().viewport(): self.audio_combo.view()}
        for viewport in self._tooltip_views:
            viewport.installEventFilter(self)
        self.audio_combo.currentIndexChanged.connect(self._audio_changed)
        self.audio_combo.activated.connect(self._audio_activated)
        self.subtitle_list.itemChanged.connect(self._subtitle_changed)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.container_combo.currentIndexChanged.connect(self._user_changed)

    def eventFilter(self, watched, event):
        view = self._tooltip_views.get(watched)
        if view is not None:
            if event.type() == QEvent.Type.ToolTip:
                index = view.indexAt(event.pos())
                value = index.data(Qt.ItemDataRole.ToolTipRole) if index.isValid() else ''
                if value:
                    self._tooltip.setText(str(value))
                    self._tooltip.adjustSize()
                    self._tooltip.move(event.globalPos())
                    self._tooltip.show()
                    self._tooltip_timer.start(12000)
                else:
                    self._tooltip.hide()
                return True
            if event.type() in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress):
                self._tooltip.hide()
        return super().eventFilter(watched, event)

    def _safe(self, value, limit=256):
        text = ''.join(char for char in str(value or '') if ord(char) >= 32)
        return self._redactor.redact(text)[:limit]

    def _track_text(self, track, *, audio=False):
        parts = [language_name(track.get('language'))]
        if track.get('name'):
            parts.append(self._safe(track['name']))
        if audio:
            parts.append(self._safe(track.get('codec')) or '编码未知')
            channels, rate, bitrate = (_positive(track.get(key)) for key in ('channels', 'sample_rate', 'bitrate'))
            parts.append(f'{channels:g} 声道' if channels else '声道未知')
            parts.append(f'{rate / 1000:g} kHz' if rate else '采样率未知')
            parts.append(f'{bitrate / 1000:g} kb/s' if bitrate else '码率未知')
        else:
            parts.append(self._safe(track.get('format')) or self._safe(track.get('codec')) or '格式未知')
            parts.append({True: '自动字幕', False: '人工字幕'}.get(track.get('automatic'), '生成方式未知'))
        if track.get('default'):
            parts.append('默认')
        if track.get('forced'):
            parts.append('强制')
        if not track.get('supported'):
            parts.append('不可选择：' + (self._safe(track.get('reason'), 500) or '此轨道暂不支持独立选择。'))
        return ' · '.join(parts)

    def set_candidate(self, candidate_or_none, preferences=None):
        candidate = candidate_or_none
        preferences = normalize_preferences(preferences)
        identity = (candidate.id, candidate.url) if candidate is not None else None
        raw = catalog(candidate) if candidate is not None else {'audio': [], 'subtitles': [], 'known': False}
        if (identity == self._identity and preferences == self._preferences
                and raw == getattr(self, '_catalog_snapshot', None)):
            # 后台采集会频繁刷新同一候选；目录未变时保留控件、焦点与人工选择。
            self._candidate = candidate
            self._update_summary()
            return
        self._catalog_snapshot = deepcopy(raw)
        current = None
        if candidate is not None and identity == self._identity and preferences == self._preferences:
            try:
                current = self.selection()
            except ValueError:
                pass
        self._updating = True
        self._candidate, self._identity, self._preferences = candidate, identity, preferences
        self._audio_needs_choice = self._subtitle_needs_choice = False
        self._notices = []
        self._tooltip_timer.stop()
        self._tooltip.hide()
        self.audio_combo.clear()
        self.subtitle_list.clear()
        self._values = {'audio': [], 'subtitles': [], 'known': False}
        if candidate is not None:
            self._redactor = Redactor(candidate.request_context.headers, [candidate.url, candidate.final_url])
            self._values = {}
            for kind in ('audio', 'subtitles'):
                tracks = raw.get(kind)
                tracks = tracks if isinstance(tracks, list) else []
                self._values[kind] = [dict(track) for track in tracks[:MAX_TRACKS]
                    if isinstance(track, dict) and isinstance(track.get('id'), str) and track['id']]
            self._values['known'] = raw.get('known') is True
        else:
            self._redactor = Redactor()
        self.audio_combo.addItem('原有音轨（不指定独立音轨）', '')
        for track in self._values['audio']:
            index = self.audio_combo.count()
            label = self._track_text(track, audio=True)
            self.audio_combo.addItem(label, track['id'])
            self.audio_combo.setItemData(index, label, Qt.ItemDataRole.ToolTipRole)
            if not track.get('supported'):
                self.audio_combo.model().item(index).setEnabled(False)
        for track in self._values['subtitles']:
            label = self._track_text(track)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, track['id'])
            item.setToolTip(label)
            if track.get('supported'):
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.subtitle_list.addItem(item)
        if self.subtitle_list.count() == 0:
            item = QListWidgetItem('未发现可用字幕')
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.subtitle_list.addItem(item)
        selected = None
        saved = candidate.metadata.get('track_selection') if candidate is not None else None
        for value in (current, saved):
            if value is None:
                continue
            try:
                value = _selection(value)
                validate_selection(self._values, value)
                selected = value
                break
            except (ValueError, TypeError, KeyError):
                continue
        if selected is None:
            try:
                selected = default_selection(self._values, preferences)
            except ValueError as error:
                self._notices.append(self._safe(str(error), 1000))
                language = preferences['audio_language']
                self._audio_needs_choice = bool(language and not any(track.get('supported')
                    and same_language(track.get('language'), language) for track in self._values['audio']))
                self._subtitle_needs_choice = preferences['subtitle_mode'] != 'none' and any(
                    not any(track.get('supported') and same_language(track.get('language'), language)
                            for track in self._values['subtitles']) for language in preferences['subtitle_languages'])
                selected = default_selection(self._values, {
                    'audio_language': '' if self._audio_needs_choice else preferences['audio_language'],
                    'subtitle_mode': preferences['subtitle_mode'], 'container': preferences['container']})
                if self._audio_needs_choice:
                    selected['audio_id'] = ''
        self.audio_combo.setCurrentIndex(max(0, self.audio_combo.findData(selected['audio_id'])))
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(selected['subtitle_mode']))
        self.container_combo.setCurrentIndex(self.container_combo.findData(selected['container']))
        for index in range(self.subtitle_list.count()):
            item = self.subtitle_list.item(index)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole)
                    in selected['subtitle_ids'] else Qt.CheckState.Unchecked)
        has_audio = any(track.get('supported') for track in self._values['audio'])
        self.audio_combo.setEnabled(candidate is not None and (has_audio or self._audio_needs_choice))
        self._form.setRowVisible(self.audio_combo, has_audio or self._audio_needs_choice)
        self.subtitle_list.setEnabled(candidate is not None)
        has_subtitles = any(track.get('supported') for track in self._values['subtitles']) or self._subtitle_needs_choice
        self.mode_combo.setVisible(has_subtitles)
        self._output_label.setText('字幕输出' if has_subtitles else '输出容器')
        self.mode_combo.setEnabled(candidate is not None)
        self.container_combo.setEnabled(candidate is not None)
        self._updating = False
        self._update_summary()

    def _current_selection(self):
        subtitles = []
        for index in range(self.subtitle_list.count()):
            item = self.subtitle_list.item(index)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and item.checkState() == Qt.CheckState.Checked:
                subtitles.append(item.data(Qt.ItemDataRole.UserRole))
        return {'audio_id': self.audio_combo.currentData() or '', 'subtitle_ids': subtitles,
                'subtitle_mode': self.mode_combo.currentData(), 'container': self.container_combo.currentData()}

    def selection(self):
        if self._candidate is None:
            raise ValueError('请先选择媒体候选。')
        if self._audio_needs_choice:
            raise ValueError('偏好的音轨语言未找到，请手动选择当前媒体的音轨或明确使用原有音轨。')
        if self._subtitle_needs_choice:
            raise ValueError('偏好的字幕语言未找到，未替换为其他语言；请手动选择字幕或关闭字幕。')
        value = self._current_selection()
        validate_selection(self._values, value)
        return deepcopy(value)

    def _audio_changed(self, *_):
        if self._updating:
            return
        self._audio_needs_choice = False
        self._user_changed()

    def _audio_activated(self, *_):
        # 只有明确重选原有音轨时，才解除缺失语言的等待；普通下拉变化已处理。
        if self._audio_needs_choice:
            self._audio_changed()

    def _subtitle_changed(self, item):
        if self._updating:
            return
        if not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._subtitle_needs_choice = False
            if self.mode_combo.currentData() == 'none':
                self._updating = True
                self.mode_combo.setCurrentIndex(self.mode_combo.findData('sidecar'))
                self._updating = False
        self._user_changed()

    def _mode_changed(self, *_):
        if self._updating:
            return
        if self.mode_combo.currentData() == 'none':
            self._subtitle_needs_choice = False
            self._updating = True
            for index in range(self.subtitle_list.count()):
                item = self.subtitle_list.item(index)
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(Qt.CheckState.Unchecked)
            self._updating = False
        self._user_changed()

    def _user_changed(self, *_):
        if self._updating:
            return
        self._update_summary()
        self.changed.emit()

    def _update_summary(self):
        mode, container = self.mode_combo.currentData(), self.container_combo.currentData()
        actual_container = str(getattr(self._candidate, 'container', '') or '').lower()
        mp4 = container == 'mp4' or container == 'original' and actual_container in ('mp4', 'mov,mp4,m4a,3gp,3g2,mj2')
        self.conversion_hint.setVisible(bool(mode == 'mux' and mp4))
        if self._candidate is None:
            self.summary.setText('选择媒体后可查看音轨与字幕设置。')
            return
        lines = []
        if not self._values['known']:
            lines.append('当前尚无可靠的轨道目录；下载后检查实际轨道。')
        elif not any(track.get('supported') for track in self._values['audio']):
            lines.append('未发现可独立选择的音轨；下载后检查实际轨道。')
            reasons = list(dict.fromkeys(self._safe(track.get('reason'), 300)
                           for track in self._values['audio'] if track.get('reason')))
            if reasons:
                lines.append('；'.join(reasons[:3]))
        subtitles = [track for track in self._values['subtitles'] if track.get('supported')]
        if not subtitles:
            lines.append('未发现可用字幕。')
        if self._notices:
            lines.append('偏好提示：' + '；'.join(self._notices))
        try:
            chosen = self.selection()
            audio = next((track for track in self._values['audio'] if track['id'] == chosen['audio_id']), None)
            audio_text = self._track_text(audio, audio=True) if audio else '保留原有音轨'
            selected = [track for track in subtitles if track['id'] in chosen['subtitle_ids']]
            subtitle_text = '、'.join(language_name(track.get('language')) for track in selected) if selected else '未选择'
            lines.append(f'音轨：{audio_text}\n字幕：{subtitle_text} · {self.mode_combo.currentText()} · {self.container_combo.currentText()}')
        except ValueError as error:
            lines.append(str(error))
        self.summary.setText('\n'.join(lines))
