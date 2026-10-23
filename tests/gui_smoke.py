"""用真正 Qt 事件循环验收 GUI，支持源码和打包 EXE。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLineEdit
from ui.main_window import MainWindow
from utils.config import ConfigStore
from tests.hls_fixture import HlsFixture, DISGUISED_HEADERS, OBSERVED_HEADERS


def run_smoke(app, args):
    from ui.app_setup import configure_app
    configure_app(app)
    index = args.index('--smoke-test')
    directory = Path(args[index + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # HLS工具来自源码/EXE旁tools；临时HTTP服务器只用于我们生成的测试视频。
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as fixture_module
    fixture_module.ROOT = APP_DIR
    fixture = HlsFixture(directory / 'media')
    checks = []
    window = None
    try:
        fixture.generate()
        fixture.start()
        store = ConfigStore(directory / 'test-config.json')
        window = MainWindow(store)
        native = app.platformName() == 'windows'
        if native:
            window.showMinimized()
        else:
            window.show()
        deadline = time.monotonic() + 30
        while window.detector is not None or not window.yt_info.found:
            app.processEvents()
            time.sleep(0.01)
            if time.monotonic() >= deadline:
                raise RuntimeError(window.tool_status.text())
        assert window.yt_info.found and window.ff_info.found
        checks.append('GUI启动与工具检测')
        assert window.windowTitle() == 'MediaFlow'
        assert app.applicationName() == 'MediaFlow'
        assert window.max_concurrent_videos.value() == 3 and window.concurrent_fragments.value() == 4
        checks.append('MediaFlow名称与默认3视频4分片')
        for mode in ('cmd', 'bash'):
            if native:
                # 原生后台验收不替换用户正在使用的系统剪贴板。
                window.parse_text(fixture.curl(mode, '/disguised/index', DISGUISED_HEADERS))
            else:
                QApplication.clipboard().setText(fixture.curl(mode, '/disguised/index', DISGUISED_HEADERS))
                window.parse_clipboard_button.click()
            assert window.url.text() == fixture.base + '/disguised/index'
            assert window.origin.text() == 'https://local.fixture'
            assert '&x=2' in window.referer.text()
            checks.append(f'{mode.upper()}自动填充' if native else f'剪贴板{mode.upper()}自动填充')
        for row in range(window.custom_headers.rowCount()):
            name = window.custom_headers.cellWidget(row, 0).text()
            if name in ('cookie', 'authorization', 'x-api-key'):
                assert window.custom_headers.cellWidget(row, 1).echoMode() == QLineEdit.EchoMode.Password
        checks.append('敏感Header默认隐藏')
        for mode in ('cmd', 'bash'):
            assert window.parse_text(fixture.curl(mode, '/observed/index.m3u8', OBSERVED_HEADERS))
            assert window.collect_headers() == OBSERVED_HEADERS
            assert window.inherit_range.isChecked() and not window.inherit_browser_metadata.isChecked()
            assert window.origin.text() == ''
            checks.append(f'{mode.upper()}三头请求自动启用Range继承')
        window.url.setText(fixture.base + '/observed/index.m3u8?sign=fixture&t=123')
        assert not window.signature_hint.isHidden()
        checks.append('时效签名提醒')
        window.output_directory.setText(str(directory / 'downloads' / str(time.time_ns()) / '界面下载 & spaces'))
        window.filename.setText('GUI video01')
        window.start_button.click()
        assert window.start_button.isEnabled() and window.url.isEnabled()
        assert window.stop_button.isEnabled()
        checks.append('下载期间按钮状态')
        tick_count = [0]
        timer = QTimer()
        timer.timeout.connect(lambda: tick_count.__setitem__(0, tick_count[0] + 1))
        timer.start(50)
        deadline = time.monotonic() + 60
        while window.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        timer.stop()
        assert window.worker is None, 'GUI下载超时'
        assert window.last_result and window.last_result['success'], window.last_result
        assert tick_count[0] >= 5, '下载时GUI没有持续处理事件'
        assert window.start_button.isEnabled() and not window.stop_button.isEnabled()
        assert window.progress_bar.value() == 1000
        assert window.last_result['files'][0].endswith('.mp4')
        assert Path(window.last_result['files'][0]).is_file()
        checks.extend(['GUI真实AES128 PNG分片到MP4', '后台下载时界面响应', '完成后输出文件与按钮'])
        assert window._diagnostic['classification'] == 'HLS_AES128' and window._diagnostic['drm'] is False
        assert window._diagnostic['playlist_type'] == 'VOD'
        assert all(resource['role'] == 'Media Segment' for resource in window._diagnostic['resources'])
        hits = [request for request in fixture.requests if request['path'].startswith('/observed/')]
        assert any(request['path'].endswith('enc.key') for request in hits)
        assert any(request['path'].endswith('.png') for request in hits)
        assert all(request['observed_headers_match'] for request in hits)
        assert not any(request['has_origin'] or request['has_cookie'] or request['has_authorization']
                       or request['browser_metadata_present'] for request in hits)
        checks.append('三类资源继承Range且不伪造Origin或凭据')
        from ui.dialogs import DetectionDialog
        dialog = DetectionDialog(window._diagnostic, window)
        detail = dialog.editor.toPlainText()
        assert 'METHOD：AES-128' in detail and 'DRM：No' in detail and 'Key URI：enc.key' in detail
        assert 'HLS 播放类型：VOD' in detail and 'Media Segment：abc000.png' in detail
        assert 'Range 继承：开启' in detail and '请求 Range：bytes=0-' in detail
        assert 'Origin：--' in detail and 'HTTP：206' in detail
        if native:
            dialog.showMinimized()
        else:
            dialog.show()
        app.processEvents()
        dialog.grab().save(str(directory / 'detection-details.png'))
        dialog.close()
        checks.append('检测详情展示AES128非DRM及VOD/PNG分片语义')
        content = window.log_view.toPlainText()
        assert '[hlsnative]' in content
        for secret in ('fixture-private-cookie', 'fixture-private-auth', 'fixture-private-key'):
            assert secret not in content and secret not in store.path.read_text(encoding='utf-8')
        checks.append('日志及配置无敏感Header')
        original_output = window.last_result['files'][0]

        # 在同一个真实窗口验证多任务，分别导入会话头与仅三头请求。
        from tests.hls_fixture import FIXTURE_HEADERS
        import tests.integration_hls as integration_module
        integration_module.ROOT = fixture_module.ROOT
        reference = integration_module.decoded_hash(fixture.directory / 'direct.mp4')
        fixture.parallel_delay = 1.0
        window.pause_queue()
        batch_jobs = []
        contexts = [FIXTURE_HEADERS, OBSERVED_HEADERS,
                    {**OBSERVED_HEADERS, 'referer': 'https://another.fixture/watch', 'user-agent': 'Another Fixture Browser'},
                    {**FIXTURE_HEADERS, 'cookie': 'session=fixture-other-cookie'}]
        for number, headers in enumerate(contexts):
            name = f'gui-batch-{number}'
            fixture.parallel_headers[name] = headers.copy()
            assert window.parse_text(fixture.curl('cmd' if number % 2 == 0 else 'bash',
                                                 f'/parallel/{name}/index.m3u8', headers))
            window.filename.setText('GUI same')
            window.add_queue_button.click()
            batch_jobs.append(window.queue.jobs[-1])
        assert len({job.name.casefold() for job in batch_jobs}) == 4
        assert window.queue.active_count == 0 and all(job.state == 'queued' for job in batch_jobs)
        active_counts = []
        window.queue.changed.connect(lambda _: active_counts.append(window.queue.active_count))
        window.start_queue_button.click()
        assert window.queue.active_count == 3 and batch_jobs[3].state == 'queued'
        window.input_tabs.setCurrentIndex(1)
        window.select_job(batch_jobs[1].id)
        assert window.url.isEnabled() and window.start_button.isEnabled()
        deadline = time.monotonic() + 65
        while not fixture.parallel_stats.get('gui-batch-0', {}).get('active', 0):
            app.processEvents()
            time.sleep(0.01)
            assert time.monotonic() < deadline, '并发请求启动超时'
        window.select_job(batch_jobs[0].id)
        window.stop_button.click()
        while window.queue.active_count and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.queue.active_count == 0
        assert batch_jobs[0].state == 'cancelled'
        assert all(job.result['success'] for job in batch_jobs[1:]), [job.result for job in batch_jobs]
        assert max(active_counts) == 3
        for number, job in enumerate(batch_jobs[1:], 1):
            assert fixture.parallel_stats[f'gui-batch-{number}']['headers_ok']
            assert integration_module.decoded_hash(job.result['files'][0]) == reference
        window.select_job(batch_jobs[1].id)
        assert window._diagnostic['origin'] == '' and window._diagnostic['inherit_range']
        assert window.progress_bar.value() == 1000 and window.last_result == batch_jobs[1].result
        window.select_job(batch_jobs[0].id)
        assert window.retry_button.isEnabled()
        window.retry_button.click()
        deadline = time.monotonic() + 60
        while window.queue.active_count and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert window.queue.active_count == 0 and batch_jobs[0].result['success'], batch_jobs[0].result
        assert integration_module.decoded_hash(batch_jobs[0].result['files'][0]) == reference
        for job in batch_jobs:
            for secret in ('fixture-private-cookie', 'fixture-private-auth', 'fixture-private-key', 'fixture-other-cookie'):
                assert secret not in '\n'.join(job.logs) + json.dumps(job.diagnostic) + json.dumps(job.result)
        checks.extend(['GUI真实3视频4分片并行与等待补位', 'GUI停止单个任务其他视频完成与重试',
                       'GUI任务独立上下文进度日志和文件名'])
        app.processEvents()
        window.grab().save(str(directory / 'main-window.png'))
        window.grab().save(str(directory / 'queue-window.png'))
        (directory / 'download-log.txt').write_text(content, encoding='utf-8')
        window.input_tabs.setCurrentIndex(0)
        window.advanced_toggle.setChecked(True)
        app.processEvents()
        window.grab().save(str(directory / 'advanced-window.png'))
        report = {'passed': True, 'packaged': bool(getattr(sys, 'frozen', False)),
                  'qt_platform': app.platformName(),
                  'checks': checks, 'ui_ticks_during_download': tick_count[0],
                  'yt_dlp': window.yt_info.version, 'ffmpeg': window.ff_info.version,
                  'output_exists': True, 'output': original_output,
                  'queue_outputs': [job.result['files'][0] for job in batch_jobs],
                  'peak_video_workers': max(active_counts), 'default_concurrent_fragments': 4,
                  'source_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, 'frozen', False) else ''}
        (directory / 'gui-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        app.processEvents()
        return 0
    except Exception as error:
        report = {'passed': False, 'checks': checks, 'error': str(error),
                  'tool_errors': {'yt_dlp': window.yt_info.error, 'ffmpeg': window.ff_info.error} if window else {}}
        (directory / 'gui-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if window:
            window.queue.stop_all()
            deadline = time.monotonic() + 25
            while window.queue.active_count and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
        if window and window.detector:
            window.detector.wait(30000)
            app.processEvents()
        return 1
    finally:
        fixture.close()


if __name__ == '__main__':
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    app = QApplication([])
    raise SystemExit(run_smoke(app, ['--smoke-test', str(Path(__file__).resolve().parents[1] / 'verification' / 'gui')]))
