"""运行真实 yt-dlp + FFmpeg；不访问任何第三方视频。"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication, QTimer
from core.downloader import DownloadRequest, DownloadWorker, friendly_error
from core.yt_dlp_manager import detect_yt_dlp, detect_ffmpeg
from tests.hls_fixture import HlsFixture, FIXTURE_HEADERS, DISGUISED_HEADERS, OBSERVED_HEADERS


def run_worker(app, request, stop_after=None):
    worker = DownloadWorker(request)
    results, logs, progress, statuses = [], [], [], []
    worker.completed.connect(results.append)
    worker.log.connect(logs.append)
    worker.progress.connect(progress.append)
    worker.status.connect(statuses.append)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(worker.request_stop)
    worker.start()
    if stop_after:
        timer.start(stop_after)
    deadline = time.monotonic() + 70
    while (worker.isRunning() or not results) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    timer.stop()
    if worker.isRunning():
        worker.request_stop()
        worker.wait(15000)
        raise RuntimeError('下载测试超时')
    worker.wait()
    app.processEvents()
    return results[0], logs, progress, statuses


def probe(path):
    result = subprocess.run([str(ROOT / 'tools' / 'ffprobe.exe'), '-v', 'error', '-show_entries',
                             'format=duration:stream=codec_type,codec_name', '-of', 'json', str(path)],
                            shell=False, capture_output=True, encoding='utf-8', check=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
    data = json.loads(result.stdout)
    assert float(data['format']['duration']) >= 6.8, data
    assert any(s['codec_type'] == 'video' for s in data['streams']), data
    assert any(s['codec_type'] == 'audio' for s in data['streams']), data
    return data


def decoded_hash(path):
    result = subprocess.run([str(ROOT / 'tools' / 'ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-i', str(path),
                             '-map', '0:v:0', '-f', 'hash', '-hash', 'sha256', '-'], shell=False, capture_output=True,
                            encoding='utf-8', check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=20)
    return result.stdout.strip()


def main():
    directory = ROOT / 'verification'
    directory.mkdir(exist_ok=True)
    app = QCoreApplication.instance() or QCoreApplication([])
    yt, ff = detect_yt_dlp(), detect_ffmpeg()
    assert yt.found and ff.found, (yt, ff)
    fixture = HlsFixture(directory / 'media')
    fixture.generate()
    fixture.start()
    checks = []
    run_directory = directory / 'downloads' / str(time.time_ns())
    try:
        cases = [
            ('plain_url', '/plain/index.m3u8', {}, ''),
            ('signed_url_ampersand', '/signed/index.m3u8?sign=fixture%2Fsigned%2Bvalue&t=1234567890', {}, 'signed & output'),
            ('full_dynamic_headers', '/headers/index.m3u8', FIXTURE_HEADERS, 'headers_test'),
            ('retry_423', '/retry/index.m3u8', {}, 'retry_test'),
            ('master_playlist', '/master.m3u8', {}, 'master_test'),
            ('direct_mp4', '/plain/direct.mp4', {}, 'direct_test'),
            ('literal_percent_filename', '/plain/index.m3u8', {}, '100% done'),
            ('aes128_headers_key_segments', '/aes/endpoint?sign=a%2Fb%2Bc&t=123', FIXTURE_HEADERS, 'aes128_test'),
            ('redirect_final_relative_aes', '/redirect', FIXTURE_HEADERS, 'redirect_aes'),
            ('cross_origin_key_segments', '/cross-host', FIXTURE_HEADERS, 'cross_origin'),
            ('dash_no_extension_template', '/dash/endpoint', FIXTURE_HEADERS, 'dash_test'),
            ('post_manifest_body', '/post/endpoint', FIXTURE_HEADERS, 'post_test'),
            ('head_request_compatible', '/plain/index.m3u8', {}, 'head_test'),
            ('mp4_large_moov_at_end', '/plain/tail.mp4', {}, 'tail_test'),
            ('aes128_png_segment_semantics', '/disguised/index', DISGUISED_HEADERS, 'png_test'),
            ('aes128_mixed_extension_segments', '/disguised/mixed', DISGUISED_HEADERS, 'mixed_segments'),
            ('browser_metadata_opt_in', '/disguised-compat/index', DISGUISED_HEADERS, 'metadata_test'),
            ('origin_never_invented', '/disguised-no-origin/index',
             {key: value for key, value in DISGUISED_HEADERS.items() if key != 'origin'}, 'no_origin_test'),
            ('observed_range_no_credentials', '/observed/index.m3u8?sign=keep%2Fraw&t=123', OBSERVED_HEADERS, 'observed_range'),
            ('observed_range_cross_host', '/observed-cross/index.m3u8', OBSERVED_HEADERS, 'observed_cross'),
            ('observed_range_redirect', '/observed-redirect', OBSERVED_HEADERS, 'observed_redirect'),
        ]
        for name, route, headers, filename in cases:
            start_request = len(fixture.requests)
            output = run_directory / name / '新建目录 & spaces'
            request = DownloadRequest(yt.path, fixture.base + route, str(output), filename, headers, ff.path)
            if name == 'post_manifest_body':
                request.method, request.body = 'POST', b'fixture=body%2Bvalue'
            if name == 'head_request_compatible':
                request.method = 'HEAD'
            if name == 'browser_metadata_opt_in':
                request.inherit_browser_metadata = True
            if name.startswith('observed_range_'):
                request.inherit_range = True
            result, logs, progress, statuses = run_worker(app, request)
            assert result['success'], (name, result, '\n'.join(logs[-20:]))
            assert result['files'] and Path(result['files'][0]).suffix == '.mp4', result
            data = probe(result['files'][0])
            if name in ('aes128_headers_key_segments', 'redirect_final_relative_aes', 'cross_origin_key_segments'):
                assert result['diagnostic']['classification'] == 'HLS_AES128', result
                assert result['diagnostic']['drm'] is False, result
                assert decoded_hash(result['files'][0]) == decoded_hash(fixture.directory / 'direct.mp4'), name
                hits = [r for r in fixture.requests if r['path'].startswith('/aes/')]
                assert any(r['path'].endswith('key.dat') for r in hits), hits
                assert any(r['path'].endswith('.ts') for r in hits), hits
                assert all(r['headers_match'] for r in hits), hits
            if name == 'redirect_final_relative_aes':
                assert 'localhost' in result['diagnostic']['final_url'], result
                assert result['diagnostic']['redirects'][0]['status'] == 302, result
            if name == 'dash_no_extension_template':
                assert result['diagnostic']['media_type'] == 'DASH', result
                hits = [r for r in fixture.requests if r['path'].startswith('/dash/')]
                assert all(r['headers_match'] for r in hits), hits
                assert any(r['path'].endswith('.m4s') for r in hits), hits
            if name == 'post_manifest_body':
                hits = [r for r in fixture.requests if r['path'].startswith('/post/')]
                assert any(r['method'] == 'POST' and r['post_body_match'] for r in hits), hits
                assert all(r['method'] == 'GET' for r in hits if r['path'].endswith('.ts')), hits
            if name == 'retry_423':
                assert any('重试' in status for status in statuses), statuses
                assert fixture.retry_counts['/retry/part002.ts'] >= 3, fixture.retry_counts
            if headers:
                for secret in ('fixture-private-cookie', 'fixture-private-auth', 'fixture-private-key'):
                    assert secret not in '\n'.join(logs), logs
            if name == 'signed_url_ampersand':
                hits = [r for r in fixture.requests if r['path'].startswith('/signed/')]
                assert hits and all(r['signed_query_intact'] for r in hits), hits
            semantic_evidence = None
            if name in ('aes128_png_segment_semantics', 'aes128_mixed_extension_segments',
                        'browser_metadata_opt_in', 'origin_never_invented'):
                diagnostic = result['diagnostic']
                assert diagnostic['protocol'] == 'HLS' and diagnostic['playlist_type'] == 'VOD', diagnostic
                assert diagnostic['encryption'] == 'AES-128' and diagnostic['drm'] is False, diagnostic
                assert diagnostic['keys'][0]['uri'] == 'enc.key', diagnostic
                resources = diagnostic['resources']
                assert resources and all(r['role'] == 'Media Segment' for r in resources), resources
                if name != 'aes128_mixed_extension_segments':
                    assert resources[0]['uri'] == 'abc000.png' and resources[1]['uri'] == 'abc001.png', resources
                assert decoded_hash(result['files'][0]) == decoded_hash(fixture.directory / 'direct.mp4'), name
                prefix = route.rsplit('/', 1)[0] + '/'
                hits = [r for r in fixture.requests if r['path'].startswith(prefix)]
                manifests = [r for r in hits if r['path'] == route]
                keys = [r for r in hits if r['path'].endswith('enc.key')]
                segments = [r for r in hits if Path(r['path']).name in {r['uri'] for r in resources}]
                assert manifests and all(r['range'] == 'bytes=0-' and r['browser_metadata_match'] for r in manifests), manifests
                assert keys and segments and all(r['range'] is None for r in keys + segments), hits
                if name == 'browser_metadata_opt_in':
                    assert all(r['browser_metadata_match'] for r in keys + segments), hits
                else:
                    assert not any(r['browser_metadata_present'] for r in keys + segments), hits
                if name == 'origin_never_invented':
                    assert not any(r['has_origin'] for r in hits), hits
                else:
                    assert all(r['headers_match'] for r in hits), hits
                semantic_evidence = {'protocol': diagnostic['protocol'], 'playlist_type': diagnostic['playlist_type'],
                                     'encryption': diagnostic['encryption'], 'drm': diagnostic['drm'], 'key_uri': 'enc.key',
                                     'resources': [{'uri': r['uri'], 'role': r['role']} for r in resources],
                                     'manifest_range': 'bytes=0-', 'key_range': None,
                                     'decoded_video_matches': True}
            checks.append({'name': name, 'passed': True, 'file': str(Path(result['files'][0]).relative_to(ROOT)),
                           'media': data, 'progress_events': len(progress)})
            if semantic_evidence:
                checks[-1]['semantic_evidence'] = semantic_evidence
            if name.startswith('observed_range_'):
                diagnostic = result['diagnostic']
                assert diagnostic['protocol'] == 'HLS' and diagnostic['playlist_type'] == 'VOD', diagnostic
                assert diagnostic['classification'] == 'HLS_AES128' and diagnostic['drm'] is False, diagnostic
                assert diagnostic['http_status'] == 206 and diagnostic['inherit_range'] is True, diagnostic
                assert diagnostic['request_range'] == 'bytes=0-' and diagnostic['origin'] == '', diagnostic
                assert Path(urlsplit(diagnostic['keys'][0]['uri']).path).name == 'enc.key', diagnostic
                resources = diagnostic['resources']
                assert all(r['role'] == 'Media Segment' for r in resources), resources
                assert {'abc000.png', 'abc001.png'} <= {Path(urlsplit(r['uri']).path).name for r in resources}, resources
                assert decoded_hash(result['files'][0]) == decoded_hash(fixture.directory / 'direct.mp4'), name
                hits = fixture.requests[start_request:]
                assert any(r['path'].endswith('enc.key') for r in hits), hits
                assert any(r['path'].endswith('abc000.png') for r in hits), hits
                assert all(r['observed_headers_match'] for r in hits), hits
                assert not any(r['has_origin'] or r['has_cookie'] or r['has_authorization'] or r['browser_metadata_present'] for r in hits), hits
                if name == 'observed_range_cross_host':
                    assert {r['host'].split(':')[0] for r in hits} == {'127.0.0.1', 'localhost'}, hits
                if name == 'observed_range_redirect':
                    assert diagnostic['redirects'][0]['status'] == 307 and 'localhost' in diagnostic['final_url'], diagnostic
                checks[-1]['semantic_evidence'] = {'protocol': 'HLS', 'playlist_type': 'VOD', 'encryption': 'AES-128',
                                                 'drm': False, 'key_uri': 'enc.key', 'manifest_http': 206,
                                                 'manifest_range': 'bytes=0-', 'key_range': 'bytes=0-',
                                                 'segment_range': 'bytes=0-', 'all_context_headers_match': True,
                                                 'has_origin': False, 'has_cookie': False, 'has_authorization': False,
                                                 'decoded_video_matches': True,
                                                 'resources': [{'uri': r['uri'], 'role': r['role']} for r in resources]}
            print(f'PASS {name}', flush=True)
        result, logs, _, _ = run_worker(app, DownloadRequest(yt.path, fixture.base + '/observed/index.m3u8',
                                       str(run_directory / 'observed_isolation'), headers=OBSERVED_HEADERS, ffmpeg_path=ff.path))
        assert not result['success'] and result['error_code'] == 'HTTP_403' and result['diagnostic']['drm'] is False, result
        assert result['diagnostic']['resource_role'] in ('Key', 'Media Segment'), result
        assert result['diagnostic']['request_range'] == '' and 'Range' in result['error'], result
        checks.append({'name': 'observed_range_isolation_403_not_drm', 'passed': True})
        print('PASS observed_range_isolation_403_not_drm', flush=True)
        for name, route, expected in (
            ('error_403', '/forbidden.m3u8', '过期'), ('remote_disconnected', '/disconnect.m3u8', 'Copy as cURL'),
            ('unsupported_page', '/page.html', '普通网页'), ('protected_media_refused', '/protected.m3u8', '受保护')):
            result, logs, _, _ = run_worker(app, DownloadRequest(yt.path, fixture.base + route,
                                                                 str(run_directory / name), headers={}, ffmpeg_path=ff.path))
            assert not result['success'] and not result['cancelled'], result
            assert expected in friendly_error(result['error']), result
            if name == 'error_403':
                assert result['error_code'] == 'HTTP_403' and result['diagnostic']['drm'] is None, result
            if name == 'protected_media_refused':
                assert result['error_code'] == 'PROTECTED_MEDIA' and result['diagnostic']['drm'] is True, result
            checks.append({'name': name, 'passed': True})
            print(f'PASS {name}', flush=True)
        for name, route in (('widevine_manifest_refused', '/drm/widevine'), ('playready_manifest_refused', '/drm/playready'),
                            ('pssh_initialization_refused', '/dash/pssh-endpoint')):
            result, logs, _, _ = run_worker(app, DownloadRequest(yt.path, fixture.base + route,
                                                                 str(run_directory / name), headers=FIXTURE_HEADERS, ffmpeg_path=ff.path))
            assert not result['success'] and not result['cancelled'], (name, result)
            assert result['error_code'] == 'PROTECTED_MEDIA' and result['diagnostic']['drm'] is True, (name, result)
            checks.append({'name': name, 'passed': True})
            print(f'PASS {name}', flush=True)
        result, _, _, _ = run_worker(app, DownloadRequest(yt.path, fixture.base + '/slow/index.m3u8',
                                                          str(run_directory / 'cancel'), 'cancel', {}, ff.path), stop_after=4000)
        assert result['cancelled'] and not result['success'], result
        checks.append({'name': 'user_stop_actual_process', 'passed': True})
        assert not any(Path(r['path']).name == 'secret.key' for r in fixture.requests)
        checks.append({'name': 'no_drm_key_requested', 'passed': True})
        report = {'passed': True, 'yt_dlp': yt.version, 'ffmpeg': ff.version, 'checks': checks,
                  'request_count': len(fixture.requests)}
        (directory / 'integration-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'PASS {len(checks)} integration checks', flush=True)
        return 0
    finally:
        fixture.close()


if __name__ == '__main__':
    raise SystemExit(main())
