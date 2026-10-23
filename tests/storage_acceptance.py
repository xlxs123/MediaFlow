"""正式 EXE 的真实本地扫描、原生回收站、SQLite 一致性和重启验收。"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import threading
import time
import traceback

from PySide6.QtCore import QCoreApplication, QEvent, QTimer

from core.library_validation import revalidate_media
from core.media_history import HistoryStore
from core.recovery_workspace import file_hash
from core.storage_cleanup import retain_lock
from core.tool_manager import APP_DIR
from tests.track_fixture import TrackFixture
from ui.main_window import MainWindow
from utils.build_info import runtime_identity
from utils.config import ConfigStore


def run_storage_acceptance(app, args):
    directory = Path(args[args.index('--storage-check') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    restart = '--storage-restart' in args
    target = directory / ('restart-result.json' if restart else 'result.json')
    report = {'passed': False, 'restart': restart, 'runtime_identity': runtime_identity(),
              'checks': [], 'external_downloads': False, 'native_recycle': False}
    window = None
    timer = QTimer(); timer.setInterval(20)
    ticks, gaps = [], []
    phase = ['startup']
    def beat():
        now = time.monotonic()
        if ticks:
            gaps.append({'seconds': now - ticks[-1], 'phase': phase[0]})
        ticks.append(now)
    timer.timeout.connect(beat)
    app.setQuitOnLastWindowClosed(False)
    def save():
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def check(value, label, **evidence):
        report['checks'].append({'label': label, 'passed': bool(value), **evidence})
        save()
        if not value:
            raise AssertionError(label + ': ' + repr(evidence))
    def wait(predicate, timeout=100):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents(); QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.004)
        raise TimeoutError('后台步骤超时：' + phase[0])
    def background(function):
        result = {}; done = threading.Event()
        def run():
            try:
                result['value'] = function()
            except Exception as error:
                result['error'] = error
            finally:
                done.set()
        threading.Thread(target=run, daemon=True).start()
        wait(done.is_set)
        if result.get('error'):
            raise result['error']
        return result.get('value')
    def entries():
        with HistoryStore(directory / 'history.sqlite3') as store:
            return store.storage_entries()
    def scan():
        old = window.storage_service.result
        window.storage_panel.scan_button.click()
        wait(lambda: window.storage_service.result is not old and not window.storage_service.busy)
        return window.storage_service.result
    try:
        config = ConfigStore(directory / 'config.json')
        if not restart:
            config.save({'output_directory': str(directory / 'downloads')})
        window = MainWindow(config); window.showMinimized()
        timer.start()
        wait(lambda: window.detector is None and window.batch_controller.ready
             and window.yt_info.found and window.ff_info.found and window.probe_info.found)
        check(all(Path(info.path).parent == APP_DIR / 'tools' for info in
                  (window.yt_info, window.ff_info, window.probe_info)), '正式程序按自身目录定位三个工具')
        panel, service = window.storage_panel, window.storage_service
        window.input_tabs.setCurrentWidget(panel)
        check(panel.tabs.tabText(0) == '重复检测' and '空间' in panel.tabs.tabText(1),
              '主窗口真实提供去重与空间两个页面')
        if restart:
            original = json.loads((directory / 'result.json').read_text('utf-8'))
            check(original['passed'], '上次进程原生验收完成')
            result = scan()
            check(result['complete'], '重启后可完整重新扫描')
            check(result['stats']['missing_files'] == 2, '重启后原缺失文件和已回收文件仍保留历史')
            check(result['cache_hits'] > 0, '重启后完整哈希缓存能安全恢复', hits=result['cache_hits'])
            keep = original['retained']
            check(background(lambda: file_hash(keep['path'])) == keep['sha256'], '重启后保留媒体字节正确')
            check(not Path(original['recycled']['path']).exists(), '重启不会重新创建或重复下载已回收文件')
            check(background(lambda: file_hash(original['recycled']['bin_path'])) == original['recycled']['sha256'],
                  '重启后系统回收站中的样本仍可读取且内容一致')
            check(not window.queue.jobs, '没有新增或重复恢复下载任务')
            report['native_recycle'] = True
        else:
            def prepare():
                fixture = TrackFixture(directory / 'fixture', APP_DIR / 'tools'); fixture.generate()
                media = directory / 'media'; media.mkdir()
                paths = [media / '保留正片.mp4', media / '不同名称.webm', media / '另一个标题.mp4',
                         media / 'a' / 'same.mp4', media / 'b' / 'same.mp4', media / '相似标题多音轨.mp4', media / 'English.m4a']
                for path in paths:
                    path.parent.mkdir(parents=True, exist_ok=True)
                for path in paths[:3]:
                    shutil.copy2(fixture.directory / 'single.mp4', path)
                paths[3].write_bytes(b'A' * 128); paths[4].write_bytes(b'B' * 128)
                shutil.copy2(fixture.directory / 'multi.mp4', paths[5])
                shutil.copy2(fixture.directory / 'en.m4a', paths[6])
                sidecar = media / '保留正片.en.srt'; shutil.copy2(fixture.directory / 'en.srt', sidecar)
                with HistoryStore(directory / 'history.sqlite3') as store:
                    for index, path in enumerate(paths + [media / '原本已丢失.mp4', paths[0]]):
                        validation = {'subtitle_files': [{'path': str(sidecar), 'language': 'en', 'format': 'srt'}]} if index == 0 else {}
                        store.record({'task_key': 'native-' + str(index), 'queue_id': index + 1,
                                      'task_state': 'completed', 'title': '独立标题 ' + str(index),
                                      'source_site': 'local.test', 'finished_at': '2026-10-21T12:00:00Z',
                                      'files': [{'path': str(path), 'validation': validation}]})
                extra = directory / 'extra'; extra.mkdir()
                for index in range(480):
                    (extra / ('local-' + str(index) + '.bin')).write_bytes(index.to_bytes(8, 'big') + b'x' * 120)
                return paths, sidecar, extra
            phase[0] = 'prepare-fixtures'
            paths, sidecar, extra = background(prepare)
            total = background(lambda: sum(path.stat().st_size for path in paths) + sidecar.stat().st_size)
            digest = background(lambda: file_hash(paths[0]))
            phase[0] = 'default-scan'
            result = scan()
            check(result['complete'], '默认媒体库扫描完整完成')
            check(result['stats']['total_files'] == 8 and result['stats']['total_bytes'] == total,
                  '真实媒体与字幕容量、同路径多历史去重准确', stats=result['stats'])
            check(result['stats']['missing_files'] == 1, '历史丢失文件不计当前占用')
            check(len(result['groups']) == 1 and len(result['groups'][0]['files']) == 3,
                  '不同扩展名和不同标题仅按完整字节分为三副本组')
            check(result['stats']['reclaimable_bytes'] == 2 * paths[0].stat().st_size,
                  '理论冗余容量只累计两份，保留一份')
            check(not panel.selected_cleanup_paths() and all(path.exists() for path in paths),
                  '扫描不默认勾选、不自动删除，同名同大小不同内容不误选')
            check(not panel.extra_dirs(), '默认没有额外目录，不扫描整个系统盘')
            check(panel.duplicates_tree.height() >= 180, '默认窗口下重复文件列表保持可读高度',
                  height=panel.duplicates_tree.height())
            window.grab().save(str(directory / 'duplicates.png'))
            panel.add_directory(str(extra))
            phase[0] = 'many-files-scan'
            result = scan()
            check(result['stats']['total_files'] == 488 and result['stats']['total_bytes'] == total + 480 * 128,
                  '明确添加额外范围后准确统计480个真实文件')
            check(panel.files_table.rowCount() <= 100, '大文件列表按页展示，避免一次创建所有控件')
            panel.tabs.setCurrentIndex(1); app.processEvents()
            window.grab().save(str(directory / 'space.png'))
            panel.tabs.setCurrentIndex(0)
            phase[0] = 'cleanup-preview'
            def preview():
                panel.last_preview = None
                panel.set_cleanup_paths([str(paths[1])]); panel.cleanup_button.click()
                wait(lambda: panel.last_preview is not None and not service.busy)
                return panel.confirmation_dialog
            dialog = preview()
            check(panel.last_preview['token'] and panel.last_preview['count'] == 1,
                  '后台清理预览列出真实文件完整路径与容量', preview=panel.last_preview)
            check(not dialog.confirm_check.isChecked() and not dialog.apply_button.isEnabled()
                  and not panel.confirm_cleanup(True), '未勾选明确确认不能执行清理')
            check(str(paths[1]) in dialog.paths_preview.toPlainText(), '确认页完整路径不省略，可核对和复制')
            check(paths[1].exists(), '预览与拒绝确认均保留原文件')
            window.grab().save(str(directory / 'preview-window.png'))
            dialog.grab().save(str(directory / 'cleanup-preview.png'))
            # 真实 Windows 共享锁：允许读取但不允许删除，迫使回收站操作失败。
            phase[0] = 'native-recycle-failure'
            with retain_lock(paths[1]):
                dialog.confirm_check.setChecked(True); dialog.apply_button.click()
                wait(lambda: panel.last_cleanup is not None)
                failed = panel.last_cleanup
                wait(lambda: not service.busy and service.result.get('complete'))
            check(failed['completed'] == 0 and failed['failed'] == 1 and paths[1].exists(),
                  '实际系统回收失败保留原文件，不转为永久删除', result=failed)
            check(next(row for row in background(entries) if row['path'] == str(paths[1]))['file_state'] != 'missing',
                  '回收失败不伪造媒体库删除成功')
            phase[0] = 'native-recycle-success'
            dialog = preview(); panel.last_cleanup = None
            dialog.confirm_check.setChecked(True); dialog.apply_button.click()
            wait(lambda: panel.last_cleanup is not None)
            applied = panel.last_cleanup
            wait(lambda: not service.busy and service.result.get('complete'))
            check(applied['completed'] == 1 and applied['failed'] == 0 and not paths[1].exists(),
                  '正式 EXE 经实际 Qt 确认将唯一选中副本移入系统回收站', result=applied)
            receipts = list((directory / 'history-storage-cleanup').glob('*.receipt.json'))
            check(len(receipts) == 1, '生成唯一的系统原生回收凭据')
            receipt = json.loads(receipts[0].read_text('utf-8'))
            check(receipt['recycled'] and background(lambda: file_hash(receipt['bin_path'])) == digest,
                  '回收站目标确实存在，完整 SHA256 与原媒体相同', bin_path=receipt['bin_path'])
            check(background(lambda: file_hash(paths[0])) == digest and paths[2].exists(),
                  '保留副本及未选文件不受影响')
            rows = background(entries)
            check(len(rows) == 9 and sum(row['file_state'] == 'missing' for row in rows) == 1,
                  '所有历史保留，仅已确认回收的路径更新为缺失')
            check(service.result['stats']['total_bytes'] == total + 480 * 128 - receipt['size']
                  and service.result['stats']['missing_files'] == 2,
                  '清理后重复组与真实容量自动重新扫描')
            phase[0] = 'retained-ffprobe-full-decode'
            verified = background(lambda: revalidate_media(paths[0], ffmpeg_path=str(APP_DIR / 'tools/ffmpeg.exe'),
                                                           ffprobe_path=str(APP_DIR / 'tools/ffprobe.exe')))
            check(verified['validation_state'] == 'valid', '清理后保留MP4的ffprobe及完整解码通过', validation=verified)
            report['native_recycle'] = True
            report['retained'] = {'path': str(paths[0]), 'sha256': digest}
            report['recycled'] = receipt
            # 明确取消真实后台扫描；不将未完成结果用于清理。
            phase[0] = 'cancel-scan'
            service.start_scan(panel.extra_dirs()); service.cancel()
            wait(lambda: not service.busy)
            check(service.result['cancelled'] and not service.result['complete'], '实际界面取消扫描后禁止清理')
            result = scan()
            check(result['complete'], '取消后仍可重新扫描')
            report['stats'] = result['stats']
        check(window.queue._storage_cleanup_guards == 0, '清理收尾释放任务租约')
        report['ui_peak_seconds'] = max((gap['seconds'] for gap in gaps), default=0)
        report['ui_gaps'] = sorted(gaps, key=lambda value: value['seconds'], reverse=True)[:8]
        check(report['ui_peak_seconds'] < 1.0, '真实后台扫描和清理期间主界面持续响应', peak=report['ui_peak_seconds'])
        report['passed'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        timer.stop()
        if window:
            try:
                if window.storage_panel.confirmation_dialog:
                    window.storage_panel.confirmation_dialog.close()
                window.close()
                wait(lambda: window.storage_service.is_closed and window.library_service.is_closed
                     and window.batch_controller.is_closed and not window._relocation_workers)
            except Exception:
                report['close_error'] = traceback.format_exc(); report['passed'] = False
        save()
    return 0 if report['passed'] else 1
