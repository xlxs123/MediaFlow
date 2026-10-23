"""Read-only, cancellable media-library checks. Call these functions off the UI thread."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time

from core.errors import MediaCheckCancelled, MediaCheckError
from core.output_validator import validate_output
from core.process_tree import NO_WINDOW, ProcessTree
from core.tool_manager import get_tool_manager


MAX_OUTPUT = 256 * 1024
MAX_STREAMS = 256
TEXT_SUBTITLE_CODECS = frozenset(('ass', 'ssa', 'subrip', 'srt', 'webvtt', 'mov_text', 'text', 'ttml'))


def file_status(path):
    """Check a regular local file without changing it or reading its entire contents."""
    result = {'file_state': 'inaccessible', 'size': 0, 'mtime_ns': 0, 'message': ''}
    try:
        raw = os.fspath(path)
        if not isinstance(raw, str) or not raw or '\x00' in raw or re.match(r'^\w+://', raw):
            result['message'] = '路径无效，请重新定位本地媒体文件。'
            return result
        media = Path(raw)
        info = media.stat()
        if not stat.S_ISREG(info.st_mode):
            result['message'] = '该路径不是普通文件，请重新定位媒体文件。'
            return result
        # A successful stat alone does not prove that the current account can read it.
        with media.open('rb') as stream:
            stream.read(1)
            opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            result['message'] = '该路径不是普通文件。'
            return result
        result.update(file_state='exists', size=opened.st_size, mtime_ns=opened.st_mtime_ns,
                      message='文件存在且可以读取。')
    except FileNotFoundError:
        result.update(file_state='missing', message='文件已移动或丢失，历史记录仍保留；可重新定位文件。')
    except PermissionError:
        result['message'] = '文件不可访问：当前账户没有读取权限，或文件正被其他程序独占。'
    except (OSError, ValueError, TypeError):
        result['message'] = '文件不可访问，请检查路径、磁盘连接和读取权限。'
    return result


class _Control:
    """Event-compatible deadline shared by tool discovery, ffprobe, and full decoding."""
    def __init__(self, event, timeout):
        self.event = event
        self.deadline = time.monotonic() + timeout

    def cancelled(self):
        return self.event is not None and self.event.is_set()

    def expired(self):
        return time.monotonic() >= self.deadline

    def is_set(self):
        return self.cancelled() or self.expired()

    def wait(self, seconds):
        remaining = max(0, min(seconds, self.deadline - time.monotonic()))
        if self.event is not None:
            self.event.wait(remaining)
        elif remaining:
            time.sleep(remaining)
        return self.is_set()

    def check(self):
        if self.cancelled():
            raise _Incomplete('验证已取消，原文件和先前验证结果保留。')
        if self.expired():
            raise _Incomplete('媒体验证超时，尚未完成全文件检查；原文件保留，可稍后重试。')


class _Incomplete(Exception):
    pass


class _InvalidMedia(Exception):
    pass


def _stop(process, tree):
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                pass
        if tree is not None:
            tree.terminate()
        elif process.poll() is None:
            process.kill()
            process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        if tree is not None:
            tree.close()


def _run(args, control):
    """Bound process launch, execution, and captured output, including a late launch."""
    control.check()
    stdout, stderr = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    ready, lock = threading.Event(), threading.Lock()
    state = {'abandoned': False}

    def start():
        process, tree = None, None
        try:
            process = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, creationflags=NO_WINDOW,
                                       start_new_session=os.name != 'nt')
            tree = ProcessTree(process)
        except Exception:
            state['error'] = True
        with lock:
            abandoned = state['abandoned']
            state.update(process=process, tree=tree)
            ready.set()
        if abandoned:
            if process is not None:
                _stop(process, tree)
            stdout.close()
            stderr.close()

    threading.Thread(target=start, name='MediaFlow-library-process', daemon=True).start()
    launch_deadline = min(control.deadline, time.monotonic() + 3)
    while not ready.wait(0.03):
        if control.is_set() or time.monotonic() >= launch_deadline:
            with lock:
                if ready.is_set():
                    break
                state['abandoned'] = True
            control.check()
            raise _Incomplete('媒体检查工具启动超时，请检查安全软件或重新检测工具。')
    process, tree = state['process'], state['tree']
    try:
        if state.get('error') or process is None or tree is None:
            raise _Incomplete('媒体检查工具启动失败，请在工具设置中重新检测 FFmpeg 和 ffprobe。')
        while process.poll() is None:
            control.check()
            if os.fstat(stdout.fileno()).st_size > MAX_OUTPUT or os.fstat(stderr.fileno()).st_size > MAX_OUTPUT:
                raise _Incomplete('媒体检查输出超过限制，未能完成可靠验证；请检查工具后重试。')
            control.wait(0.03)
        control.check()
        if os.fstat(stdout.fileno()).st_size > MAX_OUTPUT or os.fstat(stderr.fileno()).st_size > MAX_OUTPUT:
            raise _Incomplete('媒体检查输出超过限制，未能完成可靠验证；请检查工具后重试。')
        stdout.seek(0)
        stderr.seek(0)
        return process.returncode, stdout.read(MAX_OUTPUT), stderr.read(MAX_OUTPUT)
    finally:
        if process is not None:
            if process.poll() is None or tree is None:
                _stop(process, tree)
            else:
                tree.close()
        stdout.close()
        stderr.close()


def _stream_details(path, ffprobe, control):
    args = [ffprobe, '-v', 'error', '-max_alloc', '67108864', '-protocol_whitelist', 'file,pipe',
            '-show_entries', 'stream=index,codec_type,time_base,nb_frames:stream_disposition=attached_pic',
            '-of', 'json', '-i', str(path)]
    code, output, errors = _run(args, control)
    if code or errors.strip():
        raise _InvalidMedia('ffprobe 无法完整读取媒体流信息，文件可能损坏或已发生变化。')
    try:
        raw = json.loads(output)
        streams = raw['streams']
        if not isinstance(streams, list) or len(streams) > MAX_STREAMS:
            raise ValueError
        result = []
        for stream in streams:
            if not isinstance(stream, dict):
                raise ValueError
            if stream.get('codec_type') not in ('video', 'audio'):
                continue
            if (stream.get('disposition') or {}).get('attached_pic') in (1, True, '1'):
                continue
            index = stream.get('index')
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < MAX_STREAMS:
                raise ValueError
            item = {'index': index, 'codec_type': stream['codec_type']}
            base = stream.get('time_base', '')
            match = re.fullmatch(r'([1-9]\d{0,9})/([1-9]\d{0,9})', base) if isinstance(base, str) else None
            if match:
                item['time_base'] = ':'.join(match.groups())
            frames = stream.get('nb_frames', '')
            if isinstance(frames, (str, int)) and re.fullmatch(r'[1-9]\d{0,11}', str(frames)):
                item['nb_frames'] = int(frames)
            result.append(item)
        if not result:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise _Incomplete('ffprobe 返回的媒体流信息无效，请重新检测工具后重试。') from None


def _full_decode(path, ffmpeg, streams, control):
    args = [ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-max_alloc', '67108864',
            '-protocol_whitelist', 'file,pipe', '-i', str(path)]
    for stream in streams:
        args.extend(['-map', '0:' + str(stream['index'])])
    counts = {'video': 0, 'audio': 0}
    for stream in streams:
        kind = stream['codec_type']
        output_index = counts[kind]
        counts[kind] += 1
        if kind == 'video':
            if 'time_base' not in stream:
                raise _Incomplete('无法取得视频的真实时间基，尚未执行可靠的全文件解码检查。')
            # The null muxer's default frame-rate time base rounds VFR timestamps.
            # Preserve each input time base to avoid false duplicate-DTS errors.
            args.extend([f'-enc_time_base:v:{output_index}', stream['time_base'],
                         f'-fps_mode:v:{output_index}', 'passthrough'])
        elif 'time_base' in stream:
            args.extend([f'-enc_time_base:a:{output_index}', stream['time_base']])
    args.extend(['-progress', 'pipe:1', '-f', 'null', '-'])
    started = time.monotonic()
    code, output, errors = _run(args, control)
    if code or errors.strip():
        # Raw decoder messages can include filenames or embedded media URLs.
        text = errors.decode('utf-8', errors='replace').lower()
        if any(message in text for message in ('unknown decoder', 'decoder not found',
                'no decoder found', 'automatic encoder selection failed', 'error selecting an encoder',
                'unrecognized option', 'option not found', 'cannot allocate memory',
                'resource temporarily unavailable')):
            raise _Incomplete('当前 FFmpeg 缺少所需解码能力或可用资源，尚未完成全文件检查；请重新检测工具后重试。')
        if 'not on whitelist' in text:
            raise _Incomplete('所选文件依赖外部媒体资源，无法作为独立本地文件完成检查；请重新分析来源并下载完整媒体。')
        if 'permission denied' in text:
            raise _Incomplete('FFmpeg 无法读取媒体文件，请检查文件读取权限或独占占用后重试。')
        reason = ('音频或视频数据损坏，解码时发现无效数据。' if any(word in text for word in
                  ('invalid data', 'corrupt', 'error while decoding', 'decode error')) else
                  f'FFmpeg 全文件读取失败（退出码 {code}），媒体可能损坏或包含不可读取的轨道。')
        raise _InvalidMedia(reason)
    progress = {}
    for line in output.decode('ascii', errors='replace').splitlines():
        key, sep, value = line.partition('=')
        if sep and key in ('frame', 'drop_frames', 'progress'):
            progress[key] = value.strip()
    if progress.get('progress') != 'end':
        raise _Incomplete('FFmpeg 未确认全文件读取完成，验证结果尚未确定。')
    result = {'status': 'passed', 'return_code': code, 'streams_read': len(streams),
              'progress_end': True, 'duration_seconds': round(time.monotonic() - started, 3)}
    videos = [stream for stream in streams if stream['codec_type'] == 'video']
    frame_text = progress.get('frame', '')
    if videos and frame_text.isdigit():
        result['decoded_frames'] = int(frame_text)
        if len(videos) == 1 and 'nb_frames' in videos[0]:
            result['expected_frames'] = videos[0]['nb_frames']
            if result['decoded_frames'] != result['expected_frames']:
                raise _InvalidMedia('实际解码帧数与容器声明不一致，文件可能不完整。')
    if progress.get('drop_frames', '').isdigit():
        result['dropped_frames'] = int(progress['drop_frames'])
        if result['dropped_frames']:
            raise _InvalidMedia('全文件读取时丢失视频帧，媒体可能损坏。')
    return result


def _metadata(summary):
    videos = summary.get('video_streams') or []
    audios = summary.get('audio_streams') or []
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    return {'duration': summary.get('duration', 0), 'width': video.get('width', 0),
            'height': video.get('height', 0), 'container': summary.get('format_name', ''),
            'video_codec': video.get('codec_name', ''), 'audio_codec': audio.get('codec_name', ''),
            'media_type': 'video' if videos else 'audio' if audios else 'unknown'}


def check_subtitle_tracks(path, streams, ffmpeg, ffprobe, control):
    """单独读取实际内嵌字幕；图片/未知编码只查数据包，不冒充完整解码。

    与调用方共享取消事件和总时限。这里只返回安全状态，不返回解码原文、
    字幕内容或工具原始错误；incomplete 仅供调用方保留未确定状态。
    """
    if not isinstance(streams, list) or len(streams) > 128:
        raise _Incomplete('字幕轨道信息无效或超过检查数量限制。')
    report = {'status': 'none' if not streams else 'not_checked', 'count': len(streams), 'streams': []}
    if not streams:
        return report
    for stream in streams:
        index = stream.get('index') if isinstance(stream, dict) else None
        if type(index) is not int or not 0 <= index < MAX_STREAMS:
            raise _Incomplete('字幕轨缺少可靠的流索引，未执行逐轨读取。')
        report['streams'].append({'index': index, 'status': 'not_checked', 'method': 'none'})
    if len({item['index'] for item in report['streams']}) != len(streams):
        raise _Incomplete('字幕轨流索引重复，未执行逐轨读取。')
    try:
        args = [ffprobe, '-v', 'error', '-max_alloc', '67108864', '-protocol_whitelist', 'file,pipe',
                '-select_streams', 's', '-count_packets', '-show_entries', 'stream=index,nb_read_packets',
                '-of', 'json', '-i', str(path)]
        code, output, errors = _run(args, control)
        if code or errors.strip():
            message = errors.decode('utf-8', errors='replace').lower()
            if any(part in message for part in ('permission denied', 'not on whitelist', 'cannot allocate memory',
                    'resource temporarily unavailable', 'option not found', 'unrecognized option',
                    'unknown decoder', 'decoder not found', 'no decoder found')):
                raise _Incomplete('当前工具或文件访问条件不允许完整读取字幕数据包，请检查权限和工具后重试。')
            report.update(status='failed', reason='ffprobe 无法完整读取字幕数据包，字幕轨可能损坏。')
            return report
        try:
            raw = json.loads(output)
            found = raw['streams']
            if not isinstance(found, list) or len(found) > MAX_STREAMS:
                raise ValueError
            packets = {}
            for item in found:
                if not isinstance(item, dict) or type(item.get('index')) is not int:
                    raise ValueError
                count = str(item.get('nb_read_packets', ''))
                if not re.fullmatch(r'\d{1,16}', count) or item['index'] in packets:
                    raise ValueError
                packets[item['index']] = int(count)
            if any(item['index'] not in packets for item in report['streams']):
                raise ValueError
        except (TypeError, ValueError, KeyError, UnicodeError):
            raise _Incomplete('ffprobe 未提供可靠的字幕包计数，字幕尚未通过读取检查。') from None
        for stream, item in zip(streams, report['streams']):
            control.check()
            item['method'] = 'packet_read'
            if packets[item['index']] <= 0:
                item.update(status='failed', reason='该字幕轨没有可读取的数据包。')
                report.update(status='failed', reason='字幕轨没有可读取的数据包。')
                return report
            if stream.get('codec_name') not in TEXT_SUBTITLE_CODECS:
                item['reason'] = '仅完成字幕数据包读取；该编码尚未进行完整字幕解码。'
                continue
            args = [ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-max_alloc', '67108864',
                    '-protocol_whitelist', 'file,pipe', '-i', str(path),
                    '-map', '0:' + str(item['index']), '-c:s', 'ass',
                    '-progress', 'pipe:1', '-f', 'null', '-']
            item['method'] = 'text_decode'
            code, output, errors = _run(args, control)
            if code or errors.strip():
                message = errors.decode('utf-8', errors='replace').lower()
                if any(part in message for part in ('unknown decoder', 'decoder not found', 'no decoder found',
                    'unknown encoder', 'encoder not found', 'error selecting an encoder', 'unrecognized option',
                    'option not found', 'permission denied', 'not on whitelist', 'cannot allocate memory',
                    'resource temporarily unavailable')):
                    raise _Incomplete('当前 FFmpeg 无法完成该字幕编码的读取，字幕尚未通过完整解码检查。')
                item.update(status='failed', reason='文本字幕完整解码失败，字幕可能损坏。')
                report.update(status='failed', reason='文本字幕完整解码失败，字幕可能损坏。')
                return report
            progress = {}
            for line in output.decode('ascii', errors='replace').splitlines():
                key, separator, value = line.partition('=')
                if separator:
                    progress[key] = value.strip()
            if progress.get('progress', '').strip() != 'end':
                raise _Incomplete('FFmpeg 未确认文本字幕读取完成，字幕验证结果尚未确定。')
            item['status'] = 'passed'
        if all(item['status'] == 'passed' for item in report['streams']):
            report['status'] = 'passed'
        else:
            report['reason'] = '部分字幕仅完成数据包读取，未完成完整字幕解码。'
        return report
    except _Incomplete as error:
        report.update(status='not_checked', reason=str(error), incomplete=True)
        return report


def revalidate_media(path, ffmpeg_path='', ffprobe_path='', cancel_event=None, timeout=300):
    """Return a safe result; missing tools, interruption, and timeouts never mean corruption."""
    result = {**file_status(path), 'validation_state': 'unverified', 'validation_reason': '',
              'validation': {}, 'duration': 0, 'width': 0, 'height': 0, 'container': '',
              'video_codec': '', 'audio_codec': '', 'media_type': 'unknown'}
    if result['file_state'] != 'exists':
        result['validation_reason'] = result['message']
        return result
    try:
        seconds = float(timeout)
        if not math.isfinite(seconds) or seconds <= 0:
            raise _Incomplete('验证超时时间无效，请重新发起验证。')
        control = _Control(cancel_event, min(seconds, 3600))
        control.check()
        manager = get_tool_manager()
        probe = manager.detect('ffprobe', str(ffprobe_path or ''), mode='custom' if ffprobe_path else 'auto',
                               sibling=str(ffmpeg_path or ''), cancel_event=control, refresh=False)
        control.check()
        if not probe.found:
            raise _Incomplete('ffprobe 不可用：' + (probe.error or '请在工具设置中重新检测。'))
        summary = validate_output(path, ffmpeg_path=ffmpeg_path, ffprobe_path=probe.path,
                                  cancel_event=control)
        result['validation'] = summary
        result.update(_metadata(summary))
        duration = summary.get('duration', 0)
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise _InvalidMedia('媒体时长无效，尚未通过完整性验证。')
        control.check()
        encoder = manager.detect('ffmpeg', str(ffmpeg_path or ''), mode='custom' if ffmpeg_path else 'auto',
                                 sibling=probe.path, cancel_event=control, refresh=False)
        control.check()
        if not encoder.found:
            raise _Incomplete('FFmpeg 不可用，已完成容器探测，但未完成全文件读取：' +
                              (encoder.error or '请在工具设置中重新检测。'))
        streams = _stream_details(path, probe.path, control)
        summary['full_decode'] = _full_decode(path, encoder.path, streams, control)
        summary['subtitle_check'] = check_subtitle_tracks(path, summary.get('subtitle_streams', []),
                                                        encoder.path, probe.path, control)
        subtitle_check = summary['subtitle_check']
        if subtitle_check['status'] == 'failed':
            raise _InvalidMedia(subtitle_check['reason'])
        if subtitle_check.get('incomplete'):
            raise _Incomplete(subtitle_check['reason'])
        control.check()
        final_status = file_status(path)
        if final_status['file_state'] != 'exists':
            result.update(final_status)
            raise _Incomplete(final_status['message'])
        if (final_status['size'], final_status['mtime_ns']) != (result['size'], result['mtime_ns']):
            result.update(final_status, file_changed=True)
            raise _Incomplete('文件在验证过程中发生变化，请在写入结束后重新验证。')
        summary['mtime_ns'] = final_status['mtime_ns']
        summary['checked_at'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        result.update(validation_state='valid', validation_reason='容器、媒体流、时长和全部音视频轨完整读取均已通过验证。')
        if subtitle_check['status'] == 'passed':
            result['validation_reason'] += ' 全部文本字幕轨已单独完整解码通过。'
        elif subtitle_check['status'] == 'not_checked':
            result['validation_reason'] += ' 部分字幕仅完成数据包读取，尚未完整解码。'
    except MediaCheckCancelled:
        result['validation_reason'] = ('媒体验证超时，尚未完成全文件检查；原文件保留，可稍后重试。'
                                       if 'control' in locals() and control.expired() and not control.cancelled()
                                       else '验证已取消，原文件和先前验证结果保留。')
    except MediaCheckError as error:
        message = str(error)
        incomplete = error.code == 'OUTPUT_PROBE_UNAVAILABLE' or any(word in message for word in
                         ('超时', '无法启动', '无效探测结果', '读取输出探测结果', '超过限制'))
        result.update(validation_state='unverified' if incomplete else 'failed', validation_reason=message)
        status = file_status(path)
        if status['file_state'] != 'exists':
            result.update(status, validation_state='unverified', validation_reason=status['message'])
    except _Incomplete as error:
        result['validation_reason'] = str(error)
    except _InvalidMedia as error:
        result.update(validation_state='failed', validation_reason=str(error))
    except (OSError, ValueError, TypeError, OverflowError):
        result['validation_reason'] = '无法完成媒体检查，请检查本地文件和工具设置后重试；原文件保留。'
        status = file_status(path)
        if status['file_state'] != 'exists':
            result.update(status, validation_reason=status['message'])
    except Exception:
        # A background service must always deliver an actionable result to the UI.
        result['validation_reason'] = '媒体检查遇到异常，尚未完成验证；请重新检测工具后重试。'
    # A file may be moved or replaced while a decoder is reporting an error.
    current = file_status(path)
    if current['file_state'] != 'exists':
        result.update(current, validation_state='unverified', validation_reason=current['message'])
    elif (current['size'], current['mtime_ns']) != (result['size'], result['mtime_ns']):
        result.update(current, validation_state='unverified', file_changed=True,
                      validation_reason='文件在验证过程中发生变化，请在写入结束后重新验证。')
    return result
