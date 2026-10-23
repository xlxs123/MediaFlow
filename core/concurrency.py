"""所有下载任务共享的远端 HTTP 槽位，包含响应读取阶段。"""
from __future__ import annotations

import threading

from core.errors import MediaCheckCancelled


class _Lease:
    def __init__(self, controller):
        self.controller = controller
        self._released = False

    def release(self):
        with self.controller._condition:
            if not self._released:
                self._released = True
                self.controller._active -= 1
                self.controller._condition.notify_all()


class GlobalConcurrencyController:
    def __init__(self, max_requests=12):
        self._condition = threading.Condition()
        self._active = self._peak = 0
        self.set_limit(max_requests)

    def __deepcopy__(self, memo):
        # Request 的独立快照仍然共用这一份额度，不复制锁或重新创建额度。
        return self

    def set_limit(self, value):
        if type(value) is not int or not 1 <= value <= 64:
            raise ValueError('全局请求并发数必须是 1 到 64 的整数。')
        with self._condition:
            self.max_requests = value
            self._condition.notify_all()

    @property
    def active_count(self):
        with self._condition:
            return self._active

    @property
    def peak_count(self):
        with self._condition:
            return self._peak

    def acquire(self, cancel_event=None):
        with self._condition:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise MediaCheckCancelled('下载已停止。')
                if self._active < self.max_requests:
                    self._active += 1
                    self._peak = max(self._peak, self._active)
                    return _Lease(self)
                self._condition.wait(0.1)
