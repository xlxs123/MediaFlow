"""Updates remain separate from user data migration; all I/O runs off the UI thread."""
from copy import deepcopy
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget)

from core.update_manager import UpdateManager
from utils.build_info import runtime_identity


_STATES = {'prepared': '等待部署确认', 'deploying': '正在部署', 'deployed': '已部署，尚未检查',
    'healthy': '启动检查通过', 'health_failed': '启动检查失败', 'cancelled': '已取消',
    'failed': '失败', 'interrupted': '上次操作中断'}


def _label(text=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _error(error):
    if type(error).__name__ in ('UpdateError', 'UpdateCancelled', 'UpdateValidationError'):
        return str(error)
    if isinstance(error, (OSError, ValueError)):
        return '无法读取或保存更新文件，请检查文件格式、访问权限和磁盘空间。'
    return '更新操作未完成，请查看更新源配置或重新导入发布包。'


class _Worker(QThread):
    result = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(object)

    def __init__(self, operation, function, parent):
        super().__init__(parent)
        self.operation = operation
        self.function = function
        self.cancel = threading.Event()

    def run(self):
        try:
            self.result.emit(self.operation, self.function(self.cancel, self.progress.emit))
        except Exception as error:
            self.failed.emit(self.operation, _error(error))


class UpdateDialog(QDialog):
    shutdown_complete = Signal()
    message = Signal(str)

    def __init__(self, manager=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('更新与版本管理')
        self.resize(960, 780)
        self.setMinimumSize(700, 570)
        self.manager = None
        self.preview = None
        self.last_result = None
        self.is_closed = False
        self.busy = False
        self._closing = False
        self._worker = None
        self._settings = {}
        self._versions = []
        self._online_manifest = None
        self._startup_pending = False
        self._initialized = False
        self._build_ui()
        factory = manager or UpdateManager

        def initialize(cancel, progress):
            instance = factory() if callable(factory) else factory
            instance.recover_interrupted()
            return {'manager': instance, 'settings': instance.load_settings(),
                    'versions': instance.list_versions()}
        self._start('initialize', initialize)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        identity = runtime_identity()
        self.current_version = _label(f'当前版本：{identity["app_version"]}  ·  构建：{identity["build_id"]}')
        layout.addWidget(self.current_version)
        self.status_label = _label('正在读取更新设置…')
        layout.addWidget(self.status_label)
        self.channel_label = _label('正式发布渠道：正在读取…')
        layout.addWidget(self.channel_label)
        self.last_check = _label('最近检查：尚未检查')
        layout.addWidget(self.last_check)
        self.tabs = QTabWidget()
        page = QWidget()
        body = QVBoxLayout(page)
        form = QFormLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText('未配置在线更新源；可直接导入本地 ZIP')
        form.addRow('HTTPS 发布清单', self.source_edit)
        self.startup_check = QCheckBox('启动时检查更新（只检查，不下载或安装）')
        form.addRow('', self.startup_check)
        body.addLayout(form)
        controls = QHBoxLayout()
        self.save_button = QPushButton('保存更新设置')
        self.key_button = QPushButton('导入可信公钥…')
        self.check_button = QPushButton('检查更新')
        self.local_button = QPushButton('导入本地 ZIP…')
        self.download_button = QPushButton('下载并校验')
        for button in (self.save_button, self.key_button, self.check_button,
                       self.local_button, self.download_button):
            controls.addWidget(button)
        body.addLayout(controls)
        self.trust_label = _label('可信公钥：尚未读取；本地包内哈希不能证明发布者身份。')
        body.addWidget(self.trust_label)
        self.signature_label = _label('数字签名：尚未验证')
        self.integrity_label = _label('发布包完整性：尚未校验')
        body.addWidget(self.signature_label)
        body.addWidget(self.integrity_label)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setPlaceholderText('选择本地发布 ZIP 或检查可信更新源后，在此查看版本、路径和校验结果。')
        body.addWidget(self.preview_text, 2)
        self.release_notes = QPlainTextEdit()
        self.release_notes.setReadOnly(True)
        self.release_notes.setPlaceholderText('发布说明')
        self.release_notes.setMaximumHeight(110)
        body.addWidget(self.release_notes, 1)
        self.deploy_confirm_check = QCheckBox('我已检查预览，同意部署到独立的新版本目录')
        self.unsigned_confirm_check = QCheckBox('该本地包未经过发布者身份认证；我已独立确认来源并信任它')
        self.downgrade_confirm_check = QCheckBox('我确认安装较旧版本；用户数据不会自动降级或回滚')
        self.execute_confirm_check = QCheckBox('我信任已部署程序，允许启动该 EXE 执行有限健康检查')
        for check in (self.deploy_confirm_check, self.unsigned_confirm_check,
                      self.downgrade_confirm_check, self.execute_confirm_check):
            body.addWidget(check)
            check.toggled.connect(self._refresh_controls)
        actions = QHBoxLayout()
        self.deploy_button = QPushButton('确认部署')
        self.health_button = QPushButton('运行启动健康检查')
        self.launch_new_button = QPushButton('打开新版本')
        self.launch_previous_button = QPushButton('打开旧版本')
        for button in (self.deploy_button, self.health_button, self.launch_new_button, self.launch_previous_button):
            actions.addWidget(button)
        body.addLayout(actions)
        self.tabs.addTab(page, '检查与部署')
        versions_page = QWidget()
        versions_layout = QVBoxLayout(versions_page)
        versions_layout.addWidget(_label('版本切换会独立打开所选 EXE。旧版本、媒体和用户数据保持保留；数据迁移需另行预览确认。'))
        self.versions_table = QTableWidget(0, 4)
        self.versions_table.setHorizontalHeaderLabels(('版本 / 构建', '状态', '独立版本目录', '最近结果'))
        self.versions_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.versions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.versions_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.versions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.versions_table.horizontalHeader().setStretchLastSection(True)
        self.versions_table.verticalHeader().hide()
        versions_layout.addWidget(self.versions_table)
        self.empty_label = _label('尚无更新包或已部署版本。')
        versions_layout.addWidget(self.empty_label)
        self.refresh_button = QPushButton('刷新版本记录')
        versions_layout.addWidget(self.refresh_button)
        self.tabs.addTab(versions_page, '版本记录')
        layout.addWidget(self.tabs)
        bottom = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        bottom.addWidget(self.progress, 1)
        self.release_button = QPushButton('打开发布页面')
        self.folder_button = QPushButton('打开新版本目录')
        self.cancel_button = QPushButton('取消当前操作')
        self.close_button = QPushButton('关闭')
        for button in (self.release_button, self.folder_button, self.cancel_button, self.close_button):
            bottom.addWidget(button)
        layout.addLayout(bottom)
        self.check_button.clicked.connect(self.check_updates)
        self.local_button.clicked.connect(self._choose_local)
        self.download_button.clicked.connect(self.download_update)
        self.save_button.clicked.connect(self.save_settings)
        self.key_button.clicked.connect(self._choose_key)
        self.deploy_button.clicked.connect(self.deploy_update)
        self.health_button.clicked.connect(self.check_health)
        self.launch_new_button.clicked.connect(self.launch_new)
        self.launch_previous_button.clicked.connect(self.launch_previous)
        self.refresh_button.clicked.connect(self.refresh_versions)
        self.versions_table.itemSelectionChanged.connect(self._select_version)
        self.source_edit.textChanged.connect(self._source_changed)
        self.cancel_button.clicked.connect(self.cancel)
        self.release_button.clicked.connect(self._open_release)
        self.folder_button.clicked.connect(self._open_folder)
        self.close_button.clicked.connect(self.reject)

    def _start(self, operation, function):
        if self.busy or self._closing or self.is_closed:
            return False
        self.busy = True
        self.progress.setRange(0, 0)
        worker = _Worker(operation, function, self)
        self._worker = worker
        worker.result.connect(self._done)
        worker.failed.connect(self._failed)
        worker.progress.connect(self._progress)
        worker.finished.connect(self._finished)
        self._refresh_controls()
        worker.start()
        return True

    def _request(self, operation, method, *args, **kwargs):
        if self.manager is None:
            return False
        def execute(cancel, progress):
            if method in ('check_updates', 'prepare_local', 'download_update', 'deploy', 'check_health'):
                kwargs.update(cancel=cancel, progress=progress)
            result = getattr(self.manager, method)(*args, **kwargs)
            return {'result': result, 'settings': self.manager.load_settings(),
                    'versions': self.manager.list_versions()}
        return self._start(operation, execute)

    def _say(self, text):
        self.status_label.setText(str(text))
        self.message.emit(str(text))

    def _done(self, operation, value):
        if operation == 'initialize':
            self.manager = value['manager']
            identity = self.manager.current_identity if hasattr(self.manager, 'current_identity') else runtime_identity()
            self.current_version.setText(f'当前版本：{identity["app_version"]}  ·  构建：{identity["build_id"]}')
            self._initialized = True
        self._settings = value.get('settings', self._settings)
        self.source_edit.setText(self._settings.get('source_url', ''))
        self.startup_check.setChecked(self._settings.get('startup_check') is True)
        self.last_check.setText('最近检查：' + (self._settings.get('last_checked') or '尚未检查'))
        key_count = len(self._settings.get('trusted_keys', {}))
        self.channel_label.setText('正式发布渠道：已固定发布公钥；实际公网可用性须通过检查确认。'
            if self._settings.get('channel_state') == 'initialized' else
            '正式发布渠道：待配置更新地址和正式公钥；当前构建未完成公网发布验收。')
        self.trust_label.setText(f'可信公钥：{key_count} 个（Ed25519 / 兼容旧 RSA）' +
            ('；发布身份待初始化，请从独立可信渠道导入公钥。' if not key_count else '；清单不能增加可信密钥。'))
        self._versions = value.get('versions', [])
        self._render_versions()
        result = value.get('result')
        self.last_result = result
        if operation == 'initialize':
            self._say(self._settings.get('last_status', '尚未检查') if self._settings.get('source_url') else '未配置在线更新源，可导入本地发布 ZIP。')
        elif operation == 'check':
            self._online_manifest = deepcopy(result.get('manifest'))
            self._say(result.get('message', '检查完成'))
            self.release_notes.setPlainText((self._online_manifest or {}).get('release_notes', ''))
            self.preview_text.setPlainText(self._online_text(result))
            self.signature_label.setText('数字签名：已通过可信公钥验证' if result.get('authenticated') else '数字签名：尚未验证')
        elif operation in ('prepare', 'download', 'deploy'):
            self._set_preview(result)
            self._say(result.get('message', '操作完成'))
        elif operation == 'health':
            if self.preview:
                updated = next((r for r in self._versions if r.get('id') == self.preview.get('id')), None)
                if updated:
                    self._set_preview(updated)
            self._say(result.get('message') or ('有限启动健康检查通过。' if result.get('passed') else '启动检查失败；可打开保留的旧版本。'))
        elif operation in ('launch_new', 'launch_previous'):
            self._say(result.get('message', '已发送启动请求。'))
        elif operation in ('settings', 'keys'):
            self._online_manifest = None
            self._say('更新设置已保存。' if operation == 'settings' else '已保存独立选择并明确确认的可信公钥。')
        elif operation == 'refresh':
            self._say(f'已读取 {len(self._versions)} 条更新记录。')

    def _failed(self, operation, message):
        self.last_result = {'passed': False, 'operation': operation, 'message': message}
        self._say(message)
        if operation == 'check':
            self._online_manifest = None
            self.signature_label.setText('数字签名：检查未完成，发布信息未获信任')
        if operation in ('prepare', 'download'):
            self.preview = None
            self._reset_confirmations()
            self.integrity_label.setText('发布包完整性：未通过，不能部署')

    def _finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self.busy = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._refresh_controls()
        if self._closing:
            self._finish_close()
        elif self._startup_pending:
            self._startup_pending = False
            self.check_on_startup()

    def _progress(self, value):
        if self._closing:
            return
        self._say(value.get('message', '正在处理更新…'))
        stage = value.get('stage')
        if stage == 'signature':
            self.signature_label.setText('数字签名：正在验证')
        elif stage == 'signature_verified':
            self.signature_label.setText('数字签名：已通过可信公钥验证')
        elif stage in ('package_hash', 'verify'):
            self.integrity_label.setText('发布包完整性：正在校验 SHA-256 和文件结构')
        elif stage == 'integrity_verified':
            self.integrity_label.setText('发布包完整性：已通过；尚未部署')
        completed, total = value.get('completed', 0), value.get('total', 0)
        if isinstance(total, int) and total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(max(0, min(1000, int(completed * 1000 / total))))
            self.progress.setFormat(f'{completed:,} / {total:,}')
        else:
            self.progress.setRange(0, 0)

    def _settings_input(self):
        return {'source_url': self.source_edit.text().strip(), 'startup_check': self.startup_check.isChecked()}

    def save_settings(self):
        return self._request('settings', 'save_settings', self._settings_input())

    def check_updates(self):
        if self.manager is None:
            return False
        values = self._settings_input()
        self._online_manifest = None
        self.preview = None
        self._reset_confirmations()
        self.signature_label.setText('数字签名：尚未验证')
        self.integrity_label.setText('发布包完整性：尚未下载')
        def check(cancel, progress):
            self.manager.save_settings(values)
            result = self.manager.check_updates(cancel=cancel, progress=progress)
            return {'result': result, 'settings': self.manager.load_settings(), 'versions': self.manager.list_versions()}
        return self._start('check', check)

    def check_on_startup(self):
        if self._closing or self.is_closed:
            return False
        if not self._initialized or self.busy:
            self._startup_pending = True
            return False
        if self._settings.get('startup_check') is not True:
            return False
        self.show()
        return self.check_updates()

    def _choose_local(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 MediaFlow 发布 ZIP', '', '发布 ZIP (*.zip)')
        if path:
            self.import_local(path)

    def import_local(self, path):
        if self.busy:
            return False
        self.preview = None
        self._reset_confirmations()
        self.signature_label.setText('数字签名：等待检查本地包')
        self.integrity_label.setText('发布包完整性：正在校验')
        return self._request('prepare', 'prepare_local', str(path))

    def download_update(self):
        if not self._online_manifest:
            return False
        self.preview = None
        self._reset_confirmations()
        self.integrity_label.setText('发布包完整性：等待下载完成后校验')
        return self._request('download', 'download_update', deepcopy(self._online_manifest))

    def _choose_key(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择独立提供的可信公钥 JSON', '', '公钥 JSON (*.json)')
        if not path:
            return
        answer = QMessageBox.question(self, '信任发布者公钥',
            f'公钥文件：{path}\n请通过独立可信渠道确认该公钥归属。添加后，该密钥签署的在线发布清单将被信任。\n是否明确同意信任此公钥文件？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        def keys(cancel, progress):
            source = Path(path)
            if source.stat().st_size > 128 * 1024:
                raise ValueError('公钥文件过大')
            data = json.loads(source.read_text(encoding='utf-8-sig'))
            if not isinstance(data, dict):
                raise ValueError('公钥格式无效')
            values = dict(self.manager.load_settings().get('trusted_keys', {}))
            incoming = data.get('trusted_keys', data)
            if not isinstance(incoming, dict):
                raise ValueError('公钥格式无效')
            if any(key in values and values[key] != value for key, value in incoming.items()):
                from core.update_manager import UpdateError
                raise UpdateError('该公钥 ID 已对应其他密钥，已停止导入。请使用独立的新密钥 ID。')
            values.update(incoming)
            settings = self.manager.save_settings({'trusted_keys': values})
            return {'settings': settings, 'versions': self.manager.list_versions(), 'result': settings}
        self._start('keys', keys)

    def deploy_update(self):
        if not self.preview or not self.deploy_confirm_check.isChecked():
            return False
        if not self.preview.get('authenticated') and not self.unsigned_confirm_check.isChecked():
            return False
        if self.preview.get('is_downgrade') and not self.downgrade_confirm_check.isChecked():
            return False
        return self._request('deploy', 'deploy', self.preview['id'], confirmed=True,
            allow_unsigned=self.unsigned_confirm_check.isChecked(),
            allow_downgrade=self.downgrade_confirm_check.isChecked())

    def check_health(self):
        if not self.preview or not self.execute_confirm_check.isChecked():
            return False
        return self._request('health', 'check_health', self.preview['id'], confirmed=True)

    def _confirm_launch(self, previous=False):
        if not self.preview or self.busy:
            return False
        key = 'current_path' if previous else 'target_path'
        path = str(Path(self.preview.get(key, '')) / 'MediaFlow.exe')
        answer = QMessageBox.question(self, '打开旧版本' if previous else '打开新版本',
            f'将独立运行：\n{path}\n此操作不会回滚用户数据，也不会切换现有快捷方式。是否启动？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def launch_new(self):
        if not self.preview or self.preview.get('state') != 'healthy' or not self._confirm_launch():
            return False
        return self._request('launch_new', 'launch_new', self.preview['id'], confirmed=True)

    def launch_previous(self):
        if not self._confirm_launch(previous=True):
            return False
        return self._request('launch_previous', 'launch_previous', self.preview['id'], confirmed=True)

    def refresh_versions(self):
        return self._request('refresh', 'recover_interrupted')

    def _reset_confirmations(self):
        for check in (self.deploy_confirm_check, self.unsigned_confirm_check,
                      self.downgrade_confirm_check, self.execute_confirm_check):
            check.setChecked(False)

    def _set_preview(self, result):
        self.preview = deepcopy(result)
        self._reset_confirmations()
        self.signature_label.setText('数字签名：已通过可信公钥验证' if result.get('authenticated') else '数字签名：本地无签名包，未经身份认证')
        self.integrity_label.setText('发布包完整性：已通过；' + _STATES.get(result.get('state'), '等待部署'))
        fields = [('版本', result.get('version')), ('构建', result.get('build_id')),
            ('状态', _STATES.get(result.get('state'), result.get('state'))),
            ('发布者身份', result.get('authentication') or ('已认证' if result.get('authenticated') else '未经身份认证')),
            ('发布包大小', f'{result.get("package_size", 0):,} 字节'),
            ('解压大小', f'{result.get("unpacked_size", 0):,} 字节'),
            ('文件数量', result.get('files_count')), ('旧版本目录', result.get('current_path')),
            ('新版本目录', result.get('target_path')), ('用户数据目录', result.get('data_directory')),
            ('数据迁移', result.get('migration_state')), ('最近结果', result.get('message'))]
        self.preview_text.setPlainText('\n'.join(f'{name}：{value}' for name, value in fields if value is not None))
        self.release_notes.setPlainText(result.get('release_notes', ''))
        self.tabs.setCurrentIndex(0)
        self._refresh_controls()

    def _online_text(self, result):
        manifest = result.get('manifest') or {}
        if not manifest:
            return result.get('message', '没有可用发布清单。')
        return '\n'.join((f'可用版本：{manifest.get("version", "未知")}',
            f'构建：{manifest.get("build_id", "未知")}',
            f'发布时间：{manifest.get("published_at", "旧版清单未提供")}',
            f'发布来源：{urlsplit(self._settings.get("source_url", "")).netloc or "未配置"}',
            f'签名密钥：{manifest.get("signature", {}).get("key_id", "未提供")}',
            f'发布者身份：{result.get("authentication", "尚未认证")}',
            f'发布包大小：{manifest.get("package", {}).get("size", "未知")} 字节',
            f'最低应用版本：{manifest.get("minimum_app_version") or "未声明"}',
            '数据迁移：部署不会迁移用户数据；由迁移助手单独预览并确认。'))

    def _render_versions(self):
        self.versions_table.blockSignals(True)
        self.versions_table.setRowCount(len(self._versions))
        for row, record in enumerate(self._versions):
            values = (f'{record.get("version", "未知")} / {record.get("build_id", "")}',
                _STATES.get(record.get('state'), record.get('state', '未知')),
                record.get('target_path', ''), record.get('message', ''))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.versions_table.setItem(row, column, item)
        self.versions_table.blockSignals(False)
        self.empty_label.setVisible(not self._versions)

    def _select_version(self):
        row = self.versions_table.currentRow()
        if not self.busy and 0 <= row < len(self._versions):
            self._set_preview(self._versions[row])

    def _source_changed(self):
        self._online_manifest = None
        self._refresh_controls()

    def _release_url(self):
        return (self.preview or self._online_manifest or {}).get('release_page', '')

    def _open_release(self):
        value = self._release_url()
        try:
            url = urlsplit(value)
            if url.scheme == 'https' and url.hostname and not url.username and not url.password:
                QDesktopServices.openUrl(QUrl(value))
        except ValueError:
            self._say('发布页面地址无效。')

    def _open_folder(self):
        target = Path((self.preview or {}).get('target_path', ''))
        if self.preview and target.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        else:
            self._say('新版本目录尚不存在，请先完成部署。')

    def _refresh_controls(self):
        ready = self._initialized and not self.busy and not self._closing and not self.is_closed
        preview = self.preview or {}
        state = preview.get('state')
        for control in (self.source_edit, self.startup_check, self.save_button, self.key_button,
                        self.check_button, self.local_button, self.refresh_button, self.versions_table):
            control.setEnabled(ready)
        self.download_button.setEnabled(ready and bool(self._online_manifest))
        unsigned = bool(preview) and not preview.get('authenticated')
        downgrade = bool(preview.get('is_downgrade'))
        self.unsigned_confirm_check.setVisible(unsigned)
        self.downgrade_confirm_check.setVisible(downgrade)
        for check in (self.deploy_confirm_check, self.unsigned_confirm_check, self.downgrade_confirm_check):
            check.setEnabled(ready and bool(preview))
        self.execute_confirm_check.setEnabled(ready and state in ('deployed', 'healthy', 'health_failed'))
        self.deploy_button.setEnabled(ready and bool(preview) and state in ('prepared', 'interrupted', 'cancelled', 'failed')
            and self.deploy_confirm_check.isChecked() and (not unsigned or self.unsigned_confirm_check.isChecked())
            and (not downgrade or self.downgrade_confirm_check.isChecked()))
        self.health_button.setEnabled(ready and state in ('deployed', 'healthy', 'health_failed') and self.execute_confirm_check.isChecked())
        self.launch_new_button.setEnabled(ready and state == 'healthy')
        self.launch_previous_button.setEnabled(ready and bool(preview.get('current_path')))
        self.folder_button.setEnabled(ready and state in ('deployed', 'healthy', 'health_failed'))
        self.release_button.setEnabled(ready and bool(self._release_url()))
        self.cancel_button.setEnabled(self.busy and not self._closing)

    def cancel(self):
        if self._worker is not None:
            self._worker.cancel.set()
            self._say('正在取消当前操作，等待安全停止…')

    def shutdown(self):
        if self.is_closed:
            return
        self._closing = True
        self._startup_pending = False
        self.cancel()
        self._refresh_controls()
        if not self.busy:
            self._finish_close()

    def _finish_close(self):
        if self.is_closed:
            return
        self.is_closed = True
        super().reject()
        self.shutdown_complete.emit()

    def reject(self):
        self.shutdown()

    def closeEvent(self, event):
        if not self.is_closed:
            event.ignore()
            self.shutdown()
        else:
            super().closeEvent(event)
