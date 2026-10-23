"""串行发现与既有下载队列之间的编排；请求上下文仍由 TaskStore 管理。"""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import threading
from urllib.parse import urlsplit
import uuid

from PySide6.QtCore import QObject, QTimer, Signal

from core.batch_input import parse_urls
from core.batch_store import BatchStore, BatchStoreError, _options, _safe_text
from core.download_manager import DownloadManager
from core.file_names import sanitize_filename
from core.media_groups import available_candidate
from core.media_quality import quality_candidates, recommend_candidate
from core.task_store import utc_now


TERMINAL = frozenset(('completed', 'failed', 'cancelled'))


def queue_key(job):
    """与历史记录相同的工作目录身份，不依赖可重新分配的数字任务号。"""
    name = Path(job.request.temporary_directory).name.lower().replace('-', '')
    return hashlib.sha256(('workspace:' + name).encode('utf-8')).hexdigest() if name else ''


def automatic_choice(groups, url, preference='auto'):
    choices = []
    for group in groups:
        if not group.selected or group.is_ad or group.is_preview:
            continue
        candidate = recommend_candidate([value for value in quality_candidates(group)
                                         if available_candidate(value)], preference)
        if candidate is None or candidate.requires_confirmation:
            continue
        from core.media_discovery import is_verified_resolver
        evidence = candidate.metadata.get('confidence_evidence', {})
        associated = (evidence.get('current_player') is True or is_verified_resolver(candidate)
                      or url in (candidate.url, candidate.original_url, candidate.final_url))
        if associated:
            choices.append((group, candidate))
    # 多个正片身份仍需要用户选择；分数或请求先后不能证明是哪一个作品。
    return choices[0] if len(choices) == 1 else None


class _BatchDatabase(QObject):
    done = Signal(str, str, object, str)
    stopped = Signal()

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._condition = threading.Condition()
        self._pending = deque()
        self._closing = False
        self._thread = threading.Thread(target=self._run, name='MediaFlowBatches', daemon=True)
        self._thread.start()

    def submit(self, command, payload=None, token=''):
        with self._condition:
            if self._closing:
                return False
            self._pending.append((command, deepcopy(payload), token))
            self._condition.notify()
        return True

    def shutdown(self):
        with self._condition:
            self._closing = True
            self._condition.notify()

    def _run(self):
        store = None
        try:
            while True:
                with self._condition:
                    idle = not self._pending
                if idle and store is not None:
                    store.close()
                    store = None
                with self._condition:
                    self._condition.wait_for(lambda: self._pending or self._closing)
                    if not self._pending:
                        break
                    command, payload, token = self._pending.popleft()
                error, value = '', None
                try:
                    if store is None:
                        store = BatchStore(self.path)
                    if command == 'load':
                        value = store.load()
                    elif command == 'create':
                        store.create_batch(payload['batch'], payload['items'])
                    elif command == 'batch':
                        store.save_batch(payload)
                    elif command == 'items':
                        store.save_items(payload)
                except Exception as exception:
                    error = str(exception) if isinstance(exception, BatchStoreError) else '批次存储操作失败，请检查目录权限和可用空间。'
                    if store is not None:
                        store.close()
                        store = None
                try:
                    self.done.emit(command, token, value, error)
                except RuntimeError:
                    break
        finally:
            if store is not None:
                store.close()
            try:
                self.stopped.emit()
            except RuntimeError:
                pass


