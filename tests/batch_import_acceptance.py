"""1.4.0 成品入口：仅 localhost 的真实批量发现、下载和跨进程恢复。

main.py 的入口约定：run_batch_import_acceptance(app, sys.argv)，参数为
--batch-import-check <新目录> --batch-import-stage first|restart --native-smoke。
不替换下载器、不构造 MediaCandidate、不修改评分，也不访问外部媒体网站。
"""
from __future__ import annotations

import hashlib
import faulthandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.parse import urlsplit

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from core.media_history import HistoryStore, snapshot_job
from core.tool_manager import APP_DIR
from tests.hls_fixture import HlsFixture
from ui.batch_discovery import BatchCandidateDialog
from ui.batch_import import BatchImportDialog
from ui.discovery_dialog import DiscoveryDialog
from ui.main_window import MainWindow
from utils.build_info import runtime_identity
from utils.config import ConfigStore


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class _LocalBatchPages:
    """复用 HlsFixture 字节，每个页面拥有互不相同的媒体资源目录。"""
    KINDS = {'direct': 'Direct', 'hls': 'HLS', 'dash': 'DASH',
             'restart-complete': 'Direct', 'restart-hls': 'HLS', 'cancel': 'Direct'}

    def __init__(self, directory, port=0):
        self.directory = Path(directory)
        self.requests = []
        self.lock = threading.Lock()
        self.segment_active = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def do_HEAD(self):
                self.serve(True)

            def do_GET(self):
                self.serve(False)

            def serve(self, head):
                parsed = urlsplit(self.path)
                path, status, mime, body, identity = parsed.path, 404, 'text/plain', b'Not Found', ''
                if path.startswith('/page/'):
                    identity = path.rsplit('/', 1)[-1]
                    if identity in owner.KINDS or identity == 'no-media':
                        status, mime = 200, 'text/html;charset=utf-8'
                        if identity == 'no-media':
                            html = '<html><head><title>No media</title></head><body>没有可下载媒体</body></html>'
                        else:
                            extension = {'Direct': 'direct.mp4', 'HLS': 'source.m3u8', 'DASH': 'source.mpd'}[owner.KINDS[identity]]
                            resource = '/media/' + identity + '/' + extension
                            html = ('<!doctype html><html><head><meta charset="utf-8"><title>Shared Video</title></head>'
                                '<body style="margin:0"><video id="main" title="Shared Video" data-video-id="' + identity +
                                '" controls muted autoplay loop preload="auto" style="width:640px;height:360px" src="' + resource +
                                '"></video><script>fetch(' + json.dumps(resource) +
                                ',{headers:{Range:"bytes=0-"}}).then(r=>r.arrayBuffer());</script></body></html>')
                        body = html.encode('utf-8')
                elif path.startswith('/media/'):
                    parts = path.split('/')
                    if len(parts) == 4:
                        identity, filename = parts[2:]
                        kind = owner.KINDS.get(identity)
                        target = None
                        if kind == 'Direct' and filename == 'direct.mp4':
                            target = owner.directory / filename
                        elif kind == 'HLS' and (filename == 'source.m3u8' or re.fullmatch(r'part\d+\.ts', filename)):
                            target = owner.directory / filename
                        elif kind == 'DASH' and (filename == 'source.mpd' or re.fullmatch(r'(?:init|chunk)-stream\d+(?:-\d+)?\.m4s', filename)):
                            target = owner.directory / 'dash' / filename
                        if target is not None and target.is_file():
                            status, body = 200, target.read_bytes()
                            mime = {'.mp4': 'video/mp4', '.m3u8': 'application/vnd.apple.mpegurl',
                                    '.mpd': 'application/dash+xml', '.ts': 'video/mp2t',
                                    '.m4s': 'video/iso.segment'}.get(target.suffix, 'application/octet-stream')
                complete_length = len(body)
                content_range = ''
                request_range = self.headers.get('Range', '')
                if status == 200 and path.startswith('/media/') and request_range:
                    match = re.fullmatch(r'bytes=(\d+)-(\d*)', request_range)
                    if match:
                        start = int(match[1])
                        end = min(complete_length - 1, int(match[2]) if match[2] else complete_length - 1)
                        if start >= complete_length or end < start:
                            status, body = 416, b''
                            content_range = f'bytes */{complete_length}'
                        else:
                            status, body = 206, body[start:end + 1]
                            content_range = f'bytes {start}-{end}/{complete_length}'
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('ETag', '"batch-fixture-' + hashlib.sha256(path.encode()).hexdigest()[:20] + '"')
                if content_range:
                    self.send_header('Content-Range', content_range)
                self.end_headers()
                record = {'path': path, 'method': self.command, 'status': status,
                          'range': request_range, 'bytes_sent': 0, 'at': time.monotonic()}
                segment = not head and path.endswith('.ts') and status in (200, 206)
                with owner.lock:
                    owner.requests.append(record)
                    if segment:
                        owner.segment_active[identity] = owner.segment_active.get(identity, 0) + 1
                try:
                    if segment:
                        time.sleep(0.8)
                    if not head:
                        self.wfile.write(body)
                        record['bytes_sent'] = len(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    record['connection_closed_early'] = True
                finally:
                    if segment:
                        with owner.lock:
                            owner.segment_active[identity] -= 1

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval': 0.1}, name='batch-acceptance-http', daemon=True)
        self.thread.start()

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def run_batch_import_acceptance(app, args):
    def option(flag, default=''):
        return args[args.index(flag) + 1] if flag in args else default

    directory = Path(option('--batch-import-check')).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    traceback_file = (directory / 'threads.txt').open('a', encoding='utf-8')
    faulthandler.dump_traceback_later(60, repeat=True, file=traceback_file)
    stage = option('--batch-import-stage', 'first')
    report_path = directory / ('restart-result.json' if stage == 'restart' else 'result.json')
    report = {'passed': False, 'stage': stage, 'runtime_identity': runtime_identity(),
        'private_directory': str(directory), 'checks': [], 'actual_downloads': [],
        'discovery_pages': [], 'manual_confirmations': [], 'external_websites_downloaded': False}
    window = server = None
    started = time.monotonic()
    ticks, messages, discovery = [], [], []
    observed_dialogs = set()
    browser_backends = set()
    peak_discovery_dialogs = 0
    app.setQuitOnLastWindowClosed(False)
    timer = QTimer()
    timer.setInterval(25)

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def check(condition, description, **evidence):
        if not condition:
            raise AssertionError(description)
        report['checks'].append({'description': description, **evidence})
        save()

    def heartbeat():
        nonlocal peak_discovery_dialogs
        ticks.append(time.monotonic())
        dialogs = [widget for widget in app.topLevelWidgets() if isinstance(widget, DiscoveryDialog)
                   and not getattr(widget, '_closing', False)]
        peak_discovery_dialogs = max(peak_discovery_dialogs, len(dialogs))
        observed_dialogs.update(id(dialog) for dialog in dialogs)
        browser_backends.update(dialog.browser_backend for dialog in dialogs)

    timer.timeout.connect(heartbeat)

    def wait(predicate, seconds=30, reason='批量验收等待超时'):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(0.005)
        raise TimeoutError(reason)

    def background(function, seconds=60):
        result, done = {}, threading.Event()
        def execute():
            try:
                result['value'] = function()
            except Exception as error:
                result['error'] = error
            finally:
                done.set()
        threading.Thread(target=execute, name='batch-acceptance-background', daemon=True).start()
        wait(done.is_set, seconds, '本地媒体或历史后台操作超时')
        if 'error' in result:
            raise result['error']
        return result.get('value')

    def history():
        def read():
            store = HistoryStore(directory / 'history.sqlite3')
            try:
                return store.query(view='history', limit=500)
            finally:
                store.close()
        return background(read, 20)

    def opened_window():
        nonlocal window
        window = MainWindow(ConfigStore(directory / 'config.json'))
        window.showMinimized()
        window.batch_controller.message.connect(messages.append)
        window.library_service.message.connect(messages.append)
        wait(lambda: window.detector is None and window.yt_info.found and window.ff_info.found
             and window.probe_info.found and window.batch_controller.ready, 45,
             '工具或批次持久化没有完成启动')
        check(app.platformName() == 'windows', '使用原生 Windows 验收环境')
        window.input_tabs.setCurrentWidget(window.batch_panel)
        window.concurrent_fragments.setValue(1)
        window.max_concurrent_requests.setValue(2)
        window.batch_driver.finished.connect(discovered)
        return window.batch_controller

    def discovered(item_id, payload, error):
        item = window.batch_controller.items.get(item_id, {})
        page_key = urlsplit(item.get('url', '')).path.rsplit('/', 1)[-1]
        candidates = [candidate for group in payload.get('groups', []) for candidate in group.candidates]
        paths = sorted({urlsplit(candidate.url).path for candidate in candidates})
        valid = all(path.startswith('/media/' + page_key + '/') for path in paths)
        proof = {'item_id': item_id, 'page_key': page_key, 'candidate_count': len(candidates),
                 'candidate_paths': paths, 'isolated': valid, 'error': bool(error)}
        discovery.append(proof)
        report['discovery_pages'] = discovery
        save()

    def members(batch_id):
        return sorted((item for item in window.batch_controller.items.values() if item['batch_id'] == batch_id),
                      key=lambda item: item['ordinal'])

    def job_for(item):
        key = item['id']
        return next((job for job in window.queue.jobs if
                     job.request.media_metadata.get('batch_item_id') == key), None)

    def confirm_candidate(item):
        controller, panel = window.batch_controller, window.batch_panel
        groups = controller.candidate_groups(item['id'])
        page_key = urlsplit(item['url']).path.rsplit('/', 1)[-1]
        expected = '/media/' + page_key + '/'
        from core.media_quality import quality_candidates
        candidates = [candidate for group in groups for candidate in quality_candidates(group)
                      if urlsplit(candidate.url).path.startswith(expected)]
        from core.media_groups import available_candidate
        candidate = next((candidate for candidate in candidates if available_candidate(candidate)), None)
        check(candidate is not None, '待确认任务保留本页完整且有下载资格的真实候选', page_key=page_key)
        seen, end = {}, time.monotonic() + 12
        poll = QTimer()
        poll.setInterval(15)

        def answer():
            dialogs = [widget for widget in app.topLevelWidgets() if isinstance(widget, QDialog) and widget.isVisible()]
            for dialog in dialogs:
                if isinstance(dialog, QMessageBox) and dialog.windowTitle() == '确认当前正片':
                    dialog.setWindowState(dialog.windowState() | Qt.WindowState.WindowMinimized)
                    button = dialog.button(QMessageBox.StandardButton.Yes)
                    if button:
                        seen['confidence_confirmation'] = True
                        button.click()
                        return
                if isinstance(dialog, BatchCandidateDialog) and not seen.get('selected'):
                    dialog.setWindowState(dialog.windowState() | Qt.WindowState.WindowMinimized)
                    index = dialog.choices.findData(candidate.id)
                    if index >= 0:
                        seen['selected'] = True
                        dialog.choices.setCurrentIndex(index)
                        # OK 可同步进入第二个模态确认；独立 singleShot 才能在嵌套循环中应答。
                        QTimer.singleShot(25, answer)
                        dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
                        return
            if time.monotonic() >= end:
                seen['timeout'] = True
                for dialog in dialogs:
                    if isinstance(dialog, (BatchCandidateDialog, QMessageBox)):
                        dialog.reject()
                poll.stop()

        poll.timeout.connect(answer)
        poll.start()
        panel.set_snapshot(*controller.snapshot())
        check(panel.select_item(item['id']), '真实批次面板可定位待选择任务')
        panel.buttons['select'].click()
        poll.stop()
        check(seen.get('selected') and not seen.get('timeout'), '通过真实候选对话框确认本页媒体', page_key=page_key)
        wait(lambda: job_for(item) is not None or item['state'] == 'failed', 15)
        report['manual_confirmations'].append({'page_key': page_key,
            'candidate_dialog': True, 'confidence_confirmation': bool(seen.get('confidence_confirmation'))})

    def terminal(batch_id, seconds=180, pause_hls=False):
        paused, end = False, time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            for item in members(batch_id):
                if item['state'] == 'waiting_selection':
                    confirm_candidate(item)
                job = job_for(item)
                key = urlsplit(item.get('url', '')).path.rsplit('/', 1)[-1]
                if (pause_hls and not paused and key == 'hls' and job is not None and job.worker is not None
                        and job.request.resume_identity is not None
                        and (job.progress.get('fragment_index') or 0) >= 1
                        and server.segment_active.get('hls', 0)):
                    previous = (job.id, job.output_path, job.request.temporary_directory)
                    window.batch_controller.item_action('pause', item['id'])
                    wait(lambda: job.worker is None and item['state'] == 'paused', 30, '单任务安全暂停未结束')
                    check(Path(job.request.temporary_directory).is_dir(), '暂停保留既有续传工作目录')
                    window.batch_controller.item_action('resume', item['id'])
                    check((job.id, job.output_path, job.request.temporary_directory) == previous,
                          '单任务继续保留任务身份、输出路径和续传目录')
                    paused = True
            if all(item['state'] in ('completed', 'failed', 'cancelled') and
                   (job_for(item) is None or job_for(item).worker is None) for item in members(batch_id)):
                return paused
            time.sleep(0.01)
        states = [{'ordinal': item['ordinal'], 'state': item['state'], 'error': item.get('error', '')}
                  for item in members(batch_id)]
        raise TimeoutError('批次没有正常结束：' + json.dumps(states, ensure_ascii=False))

    def successful(item):
        job = job_for(item)
        result = job.result or {} if job else {}
        check(job is not None and job.state == 'completed' and result.get('success'),
              '真实 DownloadWorker 完成批次任务', item_id=item['id'])
        output = Path(result['files'][0])
        validation = result.get('output_validation', [{}])[0]
        check(output.is_file() and output.stat().st_size > 0 and validation.get('duration', 0) > 0
              and validation.get('video_streams'), '真实输出由既有 ffprobe 验证时长及视频轨', filename=output.name)
        entry_key = snapshot_job(job)['task_key']
        wait(lambda: not window.thumbnails.busy, 30)
        state = {}
        def found():
            state['entry'] = next((entry for entry in history()['items'] if entry['task_key'] == entry_key), None)
            return bool(state['entry'])
        wait(found, 20, '成功媒体没有自动进入下载历史')
        entry = state['entry']
        check(entry['task_state'] == 'completed' and entry['validation_state'] == 'valid'
              and entry['path'] == str(output), '成功媒体自动进入媒体库及独立下载历史')
        proof = {'item_id': item['id'], 'queue_id': job.id, 'history_id': entry['id'],
            'file': str(output), 'sha256': _sha(output), 'size': output.stat().st_size,
            'duration': validation['duration'], 'protocol': job.request.download_protocol,
            'workspace': job.request.temporary_directory}
        report['actual_downloads'].append(proof)
        save()
        return proof

    def close_window():
        nonlocal window
        if window is None:
            return
        old = window
        old.batch_controller.shutdown()
        wait(lambda: old.batch_controller.is_closed and not old.queue.active_count and not old.thumbnails.busy,
             45, '批次或续传任务没有完成安全关闭')
        if old.detector is not None:
            old.detector.cancel()
        wait(lambda: old.detector is None, 10)
        old.close()
        wait(lambda: old.library_service.is_closed and not old.isVisible(), 25, '窗口或媒体历史没有关闭')
        window = None

    try:
        if stage not in ('first', 'restart'):
            raise ValueError('批量验收阶段仅支持 first 或 restart。')
        timer.start()
        if stage == 'first':
            if (directory / 'config.json').exists() or (directory / 'downloads').exists():
                raise ValueError('first 阶段需要新的验收目录，避免覆盖此前结果。')
            downloads = directory / 'downloads'
            downloads.mkdir()
            ConfigStore(directory / 'config.json').save({'output_directory': str(downloads)})
            controller = opened_window()
            import tests.hls_fixture as fixture_module
            fixture_module.ROOT = APP_DIR
            fixture = HlsFixture(directory / 'fixtures')
            background(fixture.generate, 90)
            server = _LocalBatchPages(fixture.directory)
            urls = [server.base + '/page/' + key for key in ('direct', 'no-media', 'hls', 'dash')]
            options = {'output_directory': str(downloads), 'quality': 'auto', 'naming': 'title',
                       'max_concurrent': 2, 'retry_limit': 0, 'discovery_seconds': 6}
            for name in ('Shared Video.mp4', 'Shared Video (1).mp4'):
                shutil.copy2(fixture.directory / 'direct.mp4', downloads / name)
            originals = {str(downloads / name): _sha(downloads / name)
                         for name in ('Shared Video.mp4', 'Shared Video (1).mp4')}
            dialog = BatchImportDialog(window, existing=controller.existing_urls(), defaults=options)
            dialog.showMinimized()
            job_count = len(window.queue.jobs)
            dialog.editor.setPlainText(urls[0] + '\n\n' + urls[0] + '\nnot-a-url\n' + urls[1])
            text_file = directory / 'import.txt'
            text_file.write_text('\ufeff' + '\n'.join(urls[2:]), encoding='utf-8')
            dialog.load_file(str(text_file))
            wait(lambda: not dialog._reading, 10)
            check(dialog._preview['valid'] == 4 and dialog._preview['duplicates'] == 1
                  and dialog._preview['invalid'] == 1, '多行与真实 TXT 导入正确统计有效、重复、无效及空行')
            check(len(window.queue.jobs) == job_count and not controller.items and not server.requests,
                  '粘贴和 TXT 预览期间没有创建下载任务或加载网页')
            dialog.grab().save(str(directory / 'import-preview.png'))
            dialog.confirm_button.click()
            check(dialog.result() == QDialog.DialogCode.Accepted and dialog.result_urls == urls,
                  '只有真实确认按钮才提交按顺序去重后的原网址')
            batch_id = window.submit_batch_urls(dialog.result_urls, dialog.options)
            dialog.deleteLater()
            controller.batch_action('pause', batch_id)
            wait(lambda: batch_id not in controller._pending_creates, 10)
            time_before = len(server.requests)
            end = time.monotonic() + 0.25
            while time.monotonic() < end:
                app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                time.sleep(0.005)
            check(controller.batches[batch_id]['paused'] and len(server.requests) == time_before
                  and all(item['state'] == 'paused' for item in members(batch_id)),
                  '暂停整个批次会保留各任务状态并停止后续页面发现')
            controller.batch_action('resume', batch_id)
            paused = terminal(batch_id, pause_hls=True)
            check(paused, '实际 HLS 传输中执行了单任务暂停及继续')
            results = {urlsplit(item['url']).path.rsplit('/', 1)[-1]: item for item in members(batch_id)}
            check(results['no-media']['state'] == 'failed' and bool(results['no-media']['error']),
                  '无媒体页面记录明确失败原因')
            complete = [successful(results[key]) for key in ('direct', 'hls', 'dash')]
            check({entry['protocol'] for entry in complete} == {'Direct', 'HLS', 'DASH'},
                  '同一批次经原有 Direct、HLS、DASH 管线真实下载，失败页面不阻断后续项')
            check(discovery and all(item['isolated'] for item in discovery) and peak_discovery_dialogs == 1,
                  '页面发现保持串行，候选仅属于各自页面的媒体资源目录',
                  peak_discovery_dialogs=peak_discovery_dialogs)
            check(browser_backends == {'webview2'}, '真实批次发现均使用现有 WebView2 浏览器')
            check(all(Path(path).is_file() and _sha(path) == digest for path, digest in originals.items())
                  and not any(item['file'] in originals for item in complete),
                  '真实同名正常文件的字节保持原样，新下载沿用现有续号策略')
            other = BatchImportDialog(window)
            first = 'https://www.douyin.com/user/self?modal_id=7526879737634950451&showTab=record'
            second = 'https://www.douyin.com/user/self?modal_id=7657489271422549747&showTab=record'
            other.editor.setPlainText(first + '\n' + first + '\n' + second)
            other.refresh_preview()
            check(other._preview['urls'] == [first, second], '不同作品 ID 与完整查询参数保留，完全相同网址去重')
            other.reject()
            other.deleteLater()
            recovery_urls = [server.base + '/page/' + key for key in ('restart-complete', 'restart-hls', 'cancel')]
            recovery_batch = window.submit_batch_urls(recovery_urls, options)
            pending = members(recovery_batch)
            controller.item_action('cancel', pending[2]['id'])
            resume_item = pending[1]
            end = time.monotonic() + 100
            while time.monotonic() < end:
                app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                for item in pending:
                    if item['state'] == 'waiting_selection':
                        confirm_candidate(item)
                job = job_for(resume_item)
                if (job is not None and job.worker is not None and job.request.resume_identity is not None
                        and (job.progress.get('fragment_index') or 0) >= 1
                        and server.segment_active.get('restart-hls', 0)):
                    controller.item_action('pause', resume_item['id'])
                    wait(lambda: job.worker is None and resume_item['state'] == 'paused', 35)
                    break
                time.sleep(0.01)
            else:
                raise TimeoutError('恢复样本没有进入真实 HLS 传输')
            check(pending[0]['state'] == 'completed' and pending[2]['state'] == 'cancelled',
                  '待重启批次同时保存已完成、已暂停及已取消任务')
            complete.append(successful(pending[0]))
            controller.batch_action('pause', recovery_batch)
            workspace = Path(job.request.temporary_directory)
            workspace_files = {str(path.relative_to(workspace)): {'size': path.stat().st_size, 'sha256': _sha(path)}
                               for path in workspace.rglob('*') if path.is_file()}
            segment_hashes = {_sha(path) for path in fixture.directory.glob('part*.ts')}
            cached_segments = {name: proof for name, proof in workspace_files.items()
                               if proof['sha256'] in segment_hashes}
            check(bool(cached_segments), '中途退出前已缓存实际完成的 HLS 分片，不只保存任务信息',
                  cached_segment_files=list(cached_segments))
            wait(lambda: not window.thumbnails.busy, 30)
            entries = history()
            check(any(item['task_state'] == 'failed' for item in entries['items'])
                  and any(item['task_state'] == 'cancelled' for item in entries['items']),
                  '发现失败和用户取消均记录到独立下载历史')
            expected = {'port': server.server.server_port, 'recovery_batch': recovery_batch,
                'resume_item_id': resume_item['id'], 'cancel_item_id': pending[2]['id'],
                'completed': complete, 'workspace': str(workspace), 'workspace_files': workspace_files,
                'cached_segments': cached_segments,
                'history_total': entries['total'], 'originals': originals,
                'all_item_ids': list(controller.items), 'all_batch_ids': list(controller.batches)}
            window.grab().save(str(directory / 'batch-first.png'))
            close_window()
            (directory / 'restart-state.json').write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding='utf-8')
            report['restart_prepared'] = True
        else:
            expected = json.loads((directory / 'restart-state.json').read_text(encoding='utf-8'))
            server = _LocalBatchPages(directory / 'fixtures', expected['port'])
            controller = opened_window()
            check(set(controller.items) == set(expected['all_item_ids'])
                  and set(controller.batches) == set(expected['all_batch_ids']),
                  '新进程恢复相同批次和任务身份，没有重复创建任务')
            check(all(item['state'] in ('completed', 'failed', 'cancelled', 'paused', 'waiting_context')
                      for item in controller.items.values()) and not window.batch_driver.busy and not window.queue.active_count,
                  '新进程默认暂停未完成任务，不擅自开始网页分析或下载')
            check(not server.requests, '重启等待用户继续期间没有重复请求已完成媒体')
            for proof in expected['completed']:
                item = controller.items[proof['item_id']]
                check(item['state'] == 'completed' and _sha(proof['file']) == proof['sha256'],
                      '已完成任务状态和真实输出跨进程保持', item_id=item['id'])
            workspace = Path(expected['workspace'])
            check(all((workspace / name).is_file() and _sha(workspace / name) == proof['sha256']
                      for name, proof in expected['workspace_files'].items()),
                  '暂停任务的既有续传文件跨进程保留')
            resume_item = controller.items[expected['resume_item_id']]
            check(resume_item['state'] == 'paused' and job_for(resume_item) is not None,
                  '恢复批次正确关联原任务进度与暂停状态')
            check(controller.items[expected['cancel_item_id']]['state'] == 'cancelled', '取消任务重启后不重新下载')
            job = job_for(resume_item)
            before = (job.request.temporary_directory, job.output_path)
            controller.batch_action('resume', expected['recovery_batch'])
            check(resume_item['state'] == 'paused', '整批继续保留用户先前单独暂停的任务')
            controller.item_action('resume', resume_item['id'])
            terminal(expected['recovery_batch'], 110)
            proof = successful(resume_item)
            check((job.request.temporary_directory, job.output_path) == before, '手动继续使用原续传目录和输出路径')
            completed_keys = {urlsplit(controller.items[entry['item_id']]['url']).path.rsplit('/', 1)[-1]
                              for entry in expected['completed']}
            check(not any(any('/' + key + '/' in request['path'] or request['path'] == '/page/' + key
                               for key in completed_keys) for request in server.requests),
                  '继续批次没有重新发现或下载此前已经完成的任务')
            entries = history()
            check(entries['total'] == expected['history_total'] + 1,
                  '续传完成自动新增一条媒体历史，没有重复历史记录')
            check(all(_sha(path) == digest for path, digest in expected['originals'].items()),
                  '重启和继续后原有同名文件仍保持不变')
            report['new_process_reopened_same_private_databases'] = True
            report['resumed_output'] = proof
            window.grab().save(str(directory / 'batch-restart.png'))
            close_window()
        report['passed'] = True
    except Exception as error:
        report['error_type'], report['error'] = type(error).__name__, str(error)
    finally:
        try:
            close_window()
        except Exception as error:
            report['cleanup_error'] = type(error).__name__ + ': ' + str(error)
            report['passed'] = False
        if server is not None:
            report['http_requests'] = server.requests
            report['localhost_media_bytes'] = sum(item.get('bytes_sent', 0) for item in server.requests
                                                 if item['path'].startswith('/media/'))
            server.close()
        timer.stop()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['ui_ticks'] = len(ticks)
        report['max_ui_tick_gap_seconds'] = round(max((second - first for first, second in zip(ticks, ticks[1:])), default=0), 4)
        report['peak_discovery_dialogs'] = peak_discovery_dialogs
        report['browser_backends'] = sorted(browser_backends)
        report['messages'] = messages[-20:]
        save()
        faulthandler.cancel_dump_traceback_later()
        traceback_file.close()
    return 0 if report['passed'] else 1
