"""当前 Rou 协议的真实 Chromium / 原队列 / yt-dlp / FFmpeg 验收。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from urllib.parse import urlsplit
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QMessageBox
from core.errors import ProtectedMediaError
from core.downloader import DownloadRequest
from core.media_guard import check_media
from core.media_groups import group_media
from core.site_resolvers import ResolverRegistry
from core.url_policy import UrlResolutionPolicy
from tests.rou_current_fixture import CurrentRouFixture, LocalCurrentRouResolver
from ui.discovery_dialog import DiscoveryDialog
from ui.main_window import MainWindow
from utils.config import ConfigStore


def run_rou_current_smoke(app, args):
    directory = Path(args[args.index('--rou-current-smoke') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from core.yt_dlp_manager import APP_DIR
    import tests.hls_fixture as hls
    import tests.integration_hls as integration
    hls.ROOT = integration.ROOT = APP_DIR
    fixture = CurrentRouFixture(directory / 'media')
    window = dialog = None
    checks, outputs, metrics, thumbnails = [], [], {}, []
    native = app.platformName() == 'windows'
    ticks = [0]
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    timer.start()
    app.setQuitOnLastWindowClosed(False)

    def pump(predicate=lambda: False, timeout=20, required=True):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.01)
        if required:
            raise AssertionError('当前 Rou 协议浏览器 / 下载验收等待超时')

    def close_dialog():
        nonlocal dialog
        if dialog:
            dialog.reject()
            pump(lambda: window._discovery_dialog is None, 20)
            dialog = None

    def find(identifier, predicate=None):
        nonlocal dialog
        close_dialog()
        dialog = DiscoveryDialog(window, fixture.url('/v/' + identifier),
                                 resolver_registry=ResolverRegistry([LocalCurrentRouResolver()]))
        window._discovery_dialog = dialog
        dialog.media_selected.connect(window.import_discovered_media)
        dialog.batch_requested.connect(window.download_discovered_batch)
        dialog.shutdown_complete.connect(lambda current=dialog: window._discovery_closed(current))
        dialog.showMinimized() if native else dialog.show()
        dialog.deep_find()
        pump(lambda: dialog._page_loaded and (predicate(dialog) if predicate else
             any(c.metadata.get('resolver_verified') and c.metadata.get('video_id') == identifier for c in dialog._candidates)), 30)
        pump(timeout=.3, required=False)
        return dialog.selected_candidate() or next((c for c in dialog._candidates
            if c.metadata.get('resolver_verified') and c.metadata.get('video_id') == identifier), None)

    def verify_job(job):
        pump(lambda: job.worker is None and job.state in ('completed', 'failed', 'cancelled'), 100)
        assert job.state == 'completed' and job.result['success'], (job.state, job.result, list(job.logs)[-5:])
        output = Path(job.result['files'][0])
        validations = job.result['output_validation']
        assert validations and all('mp4' in v['format_name'].split(',') and v['video_streams'] for v in validations)
        assert not output.read_bytes().startswith(b'\x89PNG')
        integration.probe(output)
        assert integration.decoded_hash(output) == integration.decoded_hash(fixture.directory / 'direct.mp4')
        original_result = deepcopy(job.result)
        pump(lambda: job.id in window.thumbnails.results and
             window.thumbnails.results[job.id]['status'] != 'pending', 70)
        thumbnail = window.thumbnails.results[job.id]
        assert thumbnail['status'] == 'generated', thumbnail
        assert Path(thumbnail['items'][0]['path']) == output.with_suffix('.jpg')
        assert output.with_suffix('.jpg').read_bytes().startswith(b'\xff\xd8\xff')
        assert not window._task_cards[job.id].thumbnail.pixmap().isNull()
        assert job.state == 'completed' and job.result == original_result
        thumbnails.append(thumbnail)
        outputs.append(str(output))
        return output

    def download_current(name):
        nonlocal dialog
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            dialog.confirm_button.click()
        pump(lambda: window._discovery_dialog is None)
        dialog = None
        window.filename.setText(name)
        job = window.add_current_to_queue()
        window.queue.start()
        verify_job(job)
        return job

    try:
        fixture.generate()
        fixture.start()
        window = MainWindow(ConfigStore(directory / 'configs' / (str(time.time_ns()) + '.json')))
        window.showMinimized() if native else window.show()
        pump(lambda: window.detector is None and window.yt_info.found and window.ff_info.found, 45)
        output_dir = directory / 'downloads' / str(time.time_ns())
        window.output_directory.setText(str(output_dir))
        captured = []
        for identifier in ('sample-a', 'sample-b', 'sample-c'):
            candidate = find(identifier)
            assert candidate.metadata['resolver_verified'] is True
            assert candidate.source == 'RouVideoResolver' and candidate.sources == {'Resolver'}
            assert candidate.score == 100 and candidate.confidence >= 70
            assert any('入口已验证' in reason for reason in candidate.confidenceReasons)
            assert candidate.media_type == 'HLS' and candidate.manifest_type == 'Media'
            assert candidate.manifest_text.startswith('#EXTM3U') and candidate.validated
            assert candidate.manifest_url == candidate.transport_url == candidate.url
            assert candidate.manifest_origin == 'embedded:png/roUd'
            assert candidate.playlist_type == 'VOD' and candidate.encryption == 'None' and candidate.drm is False
            assert candidate.url == fixture.index(identifier), (candidate.url, fixture.index(identifier))
            assert candidate.request_context.original_url == fixture.url('/api/hls/' + identifier)
            assert candidate.request_context.url_resolution_policy == UrlResolutionPolicy.STANDARD_URLJOIN
            trace = candidate.metadata['resolver_trace']
            assert trace['api_status'] == 302 and trace['container'] == 'PNG'
            assert trace['roud'] == 'FOUND' and trace['flag'] == '0x01' and trace['compression'] == 'zlib'
            assert trace['playlist'] == 'VALID' and candidate.metadata['segment_count'] == 7
            resources = candidate.metadata['manifest']['resources']
            segments = [r for r in resources if r['role'] == 'Media Segment']
            assert len(segments) == 7 and all(r['uri'].endswith('&v=6') for r in segments)
            assert len({urlsplit(r['uri']).netloc for r in segments}) == 3
            assert all(r['uri'].startswith('http://') and '.png?' in r['uri'] for r in segments)
            assert not any('/thumbs/' in c.url for c in dialog._candidates)
            assert len([g for g in dialog._groups if g.selected]) == 1
            assert dialog._groups[0].selected_candidate.metadata['resolver_verified']
            window._remember_resolver_session(candidate, dialog)
            captured.append(deepcopy(candidate))
        checks.append('当前API302 Location精确保留与三种CDN目录PNG分片结构真实浏览器验证')
        checks.append('PNG Chunk roUd flag zlib HLS VOD全部可见且协议验证与真实性评分分别保留')
        checks.append('绝对分片三CDN和原始签名Query保持原样不猜文件名')
        checks.append('TSR无NEXT页面正片优先于广告MP4且缩略图302排除')
        dialog.grab().save(str(directory / 'rou-current-discovery.png'))
        close_dialog()
        groups = group_media(captured)
        assert len(groups) == 3
        index_count_before = sum('/index.png' in r['path'] and r['path'].startswith('/hls/') for r in fixture.requests)
        jobs = window.download_manager.add_batch(groups, window._collect_request(captured[0].url, ''))
        assert len(jobs) == 3
        assert [Path(j.output_path).name for j in jobs] == ['Current Rou Fixture.mp4', 'Current Rou Fixture (1).mp4', 'Current Rou Fixture (2).mp4']
        assert all(j.request.concurrent_fragments == 4 for j in jobs)
        assert all(j.request.manifest_text.startswith('#EXTM3U') and j.request.download_protocol == 'HLS' for j in jobs)
        for job in jobs:
            verify_job(job)
        checks.append('三视频四分片原队列批量真实下载同名续号且解码画面一致')
        assert fixture.wrapped_segment_requests >= 21 and fixture.segment_flags == {0, 1}
        metrics['png_segment_payload'] = {'flags': sorted(fixture.segment_flags),
            'clear_wrapped_segments_downloaded': 21, 'decoded_video_hash_matches': True}
        checks.append('真实PNG分片roUd明文及zlib二进制TS载荷解包后合并为视频且画面哈希一致')
        index_count_after = sum('/index.png' in r['path'] and r['path'].startswith('/hls/') for r in fixture.requests)
        assert index_count_after == index_count_before, (index_count_before, index_count_after)
        metrics['embedded_manifest'] = {'batch_transport_refetches': 0, 'decoded_hls_body_carried': True}
        checks.append('Resolver候选到批量请求完整传递manifest_text且下载时不重新GET signed index.png')
        checks.append('PNG传输地址强制按声明HLS分流并下载EXTINF PNG分片而非直接复制索引')
        segment_requests = [r for r in fixture.requests if r['path'].startswith('/asset/sample-')]
        assert segment_requests and all(r['headers'].get('referer') and r['headers'].get('user-agent') for r in segment_requests)
        for candidate in captured:
            expected_range = candidate.request_context.headers.get('range') if candidate.request_context.inherit_range else None
            actual = [r for r in segment_requests if r['path'].startswith('/asset/' + candidate.metadata['video_id'] + '/')]
            assert actual and all(r['headers'].get('range') == expected_range for r in actual)
        assert all(not r['headers'].get('cookie') and not r['headers'].get('authorization') for r in segment_requests)
        checks.append('实际浏览器最终CDN上下文与Range继承不误复制API会话凭据')
        candidate = find('aes')
        assert candidate.classification == 'HLS_AES128' and candidate.drm is False
        assert candidate.encryption == 'AES-128' and candidate.metadata['segment_count'] == 4
        download_current('AES PNG')
        assert any(r['path'].endswith('/enc.key') for r in fixture.requests)
        checks.append('标准AES128 key和伪装PNG分片通过原HLS下载合并且画面一致')
        checks.append('PNG分片roUd内标准AES128密文先解包再由原yt-dlp解密且画面一致')
        checks.append('所有成功MP4经实际ffprobe验证MP4 family容器与真实视频流')
        invalid = window.queue.add(DownloadRequest(window.yt_info.path, fixture.url('/invalid-output/index.mp4'),
                            str(output_dir), 'PNG MUST FAIL', ffmpeg_path=window.ff_info.path))
        window.queue.start()
        pump(lambda: invalid.worker is None and invalid.state in ('completed', 'failed'), 80)
        assert invalid.state == 'failed' and not invalid.result['success'], invalid.result
        assert invalid.result['error_code'] == 'OUTPUT_NOT_VIDEO', invalid.result
        assert invalid.result['diagnostic']['format_name'] == 'png_pipe', invalid.result
        assert not invalid.result['files']
        assert not (output_dir / 'PNG MUST FAIL.mp4').exists()
        rejected = Path(invalid.request.temporary_directory) / 'staging' / 'PNG MUST FAIL.mp4'
        assert rejected.read_bytes().startswith(b'\x89PNG')
        checks.append('真实yt-dlp零退出PNG伪MP4被ffprobe判OUTPUT_NOT_VIDEO且队列FAILED，仅保留暂存文件')
        candidate = find('png-segments')
        with patch('ui.discovery_dialog.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            dialog.confirm_button.click()
        pump(lambda: window._discovery_dialog is None)
        dialog = None
        window.filename.setText('HLS PNG MUST FAIL')
        invalid_hls = window.add_current_to_queue()
        window.queue.start()
        pump(lambda: invalid_hls.worker is None and invalid_hls.state in ('completed', 'failed'), 80)
        assert invalid_hls.request.download_protocol == 'HLS' and invalid_hls.request.manifest_text.startswith('#EXTM3U')
        assert invalid_hls.state == 'failed' and invalid_hls.result['error_code'] == 'OUTPUT_NOT_VIDEO', invalid_hls.result
        assert not invalid_hls.result['files']
        assert not (output_dir / 'HLS PNG MUST FAIL.mp4').exists()
        checks.append('正确HLS管线中服务器实际返回PNG分片同样FAILED不将图片拼接误报成功')
        candidate = find('cors')
        assert candidate.request_context.cookies == ''
        download_current('CORS PNG')
        checks.append('同源API合法Cookie和跨CDN通配CORS不强制include凭据')
        candidate = find('image')
        assert candidate.request_context.origin == ''
        download_current('Observed Image')
        checks.append('已加载Image的真实PNG响应经CDP取body且缺失Origin保持缺失')
        find('denied', lambda d: any(item.get('error_code') == 'AUTH_REQUIRED' for item in d._resolver_diagnostics))
        assert not any(c.metadata.get('resolver_verified') for c in dialog._candidates)
        assert not any(item.get('error_code') == 'PROTECTED_MEDIA' for item in dialog._resolver_diagnostics)
        checks.append('API401合法会话缺失单独诊断不猜CDN不判DRM')
        find('generic', lambda d: any(c.media_type == 'Direct' and c.downloadable for c in d._candidates))
        generic = next(c for c in dialog._candidates if c.media_type == 'Direct' and c.downloadable)
        assert not generic.metadata.get('resolver_verified')
        assert dialog.selected_candidate() is None or dialog.selected_candidate().confidence >= 70
        checks.append('当前API失败保留Generic普通Direct后备解析')
        candidate = find('drm')
        assert candidate.drm is True and not candidate.downloadable and not dialog.confirm_button.isEnabled()
        before = len(fixture.requests)
        try:
            check_media(candidate.request_context)
        except ProtectedMediaError:
            pass
        else:
            raise AssertionError('FairPlay应由标准DRM guard拒绝')
        assert not any('/asset/drm/' in r['path'] for r in fixture.requests[before:])
        checks.append('真实FairPlay KEYFORMAT解析与下载guard拒绝且无密钥许可证请求')
        find('retry')
        retry = download_current('Retry PNG')
        attempts = sum(r['path'].startswith('/asset/retry/') and '/opaque-2.png' in r['path'] for r in fixture.requests)
        assert attempts == 2 and retry.result['success']
        checks.append('暂时HTTP500正常重试后完成PNG分片真实下载')
        candidate = find('expiry')
        original_profile = dialog.profile
        window.concurrent_fragments.setValue(1)
        expired = download_current('Signed Refresh')
        assert expired.retry_count == 1, expired.retry_count
        assert expired.request.manifest_text.startswith('#EXTM3U')
        assert expired.request.manifest_url == expired.request.url and expired.request.download_protocol == 'HLS'
        assert expired.request.media_metadata['video_id'] == 'expiry'
        assert window._resolver_sessions[expired.request.media_metadata['browser_session_id']] is original_profile
        assert 'new-random/index.png' in expired.request.url
        assert urlsplit(candidate.url).netloc != urlsplit(expired.request.url).netloc
        old = [r for r in fixture.requests if r['path'].startswith('/asset/expiry/old-random/')]
        new = [r for r in fixture.requests if r['path'].startswith('/asset/expiry/new-random/')]
        assert sum('opaque-0.png' in r['path'] for r in old) == 1
        assert sum('opaque-1.png' in r['path'] for r in old) == 1
        assert new and not any(r['path'].endswith(('opaque-0.png', 'opaque-1.png')) for r in new)
        api_requests = [r for r in fixture.requests if r['path'] == '/api/hls/expiry']
        assert len(api_requests) >= 2 and all(fixture.cookie in r['headers'].get('cookie', '') for r in api_requests)
        metrics['auto_refresh'] = {'retries': expired.retry_count, 'redownloaded_finished_fragments': 0,
                                   'same_in_memory_browser_profile': True, 'cdn_and_directory_changed': True}
        checks.append('单视频签名401自动复用原合法会话重新API解析并跨CDN目录续传不重下已完成分片')
        checks.append('同视频API身份与分片布局先核验后复用原任务部分文件')
        checks.append('过期刷新替换新manifest_text和最终base URL不复用旧正文')
        safe = json.dumps([c.safe_diagnostic() for c in captured], ensure_ascii=False)
        saved = window.config_store.path.read_text(encoding='utf-8')
        assert fixture.cookie not in safe + saved and 'old%2Factual%2Bvalue' not in safe + saved
        assert 'local-authorized-session' not in saved
        checks.append('诊断Trace配置脱敏且下载配置不序列化CookieAuthorization')
        assert ticks[0] > 100
        checks.append('浏览器解析并行下载隐藏刷新期间GUI保持响应和生命周期清理')
        checks.append('Rou所有已验证视频自动生成本地JPEG且下载成功结果保持不变')
        report = {'passed': True, 'checks': checks, 'metrics': metrics, 'outputs': outputs,
                  'thumbnails': thumbnails,
                  'packaged': bool(getattr(sys, 'frozen', False)), 'qt_platform': app.platformName(),
                  'source_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() if getattr(sys, 'frozen', False) else '',
                  'gui_ticks': ticks[0], 'third_party_site_tested': False,
                  'fixture': 'FFmpeg自生成视频 / localhost多origin / 测试专用当前协议Resolver'}
    except Exception as error:
        report = {'passed': False, 'checks': checks, 'metrics': metrics, 'error': str(error), 'traceback': traceback.format_exc()}
        if dialog:
            report['status'] = dialog.status_label.text()
            report['candidates'] = [candidate.safe_diagnostic() for candidate in dialog._candidates]
            report['resolver_diagnostics'] = dialog._resolver_trace_values()
    finally:
        timer.stop()
        if dialog:
            close_dialog()
        if window:
            window.queue.stop_all()
            pump(lambda: window.queue.active_count == 0, 20)
            window.close()
            if window._webview2_profile:
                pump(lambda: window._webview2_profile.host._finished_once)
            pump(timeout=.2, required=False)
        fixture.close()
    (directory / 'rou-current-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1
