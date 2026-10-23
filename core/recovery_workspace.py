"""Per-task yt-dlp work files; verify before reuse and publish without overwrites."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
from functools import lru_cache

from core.errors import MediaCheckError

BLOCK_SIZE = 1024 * 1024


def replace_with_retry(source, target):
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 5:
                raise
            # Antivirus/short-lived readers can temporarily prevent Windows rename.
            time.sleep(min(.02 * 2 ** attempt, .16))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@lru_cache(maxsize=1)
def _windows_file_api():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    create = api.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = api.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    return api, create, close


def shared_read(path):
    """检查恢复摘要时允许原生下载器改名；不阻挡其文件提交。"""
    if os.name != 'nt':
        return Path(path).open('rb')
    import ctypes
    import msvcrt
    _, create, close = _windows_file_api()
    # READ|WRITE|DELETE sharing；FILE_SHARE_DELETE同样允许改名。
    # https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
    handle = create(str(Path(path).resolve()), 0x80000000, 0x1 | 0x2 | 0x4,
                    None, 3, 0x80 | 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')  # fd接管句柄，文件对象关闭时恰好释放一次。
    except Exception:
        os.close(descriptor)
        raise


def file_hash(path):
    with shared_read(path) as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class RecoveryWorkspace:
    def __init__(self, request, identity, protocol, notice=None, lock_workspace=False):
        self.request = request
        self.protocol = protocol
        self.notice = notice or (lambda text: None)
        output = Path(request.workspace_output_directory or request.output_directory).expanduser().resolve()
        if not request.temporary_directory:
            request.temporary_directory = str(output / '.stream-downloads' / uuid.uuid4().hex)
        self.root = Path(request.temporary_directory).resolve()
        self._lease = None
        if self.root.parent != output / '.stream-downloads' or self.root.is_symlink():
            raise MediaCheckError('续传工作目录必须是输出目录下的独立 .stream-downloads 子目录。', 'RECOVERY_PATH_INVALID')
        self.marker = self.root / 'workspace.json'
        self.identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.state = {'version': 1, 'identity': self.identity_hash, 'status': 'new', 'files': {}}
        self._last_checkpoint = 0.0
        self._known = {}
        if lock_workspace:
            self.root.mkdir(parents=True, exist_ok=True)
            lease_path = self.root / 'active.lock'
            if lease_path.is_symlink():
                raise MediaCheckError('工作区锁文件是外部链接。', 'RECOVERY_PATH_INVALID')
            lease = lease_path.open('a+b')
            if lease.tell() == 0:
                lease.write(b'\0')
                lease.flush()
            lease.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lease.close()
                raise MediaCheckError('同一任务正在另一个进程中下载，请先暂停该任务。', 'RECOVERY_BUSY') from None
            self._lease = lease
        existing = list(self.root.iterdir()) if self.root.exists() else []
        empty_owned = all(p.name == 'active.lock' or (p.name in ('staging', 'transfer', 'cache')
                          and p.is_dir() and not any(p.iterdir())) for p in existing)
        if existing and not empty_owned and not self.marker.is_file():
            raise MediaCheckError('已有工作目录没有可信恢复记录，已保留文件，请使用新任务。', 'RECOVERY_STATE_INVALID')
        if self.marker.is_file():
            try:
                if self.marker.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('record too large')
                self.state = json.loads(self.marker.read_text(encoding='utf-8'))
                if self.state.get('version') != 1 or not isinstance(self.state.get('files'), dict):
                    raise ValueError('invalid record')
                if self.state.get('identity') != self.identity_hash:
                    raise MediaCheckError('工作区中的媒体身份已经变化，旧进度已保留，请添加新任务。', 'RESUME_MISMATCH')
            except (ValueError, OSError) as error:
                if isinstance(error, MediaCheckError):
                    raise
                raise MediaCheckError('续传记录损坏，已保留原文件，不能安全追加。', 'RECOVERY_STATE_INVALID') from None
        for name in ('staging', 'transfer', 'cache'):
            path = self.root / name
            if path.is_symlink() or (path.exists() and path.resolve().parent != self.root):
                raise MediaCheckError('续传工作目录包含外部链接，不能安全写入。', 'RECOVERY_PATH_INVALID')
            path.mkdir(parents=True, exist_ok=True)
        self.home = self.root / 'staging'
        self.temporary = self.root / 'transfer'

    def close(self):
        lease = getattr(self, '_lease', None)
        if lease is not None:
            lease.close()
            self._lease = None

    def __del__(self):
        self.close()

    def _files(self):
        for directory in (self.home, self.temporary):
            for path in directory.rglob('*'):
                if path.is_symlink():
                    raise MediaCheckError('工作区内存在外部链接，已停止恢复。', 'RECOVERY_PATH_INVALID')
                if path.is_file():
                    if not path.resolve().is_relative_to(self.root):
                        raise MediaCheckError('工作文件超出任务目录。', 'RECOVERY_PATH_INVALID')
                    yield path

    def _quarantine(self, paths, reason):
        paths = [p for p in paths if p.is_file()]
        if not paths:
            return
        directory = self.root / 'retained' / uuid.uuid4().hex
        directory.mkdir(parents=True)
        for path in paths:
            relative = path.relative_to(self.root)
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            path.rename(target)
            self.state['files'].pop(str(relative), None)
        self.notice(reason + '；旧部分文件已保留，重新下载需要的内容。')

    def _verify_prefix(self, path, entry):
        size = entry.get('size', -1)
        blocks = entry.get('blocks')
        if type(size) is not int or size < 0 or path.stat().st_size < size or not isinstance(blocks, list):
            return False
        with path.open('rb') as stream:
            remaining = size
            for expected in blocks:
                data = stream.read(min(BLOCK_SIZE, remaining))
                if not data or hashlib.sha256(data).hexdigest() != expected:
                    return False
                remaining -= len(data)
        return remaining == 0

    def _prefix_partial(self, path):
        if not path.name.endswith('.part'):
            return False
        if self.protocol == 'Direct' or '-Frag' in path.name:
            return True
        # A DASH HTTP track without a native fragment cursor is a byte stream.
        return self.protocol == 'DASH' and not path.with_suffix('.ytdl').exists()

    @staticmethod
    def _partial(path):
        return (path.name.endswith('.part') or path.suffix == '.ytdl' or '.part-Frag' in path.name
                or ('-Frag' not in path.name and '.part' in path.name))

    def prepare(self):
        previous = self.state.get('status')
        files = list(self._files())
        unsafe_fragments = previous == 'active' and self.protocol in ('HLS', 'DASH')
        broken = []
        for path in files:
            entry = self.state['files'].get(str(path.relative_to(self.root)), {})
            partial = self._partial(path)
            if self._prefix_partial(path):
                if not self._verify_prefix(path, entry):
                    broken.append(path)
                elif path.stat().st_size != entry['size']:
                    # Only application-owned, uncheckpointed tail bytes are discarded.
                    with path.open('r+b') as stream:
                        stream.truncate(entry['size'])
                    self.notice('异常退出后的 Direct 尾部尚未校验，回到最后已验证偏移续传。')
                continue
            if partial and unsafe_fragments:
                broken.append(path)
                continue
            if (not entry.get('sha256') or path.stat().st_size != entry.get('size')
                    or file_hash(path) != entry['sha256']):
                broken.append(path)
        if broken and self.protocol in ('HLS', 'DASH'):
            # A native fragment cursor and its concatenated .part must agree.
            broken.extend(p for p in files if self._partial(p) and not self._prefix_partial(p))
        self._quarantine(dict.fromkeys(broken), '检测到未验证或损坏的中间文件，将从已校验分片缓存恢复')
        retained = list(self._files())
        if retained:
            self.notice('已校验原有下载文件，正在恢复；完整音视频轨与有效进度将继续复用。')
        elif any((self.root / 'cache').glob('*.json')):
            self.notice('正在恢复：重建下载记录并复用已校验分片缓存。')
        self.state['status'] = 'active'
        atomic_json(self.marker, self.state)

    def checkpoint(self, final=False):
        now = time.monotonic()
        if not final and now - self._last_checkpoint < 1.0:
            return
        self._last_checkpoint = now
        entries = {}
        for path in self._files():
            relative = str(path.relative_to(self.root))
            partial = self._partial(path)
            if partial and self.protocol == 'HLS' and not final:
                # Completed HLS resources already have a verified bridge cache.
                # Native fragment .part files can be pending rename/exclusively held on Windows.
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                # yt-dlp atomically renames completed fragment files while downloading.
                continue
            old = self.state['files'].get(relative, {})
            partial = self._partial(path)
            prefix = self._prefix_partial(path)
            if partial and not prefix and not final:
                continue
            if prefix:
                # Hash new blocks once; retain only a verified prefix on abrupt exit.
                blocks = list(old.get('blocks', []))
                covered = old.get('size', 0)
                if type(covered) is not int or covered % BLOCK_SIZE or covered > stat.st_size:
                    covered, blocks = 0, []
                target = stat.st_size if final else stat.st_size // BLOCK_SIZE * BLOCK_SIZE
                try:
                    with shared_read(path) as stream:
                        stream.seek(covered)
                        while covered < target:
                            data = stream.read(min(BLOCK_SIZE, target - covered))
                            if not data:
                                break
                            blocks.append(hashlib.sha256(data).hexdigest())
                            covered += len(data)
                except FileNotFoundError:
                    continue
                except PermissionError:
                    if final:
                        raise
                    if old:
                        entries[relative] = old
                    continue
                entries[relative] = {'size': covered, 'blocks': blocks}
                continue
            stamp = (stat.st_size, stat.st_mtime_ns)
            if self._known.get(relative, (None,))[0] == stamp:
                entries[relative] = self._known[relative][1]
            else:
                try:
                    entry = {'size': stat.st_size, 'sha256': file_hash(path)}
                except FileNotFoundError:
                    continue
                except PermissionError:
                    if final:
                        raise
                    # 活跃文件可能由写入方独占；保留旧证明，不把未读内容标成已验证。
                    if old:
                        entries[relative] = old
                    continue
                entries[relative] = entry
                self._known[relative] = (stamp, entry)
        self.state['files'] = entries
        if final and self.state.get('status') != 'publishing':
            self.state['status'] = 'paused'
        atomic_json(self.marker, self.state)

    def publish(self, files, validations, auxiliary=None):
        published = []
        from core.output_organization import final_directory
        try:
            output = final_directory(self.request)
            # 最终目录仅在完整媒体验证通过、准备安全提交时创建。
            output.mkdir(parents=True, exist_ok=True)
            if final_directory(self.request) != output:
                raise ValueError('最终分类目录在提交前发生变化。')
        except (OSError, ValueError) as error:
            raise MediaCheckError('无法创建最终分类目录，已保留验证后的暂存文件：' + str(error), 'FILE_PERMISSION') from None
        for source, validation in zip(files, validations, strict=True):
            source = Path(source).resolve()
            if not source.is_relative_to(self.root) or not source.is_file():
                raise MediaCheckError('下载器返回了工作区以外的文件，不能提交。', 'RECOVERY_PATH_INVALID')
            target = output / source.name
            if self.request.output_path:
                target = Path(self.request.output_path).resolve()
                if target.parent != output:
                    raise MediaCheckError('最终文件路径不属于输出目录。', 'RECOVERY_PATH_INVALID')
            digest = file_hash(source)
            if target.exists():
                raise MediaCheckError('最终文件已存在，已保留验证后的暂存文件，请使用新文件名。', 'OUTPUT_EXISTS')
            relative = str(source.relative_to(self.root))
            published.append({'path': str(target), 'source': relative,
                              'sha256': digest, 'size': source.stat().st_size})
            self.state['files'][relative] = {'size': source.stat().st_size, 'sha256': digest}
        if auxiliary:
            from core.file_names import FileNameAllocator
            from core.media_tracks import language_code
            allocator = FileNameAllocator()
            stem = Path(published[0]['path']).stem
            for item in auxiliary:
                source = Path(item['path']).resolve()
                if not source.is_relative_to(self.root) or not source.is_file() or source.is_symlink():
                    raise MediaCheckError('字幕不属于当前任务工作区。', 'RECOVERY_PATH_INVALID')
                language = language_code(item.get('language')) or 'und'
                fmt = item.get('format')
                if fmt not in ('srt', 'vtt', 'ass', 'ssa', 'ttml'):
                    raise MediaCheckError('字幕输出格式无效。', 'SUBTITLE_INVALID')
                target = allocator.allocate(output, stem + '.' + language + '.' + item['id'][:8] + '.' + fmt)
                relative = str(source.relative_to(self.root))
                digest, size = file_hash(source), source.stat().st_size
                published.append({'path': str(target), 'source': relative, 'sha256': digest, 'size': size,
                                  'kind': 'subtitle', 'language': language, 'format': fmt, 'status': 'verified'})
                self.state['files'][relative] = {'size': size, 'sha256': digest}
        # Persist proof of validation before rename, so a crash at publication is recoverable.
        self.state.update(status='publishing', published=published)
        atomic_json(self.marker, self.state)
        validation_iter = iter(validations)
        for entry in published:
            source = self.root / entry['source']
            target = Path(entry['path'])
            try:
                if os.name == 'nt':
                    source.rename(target)  # Windows rename fails when target exists.
                else:
                    os.link(source, target)
                    source.unlink()
            except FileExistsError:
                raise MediaCheckError('最终文件已存在，已保留验证后的暂存文件，请使用新文件名。', 'OUTPUT_EXISTS') from None
            if entry.get('kind') != 'subtitle':
                next(validation_iter)['path'] = str(target)
        sidecars = [{key: entry[key] for key in ('path', 'language', 'format', 'status', 'sha256', 'size')}
                    for entry in published if entry.get('kind') == 'subtitle']
        if sidecars:
            for validation in validations:
                validation['subtitle_files'] = sidecars
        self.state.update(status='completed', published=published)
        atomic_json(self.marker, self.state)
        # Remove only owned disposable files after validation and safe publication.
        for directory in (self.home, self.temporary, self.root / 'cache'):
            if directory.resolve().parent != self.root or directory.is_symlink():
                continue
            try:
                shutil.rmtree(directory)
            except OSError:
                self.notice('媒体已安全提交；部分暂存文件仍被占用，已保留待下次清理。')
        return [item['path'] for item in published if item.get('kind') != 'subtitle']

    def resume_auxiliary_publication(self):
        """仅处理新版本的媒体+字幕原子提交日志；逐项核对持久化的验证摘要。"""
        entries = self.state.get('published', [])
        if self.state.get('status') != 'publishing' or not any(v.get('kind') == 'subtitle' for v in entries):
            return
        from core.output_organization import final_directory
        output = final_directory(self.request)
        moves = []
        for entry in entries:
            target = Path(entry.get('path', '')).resolve()
            source = (self.root / entry.get('source', '')).resolve()
            if target.parent != output or not source.is_relative_to(self.root):
                raise MediaCheckError('恢复提交路径不属于任务目录。', 'RECOVERY_PATH_INVALID')
            present = target if target.exists() else source
            if not present.is_file() or present.stat().st_size != entry.get('size') or file_hash(present) != entry.get('sha256'):
                raise MediaCheckError('恢复提交的媒体或字幕已变化，文件已保留。', 'RECOVERY_FILE_CHANGED')
            if present == source:
                moves.append((source, target))
        for source, target in moves:
            if os.name == 'nt':
                source.rename(target)
            else:
                os.link(source, target)
                source.unlink()
        self.state['status'] = 'completed'
        atomic_json(self.marker, self.state)

    def subtitle_files(self):
        return [{key: entry[key] for key in ('path', 'language', 'format', 'status', 'sha256', 'size')}
                for entry in self.state.get('published', []) if entry.get('kind') == 'subtitle']

    def committed_files(self):
        if self.state.get('status') not in ('completed', 'publishing'):
            return []
        from core.output_organization import final_directory
        output = final_directory(self.request)
        files = []
        for entry in self.state.get('published', []):
            path = Path(entry.get('path', '')).resolve()
            if not path.exists() and self.state.get('status') == 'publishing':
                return []
            if (path.parent != output or not path.is_file() or path.stat().st_size != entry.get('size')
                    or file_hash(path) != entry.get('sha256')):
                raise MediaCheckError('已提交的文件已变化，不能将任务自动标记为完成。', 'RECOVERY_FILE_CHANGED')
            if entry.get('kind') != 'subtitle':
                files.append(str(path))
        if files and self.state.get('status') == 'publishing':
            self.state['status'] = 'completed'
            atomic_json(self.marker, self.state)
        return files

    def pending_files(self):
        if self.state.get('status') != 'publishing':
            return []
        files = []
        for entry in self.state.get('published', []):
            if entry.get('kind') == 'subtitle':
                continue
            path = (self.root / entry.get('source', '')).resolve()
            if (not path.is_relative_to(self.root) or not path.is_file()
                    or path.stat().st_size != entry.get('size') or file_hash(path) != entry.get('sha256')):
                return []
            files.append(str(path))
        return files
