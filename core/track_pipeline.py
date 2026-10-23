"""下载后、正式提交前的字幕处理；复用请求上下文与 FFmpeg，始终复制音视频流。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

from core.errors import MediaCheckError, MediaCheckCancelled
from core.http_client import ContextHttpClient, cancelled
from core.library_validation import _Control, _Incomplete, _run
from core.media_tracks import language_code, probe_language, text
from core.output_validator import _probe_file, validate_output
from core.recovery_workspace import atomic_json, file_hash

MAX_SUBTITLE_BYTES = 8 * 1024 * 1024
MAX_SUBTITLE_SEGMENTS = 2000
_CUE = re.compile(r'(?m)^\s*(\d{1,3}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})\s*-->\s*'
                  r'(\d{1,3}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})([^\n]*)\n(.*?)(?=\n\s*\n|\Z)', re.S)


def _error(message, code='SUBTITLE_INVALID'):
    return MediaCheckError(message, code)


def _seconds(stamp):
    fields = stamp.replace(',', '.').split(':')
    return sum(float(value) * scale for value, scale in zip(reversed(fields), (1, 60, 3600)))


def _decode(data):
    try:
        return data.decode('utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig').replace('\r\n', '\n')
    except UnicodeError:
        raise _error('字幕不是可识别的 UTF-8 / UTF-16 文本，未替换原文件。') from None


def validate_subtitle(data, fmt):
    """校验实际文本结构；HTML 登录页、空文件和错误扩展名不能成为字幕。"""
    if not data or len(data) > MAX_SUBTITLE_BYTES or b'\x00' in data and not data.startswith((b'\xff\xfe', b'\xfe\xff')):
        raise _error('字幕为空、超过大小限制或包含无效文本。')
    content = _decode(data)
    if re.search(r'(?i)<(?:!doctype\s+html|html|body)(?:\s|>)', content[:2000]):
        raise _error('字幕请求返回了 HTML 页面，请重新分析来源或检查登录状态。')
    if fmt in ('vtt', 'srt'):
        if fmt == 'vtt' and not content.lstrip().startswith('WEBVTT'):
            raise _error('字幕内容不是声明的 WebVTT 格式。')
        cues = list(_CUE.finditer(content))
        if not cues or any(_seconds(cue[2]) <= _seconds(cue[1]) for cue in cues):
            raise _error('字幕没有有效的时间轴或可读取的字幕条目。')
    elif fmt in ('ass', 'ssa'):
        if '[Events]' not in content or not re.search(r'(?m)^Dialogue:', content):
            raise _error('字幕内容不是可读取的 ASS / SSA 字幕。')
    elif fmt == 'ttml':
        if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
            raise _error('字幕 XML 包含不支持的外部实体。')
        try:
            root = ET.fromstring(content)
            if root.tag.rsplit('}', 1)[-1] != 'tt' or not any(n.tag.rsplit('}', 1)[-1] == 'p' for n in root.iter()):
                raise ValueError
        except (ET.ParseError, ValueError):
            raise _error('字幕内容不是有效的 TTML 文本。') from None
    else:
        raise _error('该字幕格式暂不支持可靠验证，请使用网站提供的 SRT / VTT / ASS 文件。')
    return content


def _fetch(client, context, url, deadline, limit=MAX_SUBTITLE_BYTES):
    cancelled(client.cancel_event)
    if time.monotonic() > deadline:
        raise _error('字幕获取超过执行时限；已保留下载结果，可稍后重试。', 'SUBTITLE_TIMEOUT')
    derived = context.derive(url)
    with client.open(derived, overrides={'range': None}) as response:
        output = bytearray()
        while True:
            cancelled(client.cancel_event)
            if time.monotonic() > deadline:
                raise _error('字幕获取超时；已保留下载结果。', 'SUBTITLE_TIMEOUT')
            chunk = response.read(min(65536, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise _error('字幕响应超过大小限制。')
        return bytes(output), response.context


def _hls_vtt(client, context, url, deadline):
    from core.manifests import hls_lines, parse_attribute_list
    playlist, current = _fetch(client, context, url, deadline, 1024 * 1024)
    content = _decode(playlist)
    if not content.lstrip().startswith('#EXTM3U') or '#EXT-X-ENDLIST' not in content:
        raise _error('字幕播放列表不是完整 VOD；直播字幕暂不支持独立提取。')
    if any(line.startswith(('#EXT-X-STREAM-INF', '#EXT-X-MAP', '#EXT-X-BYTERANGE', '#EXT-X-PART'))
           for line in hls_lines(content)):
        raise _error('此字幕分段结构暂不支持；请选择独立字幕或兼容来源。')
    for line in hls_lines(content):
        if line.startswith('#EXT-X-KEY:') and parse_attribute_list(line.partition(':')[2]).get('METHOD') != 'NONE':
            raise _error('此字幕声明了加密，暂不提供独立提取；不尝试保护绕过。')
    urls = [line for line in hls_lines(content) if line and not line.startswith('#')]
    if not urls or len(urls) > MAX_SUBTITLE_SEGMENTS:
        raise _error('字幕分片数量无效或超过限制。')
    cues, seen, size = [], set(), 0
    for url in urls:
        data, _ = _fetch(client, current, url, deadline)
        size += len(data)
        if size > MAX_SUBTITLE_BYTES:
            raise _error('字幕分片总量超过大小限制。')
        segment = validate_subtitle(data, 'vtt')
        mapping = re.search(r'(?mi)^X-TIMESTAMP-MAP\s*=\s*(.+)$', segment)
        if mapping:
            local = re.search(r'LOCAL:([0-9:.]+)', mapping[1])
            mpegts = re.search(r'MPEGTS:(\d+)', mapping[1])
            if not local or not mpegts or abs(_seconds(local[1]) - int(mpegts[1]) / 90000) > .001:
                raise _error('该 HLS 字幕需要非零 MPEGTS 时间轴对齐，当前不进行未经验证的拼接；请使用独立字幕。')
        for cue in _CUE.finditer(segment):
            key = (_seconds(cue[1]), _seconds(cue[2]), cue[4].strip())
            if key not in seen:
                seen.add(key)
                cues.append((key[0], cue[1].replace(',', '.') + ' --> ' + cue[2].replace(',', '.') + cue[3] + '\n' + key[2]))
    return ('WEBVTT\n\n' + '\n\n'.join(value for _, value in sorted(cues, key=lambda v: v[0])) + '\n').encode()


def _download_subtitle(request, item, workspace, cancel_event):
    identifier = hashlib.sha256(str(item['id']).encode()).hexdigest()[:24]
    fmt = 'vtt' if item.get('format') == 'hls-vtt' else item.get('format')
    if fmt not in ('srt', 'vtt', 'ass', 'ssa', 'ttml'):
        raise _error('所选字幕格式不支持独立保存。')
    directory = workspace.home / 'subtitles'
    directory.mkdir(exist_ok=True)
    path, marker = directory / (identifier + '.' + fmt), directory / (identifier + '.json')
    if path.is_symlink() or marker.is_symlink():
        raise _error('字幕工作目录包含外部链接。', 'RECOVERY_PATH_INVALID')
    try:
        state = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() and marker.stat().st_size < 4096 else {}
    except (OSError, ValueError):
        state = {}
    if path.is_file() and path.stat().st_size <= MAX_SUBTITLE_BYTES and state.get('sha256') == file_hash(path):
        validate_subtitle(path.read_bytes(), fmt)
        return {'path': str(path), 'format': fmt, 'language': language_code(item.get('language')),
                'name': text(item.get('name')), 'id': identifier, 'status': 'verified'}
    client = ContextHttpClient(cancel_event=cancel_event, timeout=8, concurrency=request.concurrency)
    deadline = time.monotonic() + 120
    context = request.to_context()
    if item.get('format') == 'hls-vtt':
        data = _hls_vtt(client, context, item['url'], deadline)
    else:
        data, _ = _fetch(client, context, item['url'], deadline)
    validate_subtitle(data, fmt)
    # 工作区中自己的过期字幕可以替换；正式目录中的文件只通过 publish 提交。
    temporary = path.with_suffix(path.suffix + '.partial')
    if temporary.is_symlink():
        raise _error('字幕临时路径包含外部链接。', 'RECOVERY_PATH_INVALID')
    temporary.write_bytes(data)
    temporary.replace(path)
    atomic_json(marker, {'sha256': file_hash(path), 'size': path.stat().st_size})
    return {'path': str(path), 'format': fmt, 'language': language_code(item.get('language')),
            'name': text(item.get('name')), 'id': identifier, 'status': 'verified'}


def check_compatibility(summary, subtitles, container):
    videos = [v.get('codec_name', '') for v in summary.get('video_streams', [])]
    audios = [v.get('codec_name', '') for v in summary.get('audio_streams', [])]
    existing = [v.get('codec_name', '') for v in summary.get('subtitle_streams', [])]
    if container == 'webm' and (any(v not in ('vp8', 'vp9', 'av1') for v in videos) or
                               any(v not in ('opus', 'vorbis') for v in audios)):
        raise _error('所选音视频编码不能直接封装到 WebM；请选择 MKV 或兼容的 MP4。本功能不会自动转码音视频。', 'TRACK_INCOMPATIBLE')
    if container == 'mp4' and (any(v not in ('h264', 'hevc', 'av1', 'mpeg4', 'vp9') for v in videos) or
                              any(v not in ('aac', 'mp3', 'ac3', 'eac3', 'alac', 'opus') for v in audios)):
        raise _error('所选编码暂不支持无损封装到 MP4，请选择 MKV；不会自动转码。', 'TRACK_INCOMPATIBLE')
    if container not in ('mp4', 'mkv', 'webm') and subtitles:
        raise _error('此输出容器不支持字幕封装，请使用独立字幕文件或 MKV。', 'TRACK_INCOMPATIBLE')
    if any(v.get('format') == 'ttml' for v in subtitles):
        raise _error('TTML 暂支持独立保存；请使用独立文件，或选择 SRT / VTT / ASS 后封装。', 'TRACK_INCOMPATIBLE')
    allowed = {'mp4': {'mov_text'}, 'webm': {'webvtt'}, 'mkv': None}.get(container)
    if allowed is not None and any(v not in allowed for v in existing):
        raise _error('原文件内嵌字幕不能直接复制到所选容器，请选择 MKV 以保留原轨道，或使用独立字幕。', 'TRACK_INCOMPATIBLE')


def process_outputs(request, files, workspace, cancel_event, notice=lambda _: None):
    """返回媒体暂存路径及可单独提交的字幕；失败时原媒体仍位于可信工作区。"""
    selection = request.media_metadata.get('track_selection') or {}
    mode = selection.get('subtitle_mode', 'none')
    selected = request.media_metadata.get('selected_subtitles') or []
    audio = request.media_metadata.get('selected_audio') or {}
    if not selected and not audio:
        return files, []
    cancelled(cancel_event)
    subtitles = []
    if mode in ('sidecar', 'mux'):
        for item in selected:
            notice('正在获取并检查所选字幕…')
            subtitles.append(_download_subtitle(request, item, workspace, cancel_event))
            workspace.checkpoint(final=True)
    language = language_code(audio.get('language'))
    # HLS / DASH 的容器标签经提取器合并时可能丢失；以已选择的真实声明补齐。
    if mode != 'mux' and not language:
        return files, subtitles if mode == 'sidecar' else []
    output = []
    for original in files:
        base = Path(original)
        if base.is_symlink() or not base.resolve().is_relative_to(workspace.home.resolve()):
            raise _error('轨道处理只允许当前任务的暂存文件。', 'RECOVERY_PATH_INVALID')
        summary = validate_output(base, ffmpeg_path=request.ffmpeg_path, ffprobe_path=request.ffprobe_path,
                                  expected_extension=request.output_extension, cancel_event=cancel_event)
        mux_subs = subtitles if mode == 'mux' else []
        check_compatibility(summary, mux_subs, request.output_extension or base.suffix.lstrip('.'))
        if not request.ffmpeg_path:
            raise _error('所选音轨标签或字幕封装需要 FFmpeg，请在工具设置重新检测。', 'TRACK_TOOL_UNAVAILABLE')
        target_directory = workspace.home / 'track-output'
        if target_directory.is_symlink():
            raise _error('轨道暂存目录包含外部链接。', 'RECOVERY_PATH_INVALID')
        target_directory.mkdir(exist_ok=True)
        destination = target_directory / base.name
        if destination.is_symlink():
            raise _error('轨道暂存路径包含外部链接。', 'RECOVERY_PATH_INVALID')
        if destination.exists():
            # 仅重建本任务拥有的上一次封装结果；原下载媒体始终保留。
            destination.unlink()
        args = [request.ffmpeg_path, '-hide_banner', '-nostdin', '-v', 'error', '-n',
                '-protocol_whitelist', 'file,pipe', '-i', str(base)]
        for item in mux_subs:
            args += ['-protocol_whitelist', 'file,pipe', '-i', item['path']]
        args += ['-map', '0']
        for index in range(len(mux_subs)):
            args += ['-map', str(index + 1) + ':0']
        args += ['-c', 'copy']
        if language and len(summary['audio_streams']) == 1:
            args += ['-metadata:s:a:0', 'language=' + probe_language(language)]
            if audio.get('name'):
                args += ['-metadata:s:a:0', 'title=' + text(audio['name'])]
        offset = len(summary.get('subtitle_streams', []))
        for index, item in enumerate(mux_subs):
            stream = index + offset
            fmt = request.output_extension or base.suffix.lstrip('.')
            codec = 'mov_text' if fmt == 'mp4' else 'webvtt' if fmt == 'webm' else 'copy'
            args += [f'-c:s:{stream}', codec, f'-metadata:s:s:{stream}', 'language=' + probe_language(item['language']),
                     f'-metadata:s:s:{stream}', 'title=' + item['name'], f'-disposition:s:{stream}', '0']
        args += [str(destination)]
        notice('正在封装所选轨道：复制音视频流，仅转换兼容字幕格式…')
        try:
            code, _, _ = _run(args, _Control(cancel_event, 300))
        except _Incomplete as error:
            cancelled(cancel_event)
            raise _error(str(error) + ' 原始下载结果仍保留。', 'TRACK_MUX_TIMEOUT') from None
        if code:
            raise _error('FFmpeg 轨道封装失败，原始下载结果仍保留；请使用独立字幕或兼容容器后创建新任务。', 'TRACK_MUX_FAILED')
        actual = validate_output(destination, ffmpeg_path=request.ffmpeg_path, ffprobe_path=request.ffprobe_path,
                                 expected_extension=request.output_extension, cancel_event=cancel_event)
        if len(actual.get('subtitle_streams', [])) != offset + len(mux_subs):
            raise _error('封装输出的字幕轨数量与选择不符，未提交结果。', 'OUTPUT_VALIDATION_ERROR')
        if len(actual['audio_streams']) != len(summary['audio_streams']) or len(actual['video_streams']) != len(summary['video_streams']):
            raise _error('封装改变了原有音视频轨数量，未提交结果。', 'OUTPUT_VALIDATION_ERROR')
        output.append(str(destination))
    return output, subtitles if mode == 'sidecar' else []