class BatchController(QObject):
    changed = Signal()
    message = Signal(str)
    history_event = Signal(dict)
    loaded = Signal()
    closed = Signal()

    def __init__(self, queue, driver, path, template_factory, parent=None):
        super().__init__(parent)
        self.queue, self.driver = queue, driver
        self.template_factory = template_factory
        self.batches, self.items = OrderedDict(), OrderedDict()
        self._pending_creates, self._dirty = set(), set()
        self._candidates = OrderedDict()
        self._links = {}
        self._active = ''
        self.ready = self.is_closed = self._closing = self._db_stopping = False
        self.storage_error = ''
        self._db = _BatchDatabase(path)
        self._db.done.connect(self._database_result)
        self._db.stopped.connect(self._database_closed)
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._pump)
        self._timer.start()
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self._flush)
        self._save_timer.start()
        self._notify = QTimer(self)
        self._notify.setSingleShot(True)
        self._notify.setInterval(80)
        self._notify.timeout.connect(self.changed)
        self.driver.finished.connect(self._discovered)
        self.queue.changed.connect(self._job_changed)
        self.queue.idle.connect(self._pump)
        self._db.submit('load')

    @property
    def active_item_id(self):
        return self._active

    def existing_urls(self):
        values = {item['url'] for item in self.items.values() if item.get('url')}
        for job in self.queue.jobs:
            values.add(job.request.media_metadata.get('page_url') or job.request.url)
        return values

    def create(self, urls, options):
        if not self.ready or self.storage_error or self._closing:
            raise ValueError(self.storage_error or '批次记录正在加载，请稍后再导入。')
        parsed = parse_urls('\n'.join(urls), self.existing_urls())
        if parsed['error'] or parsed['invalid'] or not parsed['urls']:
            raise ValueError(parsed['error'] or '没有可导入的新网址，请检查无效或重复项。')
        now, batch_id = utc_now(), str(uuid.uuid4())
        batch = {'id': batch_id, 'title': '批次 ' + datetime.now().strftime('%m-%d %H:%M:%S'),
                 'created_at': now, 'updated_at': now, 'paused': False, 'options': _options(options)}
        if batch['options'].get('collection_name'):
            from core.output_organization import create_collection
            try:
                directory = create_collection(batch['options']['output_directory'],
                    batch['options']['collection_name'], batch_id)
            except (OSError, ValueError) as error:
                raise ValueError('无法创建独立合集目录：' + str(error)) from None
            batch['options']['collection_directory'] = directory
            batch['options']['collection_name'] = Path(directory).name
            batch['title'] = Path(directory).name
        items = []
        for ordinal, url in enumerate(parsed['urls'], 1):
            item = {'id': str(uuid.uuid4()), 'batch_id': batch_id, 'ordinal': ordinal, 'url': url,
                    'title': f'网址 {ordinal}', 'source_site': urlsplit(url).hostname or '',
                    'state': 'waiting_discovery', 'candidate_count': 0, 'progress': {},
                    'queue_key': '', 'job_id': 0, 'output_path': '', 'error': '',
                    'attempts': 0, 'pause_scope': '', 'previous_state': '',
                    'created_at': now, 'updated_at': now, 'context_error': ''}
            items.append(item)
            self.items[item['id']] = item
        self.batches[batch_id] = batch
        self._pending_creates.add(batch_id)
        # 只有原子存储成功，才能打开页面或交接下载任务。
        self._db.submit('create', {'batch': batch, 'items': items}, batch_id)
        self._changed()
        return batch_id

    def _database_result(self, command, token, value, error):
        if error:
            self.storage_error = error
            self.message.emit(error + ' 为保留恢复信息，已停止新增分析；已有下载可在任务队列管理。')
            if command == 'create':
                self._pending_creates.discard(token)
                batch = self.batches.get(token)
                if batch:
                    batch['paused'] = True
                for item in self.items.values():
                    if item['batch_id'] == token:
                        item.update(state='failed', error=error)
            self._changed()
            return
        if command == 'load':
            self.batches.update((batch['id'], batch) for batch in value['batches'])
            self.items.update((item['id'], item) for item in value['items'])
            for batch in self.batches.values():
                batch['paused'] = True
            for item in self.items.values():
                job = self._job(item)
                if job:
                    self._sync_job(item, job)
                if item['state'] not in TERMINAL:
                    item['previous_state'] = item['state']
                    item['state'] = 'waiting_context' if not item.get('url') else 'paused'
                    item['pause_scope'] = item.get('pause_scope') or 'batch'
                    item['error'] = item.get('context_error') or '重启后已暂停；继续时复用安全进度，失效链接需重新分析。'
                self._dirty.add(item['id'])
            self.ready = True
            self.loaded.emit()
        elif command == 'create':
            self._pending_creates.discard(token)
            # 导入确认是显式开始动作；后续交接尊重队列暂停设置。
            if not self._closing and not self.batches[token]['paused']:
                self.queue.start()
        self._changed()
        self._pump()

    def _changed(self):
        if not self._notify.isActive():
            self._notify.start()

    def _touch(self, item, immediate=False):
        item['updated_at'] = utc_now()
        self._dirty.add(item['id'])
        self._changed()
        if immediate:
            self._flush()

    def _flush(self):
        ids = self._dirty - {key for key, item in self.items.items()
                              if item['batch_id'] in self._pending_creates}
        if ids and not self.storage_error and not self._db_stopping:
            self._db.submit('items', [self.items[key] for key in ids])
            self._dirty.difference_update(ids)

    def _job(self, item):
        key, item_id = item.get('queue_key'), item['id']
        return next((job for job in self.queue.jobs if
                     (key and queue_key(job) == key) or
                     job.request.media_metadata.get('batch_item_id') == item_id), None)

    def _pump(self):
        if self._closing:
            if not self._active and not self.driver.busy and not self.queue.active_count and not self._db_stopping:
                self._flush()
                self._db_stopping = True
                self._db.shutdown()
            return
        if not self.ready or self.storage_error or self._active or self.driver.busy or not self.driver.can_start():
            return
        for item in self.items.values():
            batch = self.batches[item['batch_id']]
            if batch['paused'] or batch['id'] in self._pending_creates or item['state'] != 'waiting_discovery':
                continue
            if not item.get('url'):
                item.update(state='waiting_context', error=item.get('context_error') or '来源网址无法解密，请重新导入原始播放页。')
                self._touch(item, True)
                continue
            self._active = item['id']
            item.update(state='analyzing', error='', attempts=item['attempts'] + 1)
            self._touch(item, True)
            try:
                self.driver.start(item['id'], item['url'], batch['options'])
            except Exception:
                self._discovered(item['id'], {}, '无法启动媒体发现，请检查 WebView2 和浏览器状态。')
            return

    def _discovered(self, item_id, payload, error):
        if item_id != self._active:
            return
        self._active = ''
        item = self.items.get(item_id)
        if item is None or item['state'] != 'analyzing' or self._closing:
            QTimer.singleShot(0, self._pump)
            return
        groups = payload.get('groups', [])
        item['candidate_count'] = len({candidate.id for group in groups for candidate in group.candidates})
        if error:
            self._fail(item, error)
        else:
            manual = payload.get('manual_candidate')
            choice = next(((group, candidate) for group in groups for candidate in quality_candidates(group)
                           if candidate.id == manual and available_candidate(candidate)), None) if manual else None
            choice = choice or automatic_choice(groups, item['url'], self.batches[item['batch_id']]['options']['quality'])
            preference_error = ''
            if choice:
                from core.media_tracks import catalog, default_selection
                preferences = self.batches[item['batch_id']]['options'].get('track_preferences')
                if preferences:
                    try:
                        candidate = deepcopy(choice[1])
                        candidate.metadata['track_selection'] = default_selection(catalog(candidate), preferences)
                        from core.media_tracks import apply_selection
                        candidate = apply_selection(candidate)
                        choice = (choice[0], candidate)
                    except ValueError as exception:
                        preference_error, choice = str(exception), None
            if choice:
                self._handoff(item, *choice)
            elif any(available_candidate(candidate) for group in groups for candidate in group.candidates):
                item.update(state='waiting_selection', error=preference_error or '请查看候选并确认正片及画质。')
                self._candidates[item_id] = deepcopy(groups)
                while len(self._candidates) > 12:
                    evicted, _ = self._candidates.popitem(last=False)
                    self.items[evicted]['error'] = '临时候选已释放；选择时将重新分析原始网址。'
                    self._touch(self.items[evicted])
                self._touch(item, True)
            else:
                reasons = [reason for group in groups for candidate in group.candidates
                           for reason in candidate.unavailable_reasons]
                self._fail(item, '；'.join(dict.fromkeys(reasons))[:2000] or '未发现完整且可下载的媒体；请检查播放器或登录状态后重试。')
        QTimer.singleShot(0, self._pump)

    def candidate_groups(self, item_id):
        return deepcopy(self._candidates.get(item_id, []))

    def select_candidate(self, item_id, candidate_id, confirmed=False, track_selection=None):
        item = self.items.get(item_id)
        if not item or item['state'] != 'waiting_selection' or self._closing:
            return False
        for group in self._candidates.get(item_id, []):
            for candidate in quality_candidates(group):
                if candidate.id != candidate_id or not available_candidate(candidate):
                    continue
                if candidate.requires_confirmation and not confirmed:
                    self.message.emit('该候选需要用户确认正片，不能自动加入下载。')
                    return False
                if track_selection is not None:
                    from core.media_tracks import apply_selection
                    try:
                        candidate = apply_selection(candidate, track_selection)
                    except ValueError as exception:
                        self.message.emit(str(exception))
                        return False
                self._handoff(item, group, candidate)
                return item['state'] != 'failed'
        self.message.emit('候选已释放或不可下载，请重新分析此网址。')
        return False

    def _handoff(self, item, group, candidate):
        if not available_candidate(candidate):
            self._fail(item, '候选未通过完整性或访问资格检查，请重新分析。')
            return
        options = self.batches[item['batch_id']]['options']
        was_paused = self.queue._paused
        self.queue.pause()
        try:
            if options.get('collection_name'):
                from core.output_organization import check_collection
                check_collection(options['output_directory'], options.get('collection_directory', ''), item['batch_id'])
            template = self.template_factory(options)
            template.organization = deepcopy(options.get('organization', {}))
            template.collection_directory = options.get('collection_directory', '')
            request = DownloadManager._request(candidate, group, template)
            request.media_metadata.update(batch_item_id=item['id'], batch_import_id=item['batch_id'], page_url=item['url'])
            title = request.filename
            if options['naming'] == 'index_title':
                title = f'{item["ordinal"]:03d}_' + title
            elif options['naming'] == 'date_title':
                title = datetime.now().strftime('%Y%m%d_') + title
            request.filename = Path(sanitize_filename(title + '.' + request.output_extension)).stem
            request.recovery_enabled = options['retry_limit'] > 0
            request.recovery_max_attempts = options['retry_limit'] + 1
            job = self._job(item)
            if job and job.state == 'completed':
                self._sync_job(item, job)
            elif job:
                # 更新同一任务上下文；已有工作目录与续传身份不重新分配。
                self.queue.retry_with_context(job.id, candidate.request_context, request.media_metadata, candidate.download_payload())
            else:
                job = self.queue.add(request)
                job.media_candidate, job.media_group = deepcopy(candidate), deepcopy(group)
                self._sync_job(item, job)
            self._candidates.pop(item['id'], None)
            item['title'] = _safe_text(group.title or title, 1000, item['url'])
            item['error'] = ''
            self._touch(item, True)
        except Exception as exception:
            self._fail(item, str(exception) if isinstance(exception, ValueError) else '加入下载队列失败，请检查输出目录、工具和任务状态。')
        finally:
            if not was_paused:
                self.queue.start()

    def _sync_job(self, item, job):
        self._links[queue_key(job)] = item['id']
        state = {'queued': 'waiting_download', 'running': 'downloading', 'stopping': 'paused'}.get(job.state, job.state)
        stage = job.progress.get('recovery_state')
        if job.state == 'running' and stage in ('merging', 'verifying'):
            state = stage
        if state in ('paused', 'waiting_context') and not item.get('pause_scope'):
            item['pause_scope'] = 'item'
        if state not in ('paused', 'waiting_context'):
            item['pause_scope'] = ''
        item.update(job_id=job.id, queue_key=queue_key(job), state=state,
                    output_path=job.output_path, progress=deepcopy(job.progress),
                    title=_safe_text(job.name, 1000, item.get('url', '')))
        if state in ('failed', 'waiting_context'):
            item['error'] = _safe_text((job.result or {}).get('error') or job.context_error or job.status, 3000, item.get('url', ''))
        elif state == 'completed':
            item['error'] = ''

    def _job_changed(self, job_id):
        job = self.queue.get(job_id)
        if job is None:
            return
        item_id = job.request.media_metadata.get('batch_item_id') or self._links.get(queue_key(job))
        item = self.items.get(item_id)
        if item:
            previous = item['state']
            self._sync_job(item, job)
            self._touch(item, previous != item['state'])
        if self._closing:
            self._pump()

    def _history(self, item):
        if self._job(item) is not None:
            return
        self.history_event.emit({'task_key': hashlib.sha256(('batch-item:' + item['id']).encode()).hexdigest(),
            'queue_id': 0, 'title': _safe_text(item['title'], 1000, item.get('url', '')),
            'source_site': item['source_site'], 'source_url': item.get('url', ''),
            'task_state': item['state'], 'error': _safe_text(item['error'], 3000, item.get('url', '')),
            'progress': {}, 'started_at': item['created_at'], 'finished_at': item['updated_at'],
            'cancelled_at': item['updated_at'] if item['state'] == 'cancelled' else '', 'files': []})

    def _fail(self, item, error):
        item.update(state='failed', error=_safe_text(error, 3000, item.get('url', '')), pause_scope='')
        self._touch(item, True)
        self._history(item)

    def item_action(self, action, item_id, scope='item'):
        item = self.items.get(item_id)
        if not item or self._closing:
            return
        job = self._job(item)
        try:
            if action == 'pause' and item['state'] not in TERMINAL and item['state'] != 'paused':
                item['previous_state'] = item['state']
                if job:
                    self.queue.pause_job(job.id)
                item.update(state='paused', pause_scope=scope)
                if self._active == item_id:
                    self.driver.cancel()
            elif action == 'cancel' and item['state'] not in ('completed', 'cancelled'):
                if job:
                    self.queue.cancel(job.id)
                item.update(state='cancelled', error='用户取消任务', pause_scope='')
                self._candidates.pop(item_id, None)
                if self._active == item_id:
                    self.driver.cancel()
                self._touch(item, True)
                self._history(item)
            elif action in ('resume', 'retry', 'select') and item['state'] not in ('completed', 'analyzing', 'downloading', 'merging', 'verifying'):
                if not item.get('url'):
                    raise ValueError(item.get('context_error') or '来源网址无法恢复，请重新导入原始播放页。')
                if job and job.worker is not None:
                    raise ValueError('任务仍在停止，请稍后继续。')
                batch = self.batches[item['batch_id']]
                if batch['paused']:
                    batch['paused'] = False
                    self._db.submit('batch', batch)
                if job and job.state != 'waiting_context' and not job.context_error and action != 'select':
                    self.queue.retry(job.id)
                elif action == 'resume' and item['previous_state'] == 'waiting_selection' and item_id in self._candidates:
                    item['state'] = 'waiting_selection'
                else:
                    item.update(state='waiting_discovery', error='', pause_scope='')
                self._touch(item, True)
                if action != 'select':
                    self.queue.start()
            self._touch(item, True)
        except ValueError as exception:
            self.message.emit(_safe_text(str(exception), 2000, item.get('url', '')))
        self._pump()

    def relocate_completed_paths(self, mappings):
        """完成任务已从队列移除时，合集仍需保存媒体库整理后的真实路径。"""
        import os
        changed = 0
        for item in self.items.values():
            if item['state'] != 'completed' or not item.get('output_path'):
                continue
            for mapping in mappings:
                if (mapping.get('kind') != 'media' or mapping.get('task_key') != item.get('queue_key')
                        or os.path.normcase(mapping.get('old', '')).casefold() != os.path.normcase(item['output_path']).casefold()):
                    continue
                item['output_path'] = mapping['new']
                item['title'] = Path(mapping['new']).stem
                self._touch(item, True)
                changed += 1
        return changed

    def batch_action(self, action, batch_id):
        batch = self.batches.get(batch_id)
        if not batch or self._closing:
            return
        members = [item for item in self.items.values() if item['batch_id'] == batch_id]
        if action == 'pause':
            batch['paused'] = True
            for item in members:
                self.item_action('pause', item['id'], 'batch')
        elif action == 'resume':
            batch['paused'] = False
            for item in members:
                if item['state'] == 'paused' and item.get('pause_scope') == 'batch':
                    self.item_action('resume', item['id'])
            self.queue.start()
        elif action == 'cancel':
            batch['paused'] = True
            for item in members:
                self.item_action('cancel', item['id'])
        elif action == 'retry_failed':
            batch['paused'] = False
            for item in members:
                if item['state'] in ('failed', 'waiting_context'):
                    self.item_action('retry', item['id'])
            self.queue.start()
        batch['updated_at'] = utc_now()
        self._db.submit('batch', batch)
        self._changed()
        self._pump()

    def snapshot(self):
        # 界面只收到脱敏展示信息；凭据、完整上下文和签名链接不会进入表格或提示。
        from ui.batch_import import source_display
        rows = []
        for item in self.items.values():
            progress = item.get('progress', {})
            speed = progress.get('speed')
            rows.append({key: item.get(key) for key in ('id', 'batch_id', 'ordinal', 'title', 'source_site',
                'state', 'candidate_count', 'error', 'output_path', 'job_id')} | {
                'percent': progress.get('percent'), 'size': progress.get('total_bytes') or progress.get('downloaded_bytes'),
                'speed': speed if item['state'] == 'downloading' else None,
                'source_display': source_display(item.get('url', ''))})
        return list(self.batches.values()), rows

    def shutdown(self):
        if self._closing:
            self._pump()
            return
        self._closing = True
        for batch in self.batches.values():
            batch['paused'] = True
            self._db.submit('batch', batch)
        for item in self.items.values():
            if item['state'] not in TERMINAL and item['state'] != 'paused':
                item.update(previous_state=item['state'], state='paused', pause_scope='batch')
                self._touch(item)
        if self._active:
            self.driver.cancel()
        self.queue.shutdown()
        self._pump()

    def _database_closed(self):
        self.is_closed = True
        self._timer.stop()
        self._save_timer.stop()
        self.closed.emit()
