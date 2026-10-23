"""验证完成后的独立后台封面任务；不修改下载队列或下载结果。"""
from collections import deque
from copy import deepcopy
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal

from utils.thumbnails import generate_thumbnail


class ThumbnailWorker(QThread):
    def __init__(self, job_id, files, validations, ffmpeg_path, parent=None):
        super().__init__(parent)
        self.job_id = job_id
        self.files = list(files)
        self.validations = deepcopy(validations)
        self.ffmpeg_path = ffmpeg_path
        self.cancel_event = Event()
        self.result = {'status': 'failed', 'items': []}

    def run(self):
        items = []
        for path, validation in zip(self.files, self.validations):
            try:
                item = generate_thumbnail(path, ffmpeg_path=self.ffmpeg_path,
                                          validation=validation, cancel_event=self.cancel_event)
            except Exception:
                # 封面附加步骤的异常不得向原下载成功事件传播。
                item = {'status': 'failed', 'path': '', 'message': '缩略图生成失败', 'attempts': []}
            items.append(item)
            if self.cancel_event.is_set():
                break
        states = {item['status'] for item in items}
        status = ('cancelled' if self.cancel_event.is_set() else 'failed' if 'failed' in states
                  else 'generated' if states & {'generated', 'existing'} else 'skipped')
        self.result = {'status': status, 'items': items}


class ThumbnailService(QObject):
    completed = Signal(int, dict)
    idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.results = {}
        self.worker = None
        self._pending = deque()
        self._closing = False

    @property
    def busy(self):
        return self.worker is not None or bool(self._pending)

    def submit(self, job_id, files, validations, ffmpeg_path):
        if (self._closing or job_id in self.results or not files or
                not isinstance(validations, list) or len(files) != len(validations) or
                not all(isinstance(item, dict) and item.get('format_name') for item in validations)):
            return False
        self.results[job_id] = {'status': 'pending', 'items': []}
        self._pending.append((job_id, list(files), deepcopy(validations), ffmpeg_path))
        self._start_next()
        return True

    def _start_next(self):
        if self.worker is not None or not self._pending or self._closing:
            return
        self.worker = ThumbnailWorker(*self._pending.popleft(), parent=self)
        self.worker.finished.connect(self._worker_finished)
        self.worker.start()

    def _worker_finished(self):
        worker = self.worker
        worker.wait()
        self.worker = None
        self.results[worker.job_id] = worker.result
        self.completed.emit(worker.job_id, worker.result)
        worker.deleteLater()
        self._start_next()
        if not self.busy:
            self.idle.emit()

    def cancel(self):
        self._closing = True
        while self._pending:
            job_id = self._pending.popleft()[0]
            self.results[job_id] = {'status': 'cancelled', 'items': []}
        if self.worker:
            self.worker.cancel_event.set()
