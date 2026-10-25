"""Source/EXE acceptance using real workers, loopback HTTP and generated media.

Every run owns a new result directory.  No browser profile, remote service, user
download or installed tool is changed.  Failures keep their requests and files.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit


CASES = ('direct-resume', 'direct-ignore-range', 'direct-bad-range',
         'direct-changed', 'direct-416', 'output-exists', 'hls-missing',
         'hls-corrupt', 'dash-audio', 'dash-merge', 'process-recovery', 'mp3',
         'ui-recovery')
_RANGE = re.compile(r'bytes=(\d+)-(\d*)$')
_CUT_BYTES = 192 * 1024
_STAGE_EXIT = 86


class _UITransferWindow:
    """Hold a real HTTP body before EOF until the normal-close test is done."""

    def __init__(self, *, timeout, hold_after=2 * 1024 * 1024, chunk_delay=.1):
        self.timeout = timeout
        self.hold_after = hold_after
        self.chunk_delay = chunk_delay
        self.entered = threading.Event()
        self.released = threading.Event()
        self.timed_out = False

    def release(self):
        self.released.set()

    def evidence(self):
        return {'hold_after_bytes': self.hold_after, 'timeout_seconds': self.timeout,
                'chunk_delay_seconds': self.chunk_delay, 'entered': self.entered.is_set(),
                'released': self.released.is_set(), 'timed_out': self.timed_out}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + '.writing')
    staging.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                       encoding='utf-8')
    staging.replace(path)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recovery-acceptance', type=Path, required=True)
    parser.add_argument('--recovery-case', choices=('all',) + CASES, default='all')
    parser.add_argument('--recovery-timeout', type=float, default=120.0)
    parser.add_argument('--native-smoke', action='store_true')
    parser.add_argument('--recovery-stage', choices=('crash-download', 'resume-download'))
    parser.add_argument('--recovery-url', default='')
    values = list(argv)
    if values and not values[0].startswith('--'):
        values.pop(0)
    args = parser.parse_args(values)
    if not 5 <= args.recovery_timeout <= 600:
        parser.error('--recovery-timeout 必须为 5 到 600 秒。')
    if args.recovery_stage:
        parts = urlsplit(args.recovery_url)
        if (parts.scheme != 'http' or parts.hostname != '127.0.0.1'
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path != '/process-recovery/direct.mp4'):
            parser.error('内部恢复阶段只允许本地验收服务器的固定媒体路径。')
    elif args.recovery_url:
        parser.error('--recovery-url 只能与内部阶段一起使用。')
    return args


def _command(args, *, timeout=60):
    from core.process_tree import NO_WINDOW
    result = subprocess.run([str(arg) for arg in args], shell=False,
                            stdin=subprocess.DEVNULL, capture_output=True,
                            creationflags=NO_WINDOW, timeout=timeout)
    if result.returncode:
        message = result.stderr.decode('utf-8', errors='replace')[-5000:]
        raise RuntimeError(f'{Path(args[0]).name} exit={result.returncode}: {message}')
    return result


def _tools():
    from core.yt_dlp_manager import APP_DIR, detect_ffmpeg, detect_yt_dlp
    suffix = '.exe' if os.name == 'nt' else ''
    # Prefer the toolchain delivered beside the exact source/EXE under test.
    ffmpeg = detect_ffmpeg(str(APP_DIR / 'tools' / ('ffmpeg' + suffix)))
    yt_dlp = detect_yt_dlp(str(APP_DIR / 'tools' / ('yt-dlp' + suffix)))
    if not ffmpeg.found or not yt_dlp.found:
        raise RuntimeError(f'验收需要正式下载工具：{ffmpeg.error}; {yt_dlp.error}')
    probe = Path(ffmpeg.path).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    if not probe.is_file():
        raise RuntimeError('FFmpeg 同目录缺少 ffprobe，不能记录实际媒体证据。')
    return {'ffmpeg': ffmpeg.path, 'yt_dlp': yt_dlp.path, 'ffprobe': str(probe),
            'ffmpeg_version': ffmpeg.version, 'yt_dlp_version': yt_dlp.version}


def _generate_media(root, tools):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    direct = root / 'direct.mp4'
    common = [tools['ffmpeg'], '-nostdin', '-hide_banner', '-loglevel', 'error',
              '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24',
              '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000',
              '-t', '8', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
              '-g', '24', '-keyint_min', '24', '-sc_threshold', '0',
              '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart']
    _command(common + [direct])
    changed = root / 'changed.mp4'
    _command(common + ['-vf', 'hue=h=90', changed])
    if min(direct.stat().st_size, changed.stat().st_size) <= _CUT_BYTES * 2:
        raise RuntimeError('生成媒体太小，不能可靠模拟实际中断。')
    video, audio = root / 'video.mp4', root / 'audio.m4a'
    prefix = [tools['ffmpeg'], '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', direct]
    _command(prefix + ['-an', '-c:v', 'copy', '-movflags', '+faststart', video])
    _command(prefix + ['-vn', '-c:a', 'copy', '-movflags', '+faststart', audio])
    mp3 = root / 'audio.mp3'
    _command(prefix + ['-vn', '-c:a', 'libmp3lame', '-b:a', '96k', mp3])
    hls = root / 'hls'
    hls.mkdir()
    _command(prefix + ['-c', 'copy', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0',
                       '-hls_segment_filename', hls / 'segment%03d.ts', hls / 'index.m3u8'])
    segments = sorted(hls.glob('segment*.ts'))
    if len(segments) < 4:
        raise RuntimeError('HLS 生成分片不足。')
    mpd = root / 'index.mpd'
    mpd.write_text('''<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
 mediaPresentationDuration="PT8S" minBufferTime="PT1S">
 <Period duration="PT8S">
  <AdaptationSet contentType="video" mimeType="video/mp4" codecs="avc1.42c01e">
   <Representation id="video" bandwidth="2500000" width="640" height="360">
    <BaseURL>video.mp4</BaseURL>
   </Representation>
  </AdaptationSet>
  <AdaptationSet contentType="audio" mimeType="audio/mp4" codecs="mp4a.40.2" lang="und">
   <Representation id="audio" bandwidth="96000" audioSamplingRate="48000">
    <BaseURL>audio.m4a</BaseURL>
   </Representation>
  </AdaptationSet>
 </Period>
</MPD>''', encoding='utf-8')
    page = root / 'page.html'
    page.write_text('<!doctype html><meta charset="utf-8"><title>Local recovery fixture</title>'
                    '<p>本地恢复验收，不引用远程资源。</p>', encoding='utf-8')
    return {'direct': direct, 'changed': changed, 'video': video, 'audio': audio,
            'mp3': mp3, 'page': page, 'mpd': mpd, 'hls': hls, 'segments': segments}


class _FixtureServer:
    """Only registered fixture paths are served; every fault is recorded."""

    def __init__(self, assets):
        self.assets = assets
        # Bind segment names to owned fixture paths before accepting requests.
        hls_directory = assets.get('hls')
        self.segments = ({path.name: path for path in Path(hls_directory).glob('segment*.ts')
            if re.fullmatch(r'segment\d{3}\.ts', path.name) and not path.is_symlink()}
            if hls_directory is not None else {})
        self.lock = threading.RLock()
        self.requests = []
        self.cut = set()
        self.changed = set()
        self.blocked = set()
        self.counts = Counter()
        self.faults = Counter()
        self.ui_transfer_window = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def do_HEAD(self):
                owner.serve(self, head=True)

            def do_GET(self):
                owner.serve(self, head=False)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        if self.ui_transfer_window:
            self.ui_transfer_window.release()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @property
    def base(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def url(self, case, name):
        return f'{self.base}/{case}/{name}'

    def snapshot(self, case):
        with self.lock:
            return [dict(item) for item in self.requests if item['case'] == case]

    def hold_ui_transfer(self, **options):
        self.ui_transfer_window = _UITransferWindow(**options)
        return self.ui_transfer_window

    def _asset(self, case, name):
        if name == 'direct.mp4':
            key = 'changed' if case in self.changed else 'direct'
            return self.assets[key], 'video/mp4'
        if name == 'index.mpd':
            return self.assets['mpd'], 'application/dash+xml'
        if name == 'video.mp4':
            return self.assets['video'], 'video/mp4'
        if name == 'audio.m4a':
            return self.assets['audio'], 'audio/mp4'
        if name == 'audio.mp3':
            return self.assets['mp3'], 'audio/mpeg'
        if name == 'page.html':
            return self.assets['page'], 'text/html; charset=utf-8'
        if name == 'index.m3u8':
            return self.assets['hls'] / 'index.m3u8', 'application/vnd.apple.mpegurl'
        path = self.segments.get(name)
        if path is not None and not path.is_symlink() and path.is_file():
            return path, 'video/mp2t'
        return None, ''

    def serve(self, handler, *, head):
        parts = urlsplit(handler.path)
        chunks = parts.path.strip('/').split('/')
        case, name = chunks if len(chunks) == 2 else ('', '')
        if case not in CASES or parts.query:
            handler.send_error(404)
            return
        path, content_type = self._asset(case, name)
        if not path:
            handler.send_error(404)
            return
        requested = handler.headers.get('Range', '')
        match = _RANGE.fullmatch(requested)
        start = int(match[1]) if match else 0
        with self.lock:
            self.counts[(case, name, handler.command)] += 1
            ordinal = self.counts[(case, name, handler.command)]
            blocked = (case, name) in self.blocked
            cutoff = (not head and name == 'direct.mp4' and case.startswith('direct-')
                      and case not in self.cut and ordinal >= 3 and start == 0)
            if cutoff:
                self.cut.add(case)
            fault = ''
            if blocked:
                fault = 'temporary-unavailable'
            elif start and case == 'direct-bad-range':
                fault = 'wrong-content-range'
            elif start and case == 'direct-416' and not self.faults[(case, '416')]:
                fault = 'incomplete-416'
                self.faults[(case, '416')] += 1
            elif start and case == 'direct-ignore-range':
                fault = 'ignored-range'
            elif start and case == 'direct-changed':
                self.changed.add(case)
                path, content_type = self._asset(case, name)
                fault = 'changed-resource'
            record = {'case': case, 'path': parts.path, 'method': handler.command,
                      'range': requested, 'if_range': handler.headers.get('If-Range', ''),
                      'status': 0, 'content_range': '', 'sent_bytes': 0,
                      'fault': 'connection-cut' if cutoff else fault,
                      'at_monotonic': time.monotonic()}
            self.requests.append(record)
        if blocked:
            handler.send_response(503)
            handler.send_header('Retry-After', '0')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            record['status'] = 503
            return
        total = path.stat().st_size
        if fault == 'incomplete-416' or (match and start >= total):
            record.update(status=416, content_range=f'bytes */{total}')
            handler.send_response(416)
            handler.send_header('Content-Range', record['content_range'])
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return
        if fault == 'ignored-range':
            match, start = None, 0
        end = min(int(match[2]) if match and match[2] else total - 1, total - 1)
        if end < start:
            handler.send_error(416)
            record['status'] = 416
            return
        status = 206 if match else 200
        content_range = f'bytes {start + (fault == "wrong-content-range")}-{end}/{total}' if match else ''
        etag = '"changed-v2"' if case in self.changed else '"fixture-v1"'
        record.update(status=status, content_range=content_range, etag=etag,
                      content_length=end - start + 1)
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(end - start + 1))
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('ETag', etag)
        handler.send_header('Last-Modified', 'Sat, 17 Oct 2026 00:00:00 GMT')
        if content_range:
            handler.send_header('Content-Range', content_range)
        handler.end_headers()
        if head:
            return
        remaining = min(end - start + 1, _CUT_BYTES) if cutoff else end - start + 1
        window = (self.ui_transfer_window if case == 'ui-recovery' and name == 'direct.mp4'
                  and ordinal >= 3 and start == 0 and self.ui_transfer_window
                  and not self.ui_transfer_window.entered.is_set() else None)
        try:
            with path.open('rb') as stream:
                stream.seek(start)
                while remaining:
                    block = stream.read(min(32768, remaining))
                    if not block:
                        break
                    handler.wfile.write(block)
                    handler.wfile.flush()
                    record['sent_bytes'] += len(block)
                    remaining -= len(block)
                    if window and remaining and record['sent_bytes'] >= window.hold_after:
                        record['ui_window_offset'] = record['sent_bytes']
                        record['ui_window_entered_at'] = time.monotonic()
                        window.entered.set()
                        if not window.released.wait(window.timeout):
                            window.timed_out = True
                            record['ui_window_timed_out'] = True
                            handler.close_connection = True
                            return
                        record['ui_window_released_at'] = time.monotonic()
                        window = None
                    if window and remaining:
                        time.sleep(window.chunk_delay)
                    elif remaining and case in ('process-recovery', 'ui-recovery'):
                        time.sleep(.04)
            if cutoff:
                handler.close_connection = True
                try:
                    handler.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            record['peer_closed'] = True


def _request(tools, root, url, protocol='Direct', *, attempts=3, extension='mp4'):
    from core.downloader import DownloadRequest
    root = Path(root)
    output = root / 'outputs'
    output.mkdir(parents=True, exist_ok=True)
    kwargs = dict(yt_dlp_path=tools['yt_dlp'], ffmpeg_path=tools['ffmpeg'], url=url,
                  output_directory=str(output), filename='recovered',
                  output_extension=extension,
                  output_path=str(output / ('recovered.' + extension)),
                  temporary_directory=str(output / '.stream-downloads' / 'recovery-task'),
                  download_protocol=protocol,
                  concurrent_fragments=1, segment_max_attempts=1)
    supported = {field.name for field in fields(DownloadRequest)}
    additions = {'recovery_max_attempts': attempts, 'retry_base_delay': .01,
                 'recovery_enabled': True}
    kwargs.update({key: value for key, value in additions.items() if key in supported})
    return DownloadRequest(**kwargs)


def _worker(app, request, timeout):
    from core.downloader import DownloadWorker
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    worker = DownloadWorker(request)
    evidence = {'logs': [], 'statuses': [], 'progress': [], 'result': None,
                'request_options': {name: getattr(request, name, None) for name in (
                    'recovery_max_attempts', 'segment_max_attempts', 'retry_base_delay',
                    'recovery_enabled', 'temporary_directory')}}
    worker.log.connect(lambda line: evidence['logs'].append(str(line)))
    worker.status.connect(lambda status: evidence['statuses'].append(str(status)))
    worker.progress.connect(lambda progress: evidence['progress'].append(dict(progress)))
    worker.completed.connect(lambda result: evidence.update(result=dict(result)))
    worker.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)

    def timed_out():
        evidence['timed_out'] = True
        worker.request_stop()

    timer.timeout.connect(timed_out)
    timer.start(max(1, int(timeout * 1000)))
    worker.start()
    loop.exec()
    timer.stop()
    worker.wait(15000)
    app.processEvents()
    if worker.isRunning():
        raise RuntimeError('下载线程停止超时，不能安全继续下一验收。')
    if evidence.get('timed_out'):
        raise TimeoutError(f'真实 DownloadWorker 超过 {timeout:g} 秒。')
    if evidence['result'] is None:
        raise RuntimeError('DownloadWorker 未发出完成结果。')
    return evidence


def _successful(evidence):
    result = evidence['result']
    if result.get('success') is not True:
        raise AssertionError(f'下载未成功：{result.get("error_code")}: {result.get("error")}')
    return result


def _probe(path, tools, *, expected_sha='', audio_video=True, extension='mp4'):
    from core.output_validator import validate_output
    path = Path(path)
    actual_sha = _sha(path)
    if expected_sha and actual_sha != expected_sha:
        raise AssertionError('最终文件 SHA256 与完整源媒体不同，可能存在重复追加或混合资源。')
    validation = validate_output(path, ffmpeg_path=tools['ffmpeg'], expected_extension=extension)
    output = _command([tools['ffprobe'], '-v', 'error', '-show_streams', '-show_format',
                       '-of', 'json', path]).stdout
    probe = json.loads(output)
    roles = {item.get('codec_type') for item in probe.get('streams', [])}
    if audio_video and not {'audio', 'video'}.issubset(roles):
        raise AssertionError('最终 MP4 缺少真实音频轨或视频轨。')
    if not audio_video and 'audio' not in roles:
        raise AssertionError('最终音频文件没有真实音频轨。')
    return {'path': str(path.resolve()), 'size_bytes': path.stat().st_size,
            'sha256': actual_sha, 'validation': validation, 'ffprobe': probe}


def _nonzero_range(items, *, status=None):
    return [item for item in items if (match := _RANGE.fullmatch(item.get('range', '')))
            and int(match[1]) > 0 and (status is None or item['status'] == status)]


def _safe_failure_or_complete(evidence, request, tools, expected_sha):
    result = evidence['result']
    if result.get('success'):
        paths = result.get('files', [])
        if len(paths) != 1:
            raise AssertionError('成功任务必须确认唯一最终输出。')
        return _probe(paths[0], tools, expected_sha=expected_sha)
    if Path(request.output_path).exists():
        raise AssertionError('不安全响应后任务失败，却提交了最终文件。')
    if not (result.get('error') or result.get('error_code')):
        raise AssertionError('安全停止时没有给出可观察错误原因。')
    return {'safely_stopped': True, 'error_code': result.get('error_code', ''),
            'reason': result.get('error', '')}


def _case_direct(app, case, root, tools, assets, server, timeout, record):
    request = _request(tools, root, server.url(case, 'direct.mp4'))
    run = _worker(app, request, timeout)
    record['runs'].append(run)
    requests = server.snapshot(case)
    if not any(item['fault'] == 'connection-cut' and item['sent_bytes'] > 0 for item in requests):
        raise AssertionError('服务器没有实际中断已传输的媒体响应。')
    if not _nonzero_range(requests):
        raise AssertionError('中断后未观察到非零 HTTP Range 请求。')
    fault = {'direct-ignore-range': 'ignored-range', 'direct-bad-range': 'wrong-content-range',
             'direct-changed': 'changed-resource', 'direct-416': 'incomplete-416'}.get(case)
    if fault and not any(item['fault'] == fault for item in requests):
        raise AssertionError(f'没有实际触发 {fault}。')
    expected = _sha(assets['changed'] if case == 'direct-changed' else assets['direct'])
    if case in ('direct-resume', 'direct-ignore-range'):
        result = _successful(run)
        if case == 'direct-resume' and not _nonzero_range(requests, status=206):
            raise AssertionError('未获得真实非零 206 续传证据。')
        record['outputs'].append(_probe(result['files'][0], tools, expected_sha=expected))
    else:
        record['outputs'].append(_safe_failure_or_complete(run, request, tools, expected))


def _completed_resource_counts(items):
    return Counter(item['path'] for item in items if item['method'] == 'GET'
                   and item['status'] in (200, 206) and not item.get('peer_closed')
                   and item['sent_bytes'] == item.get('content_length') and item['sent_bytes'] > 0)


def _case_hls(app, case, root, tools, assets, server, timeout, record):
    blocked_name = assets['segments'][2].name
    server.blocked.add((case, blocked_name))
    request = _request(tools, root, server.url(case, 'index.m3u8'), 'HLS', attempts=1)
    first = _worker(app, request, timeout)
    record['runs'].append(first)
    if first['result'].get('success'):
        raise AssertionError('必需 HLS 分片 503 时错误地报告成功。')
    if Path(request.output_path).exists():
        raise AssertionError('缺失分片时提交了最终文件。')
    first_requests = server.snapshot(case)
    if not any(item['path'].endswith('/' + blocked_name) and item['status'] == 503
               for item in first_requests):
        raise AssertionError('HLS 首轮未到达预设缺片 503，属于其他运行失败。')
    before = _completed_resource_counts(first_requests)
    completed = {path: count for path, count in before.items() if path.endswith('.ts')}
    if not completed:
        raise AssertionError('首次失败没有实际完成任何可复用 HLS 分片。')
    corrupted_name = None
    if case == 'hls-corrupt':
        segment = assets['segments'][0]
        expected = _sha(segment)
        cache = Path(request.temporary_directory) / 'cache'
        payloads = [path for path in cache.rglob('*') if path.is_file()
                    and path.stat().st_size == segment.stat().st_size and _sha(path) == expected]
        if not payloads:
            raise AssertionError('未找到与真实 HLS 分片一致的 SHA256 磁盘缓存。')
        victim = payloads[0]
        data = bytearray(victim.read_bytes())
        data[len(data) // 2] ^= 0x7F
        victim.write_bytes(data)
        record['corrupted_cache'] = {'path': str(victim), 'before_sha256': expected,
                                      'after_sha256': _sha(victim)}
        corrupted_name = segment.name
        # Also force the downloader to revalidate its partial output.  These are
        # exclusively this case's generated files, never user downloads.
        partials = [path for directory in ('staging', 'transfer')
                    for path in (Path(request.temporary_directory) / directory).rglob('*')
                    if path.is_file() and '.part' in path.name]
        record['corrupted_partials'] = []
        for partial in partials:
            if partial.stat().st_size:
                with partial.open('r+b') as stream:
                    stream.seek(partial.stat().st_size // 2)
                    original = stream.read(1)
                    stream.seek(-1, 1)
                    stream.write(bytes([original[0] ^ 0x55]))
                record['corrupted_partials'].append(str(partial))
    server.blocked.discard((case, blocked_name))
    second = _worker(app, request, timeout)
    record['runs'].append(second)
    result = _successful(second)
    record['outputs'].append(_probe(result['files'][0], tools))
    after = _completed_resource_counts(server.snapshot(case))
    reused = {path: count for path, count in completed.items()
              if not path.endswith('/' + str(corrupted_name))}
    if not reused:
        raise AssertionError('没有可核对的未损坏缓存分片。')
    if any(after[path] != count for path, count in reused.items()):
        raise AssertionError('恢复时重复向服务器完整请求了已校验的 HLS 分片。')
    if corrupted_name:
        path = f'/{case}/{corrupted_name}'
        if after[path] <= before[path]:
            raise AssertionError('损坏的缓存分片没有重新请求。')
    record['reused_resources'] = sorted(reused)
    record['resource_counts_before'] = dict(before)
    record['resource_counts_after'] = dict(after)


def _case_dash(app, case, root, tools, assets, server, timeout, record):
    request = _request(tools, root, server.url(case, 'index.mpd'), 'DASH', attempts=1)
    if case == 'dash-audio':
        server.blocked.add((case, 'audio.m4a'))
    else:
        # A private copy of ffprobe can answer -version but cannot merge.  This
        # deliberately breaks only this task's tool path, keeping real tools
        # and completed independent media tracks intact for the retry.
        broken = Path(root) / 'merge-failure-tool'
        broken.mkdir()
        replacement = broken / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
        shutil.copy2(tools['ffprobe'], replacement)
        shutil.copy2(tools['ffprobe'], broken / Path(tools['ffprobe']).name)
        request.ffmpeg_path = str(replacement)
    first = _worker(app, request, timeout)
    record['runs'].append(first)
    if first['result'].get('success'):
        raise AssertionError('音频未完成或合并工具失败时错误地报告成功。')
    if Path(request.output_path).exists():
        raise AssertionError('双轨未完成合并校验时提交了最终文件。')
    first_requests = server.snapshot(case)
    before = _completed_resource_counts(first_requests)
    video_path, audio_path = f'/{case}/video.mp4', f'/{case}/audio.m4a'
    if not before[video_path]:
        raise AssertionError('失败前没有实际完成视频轨，不能证明独立恢复。')
    if case == 'dash-audio' and not any(
            item['path'] == audio_path and item['status'] == 503 for item in first_requests):
        raise AssertionError('DASH 首轮未到达预设音频 503，属于其他运行失败。')
    if case == 'dash-merge':
        if not before[audio_path]:
            raise AssertionError('合并失败测试未完成音频轨。')
        if not any('merg' in line.lower() or '合并' in line for line in first['logs'] + first['statuses']):
            raise AssertionError('首次失败没有到达真实合并阶段。')
    request.ffmpeg_path = tools['ffmpeg']
    server.blocked.discard((case, 'audio.m4a'))
    second = _worker(app, request, timeout)
    record['runs'].append(second)
    result = _successful(second)
    record['outputs'].append(_probe(result['files'][0], tools))
    all_requests = server.snapshot(case)
    after = _completed_resource_counts(all_requests)
    media_before = Counter(item['path'] for item in first_requests if item['method'] == 'GET'
                           and item['path'] in (video_path, audio_path))
    media_after = Counter(item['path'] for item in all_requests if item['method'] == 'GET'
                          and item['path'] in (video_path, audio_path))
    record['media_request_counts_before'] = dict(media_before)
    record['media_request_counts_after'] = dict(media_after)
    if after[video_path] != before[video_path]:
        raise AssertionError('重试音频/合并时重新完整请求了已完成视频轨。')
    if case == 'dash-merge' and after[audio_path] != before[audio_path]:
        raise AssertionError('合并重试时重新完整请求了已完成音频轨。')
    if media_after[video_path] != media_before[video_path]:
        raise AssertionError('恢复已完成视频轨时增加了远端媒体请求。')
    if case == 'dash-merge' and media_after[audio_path] != media_before[audio_path]:
        raise AssertionError('合并重试时增加了远端音频媒体请求。')
    record['resource_counts_before'] = dict(before)
    record['resource_counts_after'] = dict(after)


def _case_output_exists(app, case, root, tools, assets, server, timeout, record):
    request = _request(tools, root, server.url(case, 'direct.mp4'))
    destination = Path(request.output_path)
    shutil.copy2(assets['direct'], destination)
    original = _sha(destination)
    run = _worker(app, request, timeout)
    record['runs'].append(run)
    if run['result'].get('success') or _sha(destination) != original:
        raise AssertionError('已存在的最终文件未被安全保护。')
    if run['result'].get('error_code') != 'OUTPUT_EXISTS':
        raise AssertionError('最终文件冲突没有给出 OUTPUT_EXISTS。')
    record['outputs'].append(_probe(destination, tools, expected_sha=original))


def _case_mp3(app, case, root, tools, assets, server, timeout, record):
    request = _request(tools, root, server.url(case, 'audio.mp3'), extension='mp3')
    run = _worker(app, request, timeout)
    record['runs'].append(run)
    result = _successful(run)
    record['outputs'].append(_probe(result['files'][0], tools, extension='mp3',
                                     audio_video=False, expected_sha=_sha(assets['mp3'])))
    if not any(item['method'] == 'GET' and item['status'] == 200 and item['sent_bytes'] > 0
               for item in server.snapshot(case)):
        raise AssertionError('MP3 用例没有实际本地 HTTP 下载证据。')


def _wait_for(app, predicate, timeout, reason):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise TimeoutError(reason)


def _pump_for(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)


def _wait_for_ui_checkpoint(app, job, window, timeout, record):
    """Observe cheap metadata while the fixture keeps the live download open."""
    deadline = time.monotonic() + timeout
    last_poll = 0.0
    record['checkpoint_observations'] = observations = []
    while time.monotonic() < deadline:
        before_events = time.monotonic()
        app.processEvents()
        now = time.monotonic()
        if now - last_poll >= .1:
            last_poll = now
            partials = _verified_partials(job.request.temporary_directory, include_hashes=False)
            marker = Path(job.request.temporary_directory) / 'workspace.json'
            try:
                marker_mtime = marker.stat().st_mtime_ns
            except OSError:
                marker_mtime = None
            downloaded = job.progress.get('downloaded_bytes', 0)
            observations.append({'at_monotonic': now, 'downloaded_bytes': downloaded,
                                 'state': job.state, 'worker_active': job.worker is not None,
                                 'process_events_seconds': now - before_events,
                                 'marker_mtime_ns': marker_mtime, 'partials': partials,
                                 'http_window_entered': window.entered.is_set()})
            if window.timed_out:
                raise TimeoutError('UI 验收的 HTTP 观察窗口已超时。')
            if window.entered.is_set() and downloaded >= 1536 * 1024 and partials:
                # The sender remains gated; hash once after observing a real checkpoint.
                record['normal_close_partials'] = _verified_partials(job.request.temporary_directory)
                if record['normal_close_partials']:
                    return
            if job.worker is None or job.state != 'running':
                raise AssertionError('UI 下载在观察到已校验断点之前已结束。')
        time.sleep(.01)
    raise TimeoutError('真实 UI 下载未形成非零已校验断点。')


def _ui_snapshot(window):
    return {'visible': window.isVisible(), 'title': window.windowTitle(),
            'config_path': str(window.config_store.path),
            'browser_profile': str(window.browser_profile_directory),
            'queue_paused': window.queue.paused, 'active_count': window.queue.active_count,
            'jobs': [{'id': job.id, 'name': job.name, 'state': job.state,
                      'workspace': job.request.temporary_directory,
                      'progress': dict(job.progress), 'worker_active': job.worker is not None}
                     for job in window.queue.jobs],
            'status_label': window.status_label.text(),
            'resume_button': {'text': window.resume_job_button.text(),
                              'enabled': window.resume_job_button.isEnabled()},
            'hidden_refresh_started': bool(window._discovery_dialog or window._refresh_dialogs
                                           or window._auto_refresh_dialog
                                           or window._auto_refresh_job_id is not None
                                           or window._auto_refresh_ids
                                           or window.download_manager._refresh_attempted)}


def _case_ui(app, case, root, tools, assets, server, timeout, record):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox
    from ui.main_window import MainWindow
    from utils.config import ConfigStore

    root = Path(root).resolve()
    config_path = root / 'private-config' / 'config.json'
    store = ConfigStore(config_path)
    if not store.save({'output_directory': str(root / 'outputs'),
                       'yt_dlp_path': tools['yt_dlp'], 'max_concurrent_videos': 1,
                       'concurrent_fragments': 1, 'segment_max_attempts': 1}):
        raise AssertionError(store.error)
    windows = []
    original_path = os.environ.get('PATH')
    original_quit = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    os.environ['PATH'] = str(Path(tools['ffmpeg']).parent) + os.pathsep + (original_path or '')
    record['platform'] = app.platformName()
    record['config_path'] = str(config_path)
    record['screenshots'] = []
    record['window_snapshots'] = []
    transfer_window = server.hold_ui_transfer(timeout=timeout + 30)

    def open_window():
        window = MainWindow(ConfigStore(config_path))
        windows.append(window)
        window.showMinimized() if app.platformName() == 'windows' else window.show()
        _wait_for(app, lambda: window.detector is None and window.yt_info.found
                  and window.ff_info.found, 30, '真实 MainWindow 工具检测未完成。')
        if not window.browser_profile_directory.resolve().is_relative_to(root):
            raise AssertionError('UI 验收没有隔离浏览器配置目录。')
        return window

    def screenshot(window, name):
        path = root / name
        if window.grab().save(str(path)):
            record['screenshots'].append(str(path))

    try:
        first = open_window()
        primary = first.queue.add(_request(tools, root, server.url(case, 'direct.mp4')))
        waiting_request = _request(tools, root, server.url(case, 'direct.mp4'))
        waiting_request.filename = 'context-required'
        waiting_request.output_path = str(Path(waiting_request.output_directory) / 'context-required.mp4')
        waiting = first.queue.add(waiting_request)
        # Construct the saved waiting-context state without emitting changed:
        # this exercises restoration's gating, not a live refresh action.
        waiting.state, waiting.status = 'waiting_context', '本地验收：等待用户重新分析'
        waiting.request.media_metadata = {'resolver_verified': True,
                                          'resolver_id': 'acceptance-local',
                                          'video_id': 'waiting-context-fixture',
                                          'page_url': server.url(case, 'page.html')}
        if not first.queue.save_tasks():
            raise AssertionError(first.queue.persistence_error)
        first.select_job(primary.id)
        first.input_tabs.setCurrentIndex(1)
        first.start_queue_button.click()
        _wait_for_ui_checkpoint(app, primary, transfer_window, timeout, record)
        if primary.worker is None or primary.state != 'running':
            raise AssertionError('正常关闭测试未处于实际正在下载阶段。')
        record['window_snapshots'].append({'phase': 'before-normal-close', **_ui_snapshot(first)})
        screenshot(first, 'ui-downloading.png')
        confirmation = QTimer()
        confirmation.setInterval(10)
        record['normal_close_confirmation_clicked'] = False

        def accept_own_close_dialog():
            for widget in app.topLevelWidgets():
                if (isinstance(widget, QMessageBox) and widget.parentWidget() is first
                        and widget.windowTitle() == '下载任务仍在运行'):
                    button = widget.button(QMessageBox.StandardButton.Yes)
                    if button is not None:
                        record['normal_close_confirmation_clicked'] = True
                        button.click()
                        confirmation.stop()

        confirmation.timeout.connect(accept_own_close_dialog)
        confirmation.start()
        try:
            first.close()
            _wait_for(app, lambda: not first.isVisible() and first.queue.active_count == 0,
                      30, '真实 MainWindow 正常关闭没有暂停并保存任务。')
        finally:
            confirmation.stop()
        if not record['normal_close_confirmation_clicked']:
            raise AssertionError('没有通过真实关闭确认按钮保存活动任务。')
        if Path(primary.request.output_path).exists() or primary.state != 'paused':
            raise AssertionError('正常关闭未保留暂停状态，或提交了未完成文件。')
        transfer_window.release()
        record['window_snapshots'].append({'phase': 'normally-closed', **_ui_snapshot(first)})
        record['saved_tasks_sha256'] = _sha(config_path.with_name('tasks.json'))
        before_reopen = len(server.snapshot(case))
        second = open_window()
        _pump_for(app, .75)
        restored = second.queue.get(primary.id)
        restored_waiting = second.queue.get(waiting.id)
        if (not restored or not restored_waiting or restored.state != 'paused'
                or restored_waiting.state != 'waiting_context'):
            raise AssertionError('重开真实窗口未正确恢复暂停任务和等待重新分析任务。')
        if restored.request.temporary_directory != primary.request.temporary_directory:
            raise AssertionError('真实 UI 恢复没有保留原任务工作目录。')
        snapshot = _ui_snapshot(second)
        record['window_snapshots'].append({'phase': 'restored-before-user-action', **snapshot})
        record['requests_before_reopen'] = before_reopen
        record['requests_before_resume_click'] = len(server.snapshot(case))
        if (second.queue.active_count or any(job.worker is not None for job in second.queue.jobs)
                or not second.queue.paused or snapshot['hidden_refresh_started']
                or len(server.snapshot(case)) != before_reopen):
            raise AssertionError('窗口恢复时自动启动下载或隐藏媒体解析。')
        second.input_tabs.setCurrentIndex(1)
        second.select_job(restored.id)
        if not second.resume_job_button.isEnabled():
            raise AssertionError('真实 UI 的既有继续按钮不可用。')
        screenshot(second, 'ui-restored.png')
        record['resume_button_clicked'] = second.resume_job_button.text()
        second.resume_job_button.click()
        _wait_for(app, lambda: restored.worker is None and second.queue.active_count == 0
                  and restored.state in ('completed', 'failed', 'waiting_context', 'cancelled'),
                  timeout, '点击真实继续按钮后未在期限内完成。')
        if restored.state != 'completed' or not (restored.result or {}).get('success'):
            raise AssertionError(f'UI 继续任务失败：{restored.result}')
        if not _nonzero_range(server.snapshot(case)[before_reopen:], status=206):
            raise AssertionError('UI 正常关闭后继续没有发出非零 206 续传请求。')
        if restored_waiting.worker is not None or restored_waiting.state != 'waiting_context':
            raise AssertionError('手动继续其他任务时错误启动了等待重新分析的任务。')
        _wait_for(app, lambda: not second.thumbnails.busy, 30, '真实缩略图工作未完成。')
        record['thumbnail'] = second.thumbnails.results.get(restored.id)
        record['ui_completed_logs'] = list(restored.logs)
        record['window_snapshots'].append({'phase': 'completed', **_ui_snapshot(second)})
        screenshot(second, 'ui-completed.png')
        record['outputs'].append(_probe(restored.result['files'][0], tools,
                                         expected_sha=_sha(assets['direct'])))
        second.close()
        _wait_for(app, lambda: not second.isVisible(), 30, '完成后的窗口未正常关闭。')
    finally:
        transfer_window.release()
        record['http_transfer_window'] = transfer_window.evidence()
        for window in windows:
            try:
                window.queue.shutdown()
                _wait_for(app, lambda current=window: current.queue.active_count == 0,
                          30, '清理独立 UI 验收任务超时。')
                window.close()
                _wait_for(app, lambda current=window: not current.isVisible(),
                          30, '关闭独立 UI 验收窗口超时。')
            except Exception as error:
                record.setdefault('cleanup_errors', []).append(str(error))
        app.setQuitOnLastWindowClosed(original_quit)
        if original_path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = original_path


def _entry_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / 'main.py')]


def _read_live_json(path):
    """Observe atomic checkpoints without denying Windows delete/rename sharing."""
    path = Path(path)
    if os.name != 'nt':
        return json.loads(path.read_text(encoding='utf-8'))
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL
    handle = create(str(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        close(handle)
        raise
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        content = stream.read()
    # Parsing and subsequent observation must not extend the OS handle lifetime.
    return json.loads(content)


def _verified_partials(workspace, *, include_hashes=True):
    """Crash only after the real worker persisted a nonzero verified prefix."""
    workspace = Path(workspace).resolve()
    try:
        state = _read_live_json(workspace / 'workspace.json')
    except (OSError, ValueError):
        return []
    result = []
    for relative, entry in state.get('files', {}).items():
        path = (workspace / relative).resolve()
        size = entry.get('size', 0)
        if (path.is_relative_to(workspace) and path.suffix == '.part' and path.is_file()
                and type(size) is int and size > 0 and entry.get('blocks')
                and path.stat().st_size >= size):
            item = {'path': str(path), 'size': path.stat().st_size, 'verified_bytes': size}
            if include_hashes:
                item['sha256'] = _sha(path)
            result.append(item)
    return result


def _child(command, app, timeout, log_path):
    from core.process_tree import NO_WINDOW, ProcessTree
    with Path(log_path).open('wb') as output:
        process = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=NO_WINDOW, start_new_session=os.name != 'nt',
                                   env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
        tree = ProcessTree(process)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                app.processEvents()
                if time.monotonic() >= deadline:
                    tree.terminate()
                    raise TimeoutError('独立 EXE/源码子进程验收超时。')
                time.sleep(.02)
            return process.wait()
        finally:
            if process.poll() is None:
                tree.terminate()
            tree.close()


def _process_stage(app, args, tools, identity):
    from core.download_queue import DownloadQueue
    from core.task_store import TaskStore
    root = args.recovery_acceptance.resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root / 'tasks.json')
    queue = DownloadQueue(max_concurrent=1, task_store=store)
    if args.recovery_stage == 'crash-download':
        job = queue.add(_request(tools, root, args.recovery_url))
    else:
        jobs = queue.restore_tasks()
        if len(jobs) != 1:
            raise AssertionError('独立进程未恢复唯一的持久化任务。')
        job = jobs[0]
        if job.state != 'paused':
            raise AssertionError(f'原运行任务没有恢复为安全暂停状态：{job.state}')
        queue.resume_job(job.id)
    result = {}
    queue.completed.connect(lambda job_id, value: result.update(value) if job_id == job.id else None)
    queue.start()
    deadline = time.monotonic() + args.recovery_timeout
    last_checkpoint_poll = 0.0
    while time.monotonic() < deadline:
        app.processEvents()
        if args.recovery_stage == 'crash-download':
            partials = []
            if (job.progress.get('downloaded_bytes', 0) >= 1536 * 1024
                    and time.monotonic() - last_checkpoint_poll >= .1):
                last_checkpoint_poll = time.monotonic()
                partials = _verified_partials(job.request.temporary_directory)
            if partials:
                if not queue.save_tasks():
                    raise AssertionError(f'任务无法实际持久化：{queue.persistence_error}')
                _write_json(root / 'crash-stage.json', {
                    'build': identity, 'job_id': job.id,
                    'workspace': job.request.temporary_directory,
                    'output_path': job.request.output_path,
                    'partials': partials,
                    'state_before_exit': job.state, 'exit_code': _STAGE_EXIT})
                # Intentional process crash; does not call graceful shutdown or
                # alter the queue state to pretend a clean pause was restored.
                os._exit(_STAGE_EXIT)
            if result and queue.active_count == 0:
                raise AssertionError(f'异常退出前下载已结束，未形成可恢复部分：{result}')
        elif result and queue.active_count == 0:
            queue.save_tasks()
            _write_json(root / 'resume-stage.json', {
                'build': identity, 'job_id': job.id, 'state': job.state,
                'workspace': job.request.temporary_directory,
                'result': result, 'progress': job.progress,
                'logs': list(job.logs)})
            return 0 if result.get('success') else 1
        time.sleep(.01)
    queue.shutdown()
    drain_deadline = time.monotonic() + 30
    while queue.active_count and time.monotonic() < drain_deadline:
        app.processEvents()
        time.sleep(.01)
    raise TimeoutError('独立进程没有在期限内留下有效部分文件或完成恢复。')


def _case_process(app, case, root, tools, assets, server, timeout, record):
    common = _entry_command() + ['--recovery-acceptance', str(Path(root).resolve()),
                                '--recovery-case', case, '--recovery-timeout', str(timeout),
                                '--recovery-url', server.url(case, 'direct.mp4')]
    exit_code = _child(common + ['--recovery-stage', 'crash-download'], app,
                       timeout + 30, Path(root) / 'crash-process.log')
    record['crash_exit_code'] = exit_code
    if exit_code != _STAGE_EXIT:
        raise AssertionError(f'首个真实进程未按异常退出阶段结束：{exit_code}')
    crash = json.loads((Path(root) / 'crash-stage.json').read_text(encoding='utf-8'))
    record['crash_stage'] = crash
    if not crash['partials'] or Path(crash['output_path']).exists():
        raise AssertionError('异常退出时没有真实部分文件，或已错误提交最终文件。')
    before = len(server.snapshot(case))
    exit_code = _child(common + ['--recovery-stage', 'resume-download'], app,
                       timeout + 30, Path(root) / 'resume-process.log')
    record['resume_exit_code'] = exit_code
    resume_path = Path(root) / 'resume-stage.json'
    if resume_path.exists():
        record['resume_stage'] = json.loads(resume_path.read_text(encoding='utf-8'))
    if exit_code:
        raise AssertionError(f'第二个真实进程恢复失败：{exit_code}')
    resume = record['resume_stage']
    if (resume['job_id'], resume['workspace']) != (crash['job_id'], crash['workspace']):
        raise AssertionError('恢复进程没有保留同一任务 ID / 工作目录。')
    if resume['build']['build_id'] != crash['build']['build_id']:
        raise AssertionError('两个验收进程并非当前同一构建。')
    if not _nonzero_range(server.snapshot(case)[before:], status=206):
        raise AssertionError('跨进程恢复未观察到真实非零 206 Range。')
    if resume['state'] != 'completed' or resume['result'].get('success') is not True:
        raise AssertionError('队列没有将校验成功的恢复任务标记为 completed。')
    record['outputs'].append(_probe(resume['result']['files'][0], tools,
                                     expected_sha=_sha(assets['direct'])))


def run_recovery_acceptance(app, argv):
    """Return a process exit code; results.json is the authoritative evidence."""
    args = parse_arguments(argv)
    from utils.build_info import runtime_identity
    identity = runtime_identity()
    root = args.recovery_acceptance.expanduser().resolve()
    if args.recovery_stage:
        try:
            return _process_stage(app, args, _tools(), identity)
        except Exception as error:
            _write_json(root / (args.recovery_stage + '-error.json'), {
                'build': identity, 'error': str(error), 'traceback': traceback.format_exc()})
            return 1
    # Never reuse an old acceptance directory or overwrite prior evidence.
    root.mkdir(parents=True, exist_ok=False)
    report = {'schema_version': 1, 'build': identity, 'build_id': identity['build_id'],
              'started_at_unix': time.time(), 'acceptance_complete': False,
              'cases': [], 'scope': 'generated local media; real DownloadWorker/queue and tools',
              'remote_websites_tested': False}
    _write_json(root / 'results.json', report)
    try:
        tools = _tools()
        report['tools'] = tools
        assets = _generate_media(root / 'fixtures', tools)
        report['fixtures'] = {key: {'path': str(assets[key]), 'sha256': _sha(assets[key]),
                                     'size_bytes': assets[key].stat().st_size}
                              for key in ('direct', 'changed', 'video', 'audio', 'mp3', 'mpd', 'page')}
        selected = CASES if args.recovery_case == 'all' else (args.recovery_case,)
        with _FixtureServer(assets) as server:
            for case in selected:
                case_root = root / case
                case_root.mkdir()
                record = {'name': case, 'build_id': identity['build_id'], 'passed': False,
                          'runs': [], 'outputs': [], 'started_at_unix': time.time()}
                report['cases'].append(record)
                try:
                    function = (_case_direct if case.startswith('direct-') else
                                _case_hls if case.startswith('hls-') else
                                _case_dash if case.startswith('dash-') else
                                _case_process if case == 'process-recovery' else
                                _case_mp3 if case == 'mp3' else
                                _case_ui if case == 'ui-recovery' else _case_output_exists)
                    function(app, case, case_root, tools, assets, server,
                             args.recovery_timeout, record)
                    record['passed'] = True
                except Exception as error:
                    record['error'] = str(error)
                    record['traceback'] = traceback.format_exc()
                finally:
                    record['requests'] = server.snapshot(case)
                    record['request_count'] = len(record['requests'])
                    record['finished_at_unix'] = time.time()
                    _write_json(case_root / 'result.json', record)
                    _write_json(root / 'results.json', report)
        report['acceptance_complete'] = bool(report['cases']) and all(
            item['passed'] for item in report['cases'])
        report['full_local_suite_complete'] = (len(report['cases']) == len(CASES)
                                                and report['acceptance_complete'])
    except Exception as error:
        report['fatal_error'] = str(error)
        report['traceback'] = traceback.format_exc()
    finally:
        report['finished_at_unix'] = time.time()
        _write_json(root / 'results.json', report)
    return 0 if report['acceptance_complete'] else 1
