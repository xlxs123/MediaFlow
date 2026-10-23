"""磁盘分析后台服务；文件遍历、完整哈希和回收事务均不占用界面线程。"""
from collections import deque
from copy import deepcopy
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, Signal, Slot


def _backend():
    # 后台首次操作再加载；应用启动和页面创建不检查文件或打开数据库。
    from core.media_history import HistoryStore
    from core.media_storage_scan import StorageScanner
    from core.storage_cleanup import StorageCleanup
    from core.storage_safety import ProtectionPolicy
    return HistoryStore, StorageScanner, StorageCleanup, ProtectionPolicy


def _operation_error(error):
    if isinstance(error, PermissionError):
        return '无法访问所选文件或空间分析存储，请检查访问权限后重新扫描。'
    if isinstance(error, FileNotFoundError):
        return '文件或目录已被移动或删除，请重新扫描；原有历史记录会保留。'
    if isinstance(error, OSError):
        return f'磁盘操作未完成（系统错误 {getattr(error, "winerror", None) or error.errno or "未知"}），请检查文件占用、磁盘连接和权限。'
    if type(error).__name__ in ('HistoryStoreError', 'StorageCleanupError', 'StorageSafetyError',
                               'StorageScanError', 'StorageCacheError', 'CleanupError'):
        return str(error)
    return '空间管理操作未完成，请重新扫描并检查本地存储和文件权限。'


class _StorageWorker(QObject):
    done = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(object)
    notice = Signal(str)
    stopped = Signal()

    def __init__(self, history_path):
        super().__init__()
        self.path = Path(history_path)
        self.cancel_event = threading.Event()
        self._condition = threading.Condition()
        self._pending = deque()
        self._closing = False
        self._cleanup = None
        self._recovered = False
        self._thread = threading.Thread(target=self._run, name='MediaFlowStorage', daemon=True)
        self._thread.start()

    def submit(self, operation, payload):
        with self._condition:
            if self._closing:
                return False
            self.cancel_event.clear()
            self._pending.append((operation, payload))
            self._condition.notify()
        return True

    def shutdown(self):
        with self._condition:
            self._closing = True
            self.cancel_event.set()
            self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closing)
                if not self._pending:
                    break
                operation, payload = self._pending.popleft()
            store = None
            value = None
            error_text = ''
            try:
                HistoryStore, Scanner, Cleanup, Protection = _backend()
                store = HistoryStore(self.path)
                if self._cleanup is None:
                    self._cleanup = Cleanup(self.path.with_name(self.path.stem + '-storage-cleanup'))
                if not self._recovered or operation == 'scan':
                    recovered = self._cleanup.recover(store)
                    self._recovered = True
                    if isinstance(recovered, dict) and recovered.get('errors'):
                        self.notice.emit('上次清理仍有待处理的存储更新，请重新扫描。' + '\n'.join(
                            str(item) for item in recovered['errors'][:10]))
                protection = Protection(payload['snapshot'], self.path.with_name('tasks.json'))
                if operation == 'scan':
                    scanner = Scanner(self.path.with_name(self.path.stem + '-storage-cache.sqlite3'))
                    previous = [0.0, '']

                    def report(progress):
                        phase = progress.get('phase', '')
                        now = time.monotonic()
                        if phase != previous[1] or phase == 'done' or now - previous[0] >= 0.06:
                            previous[:] = [now, phase]
                            self.progress.emit(dict(progress))

                    try:
                        value = scanner.scan(store.storage_entries(), extra_dirs=payload['extra_dirs'],
                                             cancel_event=self.cancel_event, progress=report,
                                             guard=protection.reason)
                    finally:
                        closer = getattr(scanner, 'close', None)
                        if closer:
                            closer()
                elif operation == 'preview':
                    value = self._cleanup.preview(store, payload['result'], payload['paths'], protection,
                                                  cancel_event=self.cancel_event)
                elif operation == 'cleanup':
                    value = self._cleanup.apply(store, payload['token'], confirmed=True,
                                                protection=protection, cancel_event=self.cancel_event)
            except Exception as error:
                error_text = _operation_error(error)
            finally:
                if store is not None:
                    try:
                        store.close()
                    except Exception:
                        error_text = error_text or '空间分析存储关闭失败，请重新打开软件并重新扫描。'
            # 清理 gate 只能在当前事务与数据库关闭完成后释放。
            if error_text:
                self.failed.emit(operation, error_text)
            else:
                self.done.emit(operation, value)
        self.stopped.emit()


