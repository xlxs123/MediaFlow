"""清理只消费本地路径和任务状态；不解密、不记录任何下载访问上下文。"""
from __future__ import annotations

import os
from pathlib import Path
import time

from core.task_store import TaskStore


def path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path))).casefold()


def _inside(path, directory):
    left, right = path_key(path), path_key(directory).rstrip('\\/')
    return left == right or left.startswith(right + os.sep)


def queue_snapshot(queue):
    """仅在 Qt owner 线程调用；后台不得遍历活的 DownloadJob。"""
    jobs = []
    for job in queue.jobs:
        request = job.request
        record = {'id': job.id, 'state': job.state, 'paths': [], 'directories': [],
                  'uncertain': bool(job.context_error or job.worker is not None)}
        for key in ('output_path',):
            value = getattr(request, key, '')
            if value:
                record['paths'].append(str(value))
        workspace = getattr(request, 'temporary_directory', '')
        if workspace:
            record['directories'].append(str(workspace))
        # 未完成/失败/取消/未知任务可能在恢复，未知文件名时保守保护整个输出目录。
        if job.state != 'completed' or record['uncertain']:
            for key in ('output_directory', 'final_output_directory', 'collection_directory'):
                value = getattr(request, key, '')
                if value:
                    record['directories'].append(str(value))
        result = job.result or {}
        record['paths'].extend(str(p) for p in result.get('files', []) if isinstance(p, str))
        validation = result.get('output_validation') or []
        validation_files = validation.get('files', []) if isinstance(validation, dict) else validation
        for item in validation_files if isinstance(validation_files, list) else []:
            if isinstance(item, dict):
                if item.get('path'):
                    record['paths'].append(item['path'])
                record['paths'].extend(p['path'] for p in item.get('subtitle_files', [])
                                       if isinstance(p, dict) and p.get('path'))
        jobs.append(record)
    return {'jobs': jobs, 'captured_at': time.time(), 'error': ''}


class ProtectionPolicy:
    def __init__(self, snapshot=None, task_store_path=None):
        self.paths, self.directories = set(), set()
        self.error = ''
        self.snapshot = snapshot or {'jobs': []}
        self._consume_snapshot(self.snapshot)
        self.task_store_path = Path(task_store_path) if task_store_path else None
        if self.task_store_path:
            self._read_tasks()

    def _consume_snapshot(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get('error') or not isinstance(snapshot.get('jobs'), list):
            self.error = '无法确认当前任务状态，已禁止清理；请停止任务并重新扫描。'
            return
        for job in snapshot['jobs']:
            if not isinstance(job, dict) or not isinstance(job.get('state'), str):
                self.error = '任务状态未知，已禁止清理；请重新载入任务列表。'
                continue
            # 完成任务最终媒体可以管理；其工作目录仍永远保留。
            dirs = job.get('directories', [])
            if not isinstance(dirs, list) or not isinstance(job.get('paths', []), list):
                self.error = '任务路径数据不完整，已禁止清理。'
                continue
            self.directories.update(path_key(p) for p in dirs if p)
            if job['state'] != 'completed' or job.get('uncertain'):
                self.paths.update(path_key(p) for p in job.get('paths', []) if p)
                if not dirs and not job.get('paths'):
                    self.error = '存在路径未知的未完成任务，已禁止清理；请检查恢复数据。'

    def _read_tasks(self):
        source = self.task_store_path
        backup = source.with_name(source.name + '.bak')
        if not source.exists() and not backup.exists():
            return
        try:
            # 清理不使用 .bak 推测当前状态，也不调用 DPAPI 解密。
            if not source.is_file() or source.stat().st_size > 64 * 1024 * 1024:
                raise ValueError()
            _, document = TaskStore._read_document(source)
            records = []
            for row in document['tasks']:
                data = row['metadata']
                request = data.get('request')
                if not isinstance(request, dict) or not isinstance(data.get('state'), str):
                    raise ValueError()
                dirs = [request['temporary_directory']] if request.get('temporary_directory') else []
                uncertain = bool(row.get('context_error'))
                if data['state'] != 'completed' or uncertain:
                    dirs.extend(request[name] for name in ('output_directory', 'final_output_directory')
                                if request.get(name))
                paths = [request['output_path']] if request.get('output_path') else []
                records.append({'state': data['state'], 'directories': dirs, 'paths': paths,
                                'uncertain': uncertain})
            self._consume_snapshot({'jobs': records})
        except (OSError, ValueError, TypeError, KeyError):
            self.error = '任务恢复快照缺失、损坏或不可读，无法安全清理；请在任务页修复并重新扫描。'

    def refresh(self):
        """每次操作前重读持久化状态；已有保护只增不减。"""
        if self.task_store_path:
            self._read_tasks()
        return self

    def reason(self, path):
        if self.error:
            return self.error
        try:
            target = Path(path)
            parts = {p.casefold() for p in target.parts}
            if ('.stream-downloads' in parts or any(p.endswith(('.part', '.ytdl', '.part-frag')) for p in parts)
                    or target.name.casefold() in ('workspace.json', 'active.lock')):
                return '下载临时文件或断点续传工作目录，禁止清理。'
            if any(p in parts for p in ('browserprofile', '.ssh', '.gnupg', 'credentials', '.aws', '.azure', '.codex')):
                return '浏览器登录配置或凭据目录不属于媒体清理范围。'
            key = path_key(path)
            if key in self.paths or any(_inside(path, p) for p in self.directories):
                return '文件可能被下载、合并或恢复任务使用，请完成或移除相关任务后重新扫描。'
        except (OSError, TypeError, ValueError):
            return '无法确认文件路径，禁止清理。'
        return ''
