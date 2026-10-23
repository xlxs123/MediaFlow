"""QThread 中运行 yt-dlp 参数数组，并消费结构化进度输出。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
from urllib.parse import urlsplit
import ipaddress

from PySide6.QtCore import QThread, Signal
from core.media_guard import check_media, validate_url, MediaCheckCancelled
from core.errors import MediaCheckError, FATAL_TRANSFER_ERRORS
from core.request_context import RequestContext
from core.media_bridge import MediaBridge
from core.process_tree import ProcessTree, NO_WINDOW
from utils.security import normalize_headers, Redactor, redact_data

PROGRESS_MARKER = '__M3U8_PROGRESS__'
FILE_MARKER = '__M3U8_FILE__'
DEFAULT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) M3U8Downloader/1.0'
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_FILENAME = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


@dataclass
class DownloadRequest:
    yt_dlp_path: str
    url: str
    output_directory: str
    filename: str = ''
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    ffmpeg_path: str | None = None
    method: str = 'GET'
    body: bytes | None = field(default=None, repr=False)
    inherit_browser_metadata: bool = False
    inherit_range: bool = False
    concurrent_fragments: int = 4
    context_snapshot: RequestContext | None = field(default=None, repr=False)
    segment_max_attempts: int = 3
    media_metadata: dict = field(default_factory=dict, repr=False)
    resume_identity: dict | None = field(default=None, repr=False)
    download_protocol: str = ''
    output_extension: str = ''
    output_path: str = ''
    temporary_directory: str = ''
    format_selector: str = ''
    concurrency: object = field(default=None, repr=False, compare=False)
    transport_url: str = field(default='', repr=False)
    manifest_url: str = field(default='', repr=False)
    manifest_text: str = field(default='', repr=False)
    manifest_origin: str = ''
    recovery_max_attempts: int = 3
    retry_base_delay: float = 2.0
    recovery_enabled: bool = True
    ffprobe_path: str = ''
    organization: dict = field(default_factory=dict)
    final_output_directory: str = ''
    collection_directory: str = ''
    workspace_output_directory: str = ''

    def to_context(self):
        headers = normalize_headers(self.headers)
        headers.setdefault('user-agent', DEFAULT_USER_AGENT)
        if self.context_snapshot and self.context_snapshot.url == self.url:
            return replace(self.context_snapshot, method=self.method, headers=headers, body=self.body,
                           inherit_browser_metadata=self.inherit_browser_metadata,
                           inherit_range=self.inherit_range)
        return RequestContext(self.url, self.method, headers, self.body,
                              inherit_browser_metadata=self.inherit_browser_metadata,
                              inherit_range=self.inherit_range)


def validate_filename(filename: str) -> str:
    name = filename.strip()
    if name.lower().endswith('.mp4'):
        name = name[:-4]
    if name and (_INVALID_FILENAME.search(name) or _RESERVED_FILENAME.match(name)
                 or name.endswith(('.', ' ')) or len(name) > 180 or name in ('.', '..')):
        raise ValueError('文件名不能包含 \\ / : * ? " < > |，不能使用 CON、NUL 等 Windows 保留名称。')
    return name


def build_arguments(request: DownloadRequest, *, require_tools=True) -> list[str]:
    request.to_context()
    if request.download_protocol not in ('', 'HLS', 'DASH', 'Direct'):
        raise ValueError('不支持的下载协议。')
    if not isinstance(request.manifest_text, str):
        raise ValueError('解码清单必须是文本。')
    for manifest_address in (request.transport_url, request.manifest_url):
        if manifest_address:
            validate_url(manifest_address)
    if type(request.concurrent_fragments) is not int or not 1 <= request.concurrent_fragments <= 16:
        raise ValueError('分片并发数必须是 1 到 16 的整数。')
    if type(request.segment_max_attempts) is not int or not 1 <= request.segment_max_attempts <= 10:
        raise ValueError('每个分片的总尝试次数必须是 1 到 10 的整数。')
    if type(request.recovery_max_attempts) is not int or not 1 <= request.recovery_max_attempts <= 10:
        raise ValueError('任务恢复总尝试次数必须是 1 到 10 的整数。')
    if type(request.retry_base_delay) not in (int, float) or not 0 <= request.retry_base_delay <= 60:
        raise ValueError('恢复等待基数必须是 0 到 60 秒。')
    if require_tools and not request.yt_dlp_path:
        raise ValueError('未检测到 yt-dlp，请选择 yt-dlp.exe 或先安装 yt-dlp。')
    if not request.output_directory.strip():
        raise ValueError('请选择输出目录。')
    name = validate_filename(request.filename)
    if request.output_extension and request.output_extension not in ('mp4', 'webm', 'mkv', 'm4a', 'mp3', 'opus', 'ogg', 'wav', 'flac', 'aac'):
        raise ValueError('不支持的输出容器。')
    if request.output_path:
        from core.output_organization import final_directory
        output = Path(request.output_path).resolve()
        if output.parent != final_directory(request) or output.name != name + '.' + request.output_extension:
            raise ValueError('已分配的输出路径与文件名或容器不匹配。')
    headers = normalize_headers(request.headers)
    headers.setdefault('user-agent', DEFAULT_USER_AGENT)
    args = [request.yt_dlp_path, '--ignore-config', '--no-plugin-dirs', '--no-playlist', '--force-generic-extractor',
            '--no-allow-unplayable-formats', '--newline', '--no-colors', '--encoding', 'utf-8',
            '--progress', '--progress-delta', '0.2',
            '--progress-template', 'download:' + PROGRESS_MARKER + '%(progress)j',
            '--print', 'after_move:' + FILE_MARKER + '%(filepath)j', '--no-simulate', '--no-quiet',
            '--retries', str(request.segment_max_attempts - 1),
            '--fragment-retries', str(request.segment_max_attempts - 1),
            '--retry-sleep', 'fragment:exp=1:3:3', '--retry-sleep', 'http:exp=1:3:3', '--continue',
            '--abort-on-unavailable-fragments', '--no-overwrites',
            '--concurrent-fragments', str(request.concurrent_fragments), '-P', request.output_directory]
    if name:
        args += ['-o', name.replace('%', '%%') + '.%(ext)s']
    if request.temporary_directory:
        args += ['-P', 'temp:' + request.temporary_directory]
    if request.recovery_enabled:
        args += ['--keep-video']
    if request.format_selector:
        args += ['-f', request.format_selector]
    if request.ffmpeg_path:
        container = request.output_extension or 'mp4'
        args += ['--ffmpeg-location', request.ffmpeg_path, '--remux-video', container]
        if container in ('mp4', 'webm', 'mkv'):
            args += ['--merge-output-format', container]
    # 本机媒体直接连接；requests 与 Windows 的系统代理绕过规则可能不同。
    host = urlsplit(request.url).hostname
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == 'localhost'
    if is_loopback:
        args += ['--proxy', '']
    for key, value in headers.items():
        args += ['--add-headers', f'{key}:{value}']
    return args + ['--', request.url]


def resolve_required_tools(request, names, cancel_event):
    """Only called by DownloadWorker at the operation which needs these tools."""
    from core.tool_manager import get_tool_manager
    for name in names:
        field = name.replace('-', '_') + '_path'
        info = get_tool_manager().detect(name, getattr(request, field) or '', mode='custom',
            sibling=(request.ffmpeg_path or '') if name == 'ffprobe' else '',
            cancel_event=cancel_event, refresh=False)
        if cancel_event.is_set():
            raise MediaCheckCancelled('工具检查已取消。')
        if not info.found:
            raise MediaCheckError(f'当前操作需要 {name}：{info.error}', 'TOOL_UNAVAILABLE',
                                  {'tool': name, 'tool_status': info.status})
        setattr(request, field, info.path)


def friendly_error(text: str) -> str:
    lower = text.lower()
    if 'mediacheckerror:' in lower or 'protectedmediaerror:' in lower:
        return text.split(':', 1)[1].strip()
    if '403' in lower or 'forbidden' in lower:
        return ('视频链接可能已经过期，或者 Origin / Referer / User-Agent 等请求头不正确。'
                '也请检查 Cookie / Authorization；HTTP 403 不代表 DRM，请重新复制最新 cURL。')
    # A system name in a path/title is not protection evidence. Only explicit
    # structured errors or a downloader's anchored DRM rejection are accepted.
    if re.search(r'(?im)^(?:ERROR:\s*)?(?:PROTECTED_MEDIA:|DRM_PROTECTED:|'
                 r'This (?:video|media|format) (?:is )?DRM protected(?:[.!]|\s*$))', text):
        return '检测到真正 DRM 受保护媒体，本工具不提供密钥提取或保护绕过功能。'
    if 'sample-aes' in lower or 'unsupported encryption' in lower:
        return '当前下载器不支持这种普通媒体加密方式；这并不等于检测到了 DRM。请查看检测详情中的 METHOD / KEYFORMAT。'
    if '423' in lower or 'locked' in lower:
        return '服务器暂时限制了部分分片请求，自动重试后仍未成功，请重新获取链接。'
    if 'unsupported url' in lower:
        return '当前输入可能是普通网页地址，而不是实际媒体地址。请在 Chrome Network 中查找 index.m3u8。'
    if 'remotedisconnected' in lower or 'remote end closed' in lower:
        return '服务器主动关闭了连接，可能需要完整浏览器请求头。建议使用 Copy as cURL 自动解析。'
    if 'certificate' in lower or 'ssl:' in lower:
        return 'HTTPS 证书验证失败，请检查系统时间和网络代理；程序不会自动关闭证书检查。'
    if 'ffmpeg' in lower and any(x in lower for x in ('not found', 'not installed', 'unable to find')):
        return '未检测到 FFmpeg，无法合并或输出 MP4。请将 ffmpeg.exe、ffprobe.exe 放在程序旁的 tools 目录。'
    if any(x in lower for x in ('permissionerror', 'permission denied', 'access is denied', '拒绝访问')):
        return '无法写入输出目录，请选择有写入权限的文件夹。'
    if 'winerror 3' in lower or '系统找不到指定的路径' in lower:
        return '输出目录所在磁盘不存在，请点击“浏览”选择本机存在的磁盘。'
    if 'yt-dlp' in lower and any(x in lower for x in ('找不到', '未检测到', 'winerror 2', 'no such file')):
        return '未检测到 yt-dlp，请选择 yt-dlp.exe 或先安装 yt-dlp。'
    return text.strip()[-1800:] or '下载未完成。请点击“查看完整错误”检查详情。'


def _size(value) -> str:
    try:
        value = float(value)
    except (ValueError, TypeError):
        return '--'
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024
    return '--'


def progress_from_data(data: dict) -> dict:
    downloaded = data.get('downloaded_bytes')
    total = data.get('total_bytes') or data.get('total_bytes_estimate')
    percent = None
    if isinstance(downloaded, (float, int)) and isinstance(total, (float, int)) and total > 0:
        percent = min(100.0, max(0.0, downloaded * 100 / total))
    if data.get('status') == 'finished':
        percent = 100.0
    speed = _size(data.get('speed'))
    eta = data.get('eta')
    if isinstance(eta, (float, int)) and eta >= 0:
        seconds = int(eta)
        eta = f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}' if seconds >= 3600 else f'{seconds // 60:02d}:{seconds % 60:02d}'
    else:
        eta = '--'
    return {'percent': percent, 'speed': speed + '/s' if speed != '--' else '--',
            'downloaded': _size(downloaded), 'total': _size(total), 'eta': eta,
            'fragment_index': data.get('fragment_index'), 'fragment_count': data.get('fragment_count'),
            'downloaded_bytes': downloaded, 'total_bytes': total}


class DownloadWorker(QThread):
    log = Signal(str)
    progress = Signal(dict)
    status = Signal(str)
    completed = Signal(dict)
    diagnostic = Signal(dict)

    def __init__(self, request: DownloadRequest, parent=None):
        super().__init__(parent)
        self.request = request
        self.cancel_event = threading.Event()

    def request_stop(self):
        self.cancel_event.set()

    def run(self):
        from core.retry_policy import is_retryable, retry_delay, wait_for_retry
        result = None
        for attempt in range(1, self.request.recovery_max_attempts + 1):
            if self.cancel_event.is_set():
                result = {'success': False, 'cancelled': True, 'files': [], 'error': '', 'error_code': ''}
                break
            if attempt > 1:
                self.status.emit('正在恢复，复用已验证的下载进度…')
                self.progress.emit({'recovery_state': 'recovering', 'retry_attempt': attempt - 1,
                                    'retry_countdown': 0})
            result = self._run_once()
            if result.get('success') or result.get('cancelled'):
                break
            error = MediaCheckError(result.get('error', ''), result.get('error_code') or 'DOWNLOAD_FAILED',
                                    result.get('diagnostic') or {})
            if attempt >= self.request.recovery_max_attempts or not is_retryable(error):
                break
            delay = retry_delay(error, attempt, base=self.request.retry_base_delay)
            reason = Redactor(self.request.headers, [self.request.url]).redact(str(error))[-300:]
            self.log.emit(f'临时错误：{reason}；保留已有进度，准备第 {attempt} 次自动恢复。')

            def tick(remaining):
                self.status.emit(f'等待网络 / 正在重试 {attempt}/{self.request.recovery_max_attempts - 1} · '
                                 f'{remaining:.0f} 秒后继续 · {reason}')
                self.progress.emit({'recovery_state': 'waiting_network', 'retry_attempt': attempt,
                                    'retry_countdown': remaining, 'last_error': reason})

            if not wait_for_retry(self.cancel_event, delay, on_tick=tick):
                result['cancelled'] = True
                break
        self.completed.emit(result or {'success': False, 'cancelled': True, 'files': [], 'error': ''})

    def _run_once(self):
        redactor = Redactor(self.request.headers, [self.request.url])
        recent = deque(maxlen=300)
        files = []
        process = None
        tree = None
        bridge = None
        inspection = None
        workspace = None
        track_roles, track_progress = {}, {}
        exit_code = None
        result = {'success': False, 'cancelled': False, 'exit_code': None, 'files': [], 'error': '',
                  'error_code': '', 'diagnostic': {}}

        def emit_diagnostic(values):
            # 信号跨线程后只能包含脱敏的展示数据，绝不携带原始 context/body。
            safe = redact_data(values, redactor)
            result['diagnostic'] = safe
            self.diagnostic.emit(safe)

        def transfer_error(error):
            result['error_code'] = error.code
            values = dict(result['diagnostic'])
            known_drm = values.get('drm')
            values.update(error.diagnostic)
            if (error.code not in ('PROTECTED_MEDIA', 'DRM_PROTECTED')
                and error.diagnostic.get('drm') is None and known_drm is not None):
                # HTTP/网络失败不推翻已解析清单的保护结论，也不生成新的 DRM 证据。
                values['drm'] = known_drm
            values['stage'] = '派生媒体请求'
            values['resource_domain_changed'] = (urlsplit(values.get('final_url', '')).netloc !=
                                                 urlsplit(values.get('manifest_final_url', '')).netloc)
            emit_diagnostic(values)
            self.log.emit(redactor.redact(f'{error.code}: {error}'))
            self.progress.emit({'last_error': redactor.redact(str(error))[-300:]})

        try:
            build_arguments(self.request, require_tools=False)
            context = self.request.to_context()
            from core.recovery_workspace import RecoveryWorkspace
            if self.request.resume_identity is not None and self.request.temporary_directory:
                marker = Path(self.request.temporary_directory) / 'workspace.json'
                if marker.is_file():
                    workspace = RecoveryWorkspace(self.request, self.request.resume_identity,
                        self.request.resume_identity.get('protocol', self.request.download_protocol), self.log.emit,
                        lock_workspace=True)
                    workspace.resume_auxiliary_publication()
                    committed, pending = workspace.committed_files(), workspace.pending_files()
                    recovered_files = committed or pending
                    if recovered_files:
                        from core.output_validator import validate_output
                        resolve_required_tools(self.request, ('ffprobe',), self.cancel_event)
                        self.status.emit('正在验证异常退出前已完成的媒体…')
                        validations = [validate_output(path, ffmpeg_path=self.request.ffmpeg_path,
                            ffprobe_path=self.request.ffprobe_path,
                            expected_extension=self.request.output_extension, cancel_event=self.cancel_event)
                            for path in recovered_files]
                        if pending:
                            recovered_files = workspace.publish(pending, validations)
                        if workspace.subtitle_files():
                            for validation in validations:
                                validation['subtitle_files'] = workspace.subtitle_files()
                        exit_code = 0
                        result.update(success=True, files=recovered_files, output_validation=validations,
                                      exit_code=0, error_code='')
                        self.progress.emit({'percent': 100.0, 'recovery_state': 'completed'})
                        self.status.emit('已恢复并验证已完成文件')
                        return result
            self.status.emit('正在检查媒体清单…')
            if self.request.output_path and Path(self.request.output_path).exists():
                raise MediaCheckError('已分配的输出文件被其他程序创建，请创建新任务以安全续号。', 'OUTPUT_EXISTS')
            inspection = check_media(context, cancel_event=self.cancel_event, concurrency=self.request.concurrency,
                                     manifest_text=self.request.manifest_text, manifest_url=self.request.manifest_url,
                                     manifest_origin=self.request.manifest_origin,
                                     expected_media_type=self.request.download_protocol)
            from core.resume_identity import capture_identity, validate_resume
            identity = capture_identity(inspection, self.request.media_metadata)
            if self.request.resume_identity is not None:
                validate_resume(self.request.resume_identity, identity)
            else:
                self.request.resume_identity = identity
            if workspace is None:
                workspace = RecoveryWorkspace(self.request, identity, inspection.root.info.media_type, self.log.emit,
                                              lock_workspace=True)
            emit_diagnostic(inspection.diagnostic())
            if self.cancel_event.is_set():
                raise MediaCheckCancelled('下载已停止。')
            if inspection.diagnostic()['drm'] is not True and inspection.diagnostic()['classification'] in ('HLS_SAMPLE_AES', 'HLS_ENCRYPTION_UNKNOWN'):
                raise MediaCheckError('当前下载器不支持这种普通媒体加密方式；尚无明确 DRM 证据。',
                                      'UNSUPPORTED_MEDIA', inspection.diagnostic())
            directory = Path(self.request.output_directory).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise ValueError('输出路径不是文件夹，请重新选择。')
            # 在下载前确认真实写权限，不留下探测文件。
            import tempfile
            with tempfile.TemporaryFile(dir=directory):
                pass
            workspace.prepare()
            protocol = inspection.root.info.media_type
            if protocol == 'DASH':
                import xml.etree.ElementTree as ET
                mpd = ET.fromstring(inspection.root.data)
                for adaptation in mpd.iter():
                    if adaptation.tag.split('}')[-1] != 'AdaptationSet':
                        continue
                    inherited = adaptation.attrib.get('contentType') or adaptation.attrib.get('mimeType', '').partition('/')[0]
                    for representation in adaptation:
                        if representation.tag.split('}')[-1] == 'Representation':
                            role = representation.attrib.get('contentType') or representation.attrib.get('mimeType', '').partition('/')[0] or inherited
                            if role in ('video', 'audio'):
                                track_roles[representation.attrib.get('id', '')] = role
            cache = None
            live = any(snapshot.info.media_type == 'HLS' and snapshot.info.playlist_type != 'Master'
                       and b'#EXT-X-ENDLIST' not in snapshot.data
                       for snapshot in inspection.manifests.values())
            if protocol == 'DASH':
                import xml.etree.ElementTree as ET
                live = ET.fromstring(inspection.root.data).attrib.get('type') == 'dynamic'
            if protocol in ('HLS', 'DASH') and not live:
                from core.segment_cache import SegmentCache
                cache = SegmentCache(workspace.root / 'cache', workspace.identity_hash, self.progress.emit)
            elif live:
                self.log.emit('直播 / 滑动窗口只能保留当前会话进度，跨会话不能保证清单仍可恢复。')
            if not self.request.output_extension and inspection.root.content_type.startswith('audio/'):
                self.request.output_extension = {'audio/mpeg': 'mp3', 'audio/mp4': 'm4a',
                    'audio/aac': 'aac', 'audio/ogg': 'ogg', 'audio/wav': 'wav', 'audio/flac': 'flac'}.get(
                    inspection.root.content_type.partition(';')[0], '')
            # The existing backend uses yt-dlp for Direct, HLS and DASH. Inspection
            # and queue editing need none; validating recovered output needs only
            # ffprobe. FFmpeg is required only for merge/remux operations.
            required = ['yt-dlp']
            if protocol in ('HLS', 'DASH') or self.request.ffmpeg_path:
                required.append('ffmpeg')
            required.append('ffprobe')
            self.status.emit('正在检查当前下载需要的工具…')
            resolve_required_tools(self.request, required, self.cancel_event)
            self.status.emit('正在下载…')
            self.progress.emit({'recovery_state': 'downloading', 'retry_countdown': 0})
            self.log.emit('媒体检查通过，开始下载。')
            bridge = MediaBridge(inspection, self.cancel_event, transfer_error,
                                 segment_max_attempts=self.request.segment_max_attempts,
                                 concurrency=self.request.concurrency, cache=cache,
                                 on_progress=self.progress.emit,
                                 retry_base_delay=self.request.retry_base_delay).start()
            args = build_arguments(replace(self.request, url=bridge.url, headers={}, method='GET', body=None,
                                           download_protocol=protocol, output_directory=str(workspace.home),
                                           final_output_directory='', collection_directory='', organization={},
                                           output_path='', temporary_directory=str(workspace.temporary)))
            if not self.request.ffmpeg_path:
                self.log.emit('提示：未检测到 FFmpeg，将保留可下载的原始媒体格式；合并或封装可能失败。')
            process = subprocess.Popen(args, shell=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                                       bufsize=1, creationflags=NO_WINDOW, start_new_session=os.name != 'nt')
            tree = ProcessTree(process)
            output = queue.Queue(maxsize=2000)

            def read_lines():
                try:
                    for line in process.stdout:
                        while True:
                            try:
                                output.put(line.rstrip('\r\n'), timeout=0.1)
                                break
                            except queue.Full:
                                if self.cancel_event.is_set():
                                    return
                finally:
                    output.put(None)

            reader = threading.Thread(target=read_lines, daemon=True)
            reader.start()
            while True:
                if bridge.last_error and bridge.last_error.code in FATAL_TRANSFER_ERRORS:
                    tree.terminate()
                    break
                if self.cancel_event.is_set():
                    self.status.emit('正在停止下载…')
                    tree.terminate()
                    break
                try:
                    line = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line.startswith(PROGRESS_MARKER):
                    try:
                        native = json.loads(line[len(PROGRESS_MARKER):])
                        data = progress_from_data(native)
                        if protocol == 'DASH':
                            filename = str(native.get('filename') or native.get('tmpfilename') or '')
                            role = next((role for key, role in track_roles.items() if key and '.f' + key + '.' in filename), '')
                            if not role and filename.lower().endswith(('.m4a', '.m4a.part')):
                                role = 'audio'
                            if role:
                                track_progress[role] = {key: data.get(key) for key in ('downloaded_bytes', 'total_bytes')}
                                track_progress[role]['state'] = 'completed' if native.get('status') == 'finished' else 'downloading'
                            data['tracks'] = dict(track_progress)
                            self.request.media_metadata['_recovery_tracks'] = dict(track_progress)
                            required = set(track_roles.values())
                            if required and all(role in track_progress and track_progress[role].get('total_bytes')
                                                for role in required):
                                total = sum(track_progress[role]['total_bytes'] for role in required)
                                downloaded = sum(track_progress[role].get('downloaded_bytes') or 0 for role in required)
                                data.update(percent=100 * downloaded / total, total_bytes=total,
                                            downloaded_bytes=downloaded, total=_size(total), downloaded=_size(downloaded))
                            else:
                                data['percent'] = None
                        if data['percent'] is not None:
                            data['percent'] = min(99.9, data['percent'])
                        self.progress.emit(data)
                        workspace.checkpoint()
                        percent = f"{data['percent']:.1f}%" if data['percent'] is not None else '--'
                        self.log.emit(f"[download] {percent} | {data['downloaded']} | {data['speed']} | 剩余 {data['eta']}")
                    except (ValueError, TypeError, KeyError):
                        self.log.emit('下载进度暂时不可用。')
                    continue
                if line.startswith(FILE_MARKER):
                    try:
                        path = json.loads(line[len(FILE_MARKER):])
                        if isinstance(path, str) and Path(path).is_file():
                            files.append(str(Path(path).resolve()))
                    except (ValueError, OSError):
                        pass
                    continue
                safe_line = redactor.redact(line.replace(bridge.base, '[媒体传输]/'))
                recent.append(safe_line)
                self.log.emit(safe_line)
                fragments = re.search(r'Total fragments:\s*(\d+)', line)
                if fragments:
                    self.progress.emit({'fragment_count': int(fragments.group(1))})
                if 'retrying fragment' in line.lower() or ('423' in line and 'locked' in line.lower()):
                    self.status.emit('服务器暂时拒绝部分分片，yt-dlp 正在自动重试。')
                if any(key in line.lower() for key in ('[merger]', '[videoremuxer]', '[fixupm3u8]')):
                    self.status.emit('下载分片已完成，正在合并或整理视频…')
                    self.progress.emit({'recovery_state': 'merging'})
            exit_code = process.wait()
            reader.join(timeout=3)
            if (not self.cancel_event.is_set() and bridge.last_error and
                    bridge.last_error.code in FATAL_TRANSFER_ERRORS):
                raise bridge.last_error
            if self.cancel_event.is_set():
                result['cancelled'] = True
                self.log.emit('下载已停止，未删除已下载的分片或 .part 文件，可重新开始续传。')
            elif exit_code == 0 and files:
                from core.output_validator import validate_output
                self.status.emit('正在校验输出容器和媒体流…')
                self.progress.emit({'recovery_state': 'verifying'})
                verified_files = list(dict.fromkeys(files))
                from core.track_pipeline import process_outputs
                verified_files, subtitle_files = process_outputs(self.request, verified_files, workspace,
                                                                 self.cancel_event, self.log.emit)
                validations = [validate_output(path, ffmpeg_path=self.request.ffmpeg_path,
                    ffprobe_path=self.request.ffprobe_path,
                    expected_extension=self.request.output_extension or ('mp4' if self.request.ffmpeg_path else ''),
                    cancel_event=self.cancel_event) for path in verified_files]
                if protocol == 'DASH':
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(inspection.root.data)
                    types = {node.attrib.get('contentType', '') or node.attrib.get('mimeType', '').partition('/')[0]
                             for node in root.iter()}
                    if 'video' in types and 'audio' in types and any(
                            not value['video_streams'] or not value['audio_streams'] for value in validations):
                        raise MediaCheckError('DASH 必需的视频轨或音频轨尚未合并完成。', 'OUTPUT_VALIDATION_ERROR')
                verified_files = workspace.publish(verified_files, validations, auxiliary=subtitle_files)
                result['success'] = True
                result['files'] = verified_files
                result['output_validation'] = validations
                self.log.emit('输出媒体验证通过：' + ', '.join(item['format_name'] for item in validations))
                result['error_code'] = ''
                emit_diagnostic({**inspection.diagnostic(), 'stage': '下载完成',
                                 'output_validation': validations,
                                 'resource_domains': sorted(bridge.resource_domains)})
                self.status.emit('下载完成')
                self.progress.emit({'percent': 100.0, 'recovery_state': 'completed', 'retry_countdown': 0})
            else:
                if bridge.last_error:
                    result['error'] = f'{bridge.last_error.code}: {bridge.last_error}\n' + '\n'.join(recent)
                    result['error_code'] = bridge.last_error.code
                    emit_diagnostic({**result['diagnostic'], **bridge.last_error.diagnostic})
                else:
                    result['error'] = '\n'.join(recent) or 'yt-dlp 已退出，但未确认输出文件。请查看日志。'
                    result['error_code'] = ('MERGE_FAILED' if any('ffmpeg' in item.lower() for item in recent)
                                            else 'DOWNLOAD_FAILED')
                self.status.emit('下载失败')
        except MediaCheckCancelled:
            result['cancelled'] = True
            self.status.emit('下载已停止')
        except Exception as error:
            if self.cancel_event.is_set():
                result['cancelled'] = True
            else:
                if isinstance(error, MediaCheckError):
                    result['error_code'] = error.code
                    values = {**(inspection.diagnostic() if inspection else {}), **error.diagnostic}
                    if error.code.startswith('OUTPUT_'):
                        values['stage'] = ('PNG 分片解包 / 媒体校验' if error.diagnostic.get('resource_role')
                                           else '输出容器 / 媒体流校验')
                    emit_diagnostic(values)
                else:
                    import errno
                    result['error_code'] = ('DISK_FULL' if isinstance(error, OSError) and
                        (error.errno == errno.ENOSPC or getattr(error, 'winerror', None) == 112)
                        else 'FILE_PERMISSION' if isinstance(error, PermissionError) else 'MEDIA_CHECK_ERROR')
                    emit_diagnostic({'error_code': 'MEDIA_CHECK_ERROR', 'exception_type': type(error).__name__,
                                     'exception_message': str(error), 'drm': None, 'stage': '下载检查 / 文件操作'})
                result['error'] = redactor.redact(f'{type(error).__name__}: {error}')
                self.log.emit(result['error'])
                self.status.emit('下载失败')
        finally:
            if tree:
                if process and process.poll() is None:
                    tree.terminate()
                tree.close()
            if process and process.stdout:
                process.stdout.close()
            if bridge:
                bridge.close()
            if workspace and workspace.state.get('status') != 'completed':
                try:
                    workspace.checkpoint(final=True)
                except (OSError, ValueError) as error:
                    self.log.emit(redactor.redact(f'未能完整保存本次恢复检查点：{type(error).__name__}；原文件已保留。'))
            result['exit_code'] = exit_code
            result['error'] = redactor.redact(result['error'])
            if workspace:
                workspace.close()
        return result
