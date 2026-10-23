"""Windows 安全文件名与队列共用的线程安全占名器；不创建媒体占位文件。"""
from __future__ import annotations

import os
from pathlib import Path
import re
from threading import RLock


_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_DEVICE = re.compile(r'^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$', re.I)
_PARTIAL = re.compile(r'^(.*?)(?:\.part(?:-frag\d+)?|\.partial|\.ytdl)$', re.I)
DEFAULT_MAX_FILENAME = 180


def _units(text):
    return sum(2 if ord(character) > 0xffff else 1 for character in text)


def _truncate(text, limit):
    used = 0
    result = []
    for character in text:
        width = 2 if ord(character) > 0xffff else 1
        if used + width > limit:
            break
        result.append(character)
        used += width
    return ''.join(result)


def _truncate_stem(stem, limit):
    # 清晰度标签是文件名的一部分；截断标题或添加续号都不能截掉它。
    label = re.search(r' \[(?:\d{2,6}p|HLS|DASH|Direct)\]$', stem, re.I)
    if label and _units(label[0]) < limit:
        budget = limit - _units(label[0])
        title = _truncate(stem[:label.start()], budget).rstrip(' .') or _truncate('media', budget)
        return title + label[0]
    return _truncate(stem, limit)


def _split_filename(name):
    stem, separator, extension = name.rpartition('.')
    if separator and extension:
        return stem.rstrip(' .') or 'media', '.' + extension
    return name, ''


def sanitize_filename(desired_filename, max_length=DEFAULT_MAX_FILENAME) -> str:
    """清理为一个文件名组件；长度按 Windows UTF-16 单位计数并保留输出扩展名。"""
    if type(max_length) is not int or max_length < 1:
        raise ValueError('文件名长度必须是正整数。')
    limit = min(max_length, 255)
    name = _ILLEGAL.sub('_', os.fspath(desired_filename)).strip().rstrip(' .')
    if not name or name in ('.', '..'):
        name = 'media'
    stem, extension = _split_filename(name)
    # Windows 在首个点前识别设备名；CON.foo.mp4 也需要避开。
    if _DEVICE.fullmatch(stem.split('.', 1)[0].rstrip(' .')):
        stem = '_' + stem
    available = limit - _units(extension)
    if available < 1:
        raise ValueError('文件名长度不足以保留输出扩展名。')
    stem = _truncate_stem(stem, available).strip().rstrip(' .')
    if not stem:
        stem = _truncate('media', available)
    if _DEVICE.fullmatch(stem.split('.', 1)[0].rstrip(' .')):
        stem = _truncate('_' + stem, available)
    return stem + extension


def _path_key(path):
    return os.path.normcase(str(path)).casefold()


def _occupied(names):
    result = set()
    for name in names:
        name = name.casefold()
        result.add(name)
        partial = _PARTIAL.fullmatch(name)
        if partial:
            result.add(partial[1])
    return result


class FileNameAllocator:
    """同一个队列共享一个实例，在 allocate 返回前原子保留名称。

    reserved_names 接受目标目录中的文件名或完整文件路径。磁盘上存在的文件、
    目录以及 .part/.partial/.ytdl 续传状态均占名；release 仅释放内存占名。
    """

    def __init__(self):
        self._lock = RLock()
        self._reserved: dict[str, Path] = {}

    def allocate(self, directory, desired_filename, reserved_names=(), max_length=DEFAULT_MAX_FILENAME) -> Path:
        target_directory = Path(directory).expanduser().resolve()
        desired = sanitize_filename(desired_filename, max_length)
        stem, extension = _split_filename(desired)
        directory_key = _path_key(target_directory)
        with self._lock:
            if target_directory.exists() and not target_directory.is_dir():
                raise NotADirectoryError(str(target_directory))
            disk_names = [entry.name for entry in target_directory.iterdir()] if target_directory.exists() else []
            held_names = [path.name for path in self._reserved.values() if _path_key(path.parent) == directory_key]
            extra_names = []
            for value in reserved_names:
                reserved = Path(value)
                if reserved.parent == Path('.'):
                    extra_names.append(reserved.name)
                elif reserved.is_absolute() and _path_key(reserved.resolve().parent) == directory_key:
                    extra_names.append(reserved.name)
            occupied = _occupied(disk_names + held_names + extra_names)
            number = 0
            while True:
                suffix = f' ({number})' if number else ''
                budget = max_length - _units(extension) - _units(suffix)
                if budget < 1:
                    raise ValueError('文件名长度不足以添加同名续号。')
                numbered_stem = _truncate_stem(stem, budget).rstrip(' .') or 'm'
                name = numbered_stem + suffix + extension
                if name.casefold() not in occupied:
                    path = target_directory / name
                    self._reserved[_path_key(path)] = path
                    return path
                number += 1

    def reserve(self, path) -> Path:
        """接管已有任务输出名称，包括已存在的续传文件对应路径。"""
        path = Path(path).expanduser().resolve()
        with self._lock:
            self._reserved[_path_key(path)] = path
        return path

    def release(self, path) -> None:
        path = Path(path).expanduser().resolve()
        with self._lock:
            self._reserved.pop(_path_key(path), None)
