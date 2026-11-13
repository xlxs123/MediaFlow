"""真实磁盘统计和串行完整哈希；不使用媒体标题或扩展名确认重复。"""
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime
import hashlib
import os
from pathlib import Path
import stat
import threading
from time import perf_counter_ns
import uuid

from core.storage_cache import HashCache, StorageCacheError
from core.storage_safety import path_key
from core.storage_identity import identity_for, identity_from_stream, open_read_locked


class StorageScanError(RuntimeError):
    pass


_SKIP = frozenset(('.stream-downloads', 'browserprofile', '.ssh', '.gnupg', '.codex', '.aws', '.azure',
                    'credentials', '$recycle.bin', 'system volume information', 'user data'))
_CACHE_BATCH_SIZE = 64
_HASH_BLOCK = 1024 * 1024
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
        attributes = getattr(info, 'st_file_attributes', None)
        tag = getattr(info, 'st_reparse_tag', None)
        # Windows lstat reports name-surrogate reparse points without following them.
        # Keep the separate junction query when this snapshot has no Windows metadata.
        if (stat.S_ISLNK(info.st_mode) or (attributes or 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or tag or ((attributes is None or tag is None)
                           and getattr(current, 'is_junction', lambda: False)())):
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


def _read_digest(path, expected, cancel, on_chunk, retain=None):
    """Hash the current file through one checked handle. On Windows, exclude writers."""
    _safe(path)
    with ExitStack() as opened:
        stream = opened.enter_context(open_read_locked(path))

        def check_handle():
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or identity_from_stream(stream, info) != expected):
                raise StorageScanError('文件身份或链接状态发生变化，本次不确认重复。')

        check_handle()
        algorithm = hashlib.sha256()
        remaining = expected[2]
        while remaining:
            if cancel.is_set():
                return None
            data = stream.read(min(remaining, _HASH_BLOCK))
            if not data:
                raise StorageScanError('文件在读取期间缩短，本次不确认重复。')
            algorithm.update(data)
            remaining -= len(data)
            on_chunk(len(data))
        if cancel.is_set():
            return None
        if stream.read(1):
            raise StorageScanError('文件在读取期间增大，本次不确认重复。')
        check_handle()
        _safe(path)
        if identity_for(path) != expected:
            raise StorageScanError('读取后文件路径发生变化，本次不确认重复。')
        if retain is not None:
            retain.enter_context(opened.pop_all())
        return algorithm.hexdigest()


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
                  'hash_bytes': 0, 'hash_total': 0, 'hashed_files': 0, 'cache_hits': 0,
                  'sha256_reads': 0, 'confirmed_files': 0, 'confirmation_ns': 0}
        records, visited, physical = {}, set(), {}
        pending_cache = []

        def issue(path, reason):
            result['issues'].append({'path': str(path), 'reason': reason})

        def reject(row, error):
            row['status'] = ('missing' if isinstance(error, FileNotFoundError) else
                             'inaccessible' if isinstance(error, PermissionError) else 'changed')
            row['sha256'] = ''
            issue(row['path'], str(error) if isinstance(error, StorageScanError) else
                  '文件无法稳定读取或已移动，本次不确认重复。')

        def flush_cache():
            if not pending_cache or cancel.is_set():
                return
            try:
                self.cache.put_many(pending_cache)
            except StorageCacheError as error:
                issue(self.cache_path, str(error))
                self.close()
            finally:
                pending_cache.clear()

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
                    root_key = path_key(path)
                    if any(root_key == key or root_key.startswith(key + os.sep) for key in roots):
                        continue
                    roots.append(root_key)
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
            observed = defaultdict(list)
            confirmed_bytes = 0

            def report(phase, row, index, done, total, message):
                notify({'phase': phase, 'files_done': index, 'files_total': len(candidates),
                        'bytes_done': done, 'bytes_total': total, 'path': row['path'], 'message': message})

            def read(row, index, phase, retain=None):
                def advance(count):
                    nonlocal confirmed_bytes
                    if phase == 'read':
                        result['hash_bytes'] += count
                    else:
                        confirmed_bytes += count
                    report(phase, row, index, result['hash_bytes'] + confirmed_bytes,
                           2 * result['hash_total'], '正在读取当前文件内容' if phase == 'read' else '正在复核当前文件内容')

                digest = _read_digest(Path(row['path']), row['identity'], cancel, advance, retain)
                if digest is not None:
                    result['sha256_reads'] += 1
                return digest

            for index, row in enumerate(candidates):
                if cancel.is_set():
                    break
                path = Path(row['path'])
                try:
                    checkpoint = result['hash_bytes'] + min(row['size'], _HASH_BLOCK)
                    digest = read(row, index, 'read')
                    if digest is None or cancel.is_set():
                        break
                    # Deliver the first hash checkpoint once the read handle has closed.
                    # Later callbacks may touch the path. Confirmation reads it again.
                    if row['size']:
                        report('hash', row, index, checkpoint, result['hash_total'], '文件内容已读取，等待复核')
                    if cancel.is_set():
                        break
                    cache_key = path_key(path) if self.cache else ''
                    cached = self.cache.get(cache_key, row['identity']) if self.cache else ''
                    if cached == digest:
                        result['cache_hits'] += 1
                    else:
                        result['hashed_files'] += 1
                        if self.cache:
                            pending_cache.append((cache_key, row['identity'], digest))
                            if len(pending_cache) == _CACHE_BATCH_SIZE:
                                flush_cache()
                    observed[digest].append((row, cache_key))
                    report('hash', row, index + 1, result['hash_bytes'], result['hash_total'],
                           '文件内容已读取，等待复核')
                except (OSError, StorageScanError) as error:
                    reject(row, error)
            if not cancel.is_set():
                flush_cache()
                started = perf_counter_ns()
                invalid = []
                index = 0
                for reference, rows in observed.items():
                    if cancel.is_set():
                        break
                    with ExitStack() as retained:
                        valid = []
                        for row, cache_key in rows:
                            if cancel.is_set():
                                break
                            try:
                                digest = read(row, index, 'confirm', retained)
                                if digest is None:
                                    break
                                if digest != reference:
                                    raise StorageScanError('两次读取的文件内容不同，本次不确认重复。')
                                valid.append((row, cache_key))
                            except (OSError, StorageScanError) as error:
                                reject(row, error)
                                invalid.append((cache_key, row['identity'], reference))
                            index += 1
                        if cancel.is_set():
                            break
                        group = []
                        for row, cache_key in valid:
                            try:
                                path = _safe(Path(row['path']))
                                info = path.stat()
                                if info.st_nlink != 1 or identity_for(path, info) != row['identity']:
                                    raise StorageScanError('确认完成前文件路径发生变化，本次不确认重复。')
                                row['sha256'] = reference
                                result['confirmed_files'] += 1
                                group.append(row)
                            except (OSError, StorageScanError) as error:
                                reject(row, error)
                                invalid.append((cache_key, row['identity'], reference))
                        if len(group) > 1:
                            size = group[0]['size']
                            result['groups'].append({'id': reference, 'sha256': reference, 'files': group,
                                                     'size': size, 'reclaimable_bytes': size * (len(group) - 1)})
                if self.cache and invalid:
                    try:
                        self.cache.discard_many(invalid)
                    except StorageCacheError as error:
                        issue(self.cache_path, str(error))
                result['confirmation_ns'] = perf_counter_ns() - started
            result['cancelled'] = cancel.is_set()
            result['complete'] = not result['cancelled']
            if result['cancelled']:
                result['groups'].clear()
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
