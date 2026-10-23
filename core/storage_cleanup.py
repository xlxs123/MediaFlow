"""明确预览、逐项复核和回收站凭据驱动的清理事务。"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from core.library_organizer import _safe_path
from core.storage_safety import path_key, ProtectionPolicy
from core.storage_identity import identity_for
from core.task_store import TaskStore
from core.windows_recycle import recycle_file, read_receipt, RecycleError


class StorageCleanupError(RuntimeError):
    pass


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def proof(path, cancel=None):
    path = _safe_path(path)
    first = path.stat()
    initial_identity = identity_for(path, first)
    if not stat.S_ISREG(first.st_mode) or first.st_nlink != 1:
        raise StorageCleanupError('只能清理普通独立文件；链接、多重硬链接和目录不会清理。')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        if identity(os.fstat(stream.fileno()))[:4] != initial_identity[:4]:
            raise StorageCleanupError('文件已被替换，请重新扫描。')
        while data := stream.read(1024 * 1024):
            if cancel and cancel.is_set():
                raise StorageCleanupError('操作已取消，尚未处理的文件已保留。')
            digest.update(data)
        if identity(os.fstat(stream.fileno()))[:4] != initial_identity[:4]:
            raise StorageCleanupError('读取期间文件发生变化，请重新扫描。')
    if initial_identity != identity_for(path):
        raise StorageCleanupError('文件发生变化，请重新扫描。')
    return {'identity': initial_identity, 'sha256': digest.hexdigest(), 'size': first.st_size}


@contextmanager
def retain_lock(path):
    """Windows 保留副本持有禁止写入/删除的读句柄，直到当前回收返回。"""
    if os.name != 'nt':
        with Path(path).open('rb') as stream:
            yield stream
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise StorageCleanupError('无法锁定保留副本（可能正在写入），未清理该组文件。')
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        yield stream


def _verify(path, expected, cancel=None):
    actual = proof(path, cancel)
    if actual != expected:
        raise StorageCleanupError('文件身份或内容自扫描后发生变化，请重新扫描。')
    return actual


def _descriptor(journal):
    return {key: journal[key] for key in ('version', 'id', 'created', 'files', 'retained')}


def _digest(journal):
    return hashlib.sha256(json.dumps(_descriptor(journal), ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _message(error):
    if isinstance(error, (StorageCleanupError, RecycleError)) or type(error).__name__ in ('HistoryStoreError', 'OrganizationError'):
        return str(error)
    if isinstance(error, FileNotFoundError):
        return '文件已移动或删除；保留历史记录，请重新扫描。'
    if isinstance(error, PermissionError):
        return '文件无读取或修改权限，未执行清理。'
    return '文件清理未完成（' + type(error).__name__ + '），请重新扫描并检查磁盘状态。'


class StorageCleanup:
    def __init__(self, journal_directory, recycler=None):
        self.directory = Path(journal_directory)
        self.recycler = recycler or recycle_file
        self._plans = {}

    def _policy(self, policy):
        return policy if hasattr(policy, 'reason') else ProtectionPolicy(policy)

    def _allowed(self, path, policy):
        if hasattr(policy, 'refresh'):
            policy.refresh()
        reason = policy.reason(path)
        if reason:
            raise StorageCleanupError(reason)

    def preview(self, store, scan_result, selected_paths, protection, cancel_event=None):
        if not scan_result.get('complete') or scan_result.get('cancelled'):
            raise StorageCleanupError('扫描尚未完整完成，请重新扫描后再清理。')
        selected = {path_key(path) for path in selected_paths}
        if not selected or len(selected) > 1000:
            raise StorageCleanupError('请明确选择 1 至 1000 个待回收文件。')
        found, files, retained, errors = set(), [], [], []
        policy = self._policy(protection)
        for group in scan_result.get('groups', []):
            members = group.get('files', [])
            chosen = [row for row in members if path_key(row['path']) in selected]
            if not chosen:
                continue
            keep = [row for row in members if path_key(row['path']) not in selected]
            if not keep:
                errors.append('重复组必须保留至少一个独立副本：' + str(group.get('id', ''))[:12])
                continue
            try:
                verified = []
                for row in members:
                    self._allowed(row['path'], policy)
                    actual = proof(row['path'], cancel_event)
                    if actual['sha256'] != group.get('sha256') or actual['identity'] != list(row['identity']):
                        raise StorageCleanupError('扫描后文件发生变化，请重新扫描。')
                    verified.append((row, actual))
                for row, actual in verified:
                    record = {'path': row['path'], 'proof': actual, 'group': group['sha256']}
                    if path_key(row['path']) in selected:
                        files.append(record)
                        found.add(path_key(row['path']))
                    else:
                        retained.append(record)
            except Exception as error:
                errors.append(_message(error))
        if found != selected:
            errors.append('部分所选文件不属于当前可安全清理的完整哈希重复组，请重新扫描。')
        return self._make_plan(store, files, retained, errors)

    def preview_files(self, store, paths, protection):
        """原媒体库单文件回收入口：仍要求明确确认，不自动处理关联文件。"""
        files, errors = [], []
        policy = self._policy(protection)
        for path in dict.fromkeys(str(Path(p).absolute()) for p in paths):
            try:
                self._allowed(path, policy)
                if not store.organization_references([path])[path]:
                    raise StorageCleanupError('所选文件没有对应媒体库引用，不能从此入口清理。')
                files.append({'path': path, 'proof': proof(path), 'group': ''})
            except Exception as error:
                errors.append(_message(error))
        return self._make_plan(store, files, [], errors)

    def _make_plan(self, store, files, retained, errors):
        rows = []
        references = store.organization_references([item['path'] for item in files])
        for item in files:
            rows.append({'path': item['path'], 'name': Path(item['path']).name,
                         'size': item['proof']['size'], 'entry_ids': sorted(references[item['path']]),
                         'reason': '仅回收此文件；关联字幕、合集文件和下载历史不会一并删除。'})
        token = uuid.uuid4().hex if files and not errors else ''
        if token:
            self._plans[token] = {'files': deepcopy(files), 'retained': deepcopy(retained),
                                  'references': references, 'expires': time.monotonic() + 900}
        self._plans = {key: value for key, value in self._plans.items() if value['expires'] > time.monotonic()}
        return {'token': token, 'rows': rows, 'count': len(rows), 'bytes': sum(row['size'] for row in rows),
                'errors': errors, 'recycle_notice': '移入回收站后仍占用磁盘；真正释放空间需由用户清空回收站。'}

    def _write(self, path, journal):
        TaskStore._atomic_write(path, json.dumps(journal, ensure_ascii=False, sort_keys=True).encode('utf-8'))

    def apply(self, store, token, confirmed=False, protection=None, cancel_event=None):
        if confirmed is not True:
            raise StorageCleanupError('尚未明确确认，文件没有改变。')
        plan = self._plans.pop(token, None)
        if not plan or plan['expires'] <= time.monotonic():
            raise StorageCleanupError('清理预览已过期或已经执行，请重新扫描和预览。')
        policy = self._policy(protection)
        paths = [item['path'] for item in plan['files']]
        if store.organization_references(paths) != plan['references']:
            raise StorageCleanupError('媒体库引用自预览后发生变化，请重新预览。')
        for item in plan['files'] + plan['retained']:
            self._allowed(item['path'], policy)
            _verify(item['path'], item['proof'], cancel_event)
        self.directory.mkdir(parents=True, exist_ok=True)
        _safe_path(self.directory)
        transaction = uuid.uuid4().hex
        files = [dict(item, nonce=uuid.uuid4().hex, receipt=f'{transaction}-{i}.receipt.json')
                 for i, item in enumerate(plan['files'])]
        journal = {'version': 1, 'id': transaction, 'created': time.time(), 'files': files,
                   'retained': plan['retained'], 'states': {}, 'errors': {}}
        store.register_storage_cleanup(transaction, _digest(journal))
        journal_path = self.directory / (transaction + '.json')
        self._write(journal_path, journal)
        result = {'completed': 0, 'failed': 0, 'bytes': 0, 'errors': [], 'paths': [],
                  'needs_rescan': True, 'database_pending': 0, 'retained_verified': []}
        for item in files:
            if cancel_event and cancel_event.is_set():
                result['errors'].append('已取消后续清理，尚未处理的文件已保留。')
                break
            path = item['path']
            recycled = False
            try:
                with ExitStack() as locks:
                    keep = [record for record in plan['retained'] if record['group'] == item['group']] if item['group'] else []
                    for retained in keep:
                        locks.enter_context(retain_lock(retained['path']))
                        self._allowed(retained['path'], policy)
                        _verify(retained['path'], retained['proof'], cancel_event)
                    self._allowed(path, policy)
                    _verify(path, item['proof'], cancel_event)
                    journal['states'][path] = 'recycling'
                    self._write(journal_path, journal)
                    receipt = self.recycler(path, item['proof'], self.directory / item['receipt'],
                                            nonce=item['nonce'], cancel_event=cancel_event)
                    if (not receipt or receipt.get('recycled') is not True or receipt.get('sha256') != item['proof']['sha256']
                            or receipt.get('nonce') != item['nonce'] or Path(path).exists()):
                        raise StorageCleanupError('未取得有效回收凭据，不能确认删除成功；请重新扫描。')
                    recycled = True
                    journal['states'][path] = 'recycled'
                    self._write(journal_path, journal)
                    result['completed'] += 1
                    result['bytes'] += item['proof']['size']
                    result['paths'].append(path)
                    store.mark_storage_removed([path], '用户确认移入系统回收站，历史记录已保留。')
                    journal['states'][path] = 'committed'
                    self._write(journal_path, journal)
                    for retained in keep:
                        _verify(retained['path'], retained['proof'])
                        if retained['path'] not in result['retained_verified']:
                            result['retained_verified'].append(retained['path'])
            except Exception as error:
                text = _message(error)
                if recycled:
                    result['database_pending'] += 1
                    text = '文件已确认回收，但后续记录更新未完成；保留凭据，重新扫描会恢复。' + text
                else:
                    result['failed'] += 1
                result['errors'].append(path + '：' + text)
                journal['errors'][path] = text
                try:
                    self._write(journal_path, journal)
                except OSError:
                    result['errors'].append('清理日志写入失败，请保留本地存储并重新扫描。')
        return result

    def recover(self, store):
        result = {'recovered': 0, 'errors': []}
        if not self.directory.exists():
            return result
        for file in self.directory.glob('*.json'):
            if file.name.endswith('.receipt.json'):
                continue
            try:
                _safe_path(file)
                if file.stat().st_size > 4 * 1024 * 1024:
                    raise StorageCleanupError('清理日志超过安全上限，请保留原件并检查。')
                data = json.loads(file.read_text(encoding='utf-8'))
                if (data.get('version') != 1 or file.stem != data['id']
                        or store.storage_cleanup_transaction(data['id']) != _digest(data)):
                    raise StorageCleanupError('清理日志与数据库不匹配，未自动更新记录。')
                for item in data['files']:
                    receipt_path = self.directory / item['receipt']
                    if receipt_path.parent != self.directory or Path(item['receipt']).name != item['receipt']:
                        raise StorageCleanupError('清理凭据路径无效。')
                    receipt = read_receipt(receipt_path, item['path'], item['proof'], item['nonce'])
                    if receipt and not Path(item['path']).exists():
                        store.mark_storage_removed([item['path']], '已依据系统回收凭据恢复记录；历史已保留。')
                        if data['states'].get(item['path']) != 'committed':
                            result['recovered'] += 1
                        data['states'][item['path']] = 'committed'
                    elif data['states'].get(item['path']) in ('recycling', 'recycled'):
                        result['errors'].append(item['path'] + '：状态无法确认或文件已恢复，未认定删除成功，请重新扫描。')
                self._write(file, data)
            except Exception as error:
                result['errors'].append(file.name + '：' + _message(error))
        return result
