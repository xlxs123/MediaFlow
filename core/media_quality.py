"""同一媒体已确认版本的质量评分与推荐；不授予下载资格或执行请求。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import re

from core.discovery_models import MediaCandidate
from core.media_details import (_codec, _codec_kind, _codecs, _dimensions, _fps,
                                _integer, _number, enrich_media_details)
from core.media_groups import _flags, available_candidate


QUALITY_PREFERENCES = (('auto', '自动推荐'), ('highest', '最高画质'),
                       ('balanced', '平衡'), ('smallest', '最小文件'))


def _metadata(candidate):
    return candidate.metadata if isinstance(candidate.metadata, dict) else {}


def _size(candidate):
    metadata = _metadata(candidate)
    for value in (candidate.estimatedSize, metadata.get('estimated_size'),
                  metadata.get('filesize'), metadata.get('file_size')):
        number = _integer(value)
        if number:
            return number
    return None


def _resolution(candidate):
    dimensions = _dimensions((candidate.width, candidate.height))
    if dimensions:
        return dimensions
    metadata = _metadata(candidate)
    if 'declared_resolution' in metadata:
        return _dimensions(metadata['declared_resolution']) or (0, 0)
    dimensions = _dimensions(candidate.resolution)
    if dimensions:
        return dimensions
    return (_dimensions(metadata.get('resolution')) or
            _dimensions((metadata.get('width'), metadata.get('height'))) or (0, 0))


def _codec_details(candidate):
    metadata = _metadata(candidate)
    declared = _codecs(metadata.get('codecs'))
    video = _codec(candidate.videoCodec) or _codec(metadata.get('video_codec')) or declared.get('video')
    if not video and isinstance(metadata.get('codecs'), str):
        video = next((value for item in metadata['codecs'][:512].split(',')[:16]
                      if (value := _codec(item.strip())) and
                      value.lower() in ('h.264', 'h.265', 'avc')), None)
    audio = _codec(candidate.audioCodec) or _codec(metadata.get('audio_codec')) or declared.get('audio')
    return video, audio


def _confidence(candidate):
    value = _number(candidate.confidenceScore, 100)
    return value or 0


def _bitrate(candidate):
    metadata = _metadata(candidate)
    video = _integer(candidate.videoBitrate) or _integer(metadata.get('video_bitrate'))
    audio = _integer(candidate.audioBitrate) or _integer(metadata.get('audio_bitrate'))
    return ((video or 0) + (audio or 0) or
            _integer(metadata.get('declared_bandwidth')) or _integer(candidate.bandwidth))


def update_quality(candidate) -> None:
    """列出的正向权重最高合计 60；保持 0..100 量表，不另行归一化。

    已知体积/时长的平均总码率：480P 以下超过 8Mbps、720P 超过 16Mbps、
    1080P 超过 30Mbps 时，每超过阈值一倍扣 5 分（向上取整），最多扣 20。
    只对有实际分辨率、体积和时长的低分辨率版本扣分；未知信息不作惩罚。
    """
    metadata = _metadata(candidate)
    details = []

    def add(points, reason):
        details.append({'reason': reason, 'points': points})

    width, height = _resolution(candidate)
    edge = min(width, height)
    for threshold, points, label in ((2160, 40, '4K'), (1080, 30, '1080P'),
                                     (720, 20, '720P'), (480, 10, '480P')):
        if edge >= threshold:
            add(points, f'{label} 分辨率：{width}×{height}')
            break
    video, audio = _codec_details(candidate)
    codec = video.lower() if video else ''
    if codec.startswith(('av01', 'hvc1', 'hev1')) or codec in ('av1', 'h265', 'h.265', 'hevc'):
        add(10, 'AV1 / H265 编码')
    elif codec.startswith(('avc1', 'avc3')) or codec in ('h264', 'h.264', 'avc'):
        add(5, 'H264 编码')
    if audio and _codec_kind(audio) == 'audio' or metadata.get('has_audio') is True:
        add(10, '存在有效音频轨')

    size = _size(candidate)
    duration = _number(candidate.duration) or _number(metadata.get('detail_duration'))
    if edge and edge <= 1080 and size and duration:
        average = size * 8 / duration
        limit = 8_000_000 if edge <= 480 else 16_000_000 if edge <= 720 else 30_000_000
        if average > limit:
            penalty = min(20, math.ceil(5 * (average / limit - 1))) if math.isfinite(average) else 20
            add(-penalty, f'体积与分辨率不相称：平均 {average / 1_000_000:.1f} Mbps，'
                          f'参考上限 {limit / 1_000_000:g} Mbps')
    candidate.qualityScore = max(0, min(100, sum(item['points'] for item in details)))
    if isinstance(candidate.metadata, dict):
        metadata['quality_details'] = details
        metadata['combined_score'] = round(candidate.qualityScore * .6 + _confidence(candidate) * .4, 2)


def _available(candidate):
    if not isinstance(candidate, MediaCandidate) or _confidence(candidate) < 40:
        return False
    try:
        return available_candidate(candidate) and not any(_flags(candidate))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def _main_evidence(candidate):
    metadata = _metadata(candidate)
    evidence = metadata.get('confidence_evidence')
    evidence = evidence if isinstance(evidence, dict) else {}
    if evidence.get('current_player') is True:
        return True
    if 'current_player' not in evidence and metadata.get('dom_current') is True and metadata.get('dom_main') is True:
        return True
    if evidence.get('resolver_entry') is True or metadata.get('resolver_verified') is True:
        return True
    if ('Resolver' in candidate.sources and candidate.validated and candidate.manifest_text
            and candidate.media_type in ('HLS', 'DASH')):
        return True
    # 子清单的 current 证据由发现服务从实际父清单继承并写入评分明细。
    details = metadata.get('confidence_details')
    return isinstance(details, list) and any(
        isinstance(item, dict) and item.get('points') == 40 and
        isinstance(item.get('reason'), str) and 'video.currentSrc' in item['reason']
        for item in details[:32])


def _video_representation(rep):
    if not isinstance(rep, dict):
        return False
    mime = rep.get('mime_type') or rep.get('mimeType') or ''
    content = rep.get('content_type') or rep.get('contentType')
    return (bool(_dimensions((rep.get('width'), rep.get('height')))) or
            'video' in _codecs(rep.get('codecs')) or content == 'video' or
            isinstance(mime, str) and mime.lower().startswith('video/'))


def _selector(rep):
    dimensions = _dimensions((rep.get('width'), rep.get('height')))
    filters = ''.join(f'[{name}={value}]' for name, value in zip(('width', 'height'), dimensions or ()))
    video = _codecs(rep.get('codecs')).get('video')
    if video:
        filters += f'[vcodec^={video.partition(".")[0]}]'
    if not filters:
        return None
    return f'bestvideo{filters}+bestaudio/best{filters}/bestvideo{filters}'


def quality_candidates(group) -> list[MediaCandidate]:
    """只展开给定身份组；普通候选保留引用，DASH 版本使用独立副本。"""
    members = getattr(group, 'candidates', ())
    if not isinstance(members, (list, tuple)):
        return []
    result = []
    for candidate in members:
        if not isinstance(candidate, MediaCandidate) or candidate.confirmed is not True:
            continue
        metadata = _metadata(candidate)
        reps = metadata.get('representations') if candidate.media_type == 'DASH' else None
        expanded, seen = [], set()
        if isinstance(reps, list) and len(reps) <= 128:
            for rep in reps:
                if not _video_representation(rep):
                    continue
                selector = _selector(rep)
                if not selector:
                    continue
                dimensions = _dimensions((rep.get('width'), rep.get('height'))) or (0, 0)
                rep_id = rep.get('id')
                rep_id = str(rep_id) if type(rep_id) is int else rep_id
                if not isinstance(rep_id, str) or not re.fullmatch(r'[\w.+-]{1,128}', rep_id):
                    identity = repr((dimensions, rep.get('bandwidth'), rep.get('codecs'), rep.get('frame_rate')))
                    rep_id = 'representation-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
                if rep_id in seen:
                    continue
                seen.add(rep_id)
                variant = deepcopy(candidate)
                variant.id += ':dash:' + rep_id
                variant.resolution = dimensions
                variant.bandwidth = _integer(rep.get('bandwidth')) or 0
                for field in ('width', 'height', 'fps', 'videoCodec', 'audioCodec',
                              'videoBitrate', 'audioBitrate', 'estimatedSize', 'container'):
                    setattr(variant, field, None)
                for key in ('codecs', 'frame_rate', 'declared_resolution', 'declared_bandwidth',
                            'videoCodec', 'audioCodec', 'videoBitrate', 'audioBitrate',
                            'video_codec', 'audio_codec', 'video_bitrate', 'audio_bitrate',
                            'detail_sources', 'size_is_estimate', 'quality_details', 'combined_score',
                            'estimated_size', 'filesize', 'file_size'):
                    variant.metadata.pop(key, None)
                variant.metadata.update(format_selector=selector, representation_id=rep_id,
                                        declared_resolution=dimensions)
                # Preserve actual id for the DASH metadata matcher (generated ids are UI only).
                if rep.get('id') != rep_id:
                    variant.metadata['representation_id'] = rep.get('id')
                enrich_media_details(variant)
                update_quality(variant)
                expanded.append(variant)
        result.extend(expanded or [candidate])
    if any(candidate.manifest_type != 'Master' and _available(candidate) for candidate in result):
        result = [candidate for candidate in result if candidate.manifest_type != 'Master']
    return result


def recommend_candidate(candidates, preference='auto') -> MediaCandidate | None:
    """可靠性与当前正片优先，再在同一媒体版本中应用质量偏好。"""
    values = [candidate for candidate in candidates if isinstance(candidate, MediaCandidate)]
    for candidate in values:
        update_quality(candidate)
    values = [candidate for candidate in values if _available(candidate)]
    if not values:
        return None
    reliable = [candidate for candidate in values if _confidence(candidate) >= 70]
    values = reliable or values
    main = [candidate for candidate in values if _main_evidence(candidate)]
    values = main or values

    def common(candidate):
        width, height = _resolution(candidate)
        size = _size(candidate)
        return (_metadata(candidate).get('combined_score', 0), _confidence(candidate),
                min(width, height), width * height, -(size or math.inf))

    if preference == 'highest':
        def key(candidate):
            width, height = _resolution(candidate)
            return min(width, height), width * height, candidate.qualityScore, common(candidate)
        return max(values, key=key)
    if preference == 'balanced':
        def key(candidate):
            width, height = _resolution(candidate)
            edge = min(width, height)
            tier = 3 if 720 <= edge <= 1080 else 2 if 480 <= edge < 720 else 1 if edge > 1080 else 0
            return tier, common(candidate)
        return max(values, key=key)
    if preference == 'smallest':
        known = [candidate for candidate in values if _size(candidate)]
        if known:
            return min(known, key=lambda candidate: (_size(candidate), -common(candidate)[0]))
        def key(candidate):
            width, height = _resolution(candidate)
            bitrate = _bitrate(candidate)
            return bitrate is None, bitrate or math.inf, min(width, height) or math.inf, -common(candidate)[0]
        return min(values, key=key)
    return max(values, key=common)
