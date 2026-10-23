"""从已验证的本地视频生成封面；失败只返回结果，不改变下载状态。"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from PySide6.QtGui import QImageReader


MAX_JPEG_BYTES = 4 * 1024 * 1024
MAX_TIMEOUT = 60.0
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _result(status, attempts, message, path=''):
    return {'status': status, 'path': str(path) if path else '',
            'message': message, 'attempts': list(attempts)}


def _cancelled(event):
    return event is not None and event.is_set()


def _local_file(value):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or '\x00' in raw or re.match(r'^\w+://', raw)
            or raw.startswith(('\\\\', '//'))):
        raise ValueError('需要本地文件')
    path = Path(raw).resolve(strict=True)
    if not path.is_file():
        raise ValueError('需要普通文件')
    return path


def _valid_jpeg(path):
    """同时检查签名、大小和真实 JPEG 解码，不把任意同名文件当封面。"""
    try:
        if not path.is_file() or not 4 <= path.stat().st_size <= MAX_JPEG_BYTES:
            return False
        with path.open('rb') as source:
            if source.read(2) != b'\xff\xd8':
                return False
            source.seek(-2, os.SEEK_END)
            if source.read(2) != b'\xff\xd9':
                return False
        reader = QImageReader(str(path), b'jpeg')
        size = reader.size()
        if not 0 < size.width() <= 4096 or not 0 < size.height() <= 4096:
            return False
        return not reader.read().isNull()
    except Exception:
        return False


def _existing_cover(path, attempts):
    if os.path.lexists(path):
        if _valid_jpeg(path):
            return _result('existing', attempts, '已保留现有 JPEG 封面。', path)
        return _result('failed', attempts, '已有同名封面无法验证为 JPEG，已保留且未覆盖。')
    return None


def _video_stream(validation, video):
    if not isinstance(validation, dict):
        return None
    format_name = validation.get('format_name')
    streams = validation.get('video_streams')
    size = validation.get('size')
    if (not isinstance(format_name, str) or not re.fullmatch(r'[a-zA-Z0-9_,]{1,160}', format_name)
            or not isinstance(streams, list) or len(streams) > 256
            or type(size) is not int or size <= 0 or size != video.stat().st_size):
        return None
    formats = set(format_name.lower().split(','))
    if (video.suffix.lower() == '.mp3' or formats & {'mp3', 'image2', 'image2pipe', 'gif', 'apng', 'ico'}
            or any(name.endswith('_pipe') for name in formats)):
        return None
    for stream in streams:
        if (not isinstance(stream, dict) or stream.get('codec_type') != 'video'
                or not isinstance(stream.get('codec_name'), str)
                or not re.fullmatch(r'[a-zA-Z0-9_]{1,80}', stream['codec_name'])
                or stream['codec_name'].lower() == 'png'
                or stream.get('attached_pic') in (1, '1', True)
                or isinstance(stream.get('disposition'), dict)
                and stream['disposition'].get('attached_pic') in (1, '1', True)):
            continue
        index = stream.get('index')
        if index is None or type(index) is int and 0 <= index < 256:
            return stream
    return None


def _seek_times(validation):
    times = [1.0, 3.0]
    duration = validation.get('duration')
    if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
        times.append(duration / 2)
    return times


def _run_ffmpeg(args, cancel_event, deadline):
    process = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW)
    try:
        while True:
            if _cancelled(cancel_event):
                return 'cancelled'
            code = process.poll()
            if code is not None:
                return 'ok' if code == 0 else 'failed'
            if time.monotonic() >= deadline:
                return 'timeout'
            if cancel_event is None:
                time.sleep(0.05)
            else:
                cancel_event.wait(0.05)
    finally:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=1)


def _publish(temp, cover):
    """硬链接创建是独占的；Windows 不支持硬链接的卷使用不覆盖 rename。"""
    try:
        os.link(temp, cover)
    except FileExistsError:
        return False
    except OSError:
        if os.name != 'nt':
            raise
        try:
            os.rename(temp, cover)
        except FileExistsError:
            return False
    return True


def generate_thumbnail(video_path, *, ffmpeg_path, validation, cancel_event=None, timeout=20.0):
    """返回 status/path/message/attempts；timeout 是整个生成操作的秒数上限。

    validation 必须是原下载管线的真实输出验证摘要。所有错误均保留视频和
    已有封面；只清理本调用创建的临时 JPEG，且不会发出网络请求。
    """
    attempts = []
    temp = None
    try:
        if _cancelled(cancel_event):
            return _result('cancelled', attempts, '缩略图生成已取消。')
        video = _local_file(video_path)
        stream = _video_stream(validation, video)
        if stream is None:
            return _result('skipped', attempts, '没有有效的已验证视频流，已跳过缩略图。')
        cover = video.with_suffix('.jpg')
        if cover == video:
            return _result('skipped', attempts, '封面路径与视频相同，已跳过以保留视频。')
        existing = _existing_cover(cover, attempts)
        if existing is not None:
            return existing
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            return _result('failed', attempts, '缩略图生成时限无效，视频已保留。')
        executable = _local_file(ffmpeg_path)
        deadline = time.monotonic() + min(float(timeout), MAX_TIMEOUT)
        descriptor, name = tempfile.mkstemp(prefix='.mediaflow-thumbnail-', suffix='.jpg', dir=video.parent)
        os.close(descriptor)
        temp = Path(name)
        mapping = '0:' + str(stream['index']) if stream.get('index') is not None else '0:v:0'
        for seconds in _seek_times(validation):
            if _cancelled(cancel_event):
                return _result('cancelled', attempts, '缩略图生成已取消。')
            if time.monotonic() >= deadline:
                return _result('failed', attempts, '缩略图生成超时，视频已保留。')
            seek = f'{seconds:.6f}'
            args = [str(executable), '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-max_alloc', '67108864', '-protocol_whitelist', 'file,pipe',
                    '-probesize', '8388608', '-analyzeduration', '10000000',
                    '-threads', '1', '-ss', seek, '-i', str(video), '-map', mapping,
                    '-an', '-sn', '-dn', '-frames:v', '1', '-filter_threads', '1',
                    '-vf', 'scale=480:270:force_original_aspect_ratio=decrease,setsar=1',
                    '-c:v', 'mjpeg', '-q:v', '3', '-threads', '1', '-fs', str(MAX_JPEG_BYTES),
                    '-f', 'image2', '-update', '1', str(temp)]
            # 清空本调用自己的文件，避免上次失败留下的图像被当成新成功结果。
            temp.write_bytes(b'')
            attempts.append(float(seek))
            status = _run_ffmpeg(args, cancel_event, deadline)
            if status == 'cancelled' or _cancelled(cancel_event):
                return _result('cancelled', attempts, '缩略图生成已取消。')
            if status == 'timeout':
                return _result('failed', attempts, '缩略图生成超时，视频已保留。')
            if status != 'ok' or not _valid_jpeg(temp):
                continue
            if _cancelled(cancel_event):
                return _result('cancelled', attempts, '缩略图生成已取消。')
            if _publish(temp, cover):
                return _result('generated', attempts, '已生成本地视频缩略图。', cover)
            return _existing_cover(cover, attempts) or _result('failed', attempts, '无法保存封面，视频已保留。')
        return _result('failed', attempts, '未能从视频生成有效 JPEG，视频已保留。')
    except Exception:
        return _result('failed', attempts, '本地缩略图生成失败，视频和已有封面已保留。')
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
