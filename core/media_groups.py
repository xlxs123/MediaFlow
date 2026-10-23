"""把已发现候选归为独立媒体；只使用协议关系和现有元数据，不执行请求。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from core.discovery_models import MediaCandidate


# 只用于内部身份比较。实际 RequestContext 与签名 URL 始终保持原样。
_DYNAMIC_QUERY = frozenset(('token', 'sign', 'signature', 'expires', 'expiry', 'exp',
                            'access_token', 'auth_token', 'auth', 't'))
_ID_FIELDS = ('canonical_media_id', 'video_id', 'api_video_id', 'media_id', 'videoId', 'mediaId')
_TITLE_FIELDS = ('video_title', 'media_title', 'title', 'videoTitle', 'name')
_AD_WORDS = re.compile(r'(?<![a-z0-9])(?:vast|advertisement|ad-provider|adprovider|preroll|midroll|tracker|ads?)(?![a-z0-9])', re.I)
_PREVIEW_WORDS = re.compile(r'(?<![a-z0-9])(?:preview|trailer|sample)(?![a-z0-9])', re.I)
_THUMB_WORDS = re.compile(r'(?<![a-z0-9])(?:thumbnail|thumbs?|poster|sprite)(?![a-z0-9])', re.I)
_QUALITY_TOKEN = re.compile(r'(?<!\d)(?:\d{3,4}p|\d{3,4}x\d{3,4}|[248]k)(?![a-z0-9])', re.I)
_NON_MEDIA_ROLES = frozenset(('thumbnail', 'poster', 'sprite', 'image', 'key', 'subtitle',
                              'media segment', 'partial segment', 'preload segment',
                              'dash media segment', 'initialization segment', 'auxiliary rendition'))
_CONTEXT_METADATA = frozenset(('error_code', 'probe_error', 'discovered_at', 'last_sequence',
                               'timestamp', 'last_seen', 'response_headers', 'timing', 'expires_hint'))


@dataclass
class MediaGroup:
    id: str
    title: str
    candidates: list[MediaCandidate] = field(repr=False)
    selected_candidate: MediaCandidate | None = field(repr=False)
    duration: float | None = None
    thumbnail: str = ''
    source_page: str = ''
    confidence: int = 0
    selected: bool = True
    is_ad: bool = False
    is_preview: bool = False


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    if isinstance(value, str):
        return re.sub(r'[\x00-\x1f\x7f]', '', value).strip()[:4096]
    if type(value) in (int, float):
        try:
            return str(value) if math.isfinite(value) else ''
        except OverflowError:
            return ''
    return ''


def _number(value):
    if type(value) not in (int, float, str):
        return 0.0
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _true(value):
    return value is True or type(value) in (int, float) and value == 1 or isinstance(value, str) and value.lower() in ('true', 'yes', '1')


def _metadata(candidate):
    return _dict(candidate.metadata)


def _metadata_values(candidate):
    metadata = _metadata(candidate)
    return [metadata] + [_dict(metadata.get(key)) for key in
                        ('media_metadata', 'video_metadata', 'probe', 'probe_result', 'video', 'info')]


def _first(candidate, keys):
    return next((_text(values.get(key)) for values in _metadata_values(candidate) for key in keys
                 if _text(values.get(key))), '')


def _url_key(url):
    try:
        parsed = urlsplit(_text(url))
        if parsed.scheme.lower() not in ('https', 'http') or not parsed.netloc:
            return ''
        query = tuple(sorted((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                             if key.lower() not in _DYNAMIC_QUERY))
        return json.dumps((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query), ensure_ascii=False)
    except ValueError:
        return ''


def _page(candidate, fallback=''):
    return _first(candidate, ('source_page', 'page_url', 'source_page_url')) or fallback or candidate.request_context.referer


def _scope(candidate, fallback=''):
    page = _url_key(_page(candidate, fallback))
    resolver = _first(candidate, ('resolver_id',))
    if not page:
        page = urlsplit(candidate.final_url).netloc.lower()
    return resolver, page


def _ids(candidate):
    return {_text(values.get(key)) for values in _metadata_values(candidate) for key in _ID_FIELDS
            if _text(values.get(key))}


def _id_conflict(left, right):
    def identities(candidate):
        aliases = {'videoId': 'video_id', 'api_video_id': 'video_id', 'mediaId': 'media_id'}
        return {aliases.get(key, key): _text(values.get(key)) for values in reversed(_metadata_values(candidate))
                for key in _ID_FIELDS if _text(values.get(key))}
    left_values, right_values = identities(left), identities(right)
    left_ids, right_ids = _ids(left), _ids(right)
    return (any(left_values[key] != right_values[key] for key in left_values.keys() & right_values.keys())
            or bool(left_ids and right_ids and not left_ids.intersection(right_ids)))


def _path_text(candidate):
    parsed = urlsplit(candidate.final_url)
    return unquote(parsed.netloc + parsed.path)


def _flags(candidate):
    metadata = _metadata(candidate)
    role = _first(candidate, ('resource_role', 'role', 'resource_kind')).lower()
    ad = candidate.is_ad or any(_true(metadata.get(key)) for key in
                               ('is_ad', 'vast_ad', 'ad_initiator', 'advertisement'))
    verified_main = (metadata.get('resolver_verified') is True and
                     metadata.get('resolver_route') == 'api_redirect')
    if not verified_main:
        ad = ad or bool(_AD_WORDS.search(_path_text(candidate) + ' ' + _text(candidate.source_type)))
    preview = any(_true(metadata.get(key)) for key in ('preview', 'is_preview', 'trailer', 'sample'))
    preview = preview or role in ('preview', 'trailer', 'sample')
    if not verified_main:
        preview = preview or bool(_PREVIEW_WORDS.search(_path_text(candidate)))
    thumbnail = _true(metadata.get('is_thumbnail')) or role in _NON_MEDIA_ROLES
    # 已解析 HLS/DASH 入口的协议语义优先；不能因为真实入口是 .png 就丢弃。
    if candidate.media_type not in ('HLS', 'DASH'):
        thumbnail = thumbnail or bool(_THUMB_WORDS.search(_path_text(candidate)))
        thumbnail = thumbnail or _text(candidate.content_type).lower().startswith('image/')
    return bool(ad), bool(preview), bool(thumbnail)


def candidate_unavailable_reasons(candidate: MediaCandidate) -> list[str]:
    """实际下载资格与推荐分数独立；只按协议、响应及验证证据阻止下载。"""
    metadata = _metadata(candidate)
    reasons = []
    try:
        parsed = urlsplit(candidate.final_url)
        valid_url = parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)
    except (ValueError, AttributeError):
        valid_url = False
    if not valid_url:
        reasons.append('缺少有效媒体 URL')
    if candidate.drm is True:
        reasons.append('DRM 限制：受保护媒体不可下载')
    elif candidate.media_type == 'HLS' and candidate.classification in ('HLS_SAMPLE_AES', 'HLS_ENCRYPTION_UNKNOWN'):
        reasons.append('下载器无法处理该 HLS 加密方式；未将其判定为 DRM')
    if not candidate.context_complete:
        reasons.append('请求上下文不完整，未捕获到可复现的实际请求')
    # 只有解析/验证给出了必要头证据，才要求该头；不凭空要求 Origin 或 Cookie。
    required = metadata.get('required_headers', ())
    if isinstance(required, (list, tuple)):
        missing = [name for name in required if isinstance(name, str)
                   and re.fullmatch(r'[A-Za-z0-9-]{1,64}', name)
                   and not candidate.request_context.headers.get(name.lower())]
        if missing:
            reasons.append('缺少必要请求 Header：' + '、'.join(missing))
    role = _first(candidate, ('resource_role', 'role', 'resource_kind')).lower()
    fragment = role in _NON_MEDIA_ROLES and 'segment' in role or metadata.get('fragment_only') is True
    if fragment:
        reasons.append('只有媒体分片，未关联到完整 M3U8 / MPD；请继续播放或深度查找')
    elif _flags(candidate)[2]:
        reasons.append('资源不是完整媒体：图片、密钥、字幕或辅助轨道')
    if _flags(candidate)[0]:
        reasons.append('检测到广告资源，当前不作为正片下载')
    if candidate.http_status not in (0, 304) and not 200 <= candidate.http_status < 300:
        reasons.append(f'HTTP {candidate.http_status}：' + ('访问被拒绝，请检查请求上下文或过期签名' if candidate.http_status == 403
                       else '需要有效登录状态' if candidate.http_status == 401 else '媒体请求失败'))
    code = _text(metadata.get('error_code'))
    error = _text(metadata.get('probe_error'))
    if code or error:
        reasons.append(('检测失败：' + code + (' · ' if error else '') + error)[:600])
    if candidate.media_type not in ('HLS', 'DASH', 'Direct') and not fragment:
        reasons.append('下载器无法处理该资源类型，尚未确认完整媒体协议')
    if not candidate.confirmed and not fragment:
        reasons.append('未通过媒体类型验证，请继续播放或重新检测')
    return list(dict.fromkeys(reasons))


def available_candidate(candidate: MediaCandidate) -> bool:
    """低可信度仅影响推荐与确认，不阻止有效完整媒体的手动下载。"""
    return not candidate_unavailable_reasons(candidate)


def _dimensions(value):
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return int(_number(value[0])), int(_number(value[1]))
    if isinstance(value, str):
        match = re.fullmatch(r'\s*(\d+)\s*[xX]\s*(\d+)\s*', value)
        if match:
            return int(match[1]), int(match[2])
        match = re.fullmatch(r'\s*(\d{3,4})p\s*', value, re.I)
        if match:
            return 0, int(match[1])
    return 0, 0


def quality_key(candidate: MediaCandidate) -> tuple[int, int, int, float]:
    """实际分辨率、带宽优先；元数据/探测其次，文件名质量仅作最后提示。"""
    width, height = _dimensions(candidate.resolution)
    values = _metadata_values(candidate)
    if not height:
        for metadata in values:
            dimensions = _dimensions(metadata.get('resolution') or metadata.get('RESOLUTION'))
            if not dimensions[1]:
                dimensions = int(_number(metadata.get('width'))), int(_number(metadata.get('height')))
            if dimensions[1]:
                width, height = dimensions
                break
    bandwidth = int(_number(candidate.bandwidth)) or next((int(_number(value.get('bandwidth') or value.get('BANDWIDTH')))
                                                          for value in values if _number(value.get('bandwidth') or value.get('BANDWIDTH'))), 0)
    if not height:
        label = _first(candidate, ('quality', 'quality_label', 'resolution_label')) or unquote(urlsplit(candidate.final_url).path)
        match = re.search(r'(?<!\d)(\d{3,4})p(?![a-z0-9])', label, re.I)
        if match:
            height = int(match[1])
        else:
            match = re.search(r'(?<!\d)(\d{3,4})x(\d{3,4})(?!\d)', label, re.I)
            if match:
                width, height = int(match[1]), int(match[2])
            elif re.search(r'(?<![a-z0-9])4k(?![a-z0-9])', label, re.I):
                height = 2160
            elif re.search(r'(?<![a-z0-9])8k(?![a-z0-9])', label, re.I):
                height = 4320
            elif re.search(r'(?<![a-z0-9])2k(?![a-z0-9])', label, re.I):
                height = 1440
    frame_rate = next((_number(value.get('frame_rate') or value.get('frameRate')) for value in values
                       if _number(value.get('frame_rate') or value.get('frameRate'))), 0.0)
    return height, width, bandwidth, frame_rate


def _ordered(candidates):
    # 实际验证过的 Variant 优于同质量的 Master；后者仍可作为同视频回退入口。
    return sorted(candidates, key=lambda candidate: (candidate.confidence, quality_key(candidate), candidate.manifest_type != 'Master',
                                                     candidate.score, _freshness(candidate)), reverse=True)


def ordered_candidates(group: MediaGroup) -> list[MediaCandidate]:
    return _ordered(candidate for candidate in group.candidates if available_candidate(candidate))


def _freshness(candidate):
    metadata = _metadata(candidate)
    return (_number(metadata.get('discovered_at') or metadata.get('timestamp') or metadata.get('last_seen')),
            _number(metadata.get('last_sequence') or metadata.get('created_sequence')))


def _conflict(left, right):
    return (_flags(left)[:2] != _flags(right)[:2]
            or _id_conflict(left, right))


def _resource_key(candidate):
    context = candidate.request_context
    return context.method, hashlib.sha256(context.body or b'').hexdigest(), _url_key(candidate.final_url)


def _exact_resource_key(candidate):
    context = candidate.request_context
    return context.method, hashlib.sha256(context.body or b'').hexdigest(), candidate.final_url


def _same_resource(left, right, fallback):
    if _id_conflict(left, right) or _resource_key(left) != _resource_key(right):
        return False
    if left.final_url == right.final_url:
        return True
    # token 有时本身选择内容；只删签名并不能证明是同一个视频。
    page = _page(left, fallback)
    if not page or _scope(left, fallback) != _scope(right, fallback):
        return False
    if _ids(left).intersection(_ids(right)):
        return True
    dom_left = _first(left, ('dom_video_id', 'dom_element_id', 'video_element_id'))
    dom_right = _first(right, ('dom_video_id', 'dom_element_id', 'video_element_id'))
    if dom_left and dom_left == dom_right and left.frame_id == right.frame_id:
        return True
    fingerprint = _first(left, ('media_fingerprint', 'fingerprint'))
    return bool(fingerprint and fingerprint == _first(right, ('media_fingerprint', 'fingerprint')))


def deduplicate_candidates(candidates, *, source_page='') -> list[MediaCandidate]:
    """消除同资源重复发现并保留最新有效上下文；不同清晰度仍交给分组。"""
    result = []
    for position, candidate in enumerate(candidates):
        if not isinstance(candidate, MediaCandidate):
            continue
        candidate = deepcopy(candidate)
        duplicate = next((index for index, old in enumerate(result)
                          if _same_resource(old, candidate, source_page)), None)
        if duplicate is None:
            result.append(candidate)
            continue
        old = result[duplicate]
        preference = lambda item, order: (available_candidate(item), item.context_complete, item.confirmed,
                                          _freshness(item), order)
        selected, other = (candidate, old) if preference(candidate, position) >= preference(old, -1) else (old, candidate)
        selected.sources.update(other.sources)
        selected.score = max(selected.score, other.score)
        selected.confirmed = selected.confirmed or other.confirmed
        if quality_key(other) > quality_key(selected):
            selected.resolution = other.resolution
            selected.bandwidth = other.bandwidth
        selected.duration = max(_number(selected.duration), _number(other.duration)) or None
        selected.metadata = {**{key: value for key, value in _metadata(other).items() if key not in _CONTEXT_METADATA},
                             **_metadata(selected)}
        if _flags(other)[0]:
            selected.is_ad = True
        if _flags(other)[1]:
            selected.metadata['is_preview'] = True
        if other.drm is True:
            selected.drm = True
            selected.classification, selected.encryption = other.classification, other.encryption
        result[duplicate] = selected
    return result


def _strong_anchors(candidate, lookup, fallback):
    scope = _scope(candidate, fallback)
    anchors = {('video', scope, value) for value in _ids(candidate)}
    group_id = _first(candidate, ('media_group_id', 'group_id')) or _text(_dict(_metadata(candidate).get('media_group')).get('id'))
    if group_id:
        anchors.add(('group', scope, group_id))
    dom_id = _first(candidate, ('dom_video_id', 'dom_element_id', 'video_element_id'))
    if dom_id and _url_key(_page(candidate, fallback)):
        anchors.add(('dom', scope, candidate.frame_id, dom_id))
    parent = _first(candidate, ('parent_manifest', 'parent_id', 'manifest_parent', 'master_url',
                                'master_playlist_url', 'master_playlist', 'mpd_url'))
    seen = set()
    while parent and parent not in seen and len(seen) < 16:
        seen.add(parent)
        owner = lookup.get(parent)
        if owner is None:
            anchors.add(('manifest', parent) if _url_key(parent) else ('parent', scope, parent))
            break
        anchors.add(('manifest', owner.final_url))
        parent = _first(owner, ('parent_manifest', 'parent_id', 'manifest_parent', 'master_url', 'mpd_url'))
    if candidate.manifest_type in ('Master', 'MPD'):
        anchors.add(('manifest', candidate.final_url))
    return anchors


def _clean_title(value):
    title = _text(value)
    if re.fullmatch(r'(?:index|master)(?:\.[a-z0-9]+)?|abc\d+\.png', title, re.I):
        return ''
    return title


def _title(candidate):
    return next((_clean_title(values.get(key)) for values in _metadata_values(candidate) for key in _TITLE_FIELDS
                 if _clean_title(values.get(key))), '')


def _family(candidate):
    parsed = urlsplit(candidate.final_url)
    path = unquote(parsed.path)
    if not _QUALITY_TOKEN.search(path):
        return ''
    normalized = _QUALITY_TOKEN.sub('{quality}', path)
    return parsed.scheme.lower() + '://' + parsed.netloc.lower() + normalized


def _weak_match(left, right, fallback):
    if _conflict(left, right):
        return False
    page = _url_key(_page(left, fallback))
    if not page or page != _url_key(_page(right, fallback)):
        return False
    if _scope(left, fallback)[0] != _scope(right, fallback)[0]:
        return False
    duration_left, duration_right = _number(left.duration), _number(right.duration)
    if not duration_left or not duration_right or abs(duration_left - duration_right) > max(1, min(3, duration_left * .005)):
        return False
    title = _title(left)
    same_title = bool(title and title.casefold() == _title(right).casefold())
    thumbnail = _first(left, ('thumbnail', 'thumbnail_url', 'poster'))
    same_thumbnail = bool(thumbnail and thumbnail == _first(right, ('thumbnail', 'thumbnail_url', 'poster')))
    fingerprint = _first(left, ('media_fingerprint', 'fingerprint'))
    same_fingerprint = bool(fingerprint and fingerprint == _first(right, ('media_fingerprint', 'fingerprint')))
    family = _family(left)
    same_family = bool(family and family == _family(right))
    # page + duration 只是基础，文件名仍须标题/缩略图等另一条独立证据。
    score = 3 + 2 * same_title + 2 * same_thumbnail + 4 * same_fingerprint + 2 * same_family
    return score >= 7 and (same_family or same_fingerprint)


def _stable_id(members, anchors, fallback):
    priorities = {'video': 0, 'group': 1, 'manifest': 2, 'parent': 3, 'dom': 4}
    video_anchors = [anchor for anchor in anchors if anchor[0] == 'video']
    if video_anchors:
        identity = ('video', sorted(video_anchors, key=lambda item: json.dumps(item, ensure_ascii=False)))
    elif anchors:
        identity = min(anchors, key=lambda item: (priorities[item[0]], json.dumps(item, ensure_ascii=False)))
    else:
        candidate = members[0]
        family = _family(candidate)
        if family and _page(candidate, fallback) and _title(candidate) and _number(candidate.duration):
            identity = ('family', _scope(candidate, fallback), family, _title(candidate).casefold(), round(candidate.duration / 5))
        else:
            identity = ('resource', min(_exact_resource_key(member) for member in members))
    identity = identity, _flags(members[0])[:2]
    return 'group-' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]


def group_media(candidates, page_title='', source_page='') -> list[MediaGroup]:
    """每组只默认选择一个可用版本；无法证明同一视频时保留独立组。"""
    values = [candidate for candidate in deduplicate_candidates(candidates, source_page=source_page)
              if not _flags(candidate)[2]]
    lookup = {candidate.id: candidate for candidate in values}
    lookup.update({url: candidate for candidate in values for url in (candidate.url, candidate.final_url, candidate.original_url)})
    anchors = [_strong_anchors(candidate, lookup, source_page) for candidate in values]
    # Master 明确声明的子清单即使没有 parent_id，也能共享同一个协议父节点。
    urls = {candidate.final_url: index for index, candidate in enumerate(values)}
    for index, candidate in enumerate(values):
        if candidate.manifest_type not in ('Master', 'MPD'):
            continue
        children = _dict(_metadata(candidate).get('manifest')).get('children', [])
        if not isinstance(children, (list, tuple)):
            continue
        for child in children[:4096]:
            target = _text(_dict(child).get('resolved_uri')) if isinstance(child, dict) else _text(child)
            child_index = urls.get(target)
            if child_index is not None:
                anchors[child_index].add(('manifest', candidate.final_url))
    parents = list(range(len(values)))
    members = {index: [index] for index in parents}

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def join(left, right):
        left, right = root(left), root(right)
        if left == right or any(_conflict(values[a], values[b]) for a in members[left] for b in members[right]):
            return
        parents[right] = left
        members[left].extend(members.pop(right))

    for left in range(len(values)):
        for right in range(left + 1, len(values)):
            if anchors[left].intersection(anchors[right]):
                join(left, right)
    for left in range(len(values)):
        for right in range(left + 1, len(values)):
            if root(left) != root(right) and _weak_match(values[left], values[right], source_page):
                join(left, right)
    groups = []
    used_ids = set()
    for indices in members.values():
        candidates_in_group = _ordered([values[index] for index in indices])
        available = _ordered(candidate for candidate in candidates_in_group if available_candidate(candidate))
        selected = available[0] if available else None
        descriptive = selected or candidates_in_group[0]
        group_anchors = set().union(*(anchors[index] for index in indices))
        group_id = _stable_id(candidates_in_group, group_anchors, source_page)
        if group_id in used_ids:
            # 冲突证据已经阻止归组；弱身份的同名资源也不能共用 UI 勾选 ID。
            distinct = json.dumps(min(_exact_resource_key(candidate) for candidate in candidates_in_group), ensure_ascii=False)
            group_id += '-' + hashlib.sha256(distinct.encode('utf-8')).hexdigest()[:12]
            repeated = group_id
            number = 1
            while group_id in used_ids:
                group_id = f'{repeated}-{number}'
                number += 1
        used_ids.add(group_id)
        title = next((_title(candidate) for candidate in candidates_in_group if _title(candidate)), '')
        title = title or _clean_title(_first(descriptive, ('page_title', 'pageTitle'))) or _clean_title(page_title)
        title = title or _clean_title(_first(descriptive, ('video_id', 'api_video_id', 'videoId', 'media_id', 'mediaId', 'canonical_media_id')))
        if not title:
            timestamp = _freshness(descriptive)[0]
            try:
                title = 'media_' + (datetime.fromtimestamp(timestamp) if timestamp else datetime.now()).strftime('%Y%m%d_%H%M%S')
            except (ValueError, OSError, OverflowError):
                title = 'media_' + datetime.now().strftime('%Y%m%d_%H%M%S')
        is_ad, is_preview, _ = _flags(descriptive)
        confidence = descriptive.confidence
        groups.append(MediaGroup(group_id, title, candidates_in_group, selected,
                                 max((_number(candidate.duration) for candidate in candidates_in_group), default=0) or None,
                                 _first(descriptive, ('thumbnail', 'thumbnail_url', 'poster')),
                                 _page(descriptive, source_page), confidence,
                                 bool(selected and confidence >= 70 and not is_ad and not is_preview), is_ad, is_preview))
    return sorted(groups, key=lambda group: (not group.is_ad, group.selected_candidate is not None,
                                            group.confidence), reverse=True)
