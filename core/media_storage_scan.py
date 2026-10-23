"""真实磁盘统计和串行完整哈希；不使用媒体标题或扩展名确认重复。"""
from collections import defaultdict
from datetime import datetime
import hashlib
import os
from pathlib import Path
import stat
import threading
import uuid

from core.storage_cache import HashCache, StorageCacheError
from core.storage_safety import path_key
from core.storage_identity import identity_for


class StorageScanError(RuntimeError):
    pass


_SKIP = frozenset(('.stream-downloads', 'browserprofile', '.ssh', '.gnupg', '.codex', '.aws', '.azure',
                    'credentials', '$recycle.bin', 'system volume information', 'user data'))
_TYPES = {'video': frozenset(('mp4', 'mkv', 'webm', 'mov', 'avi', 'flv', 'm4v', 'mpg', 'mpeg', 'ts')),
          'audio': frozenset(('mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'opus', 'wma')),
          'subtitle': frozenset(('srt', 'vtt', 'ass', 'ssa', 'ttml', 'sub', 'lrc'))}


def file_identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _safe(path):
    if not path.is_absolute():
        raise StorageScanError('扫描范围必须是完整的绝对路径。')
    if any(part.casefold() in _SKIP for part in path.parts):
        raise StorageScanError('已跳过下载工作目录、回收站、浏览器配置或敏感凭据目录。')
    for current in (*reversed(path.parents), path):
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or getattr(current, 'is_junction', lambda: False)()):
            raise StorageScanError('已跳过符号链接、junction 或重解析路径，避免重复计数和误操作。')
    return path


def _kind(path, declared=''):
    if declared in ('video', 'audio', 'subtitle'):
        return declared
    extension = path.suffix.lower().lstrip('.')
    return next((kind for kind, values in _TYPES.items() if extension in values), 'other')


def _network(path):
    if str(path).startswith(('\\\\', '//')):
        return True
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32')
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        return kernel.GetDriveTypeW(path.anchor) == 4
    return False


class StorageScanner:
    def __init__(self, cache_path=None):
        self.cache_path = cache_path
        self.cache = None

    def close(self):
        if self.cache:
            self.cache.close()
            self.cache = None

    def scan(self, entries, extra_dirs=(), cancel_event=None, progress=None, guard=None):
        cancel = cancel_event or threading.Event()
        notify = progress or (lambda _value: None)
        guard = guard or (lambda _path: '')
        result = {'scan_id': uuid.uuid4().hex, 'complete': False, 'cancelled': False, 'files': [],
                  'groups': [], 'stats': {}, 'issues': [], 'scopes': ['媒体库已登记的媒体与独立字幕'],
                  'hash_bytes': 0, 'hash_total': 0, 'hashed_files': 0, 'cache_hits': 0}
        records, visited, physical = {}, set(), {}

        def issue(path, reason):
            result['issues'].append({'path': str(path), 'reason': reason})

        if self.cache_path:
            try:
                self.cache = HashCache(self.cache_path)
            except StorageCacheError as error:
                issue(self.cache_path, str(error))

        def add(value, entry=None, declared=''):
            if not value or cancel.is_set():
                return
            path = Path(value).expanduser().absolute()
            key = path_key(path)
            entry = entry or {}
            if key in records:
                row = records[key]
            else:
                row = {'path': str(path), 'name': path.name, 'size': 0, 'format': path.suffix.lower().lstrip('.'),
                       'mtime_ns': 0, 'mtime': '', 'disk': path.anchor, 'media_type': _kind(path, declared or entry.get('media_type')),
                       'entry_ids': [], 'source_sites': [], 'download_dates': [], 'identity': [], 'sha256': '',
                       'status': 'skipped', 'protected_reason': '', 'aliases': [], 'nlink': 0}
                records[key] = row
                try:
                    if _network(path):
                        raise StorageScanError('网络路径暂不自动读取或清理，请复制到可访问的本地媒体目录后重扫。')
                    _safe(path)
                    info = path.stat()
                    initial_identity = identity_for(path, info)
                    if not stat.S_ISREG(info.st_mode):
                        raise StorageScanError('非普通文件，未计入媒体容量。')
                    with path.open('rb') as stream:
                        stream.read(1)
                        if file_identity(os.fstat(stream.fileno()))[:4] != initial_identity[:4]:
                            raise StorageScanError('文件读取时已被替换，请重新扫描。')
                    row.update(size=info.st_size, mtime_ns=info.st_mtime_ns,
                               mtime=datetime.fromtimestamp(info.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                               identity=initial_identity, status='exists', nlink=info.st_nlink,
                               protected_reason=guard(str(path)))
                    if info.st_nlink > 1:
                        row['protected_reason'] = '硬链接文件只计一次容量，不计入可回收副本。'
                except FileNotFoundError:
                    row['status'] = 'missing'
                    issue(path, '文件或磁盘已移动、删除或离线；历史记录已保留。')
                except PermissionError:
                    row['status'] = 'inaccessible'
                    issue(path, '无读取权限，未将历史大小计为当前占用。')
                except StorageScanError as error:
                    row['status'] = 'skipped'
                    issue(path, str(error))
                except OSError as error:
                    row['status'] = 'inaccessible'
                    issue(path, '文件或磁盘不可访问（系统错误 ' + str(error.errno) + '）。')
            if entry.get('id') is not None and entry['id'] not in row['entry_ids']:
                row['entry_ids'].append(entry['id'])
            site = entry.get('source_site', '')
            if site and site not in row['source_sites']:
                row['source_sites'].append(site)
            date = str(entry.get('finished_at', '')).split('T')[0]
            if date and date not in row['download_dates']:
                row['download_dates'].append(date)
            notify({'phase': 'enumerate', 'files_done': len(records), 'files_total': 0,
                    'bytes_done': 0, 'bytes_total': 0, 'path': str(path), 'message': '正在检查真实文件和扫描范围'})

        try:
            for entry in entries:
                if cancel.is_set():
                    break
                add(entry.get('path'), entry)
                for sidecar in (entry.get('validation') or {}).get('subtitle_files', []):
                    if isinstance(sidecar, dict):
                        add(sidecar.get('path'), entry, 'subtitle')
            roots = []
            for value in extra_dirs:
                path = Path(value).expanduser().absolute()
                result['scopes'].append(str(path))
                try:
                    if path == Path(path.anchor):
                        raise StorageScanError('不扫描整个磁盘根目录，请选择明确的媒体文件夹。')
                    if _network(path):
                        raise StorageScanError('网络扫描目录不可用于本地去重，请选择本地媒体文件夹。')
                    if any(part.casefold() == 'appdata' for part in path.parts):
                        raise StorageScanError('应用数据目录可能含登录和凭据，不作为额外扫描范围。')
                    _safe(path)
                    if not path.is_dir():
                        raise StorageScanError('扫描目录不存在、离线或不可访问。')
                    if any(path_key(path) == path_key(p) or path_key(path).startswith(path_key(p) + os.sep) for p in roots):
                        continue
                    roots.append(path)
                    stack = [path]
                    while stack and not cancel.is_set():
                        folder = stack.pop()
                        try:
                            _safe(folder)
                            info = folder.stat()
                            folder_id = (info.st_dev, info.st_ino) if info.st_ino else path_key(folder)
                            if folder_id in visited:
                                continue
                            visited.add(folder_id)
                            with os.scandir(folder) as children:
                                for child in children:
                                    if cancel.is_set():
                                        break
                                    try:
                                        child_path = Path(child.path)
                                        if child.name.casefold() in _SKIP or child.is_symlink():
                                            issue(child_path, '已跳过链接、任务工作目录或敏感配置目录。')
                                        elif child.is_dir(follow_symlinks=False):
                                            stack.append(child_path)
                                        else:
                                            add(child_path)
                                    except OSError:
                                        issue(child.path, '无法读取此目录项，已跳过。')
                        except (OSError, StorageScanError) as error:
                            issue(folder, str(error) if isinstance(error, StorageScanError) else '目录无权限、离线或已发生变化，已跳过。')
                except (OSError, StorageScanError) as error:
                    issue(path, str(error) if isinstance(error, StorageScanError) else '扫描目录不可访问，已跳过。')

            # 同一文件多条历史或硬链接只能计一份真实容量，保留所有关联信息。
            unique = []
            for row in records.values():
                inode = tuple(row['identity'][:2]) if row['identity'] and row['identity'][1] else None
                if inode and inode in physical:
                    original = physical[inode]
                    original['aliases'].append(row['path'])
                    for key in ('entry_ids', 'source_sites', 'download_dates'):
                        original[key] = sorted(set(original[key]) | set(row[key]))
                    original['protected_reason'] = original['protected_reason'] or row['protected_reason'] or '同一物理文件别名，不重复计算可释放容量。'
                    original['nlink'] = max(original['nlink'], row['nlink'])
                else:
                    if inode:
                        physical[inode] = row
                    unique.append(row)
            result['files'] = unique
            buckets = defaultdict(list)
            for row in unique:
                if row['status'] == 'exists' and not row['protected_reason'] and not row['aliases']:
                    buckets[row['size']].append(row)
            candidates = [row for rows in buckets.values() if len(rows) > 1 for row in rows]
            result['hash_total'] = sum(row['size'] for row in candidates)
            hashed = defaultdict(list)
            for index, row in enumerate(candidates):
                if cancel.is_set():
                    break
                path = Path(row['path'])
                try:
                    _safe(path)
                    if identity_for(path) != row['identity']:
                        raise StorageScanError('文件自统计后发生变化，本次不确认重复。')
                    cached = self.cache.get(path_key(path), row['identity']) if self.cache else ''
                    if cached:
                        digest = cached
                        result['cache_hits'] += 1
                        # bytes_done 是已经完成验证的候选字节，cache_hits 单独展示，并非磁盘读速率。
                        result['hash_bytes'] += row['size']
                    else:
                        algorithm = hashlib.sha256()
                        with path.open('rb') as stream:
                            if file_identity(os.fstat(stream.fileno()))[:4] != row['identity'][:4]:
                                raise StorageScanError('文件已被替换，本次不确认重复。')
                            while data := stream.read(1024 * 1024):
                                if cancel.is_set():
                                    break
                                algorithm.update(data)
                                result['hash_bytes'] += len(data)
                                notify({'phase': 'hash', 'files_done': index, 'files_total': len(candidates),
                                        'bytes_done': result['hash_bytes'], 'bytes_total': result['hash_total'],
                                        'path': str(path), 'message': '正在串行计算完整 SHA-256'})
                            if cancel.is_set():
                                break
                            if file_identity(os.fstat(stream.fileno()))[:4] != row['identity'][:4]:
                                raise StorageScanError('计算哈希期间文件发生变化，本次不确认重复。')
                        digest = algorithm.hexdigest()
                        result['hashed_files'] += 1
                    if identity_for(path) != row['identity']:
                        raise StorageScanError('计算哈希后文件发生变化，本次不确认重复。')
                    row['sha256'] = digest
                    if self.cache and not cached:
                        try:
                            self.cache.put(path_key(path), row['identity'], digest)
                        except StorageCacheError as error:
                            issue(self.cache_path, str(error))
                            self.close()
                    hashed[digest].append(row)
                    notify({'phase': 'hash', 'files_done': index + 1, 'files_total': len(candidates),
                            'bytes_done': result['hash_bytes'], 'bytes_total': result['hash_total'], 'path': str(path),
                            'message': '完整 SHA-256 内容已核对' + ('（命中有效缓存）' if cached else '')})
                except (OSError, StorageScanError) as error:
                    row['status'] = 'changed' if not isinstance(error, PermissionError) else 'inaccessible'
                    row['sha256'] = ''
                    issue(path, str(error) if isinstance(error, StorageScanError) else '文件无法读取或已移动，本次不确认重复。')
            if not cancel.is_set():
                for digest, rows in hashed.items():
                    valid = []
                    for row in rows:
                        try:
                            if identity_for(row['path']) == row['identity']:
                                valid.append(row)
                            else:
                                row['status'] = 'changed'
                                issue(row['path'], '扫描结束前文件发生变化，已从重复组排除。')
                        except OSError:
                            row['status'] = 'missing'
                            issue(row['path'], '扫描结束前文件丢失，已从重复组排除。')
                    if len(valid) > 1:
                        size = valid[0]['size']
                        result['groups'].append({'id': digest, 'sha256': digest, 'files': valid,
                                                 'size': size, 'reclaimable_bytes': size * (len(valid) - 1)})
            result['cancelled'] = cancel.is_set()
            result['complete'] = not result['cancelled']
            stats = {'total_files': 0, 'total_bytes': 0, 'missing_files': 0, 'inaccessible_files': 0,
                     'duplicate_files': sum(len(g['files']) for g in result['groups']),
                     'reclaimable_bytes': sum(g['reclaimable_bytes'] for g in result['groups']),
                     'by_type': {}, 'by_site': {}, 'by_date': {}, 'by_folder': {}}
            for row in unique:
                if row['status'] != 'exists':
                    if row['status'] == 'missing':
                        stats['missing_files'] += 1
                    elif row['status'] in ('inaccessible', 'skipped', 'changed'):
                        stats['inaccessible_files'] += 1
                    continue
                stats['total_files'] += 1
                stats['total_bytes'] += row['size']
                for dimension, label in (('by_type', row['media_type']),
                                         ('by_site', row['source_sites'][0] if len(row['source_sites']) == 1 else '多个来源' if row['source_sites'] else '未知来源'),
                                         ('by_date', row['download_dates'][0] if len(row['download_dates']) == 1 else '多个日期' if row['download_dates'] else '未知日期'),
                                         ('by_folder', str(Path(row['path']).parent))):
                    bucket = stats[dimension].setdefault(label, {'count': 0, 'bytes': 0})
                    bucket['count'] += 1
                    bucket['bytes'] += row['size']
            result['stats'] = stats
            notify({'phase': 'done', 'files_done': len(unique), 'files_total': len(unique),
                    'bytes_done': result['hash_bytes'], 'bytes_total': result['hash_total'], 'path': '',
                    'message': '扫描已取消' if result['cancelled'] else '扫描完成；所有重复组均经完整 SHA-256 确认'})
            return result
        finally:
            self.close()
