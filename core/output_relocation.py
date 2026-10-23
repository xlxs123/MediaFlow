"""媒体库整理后的完成任务路径同步；媒体移动由独立的整理事务负责。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path

from PySide6.QtCore import QThread, Signal


def _key(value):
    return os.path.normcase(str(Path(value).expanduser().resolve())).casefold()


def _mapping(values):
    result = {}
    for item in values:
        if isinstance(item, dict) and item.get('old') and item.get('new'):
            result[_key(item['old'])] = str(Path(item['new']).resolve())
    return result


def _destination(value, mapping):
    current = str(value)
    seen = set()
    while current and _key(current) in mapping and _key(current) not in seen:
        seen.add(_key(current))
        current = mapping[_key(current)]
    return current


def _rewrite(value, mapping):
    if isinstance(value, dict):
        return {key: _rewrite(child, mapping) for key, child in value.items()}
    if isinstance(value, list):
        return [_rewrite(child, mapping) for child in value]
    if isinstance(value, str) and value and os.path.normcase(value).casefold() in mapping:
        return _destination(value, mapping)
    return value


def apply_completed_paths(queue, mappings):
    """只改完成任务，不对下载中的文件执行整理，也不触碰恢复工作区身份。"""
    affected = []
    for job in queue.jobs:
        if job.state != 'completed' or job.worker is not None:
            continue
        from core.media_history import snapshot_job
        task_key = snapshot_job(job).get('task_key')
        relevant = [item for item in mappings if not item.get('task_key') or item['task_key'] == task_key]
        mapping = _mapping(relevant)
        old = job.request.output_path
        if not old:
            # 旧版手动Direct任务只在result.files中记录最终文件，没有预分配output_path。
            old = next((path for path in (job.result or {}).get('files', []) if _key(path) in mapping), '')
        destination = _destination(old, mapping) if old else old
        result = _rewrite(job.result or {}, mapping)
        # 重放既有日志也要补做可能在上次退出时中断的工作区日志同步。
        saved = job.request.media_metadata.get('relocated_outputs', [])
        if destination == old and result == job.result and not saved:
            continue
        previous_root = job.request.workspace_output_directory or job.request.output_directory
        if destination != old:
            queue.filename_allocator.release(old)
            path = Path(destination)
            if not job.request.output_extension and path.suffix.lower()[1:] in (
                    'mp4', 'mkv', 'webm', 'm4a', 'mp3', 'opus', 'ogg', 'wav', 'flac', 'aac'):
                job.request.output_extension = path.suffix.lower()[1:]
            job.request.output_path = str(path) if job.request.output_extension else ''
            job.request.filename = path.stem
            job.request.output_directory = str(path.parent)
            job.request.final_output_directory = str(path.parent)
            job.request.workspace_output_directory = previous_root
            job.name = path.stem
            queue.filename_allocator.reserve(path)
        job.result = result
        merged = _mapping(saved + relevant)
        job.request.media_metadata['relocated_outputs'] = [{'old': key, 'new': _destination(value, merged)}
                                                         for key, value in merged.items()]
        affected.append(job)
    if affected:
        queue.save_tasks()
        for job in affected:
            queue.changed.emit(job.id)
    return affected


def update_workspace_paths(request):
    """后台更新旧发布日志，逐文件验证原有提交摘要；旧任务的根目录不迁移。"""
    from core.recovery_workspace import atomic_json, file_hash
    if not request.temporary_directory:
        return False
    root = Path(request.temporary_directory).resolve()
    expected = Path(request.workspace_output_directory or request.output_directory).resolve() / '.stream-downloads'
    if root.parent != expected:
        raise ValueError('整理后的恢复工作区归属无法确认。')
    marker = root / 'workspace.json'
    if not marker.is_file():
        return False
    if marker.stat().st_size > 16 * 1024 * 1024 or marker.is_symlink():
        raise ValueError('恢复日志无效，已保留原文。')
    state = json.loads(marker.read_text(encoding='utf-8'))
    if state.get('status') != 'completed':
        return False
    mapping = _mapping(request.media_metadata.get('relocated_outputs', []))
    changed = False
    for item in state.get('published', []):
        old = item.get('path', '')
        destination = _destination(old, mapping)
        if destination == old:
            continue
        target = Path(destination)
        if (target.parent != Path(request.final_output_directory or request.output_directory).resolve()
                or target.is_symlink() or not target.is_file() or target.stat().st_size != item.get('size')
                or file_hash(target) != item.get('sha256')):
            raise ValueError('整理后的文件与安全提交摘要不一致，未更改恢复日志。')
        item['path'] = str(target)
        changed = True
    if changed:
        atomic_json(marker, state)
    return changed


class RelocationJournalWorker(QThread):
    notice = Signal(str)

    def __init__(self, requests, parent=None):
        super().__init__(parent)
        self.requests = [deepcopy(replace(request, concurrency=None)) for request in requests]

    def run(self):
        for request in self.requests:
            try:
                update_workspace_paths(request)
            except (OSError, ValueError, KeyError):
                self.notice.emit('文件整理已提交，但恢复日志同步未完成；已保留整理映射，下次启动将重试。')