class StorageService(QObject):
    progress = Signal(object)
    scan_ready = Signal(object)
    preview_ready = Signal(object)
    cleanup_ready = Signal(object)
    message = Signal(str)
    changed = Signal()
    busy_changed = Signal(bool)
    cleanup_guard_changed = Signal(bool)
    closed = Signal()

    def __init__(self, history_path, protection_provider=None, parent=None):
        super().__init__(parent)
        self.history_path = Path(history_path)
        self.protection_provider = protection_provider
        self.result = None
        self.busy = False
        self.is_closed = False
        self.operation = ''
        self._closing = False
        self._guarded = False
        self._worker = _StorageWorker(self.history_path)
        self._worker.done.connect(self._completed)
        self._worker.failed.connect(self._failed)
        self._worker.progress.connect(self.progress)
        self._worker.notice.connect(self.message)
        self._worker.stopped.connect(self._stopped)

    def _set_guard(self, guarded):
        if self._guarded != guarded:
            self._guarded = guarded
            self.cleanup_guard_changed.emit(guarded)

    def _submit(self, operation, payload, *, guarded=False):
        if self._closing or self.is_closed:
            self.message.emit('空间管理服务正在关闭，请重新打开软件后操作。')
            return False
        if self.busy:
            self.message.emit('空间管理正在处理上一项操作，请等待完成或取消扫描。')
            return False
        if guarded:
            self._set_guard(True)
        try:
            # provider 只在当前 UI 调用栈执行；后台线程不读取 Qt 下载队列。
            payload['snapshot'] = deepcopy(self.protection_provider() if self.protection_provider else {'jobs': []})
        except Exception:
            self._set_guard(False)
            self.message.emit('无法取得当前下载任务的安全状态，已停止操作。请等待任务状态稳定后重试。')
            return False
        self.busy = True
        self.operation = operation
        if not self._worker.submit(operation, payload):
            self.busy = False
            self.operation = ''
            self._set_guard(False)
            self.message.emit('空间管理后台已关闭，操作未执行。')
            return False
        self.busy_changed.emit(True)
        return True

    def start_scan(self, extra_dirs=()):
        paths = tuple(str(path) for path in extra_dirs if str(path).strip())
        return self._submit('scan', {'extra_dirs': paths})

    def cancel(self):
        if self.busy:
            self._worker.cancel_event.set()

    def preview_cleanup(self, selected_paths):
        result = self.result
        if not result or not result.get('complete') or result.get('cancelled'):
            self.message.emit('请先完成一次完整扫描；取消或未完成的扫描不能用于清理。')
            return False
        paths = tuple(dict.fromkeys(str(path) for path in selected_paths if str(path)))
        if not paths:
            self.message.emit('请先明确勾选待移入回收站的副本，并为每组保留至少一份文件。')
            return False
        return self._submit('preview', {'result': result, 'paths': paths})

    def apply_cleanup(self, token, confirmed=False):
        if confirmed is not True:
            self.message.emit('尚未确认清理，文件没有改变。请先核对完整路径并勾选确认框。')
            return False
        if not isinstance(token, str) or not token:
            self.message.emit('清理预览已失效，请重新生成预览。')
            return False
        return self._submit('cleanup', {'token': token}, guarded=True)

    def shutdown(self):
        if not self._closing and not self.is_closed:
            self._closing = True
            self._worker.shutdown()

    def _finish(self):
        self.busy = False
        self.operation = ''
        self._set_guard(False)
        self.busy_changed.emit(False)

    @Slot(str, object)
    def _completed(self, operation, value):
        if operation == 'scan':
            self.result = value
        elif operation == 'cleanup':
            # 执行后的任何旧扫描都不再具备清理资格。
            if self.result:
                self.result = dict(self.result, complete=False)
        self._finish()
        if operation == 'scan':
            self.scan_ready.emit(value)
        elif operation == 'preview':
            self.preview_ready.emit(value)
        elif operation == 'cleanup':
            self.changed.emit()
            self.cleanup_ready.emit(value)

    @Slot(str, str)
    def _failed(self, operation, message):
        self._finish()
        self.message.emit(message)
        if operation == 'scan':
            self.result = {'complete': False, 'cancelled': False, 'files': [], 'groups': [],
                           'stats': {}, 'issues': [{'path': '', 'reason': message}], 'scopes': []}
            self.scan_ready.emit(self.result)
        elif operation == 'preview':
            self.preview_ready.emit({'token': '', 'rows': [], 'count': 0, 'bytes': 0, 'errors': [message]})
        elif operation == 'cleanup':
            if self.result:
                self.result = dict(self.result, complete=False)
            self.cleanup_ready.emit({'completed': 0, 'failed': 1, 'bytes': 0,
                                     'paths': [], 'errors': [message], 'needs_rescan': True})

    @Slot()
    def _stopped(self):
        self._finish()
        self.is_closed = True
        self.closed.emit()
