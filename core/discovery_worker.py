"""有界事件队列与协议探测；Chromium/界面始终留在 GUI 线程。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import queue
import threading
import time

from PySide6.QtCore import QThread, Signal

from core.browser_observer import MAX_BODY_BYTES, capture_body_limit, capture_total_limit
from core.discovery_models import NetworkObservation
from core.errors import MediaCheckCancelled, MediaCheckError
from core.media_guard import check_media
from core.progressive_mp4 import full_resource_context
from utils.security import Redactor, redact_data


def context_fingerprint(context):
    # 凭据不作为可输出的字典键或日志，只用哈希比较 ExtraInfo 到达前后的上下文。
    digest = hashlib.sha256()
    for value in (context.url, context.method, repr(sorted(context.headers.items())),
                  str(context.inherit_range), str(context.inherit_browser_metadata)):
        digest.update(value.encode('utf-8'))
        digest.update(b'\0')
    digest.update((str(context.url_resolution_policy) + '\0' + context.playlist_decoder).encode())
    digest.update(context.body or b'')
    return digest.digest()


class DiscoveryWorker(QThread):
    candidates_changed = Signal(object, str, int)
    status = Signal(str)
    resolver_result = Signal(object, int)
    diagnostics_changed = Signal(dict, int)
    batch_ready = Signal(object, int)

    def __init__(self, parent=None, probe=True, resolver_registry=None, max_body_bytes=MAX_BODY_BYTES):
        super().__init__(parent)
        self._queue = queue.Queue(maxsize=256)
        self._lock = threading.Lock()
        self._generation = 0
        self._epoch_cancel = threading.Event()
        self._stop = threading.Event()
        self._queued_bytes = 0
        self.max_body_bytes = capture_body_limit(max_body_bytes)
        self.max_queued_bytes = capture_total_limit(self.max_body_bytes)
        self.probe = probe
        self.resolver_registry = resolver_registry
        self._freeze_requested = False

    @property
    def generation(self):
        return self._generation

    def reset(self):
        with self._lock:
            self._freeze_requested = False
            self._generation += 1
            self._epoch_cancel.set()
            self._epoch_cancel = threading.Event()
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queued_bytes = 0
            self._queue.put_nowait((self._generation, 'reset', None, 0))

    def submit(self, kind, value):
        if self._stop.is_set():
            return
        size = len(value.body or b'') if kind == 'network' else sum(len(obs.body or b'') for obs in value.observations) if kind == 'page' else 0
        with self._lock:
            if self._freeze_requested:
                return
            if kind == 'network' and size > self.max_body_bytes:
                value, size = replace(value, body=None, body_complete=False), 0
            if self._queued_bytes + size > self.max_queued_bytes:
                return
            if self._queue.full():
                try:
                    _, _, _, dropped = self._queue.get_nowait()
                    self._queued_bytes -= dropped
                except queue.Empty:
                    pass
            self._queue.put_nowait((self._generation, kind, value, size))
            self._queued_bytes += size

    def freeze(self):
        # 停止接收新候选；线程继续排空已收集事件及有界协议检测。
        with self._lock:
            self._freeze_requested = True

    def cancel(self):
        self._stop.set()
        with self._lock:
            self._epoch_cancel.set()

    @staticmethod
    def _probe(context, cancel_event):
        try:
            inspection = check_media(context, cancel_event=cancel_event)
            return inspection.diagnostic(), inspection.root
        except MediaCheckCancelled:
            return None, None
        except MediaCheckError as error:
            redactor = Redactor(context.headers, [context.url, context.final_url])
            values = redact_data(error.diagnostic, redactor)
            values['probe_error'] = redactor.redact(str(error))
            values['error'] = values['probe_error']
            return values, None
        except Exception:
            # 未预期异常也不能输出可能带凭据的 repr/traceback。
            return {'error_code': 'MEDIA_CHECK_ERROR', 'probe_error': '候选检测未完成，请重新查找。'}, None

    def run(self):
        from core.media_discovery import MediaDiscoveryService, is_verified_resolver
        from core.site_resolvers import ResolverRegistry
        service = MediaDiscoveryService(max_body_bytes=self.max_body_bytes)
        registry = self.resolver_registry or ResolverRegistry()
        pending = {}
        attempted = set()
        generation = -1
        dirty, last_emit = False, 0.0
        frozen_generation = -1
        # 取消后等待最多两个已有、可取消的 HTTP 检测结束；窗口不阻塞等待。
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='media-probe') as pool:
            while not self._stop.is_set():
                try:
                    epoch, kind, value, size = self._queue.get(timeout=0.08)
                    with self._lock:
                        current_epoch = self._generation
                        if epoch == current_epoch:
                            self._queued_bytes = max(0, self._queued_bytes - size)
                    if epoch != current_epoch:
                        continue
                    if generation != epoch:
                        service.clear()
                        attempted.clear()
                        generation, dirty = epoch, True
                    if kind == 'network':
                        service.consume_network(value)
                        dirty = True
                    elif kind == 'page':
                        resolved = registry.resolve(value)
                        for diagnostic in resolved.diagnostics:
                            if diagnostic.get('excluded_urls'):
                                service.exclude_resource_role(diagnostic.get('excluded_urls', ()),
                                    diagnostic.get('classification') or diagnostic.get('resource_role'),
                                    diagnostic.get('request_id', ''))
                        for candidate in resolved.candidates:
                            service.consume_resolver(candidate)
                        self.resolver_result.emit(resolved, generation)
                        dirty = True
                    elif kind == 'resolver':
                        service.consume_resolver(value)
                        dirty = True
                    elif kind == 'dom':
                        service.consume_dom(value)
                        dirty = True
                except queue.Empty:
                    pass
                except Exception:
                    self.status.emit('部分资源未能解析，仍在分析其他候选……')

                for future, (epoch, identifier, fingerprint, context, request_id, progressive, proof_key) in list(pending.items()):
                    if not future.done():
                        continue
                    del pending[future]
                    with self._lock:
                        current_epoch = self._generation
                    candidate = service.get(identifier)
                    if (epoch != generation or epoch != current_epoch or not candidate
                            or is_verified_resolver(candidate)
                            or (getattr(candidate, 'video_track', None) and candidate.manifest_text)):
                        continue
                    current_context = full_resource_context(candidate.request_context) if progressive else candidate.request_context
                    if (context_fingerprint(current_context) != fingerprint
                            or not progressive and candidate.metadata.get('last_request_id') != request_id
                            or progressive and json.dumps(candidate.metadata.get('progressive_mp4'),sort_keys=True) != proof_key):
                        continue
                    diagnostic, root = future.result()
                    if diagnostic:
                        if progressive and not service.apply_progressive_probe(identifier, root):
                            if diagnostic.get('error_code'):
                                service.apply_probe(identifier, diagnostic)
                                dirty = True
                            continue
                        if (root and root.info.media_type in ('HLS', 'DASH')
                                and not candidate.metadata.get('body_complete')):
                            # 仅补充解析字节，始终使用浏览器原始上下文。
                            service.consume_network(NetworkObservation('probe:' + identifier,
                                context, root.http_status, root.content_type, root.data,
                                candidate.frame_id, 'Probe', timestamp=time.time(),
                                redirects=list(context.redirect_chain), body_complete=True))
                        service.apply_probe(identifier, diagnostic)
                        dirty = True

                can_probe_more = False
                if (self.probe and len(pending) < 2 and len(attempted) < 32 and frozen_generation != generation
                        and not any(is_verified_resolver(candidate) for candidate in service.candidates.values())):
                    for candidate in service.ranked(limit=128, include_low=True):
                        context = candidate.request_context
                        progressive = bool(candidate.metadata.get('progressive_mp4') and candidate.metadata.get('partial_resource')
                                           and not candidate.metadata.get('segment_mode'))
                        if progressive:
                            context = full_resource_context(context)
                        fingerprint = context_fingerprint(context)
                        request_id = None if progressive else candidate.metadata.get('last_request_id')
                        proof_key = json.dumps(candidate.metadata.get('progressive_mp4'),sort_keys=True) if progressive else None
                        key = (candidate.id, fingerprint, request_id, proof_key)
                        parent = service.get(candidate.metadata.get('parent_manifest', ''))
                        from_manifest = bool(parent and parent.confirmed and parent.context_complete)
                        if (key in attempted or is_verified_resolver(candidate)
                                or (getattr(candidate, 'video_track', None) and candidate.manifest_text)
                                or not candidate.context_complete or candidate.drm is True
                                or candidate.metadata.get('fragment_only') and not progressive
                                or context.method != 'GET' or candidate.is_ad
                                or not (candidate.confirmed or from_manifest)):
                            continue
                        can_probe_more = True
                        with self._lock:
                            if generation != self._generation:
                                break
                            cancel_event = self._epoch_cancel
                        attempted.add(key)
                        future = pool.submit(self._probe, deepcopy(context), cancel_event)
                        pending[future] = (generation, candidate.id, fingerprint, context, request_id, progressive, proof_key)
                        if len(pending) >= 2 or len(attempted) >= 32:
                            break
                now = time.monotonic()
                if dirty and now - last_emit >= 0.15:
                    with self._lock:
                        fresh = generation == self._generation
                    if fresh:
                        self.candidates_changed.emit(service.ranked(limit=128, include_low=True),
                                                     service.player_type, generation)
                        self.diagnostics_changed.emit(service.discovery_diagnostic(), generation)
                    dirty, last_emit = False, now
                if (self._freeze_requested and frozen_generation != generation and generation == self.generation
                        and self._queue.empty() and not pending and not can_probe_more):
                    frozen_generation = generation
                    self.batch_ready.emit(service.ranked(limit=128, include_low=True), generation)
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queued_bytes = 0
