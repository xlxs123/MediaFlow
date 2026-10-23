"""通过真实 Chromium、下载队列、yt-dlp 和 FFmpeg 验收批量入口。"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QMessageBox

from core.site_resolvers import ResolverRegistry
from core.download_manager import quality_sources
from tests.batch_fixture import BatchFixture
from tests.resolver_fixture import ResolverFixture, LocalRouResolver
from ui.discovery_dialog import DiscoveryDialog
from ui.main_window import MainWindow
from utils.config import ConfigStore


def run_batch_smoke(app, args):
    directory = Path(args[args.index('--batch-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as hls
    import tests.integration_hls as integration
    hls.ROOT = integration.ROOT = APP_DIR
    fixture = BatchFixture(directory / 'media', APP_DIR / 'tools')
    resolver_fixture = None
    window = dialog = None
    checks, outputs, metrics = [], [], {}
    native = app.platformName() == 'windows'
    ticks = [0]
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    timer.start()
    app.setQuitOnLastWindowClosed(False)

    def pump(predicate=lambda: False, timeout=30, required=True):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.01)
        if required:
            raise AssertionError('批量浏览器/下载验收等待超时')

    def close_dialog():
        nonlocal dialog
        if dialog:
            dialog.reject()
            pump(lambda: window._discovery_dialog is None, 20)
            dialog = None

    def find(url=None, registry=None, select_all=False):
        nonlocal dialog
        close_dialog()
        dialog = DiscoveryDialog(window, url or fixture.base + '/page', resolver_registry=registry)
        window._discovery_dialog = dialog
        dialog.media_selected.connect(window.import_discovered_media)
        dialog.batch_requested.connect(window.download_discovered_batch)
        dialog.worker.candidates_changed.connect(window._auto_refresh_candidates)
        dialog.shutdown_complete.connect(lambda current=dialog: window._discovery_closed(current))
        dialog.showMinimized() if native else dialog.show()
        dialog.deep_find()
        expected_groups = 1 if url and url.endswith('/page/dash') else 3
        pump(lambda: dialog._page_loaded and dialog._groups and
             (any(c.metadata.get('resolver_id') for c in dialog._candidates) if registry else
              sum(g.selected_candidate is not None and not g.is_ad for g in dialog._groups) == expected_groups), 30)
        if select_all:
            dialog.select_all_button.click()
            assert sum(group.selected for group in dialog._groups) == expected_groups
        return dialog

    def download_click():
        before = len(window.queue.jobs)
        assert dialog.download_all_button.isEnabled()
        selected_groups = [group for group in dialog._groups if
                           dialog._group_checks.get(group.id, group.selected)
                           and group.selected_candidate and not group.is_ad]
        sources = [candidate for group in selected_groups for candidate in
                   (quality_sources(group) if dialog.all_qualities.isChecked() else [group.selected_candidate])]
        medium = any(candidate.requires_confirmation for candidate in sources)
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as confirmation:
            dialog.download_all_button.click()
            pump(lambda: len(window.queue.jobs) > before and not dialog._batch_pending, 40)
        if medium:
            confirmation.assert_called_once()
        else:
            confirmation.assert_not_called()
        return window.queue.jobs[before:]

    def wait_jobs(jobs):
        pump(lambda: all(j.worker is None and j.state in ('completed', 'failed', 'cancelled', 'paused', 'waiting_context')
                         for j in jobs), 120)

    def verify(job):
        assert job.state == 'completed' and job.result['success'], (job.state, job.result, list(job.logs)[-5:])
        path = Path(job.result['files'][0])
        assert path == Path(job.output_path), (path, job.output_path)
        integration.probe(path)
        outputs.append(str(path))
        return path

    try:
        fixture.generate()
        fixture.start()
        window = MainWindow(ConfigStore(directory / 'configs' / (str(time.time_ns()) + '.json')))
        window.showMinimized() if native else window.show()
        pump(lambda: window.detector is None and window.yt_info.found and window.ff_info.found, 45)
        window.url.setText('https://form.example/unchanged')
        original_input = window.url.text()
        output_dir = directory / 'downloads' / str(time.time_ns())
        output_dir.mkdir(parents=True)
        for filename in ('Video.mp4', 'Video (1).mp4'):
            (output_dir / filename).write_bytes(b'original-kept')
        window.output_directory.setText(str(output_dir))
        window.max_concurrent_requests.setValue(2)
        find()
        pump(lambda: any(c.resolution[1] == 720 and c.manifest_type == 'Media' for c in dialog._candidates), 20)
        pump(lambda: any(c.content_type == 'video/mp4' and c.duration and c.duration > 5
                         for c in dialog._candidates), 15)
        defaults = [g for g in dialog._groups if g.selected]
        # Multiple paused players may have no unique current player. The existing
        # discovery policy then requires an explicit selection instead of guessing.
        assert len(defaults) <= 1
        assert all('/direct/b.mp4' in g.selected_candidate.url for g in defaults)
        metrics['initial_selection'] = {'selected_groups': len(defaults),
            'selected_protocols': [g.selected_candidate.media_type for g in defaults],
            'multiple_paused_players': True}
        assert all(not g.selected for g in dialog._groups
                   if g.selected_candidate and g.selected_candidate.media_type == 'HLS')
        dialog.select_all_button.click()
        assert len([g for g in dialog._groups if g.selected]) == 3
        assert all(not g.is_ad for g in dialog._groups if g.selected)
        assert any(c.is_ad for c in dialog._candidates)
        assert not any('poster.png' in c.url for c in dialog._candidates)
        dialog.grab().save(str(directory / 'batch-discovery.png'))
        jobs = download_click()
        assert len(jobs) == 3 and window.url.text() == original_input
        assert [Path(j.output_path).name for j in jobs] == ['Video (2).mp4', 'Video (3).mp4', 'Video (4).mp4']
        assert window.queue.max_concurrent == 3 and all(j.request.concurrent_fragments == 4 for j in jobs)
        assert not dialog.download_all_button.isEnabled()
        checks.extend(['真实页面中分HLS不默选，用户全选并确认后3个独立视频入队且不回填URL',
                       '实际VP9/Opus MP4播放器读取时长尺寸并经原管线输出MP4', 'Master多清晰度默认只下载最高可用版本',
                       'VAST广告和缩略图不进入默认批量', '磁盘同名文件和批内同名在启动前续号占名'])
        main_job = next(j for j in jobs if '/hls/a/' in j.request.url)
        pump(lambda: main_job.worker is not None and main_job.request.resume_identity is not None and fixture.active > 0, 25)
        window.select_job(main_job.id)
        before_path, staging = main_job.output_path, main_job.request.temporary_directory
        window.pause_job_button.click()
        pump(lambda: main_job.state == 'paused' and main_job.worker is None, 25)
        wait_jobs(jobs)
        assert all(j.state == 'completed' for j in jobs if j is not main_job), [
            {'state': j.state, 'result': j.result, 'logs': list(j.logs)[-5:]}
            for j in jobs if j is not main_job and j.state != 'completed']
        window.select_job(main_job.id)
        window.resume_job_button.click()
        wait_jobs(jobs)
        assert main_job.output_path == before_path and main_job.request.temporary_directory == staging
        for job in jobs:
            verify(job)
        assert integration.decoded_hash(Path(next(j for j in jobs if '/hls/c/' in j.request.url).output_path)) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        assert fixture.peak <= 2 and window.queue.concurrency.peak_count == 2
        assert (output_dir / 'Video.mp4').read_bytes() == b'original-kept'
        observed = [r for r in fixture.requests if r['path'].startswith('/hls/c/') and r['path'].endswith(('.png', '.key'))]
        assert observed and all(r['ua'] and r['referer'] and r['range'] == 'bytes=0-' for r in observed)
        assert all(not r['origin'] and not r['cookie'] and not r['auth'] for r in observed)
        window.grab().save(str(directory / 'batch-queue.png'))
        metrics['global_http_limit'] = {'configured': 2, 'controller_peak': window.queue.concurrency.peak_count,
                                       'server_segment_peak': fixture.peak}
        checks.extend(['单任务暂停继续保留同一文件和续传身份其他视频继续', '共享HTTP上限实际限制多个视频合计请求',
                       '真实AES128 PNG分片Key继承Range且不伪造Origin凭据'])
        # 高级模式只勾选A；各实际编码尺寸分别生成输出，父Master不额外输出。
        fixture.long_titles = True
        find()
        pump(lambda: {180, 360, 720} <= {c.resolution[1] for c in dialog._candidates if c.downloadable}, 25)
        dialog.select_none_button.click()
        group_a = next(g for g in dialog._groups if any('/hls/a/' in c.url for c in g.candidates))
        row = next(row for row in range(dialog.table.rowCount())
                   if dialog.table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1) == group_a.id)
        dialog.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
        dialog.all_qualities.setChecked(True)
        quality_jobs = download_click()
        assert len(quality_jobs) == 3
        wait_jobs(quality_jobs)
        for job in quality_jobs:
            path = verify(job)
            info = subprocess.run([str(APP_DIR / 'tools' / 'ffprobe.exe'), '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=height', '-of', 'json', str(path)], capture_output=True, text=True, check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=15)
            assert json.loads(info.stdout)['streams'][0]['height'] == job.media_candidate.resolution[1]
            assert '[' + job.media_candidate.resolution_label + ']' in path.name
            assert len(path.name.encode('utf-16-le')) // 2 <= 180
        fixture.long_titles = False
        checks.append('勾选组与所有清晰度真实下载编码尺寸正确父Master不重复')
        checks.append('超长中文标题实际输出仍保留清晰度标签且文件名长度有界')
        # 完成候选检测之后才注入服务器故障，确保不是过滤掉失败候选来冒充隔离。
        find(select_all=True)
        def inject_failure(job_id):
            job = window.queue.get(job_id)
            if job and job.state == 'running' and '/direct/b.mp4' in job.request.url:
                fixture.fail_direct = True
        window.queue.changed.connect(inject_failure)
        failed_jobs = download_click()
        wait_jobs(failed_jobs)
        window.queue.changed.disconnect(inject_failure)
        assert sum(j.state == 'failed' for j in failed_jobs) == 1
        assert sum(j.state == 'completed' for j in failed_jobs) == 2
        fixture.fail_direct = False
        window.retry_failed_button.click()
        wait_jobs(failed_jobs)
        assert all(j.state == 'completed' for j in failed_jobs)
        for job in failed_jobs:
            verify(job)
        checks.append('一个批量任务真实HTTP500失败不终止其余任务且失败重试恢复')
        find(select_all=True)
        def inject_expiry(job_id):
            job = window.queue.get(job_id)
            if job and job.state == 'running' and '/hls/a/' in job.request.url:
                fixture.expired_hls = True
        window.queue.changed.connect(inject_expiry)
        expiry_jobs = download_click()
        wait_jobs(expiry_jobs)
        window.queue.changed.disconnect(inject_expiry)
        expired = next(j for j in expiry_jobs if '/hls/a/' in j.request.url)
        assert expired.state == 'waiting_context'
        assert all(j.state == 'completed' for j in expiry_jobs if j is not expired)
        fixture.expired_hls = False
        old = expired.request.to_context()
        url = old.url.replace('token=fixture', 'token=renewed')
        window.queue.retry_with_context(expired.id, replace(old, url=url, final_url=url, original_url=url), expired.request.media_metadata)
        window.queue.start()
        wait_jobs(expiry_jobs)
        for job in expiry_jobs:
            verify(job)
        checks.append('Signed403只等待当前视频更新地址其他继续且同任务恢复')
        find(fixture.base + '/page/dash', select_all=True)
        pump(lambda: any(c.metadata.get('representations') for c in dialog._candidates), 20)
        assert len(dialog._groups) == 1
        dialog.all_qualities.setChecked(True)
        dash_jobs = download_click()
        assert len(dash_jobs) == 2
        wait_jobs(dash_jobs)
        for job in dash_jobs:
            path = verify(job)
            info = subprocess.run([str(APP_DIR / 'tools' / 'ffprobe.exe'), '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=height', '-of', 'json', str(path)], capture_output=True, text=True, check=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=15)
            assert json.loads(info.stdout)['streams'][0]['height'] == job.media_candidate.resolution[1]
        checks.append('同一DASH MPD归一组且所有清晰度按真实Representation过滤下载')
        # 仅测试适配器映射到本地服务；生产域名规则保持原样。
        close_dialog()
        resolver_fixture = ResolverFixture(fixture.directory)
        resolver_fixture.start()
        resolver_fixture.expire = True
        resolver_fixture.token = 'old'
        window.concurrent_fragments.setValue(1)
        find(resolver_fixture.url('/v/expiry'), ResolverRegistry([LocalRouResolver()]))
        def issue_fresh(job_id):
            job = window.queue.get(job_id)
            if job and job.state == 'waiting_context' and job.batch_id:
                resolver_fixture.token = 'new'
        window.queue.changed.connect(issue_fresh)
        auto_jobs = download_click()
        pump(lambda: all(j.state == 'completed' and j.worker is None for j in auto_jobs), 120)
        window.queue.changed.disconnect(issue_fresh)
        assert len(auto_jobs) == 1 and auto_jobs[0].retry_count == 1
        path = verify(auto_jobs[0])
        assert integration.decoded_hash(path) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        fresh_segments = [r for r in resolver_fixture.requests if r['token'] == 'new' and '/part' in r['path']]
        assert fresh_segments and all(not r['path'].endswith(('part000.ts', 'part001.ts')) for r in fresh_segments)
        checks.append('批量过期自动重跑原页Resolver同一任务续传不重下载完成分片')
        metrics['auto_refresh'] = {'retries': auto_jobs[0].retry_count, 'redownloaded_finished_fragments': 0}
        report = {'passed': True, 'checks': checks, 'outputs': outputs, 'metrics': metrics,
                  'gui_ticks': ticks[0], 'qt_platform': app.platformName(),
                  'packaged': bool(getattr(sys, 'frozen', False)),
                  'source_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, 'frozen', False) else '',
                  'third_party_site_tested': False, 'yt_dlp': window.yt_info.version, 'ffmpeg': window.ff_info.version}
        code = 0
    except Exception as error:
        report = {'passed': False, 'checks': checks, 'outputs': outputs, 'metrics': metrics,
                  'error': str(error), 'traceback': traceback.format_exc(),
                  'candidates': [c.safe_diagnostic() for c in dialog._candidates] if dialog else []}
        code = 1
    finally:
        timer.stop()
        if window:
            window._auto_refresh_timer.stop()
            window._auto_refresh_ids.clear()
            window._auto_refresh_job_id = None
            window.queue.stop_all()
            pump(lambda: window.queue.active_count == 0, 30, required=False)
            close_dialog()
            if window.detector is not None:
                pump(lambda: window.detector is None, 40, required=False)
            window.close()
            pump(timeout=.1, required=False)
        fixture.close()
        if resolver_fixture:
            resolver_fixture.close()
    (directory / 'batch-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return code
