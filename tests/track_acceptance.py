"""正式 EXE 的 localhost 多轨验收；真实队列、ffprobe、FFmpeg、媒体库与跨进程恢复。"""
from __future__ import annotations

from array import array
from copy import deepcopy
import faulthandler
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from core.library_validation import revalidate_media
from core.media_groups import group_media
from core.media_quality import quality_candidates
from core.media_history import HistoryStore
from core.media_tracks import apply_selection, catalog, default_selection, ytdlp_catalog
from core.process_tree import NO_WINDOW
from core.recovery_workspace import file_hash
from core.tool_manager import APP_DIR
from tests.track_cases import discovered, request_for
from tests.track_fixture import TrackFixture
from ui.batch_discovery import BatchCandidateDialog
from ui.main_window import MainWindow
from ui.media_library import track_details_text
from utils.build_info import runtime_identity
from utils.config import ConfigStore


def run_track_acceptance(app, args):
    def option(flag, default=''):
        return args[args.index(flag) + 1] if flag in args else default
    directory = Path(option('--tracks-check')).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    restart = '--tracks-restart' in args
    result_file = directory / ('restart-result.json' if restart else 'result.json')
    report = {'passed': False, 'runtime_identity': runtime_identity(), 'restart': restart,
              'checks': [], 'outputs': [], 'saved_media': [], 'external_downloads': False}
    report['scope'] = 'browser_only' if '--tracks-batch-only' in args else 'full'
    window = fixture = None
    ticks = []
    phase = ['启动']
    gaps = []
    all_gaps = []
    started = time.monotonic()
    timer = QTimer()
    timer.setInterval(25)
    def heartbeat():
        now = time.monotonic()
        if ticks:
            all_gaps.append((now - ticks[-1], phase[0]))
        if ticks and now - ticks[-1] > .2:
            gaps.append({'gap': now - ticks[-1], 'phase': phase[0], 'at_seconds': now - started})
        ticks.append(now)
    timer.timeout.connect(heartbeat)
    timer.start()
    sample_stop = threading.Event()
    main_thread = threading.get_ident()
    samples = []
    def sample_slow_ui():
        while not sample_stop.wait(.05):
            if ticks and time.monotonic() - ticks[-1] > .15 and len(samples) < 100:
                frame = sys._current_frames().get(main_thread)
                if frame:
                    samples.append({'at_seconds': time.monotonic() - started, 'phase': phase[0],
                        'stack': [f'{Path(item.filename).name}:{item.lineno}:{item.name}'
                                  for item in traceback.extract_stack(frame, limit=14)]})
    if '--tracks-profile' in args:
        threading.Thread(target=sample_slow_ui, name='track-ui-sampler', daemon=True).start()
    app.setQuitOnLastWindowClosed(False)
    trace = (directory / ('restart-trace.txt' if restart else 'trace.txt')).open('w', encoding='utf-8')

    # CPython 3.14.0 gh-140815: the native watchdog can itself crash while
    # dumping freed frames. External owned-process deadlines remain enforced.

    def save():
        result_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def check(value, description, **evidence):
        if not value:
            raise AssertionError(description + ': ' + repr(evidence))
        report['checks'].append({'description': description, 'at_seconds': time.monotonic() - started, **evidence})
        save()
    def wait(predicate, seconds=90):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.005)
        raise TimeoutError('轨道验收后台步骤超时')
    def background(function, seconds=120):
        state, done = {}, threading.Event()
        def run():
            try:
                state['value'] = function()
            except Exception as error:
                state['error'] = error
            finally:
                done.set()
        threading.Thread(target=run, name='tracks-acceptance', daemon=True).start()
        wait(done.is_set, seconds)
        if 'error' in state:
            raise state['error']
        return state.get('value')
    def history():
        store = HistoryStore(directory / 'history.sqlite3')
        try:
            return store.query(view='history', limit=100)['items']
        finally:
            store.close()
    def finished(job):
        return job.worker is None and job.state in ('completed', 'failed', 'cancelled', 'waiting_context')
    def run_job(request, expect_success=True):
        phase[0] = '下载队列：' + request.filename
        window.queue.pause()
        job = window.queue.add(request)
        window.queue.start()
        wait(lambda: finished(job), 100)
        if expect_success:
            check(job.state == 'completed', '真实下载队列任务完成', job=job.id,
                  error=(job.result or {}).get('error', '')[-1500:])
        return job
    def validate_file(path):
        return revalidate_media(path, ffmpeg_path=str(APP_DIR / 'tools/ffmpeg.exe'),
                                ffprobe_path=str(APP_DIR / 'tools/ffprobe.exe'), timeout=120)
    def output_case(name, kind, preference=None, expected_audio=None, subtitle_count=0, candidate=None):
        phase[0] = '候选与选择：' + name
        request, _ = background(lambda: request_for(fixture, directory / 'downloads' / name, kind,
                                                    preference, candidate=candidate))
        job = run_job(request)
        phase[0] = '完整读取：' + name
        actual = background(lambda: validate_file(job.output_path))
        check(actual['validation_state'] == 'valid', '输出完整音视频读取和字幕独立检查通过', case=name,
              reason=actual['validation_reason'])
        summary = actual['validation']
        check(len(summary.get('subtitle_streams', [])) == subtitle_count, '实际字幕轨数量与选择一致', case=name)
        if expected_audio:
            check([s.get('language') for s in summary['audio_streams']] == expected_audio,
                  '实际音轨语言与选择一致', case=name, audio=summary['audio_streams'])
        record = {'case': name, 'path': job.output_path, 'sha256': background(lambda: file_hash(job.output_path)),
                  'validation': summary, 'download_validation': job.result.get('output_validation')}
        report['outputs'].append(record)
        save()
        return job
    def signal_frequency(path):
        run = subprocess.run([str(APP_DIR / 'tools/ffmpeg.exe'), '-v', 'error', '-i', str(path),
            '-map', '0:a:0', '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=30)
        if run.returncode:
            raise AssertionError('音轨波形读取失败')
        samples = array('h', run.stdout[:64000])
        scores = {}
        for frequency in (440, 660, 880):
            step = 2 * math.pi * frequency / 8000
            real = sum(sample * math.cos(step * index) for index, sample in enumerate(samples))
            imaginary = sum(sample * math.sin(step * index) for index, sample in enumerate(samples))
            scores[frequency] = real * real + imaginary * imaginary
        return max(scores, key=scores.get)
    def frame_hashes(path):
        run = subprocess.run([str(APP_DIR / 'tools/ffmpeg.exe'), '-v', 'error', '-i', str(path),
            '-map', '0:v:0', '-f', 'framemd5', 'pipe:1'], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=30)
        if run.returncode:
            raise AssertionError('视频逐帧读取失败')
        return [line.rsplit(b',', 1)[-1].strip().decode() for line in run.stdout.splitlines() if not line.startswith(b'#')]
    def batch_checks():
        # 使用成品的串行 WebView2/CDP 驱动分析两个真实 localhost 页面。
        controller = window.batch_controller
        phase[0] = 'WebView2 批次启动'
        batch_options = {
            'output_directory': str(directory / 'downloads/batch'), 'quality': 'auto', 'naming': 'index_title',
            'discovery_seconds': 5, 'retry_limit': 1, 'track_preferences': {'audio_language': 'fr',
                'subtitle_mode': 'none', 'container': 'mkv'}}
        if '--tracks-organization' in args:
            batch_options.update(collection_name='多轨合集', organization={
                'filename_template':'{title}_{resolution}.{ext}', 'classification':'type_site'})
        batch = controller.create([fixture.base + 'page/dash', fixture.base + 'page/hls', fixture.base + 'page/direct'], batch_options)
        items = [item for item in controller.items.values() if item['batch_id'] == batch]
        for item, language in zip(items, ('en', 'ja', 'original')):
            phase[0] = 'WebView2 分析及候选确认：' + language
            wait(lambda item=item: item['state'] in ('waiting_selection', 'failed'), 65)
            check(item['state'] == 'waiting_selection', '批次缺偏好音轨时等待人工选择并继续分析其他项', state=item['state'], error=item['error'])
            groups = controller.candidate_groups(item['id'])
            options = [candidate for group in groups for candidate in quality_candidates(group)
                       if candidate.downloadable and (len(catalog(candidate).get('audio', [])) == 2
                       or language == 'original' and bool(catalog(candidate).get('subtitles')))]
            check(bool(options), '批次页面保留独立轨道候选', item=item['ordinal'])
            candidate = options[0]
            seen = {}
            answer_timer = QTimer()
            answer_timer.setInterval(15)
            deadline = time.monotonic() + 12
            def answer():
                for widget in app.topLevelWidgets():
                    if isinstance(widget, QMessageBox) and widget.windowTitle() == '确认当前正片':
                        widget.setWindowState(widget.windowState() | Qt.WindowState.WindowMinimized)
                        seen['confidence'] = True
                        widget.button(QMessageBox.StandardButton.Yes).click()
                        return
                    if isinstance(widget, BatchCandidateDialog) and widget.isVisible() and not seen.get('selected'):
                        widget.setWindowState(widget.windowState() | Qt.WindowState.WindowMinimized)
                        widget.choices.setCurrentIndex(widget.choices.findData(candidate.id))
                        if language == 'original':
                            widget.track_selector.audio_combo.setCurrentIndex(0)
                            widget.track_selector.audio_combo.activated.emit(0)
                            for index in range(widget.track_selector.subtitle_list.count()):
                                row = widget.track_selector.subtitle_list.item(index)
                                if row.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                                    row.setCheckState(Qt.CheckState.Checked)
                                    break
                        else:
                            audio = next(track for track in catalog(candidate)['audio'] if
                                         track['language'] in ({'en', 'eng'} if language == 'en' else {'ja', 'jpn'}))
                            widget.track_selector.audio_combo.setCurrentIndex(widget.track_selector.audio_combo.findData(audio['id']))
                        seen['selected'] = True
                        QTimer.singleShot(25, answer)
                        widget.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
                        return
                if time.monotonic() > deadline:
                    for widget in app.topLevelWidgets():
                        if isinstance(widget, (BatchCandidateDialog, QMessageBox)):
                            widget.reject()
                    answer_timer.stop()
            answer_timer.timeout.connect(answer)
            answer_timer.start()
            window.on_batch_item_action('select', item['id'])
            answer_timer.stop()
            check(seen.get('selected') and (not candidate.requires_confirmation or seen.get('confidence')),
                  '批次真实选择窗口保留原可信度确认', item=item['ordinal'], language=language)
        wait(lambda: all(item['state'] in ('completed', 'failed') for item in items), 100)
        phase[0] = '批次输出完整读取'
        check(all(item['state'] == 'completed' for item in items), '真实批次音轨与 HTML 字幕配置均下载完成', states=[item['state'] for item in items])
        if '--tracks-organization' in args:
            collection = Path(controller.batches[batch]['options']['collection_directory'])
            check(all(Path(item['output_path']).is_relative_to(collection) for item in items),
                  'WebView2串行发现的三个完成任务均提交到独立合集分类目录', collection=str(collection))
        for item, frequency in zip(items, (440, 660, 440)):
            check(background(lambda item=item: signal_frequency(item['output_path'])) == frequency,
                  '批次实际输出波形对应各自音轨', item=item['ordinal'], hz=frequency)
            actual = background(lambda item=item: validate_file(item['output_path']))
            check(actual['validation_state'] == 'valid', '批次多轨输出通过完整读取', item=item['ordinal'])
            report['outputs'].append({'case': 'batch-' + str(item['ordinal']), 'path': item['output_path'],
                'sha256': background(lambda item=item: file_hash(item['output_path'])), 'validation': actual['validation']})
        html_job = controller._job(items[-1])
        html_subtitles = html_job.result['output_validation'][0].get('subtitle_files', [])
        check(len(html_subtitles) == 1 and html_subtitles[0]['language'] == 'en' and Path(html_subtitles[0]['path']).is_file(),
              'WebView2 当前播放器声明的 HTML 字幕被真实获取并独立提交')
    try:
        check(app.platformName() == ('windows' if '--native-smoke' in args else app.platformName()), 'Qt 验收环境', platform=app.platformName())
        config = ConfigStore(directory / 'config.json')
        if not restart:
            config.save({'output_directory': str(directory / 'downloads'), 'max_concurrent_videos': 2})
        window = MainWindow(config)
        window.showMinimized()
        wait(lambda: window.detector is None and window.batch_controller.ready and window.yt_info.found
             and window.ff_info.found and window.probe_info.found, 45)
        check(True, '实际 EXE 自动定位三个工具', tools=[window.yt_info.version, window.ff_info.version, window.probe_info.version])
        if restart:
            first = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
            check(bool(first.get('passed')), '上一进程完整验收通过')
            before = len(window.queue.jobs)
            wait(lambda: window.library_service.validator is None, 10)
            for output in first['outputs']:
                check(background(lambda output=output: file_hash(output['path'])) == output['sha256'],
                      '重启后输出文件保持不变', case=output['case'])
            entries = background(history)
            completed_paths = {entry['path'] for entry in entries if entry['task_state'] == 'completed'}
            check(all(v['path'] in completed_paths for v in first['outputs']), '重启后媒体库保留全部输出')
            check(all(job.state == 'completed' for job in window.queue.jobs), '完成任务恢复后不重新排队')
            wait(lambda: window.queue.active_count == 0, 5)
            check(len(window.queue.jobs) == before, '重启不重复创建下载任务', count=before)
            report['passed'] = True
            save()
            return 0
        fixture = TrackFixture(directory / 'fixture', APP_DIR / 'tools')
        background(fixture.generate, 90)
        fixture.start()
        if '--tracks-batch-only' in args:
            batch_checks()
            report['heartbeat_gaps'] = gaps
            report['ui_samples'] = samples
            report['passed'] = True
            save()
            return 0
        direct = output_case('direct-multi', 'Direct', expected_audio=['eng', 'jpn'])
        output_case('single', 'Single', expected_audio=['eng'])
        dash, service = background(lambda: discovered(fixture, 'DASH'))
        values = catalog(dash)
        check(len(values['audio']) == 2 and len(values['subtitles']) == 2, '真实网络发现保留 DASH 全部音轨和字幕目录')
        groups = group_media(service.ranked(include_low=True))
        dialog = BatchCandidateDialog(groups, window, track_preferences={
            'audio_language': 'ja', 'subtitle_mode': 'mux', 'subtitle_languages': ['en'], 'container': 'mp4'})
        dialog.showMinimized()
        wait(lambda: dialog.track_selector.audio_combo.count() >= 3, 5)
        chosen = dialog.track_selector.selection()
        check(chosen['audio_id'] == next(v['id'] for v in values['audio'] if v['language'] == 'jpn'),
              '正式候选窗口选择实际日语音轨，保留低可信度确认规则', requires_confirmation=dash.requires_confirmation)
        dialog.grab().save(str(directory / 'track-selection.png'))
        # 选择控件不改变候选和评分；真实下载仍重新检查媒体完整性。
        check(dash.confidence == 30 and dash.requires_confirmation, '新增轨道控件没有授予额外可信度')
        selected = apply_selection(dash, chosen)
        dialog.reject()
        dialog.deleteLater()
        japanese = output_case('dash-ja-mp4', 'DASH', {'audio_language': 'ja', 'subtitle_mode': 'mux',
            'subtitle_languages': ['en'], 'container': 'mp4'}, expected_audio=['jpn'], subtitle_count=1)
        english = output_case('dash-en-mkv', 'DASH', {'audio_language': 'en', 'subtitle_mode': 'mux',
            'subtitle_languages': ['en', 'ja'], 'container': 'mkv'}, expected_audio=['eng'], subtitle_count=2)
        hls = output_case('hls-ja-sidecar', 'HLS', {'audio_language': 'ja', 'subtitle_mode': 'sidecar',
            'subtitle_languages': ['zh-Hans']}, expected_audio=['jpn'])
        sidecars = hls.result['output_validation'][0].get('subtitle_files', [])
        check(len(sidecars) == 1 and Path(sidecars[0]['path']).is_file(), '实际独立字幕路径与媒体分离', files=sidecars)
        check(background(lambda: signal_frequency(japanese.output_path)) == 660 and
              background(lambda: signal_frequency(english.output_path)) == 440 and
              background(lambda: signal_frequency(hls.output_path)) == 660,
              '实际音频波形证明选择生效，而非仅修改语言标签', english_hz=440, japanese_hz=660)
        source_frames = background(lambda: frame_hashes(fixture.directory / 'video.mp4'))
        check(source_frames == background(lambda: frame_hashes(japanese.output_path)) ==
              background(lambda: frame_hashes(english.output_path)), '字幕封装前后视频逐帧像素哈希一致', frames=len(source_frames))
        try:
            request_for(fixture, directory / 'downloads/incompatible', 'DASH', {'audio_language': 'ja', 'container': 'webm'}, candidate=dash)
        except ValueError as error:
            check('WebM' in str(error), '开始前阻止不兼容封装，未自动转码', reason=str(error))
        else:
            raise AssertionError('不兼容的 WebM 未被阻止')
        # 由真实 yt-dlp 格式提取得到格式 ID，再走同一既有下载器 -f 入口。
        def extract_formats():
            process = subprocess.run([str(APP_DIR / 'tools/yt-dlp.exe'), '--ignore-config', '--no-plugin-dirs',
                '--no-playlist', '--force-generic-extractor', '--skip-download', '--dump-single-json', '--no-warnings',
                '--proxy', '',
                fixture.base + 'dash/master.mpd'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=NO_WINDOW, timeout=40)
            if process.returncode:
                raise AssertionError('yt-dlp 元数据提取失败：' + process.stderr.decode('utf-8', errors='replace')[-1200:])
            return json.loads(process.stdout)
        info = background(extract_formats, 45)
        yt_candidate = deepcopy(dash)
        yt_candidate.metadata['track_catalog'] = ytdlp_catalog(info)
        yt = output_case('yt-format-ja', 'DASH', {'audio_language': 'ja'}, expected_audio=['jpn'], candidate=yt_candidate)
        check('+' in yt.request.format_selector, '使用真实 yt-dlp 格式标识选择', selector=yt.request.format_selector)
        failed_request, _ = background(lambda: request_for(fixture, directory / 'downloads/recovery', 'DASH',
            {'audio_language': 'ja', 'subtitle_mode': 'sidecar', 'subtitle_languages': ['en']}))
        fixture.failures['/en.vtt'] = 1
        failed = run_job(failed_request, False)
        check(failed.state == 'failed' and not Path(failed.output_path).exists(), '字幕 503 不提前提交媒体', error_code=failed.result.get('error_code'))
        staging = Path(failed.request.temporary_directory) / 'staging'
        check(bool(list(staging.glob('*.mp4'))), '字幕失败保留已下载媒体')
        request_offset = len(fixture.requests)
        window.queue.retry(failed.id)
        window.queue.start()
        wait(lambda: finished(failed), 90)
        check(failed.state == 'completed', '同一任务重试后字幕和媒体成功提交')
        new_requests = fixture.requests[request_offset:]
        check(not any(item['path'].endswith('.m4s') for item in new_requests), '字幕恢复未重复请求音视频分片')
        actual = background(lambda: validate_file(failed.output_path))
        check(actual['validation_state'] == 'valid', '字幕恢复输出通过完整读取')
        report['outputs'].append({'case': 'subtitle-recovery', 'path': failed.output_path,
            'sha256': background(lambda: file_hash(failed.output_path)), 'validation': actual['validation']})
        # 媒体库从真实队列的完成回调写入，不插入伪造完成记录。
        wait(lambda: window.queue.active_count == 0, 10)
        entries = background(history)
        check(all(value['path'] in {entry['path'] for entry in entries} for value in report['outputs']),
              '真实完成回调自动更新媒体库和下载历史', records=len(entries))
        library_item = next(entry for entry in entries if entry['path'] == japanese.output_path)
        details = track_details_text(library_item.get('validation', {}))
        check('日语' in details and '英语' in details, '媒体库实际轨道详情显示可读语言')
        window.library_service.validate(library_item['id'], ffmpeg_path=str(APP_DIR / 'tools/ffmpeg.exe'),
                                        ffprobe_path=str(APP_DIR / 'tools/ffprobe.exe'))
        wait(lambda: window.library_service.validator is not None, 10)
        wait(lambda: window.library_service.validator is None, 60)
        until = time.monotonic() + 10
        while time.monotonic() < until:
            entries = background(history)
            library_item = next(entry for entry in entries if entry['path'] == japanese.output_path)
            if library_item['validation'].get('subtitle_check', {}).get('status') == 'passed':
                break
            next_check = time.monotonic() + .1
            wait(lambda: time.monotonic() >= next_check, .3)
        check(library_item['validation'].get('subtitle_check', {}).get('status') == 'passed', '历史媒体手动验证补充实际字幕读取状态')
        # 两条候选分别使用偏好，确保选择没有跨任务串用。
        check(japanese.request.media_metadata['track_selection']['audio_id'] != english.request.media_metadata['track_selection']['audio_id'],
              '多个队列任务的音轨选择互不污染')
        batch_checks()
        saved_manifest = option('--tracks-saved-manifest')
        if saved_manifest:
            for sample in json.loads(Path(saved_manifest).read_text(encoding='utf-8')):
                status = background(lambda sample=sample: validate_file(sample['path']), 150)
                check(status['validation_state'] == 'valid', '历史网站样本离线回归通过', sample=sample['label'])
                report['saved_media'].append({'label': sample['label'], 'source_url': sample.get('source_url'),
                                             'validation': status['validation'], 'online_download': False})
        report['heartbeat_max_gap'] = max((b - a for a, b in zip(ticks, ticks[1:])), default=0)
        report['heartbeat_gaps'] = gaps
        report['ui_samples'] = samples
        processing_gap = max((gap for gap, step in all_gaps if not step.startswith(('WebView2', '启动'))), default=0)
        report['processing_heartbeat_max_gap'] = processing_gap
        check(processing_gap < 1, '轨道下载、封装和完整检查在后台执行，Qt 界面保持响应', max_gap=processing_gap)
        if any(gap > 1 and step.startswith('WebView2') for gap, step in all_gaps):
            report['known_limitations'] = ['浏览器换页及候选窗口创建期间存在超过1秒的短暂停顿；全局与处理阶段心跳分别记录。']
        window.grab().save(str(directory / 'main-window.png'))
        report['passed'] = True
        save()
        return 0
    except Exception as error:
        report['error'] = str(error)
        report['traceback'] = traceback.format_exc()
        save()
        return 1
    finally:
        if fixture:
            fixture.close()
        if window:
            window.close()
            try:
                wait(lambda: window.batch_controller.is_closed and window.library_service.is_closed and
                     not window.queue.active_count and not window.thumbnails.busy, 30)
            except Exception as error:
                report['shutdown_error'] = str(error)
                report['passed'] = False
                save()
        timer.stop()
        sample_stop.set()
        faulthandler.cancel_dump_traceback_later()
        trace.close()
