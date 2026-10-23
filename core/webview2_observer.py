"""Native WebView2 CDP transport; observation parsing remains in the Qt observer."""
from __future__ import annotations

import json
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket

from core.browser_observer import BrowserNetworkObserver


class _NativeCdpSocket(QObject):
    textMessageReceived = Signal(str)

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self._open = True
        page.cdpMessage.connect(self._event)

    def state(self):
        return (QAbstractSocket.SocketState.ConnectedState if self._open and self.page.is_ready
                else QAbstractSocket.SocketState.UnconnectedState)

    def _event(self, value):
        if self._open:
            self.textMessageReceived.emit(json.dumps(value, ensure_ascii=True))

    def sendTextMessage(self, text):
        if not self._open:
            return
        message = json.loads(text)

        def received(value, ok):
            if not self._open:
                return
            response = value if ok and isinstance(value, dict) else {'id': message['id'], 'error': {'message': 'CDP_COMMAND_FAILED'}}
            self._event(response)
        self.page.send_cdp(message, received)

    def abort(self):
        self._open = False
        try:
            self.page.cdpMessage.disconnect(self._event)
        except (TypeError, RuntimeError):
            pass


class WebView2NetworkObserver(BrowserNetworkObserver):
    def _network_event(self, method, params, session):
        super()._network_event(method, params, session)
        request_id = str(params.get('requestId', ''))[:256]
        root = self._store.get('', request_id)
        if root is None or root.hops or not (root.extras or root.response_extras):
            return
        # WebView2 can deliver a worker's ExtraInfo on the parent Core while
        # request/response events carry SessionId. Match the actual requestId,
        # only after its response confirms ExtraInfo, never by URL/domain.
        owners = [record for (key_session, key_id), record in self._store.records.items()
                  if key_id == request_id and record.hops]
        if len(owners) != 1:
            return
        record = owners[0]
        hop = record.hops[-1]
        if (self._session_types.get(record.session) not in ('worker', 'shared_worker', 'service_worker')
                or not hop.response_seen or hop.expected_extra is not True):
            return
        record.extras.extend(root.extras)
        record.response_extras.extend(root.response_extras)
        root.extras.clear()
        root.response_extras.clear()
        self._store.records.pop(('', request_id), None)
        self._store._reconcile(record)
        self._network_counts['worker_extra_info_aligned'] = self._network_counts.get('worker_extra_info_aligned', 0) + 1
        observation = self._store.snapshot(record)
        if observation is not None:
            self._emit_observation(observation)

    def start(self):
        if self._active:
            return
        self.reset()
        self._active = True
        self._enabled = False
        self._page_id = self.page.page_id
        self._deadline = time.monotonic() + 30
        self._connect_timeout.start(30000)
        self._command_timer.start()
        self.status.emit('正在连接 Microsoft Edge WebView2…')
        socket = _NativeCdpSocket(self.page, self)
        self._socket = socket
        socket.textMessageReceived.connect(lambda text: self._socket_callback(socket, self._receive_text, text))
        self.page.backendError.connect(lambda message: self._backend_failed(socket, message))
        if self.page.is_ready:
            QTimer.singleShot(0, lambda: self._socket_callback(socket, self._connected))
        else:
            self.page.ready.connect(lambda: self._socket_callback(socket, self._connected))

    def _backend_failed(self, socket, message):
        if self._active and socket is self._socket:
            self.error.emit(message)
            self.stop()

    def network_diagnostics(self):
        value = super().network_diagnostics()
        value['backend'] = 'WebView2 原生 CDP（进程管道）'
        return value
