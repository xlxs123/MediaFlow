"""已选择媒体的安全整理；后台规划、独占写入、数据库 CAS 和可恢复的本地日志。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from core.file_names import FileNameAllocator, sanitize_filename, _units
from core.output_organization import (classification_parts, directory_for, filename_limit,
                                      normalize_options, render_filename, values_for_entry)


class OrganizationError(RuntimeError):
    """可直接显示、不含网络访问上下文的整理错误。"""


def _key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path))).casefold()


def _safe_path(value, *, missing=False):
    path = Path(value).expanduser().absolute()
    if not Path(value).expanduser().is_absolute():
        raise OrganizationError('请选择完整的本地绝对路径。')
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise OrganizationError('文件或目录已被移动或删除，历史记录已保留。') from None
        if stat.S_ISLNK(info.st_mode) or getattr(item, 'is_junction', lambda: False)():
            raise OrganizationError('为避免误移其他文件，不能整理链接或经过链接的目录。')
    return path


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _hash(path, cancel=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while data := handle.read(1024 * 1024):
            if cancel and cancel.is_set():
                raise OrganizationError('整理已取消，未提交的原文件已保留。')
            digest.update(data)
    return digest.hexdigest()


def _proof(path, cancel=None):
    path = _safe_path(path)
    first = path.lstat()
    if not stat.S_ISREG(first.st_mode) or first.st_size <= 0:
        raise OrganizationError('只能整理完整的普通文件；空文件、目录和链接不会移动。')
    digest = _hash(path, cancel)
    if _identity(first) != _identity(path.lstat()):
        raise OrganizationError('文件正在变化，请停止其他写入后重新预览。')
    return {'identity': _identity(first), 'sha256': digest, 'size': first.st_size}


def _matches(path, proof, *, identity=True, cancel=None):
    actual = _proof(path, cancel)
    return (actual['sha256'] == proof['sha256'] and actual['size'] == proof['size']
            and (not identity or actual['identity'] == proof['identity']))


def _descriptor(journal):
    """动态阶段、临时文件 inode 不参与绑定；所有可操作路径必须与 SQLite 标记匹配。"""
    return {key: journal[key] for key in ('version', 'transaction_id', 'created_at', 'root', 'entries', 'files')}


def _digest(journal):
    return hashlib.sha256(json.dumps(_descriptor(journal), ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _message(error):
    if isinstance(error, (OrganizationError, ValueError)) or type(error).__name__ == 'HistoryStoreError':
        return str(error)
    if isinstance(error, PermissionError):
        return '无法创建目录、移动或重命名文件，请检查访问权限、文件占用和磁盘空间。'
    if isinstance(error, FileExistsError):
        return '预览后目标出现同名文件，未覆盖已有文件；请重新预览。'
    if isinstance(error, FileNotFoundError):
        return '原文件或目标目录在预览后发生变化，原有文件未被覆盖。'
    return '整理未完成，未提交的原文件已保留；请检查目录权限、磁盘空间或重新预览。'


class LibraryOrganizer:
    PLAN_SECONDS = 15 * 60

    def __init__(self, journal_directory):
        self.directory = Path(journal_directory).absolute()
        self.plans = {}
        self.cancel_event = threading.Event()

    def _journal_path(self, transaction_id):
        if len(transaction_id) != 32 or any(c not in 'abcdef0123456789' for c in transaction_id):
            raise OrganizationError('整理日志标记无效，原文件已保留。')
        _safe_path(self.directory, missing=True)
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory / (transaction_id + '.json')

    def _save(self, journal):
        path = self._journal_path(journal['transaction_id'])
        if path.exists():
            _safe_path(path)
        temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as handle:
                json.dump(journal, handle, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)  # 只替换本事务自己的小型日志，绝不替换媒体。
        finally:
            if temporary.exists():
                temporary.unlink()

    def _file(self, old, new, kind, entry):
        proof = _proof(old, self.cancel_event)
        return {'old': str(old), 'new': str(new), 'kind': kind,
                'entry_id': entry['id'], 'task_key': entry['task_key'], **proof}

    def _group(self, entries, root, options, allocator, move_thumbnails):
        entry = entries[0]
        old = _safe_path(entry['path'])
        if any(part.casefold() == '.stream-downloads' for part in old.parts) or old.suffix.lower() in (
                '.part', '.partial', '.tmp', '.ytdl', '.m4s'):
            raise OrganizationError('任务临时文件和孤立分片不能作为最终媒体整理。')
        if any(item['task_state'] != 'completed' or item.get('validation_state') != 'valid' for item in entries):
            raise OrganizationError('只整理下载完成且已通过媒体验证的文件，请先重新验证。')
        destinations = []
        for item in entries:
            values = values_for_entry(item)
            raw = root.joinpath(*classification_parts(options, values))
            _safe_path(raw, missing=True)
            folder = directory_for(root, options, values)
            name = (render_filename(options['filename_template'], values, old.suffix[1:], folder)
                    if options['filename_template'] else sanitize_filename(old.name, filename_limit(folder)))
            destinations.append(folder / name)
        if len({_key(path) for path in destinations}) != 1:
            raise OrganizationError('多条记录引用同一个文件但整理规则结果不同，请分别核对记录。')
        desired = destinations[0]
        conflict = _key(desired) != _key(old) and desired.exists()
        new = desired if _key(desired) == _key(old) else allocator.allocate(
            desired.parent, desired.name, max_length=filename_limit(desired.parent))
        allocator.reserve(new)
        files = [self._file(old, new, 'media', entry)]
        for candidate in entries:
            validation = candidate.get('validation') or {}
            if validation.get('size') and validation['size'] != files[0]['size']:
                raise OrganizationError('媒体大小与最近验证不符，请先重新验证。')
            if validation.get('mtime_ns') and validation['mtime_ns'] != files[0]['identity'][3]:
                raise OrganizationError('媒体修改时间与最近验证不符，请先重新验证。')
        # 只接受明确登记的字幕，不扫描或猜测目录中的其他文件。
        seen = {_key(old)}
        for sidecar in (sidecar for candidate in entries
                        for sidecar in (candidate.get('validation') or {}).get('subtitle_files', [])):
            source = _safe_path(sidecar['path'])
            if _key(source) in seen:
                continue
            if source.suffix.lower() not in ('.srt', '.vtt', '.ass', '.ssa', '.ttml'):
                raise OrganizationError('关联字幕格式无法确认，请先核对详细信息。')
            seen.add(_key(source))
            suffix = source.name[len(old.stem):] if (source.name.startswith(old.stem + '.')
                    and len(source.name[len(old.stem):]) <= 30) else (
                '.' + (sidecar.get('language') or 'und') + '.' + hashlib.sha256(str(source).encode()).hexdigest()[:8]
                + source.suffix)
            limit = min(255, 240 - _units(str(new.parent)) - 1)
            wanted = new.with_name(sanitize_filename(new.stem + suffix, limit))
            target = wanted if _key(wanted) == _key(source) else allocator.allocate(
                wanted.parent, wanted.name, max_length=limit)
            allocator.reserve(target)
            item = self._file(source, target, 'subtitle', entry)
            if sidecar.get('sha256') and sidecar['sha256'] != item['sha256']:
                raise OrganizationError('关联字幕内容已变化，请先重新验证；整组原文件会保留。')
            files.append(item)
        if move_thumbnails:
            for candidate in entries:
                cover = candidate.get('thumbnail_path')
                if not cover or _key(cover) in seen:
                    continue
                source = Path(cover)
                if (source.parent == old.parent and source.stem == old.stem
                        and source.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')):
                    wanted = new.with_suffix(source.suffix)
                    target = wanted if _key(wanted) == _key(source) else allocator.allocate(
                        wanted.parent, wanted.name, max_length=min(255, 240 - _units(str(wanted.parent)) - 1))
                    allocator.reserve(target)
                    files.append(self._file(source, target, 'thumbnail', candidate))
                    seen.add(_key(source))
        for item in files:
            item['owners'] = [{'id': candidate['id'], 'task_key': candidate['task_key']} for candidate in entries
                if (item['kind'] == 'media' or item['kind'] == 'thumbnail' and candidate.get('thumbnail_path')
                    and _key(candidate['thumbnail_path']) == _key(item['old']) or item['kind'] == 'subtitle'
                    and any(_key(sidecar['path']) == _key(item['old']) for sidecar in
                            (candidate.get('validation') or {}).get('subtitle_files', [])))]
        return {'entries': deepcopy(entries), 'files': files, 'conflict': conflict}

    def preview(self, store, entry_ids, root, options=None):
        if not isinstance(entry_ids, (list, tuple)) or not 1 <= len(entry_ids) <= 100:
            raise OrganizationError('一次请选择 1 至 100 条媒体记录。')
        if any(type(value) is not int or value <= 0 for value in entry_ids):
            raise OrganizationError('选择的媒体记录无效，请刷新媒体库。')
        root = _safe_path(root, missing=True)
        if root.exists() and not root.is_dir():
            raise OrganizationError('输出位置不是目录，请选择其他位置。')
        settings = normalize_options(options)
        grouped = {}
        rows = []
        selected = set(entry_ids)
        for entry_id in dict.fromkeys(entry_ids):
            entry = store.get(entry_id)
            if not entry or not entry.get('path'):
                rows.append({'entry_id': entry_id, 'old': '', 'new': '', 'kind': 'media',
                             'status': 'skipped', 'reason': '记录不存在或没有已完成的本地文件。'})
                continue
            grouped.setdefault(_key(entry['path']), []).append(entry)
        allocator = FileNameAllocator()
        groups = []
        for entries in grouped.values():
            try:
                group = self._group(entries, root, settings, allocator,
                                    isinstance(options, dict) and options.get('move_thumbnails') is True)
                references = store.organization_references([item['old'] for item in group['files']])
                owners = {item['id'] for item in entries}
                for item in group['files']:
                    other = references[item['old']] - owners
                    if other:
                        raise OrganizationError('文件被其他' + ('未选中的' if other - selected else '')
                                                + '媒体记录引用，本组不会移动；请先核对共享记录。')
                group['references'] = {path: sorted(ids) for path, ids in references.items()}
                changed = any(_key(item['old']) != _key(item['new']) for item in group['files'])
                if changed:
                    groups.append(group)
                for item in group['files']:
                    rows.append({'entry_id': item['entry_id'], 'old': item['old'], 'new': item['new'],
                        'kind': item['kind'], 'status': 'ready' if changed else 'unchanged',
                        'conflict': group['conflict'] if item['kind'] == 'media' else False,
                        'mkdirs': [str(path) for path in reversed(Path(item['new']).parent.parents)
                                   if not path.exists()] + ([str(Path(item['new']).parent)]
                                   if not Path(item['new']).parent.exists() else []),
                        'reason': ('目标重名，已生成不冲突的新名称。' if group['conflict'] else '')
                                  if changed else '路径保持不变。'})
            except (OSError, ValueError, OrganizationError) as error:
                rows.extend({'entry_id': item['id'], 'old': item.get('path', ''), 'new': '', 'kind': 'media',
                             'status': 'skipped', 'reason': _message(error)} for item in entries)
        token = uuid.uuid4().hex
        now = time.monotonic()
        self.plans = {key: plan for key, plan in self.plans.items() if now - plan['created'] < self.PLAN_SECONDS}
        if len(self.plans) >= 8:
            self.plans.pop(next(iter(self.plans)))
        self.plans[token] = {'created': now, 'root': str(root), 'groups': groups}
        for row in rows:
            row['requires_directory'] = bool(row.get('mkdirs'))
        return {'token': token, 'root': str(root), 'rows': rows, 'items': rows, 'selected_count': len(selected),
                'ready_count': sum(len(group['entries']) for group in groups),
                'skipped_count': sum(row['status'] == 'skipped' for row in rows),
                'unchanged_count': sum(row['kind'] == 'media' and row['status'] == 'unchanged' for row in rows)}

    def _check_group(self, store, group):
        for entry in group['entries']:
            current = store.get(entry['id'])
            if not current or any(current.get(key) != entry.get(key) for key in (
                    'task_key', 'path', 'thumbnail_path', 'validation', 'validation_state', 'task_state', 'size')):
                raise OrganizationError('历史记录在预览后发生变化，请重新预览。')
        references = store.organization_references([item['old'] for item in group['files']])
        if {path: sorted(ids) for path, ids in references.items()} != group['references']:
            raise OrganizationError('预览后出现其他媒体记录引用该文件，请重新预览。')
        for item in group['files']:
            if not _matches(item['old'], item, cancel=self.cancel_event):
                raise OrganizationError('原文件在预览后发生变化，请重新验证后再整理。')
            if _key(item['old']) != _key(item['new']):
                _safe_path(item['new'], missing=True)
                if Path(item['new']).exists():
                    raise FileExistsError(item['new'])

    def _make_journal(self, root, group):
        transaction_id = uuid.uuid4().hex
        files = deepcopy(group['files'])
        for index, item in enumerate(files):
            item['stage'] = str(Path(item['new']).with_name('.mediaflow-organize-' + transaction_id + f'-{index}.part'))
        return {'version': 1, 'transaction_id': transaction_id,
                'created_at': datetime.now(timezone.utc).isoformat(), 'root': root,
                'entries': [{'id': item['id'], 'task_key': item['task_key'], 'old': item['path'],
                             'new': files[0]['new']} for item in group['entries']],
                'files': files, 'state': 'preparing', 'owned': {}, 'created_dirs': []}

    def _mkdir(self, parent, journal):
        missing = []
        target = _safe_path(parent, missing=True)
        for path in (target, *target.parents):
            if path.exists():
                if not path.is_dir():
                    raise NotADirectoryError(str(path))
                break
            missing.append(path)
        for path in reversed(missing):
            try:
                path.mkdir()
                journal['created_dirs'].append(str(path))
                self._save(journal)
            except FileExistsError:
                _safe_path(path)
                if not path.is_dir():
                    raise

    def _copy(self, item, journal, index):
        """先在目标卷独占写入暂存文件，SHA 完全一致才发布；跨卷也不先删原件。"""
        self._mkdir(Path(item['new']).parent, journal)
        source = _safe_path(item['old'])
        stage = _safe_path(item['stage'], missing=True)
        with stage.open('xb') as output:
            journal['owned'][str(index)] = list(_identity(os.fstat(output.fileno()))[:2])
            self._save(journal)
            with source.open('rb') as handle:
                if _identity(os.fstat(handle.fileno())) != item['identity']:
                    raise OrganizationError('原文件在复制前发生变化，原件已保留。')
                digest = hashlib.sha256()
                while data := handle.read(1024 * 1024):
                    if self.cancel_event.is_set():
                        raise OrganizationError('整理已取消，未提交的原文件已保留。')
                    output.write(data)
                    digest.update(data)
                if _identity(os.fstat(handle.fileno())) != item['identity'] or digest.hexdigest() != item['sha256']:
                    raise OrganizationError('原文件在整理期间发生变化，原件已保留。')
            output.flush()
            os.fsync(output.fileno())
        # copy2 的时间戳保留保证原有 ffprobe 结果不会只因移动被误判为变化。
        os.utime(stage, ns=(source.stat().st_atime_ns, item['identity'][3]))
        if not _matches(stage, item, identity=False, cancel=self.cancel_event):
            raise OrganizationError('写入后的文件校验不一致，未移动原件。')
        _safe_path(Path(item['new']).parent)
        if os.name == 'nt':
            os.rename(stage, item['new'])  # Windows rename 原子且不覆盖已有目标。
        else:
            os.link(stage, item['new'])  # POSIX rename 会覆盖，改用独占 link 发布。
            stage.unlink()

    @staticmethod
    def _updates(group):
        mapping = {_key(item['old']): item['new'] for item in group['files']}
        updates = []
        for entry in group['entries']:
            validation = deepcopy(entry.get('validation') or {})
            for sidecar in validation.get('subtitle_files', []):
                sidecar['path'] = mapping.get(_key(sidecar['path']), sidecar['path'])
            updates.append({'id': entry['id'], 'expected': {key: deepcopy(entry.get(key)) for key in (
                'task_key', 'path', 'thumbnail_path', 'validation', 'validation_state', 'size')},
                'path': mapping[_key(entry['path'])], 'validation': validation,
                'thumbnail_path': mapping.get(_key(entry['thumbnail_path']), entry['thumbnail_path'])
                                  if entry.get('thumbnail_path') else ''})
        return updates

    @staticmethod
    def _mappings(journal):
        result = []
        for item in journal['files']:
            if _key(item['old']) == _key(item['new']):
                continue
            owners = item.get('owners') or (journal['entries'] if item['kind'] == 'media' else [item])
            for owner in owners:
                result.append({'old': item['old'], 'new': item['new'], 'kind': item['kind'],
                    'entry_id': owner.get('id', owner.get('entry_id')), 'task_key': owner['task_key']})
        return result

    def _rollback(self, journal):
        problems = []
        for index, item in reversed(list(enumerate(journal['files']))):
            owner = journal.get('owned', {}).get(str(index))
            if not owner:
                if Path(item['stage']).exists():
                    problems.append(item['stage'])
                continue
            for value in (item['stage'], item['new']):
                path = Path(value)
                if not path.exists() or _key(path) == _key(item['old']):
                    continue
                try:
                    _safe_path(path)
                    if list(_identity(path.lstat())[:2]) != owner:
                        raise OrganizationError('目标已被外部程序替换。')
                    if value == item['new'] and not _matches(path, item, identity=False):
                        raise OrganizationError('本次目标内容已被外部程序修改，已保留。')
                    path.unlink()
                except (OSError, OrganizationError):
                    problems.append(str(path))
        for value in reversed(journal.get('created_dirs', [])):
            try:
                path = _safe_path(value)
                path.rmdir()  # 只移除本次创建且仍为空的目录。
            except (OSError, OrganizationError):
                pass
        journal['state'] = 'rollback_pending' if problems else 'rolled_back'
        self._save(journal)
        return problems

    def _cleanup(self, journal):
        pending = []
        for item in journal['files']:
            if _key(item['old']) == _key(item['new']):
                continue
            source = Path(item['old'])
            try:
                if source.exists():
                    if not _matches(source, item):
                        raise OrganizationError('原位置出现其他文件，已保留。')
                    # 提交后仍先确认目标完整，不能因删除原件造成唯一副本丢失。
                    if not _matches(item['new'], item, identity=False):
                        raise OrganizationError('新文件已变化，原件已保留。')
                    source.unlink()
            except (OSError, OrganizationError):
                pending.append(str(source))
        journal['state'] = 'cleanup_pending' if pending else 'completed'
        self._save(journal)
        return pending

    def apply(self, store, token, *, confirmed=False):
        if confirmed is not True:
            raise OrganizationError('必须先查看预览并明确确认，才会移动磁盘文件。')
        plan = self.plans.pop(token, None)
        if plan is None or time.monotonic() - plan['created'] >= self.PLAN_SECONDS:
            raise OrganizationError('整理预览已过期或已经使用，请重新生成预览。')
        result = {'token': token, 'completed': 0, 'failed': 0, 'mappings': [], 'errors': [], 'warnings': []}
        for group in plan['groups']:
            journal = None
            try:
                if self.cancel_event.is_set():
                    raise OrganizationError('整理已取消，未提交的原文件已保留。')
                self._check_group(store, group)
                journal = self._make_journal(plan['root'], group)
                digest = _digest(journal)
                store.register_organization(journal['transaction_id'], digest)
                self._save(journal)
                for index, item in enumerate(journal['files']):
                    if _key(item['old']) != _key(item['new']):
                        self._copy(item, journal, index)
                # 预览后的记录或文件再次变化会整组回滚，而不是提交旧摘要。
                self._check_group_sources(store, group)
                store.commit_organization(journal['transaction_id'], digest, self._updates(group), group['references'])
                journal['state'] = 'committed'
                self._save(journal)
                pending = self._cleanup(journal)
                result['completed'] += len(group['entries'])
                result['mappings'].extend(self._mappings(journal))
                if pending:
                    result['warnings'].append('新路径已提交；部分原件因占用或变化而保留，后台恢复会安全重试。')
            except Exception as error:
                if journal:
                    status = store.organization_transaction(journal['transaction_id'])
                    if status and status['committed']:
                        # DB 已提交后日志写入失败，绝不能回滚有效的新媒体。
                        result['completed'] += len(group['entries'])
                        result['mappings'].extend(self._mappings(journal))
                        result['warnings'].append('新路径已提交，原件清理尚未结束；下次启动会检查本地事务日志。')
                        continue
                    try:
                        leftovers = self._rollback(journal)
                        if leftovers:
                            result['warnings'].append('部分本次写入的目标无法撤回，原件已保留；请检查整理日志。')
                    except (OSError, OrganizationError):
                        result['warnings'].append('整理日志暂不可写，原件已保留；下次启动会再次检查。')
                result['failed'] += len(group['entries'])
                result['errors'].append({'entry_ids': [item['id'] for item in group['entries']], 'reason': _message(error)})
        return result

    def _check_group_sources(self, store, group):
        for entry in group['entries']:
            current = store.get(entry['id'])
            if not current or any(current.get(key) != entry.get(key) for key in (
                    'path', 'task_key', 'validation', 'thumbnail_path', 'validation_state', 'task_state', 'size')):
                raise OrganizationError('历史记录在整理期间发生变化，原件已保留。')
        references = store.organization_references([item['old'] for item in group['files']])
        if {path: sorted(ids) for path, ids in references.items()} != group['references']:
            raise OrganizationError('整理期间出现新的文件引用，原件已保留。')
        for item in group['files']:
            if not _matches(item['old'], item, cancel=self.cancel_event):
                raise OrganizationError('原文件在整理期间发生变化，原件已保留。')

    def recover(self, store):
        """未提交的独占目标撤回；已提交的事务继续清理并始终重放幂等映射。"""
        result = {'mappings': [], 'recovered': 0, 'warnings': []}
        if not self.directory.exists():
            return result
        _safe_path(self.directory)
        journals = []
        for path in self.directory.glob('*.json'):
            try:
                _safe_path(path)
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise OrganizationError('整理日志过大，已保留供人工检查。')
                journal = json.loads(path.read_text(encoding='utf-8'))
                if journal.get('version') != 1 or path.stem != journal.get('transaction_id'):
                    raise OrganizationError('整理日志标记不匹配，原文件已保留。')
                status = store.organization_transaction(journal['transaction_id'])
                if not status or status['digest'] != _digest(journal):
                    raise OrganizationError('整理日志与数据库标记不一致，原文件已保留，请人工检查。')
                journals.append((journal, status))
            except (OSError, ValueError, KeyError, TypeError, OrganizationError):
                result['warnings'].append('有整理日志无法确认，未移动或删除相关文件；请备份历史存储后检查。')
        for journal, status in sorted(journals, key=lambda item: item[0]['created_at']):
            try:
                if not status['committed']:
                    if journal['state'] != 'rolled_back':
                        if self._rollback(journal):
                            result['warnings'].append('未提交事务的部分暂存文件仍被占用，原件已保留。')
                        result['recovered'] += 1
                    continue
                if journal['state'] not in ('completed', 'superseded'):
                    # 后续整理已经改变路径时，仅重放旧映射，不重新创建旧目标。
                    current = [store.get(item['id']) for item in journal['entries']]
                    at_target = all(entry and _key(entry['path']) == _key(item['new'])
                                    for entry, item in zip(current, journal['entries']))
                    if at_target:
                        for index, item in enumerate(journal['files']):
                            if not Path(item['new']).exists() and _matches(item['old'], item):
                                self._copy(item, journal, index)
                            if not _matches(item['new'], item, identity=False):
                                raise OrganizationError('已提交整理的目标文件已变化，原件已保留，请重新验证。')
                        if self._cleanup(journal):
                            result['warnings'].append('整理已提交，部分原件仍被占用或变化，已保留。')
                    else:
                        journal['state'] = 'superseded'
                        self._save(journal)
                    result['recovered'] += 1
                result['mappings'].extend(self._mappings(journal))
            except (OSError, ValueError, KeyError, OrganizationError) as error:
                result['warnings'].append(_message(error))
        return result
