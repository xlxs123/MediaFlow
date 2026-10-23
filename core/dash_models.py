"""已接收的普通 DASH JSON 描述；不包含站点或下载器逻辑。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DashTrack:
    id: str
    url: str = field(repr=False)
    backup_urls: tuple[str, ...] = field(repr=False)
    bandwidth: int
    width: int = 0
    height: int = 0
    codecs: str = ''
    mime_type: str = ''
    initialization: str = ''
    index_range: str = ''
    frame_rate: str = ''
    role: str = 'video'
    drm: bool = False
    language: str = ''
    name: str = ''
    channels: int = 0
    sample_rate: int = 0
    default: bool = False


@dataclass(frozen=True)
class DashDescription:
    videos: tuple[DashTrack, ...]
    audios: tuple[DashTrack, ...]
    duration: float | None
    drm: bool = False
    classification: str = 'CLEAR_DASH'
