"""Real MainWindow/queue/library acceptance, usable by the final packaged EXE.

Only this run's private output directory is changed. Saved regression files are copied
before use. Synthetic history rows are explicitly separate from actual downloads.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog, QMessageBox, QPlainTextEdit

from core.media_history import HistoryStore, snapshot_job
from core.process_tree import NO_WINDOW
from core.tool_manager import APP_DIR
from ui.main_window import MainWindow
from utils.build_info import runtime_identity
from utils.config import ConfigStore


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class _AudioServer:
    def __init__(self, path):
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_HEAD(self):
                self.serve(True)

            def do_GET(self):
                self.serve(False)

            def serve(self, head):
                code = 200 if self.path == '/audio.mp3' else 404
                data = path.read_bytes() if code == 200 else b'Not Found'
                self.send_response(code)
                self.send_header('Content-Type', 'audio/mpeg' if code == 200 else 'text/plain')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                owner.requests.append({'method': self.command, 'status': code, 'bytes': 0 if head else len(data)})
                if not head:
                    self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.1}, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/audio.mp3'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def run_library_acceptance(app, args):
    def option(flag, default=''):
        return args[args.index(flag) + 1] if flag in args else default

    directory = Path(option('--library-check')).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stage = option('--library-stage', 'full')
    report_path = directory / ('restart-result.json' if stage == 'restart' else 'result.json')
    report = {'runtime_identity': runtime_identity(), 'stage': stage, 'checks': [], 'passed': False,
              'actual_downloads': [], 'saved_media_regression': [], 'file_operations': [],
              'synthetic_index_rows': 0, 'private_directory': str(directory),
              'external_websites_downloaded': False}
    window, fixture, audio_server = None, None, None
    ticks, messages = [], []
    app.setQuitOnLastWindowClosed(False)
    timer = QTimer()
    timer.setInterval(25)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    started = time.monotonic()

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(condition, description, **proof):
        if not condition:
            raise AssertionError(description)
        report['checks'].append({'description': description, **proof})
        save()

    def wait(predicate, seconds=30, reason='等待后台操作超时'):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        raise TimeoutError(reason)

    def background(function, seconds=40):
        state = {}
        done = threading.Event()
        def run():
            try:
                state['value'] = function()
            except Exception as error:
                state['error'] = error
            finally:
                done.set()
        threading.Thread(target=run, name='MediaFlow-library-acceptance', daemon=True).start()
        wait(done.is_set, seconds, '后台验收步骤超时')
        if 'error' in state:
            raise state['error']
        return state.get('value')

    def database(function):
        def run():
            store = HistoryStore(directory / 'history.sqlite3')
            try:
                return function(store)
            finally:
                store.close()
        return background(run)

    def page(predicate=lambda _: True, seconds=15):
        lib = window.media_library
        lib._debounce.stop()
        lib.refresh()
        deadline, retry = time.monotonic() + seconds, time.monotonic() + 0.2
        while time.monotonic() < deadline:
            app.processEvents()
            if not lib._loading:
                if predicate(lib):
                    return lib
                if time.monotonic() >= retry:
                    lib.refresh()
                    retry = time.monotonic() + 0.2
            time.sleep(0.005)
        raise TimeoutError('媒体库查询未得到预期结果：' + lib.summary.text() + ' / ' + lib.notice.text())

    def select(entry_id, search=''):
        lib = window.media_library
        lib.search.setText(search)
        page(lambda view: any(item['id'] == entry_id for item in view.items))
        row = next(index for index, item in enumerate(lib.items) if item['id'] == entry_id)
        lib.table.selectRow(row)
        return lib.selected_entry()

    def entry_for(job):
        key = snapshot_job(job).get('task_key')
        return database(lambda store: next((item for item in store.query(view='history', search=job.name,
                            limit=100)['items'] if item['task_key'] == key), None))

    def validate_entry(entry_id, filename):
        lib = window.media_library
        select(entry_id, filename)
        lib.buttons['validate'].click()
        wait(lambda: window.library_service.validator is None and not window.library_service._validation_pending,
             90, '真实 FFmpeg 媒体验证未结束')
        page(lambda view: any(item['id'] == entry_id and
             (item.get('validation') or {}).get('full_decode', {}).get('status') == 'passed' for item in view.items), 20)
        return next(item for item in lib.items if item['id'] == entry_id)

    def close_window():
        nonlocal window
        if window is None:
            return
        window.queue.pause()
        wait(lambda: not window.queue.active_count and not window.thumbnails.busy, 35,
             '下载或缩略图后台任务没有正常结束')
        if window.detector is not None:
            window.detector.cancel()
        wait(lambda: window.detector is None, 10, '工具检测没有退出')
        old = window
        old.close()
        wait(lambda: old.library_service.is_closed and not old.isVisible(), 20,
             '历史存储没有完成关闭')
        window = None

    def open_window():
        nonlocal window
        window = MainWindow(ConfigStore(directory / 'config.json'))
        window.showMinimized()
        window.library_service.message.connect(messages.append)
        wait(lambda: window.detector is None and window.yt_info.found and window.ff_info.found
             and window.probe_info.found, 35, '当前发行工具未能自动定位并运行')
        window.input_tabs.setCurrentWidget(window.media_library)
        page()
        return window.media_library

    def real_dialog(button, *, yes=None, filename='', full_path='', details=False):
        observed = {}
        poll = QTimer()
        poll.setInterval(10)
        deadline = time.monotonic() + 8
        def inspect():
            dialogs = [widget for widget in app.topLevelWidgets() if isinstance(widget, QDialog)
                       and widget.isVisible() and widget is not window]
            expected = [dialog for dialog in dialogs if
                        (dialog.windowTitle() == '媒体详细信息' if details else
                         isinstance(dialog, QMessageBox) and dialog.windowTitle() == '确认删除磁盘文件')]
            if expected:
                dialog = expected[0]
                # Keep the real native dialog from occupying the user's foreground.
                dialog.setWindowState(dialog.windowState() | Qt.WindowState.WindowMinimized)
                text = '\n'.join(edit.toPlainText() for edit in dialog.findChildren(QPlainTextEdit)) if details else dialog.text()
                observed.update(text_has_filename=filename in text,
                                text_has_full_path=full_path in text,
                                title=dialog.windowTitle(), answer='details' if details else 'Yes' if yes else 'No')
                screenshot = directory / ('details.png' if details else 'delete-yes.png' if yes else 'delete-no.png')
                observed['screenshot_saved'] = bool(dialog.grab().save(str(screenshot)))
                poll.stop()
                if details:
                    dialog.accept()
                else:
                    answer = QMessageBox.StandardButton.Yes if yes and observed['text_has_filename'] and observed['text_has_full_path'] else QMessageBox.StandardButton.No
                    dialog.button(answer).click()
                observed['done'] = True
            elif time.monotonic() > deadline:
                observed.update(done=True, error='未出现预期的真实确认对话框')
                poll.stop()
                for dialog in dialogs:
                    dialog.reject()
        poll.timeout.connect(inspect)
        poll.start()
        button.click()
        wait(lambda: observed.get('done'), 10, '真实对话框未完成')
        poll.stop()
        check(not observed.get('error') and observed.get('text_has_filename') and observed.get('text_has_full_path'),
              '真实详情或删除确认展示文件名和完整路径', proof=observed)
        return observed

    def restart_proof(expected):
        lib = open_window()
        page(lambda view: view.total == expected['library_total'])
        lib.view_mode.setCurrentIndex(1)
        page(lambda view: view.total == expected['history_total'])
        restored = [job for job in window.queue.jobs if job.state == 'paused']
        check(any(job.name == expected['paused_name'] for job in restored),
              '重启后旧任务恢复数据仍可恢复为暂停状态')
        for proof in expected['entries']:
            item = database(lambda store, key=proof['id']: store.get(key))
            check(bool(item) and item['path'] == proof['path'] and item['task_state'] == proof['task_state'],
                  '重启后媒体路径及历史状态保持', entry_id=proof['id'])
        check(database(lambda store: store.get(expected['removed_id'])) is None,
              '仅移除的历史记录没有被恢复任务重新导入')
        check(Path(expected['retained_file']).is_file() and _sha(expected['retained_file']) == expected['retained_sha256'],
              '重启后仅移除记录对应的媒体仍完整保留')
        report['restart_counts'] = {'library': expected['library_total'], 'history': expected['history_total'],
                                    'restored_paused_tasks': len(restored)}
        window.grab().save(str(directory / ('restart-process.png' if stage == 'restart' else 'restart-window.png')))

    try:
        if stage not in ('full', 'restart'):
            raise ValueError('未知媒体库验收阶段。')
        if stage == 'restart':
            expected = json.loads((directory / 'restart-state.json').read_text(encoding='utf-8'))
            timer.start()
            restart_proof(expected)
            report['new_process_reopened_same_private_database'] = True
        else:
            if (directory / 'downloads').exists() or (directory / 'history.sqlite3').exists():
                raise ValueError('完整验收需要新的输出目录，以保留此前结果。')
            downloads = directory / 'downloads'
            downloads.mkdir()
            ConfigStore(directory / 'config.json').save({'output_directory': str(downloads)})
            timer.start()
            lib = open_window()
            check(lib.total == 0 and lib.table.rowCount() == 0 and lib.empty.isVisible(),
                  '首次启动显示独立媒体库入口和空状态')
            report['tools'] = {name: asdict(info) for name, info in
                               (('yt-dlp', window.yt_info), ('ffmpeg', window.ff_info), ('ffprobe', window.probe_info))}

            from tests.hls_fixture import HlsFixture, FIXTURE_HEADERS
            import tests.hls_fixture as fixture_module
            fixture_module.ROOT = APP_DIR
            fixture = HlsFixture(directory / 'fixtures')
            background(fixture.generate, 60)
            fixture.start()
            audio = directory / 'fixtures' / 'audio.mp3'
            background(lambda: subprocess.run([window.ff_info.path, '-v', 'error', '-nostdin', '-y',
                '-f', 'lavfi', '-i', 'sine=frequency=660:duration=1.2', '-c:a', 'libmp3lame', str(audio)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=20))
            audio_server = _AudioServer(audio)
            jobs, entries = {}, {}
            window.output_directory.setText(str(downloads))
            for label, route, headers in (('direct', fixture.base + '/plain/direct.mp4', {}),
                    ('hls', fixture.base + '/plain/index.m3u8', {}),
                    ('dash', '/dash/endpoint', FIXTURE_HEADERS), ('audio', audio_server.url, {})):
                window.queue.pause()
                window.parse_text(fixture.curl('cmd', route, headers) if headers else route)
                window.filename.setText('library-' + label)
                if label == 'audio':
                    request = window._collect_request()
                    request.output_extension = 'mp3'
                    request.download_protocol = 'Direct'
                    job = window.queue.add(request)
                    window.select_job(job.id)
                else:
                    job = window.add_current_to_queue()
                check(job is not None, label + ' 已经通过生产输入与队列加入任务')
                job.request.recovery_max_attempts = 1
                window.start_queue()
                wait(lambda: job.worker is None and job.state in ('completed', 'failed', 'cancelled'),
                     100, label + ' 本地真实下载未结束')
                result = job.result or {}
                check(job.state == 'completed' and result.get('success') is True,
                      label + ' 经生产 DownloadWorker 真实下载成功', error_code=result.get('error_code', ''))
                wait(lambda: not window.thumbnails.busy, 25)
                lib.reset_filters()
                page(lambda view: any(item.get('queue_id') == job.id for item in view.items))
                entry = entry_for(job)
                check(bool(entry) and entry['task_state'] == 'completed' and entry['validation_state'] == 'valid'
                      and entry['size'] > 0 and entry['duration'] > 0,
                      label + ' 完成回调自动保存真实媒体元数据')
                kind = 'audio' if label == 'audio' else 'video'
                check(entry['media_type'] == kind, label + ' 媒体类型正确')
                if kind == 'video':
                    check(bool(entry['thumbnail_path']) and Path(entry['thumbnail_path']).is_file(),
                          label + ' 复用下载完成缩略图')
                    select(entry['id'], Path(entry['path']).name)
                    wait(lambda: lib.table.item(lib.table.currentRow(), 0).icon().cacheKey()
                         != lib._placeholder('video').cacheKey(), 10,
                         label + ' 的真实缩略图没有回填到媒体列表')
                    check(True, label + ' 的真实缩略图已异步显示在媒体列表')
                else:
                    select(entry['id'], Path(entry['path']).name)
                    check(not lib.table.item(lib.table.currentRow(), 0).icon().isNull(),
                          '没有封面的音频立即显示类型占位图')
                jobs[label], entries[label] = job, entry
                report['actual_downloads'].append({'kind': label, 'state': job.state, 'queue_id': job.id,
                    'history_id': entry['id'], 'file': entry['path'], 'size': entry['size'],
                    'duration': entry['duration'], 'media_type': entry['media_type'],
                    'container': entry['container'], 'thumbnail_path': entry['thumbnail_path']})
            check(any(item['method'] == 'GET' and item['status'] == 200 and item['bytes'] > 0
                      for item in audio_server.requests), 'MP3 有真实 localhost HTTP 字节传输证据')
            report['local_media_request_count'] = len(fixture.requests)

            # Failed and cancelled events use the production queue, including the queued-cancel path.
            window.queue.pause()
            window.parse_text(fixture.base + '/plain/direct.mp4')
            window.filename.setText('library-cancelled')
            cancelled = window.add_current_to_queue()
            window.queue.cancel(cancelled.id)
            window.filename.setText('library-paused')
            paused = window.add_current_to_queue()
            window.queue.pause_job(paused.id)
            window.parse_text(fixture.base + '/missing-resource.mp4')
            window.filename.setText('library-failed')
            failed = window.add_current_to_queue()
            failed.request.recovery_max_attempts = 1
            window.start_queue()
            wait(lambda: failed.worker is None and failed.state == 'failed', 60, 'HTTP 404 任务没有结束')
            lib.reset_filters()
            lib.view_mode.setCurrentIndex(1)
            page(lambda view: view.total == 6)
            states = {item['title']: item for item in lib.items}
            check(states[cancelled.name]['task_state'] == 'cancelled' and bool(states[cancelled.name]['cancelled_at']),
                  '排队任务取消后记录取消状态和时间')
            check(states[failed.name]['task_state'] == 'failed' and bool(states[failed.name]['error']),
                  '真实 HTTP 404 失败保留明确失败原因')
            check(paused.name not in states and paused.state == 'paused', '暂停任务没有被误记为取消历史')
            report['terminal_history'] = {name: {'state': item['task_state'], 'reason': item['error'],
                'cancelled_at': item['cancelled_at']} for name, item in states.items() if item['task_state'] != 'completed'}

            # User controls: filename search, type/source filtering, all four sorts, reset.
            lib.view_mode.setCurrentIndex(0)
            lib.reset_filters()
            lib.search.setText(Path(entries['audio']['path']).name)
            page(lambda view: view.total == 1 and view.items[0]['id'] == entries['audio']['id'])
            check(True, '按文件名搜索只展示对应音频')
            lib.reset_filters()
            lib.media_type.setCurrentIndex(lib.media_type.findData('audio'))
            page(lambda view: view.total == 1 and all(item['media_type'] == 'audio' for item in view.items))
            check(True, '媒体类型筛选正确')
            lib.reset_filters()
            page(lambda view: view.total == 4)
            site = entries['direct']['source_site']
            lib.source_site.setCurrentIndex(lib.source_site.findData(site))
            page(lambda view: view.total == 4 and all(item['source_site'] == site for item in view.items))
            check(True, '来源网站筛选正确')
            lib.reset_filters()
            for sort in ('date', 'size', 'duration', 'resolution'):
                lib.sort.setCurrentIndex(lib.sort.findData(sort))
                page()
                values = [item['finished_at'] if sort == 'date' else item['size'] if sort == 'size' else
                          item['duration'] if sort == 'duration' else item['width'] * item['height'] for item in lib.items]
                check(values == sorted(values, reverse=True), '媒体库按 ' + sort + ' 正确排序')
            lib.reset_filters()
            page(lambda view: view.total == 4)
            check(not lib.search.text() and not lib.media_type.currentData() and not lib.source_site.currentData(),
                  '清空筛选恢复完整媒体列表')
            wait(lambda: all(lib.table.item(row, 0).icon().cacheKey() != lib._placeholder('video').cacheKey()
                 for row, item in enumerate(lib.items) if item.get('thumbnail_path')), 10,
                 '媒体库总览中的真实缩略图未完成回填')
            window.grab().save(str(directory / 'library.png'))

            for kind in ('direct', 'audio'):
                entry = entries[kind]
                verified = validate_entry(entry['id'], Path(entry['path']).name)
                check(verified['validation_state'] == 'valid' and verified['validation']['full_decode']['status'] == 'passed',
                      kind + ' 点击重新验证后真实完整读取全部媒体轨道')
            selected = select(entries['direct']['id'], Path(entries['direct']['path']).name)
            check(all(button.isEnabled() for button in lib.buttons.values()),
                  '打开文件、文件夹、来源、详情和验证操作均可用（不启动用户外部应用）')
            real_dialog(lib.buttons['details'], details=True, filename=Path(selected['path']).name,
                        full_path=selected['path'])

            # All mutations below are confined to this run's downloaded copies.
            moved = downloads / 'moved' / Path(entries['direct']['path']).name
            moved.parent.mkdir()
            original_direct = Path(entries['direct']['path'])
            original_direct.rename(moved)
            lib.reset_filters()
            page(lambda view: any(item['id'] == entries['direct']['id'] and item['file_state'] == 'missing'
                                  for item in view.items))
            check(True, '文件移动后保留历史并显示已移动或丢失')
            window.library_service.action('relocate', entries['direct']['id'], path=str(moved))
            page(lambda view: any(item['id'] == entries['direct']['id'] and item['path'] == str(moved)
                                  and item['validation_state'] == 'unverified' for item in view.items))
            entries['direct'] = validate_entry(entries['direct']['id'], moved.name)
            check(entries['direct']['validation_state'] == 'valid', '重新定位文件后可再次完整验证')

            retained = Path(entries['hls']['path'])
            retained_sha = background(lambda: _sha(retained))
            select(entries['hls']['id'], retained.name)
            lib.remove_button.click()
            page(lambda view: view.total == 0)
            check(retained.is_file() and background(lambda: _sha(retained)) == retained_sha,
                  '仅移除历史记录不会删除或更改实际媒体文件')
            check(database(lambda store: store.get(entries['hls']['id'])) is None, '已移除的记录从历史中消失')

            # A filesystem deletion is independent of history deletion.
            audio_copy = downloads / 'relocated-audio.mp3'
            shutil.copy2(entries['audio']['path'], audio_copy)
            Path(entries['audio']['path']).unlink()
            lib.reset_filters()
            page(lambda view: any(item['id'] == entries['audio']['id'] and item['file_state'] == 'missing' for item in view.items))
            check(True, '磁盘文件被外部删除后仍保留历史记录')
            window.library_service.action('relocate', entries['audio']['id'], path=str(audio_copy))
            page(lambda view: any(item['id'] == entries['audio']['id'] and item['path'] == str(audio_copy) for item in view.items))
            entries['audio'] = validate_entry(entries['audio']['id'], audio_copy.name)

            disk = entries['dash']
            select(disk['id'], Path(disk['path']).name)
            no = real_dialog(lib.delete_button, yes=False, filename=Path(disk['path']).name, full_path=disk['path'])
            check(Path(disk['path']).is_file(), '删除磁盘文件选择 No 后文件保持存在')
            yes = real_dialog(lib.delete_button, yes=True, filename=Path(disk['path']).name, full_path=disk['path'])
            wait(lambda: not Path(disk['path']).exists(), 10, '确认后磁盘文件没有删除')
            page(lambda view: any(item['id'] == disk['id'] and item['file_state'] == 'missing' for item in view.items))
            check(database(lambda store: store.get(disk['id'])) is not None, '确认删除磁盘文件后历史记录仍保留')
            report['file_operations'].extend([{'operation': 'delete_no', 'proof': no},
                                               {'operation': 'delete_yes', 'proof': yes}])
            bad = downloads / 'damaged-media.mp4'
            bad.write_bytes(b'<html>deliberately invalid local acceptance media</html>')
            window.library_service.action('relocate', disk['id'], path=str(bad))
            lib.search.clear()
            page(lambda view: any(item['id'] == disk['id'] and item['path'] == str(bad) for item in view.items))
            select(disk['id'], bad.name)
            lib.buttons['validate'].click()
            wait(lambda: window.library_service.validator is None and not window.library_service._validation_pending, 30)
            page(lambda view: any(item['id'] == disk['id'] and item['validation_state'] == 'failed' for item in view.items))
            check(bad.read_bytes().startswith(b'<html>') and bool(lib.selected_entry()['validation_reason']),
                  '损坏文件显示具体校验失败原因，原文件不被覆盖')
            window.grab().save(str(directory / 'validation-failed.png'))

            # Optional regression imports are explicit copies of previously downloaded files.
            manifest_name = option('--library-saved-manifest')
            if manifest_name:
                manifest = json.loads(Path(manifest_name).read_text(encoding='utf-8-sig'))
                samples = manifest if isinstance(manifest, list) else manifest.get('samples', [])
                for index, sample in enumerate(samples):
                    source = Path(sample.get('source_path') or sample.get('path') or sample.get('source', ''))
                    label = str(sample.get('label') or ('saved-media-' + str(index)))
                    target = directory / 'imported' / (str(index) + '-' + source.name)
                    target.parent.mkdir(exist_ok=True)
                    source_hash = background(lambda: _sha(source))
                    background(lambda: shutil.copy2(source, target))
                    imported = deepcopy(snapshot_job(jobs['direct']))
                    source_url = sample.get('page_url') or sample.get('source_url', '')
                    imported.update(task_key=hashlib.sha256(('saved-copy:' + str(target)).encode()).hexdigest(),
                        title=label + ' (历史媒体离线回归)',
                        source_site=sample.get('source_site') or (urlsplit(source_url).hostname or '').removeprefix('www.'),
                        source_url=source_url, files=[{'path': str(target), 'validation': {}}])
                    database(lambda store: store.record(imported))
                    lib.reset_filters()
                    lib.search.setText(target.name)
                    page(lambda view: view.total == 1)
                    imported_entry = validate_entry(lib.items[0]['id'], target.name)
                    check(imported_entry['validation_state'] == 'valid' and background(lambda: _sha(source)) == source_hash
                          and background(lambda: _sha(target)) == source_hash,
                          label + ' 历史媒体复制后完整解码通过，原样本保持不变')
                    report['saved_media_regression'].append({'label': label, 'copied_to': str(target),
                        'original_sha256': source_hash, 'duration': imported_entry['duration'],
                        'width': imported_entry['width'], 'height': imported_entry['height'],
                        'full_decode': imported_entry['validation'].get('full_decode'),
                        'new_download': False, 'copied_existing_verified_media': True})

            window.queue.remove_finished()
            lib.reset_filters()
            page()
            before_pressure = database(lambda store: store.query(view='history')['total'])
            check(before_pressure >= 5 and all(job.state == 'paused' for job in window.queue.jobs),
                  '清理活动队列中的已结束任务不会清理媒体库和历史')

            pressure_ticks_start = len(ticks)
            template = deepcopy(snapshot_job(jobs['direct']))
            template['files'] = [{'path': str(moved), 'validation': deepcopy(jobs['direct'].result['output_validation'][0])}]
            template['source_site'] = 'index-pressure.local'
            template['source_url'] = ''
            def seed():
                store = HistoryStore(directory / 'history.sqlite3')
                try:
                    for index in range(10000):
                        snapshot = deepcopy(template)
                        snapshot['task_key'] = hashlib.sha256(('synthetic-index:' + str(index)).encode()).hexdigest()
                        snapshot['title'] = '合成目录索引压力记录-' + str(index).zfill(5)
                        store.record(snapshot)
                finally:
                    store.close()
            background(seed, 180)
            report['synthetic_index_rows'] = 10000
            report['synthetic_index_note'] = '只用于目录分页压力测试，复用本次下载文件的只读索引，不代表新增下载。'
            timings = []
            lib.view_mode.setCurrentIndex(1)
            lib.reset_filters()
            for unused in range(4):
                begin = time.monotonic()
                page(lambda view: view.total == before_pressure + 10000 and len(view.items) == 50)
                timings.append(time.monotonic() - begin)
                if lib.next.isEnabled():
                    lib.next.click()
                    page()
            pressure_ticks = ticks[pressure_ticks_start:]
            gap = max((second-first for first, second in zip(pressure_ticks, pressure_ticks[1:])), default=0)
            check(all(value < 0.5 for value in timings) and gap < 0.5,
                  '一万条目录索引后台写入和分页期间 UI 持续响应',
                  page_seconds=[round(value, 4) for value in timings], max_heartbeat_gap=round(gap, 4))
            report['pressure'] = {'rows': 10000, 'page_size': len(lib.items),
                'page_seconds': [round(value, 4) for value in timings], 'ui_ticks': len(pressure_ticks),
                'max_tick_gap_seconds': round(gap, 4)}
            lib.search.setText('合成目录索引压力记录-09999')
            page(lambda view: view.total == 1)
            lib.reset_button.click()
            page(lambda view: view.total == before_pressure + 10000 and len(view.items) == 50)
            check(True, '大量记录下搜索及清空筛选恢复总数且每页仍仅50条')
            window.grab().save(str(directory / 'large-history.png'))

            counts = database(lambda store: {'library_total': store.query(view='library')['total'],
                                            'history_total': store.query(view='history')['total']})
            expected = {**counts, 'paused_name': paused.name, 'removed_id': entries['hls']['id'],
                'retained_file': str(retained), 'retained_sha256': retained_sha,
                'entries': [database(lambda store, key=entries[kind]['id']: store.get(key))
                            for kind in ('direct', 'audio', 'dash')]}
            expected['entries'] = [{key: item[key] for key in ('id', 'path', 'task_state')} for item in expected['entries']]
            (directory / 'restart-state.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding='utf-8')
            report['counts'] = counts
            close_window()
            restart_proof(expected)
            check(True, '关闭真实窗口并重新创建后历史库与暂停任务恢复成功')
        close_window()
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
        report['error_type'] = type(error).__name__
    finally:
        try:
            if window is not None:
                window.queue.stop_all()
                close_window()
        except Exception as error:
            report['cleanup_error'] = type(error).__name__ + ': ' + str(error)
            report['passed'] = False
        if audio_server:
            audio_server.close()
        if fixture:
            fixture.close()
        timer.stop()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['ui_ticks'] = len(ticks)
        report['max_ui_tick_gap_seconds'] = round(max((second-first for first, second in zip(ticks, ticks[1:])), default=0), 4)
        report['messages'] = messages[-20:]
        save()
    return 0 if report['passed'] else 1
