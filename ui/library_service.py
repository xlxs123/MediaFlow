"""媒体库后台服务；数据库、文件检查和图片读取不占用界面线程。"""
from collections import deque
from copy import deepcopy
from pathlib import Path
import os
import stat
import threading

from PySide6.QtCore import QObject, QThread, Signal, QSize, Qt, QTimer
from PySide6.QtGui import QImageReader

from core.media_history import HistoryStore, snapshot_job
from core.library_validation import file_status, revalidate_media
from core.library_organizer import LibraryOrganizer, OrganizationError


class LibraryOperationError(RuntimeError):
    pass


def _identity(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or Path(path).is_symlink():
        raise LibraryOperationError('请选择普通媒体文件，不能删除目录或链接。')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class _DatabaseWorker(QObject):
    result = Signal(str, int, object)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        self._condition = threading.Condition()
        self._pending = deque()
        self._closing = False
        self._organizer = None
        self._cleanup = None
        self._thread = threading.Thread(target=self._run, name='MediaFlowHistory', daemon=True)
        self._thread.start()

    def submit(self, command, payload=None, token=0):
        with self._condition:
            if self._closing:
                return False
            if command in ('query', 'images'):
                self._pending = deque(item for item in self._pending if item[0] != command)
            self._pending.append((command, deepcopy(payload), token))
            self._condition.notify()
        return True

    def shutdown(self):
        with self._condition:
            self._closing = True
            if self._organizer is not None:
                self._organizer.cancel_event.set()
            # 写入仍按顺序提交，退出时无需读取已离开的页面。
            self._pending = deque(item for item in self._pending if item[0] not in ('query', 'images'))
            self._condition.notify()

    def _run(self):
        store = None
        while True:
            with self._condition:
                idle = not self._pending
            if idle and store is not None:
                store.close()
                store = None
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closing)
                if not self._pending:
                    break
                command, payload, token = self._pending.popleft()
            try:
                if store is None and command != 'images':
                    store = HistoryStore(self.path)
                value = self._execute(store, command, payload)
                self.result.emit(command, token, value)
            except Exception as error:
                if store is not None:
                    store.close()
                    store = None
                # 不输出数据库内容、请求上下文或完整异常堆栈。
                text = str(error) if isinstance(error, (LibraryOperationError, OrganizationError)) or type(error).__name__ in ('HistoryStoreError', 'StorageCleanupError', 'RecycleError') else {
                    'PermissionError': '无法访问文件或历史存储，请检查权限。',
                    'FileNotFoundError': '文件已被移动或删除，历史记录已保留。',
                }.get(type(error).__name__, '媒体库操作未完成，请检查文件权限或历史存储。')
                try:
                    if command.startswith('organize_'):
                        self.result.emit(command, token, {'error': text, 'mappings': [], 'completed': 0, 'failed': 0})
                    if command == 'delete_file':
                        self.result.emit(command, token, {'completed': 0, 'failed': 1, 'errors': [text]})
                    self.error.emit(text)
                except RuntimeError:
                    break
        if store is not None:
            store.close()
        try:
            self.stopped.emit()
        except RuntimeError:
            pass

    def _execute(self, store, command, payload):
        if command.startswith('organize_'):
            if self._organizer is None:
                self._organizer = LibraryOrganizer(self.path.parent / (self.path.stem + '-organization'))
            if self._closing:
                self._organizer.cancel_event.set()
            if command == 'organize_preview':
                return self._organizer.preview(store, payload['entry_ids'], payload['root'], payload.get('options'))
            if command == 'organize_apply':
                return self._organizer.apply(store, payload['token'], confirmed=payload.get('confirmed', False))
            if command in ('organize_recover', 'organize_replay'):
                return self._organizer.recover(store)
        if command == 'record':
            return store.record(payload['snapshot'], only_if_absent=payload.get('importing', False))
        if command == 'thumbnails':
            return store.update_thumbnails(payload['task_key'], payload['items'])
        if command == 'query':
            page = store.query(**payload)
            for item in page['items']:
                if item.get('path'):
                    check = file_status(item['path'])
                    item['file_state'] = check['file_state']
                    if check['file_state'] == 'exists':
                        timestamp = (item.get('validation') or {}).get('mtime_ns')
                        changed = item.get('size') and check.get('size') != item['size']
                        changed = changed or timestamp and timestamp != check.get('mtime_ns')
                        if changed:
                            item['validation_state'] = 'unverified'
                            item['validation_reason'] = '文件大小或修改时间发生变化，请重新验证。'
                            store.update(item['id'], {'validation_state': 'unverified',
                                                     'validation_reason': item['validation_reason']})
                        item['size'] = check.get('size', item.get('size', 0))
                    item['file_message'] = check.get('message', '')
                    if not item.get('thumbnail_path'):
                        cover = Path(item['path']).with_suffix('.jpg')
                        if cover.is_file():
                            item['thumbnail_path'] = str(cover)
                            store.update(item['id'], {'thumbnail_path': str(cover)})
            return page
        if command == 'images':
            images = {}
            for item in payload[:50]:
                path = item.get('thumbnail_path', '')
                if not path:
                    continue
                reader = QImageReader(path)
                size = reader.size()
                if not size.isValid() or size.width() * size.height() > 16_000_000:
                    continue
                reader.setScaledSize(size.scaled(QSize(128, 72), Qt.AspectRatioMode.KeepAspectRatio))
                picture = reader.read()
                if not picture.isNull():
                    images[item['id']] = picture
            return images
        if command == 'remove':
            return store.remove(payload['id'])
        if command in ('details', 'validation_entry'):
            return store.get(payload['id'])
        if command == 'validated':
            entry = store.get(payload['id'])
            if not entry or entry.get('path') != payload['path']:
                return False
            fields = payload['fields']
            # 用户取消或工具暂不可用时，保留之前已取得的有效校验。
            if (fields.get('validation_state') == 'unverified' and entry.get('validation_state') == 'valid'
                    and not fields.get('file_changed') and fields.get('size', entry.get('size')) == entry.get('size')):
                fields = {k: v for k, v in fields.items() if k not in (
                    'validation_state', 'validation', 'duration', 'width', 'height', 'container',
                    'video_codec', 'audio_codec', 'media_type')}
            return store.update(payload['id'], fields)
        if command in ('open_file', 'open_folder', 'delete_preview'):
            entry = store.get(payload['id'])
            if not entry or not entry.get('path'):
                raise FileNotFoundError()
            path = Path(entry['path'])
            if command == 'open_folder':
                folder = path.parent
                if not folder.is_dir():
                    raise FileNotFoundError()
                entry['open_path'] = str(folder)
            else:
                check = file_status(path)
                if check['file_state'] == 'missing':
                    raise FileNotFoundError()
                if check['file_state'] != 'exists':
                    raise PermissionError()
                entry['open_path'] = str(path)
                if command == 'delete_preview':
                    entry['identity'] = _identity(path)
                    from core.storage_cleanup import StorageCleanup
                    from core.storage_safety import ProtectionPolicy
                    if self._cleanup is None:
                        self._cleanup = StorageCleanup(self.path.with_name(self.path.stem + '-storage-cleanup'))
                        self._cleanup.recover(store)
                    entry['delete_plan'] = self._cleanup.preview_files(store, [str(path)],
                        ProtectionPolicy(payload.get('protection', {'jobs': []}), self.path.with_name('tasks.json')))
            return entry
        if command == 'delete_file':
            from core.storage_safety import ProtectionPolicy
            if self._cleanup is None:
                raise LibraryOperationError('请先核对清理预览。')
            return self._cleanup.apply(store, payload.get('token', ''), confirmed=payload.get('confirmed') is True,
                protection=ProtectionPolicy(payload.get('protection', {'jobs': []}), self.path.with_name('tasks.json')))
        if command == 'relocate':
            check = file_status(payload['path'])
            if check['file_state'] != 'exists':
                raise FileNotFoundError()
            return store.update(payload['id'], {'path': str(Path(payload['path']).absolute()),
                'size': check.get('size', 0), 'file_state': 'exists', 'validation_state': 'unverified',
                'validation_reason': '已重新定位文件，请重新验证媒体完整性。', 'validation': {},
                'thumbnail_path': '', 'duration': 0, 'width': 0, 'height': 0,
                'container': '', 'video_codec': '', 'audio_codec': '', 'media_type': 'unknown'})
        raise ValueError('未知媒体库操作。')


