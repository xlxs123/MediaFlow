"""Packaged EXE acceptance: private local data, real WebView2 and resumed bytes."""
from __future__ import annotations
from contextlib import closing
from copy import deepcopy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import traceback
from types import SimpleNamespace
import uuid

from PySide6.QtCore import QCoreApplication, QEvent, QTimer

from core.data_migration import MigrationEngine, _canonical, _hash
from core.downloader import DownloadRequest
from core.library_validation import revalidate_media
from core.media_history import HistoryStore
from core.task_store import TaskStore
from core.tool_manager import APP_DIR
from tests.track_fixture import TrackFixture
from ui.main_window import MainWindow
from ui.migration_dialog import MigrationDialog
from utils.build_info import runtime_identity
from utils.config import ConfigStore


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def run_migration_acceptance(app, args):
    directory = Path(args[args.index('--migration-check') + 1]).resolve()
    restart = '--migration-restart' in args
    rollback = '--migration-rollback' in args
    crash = '--migration-crash' in args
    directory.mkdir(parents=True, exist_ok=True)
    source, target = directory / 'old-data', directory / 'new-data'
    report_path = directory / ('rollback.json' if rollback else 'restart.json' if restart else 'result.json')
    report = {'passed': False, 'runtime_identity': runtime_identity(), 'checks': [], 'external_requests': False,
              'browser': {'configuration': 'not_checked', 'website_login': '未进行真实网站登录验证；仅使用本地合成状态。'}}
    app.setQuitOnLastWindowClosed(False)
    window = dialog = None
    fixture = server = None
    ticks, gaps = [], []
    phase = ['startup']
    timer = QTimer(); timer.setInterval(20)
    def beat():
        now = time.monotonic()
        if ticks:
            gaps.append({'seconds': now - ticks[-1], 'phase': phase[0]})
        ticks.append(now)
    timer.timeout.connect(beat)
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def check(condition, label, **evidence):
        report['checks'].append({'label': label, 'passed': bool(condition), **evidence})
        save()
        if not condition:
            raise AssertionError(label)
    def wait(predicate, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.005)
        raise TimeoutError('验收步骤超时：' + phase[0])
    def background(function):
        values = {}; event = threading.Event()
        def work():
            try:
                values['value'] = function()
            except Exception as error:
                values['error'] = error
            finally:
                event.set()
        threading.Thread(target=work, daemon=True).start()
        wait(event.is_set)
        if 'error' in values:
            raise values['error']
        return values.get('value')
    def close_window():
        nonlocal window
        if window:
            if window._discovery_dialog:
                window._discovery_dialog.reject()
                wait(lambda: window._discovery_dialog is None)
            host = window._webview2_profile.host if window._webview2_profile else None
            window.close()
            wait(lambda: not window.isVisible() and window.library_service.is_closed and window.batch_controller.is_closed)
            if host:
                wait(lambda: host._finished_once)
            window.deleteLater(); app.processEvents()
            window = None
    def browser_page(base, seed=False):
        page = window.open_discovery(page_url=base + 'page/direct', activate=False)
        from core.site_resolvers import ResolverRegistry
        page.worker.resolver_registry = ResolverRegistry([])
        page.showMinimized(); page.deep_find()
        wait(lambda: page._page_loaded)
        def js(code):
            result = []
            page.page.runJavaScript(code, result.append)
            wait(lambda: bool(result))
            return result[-1]
        if seed:
            check(js("localStorage.setItem('migration-fixture','saved');document.cookie='migration_fixture=saved; Max-Age=36000; Path=/';true") is True,
                  '真实 WebView2 建立本地合成 Cookie/LocalStorage')
        else:
            check(js("localStorage.getItem('migration-fixture')==='saved' && document.cookie.includes('migration_fixture=saved')") is True,
                  '迁移后的真实 WebView2 可打开且读取合成持久状态')
        return page
    timer.start()
    try:
        if crash:
            if not (directory / 'acceptance-owned.json').is_file() or not source.is_dir():
                raise ValueError('只允许已创建的隔离验收目录')
            engine = MigrationEngine(directory / 'crash-target')
            plan = engine.preview(source, {'settings': True, 'library': False, 'history': False})
            original = engine._write_change
            def terminate_after_write(*values):
                original(*values)
                os._exit(73)  # Dedicated acceptance child: uncommitted write-ahead state.
            engine._write_change = terminate_after_write
            engine.apply(plan, confirmed=True)
            raise AssertionError('专用崩溃进程未退出')
        if restart or rollback:
            baseline = json.loads((directory / 'baseline.json').read_text(encoding='utf-8'))
            check(hashes(source) == baseline['source'], '独立进程重启后旧数据 SHA-256 未变')
            engine = MigrationEngine(target)
            if restart:
                crash_engine = MigrationEngine(directory / 'crash-target')
                recovered = background(crash_engine.recover_interrupted)
                check(len(recovered) == 1 and recovered[0]['status'] == 'rolled_back',
                      '真实 EXE 突然退出后自动回滚 prepared 写入', recovered=recovered)
                with HistoryStore(target / 'history.sqlite3') as store:
                    check(store.query()['total'] == 3 and store.query(view='history')['total'] == 5,
                          '重启后迁移记录与恢复下载结果持续存在')
                before = hashes(source)
                repeated = background(lambda: engine.apply(engine.preview(source, {'settings': True, 'library': True,
                    'history': True, 'organization': True, 'batches': True, 'tasks': True, 'browser': False}), confirmed=True))
                check(repeated['status'] == 'completed' and repeated['counts']['history'] == 0 and repeated['counts']['tasks_recoverable'] == 0,
                      '重复迁移不重复导入任务和历史', counts=repeated['counts'])
                window = MainWindow(ConfigStore(target / 'config.json')); window.showMinimized()
                wait(lambda: window.batch_controller.ready and window.detector is None)
                check(len(window.queue.jobs) == 2 and sum(j.state == 'completed' for j in window.queue.jobs) == 1,
                      '重启恢复已完成任务且没有自动重新下载')
                check(len(window.batch_controller.batches) == 1, '重启后合集分组仍存在')
                close_window()
                check(hashes(source) == before, '重启与重复导入未改变来源')
            else:
                original = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
                with HistoryStore(target / 'history.sqlite3') as store:
                    store.record({'task_key': 'e' * 64, 'title': '迁移后新建的任务', 'task_state': 'failed', 'files': []})
                result = background(lambda: engine.rollback(original['migration']['id'], confirmed=True))
                check(result['status'] == 'rollback_partial', '回滚保留迁移后已下载和修改的数据', rollback=result['rollback'])
                with HistoryStore(target / 'history.sqlite3') as store:
                    titles = [r['title'] for r in store.query(view='history')['items']]
                    check('迁移后新建的任务' in titles and any('续传' in title for title in titles),
                          '回滚没有删除用户后建历史和恢复完成记录')
                outputs = json.loads((directory / 'result.json').read_text(encoding='utf-8'))['outputs']
                check(all(Path(p).is_file() for p in outputs), '回滚不删除实际媒体文件')
            report['passed'] = True
            save()
            return 0

        if source.exists() or target.exists():
            raise ValueError('首次验收必须使用全新的独立目录')
        (directory / 'acceptance-owned.json').write_text('{"scope":"MediaFlow migration private acceptance"}', encoding='utf-8')
        fixture = TrackFixture(directory / 'fixture', tools=APP_DIR / 'tools')
        phase[0] = 'fixture'
        background(fixture.generate)
        # A real Range server makes nonzero 206 evidence observable, without external downloads.
        requests = []
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(fixture.directory), **kw)
            def log_message(self, *_):
                pass
            def do_GET(self):
                if self.path == '/page/direct':
                    data = b'<!doctype html><title>Migration fixture</title><video muted controls src="/single.mp4"></video>'
                    self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
                    return
                if self.path == '/single.mp4':
                    data = (fixture.directory / 'single.mp4').read_bytes()
                    offset = 0
                    match = re.fullmatch(r'bytes=(\d+)-', self.headers.get('Range',''))
                    if match:
                        offset = int(match[1])
                    code = 206 if match else 200
                    requests.append({'offset':offset,'status':code})
                    self.send_response(code); self.send_header('Content-Type','video/mp4'); self.send_header('Accept-Ranges','bytes')
                    self.send_header('ETag','"migration-local-media-v1"')
                    if code == 206:
                        self.send_header('Content-Range',f'bytes {offset}-{len(data)-1}/{len(data)}')
                    self.send_header('Content-Length',str(len(data)-offset)); self.end_headers()
                    try:
                        self.wfile.write(data[offset:])
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                super().do_GET()
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = 'http://127.0.0.1:' + str(server.server_port) + '/'
        source.mkdir()
        config = ConfigStore(source / 'config.json')
        config.save({'output_directory':str(directory / 'outputs'), 'organization': {'filename_template':'{title}_{site}.{ext}', 'classification':'none'}})
        window = MainWindow(config); window.showMinimized()
        wait(lambda: window.batch_controller.ready and window.detector is None)
        phase[0] = 'browser_seed'
        browser_page(base, seed=True)
        from core.migration_browser import inspect_profile
        busy = background(lambda: inspect_profile(source / 'BrowserProfile'))
        check(not busy['available'], '浏览器配置使用中被拒绝迁移')
        close_window()
        validation = background(lambda: revalidate_media(fixture.directory / 'single.mp4',
            ffmpeg_path=str(APP_DIR / 'tools/ffmpeg.exe'), ffprobe_path=str(APP_DIR / 'tools/ffprobe.exe')))
        check(validation.get('validation_state') == 'valid', '真实测试媒体完整 ffprobe/解码通过', validation=validation)
        snapshot = {'task_key':'a'*64, 'queue_id':4, 'title':'旧版视频', 'source_site':'bilibili.com',
            'source_url':'https://www.bilibili.com/video/BV1CjaQ6NE42', 'task_state':'completed',
            'finished_at':'2026-10-21T10:00:00+00:00', 'files':[{'path':str(fixture.directory/'single.mp4'),
                'validation':validation.get('validation',{})}]}
        with HistoryStore(source / 'history.sqlite3') as store:
            store.record(snapshot)
            store.record(dict(snapshot, task_key='b'*64, title='已移动的视频', files=[{'path':str(directory/'missing.mp4')}]))
            store.record(dict(snapshot, task_key='c'*64, title='失败任务', task_state='failed', error='本地样本失败', files=[]))
            store.record(dict(snapshot, task_key='d'*64, title='取消任务', task_state='cancelled', files=[]))
        with closing(sqlite3.connect(source / 'history.sqlite3')) as db, db:
            db.execute('DROP TABLE migration_visibility')  # Exact original 1.7 table set.
        (source/'data-manifest.json').write_text('{"application":"MediaFlow","version":"1.7.0"}',encoding='utf-8')
        from core.media_guard import check_media
        from core.resume_identity import capture_identity
        from core.recovery_workspace import RecoveryWorkspace
        output = directory / 'outputs'
        output.mkdir()
        request = DownloadRequest(str(APP_DIR/'tools/yt-dlp.exe'), base+'single.mp4', str(output),
             filename='续传结果', output_extension='mp4', output_path=str(output/'续传结果.mp4'),
             ffmpeg_path=str(APP_DIR/'tools/ffmpeg.exe'), ffprobe_path=str(APP_DIR/'tools/ffprobe.exe'), download_protocol='Direct')
        inspection = background(lambda: check_media(request.to_context(), expected_media_type='Direct'))
        request.resume_identity = capture_identity(inspection)
        ws = RecoveryWorkspace(request, request.resume_identity, 'Direct')
        ws.prepare()
        payload = (fixture.directory/'single.mp4').read_bytes()
        offset = max(1,len(payload)//2)
        (ws.temporary/'续传结果.mp4.part').write_bytes(payload[:offset])
        ws.checkpoint(final=True); ws.close()
        job = SimpleNamespace(id=9,request=request,name='续传结果',state='paused',progress={'downloaded_bytes':offset},
            created_at='2026-10-21T10:00:00+00:00',updated_at='2026-10-21T10:00:01+00:00',retry_count=0,result={},context_error='')
        task_store=TaskStore(source/'tasks.json')
        check(task_store.save([job]), '旧任务通过真实当前用户 DPAPI 保存')
        document=TaskStore._read_document(source/'tasks.json')[1]
        bad=deepcopy(document['tasks'][0]); bad['metadata'].update(id=10,name='上下文无法解密的任务')
        bad['request_dpapi']='unavailable-fixture-cipher'
        document['tasks'].append(bad); document.pop('checksum'); document['checksum']=_hash(document)
        TaskStore._atomic_write(source/'tasks.json', _canonical(document))
        from core.batch_store import BatchStore
        batch_id,item_id = str(uuid.uuid4()),str(uuid.uuid4())
        batch=BatchStore(source/'batches.sqlite3')
        batch.create_batch({'id':batch_id,'title':'升级合集','options':{'collection_name':'升级合集'}},
            [{'id':item_id,'batch_id':batch_id,'ordinal':0,'url':base+'page/direct','state':'downloading',
              'queue_key':hashlib.sha256(('workspace:'+Path(request.temporary_directory).name).encode()).hexdigest(),'job_id':9}])
        batch.close()
        baseline={'source':hashes(source),'workspace':hashes(Path(request.temporary_directory)), 'workspace_path':request.temporary_directory}
        (directory/'baseline.json').write_text(json.dumps(baseline,ensure_ascii=False,indent=2),encoding='utf-8')
        phase[0]='migration'
        dialog=MigrationDialog(target, target_profile=target/'BrowserProfile',source_paths=[source])
        dialog.showMinimized()
        wait(lambda: not dialog.worker_busy and bool(dialog.sources))
        check(not dialog.selections['browser'].isChecked(), '浏览器登录配置默认不选')
        for key in ('organization','batches','tasks','browser'):
            dialog.selections[key].setChecked(True)
        dialog.preview_button.click()
        wait(lambda: not dialog.worker_busy and dialog.plan is not None)
        check(dialog.plan['counts']['library']==2 and dialog.plan['counts']['history']==4
              and dialog.plan['counts']['tasks_recoverable']==1 and dialog.plan['counts']['tasks_incompatible']==1,
              '迁移预览真实记录数量', counts=dialog.plan['counts'])
        check(not dialog.apply_button.isEnabled(), '预览后仍需最终确认')
        dialog.confirm_check.setChecked(True); dialog.browser_confirm_check.setChecked(True)
        dialog.apply_button.click()
        wait(lambda: not dialog.worker_busy and dialog.last_report is not None,timeout=180)
        report['migration']=dialog.last_report
        check(dialog.last_report['status']=='completed', '真实 Qt 向导完成迁移事务', counts=dialog.last_report['counts'])
        dialog.grab().save(str(directory/'migration.png'))
        dialog.close(); wait(lambda:dialog.is_closed); dialog=None
        check(hashes(source)==baseline['source'], '迁移后旧数据全树 SHA-256 未改变')
        check(hashes(Path(request.temporary_directory))==baseline['workspace'], '旧断点进度 SHA-256 未改变')
        with HistoryStore(target/'history.sqlite3') as store:
            rows=store.query()['items']
            check(len(rows)==2 and any(r['file_state']=='missing' for r in rows), '缺失媒体仍保留且不显示为可用')
            check(store.query(view='history')['total']==4, '成功、失败、取消历史均继承')
        phase[0]='new_window'
        window=MainWindow(ConfigStore(target/'config.json'));window.showMinimized()
        wait(lambda:window.batch_controller.ready and window.detector is None)
        check(window.yt_info.found and window.ff_info.found and window.probe_info.found, '迁移后工具自动定位正常')
        check(len(window.queue.jobs)==2 and all(j.state in ('paused','waiting_context') for j in window.queue.jobs), '未完成任务暂停导入且未自动下载')
        check(len(window.batch_controller.batches)==1 and next(iter(window.batch_controller.items.values()))['queue_key']==
             hashlib.sha256(('workspace:'+Path(request.temporary_directory).name).encode()).hexdigest(), '合集与稳定任务身份关系保留')
        browser_page(base)
        report['browser'].update(configuration='真实 WebView2 打开成功', local_persistent_state=True)
        window._discovery_dialog.reject(); wait(lambda: window._discovery_dialog is None)
        phase[0]='resume'
        first=next(j for j in window.queue.jobs if not j.context_error)
        window.select_job(first.id)
        window.resume_selected()
        check(first.state in ('queued','running','completed'), '用户通过现有继续入口手动恢复已迁移任务')
        wait(lambda:first.state in ('completed','failed','waiting_context'),timeout=180)
        check(first.state=='completed', '迁移后的原有下载管线成功完成续传', state=first.state)
        check(any(r['status']==206 and r['offset']==offset for r in requests), '实际 HTTP 206 从原有已验证偏移续传', offset=offset)
        report['outputs']=list(first.result.get('files',[]))
        check(bool(report['outputs']) and hashlib.sha256(Path(report['outputs'][0]).read_bytes()).hexdigest()==hashlib.sha256(payload).hexdigest(),
              '续传输出字节与原媒体完全一致')
        output_validation=background(lambda: revalidate_media(report['outputs'][0], ffmpeg_path=str(APP_DIR/'tools/ffmpeg.exe'), ffprobe_path=str(APP_DIR/'tools/ffprobe.exe')))
        check(output_validation.get('validation_state')=='valid', '迁移后下载输出通过 ffprobe 与完整解码', validation=output_validation)
        wait(lambda:window.library_service._db.pending==0 if hasattr(window.library_service._db,'pending') else True)
        close_window()
        check(hashes(source)==baseline['source'] and hashes(Path(request.temporary_directory))==baseline['workspace'], '下载和验证完成后旧数据与旧进度仍保持原始哈希')
        report['passed']=True
    except Exception as error:
        report.update(error=str(error),traceback=traceback.format_exc())
    finally:
        if dialog:
            dialog.close()
            wait(lambda:dialog.is_closed)
        close_window()
        if server:
            server.shutdown();server.server_close()
        if fixture:
            fixture.close()
        timer.stop()
        report['ui_peak_gap_seconds']=max((g['seconds'] for g in gaps if g['phase'] in ('migration','resume')),default=0)
        report['ui_gaps']=sorted(gaps,key=lambda g:g['seconds'],reverse=True)[:8]
        save()
    return 0 if report['passed'] else 1
