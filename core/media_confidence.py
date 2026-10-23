"""根据实际播放器、协议与响应证据评估媒体真实性；不改变下载资格。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import re


@dataclass
class ConfidenceRating:
    score: int
    reasons: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)


def _value(candidate, name, default=None):
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _finite_number(value):
    # bool 也是 int 的子类，但不是播放器给出的实际时长或尺寸。
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _normal_dimensions(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    width, height = (_finite_number(part) for part in value)
    if width is None or height is None:
        return None
    if not (64 <= width <= 16384 and 64 <= height <= 16384):
        return None
    if int(width) != width or int(height) != height:
        return None
    return int(width), int(height)


def confidence_tier(score):
    """自动选择包括 70 分，确认包括 40 分；异常输入视为不可靠。"""
    number = _finite_number(score)
    if number is None:
        return 'unreliable'
    if number >= 70:
        return 'auto'
    return 'confirm' if number >= 40 else 'unreliable'


def score_candidate(candidate, evidence=None):
    """纯评分，不访问网络、不修改候选，也不授予受保护资源下载资格。

    evidence 中的显式字段覆盖 metadata 的旧证据，避免 fresh snapshot 中
    的 False/None 被历史 True 替代。URL、签名参数和视频 poster 不参与评分。
    """
    metadata = _value(candidate, 'metadata', {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    evidence = evidence if isinstance(evidence, Mapping) else {}

    def observed(name, default=False):
        return evidence[name] if name in evidence else default

    associated = metadata.get('dom_associated') is True
    current = observed('current_player', metadata.get('dom_current') is True
                       and metadata.get('dom_main') is True) is True
    visible = observed('visible', metadata.get('dom_visible') if associated else None) is True
    hidden = observed('hidden', associated and (metadata.get('dom_hidden') is True
                      or metadata.get('dom_visible') is False)) is True
    verified_entry = observed('resolver_entry', metadata.get('resolver_verified') is True) is True

    role = next((value.strip().lower() for key in ('resource_role', 'role', 'resource_kind')
                 if isinstance((value := metadata.get(key)), str) and value.strip()), '')
    thumbnail = observed('thumbnail', metadata.get('is_thumbnail') is True
                         or role in ('thumbnail', 'poster', 'sprite', 'thumbnail_track',
                                     'thumbnail track')) is True
    preview = observed('preview', metadata.get('preview') is True
                       or metadata.get('is_preview') is True
                       or role in ('preview', 'trailer', 'sample')) is True
    content_type = _value(candidate, 'content_type', '')
    content_type = content_type.partition(';')[0].strip().lower() if isinstance(content_type, str) else ''
    if not re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]{1,128}', content_type):
        content_type = ''
    media_type = _value(candidate, 'media_type', '')
    # 已解码 HLS/DASH 的协议语义高于 PNG 传输容器；明确图片证据仍扣分。
    image = observed('image', role == 'image' or (media_type not in ('HLS', 'DASH')
                     and content_type.startswith('image/'))) is True

    details = []

    def add(points, reason):
        details.append({'points': points, 'reason': reason})

    if current:
        add(40, '当前主播放器 video.currentSrc')
    elif verified_entry:
        add(40, '当前播放页的媒体清单入口已验证')
    if visible:
        add(20, '对应播放器可见')

    duration = _finite_number(_value(candidate, 'duration'))
    if duration is not None and duration > 0:
        try:
            duration_label = f'{duration:.6g} 秒'
        except (ValueError, OverflowError):
            duration_label = ''
        add(20, '时长有效' + (f'：{duration_label}' if duration_label else ''))
        if duration > 5:
            add(10, '时长大于 5 秒')
    if content_type.startswith('video/'):
        add(10, '响应类型：' + content_type)
    dimensions = _normal_dimensions(_value(candidate, 'resolution'))
    if dimensions is not None:
        width, height = dimensions
        add(10, f'分辨率正常：{width}×{height}（{min(width, height)}P）')

    if hidden:
        add(-30, '对应 video 已隐藏')
    if duration is not None and 0 < duration < 3:
        add(-30, '时长小于 3 秒')
    if thumbnail:
        add(-50, '资源明确标记为缩略图')
    if preview:
        add(-50, '资源明确标记为预览')
    if image:
        add(-80, '响应内容为图片')

    # 必须先汇总全部证据后截断；逐项截断会改变惩罚结果。
    score = max(0, min(100, sum(item['points'] for item in details)))
    return ConfidenceRating(score, [item['reason'] for item in details], details)