class _ValidationWorker(QThread):
    def __init__(self, entry, tools, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.tools = tools
        self.cancel_event = threading.Event()
        self.result = {}

    def run(self):
        self.result = revalidate_media(self.entry['path'], cancel_event=self.cancel_event, **self.tools)
        sidecars = (self.entry.get('validation') or {}).get('subtitle_files', [])
        if sidecars and self.result.get('validation'):
            checked = []
            for item in sidecars[:32]:
                if self.cancel_event.is_set():
                    break
                status = file_status(item.get('path', ''))
                state = status['file_state']
                if state == 'exists':
                    from core.recovery_workspace import file_hash
                    state = 'unverified'
                    if item.get('sha256'):
                        try:
                            state = ('verified' if status['size'] == item.get('size') and status['size'] <= 8 * 1024 * 1024
                                     and file_hash(item['path']) == item['sha256'] else 'changed')
                        except OSError:
                            state = 'inaccessible'
                checked.append({**item, 'status': state})
            self.result['validation']['subtitle_files'] = checked


class LibraryService(QObject):
    page_ready = Signal(int, dict)
    images_ready = Signal(int, object)
    changed = Signal()
    message = Signal(str)
    action_ready = Signal(str, object)
    validation_changed = Signal(str)
    paths_moved = Signal(object)
    closed = Signal()

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self._db = _DatabaseWorker(path)
        self._db.result.connect(self._on_result)
        self._db.error.connect(self._on_error)
        self._db.stopped.connect(self._on_closed)
        self._generation = 0
        self._closing = False
        self.is_closed = False
        self.validator = None
        self._validation_pending = False
        self._validation_tools = {}
        self._job_keys = {}
        self.protection_provider = None
        self.cleanup_guard = None
        self._delete_guarded = False
        self._organization_active = False
        self.mutation_blocked = None
        # 让主窗口先连接路径同步信号，再在后台恢复上次未完成的本地整理。
        QTimer.singleShot(0, self.replay_organization_paths)

    def _on_error(self, message):
        self._validation_pending = False
        self.message.emit(message)

    def record_job(self, job, started_at='', importing=False):
        if self._closing or job.state not in ('completed', 'failed', 'cancelled', 'waiting_context'):
            return
        if importing and not job.result:
            return
        snapshot = snapshot_job(job, started_at)
        if not snapshot:
            return
        self._job_keys[job.id] = snapshot['task_key']
        self._db.submit('record', {'snapshot': snapshot, 'importing': importing})

    def record_snapshot(self, snapshot):
        """仅供未进入下载队列的批次分析失败/取消；同样通过历史存储白名单。"""
        if not self._closing and isinstance(snapshot, dict) and snapshot.get('task_state') in ('failed', 'cancelled'):
            self._db.submit('record', {'snapshot': snapshot})

    def update_thumbnails(self, job_id, result):
        task_key = self._job_keys.get(job_id)
        if task_key:
            self._db.submit('thumbnails', {'task_key': task_key, 'items': result.get('items', [])})

    def request_page(self, **filters):
        self._generation += 1
        self._db.submit('query', filters, self._generation)
        return self._generation

    def action(self, command, entry_id, **fields):
        if command in ('delete_preview', 'delete_file'):
            if command == 'delete_file':
                if fields.get('confirmed') is not True or not fields.get('token'):
                    self.message.emit('尚未明确确认清理预览，文件没有改变。')
                    return False
                if self._delete_guarded:
                    self.message.emit('正在回收文件，请稍候。')
                    return False
                self._delete_guarded = True
                if self.cleanup_guard:
                    self.cleanup_guard(True)
            try:
                fields['protection'] = self.protection_provider() if self.protection_provider else {'jobs': []}
            except Exception:
                self._release_delete_guard()
                self.message.emit('无法确认任务状态，未执行清理。')
                return False
        self._db.submit(command, {'id': entry_id, **fields})

    def _release_delete_guard(self):
        if self._delete_guarded:
            self._delete_guarded = False
            if self.cleanup_guard:
                self.cleanup_guard(False)

    def preview_organization(self, entry_ids, root, options=None):
        if not self._closing:
            self._db.submit('organize_preview', {'entry_ids': list(entry_ids), 'root': str(root),
                                                'options': options or {}})

    def apply_organization(self, token, confirmed=False):
        if not self._closing:
            if self.mutation_blocked and self.mutation_blocked():
                self.message.emit('正在回收文件，请等待清理完成后整理媒体。')
                return False
            self._organization_active = True
            self._db.submit('organize_apply', {'token': token, 'confirmed': confirmed is True})

    def replay_organization_paths(self):
        """队列恢复后可再次调用；已提交映射持久保存，接收方应幂等同步。"""
        if not self._closing:
            self._db.submit('organize_replay')

    def validate(self, entry_id, **tools):
        if self.mutation_blocked and self.mutation_blocked():
            self.message.emit('正在回收文件，请等待清理完成后验证媒体。')
            return False
        if self.validator is not None or self._validation_pending or self._closing:
            self.message.emit('已有媒体正在验证，请稍候或先取消。')
            return False
        self._validation_pending = True
        self._validation_tools = tools
        self._db.submit('validation_entry', {'id': entry_id})
        return True

    def cancel_validation(self):
        if self.validator:
            self.validator.cancel_event.set()

    def _on_result(self, command, token, value):
        if command.startswith('organize_'):
            if command == 'organize_apply':
                self._organization_active = False
            if value.get('mappings'):
                self.paths_moved.emit(value['mappings'])
            if value.get('completed') or value.get('recovered'):
                self.changed.emit()
            for warning in value.get('warnings', []):
                self.message.emit(warning)
            if command in ('organize_preview', 'organize_apply') and not self._closing:
                self.action_ready.emit(command, value)
        elif command == 'query' and token == self._generation and not self._closing:
            self.page_ready.emit(token, value)
            self._db.submit('images', value['items'], token)
        elif command == 'images' and token == self._generation and not self._closing:
            self.images_ready.emit(token, value)
        elif command == 'validation_entry':
            self._validation_pending = False
            if value and not self._closing:
                self.validator = _ValidationWorker(value, self._validation_tools, self)
                self.validator.finished.connect(self._validation_finished)
                self.validation_changed.emit('正在完整读取媒体轨道，请稍候…')
                self.validator.start()
        elif command in ('details', 'open_file', 'open_folder', 'delete_preview'):
            if not self._closing:
                self.action_ready.emit(command, value)
        elif command in ('record', 'thumbnails', 'remove', 'validated', 'delete_file', 'relocate'):
            if command == 'delete_file':
                self._release_delete_guard()
            if value and not self._closing:
                self.changed.emit()
            if command == 'delete_file':
                self.message.emit(('已移入系统回收站，历史记录已保留；清空回收站后才释放磁盘空间。'
                    if value.get('completed') else '文件未回收。') + '\n'.join(value.get('errors', [])))
            elif command == 'relocate':
                self.message.emit('已重新定位文件，请点击“重新验证”。')

    def _validation_finished(self):
        worker = self.validator
        worker.wait()
        self.validator = None
        if worker.result:
            self._db.submit('validated', {'id': worker.entry['id'], 'path': worker.entry['path'],
                                         'fields': worker.result})
            self.validation_changed.emit(worker.result.get('validation_reason') or '媒体验证完成。')
        worker.deleteLater()
        if self._closing:
            self._db.shutdown()

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        if self.validator:
            self.validator.cancel_event.set()
        else:
            self._db.shutdown()

    def _on_closed(self):
        self._release_delete_guard()
        self.is_closed = True
        self.closed.emit()
