"""真实 Chromium 播放器的 MediaFlow 回归；仅使用本地自生成媒体。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QMessageBox, QLabel

from tests.discovery_fixture import DiscoveryFixture
from ui.main_window import MainWindow
from utils.config import ConfigStore
from utils.security import Redactor


def run_mediaflow_smoke(app, args):
    directory = Path(args[args.index('--mediaflow-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as fixtures
    fixtures.ROOT = APP_DIR
    fixture = DiscoveryFixture(directory / 'media')
    window = dialog = None
    checks, cases, snapshots = [], [], []
    quality_download = None
    app.setQuitOnLastWindowClosed(False)

    def pump(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError('等待 MediaFlow 浏览器或下载验收条件超时')

    def close_dialog():
        nonlocal dialog
        if dialog:
            dialog.reject()
            pump(lambda: window._discovery_dialog is None)
            dialog = None

    def card_visible(card, scroll):
        viewport = scroll.viewport()
        bounds = card.rect().translated(card.mapTo(viewport, card.rect().topLeft()))
        return card.isVisible() and viewport.rect().intersects(bounds)

    def download_visible(card):
        dialog.candidate_card_scroll.ensureWidgetVisible(card.download_button, 10, 10)
        return card.download_button.isVisible() and dialog.candidate_card_scroll.viewport().rect().contains(
            card.download_button.mapTo(dialog.candidate_card_scroll.viewport(), card.download_button.rect().center()))

    def candidate(name):
        return next((item for item in dialog._candidates if item.url.split('?', 1)[0].endswith('/' + name + '.webm')), None)

    def loaded(names):
        return any(all(any(video.get('current_src', '').endswith('/' + name + '.webm') and
                              video.get('ready_state', 0) >= 1 and video.get('duration', 0) > 5
                              for video in snapshot.get('videos', [])) for name in names)
                   for snapshot in snapshots[-6:])

    def find(case, names, ready):
        nonlocal dialog
        snapshots.clear()
        dialog = window.open_discovery()
        dialog.observer.dom.connect(lambda snapshot: snapshots.append(snapshot))
        dialog.page_url.setText(fixture.page(case))
        dialog.find_button.click()
        pump(lambda: loaded(names) and ready())

    def save(case):
        cases.append({'case': case, 'selected': dialog.selected_candidate().id if dialog.selected_candidate() else None,
                      'checked': [group.selected_candidate.id for group in dialog._groups if group.selected],
                      'candidates': [item.safe_diagnostic() for item in dialog._candidates]})

    try:
        fixture.generate()
        fixture.generate_quality_versions()
        fixture.start()
        config_store = ConfigStore(directory / 'test-config.json')
        assert config_store.save({}), config_store.error
        window = MainWindow(config_store)
        window.showMinimized() if app.platformName() == 'windows' else window.show()
        pump(lambda: window.detector is None and window.yt_info.found and window.ff_info.found, 30)

        def main_ready():
            main, rec, hidden = candidate('main'), candidate('recommendation'), candidate('preloaded')
            return bool(main and rec and hidden and main.confidence == 100 and rec.confidence == 70
                        and hidden.confidence < 40 and dialog.selected_candidate()
                        and dialog.selected_candidate().id == main.id)

        find('confidence-multiple', ('main', 'recommendation', 'preloaded'), main_ready)
        assert [group.selected_candidate.id for group in dialog._groups if group.selected] == [candidate('main').id]
        assert candidate('preview').confidence < 40
        assert '100/100' in dialog.table.item(0, 6).text()
        assert '✓ 当前主播放器 video.currentSrc (+40)' in dialog.details.toPlainText()
        checks.append('真实多video：当前播放器100分优先于可见推荐70分，默认只勾选正片')
        main, recommendation, hidden = candidate('main'), candidate('recommendation'), candidate('preloaded')
        hidden_card = dialog._candidate_cards[hidden.id]
        assert hidden_card.isHidden() and not dialog.include_low.isChecked()
        assert '推荐' in dialog._candidate_cards[main.id].confidence_label.text()
        assert '需确认' in dialog._candidate_cards[recommendation.id].confidence_label.text()
        assert '时长检测通过' in dialog._candidate_cards[main.id].reasons_label.toolTip()
        dialog.include_low.setChecked(True)
        assert dialog._candidate_cards[hidden.id] is hidden_card and not hidden_card.isHidden()
        assert hidden_card.download_button.isEnabled() is True
        assert len(dialog._visible_candidates()) == len(dialog._candidates)
        app.processEvents()
        dialog.candidate_card_scroll.ensureWidgetVisible(hidden_card)
        pump(lambda: card_visible(hidden_card, dialog.candidate_card_scroll))
        dialog.grab().save(str(directory / 'candidate-sections-expanded.png'))
        dialog.include_low.setChecked(False)
        dialog.candidate_card_scroll.verticalScrollBar().setValue(0)
        assert hidden_card.isHidden() and dialog.selected_candidate().id == main.id
        checks.append('候选展示：推荐/需确认/低可信度分区，低分默认折叠、可展开且不改变原选择')
        save('multiple')

        completed = []
        dialog.page.runJavaScript("document.getElementById('main').pause();true", completed.append)
        pump(lambda: completed and any(any(video.get('paused') is True and video.get('is_main') is True
                                            for video in snapshot.get('videos', [])) for snapshot in snapshots[-2:]))
        assert candidate('main').confidence == 100
        checks.append('真实暂停播放器继续关联 currentSrc，不被预加载替换')
        close_dialog()

        find('confidence-preload', ('main', 'recommendation', 'preloaded'), main_ready)
        hidden = candidate('preloaded')
        assert any('隐藏' in reason for reason in hidden.confidenceReasons)
        assert not any('currentSrc' in reason for reason in hidden.confidenceReasons)
        assert hidden.download_payload()['download_protocol'] == 'Direct'
        checks.append('真实预加载：祖先display:none被识别隐藏，预览与隐藏资源不自动下载')
        save('preload')
        close_dialog()

        find('confidence-ambiguous', ('equal-one', 'equal-two'), lambda: bool(candidate('equal-one') and candidate('equal-two')
             and candidate('equal-one').confidence == candidate('equal-two').confidence == 70
             and dialog.selected_candidate() is None))
        assert not dialog.confirm_button.isEnabled()
        assert not any(group.selected for group in dialog._groups)
        checks.append('真实两个同等可见暂停播放器：同分时不猜第一个，要求手动选择')
        save('ambiguous')
        close_dialog()

        find('confidence-switch', ('main', 'recommendation', 'preloaded'), main_ready)
        old_id = candidate('main').id
        dialog.page.runJavaScript("(() => {const v=document.getElementById('main');v.src='/media/next.webm';v.play().catch(()=>{});return true;})()")
        pump(lambda: bool(candidate('next') and candidate('next').confidence == 100 and candidate('main').confidence < 70
                          and dialog.selected_candidate() and dialog.selected_candidate().id == candidate('next').id))
        assert old_id != dialog.selected_candidate().id
        assert not any('currentSrc' in reason for reason in candidate('main').confidenceReasons)
        checks.append('真实currentSrc切换：旧正片分数即时失效，新正片自动成为最高分')
        save('switch')
        close_dialog()

        find('confidence-poster', ('main',), lambda: bool(candidate('main') and candidate('main').confidence == 100))
        assert not any('缩略图' in reason or '图片' in reason for reason in candidate('main').confidenceReasons)
        assert dialog.selected_candidate().id == candidate('main').id
        pump(lambda: candidate('main').id in dialog._candidate_cards and
             card_visible(dialog._candidate_cards[candidate('main').id], dialog.candidate_card_scroll))
        dialog.grab().save(str(directory / 'mediaflow-discovery.png'))
        checks.append('真实video poster不会将正在播放的媒体错判为缩略图')
        save('poster')
        assert dialog.media_details.values['type'].text() == 'Direct'
        assert dialog.media_details.values['container'].text() in ('WebM', 'WebM/Matroska')
        assert dialog.media_details.values['resolution'].text() == '320 × 180'
        assert dialog.media_details.values['duration'].text() != '未知'
        assert dialog.media_details.values['videoCodec'].text() == '未知'
        assert window.download_quality.currentData() == dialog.quality_preference_combo.currentData() == 'auto'
        checks.append('下载前媒体详情读取真实播放器尺寸与时长，未知编码保持未知，质量偏好默认为自动推荐')
        close_dialog()

        assert window.windowTitle() == app.applicationName() == 'MediaFlow'
        assert window.subtitle.text() == '智能媒体发现与下载工具'
        assert window.english_subtitle.text() == 'Intelligent Media Discovery & Download Tool'
        assert app.organizationName() == 'MediaFlow' and app.applicationVersion()
        assert not window.queue.jobs and not window._task_cards
        assert window.url.placeholderText() == '请输入媒体URL，或使用自动查找。'
        assert not any(label.text() == '粘贴媒体网址开始分析' for label in window.findChildren(QLabel))
        assert window.task_scroll.isHidden()
        assert window.grab().save(str(directory / 'mediaflow-home-empty.png'))
        checks.append('MediaFlow空首页显示副标题和输入提示，无重复空白区域；零任务时折叠任务卡片区')

        # 复用原 fixture 的 HLS/AES 媒体完成卡片下载，不改下载默认容器或浏览器元数据。
        # 评分场景的 VP8 WebM 无法直接 remux 为默认 MP4，不能用它冒充 MP4 验收。
        window.output_directory.setText(str(directory / 'downloads' / str(time.time_ns())))
        window.filename.setText('MediaFlow card video')
        window.url.setText(fixture.page('hls_aes'))
        window.discovery_button.click()
        dialog = window._discovery_dialog
        assert dialog is not None and dialog.page_url.text() == fixture.page('hls_aes')
        dialog.find_button.click()

        def hls_ready():
            return next((item for item in dialog._candidates if item.media_type == 'HLS'
                         and item.classification == 'HLS_AES128' and item.context_complete
                         and 40 <= item.confidence < 70 and item.id in dialog._candidate_cards), None)

        pump(lambda: hls_ready() is not None)
        selected = deepcopy(hls_ready())
        card = dialog._candidate_cards[selected.id]
        pump(lambda: download_visible(card))
        assert dialog.selected_candidate() is None and not dialog.confirm_button.isEnabled()
        assert card.title_label.text() and card.source_label.text().startswith('来源：')
        assert 'HLS' in card.meta_label.text() and 'AES-128' in card.meta_label.text()
        assert '时长' in card.meta_label.text() and '--' not in card.meta_label.text().split('时长', 1)[1].split('·', 1)[0]
        assert f'{selected.confidence}/100' in card.confidence_label.text()
        assert '暂无预览' in card.thumbnail.text() and card.thumbnail.pixmap().isNull()
        assert card.checked_box.isEnabled() and not card.checked_box.isChecked()
        assert card.quality_combo.count() >= 1
        assert card.download_button.text() == '下载' and card.download_button.isEnabled()
        assert dialog.grab().save(str(directory / 'mediaflow-candidate-cards.png'))
        save('candidate-card-download')
        checks.append('候选卡片展示真实来源、HLS AES时长、可信度与明确占位预览，中分不默认选择')

        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as confirmation:
            card.download_button.click()
            pump(lambda: len(window.queue.jobs) == 1 and window._discovery_dialog is None)
        confirmation.assert_called_once()
        dialog = None
        job = window.queue.jobs[0]
        assert job.request.to_context() == selected.request_context
        pump(lambda: job.worker is None and job.state in ('completed', 'failed', 'cancelled'), 75)
        assert job.state == 'completed' and job.result and job.result['success'], (job.result, list(job.logs)[-15:])
        output = Path(job.result['files'][0])
        assert output.is_file() and output.suffix == '.mp4'
        import tests.integration_hls as integration
        integration.ROOT = APP_DIR
        media = integration.probe(output)
        assert integration.decoded_hash(output) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        validation = job.result['output_validation'][0]
        assert 'mp4' in validation['format_name'].split(',') and validation['video_streams']
        assert job.id in window._task_cards and not window.task_scroll.isHidden()
        task = window._task_cards[job.id]
        pump(lambda: card_visible(task, window.task_scroll))
        assert job.name in task.name_label.text() and task.state_label.text() == '完成'
        assert task.progress_bar.minimum() == 0 and task.progress_bar.maximum() == 1000
        assert task.progress_bar.value() == 1000 and task.progress_bar.text() == '100%'
        assert '速度' in task.speed_label.text()
        before_thumbnail = deepcopy(job.result)
        pump(lambda: job.id in window.thumbnails.results and
             window.thumbnails.results[job.id]['status'] != 'pending', 70)
        thumbnail = window.thumbnails.results[job.id]
        assert thumbnail['status'] == 'generated', thumbnail
        assert Path(thumbnail['items'][0]['path']) == output.with_suffix('.jpg')
        assert output.with_suffix('.jpg').is_file() and not task.thumbnail.pixmap().isNull()
        assert '✓ 缩略图生成成功' in task.completion_label.text()
        assert '✓ 媒体验证通过' in task.completion_label.text()
        assert job.state == 'completed' and job.result == before_thumbnail
        assert window.grab().save(str(directory / 'mediaflow-task-completed.png'))
        checks.append('点击卡片下载并确认后原队列真实完成，任务卡片100%，MP4容器、音视频和画面哈希通过')
        checks.append('HLS下载验证后后台生成同名JPEG并显示本地封面，原成功结果不变')

        # 真正不同尺寸的 HLS 版本：确认推荐、偏好、手动选择及输出一致。
        window.filename.setText('MediaFlow quality 720p')
        dialog = window.open_discovery(page_url=fixture.page('quality-versions'))
        dialog.find_button.click()

        def quality_group():
            return next((group for group in dialog._groups if {candidate.height for candidate in
                dialog._choices_for_group(group)} == {480, 720, 1080} and all(
                candidate.duration and 200 <= candidate.http_status < 300 for candidate in dialog._choices_for_group(group))), None)

        pump(lambda: quality_group() is not None, 45)
        dialog.quality_preference_combo.setCurrentIndex(dialog.quality_preference_combo.findData('highest'))
        group = quality_group()
        assert group.selected_candidate.height == 1080
        assert window.download_quality.currentData() == 'highest'
        assert window.config_store.load()['quality_preference'] == 'highest'
        choices = dialog._choices_for_group(group)
        assert {candidate.qualityScore for candidate in choices} == {25, 35, 45}
        assert all(candidate.fps == 25 and candidate.videoCodec.startswith('avc1') for candidate in choices)
        dialog.quality_preference_combo.setCurrentIndex(dialog.quality_preference_combo.findData('smallest'))
        group = quality_group()
        assert group.selected_candidate.height == 480
        low = next(candidate for candidate in dialog._choices_for_group(group) if candidate.height == 720)
        dialog._choose_quality(group.id, low.id)
        dialog._render_candidates()
        selected_quality = deepcopy(dialog.selected_candidate())
        assert selected_quality.height == 720
        assert dialog.media_details.values['resolution'].text() == '1280 × 720'
        assert dialog.media_details.values['size'].text().startswith('约 ')
        quality_card = dialog._candidate_cards[selected_quality.id]
        assert '质量 35/100' in quality_card.meta_label.text()
        pump(lambda: download_visible(quality_card))
        dialog.grab().save(str(directory / 'mediaflow-quality-selection.png'))
        save('quality-versions')
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as question:
            quality_card.download_button.click()
            pump(lambda: len(window.queue.jobs) == 2 and window._discovery_dialog is None)
        assert question.call_count == (1 if selected_quality.requires_confirmation else 0)
        dialog = None
        quality_job = window.queue.jobs[-1]
        assert quality_job.request.url == selected_quality.url and '/720.m3u8' in quality_job.request.url
        assert quality_job.request.to_context() == selected_quality.request_context
        pump(lambda: quality_job.worker is None and quality_job.state in ('completed', 'failed', 'cancelled'), 75)
        assert quality_job.state == 'completed', (quality_job.result, list(quality_job.logs)[-15:])
        quality_output = Path(quality_job.result['files'][0])
        quality_probe = integration.probe(quality_output)
        quality_video = quality_job.result['output_validation'][0]['video_streams'][0]
        assert quality_video['width'] == 1280 and quality_video['height'] == 720
        quality_probe['actual_video'] = quality_video
        assert 'mp4' in quality_job.result['output_validation'][0]['format_name'].split(',')
        pump(lambda: quality_job.id in window.thumbnails.results and
             window.thumbnails.results[quality_job.id]['status'] != 'pending', 70)
        assert window.thumbnails.results[quality_job.id]['status'] == 'generated'
        quality_download = {'output': str(quality_output), 'selected_id': selected_quality.id,
                            'details': selected_quality.safe_diagnostic(), 'media': quality_probe,
                            'output_validation': quality_job.result['output_validation'],
                            'thumbnail': window.thumbnails.results[quality_job.id]}
        checks.append('真实480/720/1080 HLS版本：最高画质选1080，最小文件选480，手动720选择在更新后保留')
        checks.append('原下载队列按选中的720p实际URL输出1280×720 MP4并生成封面；请求上下文和媒体验证保持')
        # 无可见播放器关联的真实 HLS 请求产生低分候选，手动确认后走原下载管线。
        window.filename.setText('MediaFlow low confidence HLS')
        dialog = window.open_discovery(page_url=fixture.page('hls-low'))
        dialog.find_button.click()
        pump(lambda: any(c.media_type == 'HLS' and c.confidence < 40 and c.downloadable
                         and c.id in dialog._candidate_cards for c in dialog._candidates))
        low = deepcopy(next(c for c in dialog._candidates if c.media_type == 'HLS' and c.downloadable))
        low_score = low.confidence
        assert not dialog.include_low.isChecked() and dialog._candidate_cards[low.id].isHidden()
        dialog.include_low.setChecked(True)
        low_card = dialog._candidate_cards[low.id]
        assert low_card.download_button.isEnabled()
        dialog.candidate_card_scroll.ensureWidgetVisible(low_card)
        app.processEvents()
        dialog._select_card(low.id)
        assert dialog.selected_candidate().id == low.id and dialog.confirm_button.isEnabled()
        assert '需确认' in low_card.state_label.text() and '可下载' in low_card.state_label.text()
        pump(lambda: download_visible(low_card))
        assert dialog.grab().save(str(directory / 'low-confidence-manual-download.png'))
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.No):
            low_card.download_button.click()
        assert len(window.queue.jobs) == 2 and window._discovery_dialog is dialog
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as question:
            low_card.download_button.click()
            pump(lambda: len(window.queue.jobs) == 3 and window._discovery_dialog is None)
        question.assert_called_once()
        dialog = None
        low_job = window.queue.jobs[-1]
        assert low_job.request.to_context() == low.request_context
        pump(lambda: low_job.worker is None and low_job.state in ('completed', 'failed', 'cancelled'), 75)
        assert low_job.state == 'completed', (low_job.result, list(low_job.logs)[-15:])
        low_output = Path(low_job.result['files'][0])
        assert 'mp4' in low_job.result['output_validation'][0]['format_name'].split(',')
        assert integration.decoded_hash(low_output) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        low_download = {'confidence': low_score, 'output': str(low_output), 'state': low_job.state,
                        'context_preserved': True, 'output_validation': low_job.result['output_validation'],
                        'decoded_video_matches': True}
        checks.append('真实低分HLS：默认折叠但可选择，取消不入队，确认后原队列完成且上下文/MP4/画面哈希通过')
        report = {'passed': True, 'packaged': bool(getattr(sys, 'frozen', False)), 'qt_platform': app.platformName(),
                  'checks': checks, 'cases': cases, 'fixture': 'localhost / FFmpeg 自生成视频 / 真实 Chromium video DOM',
                  'card_download': {'output': str(output), 'state': job.state, 'progress': task.progress_bar.value(),
                                    'media': media, 'output_validation': validation, 'decoded_video_matches': True,
                                    'thumbnail': thumbnail},
                  'online_site_test': False,
                  'quality_download': quality_download,
                  'low_confidence_download': low_download,
                  'exe_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, 'frozen', False) else ''}
    except Exception as error:
        if dialog:
            dialog.grab().save(str(directory / 'failure-ui.png'))
        report = {'passed': False, 'packaged': bool(getattr(sys, 'frozen', False)), 'qt_platform': app.platformName(),
                  'checks': checks, 'cases': cases, 'error': Redactor().redact(str(error)),
                  'traceback': Redactor().redact(traceback.format_exc()),
                  'last_candidates': [item.safe_diagnostic() for item in dialog._candidates] if dialog else []}
    finally:
        close_dialog()
        if window:
            window.queue.stop_all()
            if window.queue.active_count:
                pump(lambda: window.queue.active_count == 0, 25)
            window.close()
            if window._webview2_profile:
                pump(lambda: window._webview2_profile.host._finished_once)
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        fixture.stop()
    (directory / 'mediaflow-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in ('cases', 'last_candidates')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1
