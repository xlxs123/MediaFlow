"""Read-only task compatibility checks; transfer only into a new owned workspace."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import fields
import base64
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from core.downloader import DownloadRequest, build_arguments
from core.media_history import _text
from core.task_store import TaskStore, _canonical, _encode, _safe_progress


@contextmanager
def source_lock(root):
    """Do not create or edit the source lock; refuse an active downloader."""
    path = Path(root) / 'active.lock'
    handle = None
    try:
        if path.exists():
            handle = path.open('rb')
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if handle:
            handle.close()


def safe_tree(root):
    root = Path(root)
    if root.is_symlink() or root.is_junction():
        raise ValueError('任务临时目录包含链接，不能安全迁移。')
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('任务临时目录包含外部链接。')
        result.extend(Path(directory) / name for name in files if name != 'active.lock')
    return result


def inspect_workspace(request):
    root = Path(request.temporary_directory).resolve()
    output = Path(request.workspace_output_directory or request.output_directory).resolve()
    try:
        uuid.UUID(root.name)
    except ValueError:
        raise ValueError('旧任务工作区标识无效。') from None
    if root.parent != output / '.stream-downloads':
        raise ValueError('旧任务工作区不符合独立目录规则。')
    with source_lock(root):
        paths = safe_tree(root)
        marker = root / 'workspace.json'
        if not marker.is_file() or marker.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('旧任务缺少可验证的恢复记录。')
        state = json.loads(marker.read_text(encoding='utf-8'))
        expected = hashlib.sha256(json.dumps(request.resume_identity, sort_keys=True).encode()).hexdigest()
        if (state.get('version') != 1 or state.get('identity') != expected
                or not isinstance(state.get('files'), dict) or state.get('status') in ('publishing', 'completed')):
            raise ValueError('恢复身份或提交状态不兼容，需重新分析；原文件保留。')
        for relative, proof in state['files'].items():
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or path not in paths or not isinstance(proof, dict):
                raise ValueError('恢复文件证明路径无效。')
            size = proof.get('size')
            if type(size) is not int or size < 0 or path.stat().st_size < size:
                raise ValueError('恢复文件缺失或大小不符。')
            with path.open('rb') as stream:
                if proof.get('sha256'):
                    if path.stat().st_size != size or hashlib.file_digest(stream, 'sha256').hexdigest() != proof['sha256']:
                        raise ValueError('恢复文件完整摘要不符。')
                elif isinstance(proof.get('blocks'), list):
                    remaining = size
                    for digest in proof['blocks']:
                        data = stream.read(min(1024 * 1024, remaining))
                        if not data or hashlib.sha256(data).hexdigest() != digest:
                            raise ValueError('恢复文件前缀摘要不符。')
                        remaining -= len(data)
                    if remaining:
                        raise ValueError('恢复文件前缀证明不完整。')
                else:
                    raise ValueError('恢复文件没有可信摘要。')
        return {'files': paths, 'bytes': sum(p.stat().st_size for p in paths), 'root': root,
                'checkpoint': hashlib.sha256(_canonical(state)).hexdigest()}


def inspect_tasks(path):
    path = Path(path)
    if not path.exists() and not path.with_name(path.name + '.bak').exists():
        return []
    store = TaskStore(path)
    loaded = store.load()
    if store.last_error:
        raise ValueError('任务快照校验失败；旧任务未修改。')
    result = []
    for value in loaded:
        metadata = value['metadata']
        if metadata.get('state') in ('completed', 'cancelled'):
            continue
        request = value['request']
        reason = ''
        workspace = None
        try:
            if request is None:
                raise ValueError('当前 Windows 用户无法解密旧请求，需重新分析。')
            build_arguments(request, require_tools=False)
            if request.download_protocol not in ('Direct', 'HLS', 'DASH'):
                raise ValueError('旧任务协议没有经过迁移验证，需重新分析。')
            query = parse_qs(urlsplit(request.url).query)
            for key in ('expire', 'expires', 'expiration', 'x-expires'):
                for text in query.get(key, []):
                    if text.isdigit() and int(text) < time.time():
                        raise ValueError('旧媒体地址已过期，需重新分析。')
            if request.temporary_directory:
                if not request.resume_identity:
                    raise ValueError('旧任务缺少媒体身份，不能安全恢复。')
                workspace = inspect_workspace(request)
            elif metadata.get('progress', {}).get('downloaded_bytes', 0):
                raise ValueError('已有进度但没有独立临时目录，需重新分析。')
        except (ValueError, OSError, TypeError, KeyError) as error:
            # Never include request values or a system exception containing a signed URL.
            reason = ('当前 Windows 用户无法解密旧请求，需重新分析。' if request is None
                      else '旧任务的协议、链接有效期、媒体身份或临时文件无法安全验证，需重新分析；旧进度保留。')
            safe_reasons = ('旧任务协议没有经过迁移验证，需重新分析。', '旧媒体地址已过期，需重新分析。',
                '旧任务缺少媒体身份，不能安全恢复。', '已有进度但没有独立临时目录，需重新分析。',
                '恢复文件缺失或大小不符。', '恢复文件完整摘要不符。', '恢复文件前缀摘要不符。',
                '恢复文件前缀证明不完整。', '旧任务缺少可验证的恢复记录。',
                '恢复身份或提交状态不兼容，需重新分析；原文件保留。')
            if str(error) in safe_reasons:
                reason = str(error)
        result.append({'metadata': metadata, 'request': request, 'workspace': workspace,
                       'recoverable': not reason, 'reason': reason, 'source_id': str(metadata['id'])})
    return result


def migrated_record(value, job_id, target, workspace_directory=''):
    metadata = value['metadata']
    request = value['request']
    safe = {'id': job_id, 'name': _text(metadata.get('name'), 500) or '旧版任务',
            'state': 'paused' if value['recoverable'] else 'waiting_context',
            'created_at': metadata.get('created_at', ''), 'updated_at': metadata.get('updated_at', ''),
            'retry_count': 0, 'progress': _safe_progress(metadata.get('progress')),
            'request': {'yt_dlp_path': '', 'output_directory': str(target / 'Recovered')}, 'completed_files': []}
    if not value['recoverable']:
        return {'metadata': safe, 'context_error': 'CONTEXT_UNAVAILABLE'}
    request = deepcopy(request)
    if workspace_directory:
        request.workspace_output_directory = str(Path(workspace_directory).parents[1])
        request.temporary_directory = str(workspace_directory)
    # Tool binaries are resolved again by the existing manager when the task resumes.
    for field in ('yt_dlp_path', 'ffmpeg_path', 'ffprobe_path'):
        if getattr(request, field) and not Path(getattr(request, field)).is_file():
            setattr(request, field, '')
    from core.task_store import _SAFE_REQUEST_FIELDS
    safe['request'] = {name: getattr(request, name) for name in _SAFE_REQUEST_FIELDS if hasattr(request, name)}
    data = {field.name: _encode(getattr(request, field.name)) for field in fields(DownloadRequest) if field.name != 'concurrency'}
    cipher = base64.b64encode(TaskStore._protect(_canonical(data))).decode('ascii')
    return {'metadata': safe, 'request_dpapi': cipher}
