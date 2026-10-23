"""在 Qt owner 线程内调度互相独立的下载任务。"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import time
import uuid
from urllib.parse import parse_qsl

from PySide6.QtCore import QObject, Qt, Signal, Slot

from core.downloader import DownloadRequest, DownloadWorker, build_arguments, validate_filename
from core.request_context import RequestContext
from core.concurrency import GlobalConcurrencyController
from utils.security import Redactor, normalize_headers, redact_data
from core.task_store import utc_now


TERMINAL_STATES = frozenset(('completed', 'failed', 'cancelled'))


@dataclass
class DownloadJob:
    id: int
    request: DownloadRequest = field(repr=False)
    name: str
    state: str = 'queued'
    status: str = '等待下载'
    progress: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    result: dict | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=2000))
    worker: DownloadWorker | None = field(default=None, repr=False)
    media_candidate: object = field(default=None, repr=False)
    media_group: object = field(default=None, repr=False)
    batch_id: str = ''
    auto_fallback: bool = False
    retry_count: int = 0
    pause_requested: bool = False
    attempted_sources: set = field(default_factory=set, repr=False)
    restored_from_disk: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    context_error: str = ''

    @property
    def output_path(self):
        return self.request.output_path

    @property
    def request_context(self):
        return self.request.to_context()


def _job_redactor(request: DownloadRequest) -> Redactor:
    # 单个任务有独立快照及脱敏器；后续解析的新输入不会改变它。
    headers = normalize_headers(request.headers)
    if request.body:
        body = request.body.decode('utf-8', errors='replace')
        values = [body]
        values.extend(value for _, value in parse_qsl(body, keep_blank_values=True) if value)
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = None

        def collect(item):
            if isinstance(item, dict):
                for value in item.values():
                    collect(value)
            elif isinstance(item, list):
                for value in item:
                    collect(value)
            elif item is not None:
                values.append(str(item))

        collect(payload)
        for index, value in enumerate(values):
            headers[f'x-body-secret-{index}'] = value
    return Redactor(headers, [request.url])


def _directory_key(directory: str) -> str:
    # 即使在其他平台测试，也按照 Windows 的文件名大小写规则消歧。
    return str(Path(directory).expanduser().resolve()).casefold()


class DownloadQueue(QObject):
    changed = Signal(int)
    log = Signal(int, str)
    completed = Signal(int, dict)
    idle = Signal()
    persistence_changed = Signal(str)

    def __init__(self, max_concurrent=3, parent=None, worker_factory=None, max_requests=12, task_store=None):
        super().__init__(parent)
        self._validate_limit(max_concurrent)
        self.max_concurrent = max_concurrent
        self.jobs: list[DownloadJob] = []
        self._paused = True
        self._worker_factory = worker_factory or DownloadWorker
        self._workers: dict[QObject, DownloadJob] = {}
        self._redactors: dict[int, Redactor] = {}
        self._pending: deque[int] = deque()
        self._next_id = 1
        self._auto_number = 1
        self._pumping = False
        self._had_active = False
        self.concurrency = GlobalConcurrencyController(max_requests)
        from core.file_names import FileNameAllocator
        self.filename_allocator = FileNameAllocator()
        self.task_store = task_store
        self.persistence_error = ''
        self.persistence_warning = ''
        self._last_saved = 0.0
        self._shutdown_requested = False
        self._storage_cleanup_guards = 0

    def set_storage_cleanup_guard(self, active):
        """短暂的本地清理租约只阻止新任务，不中断已有下载。"""
        self._storage_cleanup_guards = max(0, self._storage_cleanup_guards + (1 if active else -1))
        if not self._storage_cleanup_guards:
            self._dispatch()

    def _check_storage_cleanup(self):
        if self._storage_cleanup_guards:
            raise ValueError('正在确认回收磁盘文件，请等待清理完成后添加或恢复任务。')

    def save_tasks(self):
        if self.task_store is None:
            return True
        succeeded = self.task_store.save(self.jobs)
        self._last_saved = time.monotonic()
        error = self.task_store.last_error
        warning = self.task_store.last_warning
        if (error, warning) != (self.persistence_error, self.persistence_warning):
            self.persistence_error, self.persistence_warning = error, warning
            self.persistence_changed.emit(error or warning)
        return succeeded

    def _persist(self, job=None, immediate=True):
        if job is not None:
            job.updated_at = utc_now()
        if self.task_store is not None and (immediate or time.monotonic() - self._last_saved >= 1.0):
            return self.save_tasks()
        return True

    def restore_tasks(self):
        if self.jobs or self._workers:
            raise ValueError('只允许向空队列恢复任务，避免重复任务和输出路径。')
        self._paused = True
        if self.task_store is None:
            return []
        records = self.task_store.load()
        self.persistence_error = self.task_store.last_error
        self.persistence_warning = self.task_store.last_warning
        for record in records:
            metadata = record['metadata']
            context_error = record.get('context_error', '')
            request = record.get('request')
            if request is None:
                # URL 占位符仅用于显示任务；context_error 会阻止调度，必须先重新分析。
                safe_request = metadata.get('request', {})
                request = DownloadRequest(url='https://mediaflow.invalid/context-required',
                                          **safe_request)
            request.concurrency = self.concurrency
            state = metadata.get('state', 'paused')
            if state not in TERMINAL_STATES and state != 'waiting_context':
                state = 'paused'
            if context_error and state != 'completed':
                state = 'waiting_context'
            try:
                if not context_error:
                    build_arguments(request, require_tools=False)
                temporary = Path(request.temporary_directory).expanduser().resolve()
                expected_parent = Path(request.workspace_output_directory or request.output_directory).expanduser().resolve() / '.stream-downloads'
                if temporary.parent != expected_parent or not temporary.name or temporary.name in ('.', '..'):
                    raise ValueError('恢复临时目录与任务目录不一致。')
            except Exception:
                context_error = '任务恢复配置无法验证，请重新分析媒体地址。'
                if state != 'completed':
                    state = 'waiting_context'
            status = {'completed': '下载完成', 'failed': '下载失败，可重试', 'cancelled': '下载已停止',
                      'waiting_context': '需要重新分析媒体地址', 'paused': '已恢复任务，等待继续下载'}.get(state, '已暂停')
            job = DownloadJob(metadata['id'], request, metadata.get('name', request.filename),
                              state=state, status=status, progress=deepcopy(metadata.get('progress', {})),
                              retry_count=metadata.get('retry_count', 0),
                              created_at=metadata.get('created_at') or utc_now(),
                              updated_at=metadata.get('updated_at') or utc_now(), context_error=context_error,
                              restored_from_disk=True)
            category = metadata.get('last_error_category', '')
            if category or context_error:
                job.result = self._empty_result(context_error or record.get('last_error') or '上次下载未完成：' + category)
                job.result['error_code'] = 'CONTEXT_UNAVAILABLE' if context_error else category
            if state == 'completed':
                job.result = {'success': True, 'cancelled': False, 'exit_code': 0,
                              'files': metadata.get('completed_files') or ([job.output_path] if job.output_path else []),
                              'error': '', 'error_code': '', 'diagnostic': {}}
            self._redactors[job.id] = _job_redactor(request)
            if job.output_path:
                self.filename_allocator.reserve(job.output_path)
            self.jobs.append(job)
            self._next_id = max(self._next_id, job.id + 1)
            self.changed.emit(job.id)
        self.persistence_changed.emit(self.persistence_error or self.persistence_warning)
        return list(self.jobs)

    def shutdown(self):
        """正常退出保留下载部分；后续 completed/finished 回调也会保存暂停状态。"""
        self._shutdown_requested = True
        self._paused = True
        for job in tuple(self.jobs):
            if job.state in ('queued', 'running'):
                self.pause_job(job.id)
        return self.save_tasks()

    @staticmethod
    def _validate_limit(value):
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError('同时下载视频数必须是 1 到 10 的整数。')

    @property
    def paused(self):
        return self._paused

    @property
    def active_count(self):
        # completed 已到达而 finished 未到达的 QThread 仍占用槽位。
        return len(self._workers)

    def get(self, job_id):
        return next((job for job in self.jobs if job.id == job_id), None)

    def _name_used(self, directory, name):
        key = _directory_key(directory)
        return any(_directory_key(job.request.output_directory) == key
                   and job.name.casefold() == name.casefold() for job in self.jobs)

    @staticmethod
    def _partial_exists(directory, name):
        prefix = name.casefold() + '.'
        try:
            return any(item.name.casefold().startswith(prefix)
                       and (item.name.casefold().endswith('.mp4')
                            or '.part' in item.name.casefold()
                            or item.name.casefold().endswith('.ytdl'))
                       for item in Path(directory).expanduser().iterdir())
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return False

    def _unique_name(self, request):
        name = validate_filename(request.filename)
        if not name:
            while True:
                candidate = f'video{self._auto_number:03d}'
                self._auto_number += 1
                if not self._name_used(request.output_directory, candidate) and not self._partial_exists(request.output_directory, candidate):
                    return candidate
        if not self._name_used(request.output_directory, name):
            return name
        number = 2
        while True:
            suffix = f' ({number})'
            candidate = name[:180 - len(suffix)].rstrip('. ') + suffix
            if not self._name_used(request.output_directory, candidate):
                return candidate
            number += 1

    def add(self, request: DownloadRequest) -> DownloadJob:
        self._check_storage_cleanup()
        snapshot = deepcopy(request)
        from core.output_organization import (directory_for, filename_limit, make_values,
                                               normalize_options, render_filename, final_directory, site_name)
        options = normalize_options(snapshot.organization)
        organized = bool(options['filename_template'] or options['classification'] != 'none' or snapshot.collection_directory)
        if organized:
            extension = snapshot.output_extension or 'mp4'
            values = snapshot.media_metadata.get('organization_values')
            if not isinstance(values, dict):
                values = make_values(snapshot.filename or snapshot.media_metadata.get('title', ''),
                    source_url=snapshot.media_metadata.get('page_url', ''),
                    site=snapshot.media_metadata.get('source_site') or site_name(source_url=snapshot.url),
                    resolution=snapshot.media_metadata.get('height', 0), extension=extension)
            snapshot.media_metadata['organization_values'] = deepcopy(values)
            target = directory_for(snapshot.output_directory, options, values, snapshot.collection_directory)
            snapshot.final_output_directory = str(target)
            snapshot.output_path = ''  # 新任务由占名器分配；恢复任务不经过add。
            if options['filename_template']:
                snapshot.filename = Path(render_filename(options['filename_template'], values, extension, target)).stem
            elif not snapshot.filename:
                snapshot.filename = Path(render_filename('', values, extension, target)).stem
            snapshot.output_extension = extension
        target_directory = final_directory(snapshot)
        build_arguments(snapshot, require_tools=False)
        if snapshot.output_extension:
            reserved = [job.name + '.' + (job.request.output_extension or 'mp4') for job in self.jobs
                        if _directory_key(final_directory(job.request)) == _directory_key(target_directory)]
            allocation = {'max_length': filename_limit(target_directory)} if organized else {}
            path = self.filename_allocator.allocate(target_directory,
                                                    snapshot.filename + '.' + snapshot.output_extension, reserved, **allocation)
            snapshot.filename, snapshot.output_path = path.stem, str(path)
        else:
            snapshot.filename = self._unique_name(snapshot)
            # 手动URL也使用固定独立工作区；不借用其他任务的 .part 状态。
            final = Path(snapshot.output_directory).expanduser().resolve() / (snapshot.filename + '.mp4')
            if final.exists():
                path = self.filename_allocator.allocate(snapshot.output_directory, final.name)
                snapshot.filename = path.stem
        snapshot.temporary_directory = str(Path(snapshot.output_directory).expanduser().resolve()
                                           / '.stream-downloads' / uuid.uuid4().hex)
        snapshot.workspace_output_directory = snapshot.output_directory
        snapshot.concurrency = self.concurrency
        job = DownloadJob(self._next_id, snapshot, snapshot.filename)
        self._next_id += 1
        self._redactors[job.id] = _job_redactor(snapshot)
        self.jobs.append(job)
        self._pending.append(job.id)
        self.changed.emit(job.id)
        self._persist(job)
        if not self._paused:
            self._dispatch()
        return job

    def add_batch(self, requests, metadata=None, start=True):
        requests = list(requests)
        if not requests or len(requests) > 500:
            raise ValueError('每批需要1到500个下载任务。')
        for request in requests:
            build_arguments(request, require_tools=False)
        was_paused = self._paused
        self._paused = True
        jobs = []
        try:
            for index, request in enumerate(requests):
                job = self.add(request)
                if metadata:
                    for key, value in metadata[index].items():
                        setattr(job, key, deepcopy(value))
                jobs.append(job)
                self.changed.emit(job.id)
        except Exception:
            for job in jobs:
                self.jobs.remove(job)
                self._pending.remove(job.id)
                self._redactors.pop(job.id, None)
                if job.output_path:
                    self.filename_allocator.release(job.output_path)
                self.changed.emit(job.id)
            self.save_tasks()
            raise
        finally:
            self._paused = was_paused
        if start:
            self.start()
        elif not self._paused:
            self._dispatch()
        self.save_tasks()
        return jobs

    def start(self):
        self._check_storage_cleanup()
        self._paused = False
        self._dispatch()

    def pause(self):
        self._paused = True
        self.save_tasks()

    def set_max_concurrent(self, value):
        self._validate_limit(value)
        self.max_concurrent = value
        self._dispatch()

    @staticmethod
    def _empty_result(error='', cancelled=False):
        return {'success': False, 'cancelled': cancelled, 'exit_code': None,
                'files': [], 'error': error, 'error_code': '', 'diagnostic': {}}

    def cancel(self, job_id):
        job = self.get(job_id)
        if not job or job.state in TERMINAL_STATES:
            return False
        job.pause_requested = False
        if job.state == 'queued':
            self._pending.remove(job.id)
            self._complete(job, self._empty_result(cancelled=True))
        elif job.state == 'running':
            job.state = 'stopping'
            job.status = '正在停止下载…'
            self.changed.emit(job.id)
            job.worker.request_stop()
        elif job.state in ('paused', 'waiting_context'):
            self._complete(job, self._empty_result(cancelled=True))
        self._persist(job)
        return True

    def pause_job(self, job_id):
        job = self.get(job_id)
        if not job or job.state not in ('queued', 'running'):
            return False
        job.pause_requested = True
        if job.state == 'queued':
            self._pending.remove(job.id)
            job.state, job.status = 'paused', '已暂停，保留未完成部分'
        else:
            job.state, job.status = 'stopping', '正在暂停，保留未完成部分…'
            job.worker.request_stop()
        self.changed.emit(job.id)
        self._persist(job)
        return True

    def resume_job(self, job_id):
        job = self.get(job_id)
        if not job or job.state != 'paused' or job.worker is not None:
            raise ValueError('任务尚未暂停完成，请稍后继续。')
        return self.retry(job_id)

    def stop_all(self):
        self.pause()
        for job in tuple(self.jobs):
            self.cancel(job.id)

    def retry(self, job_id):
        self._check_storage_cleanup()
        job = self.get(job_id)
        if not job or job.state not in ('failed', 'cancelled', 'paused', 'waiting_context'):
            raise ValueError('只能继续暂停任务或重试失败、已停止、待更新地址的任务。')
        if job.worker is not None:
            raise ValueError('下载线程仍在收尾，请稍后重试。')
        if job.context_error:
            raise ValueError(job.context_error)
        keep_progress = job.state == 'paused'
        job.restored_from_disk = False
        job.pause_requested = False
        job.retry_count += 1
        job.state = 'queued'
        job.status = '等待下载'
        if not keep_progress:
            job.progress = {}
        job.diagnostic = {}
        job.result = None
        job.logs.clear()
        self._pending.append(job.id)
        self.changed.emit(job.id)
        self._persist(job)
        self._dispatch()
        return job

    def update_context(self, job_id: int, context: RequestContext, metadata=None, media_payload=None):
        self._check_storage_cleanup()
        job = self.get(job_id)
        if not job or job.state not in ('failed', 'cancelled', 'paused', 'waiting_context') or job.worker is not None:
            raise ValueError('请先停止任务，并等待下载线程收尾，再更新媒体地址。')
        if not isinstance(context, RequestContext):
            raise ValueError('更新链接需要完整的 RequestContext。')
        previous = job.request.media_metadata
        new_metadata = deepcopy(metadata or {})
        if previous.get('video_id'):
            if metadata is None:
                from core.resume_identity import same_address_identity
                if same_address_identity(job.request.url, context.url):
                    new_metadata = deepcopy(previous)
            if (new_metadata.get('resolver_id'), new_metadata.get('video_id')) != (previous.get('resolver_id'), previous.get('video_id')):
                raise ValueError('解析器返回的视频身份与原任务不同，请重新选择同一播放页的视频。')
        # 新链接不能复用旧地址的解码正文；解析器刷新须显式传入新候选的正文。
        payload = dict(media_payload) if media_payload is not None else {
            'download_protocol': job.request.download_protocol,
            'transport_url': context.url, 'manifest_url': context.final_url,
            'manifest_text': job.request.manifest_text if context.url == job.request.url else '',
            'manifest_origin': job.request.manifest_origin if context.url == job.request.url else ''}
        updated = replace(job.request, url=context.url, method=context.method,
                          headers=deepcopy(context.headers), body=context.body,
                          context_snapshot=deepcopy(context), inherit_range=context.inherit_range,
                          inherit_browser_metadata=context.inherit_browser_metadata,
                          media_metadata=new_metadata, **payload)
        build_arguments(updated, require_tools=False)
        job.request = updated
        job.context_error = ''
        self._redactors[job.id] = _job_redactor(updated)
        job.logs.append('已更新媒体请求上下文；下载前将核对原视频身份和分片布局，保留已有部分文件。')
        self.changed.emit(job.id)
        self._persist(job)
        return job

    def retry_with_context(self, job_id, context, metadata=None, media_payload=None):
        previous_progress = deepcopy(self.get(job_id).progress) if self.get(job_id) else {}
        self.update_context(job_id, context, metadata, media_payload)
        job = self.retry(job_id)
        if job.state == 'queued':
            job.progress.update(previous_progress)
            self.changed.emit(job_id)
            self._persist(job)
        return job

    def remove_finished(self):
        for job in self.jobs:
            if job.state in TERMINAL_STATES and job.worker is None and job.output_path:
                self.filename_allocator.release(job.output_path)
        removable = {job.id for job in self.jobs
                     if job.state in TERMINAL_STATES and job.worker is None}
        self.jobs[:] = [job for job in self.jobs if job.id not in removable]
        for job_id in removable:
            self._redactors.pop(job_id, None)
            self.changed.emit(job_id)
        self.save_tasks()
        return len(removable)

    def _dispatch(self):
        if self._pumping or self._storage_cleanup_guards:
            return
        self._pumping = True
        try:
            while not self._paused and self.active_count < self.max_concurrent:
                if not self._pending:
                    break
                job = self.get(self._pending.popleft())
                if job is None or job.state != 'queued':
                    continue
                self._start_job(job)
        finally:
            self._pumping = False
        if not self.active_count and self._had_active:
            self._had_active = False
            self.idle.emit()

    def _start_job(self, job):
        worker = None
        try:
            if job.context_error:
                raise ValueError(job.context_error)
            final_path = (Path(job.output_path) if job.output_path else
                          Path(job.request.output_directory) / (job.request.filename + '.mp4'))
            commit_record = bool(job.request.resume_identity and job.request.temporary_directory
                                 and (Path(job.request.temporary_directory) / 'workspace.json').is_file())
            if final_path.exists() and not commit_record:
                result = self._empty_result('目标文件已经存在，已保留原文件；请使用新任务选择其他文件名。')
                result['error_code'] = 'FILE_EXISTS'
                self._complete(job, result)
                return
            worker = self._worker_factory(job.request, self)
            # 槽的 receiver 是队列 QObject，而非跨线程 Python lambda。
            # 同一个 sender 的 completed / finished 事件按发送次序处理。
            connection = Qt.ConnectionType.QueuedConnection
            worker.finished.connect(self._on_finished, connection)
            worker.completed.connect(self._on_completed, connection)
            worker.log.connect(self._on_log, connection)
            worker.progress.connect(self._on_progress, connection)
            worker.status.connect(self._on_status, connection)
            worker.diagnostic.connect(self._on_diagnostic, connection)
            job.worker = worker
            self._workers[worker] = job
            self._had_active = True
            job.state = 'running'
            job.status = '正在启动…'
            self.changed.emit(job.id)
            self._persist(job)
            worker.start()
        except Exception as error:
            self._complete(job, self._empty_result(f'无法启动下载线程：{type(error).__name__}: {error}'))
            if worker is not None:
                if callable(getattr(worker, 'isRunning', None)) and worker.isRunning():
                    # 极少数自定义 factory 在真正启动后抛错：仍等 finished 释放。
                    worker.request_stop()
                else:
                    self._workers.pop(worker, None)
                    job.worker = None
                    worker.deleteLater()
                    self.changed.emit(job.id)

    def _complete(self, job, result):
        safe = redact_data(deepcopy(result), self._redactors[job.id])
        job.result = safe
        if safe.get('cancelled') and job.pause_requested:
            job.state, job.status = 'paused', '已暂停，保留未完成部分'
        elif safe.get('cancelled'):
            job.state, job.status = 'cancelled', '下载已停止'
        elif safe.get('success'):
            job.state, job.status = 'completed', '下载完成'
        elif (safe.get('error_code') in ('EXPIRED_URL', 'POSSIBLE_URL_EXPIRED', 'ROU_SIGNED_URL_EXPIRED')
              or (safe.get('diagnostic') or {}).get('requires_context_refresh') is True
              or (safe.get('error_code') in ('HTTP_403', 'AUTH_REQUIRED')
                  and 'POSSIBLE_URL_EXPIRED' in (safe.get('diagnostic') or {}).get('possible_causes', []))):
            job.state, job.status = 'waiting_context', '媒体地址可能已过期，请刷新地址并继续'
        else:
            job.state, job.status = 'failed', '下载失败'
        if isinstance(safe.get('diagnostic'), dict) and safe['diagnostic']:
            job.diagnostic = safe['diagnostic']
        self.changed.emit(job.id)
        self._persist(job)
        self.completed.emit(job.id, deepcopy(safe))

    def _sender_job(self):
        return self._workers.get(self.sender())

    @Slot(str)
    def _on_log(self, message):
        job = self._sender_job()
        if job is not None:
            safe = self._redactors[job.id].redact(message)
            job.logs.append(safe)
            self.log.emit(job.id, safe)

    @Slot(dict)
    def _on_progress(self, progress):
        job = self._sender_job()
        if job is not None and job.result is None:
            safe = redact_data(deepcopy(progress), self._redactors[job.id])
            previous_recovery = (job.progress.get('recovery_state'), job.progress.get('retry_attempt'))
            # yt-dlp 的完成进度常省略分片字段；保留此前已确认的计数。
            job.progress.update({key: value for key, value in safe.items()
                                 if value is not None or key not in ('fragment_count', 'fragment_index')})
            # 子状态不改变原有 running/stopping 契约，老界面继续按同一任务更新。
            recovery = safe.get('recovery_state')
            if job.state == 'running' and recovery in ('waiting_network', 'retrying', 'recovering', 'resuming', 'restarting'):
                attempt = safe.get('retry_attempt', job.progress.get('retry_attempt', 0))
                countdown = safe.get('retry_countdown', job.progress.get('retry_countdown', 0))
                labels = {'waiting_network': '等待网络', 'retrying': '正在重试', 'recovering': '正在恢复',
                          'resuming': '正在续传', 'restarting': '正在重新下载'}
                job.status = labels[recovery]
                if recovery in ('waiting_network', 'retrying'):
                    job.status += f'（第 {attempt} 次，{countdown} 秒后重试）'
            self.changed.emit(job.id)
            self._persist(job, immediate=recovery is not None
                          and (recovery, job.progress.get('retry_attempt')) != previous_recovery)

    @Slot(str)
    def _on_status(self, status):
        job = self._sender_job()
        if job is not None and job.result is None and job.state != 'stopping':
            job.status = self._redactors[job.id].redact(status)
            self.changed.emit(job.id)
            self._persist(job, immediate=False)

    @Slot(dict)
    def _on_diagnostic(self, diagnostic):
        job = self._sender_job()
        if job is not None and job.result is None:
            job.diagnostic = redact_data(deepcopy(diagnostic), self._redactors[job.id])
            self.changed.emit(job.id)
            self._persist(job)

    @Slot(dict)
    def _on_completed(self, result):
        job = self._sender_job()
        if job is not None and job.result is None:
            self._complete(job, result)

    @Slot()
    def _on_finished(self):
        worker = self.sender()
        job = self._workers.pop(worker, None)
        if job is None:
            return
        if job.result is None:
            self._complete(job, self._empty_result('下载线程已结束，但未返回下载结果。'))
        job.worker = None
        worker.deleteLater()
        self.changed.emit(job.id)
        self._persist(job)
        self._dispatch()
