"""将独立视频转成既有队列任务；下载与续传仍由 DownloadWorker 完成。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import uuid

from PySide6.QtCore import QObject, Signal

from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest, build_arguments
from core.file_names import sanitize_filename
from core.media_groups import available_candidate, ordered_candidates, quality_key
from utils.security import Redactor


def output_extension(candidate):
    selected = candidate.metadata.get('track_output_extension')
    if selected in ('mp4', 'mkv', 'webm'):
        return selected
    if candidate.media_type in ('HLS', 'DASH'):
        return 'mp4'
    explicit = str(candidate.metadata.get('output_extension') or candidate.metadata.get('container') or '').lower().lstrip('.')
    if explicit in ('mp4', 'webm', 'mkv', 'm4a', 'mp3', 'opus', 'ogg', 'wav', 'flac', 'aac'):
        return explicit
    mime = candidate.content_type.lower().split(';', 1)[0].strip()
    return {'video/webm': 'webm', 'audio/webm': 'webm', 'video/x-matroska': 'mkv',
            'audio/mp4': 'm4a', 'audio/mpeg': 'mp3', 'audio/opus': 'opus',
            'audio/ogg': 'ogg', 'audio/wav': 'wav', 'audio/x-wav': 'wav',
            'audio/flac': 'flac', 'audio/aac': 'aac'}.get(mime, 'mp4')


def quality_sources(group):
    """DASH 质量来自实际 MPD 表示；HLS 使用已验证的变体。"""
    values = ordered_candidates(group)
    expanded = []
    for candidate in values:
        representations = candidate.metadata.get('representations', []) if candidate.media_type == 'DASH' else []
        video_reps = [rep for rep in representations if isinstance(rep, dict) and rep.get('height', 0) > 0]
        if video_reps:
            for rep in video_reps:
                variant = deepcopy(candidate)
                variant.id += ':dash:' + str(rep.get('id', rep['height']))
                variant.resolution = (int(rep.get('width', 0)), int(rep['height']))
                variant.bandwidth = int(rep.get('bandwidth', 0))
                variant.metadata['format_selector'] = (f'bestvideo[height={variant.resolution[1]}]+bestaudio/'
                                                       f'best[height={variant.resolution[1]}]')
                expanded.append(variant)
        elif candidate.manifest_type != 'Master':
            expanded.append(candidate)
    if not expanded:
        expanded = values
    # 同质量多协议/临时URL只输出一份，最高质量的实际上下文优先。
    expanded.sort(key=quality_key, reverse=True)
    unique, seen = [], set()
    for candidate in expanded:
        key = candidate.resolution[1] or (candidate.url, candidate.metadata.get('format_selector', ''))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


class DownloadManager(QObject):
    quality_fallback_required = Signal(int, str, str)
    refresh_required = Signal(int)

    def __init__(self, queue=None, parent=None):
        super().__init__(parent)
        self.queue = queue if queue is not None else DownloadQueue(parent=self)
        self._offered = set()
        self._refresh_attempted = set()
        self._refresh_counts = {}
        self.queue.changed.connect(self._job_changed)

    @staticmethod
    def _request(candidate, group, template, all_qualities=False):
        from core.media_tracks import apply_selection
        candidate = apply_selection(candidate)
        context = deepcopy(candidate.request_context)
        extension = output_extension(candidate)
        title = (group.title or candidate.metadata.get('title') or candidate.metadata.get('page_title')
                 or candidate.metadata.get('video_id') or 'media_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
        title = Redactor(context.headers, [context.url, context.final_url]).redact(str(title))
        if all_qualities:
            label = candidate.resolution_label if candidate.resolution[1] else candidate.media_type
            title += f' [{label}]'
        filename = Path(sanitize_filename(title + '.' + extension)).stem
        metadata = deepcopy(candidate.metadata)
        metadata.setdefault('page_url', group.source_page)
        metadata['media_group_id'] = group.id
        metadata['all_qualities'] = bool(all_qualities)
        from core.output_organization import make_values
        metadata['organization_values'] = make_values(title, source_url=group.source_page,
            site=metadata.get('source_site', ''), media_id=metadata.get('video_id', ''),
            resolution=candidate.resolution, quality=metadata.get('quality_label') or candidate.resolution_label,
            extension=extension, media_type=metadata.get('media_kind', ''))
        return replace(template, url=context.url, method=context.method, headers=deepcopy(context.headers),
                       body=context.body, context_snapshot=context, inherit_range=context.inherit_range,
                       inherit_browser_metadata=context.inherit_browser_metadata,
                       filename=filename, output_extension=extension, output_path='', temporary_directory='',
                       final_output_directory='',
                       format_selector=candidate.metadata.get('format_selector', ''),
                       media_metadata=metadata, resume_identity=None, **candidate.download_payload())

    def add_batch(self, groups, template: DownloadRequest, all_qualities=False, auto_fallback=False):
        batch_id = uuid.uuid4().hex
        requests, metadata = [], []
        for group in groups:
            if not group.selected or group.is_ad:
                continue
            selected = quality_sources(group) if all_qualities else [group.selected_candidate]
            for candidate in selected:
                if candidate is None or not available_candidate(candidate):
                    continue
                request = self._request(candidate, group, template, all_qualities)
                if candidate.media_type in ('HLS', 'DASH') and not request.ffmpeg_path:
                    raise ValueError('批量 HLS / DASH 输出需要 FFmpeg，请先完成工具检测。')
                build_arguments(request)
                requests.append(request)
                metadata.append({'media_candidate': candidate, 'media_group': group,
                                 'batch_id': batch_id, 'auto_fallback': bool(auto_fallback and not all_qualities),
                                 'attempted_sources': {candidate.id}})
        if not requests:
            raise ValueError('请勾选至少一个已确认可下载的独立视频。')
        return self.queue.add_batch(requests, metadata, start=True)

    def next_quality(self, job):
        if not job.media_group or not job.media_candidate or job.request.media_metadata.get('all_qualities'):
            return None
        current = job.media_candidate.resolution[1]
        for candidate in quality_sources(job.media_group):
            if (candidate.id not in job.attempted_sources and candidate.resolution[1]
                    and current and candidate.resolution[1] < current):
                selection = job.request.media_metadata.get('track_selection')
                if selection:
                    from core.media_tracks import apply_selection
                    try:
                        candidate = apply_selection(candidate, selection)
                    except ValueError:
                        continue
                return candidate
        return None

    def _job_changed(self, job_id):
        job = self.queue.get(job_id)
        if job is None or job.worker is not None:
            return
        if getattr(job, 'restored_from_disk', False):
            # Restoring a signed task must never start hidden browser refresh/download.
            return
        if job.state == 'waiting_context':
            refresh_key = (job.id, job.request.url)
            if (refresh_key not in self._refresh_attempted and self._refresh_counts.get(job.id, 0) < 3
                    and (job.batch_id or job.request.media_metadata.get('resolver_verified') is True)
                    and job.request.media_metadata.get('resolver_id')
                    and job.request.media_metadata.get('page_url')):
                self._refresh_attempted.add(refresh_key)
                self._refresh_counts[job.id] = self._refresh_counts.get(job.id, 0) + 1
                self.refresh_required.emit(job.id)
            return
        if not job.batch_id:
            return
        if job.state != 'failed' or (job.result or {}).get('error_code') in (
                'PROTECTED_MEDIA', 'DRM_PROTECTED', 'OUTPUT_EXISTS', 'OUTPUT_NOT_VIDEO',
                'OUTPUT_VALIDATION_ERROR', 'OUTPUT_PROBE_UNAVAILABLE', 'MEDIA_PROTOCOL_MISMATCH',
                'DISK_FULL', 'FILE_PERMISSION', 'FILE_EXISTS', 'RESUME_MISMATCH',
                'INCOMPLETE_MEDIA_RESPONSE', 'RECOVERY_PATH_INVALID', 'RECOVERY_STATE_INVALID'):
            return
        candidate = self.next_quality(job)
        key = (job.id, job.retry_count, job.media_candidate.id if job.media_candidate else '')
        if candidate is None or key in self._offered:
            return
        self._offered.add(key)
        if job.auto_fallback:
            self.use_next_quality(job.id)
        else:
            self.quality_fallback_required.emit(job.id, job.media_candidate.resolution_label, candidate.resolution_label)

    def use_next_quality(self, job_id):
        job = self.queue.get(job_id)
        if job is None or job.state != 'failed' or job.worker is not None:
            return False
        candidate = self.next_quality(job)
        if candidate is None:
            return False
        updated = self._request(candidate, job.media_group, job.request)
        if updated.output_extension != job.request.output_extension:
            path = self.queue.filename_allocator.allocate(updated.output_directory,
                                                          updated.filename + '.' + updated.output_extension)
            updated.filename, updated.output_path = path.stem, str(path)
        else:
            updated.filename, updated.output_path = job.request.filename, job.request.output_path
        # 不同质量不能把原质量分片接在一起；旧部分文件保留在独立暂存目录。
        updated.temporary_directory = str(Path(updated.output_directory).resolve() / '.stream-downloads' / uuid.uuid4().hex)
        updated.concurrency = self.queue.concurrency
        build_arguments(updated)
        job.request, job.media_candidate = updated, deepcopy(candidate)
        job.attempted_sources.add(candidate.id)
        from core.download_queue import _job_redactor
        self.queue._redactors[job.id] = _job_redactor(updated)
        self.queue.retry(job.id)
        notice = f'改用 {candidate.resolution_label}；原质量的未完成文件保留，当前质量重新下载。'
        job.logs.append(notice)
        self.queue.log.emit(job.id, notice)
        self.queue.start()
        return True

    def retry_failed(self):
        jobs = [job for job in self.queue.jobs if job.state == 'failed' and job.worker is None]
        for job in jobs:
            self.queue.retry(job.id)
        if jobs:
            self.queue.start()
        return len(jobs)

    def summary(self, batch_id=''):
        jobs = [job for job in self.queue.jobs if not batch_id or job.batch_id == batch_id]
        return {state: sum(job.state == state for job in jobs) for state in
                ('queued', 'running', 'paused', 'waiting_context', 'completed', 'failed', 'cancelled')}
