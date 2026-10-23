"""下载成功之前探测真实输出容器；文件名和子进程退出码不能证明是视频。"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from core.errors import MediaCheckCancelled, MediaCheckError
from core.process_tree import NO_WINDOW, ProcessTree
from core.yt_dlp_manager import APP_DIR
from core.tool_manager import ToolManager


PROBE_TIMEOUT = 30.0
MAX_PROBE_OUTPUT = 256 * 1024
MAX_STREAMS = 256
MP4_FORMATS = frozenset(('mov', 'mp4', 'm4a', '3gp', '3g2', 'mj2'))
IMAGE_FORMATS = frozenset(('image2', 'image2pipe', 'png_pipe', 'jpeg_pipe', 'gif', 'apng',
                           'bmp_pipe', 'webp_pipe', 'tiff_pipe', 'ico', 'svg_pipe', 'jpegls_pipe',
                           'jpegxl_pipe', 'qdraw_pipe', 'pam_pipe', 'pbm_pipe', 'pcx_pipe',
                           'pgm_pipe', 'pgmyuv_pipe', 'ppm_pipe', 'psd_pipe', 'qoi_pipe',
                           'sgi_pipe', 'sunrast_pipe', 'xbm_pipe', 'xpm_pipe'))
AUDIO_FORMATS = {'m4a': MP4_FORMATS, 'mp3': frozenset(('mp3',)),
                 'opus': frozenset(('ogg',)), 'ogg': frozenset(('ogg',)),
                 'wav': frozenset(('wav',)), 'flac': frozenset(('flac',)),
                 'aac': frozenset(('aac',))}
_SAFE_NAME = re.compile(r'^[a-zA-Z0-9_,]+$')


def _error(message, code='OUTPUT_VALIDATION_ERROR', diagnostic=None):
    return MediaCheckError(message, code, diagnostic)


def _cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise MediaCheckCancelled('输出验证已取消。')


def _find_ffprobe(ffprobe_path=None, ffmpeg_path=None):
    path = ToolManager(APP_DIR).locate('ffprobe', str(ffprobe_path or ''), mode='custom',
                                      sibling=str(ffmpeg_path or ''))
    if path:
        return path
    raise _error('未找到 ffprobe，无法验证输出是否为真实媒体。请选择 ffmpeg 同目录的 ffprobe 或放入 tools。',
                 'OUTPUT_PROBE_UNAVAILABLE')


def _probe_file(path, executable, cancel_event):
    args = [executable, '-v', 'error', '-max_alloc', '67108864',
            '-protocol_whitelist', 'file,pipe', '-probesize', '8388608',
            '-analyzeduration', '10000000', '-show_entries',
            'format=format_name,duration:stream=index,codec_type,codec_name,width,height,channels,channel_layout,sample_rate,bit_rate:'
            'stream_tags=language,title:stream_disposition=attached_pic,default,forced', '-of', 'json', '-i', str(path)]
    try:
        process = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   creationflags=NO_WINDOW, start_new_session=os.name != 'nt')
    except (OSError, ValueError):
        raise _error('ffprobe 无法启动，输出尚未通过媒体验证。') from None
    tree = ProcessTree(process)
    output = bytearray()
    too_large = threading.Event()
    read_failed = threading.Event()

    def read_output():
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    return
                if len(output) + len(chunk) > MAX_PROBE_OUTPUT:
                    too_large.set()
                    return
                output.extend(chunk)
        except (OSError, ValueError):
            read_failed.set()

    reader = threading.Thread(target=read_output, name='output-probe-reader', daemon=True)
    reader.start()
    deadline = time.monotonic() + PROBE_TIMEOUT
    try:
        while True:
            _cancelled(cancel_event)
            if too_large.is_set():
                raise _error('输出探测结果超过限制，无法完成媒体验证。')
            if read_failed.is_set():
                raise _error('无法读取输出探测结果。')
            if process.poll() is not None and not reader.is_alive():
                break
            if time.monotonic() >= deadline:
                raise _error('输出媒体探测超时，下载结果尚未通过验证。')
            if cancel_event is None:
                time.sleep(0.05)
            else:
                cancel_event.wait(0.05)
        if process.returncode != 0:
            raise _error('ffprobe 无法解析输出文件，下载结果尚未通过媒体验证。')
        try:
            data = json.loads(output.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise _error('ffprobe 返回无效探测结果。') from None
        if not isinstance(data, dict):
            raise _error('ffprobe 返回无效探测结果。')
        return data
    finally:
        try:
            if process.poll() is None:
                tree.terminate()
        finally:
            tree.close()
            reader.join(timeout=1)
            if process.stdout is not None:
                process.stdout.close()


def _safe_name(value):
    if isinstance(value, str) and len(value) <= 160 and _SAFE_NAME.fullmatch(value):
        return value.lower()
    return ''


def _safe_number(value, *, integer=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if integer else number


def _summarize(data, size):
    format_info = data.get('format')
    streams = data.get('streams')
    if not isinstance(format_info, dict) or not isinstance(streams, list) or len(streams) > MAX_STREAMS:
        raise _error('ffprobe 探测结果缺少有效容器或媒体流信息。')
    format_name = _safe_name(format_info.get('format_name'))
    if not format_name:
        raise _error('ffprobe 未识别出有效媒体容器。')
    summary = {'format_name': format_name, 'video_streams': [], 'audio_streams': [], 'subtitle_streams': [], 'size': size}
    duration = _safe_number(format_info.get('duration'))
    if duration is not None:
        summary['duration'] = duration
    for stream in streams:
        if not isinstance(stream, dict):
            raise _error('ffprobe 返回无效媒体流信息。')
        stream_type = stream.get('codec_type')
        if stream_type not in ('video', 'audio', 'subtitle'):
            continue
        disposition = stream.get('disposition') or {}
        if not isinstance(disposition, dict):
            raise _error('ffprobe 返回无效媒体流属性。')
        if stream_type == 'video' and disposition.get('attached_pic') in (1, '1', True):
            continue
        item = {'codec_type': stream_type, 'codec_name': _safe_name(stream.get('codec_name'))}
        for name in ('index', 'width', 'height', 'channels', 'sample_rate', 'bit_rate'):
            if name in stream:
                number = _safe_number(stream[name], integer=True)
                if number is not None:
                    item[name] = number
        tags = stream.get('tags') or {}
        if isinstance(tags, dict):
            from core.media_tracks import language_code
            from utils.security import Redactor
            if language_code(tags.get('language')):
                item['language'] = language_code(tags['language'])
            title = tags.get('title')
            if stream_type in ('audio', 'subtitle') and isinstance(title, str):
                title = Redactor().redact(title[:256])
                title = re.sub(r'https?://\S+', '[链接已隐藏]', title)
                title = re.sub(r'(?i)\b(token|secret|password|signature|authorization|cookie)(\s*[:=]\s*)\S+', r'\1\2********', title)
                item['title'] = ''.join(c for c in title if ord(c) >= 32)[:160]
        layout = stream.get('channel_layout')
        if isinstance(layout, str) and re.fullmatch(r'[A-Za-z0-9().+ _-]{1,80}', layout):
            item['channel_layout'] = layout
        for name in ('default', 'forced'):
            if name in disposition:
                item[name] = disposition[name] in (1, '1', True)
        summary[stream_type + '_streams'].append(item)
    return summary


def validate_output(path, *, ffmpeg_path=None, ffprobe_path=None, expected_extension='', cancel_event=None):
    """探测本地输出；失败保留原文件并抛出明确错误，成功摘要不包含标签或原始日志。"""
    _cancelled(cancel_event)
    try:
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or '\x00' in raw_path or re.match(r'^\w+://', raw_path):
            raise ValueError
        output_path = Path(raw_path).resolve(strict=True)
        if not output_path.is_file():
            raise ValueError
        size = output_path.stat().st_size
    except (OSError, ValueError, TypeError):
        raise _error('输出文件不存在或不是可探测的本地文件。') from None
    if size <= 0:
        raise _error('输出文件为空，不能标记下载成功。')
    if not isinstance(expected_extension, str):
        raise _error('输出格式配置无效。')
    extension = (expected_extension or output_path.suffix).strip().lower().lstrip('.')
    if extension and not re.fullmatch(r'[a-z0-9]{1,16}', extension):
        raise _error('输出格式配置无效。')
    executable = _find_ffprobe(ffprobe_path, ffmpeg_path)
    summary = _summarize(_probe_file(output_path, executable, cancel_event), size)
    _cancelled(cancel_event)
    formats = set(summary['format_name'].split(','))
    videos, audios = summary['video_streams'], summary['audio_streams']
    if formats & IMAGE_FORMATS or any(name.endswith('_pipe') for name in formats):
        raise _error('输出实际为图片容器，不能作为视频下载成功。', 'OUTPUT_NOT_VIDEO', summary)
    if any(stream['codec_name'] == 'png' for stream in videos):
        raise _error('输出视频流实际是 PNG 图片，不能作为视频下载成功。', 'OUTPUT_NOT_VIDEO', summary)
    if extension in ('mp4', 'm4v', 'mov', '3gp', '3g2', 'mj2'):
        if not formats & MP4_FORMATS or not videos:
            raise _error('输出未通过 MP4 视频容器校验：需要 MP4 family 容器和真实视频流。',
                         'OUTPUT_NOT_VIDEO', summary)
    elif extension in AUDIO_FORMATS:
        if not formats & AUDIO_FORMATS[extension] or not audios:
            raise _error('输出未通过音频容器和媒体流校验。', 'OUTPUT_NOT_VIDEO', summary)
        if extension == 'opus' and not any(stream['codec_name'] == 'opus' for stream in audios):
            raise _error('输出不包含 Opus 音频流。', 'OUTPUT_NOT_VIDEO', summary)
    elif extension in ('webm', 'mkv'):
        if not formats & {'webm', 'matroska'} or not (videos or audios):
            raise _error('输出未通过 WebM/Matroska 容器和媒体流校验。', 'OUTPUT_NOT_VIDEO', summary)
    elif not (videos or audios):
        raise _error('输出不包含可用视频或音频流。', 'OUTPUT_NOT_VIDEO', summary)
    return summary
