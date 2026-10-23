"""只分析已采集事件的媒体索引、协议语义和候选评分；不执行网络请求。"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
import re
import struct
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

from core.discovery_models import MediaCandidate, NetworkObservation
from core.errors import MediaCheckError
from core.http_ranges import parse_content_range
from core.manifests import hls_lines, parse_attribute_list, parse_manifest
from core.request_context import RequestContext
from core.media_confidence import score_candidate
from core.media_details import enrich_media_details
from core.progressive_mp4 import range_proof, verified_response, full_resource_context, same_version, resource_version
from utils.security import Redactor


MAX_CANDIDATES = 128
MAX_OBSERVATIONS = 512
MAX_RESOURCES = 4096
MAX_BODY = 16 * 1024 * 1024
_HLS_MIMES = frozenset(('application/vnd.apple.mpegurl', 'application/x-mpegurl',
                        'application/mpegurl', 'audio/mpegurl', 'audio/x-mpegurl'))
_DIRECT_MIMES = frozenset(('application/mp4', 'application/ogg', 'application/x-matroska'))
_AD_WORDS = re.compile(r'(?<![a-z0-9])(?:vast|ad|ads|advertiser|ad-provider|adprovider|preroll|midroll|tracker|banner|spot)(?![a-z0-9])', re.I)
_PREVIEW_WORDS = re.compile(r'(?<![a-z0-9])(?:thumbnail|thumbs|poster|sprite|gif|preview)(?![a-z0-9])', re.I)
_SEGMENT_ROLES = frozenset(('Media Segment', 'Partial Segment', 'Preload Segment', 'DASH Media Segment'))
_MANIFEST_FIELDS = ('media_type', 'protocol', 'manifest_type', 'playlist_type', 'hls_playlist_type',
                    'encryption', 'classification', 'drm', 'keys', 'resources', 'children', 'evidence')


def _primary_video(videos):
    """大小、焦点和真实播放进度确定主播放器；同等播放器保留歧义。"""
    eligible = [(index, video) for index, video in enumerate(videos)
                if isinstance(video, dict) and video.get('tag', 'VIDEO') != 'AUDIO'
                and video.get('visible') is not False and (video.get('current_src') or video.get('src'))
                and ('ready_state' not in video or _number(video.get('ready_state')) >= 1)]
    if not eligible:
        return None
    eligible.sort(key=lambda item: _number(item[1].get('area')), reverse=True)
    if len(eligible) == 1:
        return eligible[0][0]
    for key in ('focused', 'progressing'):
        matching = [index for index, video in eligible if video.get(key) is True]
        if len(matching) == 1:
            return matching[0]
    if (_number(eligible[0][1].get('area')) > 0 and
            _number(eligible[0][1].get('area')) >= 1.5 * _number(eligible[1][1].get('area'))):
        return eligible[0][0]
    matching = [index for index, video in eligible if video.get('is_main') is True]
    if len(matching) == 1:
        return matching[0]
    return None


def _mime(value):
    return str(value or '').partition(';')[0].strip().lower()


def _number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else default
    except (ValueError, TypeError, OverflowError):
        return default


def _integer(value):
    return int(min(_number(value), 10**12))


def _dimension(width, height):
    return _integer(width), _integer(height)


def _quality(value):
    return value[1], value[0] * value[1]


def _local(tag):
    return tag.rsplit('}', 1)[-1].rsplit(':', 1)[-1]


def _http_url(value, base=''):
    if not isinstance(value, str) or not value or value.lower().startswith(('blob:', 'data:')):
        return ''
    try:
        target = urljoin(base, value)
        RequestContext(target)
        return target
    except (ValueError, TypeError):
        return ''


def _aliases(context):
    return set(filter(None, (context.url, context.original_url, context.final_url)))


def is_verified_resolver(candidate):
    """确定性协议解析结果与通用浏览器猜测使用不同的权威级别。"""
    return bool(candidate and candidate.metadata.get('resolver_verified') is True)


def _ad_text(url, initiator=''):
    parsed = urlsplit(url)
    # 签名/token 的随机值不参与广告词匹配，实际 URL 仍原样保留。
    origin = str(initiator)[:8192]
    # parser 指向承载视频的 HTML 文档，不是广告脚本来源。
    if re.match(r'^\s*parser(?:\s|$)', origin, re.I):
        origin = ''

    def without_query(match):
        try:
            value = urlsplit(match.group(0))
            return unquote(value.netloc + value.path)
        except ValueError:
            return ''

    origin = re.sub(r'https?://[^\s<>"\']+', without_query, origin)
    return unquote(parsed.netloc + parsed.path) + ' ' + origin


def _manifest_copy(values):
    limits = {'resources': MAX_RESOURCES, 'children': MAX_CANDIDATES,
              'keys': MAX_CANDIDATES, 'evidence': MAX_CANDIDATES}
    return {key: deepcopy(values[key][:limits[key]] if key in limits and isinstance(values[key], list)
                          else values[key]) for key in _MANIFEST_FIELDS if key in values}


def _complete_moov(data, body_complete):
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                return False
            size = struct.unpack_from('>Q', data, offset + 8)[0]
            header = 16
        elif size == 0:
            if not body_complete:
                return False
            size = len(data) - offset
        if size < header or offset + size > len(data):
            return False
        if kind == b'moov':
            return True
        offset += size
    return False


def _complete_resource(observation, data):
    """读完一条 206 响应不代表读完整个资源；必须核对 Content-Range 总长度。"""
    if not data or not observation.body_complete:
        return False
    length = observation.content_length
    headers = {str(key).lower(): str(value) for key, value in observation.response_headers.items()}
    if length is None and headers.get('content-length', '').isdigit():
        length = int(headers['content-length'])
    if length is not None and length != len(data):
        return False
    if observation.status == 206:
        match = re.fullmatch(r'bytes\s+(\d+)-(\d+)/(\d+)', headers.get('content-range', '').strip(), re.I)
        return bool(match and length == len(data) and int(match[1]) == 0
                    and int(match[2]) + 1 == int(match[3]) == len(data))
    return observation.status in (0, 200)


def _complete_mp4(data):
    # 初始化片段也可能有完整 moov；Direct 还必须包含完整、非空 mdat。
    if not _complete_moov(data, True):
        return False
    offset, has_media = 0, False
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                return False
            size, header = struct.unpack_from('>Q', data, offset + 8)[0], 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            return False
        has_media = has_media or kind == b'mdat' and size > header
        offset += size
    return has_media and offset == len(data)


def _context_redactor(context):
    headers = dict(context.headers)
    if context.body:
        body = context.body.decode('utf-8', errors='replace')
        values = [body] + [value for _, value in parse_qsl(body, keep_blank_values=True) if value]
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = None

        def collect(item):
            if len(values) >= 128:
                return
            if isinstance(item, dict):
                for value in item.values():
                    collect(value)
            elif isinstance(item, list):
                for value in item:
                    collect(value)
            elif item is not None:
                values.append(str(item))

        collect(payload)
        for index, value in enumerate(values[:128]):
            headers[f'x-body-secret-{index}'] = value
    return Redactor(headers, [context.url, context.final_url])


def _safe_redirects(values):
    fields = ('status', 'from', 'to', 'location', 'url', 'original_url', 'final_url')
    return [{key: deepcopy(item[key]) for key in fields if key in item}
            for item in list(values or [])[-32:] if isinstance(item, dict)]


class MediaDiscoveryService:
    def __init__(self, max_body_bytes=MAX_BODY):
        self.max_body_bytes = max(1024 * 1024, min(int(max_body_bytes), 128 * 1024 * 1024))
        self.candidates: dict[str, MediaCandidate] = {}
        self.player_type = ''
        self.page_url = ''
        self._next_id = 1
        self._sequence = 0
        self._urls: dict[str, str] = {}
        self._network: OrderedDict[str, NetworkObservation] = OrderedDict()
        self._request_sequence: dict[str, int] = {}
        self._roles: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._templates: OrderedDict[tuple[str, str], re.Pattern] = OrderedDict()
        self._frames: OrderedDict[str, dict] = OrderedDict()
        self._ad_urls: OrderedDict[str, None] = OrderedDict()
        self._excluded: OrderedDict[str, None] = OrderedDict()
        self._excluded_roles: OrderedDict[str, str] = OrderedDict()
        self._excluded_requests: OrderedDict[str, str] = OrderedDict()
        self._segment_seen: dict[str, OrderedDict[str, None]] = {}
        self._seek_events: dict[tuple[str, int], tuple] = {}
        self._seek_sequence: dict[str, int] = {}
        self._orphan_groups = OrderedDict()
        self._dash_descriptions = OrderedDict()

    def clear(self):
        # ID 不重用，已经投递但尚未到达的旧 probe 不会命中新一轮候选。
        next_id = self._next_id
        self.__init__(max_body_bytes=self.max_body_bytes)
        self._next_id = next_id

    def get(self, candidate_id):
        return self.candidates.get(candidate_id)

    def _drop(self, candidate_id):
        self.candidates.pop(candidate_id, None)
        self._urls = {url: value for url, value in self._urls.items() if value != candidate_id}
        self._segment_seen.pop(candidate_id, None)
        for url, owners in tuple(self._roles.items()):
            owners.pop(candidate_id, None)
            if not owners:
                self._roles.pop(url, None)
        for key in tuple(self._templates):
            if key[0] == candidate_id:
                self._templates.pop(key, None)

    def exclude_resource_role(self, urls, role, request_id=''):
        """消费解析器确认的非正片角色；与站点 URL 结构无关。"""
        role = str(role or 'Excluded Resource')[:128]
        values = urls if isinstance(urls, (list, tuple, set)) else ()
        excluded = {url for value in list(values)[:MAX_RESOURCES] if (url := _http_url(value))}
        if request_id:
            request_id = str(request_id)
            self._excluded_requests[request_id] = role
            self._excluded_requests.move_to_end(request_id)
            observation = self._network.get(request_id)
            if observation:
                excluded.update(_aliases(observation.context))
        identifiers = {candidate.id for candidate in self.candidates.values()
                       if not is_verified_resolver(candidate) and _aliases(candidate.request_context) & excluded}
        # 已由缩略图 Master 派生的子清单同样不是独立正片。
        for _ in range(MAX_CANDIDATES):
            children = {candidate.id for candidate in self.candidates.values()
                        if not is_verified_resolver(candidate)
                        and candidate.metadata.get('parent_manifest') in identifiers}
            additions = children - identifiers
            if not additions:
                break
            identifiers.update(additions)
        for identifier in identifiers:
            candidate = self.candidates[identifier]
            excluded.update(_aliases(candidate.request_context))
            excluded.update(url for item in candidate.metadata.get('manifest', {}).get('resources', [])
                            if isinstance(item, dict) and (url := _http_url(item.get('resolved_uri'))))
            self._drop(identifier)
        for url in excluded:
            self._excluded_roles[url] = role
            self._excluded_roles.move_to_end(url)
        while len(self._excluded_roles) > MAX_RESOURCES:
            self._excluded_roles.popitem(last=False)
        while len(self._excluded_requests) > MAX_OBSERVATIONS:
            self._excluded_requests.popitem(last=False)
        self._refresh()

    def _new(self, context, source, frame_id='', complete=False):
        matches = {self._urls[url] for url in _aliases(context)
                   if url in self._urls and self._urls[url] in self.candidates}
        found = min((self.candidates[value] for value in matches),
                    key=lambda candidate: (not is_verified_resolver(candidate),
                                           candidate.metadata.get('created_sequence', 0), candidate.id), default=None)
        if found is None:
            candidate_id = f'media-{self._next_id:04d}'
            self._next_id += 1
            found = MediaCandidate(candidate_id, deepcopy(context), source_type=source,
                                   frame_id=frame_id, context_complete=complete)
            found.metadata['created_sequence'] = self._sequence
            self.candidates[found.id] = found
        for candidate_id in matches - {found.id}:
            other = self.candidates[candidate_id]
            if not is_verified_resolver(found):
                found.sources.update(other.sources)
                found.confirmed = found.confirmed or other.confirmed
                found.context_complete = found.context_complete or other.context_complete
                if _quality(other.resolution) > _quality(found.resolution):
                    found.resolution = other.resolution
                found.bandwidth = max(found.bandwidth, other.bandwidth)
                found.duration = max(found.duration or 0, other.duration or 0) or None
                found.metadata['eme_evidence'] = found.metadata.get('eme_evidence', False) or other.metadata.get('eme_evidence', False)
            other_urls = [url for url, value in self._urls.items() if value == candidate_id]
            for owners in self._roles.values():
                if candidate_id in owners:
                    owners[found.id] = owners[candidate_id]
            self._drop(candidate_id)
            for url in other_urls:
                self._urls[url] = found.id
        if not is_verified_resolver(found):
            found.sources.add(source)
        if frame_id and (source == 'Resolver' or not is_verified_resolver(found)):
            found.frame_id = frame_id
        for url in _aliases(context):
            self._urls.pop(url, None)
            self._urls[url] = found.id
        while len(self._urls) > MAX_OBSERVATIONS * 3:
            self._urls.pop(next(iter(self._urls)))
        if not is_verified_resolver(found):
            found.source_type = ' / '.join(name for name in ('DOM', 'Network', 'Manifest', 'MSE') if name in found.sources)
        return found

    def _bound_candidates(self):
        while len(self.candidates) > MAX_CANDIDATES:
            victim = min(self.candidates.values(), key=lambda candidate: (
                is_verified_resolver(candidate), not candidate.is_ad, candidate.downloadable,
                candidate.confidence, candidate.metadata.get('created_sequence', 0)))
            self._drop(victim.id)

    def _role_for(self, urls):
        result = {}
        for url in urls:
            result.update(self._roles.get(url, {}))
            for (owner, _), pattern in self._templates.items():
                if pattern.fullmatch(url):
                    result[owner] = 'DASH Media Segment'
        return result

    def _register_role(self, owner, uri, role):
        if not _http_url(uri):
            return
        self._roles.setdefault(uri, {})[owner.id] = role
        self._roles.move_to_end(uri)
        while len(self._roles) > MAX_RESOURCES:
            self._roles.popitem(last=False)
        existing = self._urls.get(uri)
        if existing and existing != owner.id and not is_verified_resolver(self.get(existing)):
            self._drop(existing)

    def _register_template(self, owner, url):
        parts = re.split(r'(\$(?:Number|Time)(?:%0\d+d)?\$)', url)
        expression = ''.join(r'\d+' if part.startswith('$') and part.endswith('$')
                             else re.escape(part) for part in parts)
        self._templates[(owner.id, url)] = re.compile(expression)
        self._templates.move_to_end((owner.id, url))
        while len(self._templates) > MAX_CANDIDATES * 4:
            self._templates.popitem(last=False)
        for candidate in tuple(self.candidates.values()):
            if (candidate.id != owner.id and not is_verified_resolver(candidate)
                    and any(re.fullmatch(expression, url) for url in _aliases(candidate.request_context))):
                self._drop(candidate.id)

    def _observe_resource(self, observation, owners):
        for owner_id, role in owners.items():
            owner = self.get(owner_id)
            if owner is None or role not in _SEGMENT_ROLES:
                continue
            if observation.status and not 200 <= observation.status < 300:
                continue
            seen = self._segment_seen.setdefault(owner_id, OrderedDict())
            seen[observation.context.final_url] = None
            seen.move_to_end(observation.context.final_url)
            while len(seen) > 128:
                seen.popitem(last=False)
            owner.metadata['segment_requests'] = len(seen)
            if (self._seek_sequence.get(observation.frame_id, 0)
                and self._request_sequence.get(observation.request_id, 0) > self._seek_sequence[observation.frame_id]):
                owner.metadata['seek_range'] = True

    def _reconcile_resources(self):
        for observation in self._network.values():
            owners = self._role_for(_aliases(observation.context))
            if owners:
                self._observe_resource(observation, owners)

    def _exclude(self, context):
        for url in _aliases(context):
            existing = self._urls.get(url)
            if is_verified_resolver(self.get(existing)):
                continue
            if existing:
                self._drop(existing)
            self._excluded[url] = None
            self._excluded.move_to_end(url)
        while len(self._excluded) > MAX_OBSERVATIONS:
            self._excluded.popitem(last=False)

    def _vast(self, data, base):
        if not data or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            return False
        try:
            root = ET.fromstring(data)
        except (ET.ParseError, ValueError):
            return False
        if _local(root.tag).lower() != 'vast':
            return False
        for element in root.iter():
            if _local(element.tag).lower() not in ('mediafile', 'tracking', 'impression', 'clickthrough'):
                continue
            url = _http_url(''.join(element.itertext()).strip(), base)
            if url:
                self._ad_urls[url] = None
                self._ad_urls.move_to_end(url)
                candidate = self.get(self._urls.get(url, ''))
                if candidate:
                    candidate.metadata['vast_ad'] = True
            while len(self._ad_urls) > 256:
                self._ad_urls.popitem(last=False)
        return True

    def consume_network(self, observation: NetworkObservation):
        if not isinstance(observation, NetworkObservation):
            raise TypeError('网络观测必须是 NetworkObservation。')
        if observation.resource_type == 'Probe':
            # guard 提供的正文是协议补充，不是新浏览器请求或新播放行为。
            candidate = next((self.get(self._urls[url]) for url in _aliases(observation.context)
                              if url in self._urls and self.get(self._urls[url]) is not None), None)
            data = observation.body
            if (candidate is None or is_verified_resolver(candidate)
                    or not isinstance(data, bytes) or not data or len(data) > self.max_body_bytes):
                return
            try:
                info = parse_manifest(data, candidate.final_url, observation.content_type,
                                      candidate.request_context.url_resolution_policy)
            except MediaCheckError:
                return
            known = candidate.drm, candidate.encryption, candidate.classification
            values = info.to_dict()
            if known[0] is True and info.drm is not True:
                values.update(drm=True, encryption=known[1], classification=known[2])
            self._apply_manifest(candidate, values, data, observation.body_complete)
            enrich_media_details(candidate, data, body_complete=observation.body_complete)
            if known[0] is not None and info.drm is not True:
                candidate.drm = known[0]
                candidate.encryption, candidate.classification = known[1:]
            candidate.metadata['body_complete'] = observation.body_complete
            self._refresh()
            self._bound_candidates()
            return
        self._sequence += 1
        observation = deepcopy(observation)
        data = observation.body if isinstance(observation.body, bytes) and len(observation.body) <= self.max_body_bytes else None
        record = replace(observation, body=None)
        self._network[observation.request_id] = record
        self._request_sequence.setdefault(observation.request_id, self._sequence)
        self._network.move_to_end(observation.request_id)
        while len(self._network) > MAX_OBSERVATIONS:
            removed, _ = self._network.popitem(last=False)
            self._request_sequence.pop(removed, None)
        context = observation.context
        aliases = _aliases(context)
        if observation.request_id in self._excluded_requests:
            self.exclude_resource_role(tuple(aliases), self._excluded_requests[observation.request_id], observation.request_id)
            return
        matching_roles = aliases & self._excluded_roles.keys()
        if matching_roles:
            # ExtraInfo/重放晚到时仍保存观测，但不得重新生成已排除轨道。
            self.exclude_resource_role(tuple(aliases), self._excluded_roles[next(iter(matching_roles))], observation.request_id)
            return
        if any(is_verified_resolver(self.get(self._urls.get(url))) for url in aliases):
            # 仍保存原始网络诊断，但迟到的正文、ExtraInfo 和 HTTP 错误不能
            # 替换 Resolver 已验证的清单、来源或完整浏览器上下文。
            self._refresh()
            return
        if (data and observation.body_complete and 200 <= observation.status < 300
                and data.lstrip(b'\xef\xbb\xbf \t\r\n').startswith((b'{', b'['))
                and self._consume_dash_description(observation, data)):
            self._refresh()
            self._bound_candidates()
            return
        if (data and observation.body_complete and 200 <= observation.status < 300
                and observation.resource_type == 'Document'
                and _mime(observation.content_type) in ('text/html', 'application/xhtml+xml')):
            from core.embedded_media import embedded_dash_json
            descriptions = embedded_dash_json(data, context.final_url, self.max_body_bytes)
            for description in descriptions:
                self._consume_dash_description(observation, description)
            if descriptions:
                self._refresh()
                self._bound_candidates()
                return
        resolved = next((self.get(self._urls[url]) for url in aliases
                         if url in self._urls and self.get(self._urls[url]) is not None
                         and self.get(self._urls[url]).request_context.playlist_decoder), None)
        if resolved is not None:
            context = replace(context, playlist_decoder=resolved.request_context.playlist_decoder,
                              url_resolution_policy=resolved.request_context.url_resolution_policy)
            observation.context = context
            if data and context.playlist_decoder == 'rou':
                from core.rou_playlist_decoder import RouPlaylistDecoder
                try:
                    data = RouPlaylistDecoder.decode(data)
                    observation.content_type = 'application/vnd.apple.mpegurl'
                except MediaCheckError:
                    data = None
            if not data and resolved.media_type == 'HLS':
                observation.content_type = 'application/vnd.apple.mpegurl'
        owners = self._role_for(aliases)
        if owners:
            if data and data[4:8] in (b'ftyp', b'moov', b'moof', b'styp', b'pssh'):
                try:
                    track_info = parse_manifest(data, context.final_url, observation.content_type)
                except MediaCheckError:
                    track_info = None
                if track_info and track_info.drm is True:
                    for owner_id in owners:
                        owner = self.get(owner_id)
                        if owner is not None:
                            owner.drm = True
                            owner.encryption, owner.classification = track_info.encryption, track_info.classification
                            self._retain_protection(owner)
            for url in aliases:
                candidate_id = self._urls.get(url)
                if candidate_id and candidate_id not in owners:
                    self._drop(candidate_id)
            self._observe_resource(observation, owners)
            self._refresh()
            return
        prefix_bytes = (data or b'')[:600]
        segment_bytes = (len(prefix_bytes) > 376 and all(prefix_bytes[i] == 0x47 for i in (0, 188, 376))) or prefix_bytes[4:8] in (b'moof', b'styp')
        segment_mime = _mime(observation.content_type) in ('video/mp2t', 'video/mpegts')
        if segment_bytes or segment_mime:
            parsed = urlsplit(context.final_url)
            family = re.sub(r'\d+', '#', parsed.path)
            group = self._orphan_groups.setdefault((observation.frame_id, parsed.netloc, family), set())
            if len(group) < 128:
                group.add(context.final_url)
            while len(self._orphan_groups) > 128:
                self._orphan_groups.popitem(last=False)
        mime = _mime(observation.content_type)
        prefix = (data or b'').lstrip(b'\xef\xbb\xbf \t\r\n')
        if self._vast(data, context.final_url):
            self._exclude(context)
            self._refresh()
            return
        info = None
        if data:
            try:
                info = parse_manifest(data, context.final_url, mime, context.url_resolution_policy)
            except MediaCheckError:
                pass
        manifest_body = bool(info and info.media_type in ('HLS', 'DASH'))
        # 后缀只提示孤立分片；已解析清单的语义优先，PNG 分片由 _role_for 关联。
        fragment_hint = urlsplit(context.final_url).path.lower().endswith(('.m4s', '.ts'))
        complete_direct = bool(info and info.media_type == 'Direct' and _complete_resource(observation, data)
                               and _complete_mp4(data) and not segment_bytes and not segment_mime
                               and not (observation.status == 206 and fragment_hint))
        partial_resource = observation.status == 206 and not complete_direct
        fragment_only = not manifest_body and (partial_resource or segment_bytes or segment_mime
                                               or fragment_hint and not complete_direct)
        existing = next((self.get(self._urls[url]) for url in aliases if url in self._urls
                         and self.get(self._urls[url]) is not None), None)
        intrinsic_version = existing.metadata.get('intrinsic_mp4', {}).get('resource_version') if existing else None
        if intrinsic_version and not same_version(intrinsic_version, observation):
            existing.metadata.pop('intrinsic_mp4', None)
        if (existing and existing.metadata.get('progressive_mp4')
                and not same_version(existing.metadata['progressive_mp4'], observation)):
            existing.metadata.pop('progressive_mp4', None)
            existing.metadata.pop('intrinsic_mp4', None)
            if existing.metadata.pop('progressive_direct_verified', False):
                existing.metadata.pop('protocol_validated', None)
        if (existing and existing.metadata.get('protocol_validated')
                and not manifest_body and (fragment_only or not data)):
            # 低权威的 Range/ExtraInfo/失败事件不能撤回已有完整协议证据。
            existing.metadata['last_resource_http_status'] = observation.status
            existing.metadata['last_resource_request_id'] = observation.request_id
            if info and info.drm is True:
                existing.drm, existing.encryption, existing.classification = True, info.encryption, info.classification
                self._retain_protection(existing)
            self._refresh()
            return
        excluded_body = (prefix.startswith((b'WEBVTT', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'\xff\xd8\xff'))
                         or prefix[:1024].lower().startswith((b'<!doctype html', b'<html')))
        excluded_mime = mime.startswith(('image/', 'text/')) and mime not in _HLS_MIMES
        if not manifest_body and (excluded_body or excluded_mime and info is None):
            self._exclude(context)
            self._refresh()
            return
        if info is None and prefix.startswith(b'<') and not manifest_body:
            self._exclude(context)
            self._refresh()
            return
        media_type = info.media_type if info else ('HLS' if mime in _HLS_MIMES else
                     'DASH' if mime == 'application/dash+xml' else
                     'Direct' if mime.startswith(('video/', 'audio/')) or mime in _DIRECT_MIMES else '')
        if fragment_only:
            media_type = 'Segment'
        weak = False
        if not media_type:
            path = urlsplit(context.final_url).path.lower()
            media_type = 'HLS' if path.endswith('.m3u8') else 'DASH' if path.endswith('.mpd') else ''
            weak = bool(media_type)
        if not media_type:
            self._refresh()
            return
        candidate = self._new(context, 'Network', observation.frame_id, observation.context_complete)
        candidate.request_context = deepcopy(context)
        candidate.context_complete = observation.context_complete
        candidate.content_type = mime
        candidate.http_status = observation.status
        candidate.media_type = media_type
        if fragment_only:
            candidate.metadata.update(fragment_only=True, resource_role=(
                'DASH Media Segment' if not segment_mime and not segment_bytes else 'Media Segment'))
            if partial_resource:
                candidate.metadata['partial_resource'] = True
                candidate.metadata['segment_mode'] = bool(fragment_hint or segment_bytes or segment_mime)
        else:
            candidate.metadata.pop('fragment_only', None)
            candidate.metadata.pop('resource_role', None)
            candidate.metadata.pop('partial_resource', None)
        candidate.redirects = _safe_redirects(observation.redirects or context.redirect_chain)
        candidate.metadata['network_observed'] = True
        candidate.metadata['from_cache'] = observation.from_cache
        candidate.metadata['body_complete'] = observation.body_complete
        candidate.metadata['last_sequence'] = self._sequence
        candidate.metadata['last_request_id'] = observation.request_id
        candidate.metadata['ad_initiator'] = bool(_AD_WORDS.search(_ad_text(context.final_url, observation.initiator)))
        candidate.metadata['preview'] = bool(_PREVIEW_WORDS.search(_ad_text(context.final_url, observation.initiator)))
        candidate.metadata['vast_ad'] = any(url in self._ad_urls for url in aliases)
        candidate.metadata['provisional'] = weak
        candidate.metadata['response_headers'] = deepcopy(observation.response_headers)
        proof = range_proof(observation) if partial_resource and not candidate.metadata.get('segment_mode') else None
        if proof:
            candidate.metadata['progressive_mp4'] = proof
        candidate.metadata['timing'] = deepcopy(observation.timing)
        from core.curl_parser import has_temporary_signature
        candidate.metadata.update(signed_url=has_temporary_signature(context.url) or None,
                                  discovered_at=observation.timestamp)
        if observation.status in range(200, 300) or not observation.status:
            candidate.confirmed = candidate.confirmed or bool(info) or not weak
        if (context.headers.get('range') and self._seek_sequence.get(observation.frame_id, 0)
            and self._request_sequence[observation.request_id] > self._seek_sequence[observation.frame_id]):
            candidate.metadata['seek_range'] = True
        if info and not fragment_only:
            self._apply_manifest(candidate, info.to_dict(), data, observation.body_complete)
            if manifest_body or complete_direct:
                candidate.metadata['protocol_validated'] = candidate.media_type
            if (candidate.drm is not True and observation.context_complete and observation.body_complete
                    and 200 <= observation.status < 300 and (manifest_body or complete_direct)):
                # 新浏览器成功响应已提供完整协议证据；旧探测失败不能永久封死候选。
                candidate.metadata.pop('error_code', None)
                candidate.metadata.pop('probe_error', None)
        enrich_media_details(candidate, data, body_complete=observation.body_complete,
                             content_length=observation.content_length)
        if data and data[4:8] == b'ftyp' and candidate.metadata.get('intrinsic_mp4'):
            version = resource_version(observation)
            if version:
                candidate.metadata['intrinsic_mp4']['resource_version'] = version
            else:
                candidate.metadata.pop('intrinsic_mp4', None)
        self._refresh()
        self._bound_candidates()

    def _consume_dash_description(self, observation, data):
        from core.dash_json import build_dash_manifest, choose_audio, parse_dash_description
        description = parse_dash_description(data, observation.context.final_url, max_bytes=self.max_body_bytes)
        if description is None:
            return False
        context = observation.context
        embedded = observation.resource_type == 'Document'
        if embedded:
            # 描述来自文档时，派生媒体的 Referer 是实际文档地址，而非导航前页面。
            context = replace(context, headers={**context.headers, 'referer': context.final_url})
        parsed = urlsplit(context.final_url)
        media_ids = [(key, value) for key, value in parse_qsl(parsed.query)
                     if key.lower() in ('aid', 'bvid', 'cid', 'video_id', 'media_id', 'content_id', 'id')]
        identity = hashlib.sha256((observation.frame_id + '\0' + parsed.netloc + parsed.path + '\0'
                                  + repr(sorted(media_ids) if media_ids else parsed.query)
                                  + '\0' + hashlib.sha256(context.body or b'').hexdigest()).encode()).hexdigest()
        self._dash_descriptions[identity] = {'video_tracks': len(description.videos),
                                            'audio_tracks': len(description.audios), 'embedded': embedded}
        self._dash_descriptions.move_to_end(identity)
        while len(self._dash_descriptions) > MAX_CANDIDATES:
            self._dash_descriptions.popitem(last=False)
        audio = choose_audio(description.audios)
        for video in description.videos:
            try:
                track_context = context.derive(video.url)
            except (MediaCheckError, ValueError):
                continue
            existing = self.get(self._urls.get(video.url))
            if is_verified_resolver(existing):
                continue
            candidate = self._new(track_context, 'Manifest', observation.frame_id, observation.context_complete)
            candidate.request_context = track_context
            candidate.context_complete = observation.context_complete
            candidate.video_track, candidate.audio_track = video, audio
            candidate.media_type, candidate.manifest_type, candidate.playlist_type = 'DASH', 'MPD', 'VOD'
            candidate.content_type = 'application/dash+xml'
            candidate.http_status = observation.status
            candidate.sources.update(('Network', 'Manifest'))
            candidate.confirmed = candidate.validated = True
            candidate.manifest_url = context.final_url
            candidate.transport_url = context.final_url
            candidate.manifest_origin = 'embedded:html-dash-json' if embedded else 'captured:dash-json'
            candidate.resolution = video.width, video.height
            candidate.width, candidate.height = video.width or None, video.height or None
            candidate.bandwidth = candidate.videoBitrate = video.bandwidth
            candidate.audioBitrate = audio.bandwidth if audio else None
            candidate.duration = description.duration
            known_protected = candidate.drm is True
            candidate.drm = known_protected or description.drm
            if not known_protected:
                candidate.classification = description.classification
                candidate.encryption = 'CENC' if candidate.drm else 'None'
            candidate.metadata.update(dash_description_id=identity, canonical_media_id='dash:' + identity,
                dash_video_representation_id=video.id, has_audio=bool(audio),
                dash_video_track_count=len(description.videos), dash_audio_track_count=len(description.audios),
                protocol_validated='DASH', network_observed=True, body_complete=True,
                last_request_id=observation.request_id, last_sequence=self._sequence,
                response_headers=deepcopy(observation.response_headers), timing=deepcopy(observation.timing),
                source_page=self._frames.get(observation.frame_id, {}).get('page_url') or context.referer or self.page_url,
                page_title=self._frames.get(observation.frame_id, {}).get('page_title', ''))
            from core.media_tracks import json_audio_catalog
            candidate.metadata['track_catalog'] = json_audio_catalog(description)
            for key in ('fragment_only', 'partial_resource', 'resource_role', 'error_code', 'probe_error', 'provisional'):
                candidate.metadata.pop(key, None)
            if not candidate.drm:
                candidate.manifest_text = build_dash_manifest(video, audio, description.duration)
                info = parse_manifest(candidate.manifest_text.encode(), candidate.manifest_url, 'application/dash+xml')
                self._apply_manifest(candidate, info.to_dict(), candidate.manifest_text.encode(), True)
                enrich_media_details(candidate, candidate.manifest_text.encode(), body_complete=True)
            else:
                candidate.manifest_text = ''
                self._retain_protection(candidate)
            for track in (video, audio):
                if track is not None:
                    for url in (track.url, *track.backup_urls):
                        self._register_role(candidate, url, 'DASH Media Segment')
        return True

    def consume_resolver(self, resolved: MediaCandidate):
        if not isinstance(resolved, MediaCandidate) or not resolved.confirmed or not resolved.context_complete:
            return
        verified = is_verified_resolver(resolved)
        if not verified and _aliases(resolved.request_context) & self._excluded_roles.keys():
            return
        if not verified and any(is_verified_resolver(self.get(self._urls.get(url)))
                                for url in _aliases(resolved.request_context)):
            return
        self._sequence += 1
        candidate = self._new(resolved.request_context, 'Resolver', resolved.frame_id, True)
        candidate.request_context = deepcopy(resolved.request_context)
        candidate.context_complete = True
        for field in ('media_type', 'manifest_type', 'playlist_type', 'encryption', 'classification',
                      'http_status', 'content_type', 'confirmed', 'resolution', 'bandwidth', 'duration', 'redirects',
                      'transport_url', 'manifest_url', 'manifest_text', 'manifest_origin', 'validated',
                      'width', 'height', 'fps', 'videoCodec', 'audioCodec', 'videoBitrate',
                      'audioBitrate', 'estimatedSize', 'container', 'video_track', 'audio_track'):
            setattr(candidate, field, deepcopy(getattr(resolved, field)))
        if verified:
            # 清除原先同 URL 的 Generic 猜测。保护结论来自 Resolver 解出的
            # 当前 Manifest；不让无关 Blob 播放器的关联证据污染正片身份。
            sequence = candidate.metadata.get('created_sequence', self._sequence)
            candidate.metadata = {'created_sequence': sequence, **deepcopy(resolved.metadata)}
            candidate.drm = resolved.drm
            candidate.is_ad = resolved.is_ad
            candidate.sources = set(resolved.sources) | {'Resolver'}
            candidate.source_type = resolved.source_type or 'Resolver'
        else:
            candidate.drm = True if candidate.drm is True else resolved.drm
            candidate.metadata.update(deepcopy(resolved.metadata))
            candidate.sources.update(resolved.sources | {'Resolver', 'Network'})
        self._apply_manifest(candidate, candidate.metadata.get('manifest', {}))
        self._refresh()
        self._bound_candidates()

    def _apply_manifest(self, candidate, values, data=None, complete=False):
        candidate.metadata['manifest'] = {**candidate.metadata.get('manifest', {}), **_manifest_copy(values)}
        candidate.media_type = values.get('media_type') or values.get('protocol') or candidate.media_type
        candidate.manifest_type = values.get('manifest_type') or values.get('playlist_type') or candidate.manifest_type
        candidate.encryption = values.get('encryption', candidate.encryption)
        candidate.classification = values.get('classification', candidate.classification)
        candidate.drm = values.get('drm', candidate.drm)
        if data is not None and candidate.media_type == 'Direct' and candidate.drm is not True:
            # parse_manifest 对没有 moov 的 Direct 默认只报告未发现证据。
            # 响应 MIME、206、blob 与文件后缀都不能证明整个文件没有 DRM。
            has_complete_moov = bool(data and _complete_moov(data, complete))
            candidate.drm = False if has_complete_moov else None
            if candidate.drm is None:
                candidate.encryption = 'Unknown'
        self._retain_protection(candidate)
        for key in values.get('keys', [])[:MAX_CANDIDATES]:
            if isinstance(key, dict) and key.get('resolved_uri'):
                self._register_role(candidate, key['resolved_uri'], 'Key')
        for resource in values.get('resources', [])[:MAX_RESOURCES]:
            if isinstance(resource, dict) and resource.get('resolved_uri'):
                self._register_role(candidate, resource['resolved_uri'], resource.get('role', 'Media Segment'))
        if data and candidate.media_type == 'HLS':
            if candidate.manifest_type == 'Master':
                from core.media_tracks import hls_catalog
                master = data.decode('utf-8-sig')
                candidate.metadata.update(track_master_text=master, track_master_url=candidate.final_url,
                                          track_catalog=hls_catalog(master, candidate.final_url))
            self._hls_details(candidate, data)
        elif data and candidate.media_type == 'DASH':
            if not candidate.metadata.get('dash_description_id'):
                from core.media_tracks import dash_catalog
                # XML 声明和 BOM 可能指定 UTF-16；保留既有解析器支持的编码。
                master = ET.tostring(ET.fromstring(data), encoding='unicode')
                candidate.metadata.update(track_master_text=master, track_master_url=candidate.manifest_url or candidate.final_url,
                                          track_catalog=dash_catalog(master, candidate.manifest_url or candidate.final_url))
            self._dash_details(candidate, data)
        self._reconcile_resources()

    def _hls_details(self, candidate, data):
        lines = hls_lines(data.decode('utf-8-sig').lstrip())
        audio_groups = set()
        for line in lines:
            if line.startswith('#EXT-X-MEDIA:'):
                attributes = parse_attribute_list(line.partition(':')[2])
                if attributes.get('TYPE') == 'AUDIO' and attributes.get('GROUP-ID'):
                    audio_groups.add(attributes['GROUP-ID'])
        candidate.metadata['audio_groups'] = sorted(audio_groups)
        candidate.playlist_type = candidate.metadata['manifest'].get('hls_playlist_type', '')
        if candidate.manifest_type == 'Media':
            candidate.playlist_type = candidate.playlist_type or ('VOD' if '#EXT-X-ENDLIST' in lines else 'LIVE')
            if candidate.playlist_type == 'VOD' or '#EXT-X-ENDLIST' in lines:
                candidate.duration = sum(_number(resource.get('duration')) for resource in
                                         candidate.metadata['manifest'].get('resources', [])
                                         if resource.get('role') == 'Media Segment') or None
        pending = None
        created = 0
        for line in lines[1:]:
            if line.startswith('#EXT-X-STREAM-INF:'):
                pending = parse_attribute_list(line.partition(':')[2])
            elif not line.startswith('#') and pending is not None:
                attributes, pending = pending, None
                if created >= MAX_CANDIDATES:
                    continue
                created += 1
                self._hls_variant(candidate, line, attributes)
            elif line.startswith(('#EXT-X-MEDIA:', '#EXT-X-IMAGE-STREAM-INF:', '#EXT-X-I-FRAME-STREAM-INF:')):
                attributes = parse_attribute_list(line.partition(':')[2])
                uri = attributes.get('URI')
                if not uri:
                    continue
                target = urljoin(candidate.final_url, uri)
                if line.startswith('#EXT-X-MEDIA:') and attributes.get('TYPE') == 'VIDEO':
                    if created < MAX_CANDIDATES:
                        self._hls_variant(candidate, uri, attributes)
                        created += 1
                else:
                    role = 'Subtitle' if attributes.get('TYPE') == 'SUBTITLES' else 'Auxiliary Rendition'
                    self._register_role(candidate, target, role)

    def _hls_variant(self, parent, uri, attributes):
        try:
            context = parent.request_context.derive(uri)
        except MediaCheckError:
            return
        child = self._new(context, 'Manifest', parent.frame_id, parent.context_complete)
        if child.id == parent.id:
            return
        if 'Network' not in child.sources and not child.confirmed:
            child.media_type = 'HLS'
            child.context_complete = parent.context_complete
            child.manifest_type = 'Variant'
        if parent.drm is True:
            child.drm = True
            child.encryption = parent.encryption
            child.classification = parent.classification
        dimensions = re.fullmatch(r'(\d+)x(\d+)', attributes.get('RESOLUTION', ''))
        resolution = _dimension(*dimensions.groups()) if dimensions else (0, 0)
        if _quality(resolution) > _quality(child.resolution):
            child.resolution = resolution
        if all(resolution):
            child.metadata['declared_resolution'] = resolution
        child.metadata['declared_bandwidth'] = _integer(attributes.get('AVERAGE-BANDWIDTH') or attributes.get('BANDWIDTH'))
        if attributes.get('AUDIO') in parent.metadata.get('audio_groups', []):
            child.metadata['has_audio'] = True
        child.bandwidth = max(child.bandwidth, _integer(attributes.get('BANDWIDTH', attributes.get('AVERAGE-BANDWIDTH', 0))))
        child.metadata['codecs'] = attributes.get('CODECS', '')[:512]
        child.metadata['frame_rate'] = attributes.get('FRAME-RATE', '')[:64]
        child.metadata['parent_manifest'] = parent.id
        if 'track_catalog' in parent.metadata:
            from copy import deepcopy
            catalog = deepcopy(parent.metadata['track_catalog'])
            for track in catalog.get('audio', []):
                if track.get('group') != attributes.get('AUDIO'):
                    track.update(supported=False, reason='此音轨不属于当前视频版本的音频组。')
            for track in catalog.get('subtitles', []):
                if track.get('group') != attributes.get('SUBTITLES'):
                    track.update(supported=False, reason='此字幕不属于当前视频版本的字幕组。')
            child.metadata.update(track_catalog=catalog,
                track_master_text=parent.metadata.get('track_master_text', ''),
                track_master_url=parent.metadata.get('track_master_url', parent.final_url),
                track_variant_url=child.url)
        if _quality(child.resolution) > _quality(parent.resolution):
            parent.resolution = child.resolution
            parent.metadata['codecs'] = child.metadata['codecs']
            parent.metadata['frame_rate'] = child.metadata['frame_rate']
        parent.bandwidth = max(parent.bandwidth, child.bandwidth)

    @staticmethod
    def _dash_duration(value):
        match = re.fullmatch(r'P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?', value or '')
        if not match:
            return None
        return sum(_number(item) * multiplier for item, multiplier in zip(match.groups(), (86400, 3600, 60, 1))) or None

    def _dash_details(self, candidate, data):
        root = ET.fromstring(data)
        candidate.playlist_type = 'LIVE' if root.get('type') == 'dynamic' else 'VOD'
        candidate.duration = self._dash_duration(root.get('mediaPresentationDuration'))
        representations = []

        def visit(node, base, inherited, template=None, segment_list=None, segment_base=None):
            attributes = {**inherited, **node.attrib}
            base_element = next((child for child in node if _local(child.tag) == 'BaseURL' and child.text), None)
            if base_element is not None:
                base = urljoin(base, base_element.text.strip())
            own_template = next((child for child in node if _local(child.tag) == 'SegmentTemplate'), None)
            if own_template is not None:
                template = {**(template or {}), **own_template.attrib}
            own_list = next((child for child in node if _local(child.tag) == 'SegmentList'), None)
            if own_list is not None:
                segment_list = own_list
            own_base = next((child for child in node if _local(child.tag) == 'SegmentBase'), None)
            if own_base is not None:
                segment_base = own_base
            if _local(node.tag) == 'Representation':
                dimensions = _dimension(attributes.get('width', 0), attributes.get('height', 0))
                bandwidth = _integer(attributes.get('bandwidth', 0))
                if _quality(dimensions) > _quality(candidate.resolution):
                    candidate.resolution = dimensions
                    candidate.metadata['codecs'] = attributes.get('codecs', '')[:512]
                    candidate.metadata['frame_rate'] = attributes.get('frameRate', '')[:64]
                candidate.bandwidth = max(candidate.bandwidth, bandwidth)
                if len(representations) < MAX_CANDIDATES:
                    representations.append({'id': attributes.get('id', '')[:128], 'width': dimensions[0],
                                            'height': dimensions[1], 'bandwidth': bandwidth,
                                            'codecs': attributes.get('codecs', '')[:512],
                                            'mime_type': attributes.get('mimeType', '')[:128],
                                            'content_type': attributes.get('contentType', '')[:32],
                                            'frame_rate': attributes.get('frameRate', '')[:64]})
                substitutions = {'RepresentationID': attributes.get('id', ''), 'Bandwidth': str(bandwidth)}

                def target(value):
                    for key, replacement in substitutions.items():
                        value = value.replace('$' + key + '$', replacement)
                    return urljoin(base, value)

                if template:
                    if template.get('initialization'):
                        self._register_role(candidate, target(template['initialization']), 'Initialization Segment')
                    if template.get('media'):
                        self._register_template(candidate, target(template['media']))
                if segment_list is not None:
                    for element in segment_list:
                        tag = _local(element.tag)
                        uri = element.get('media' if tag == 'SegmentURL' else 'sourceURL', '')
                        if uri and tag in ('SegmentURL', 'Initialization'):
                            self._register_role(candidate, target(uri), 'DASH Media Segment' if tag == 'SegmentURL' else 'Initialization Segment')
                if segment_base is not None:
                    for element in segment_base:
                        if _local(element.tag) == 'Initialization':
                            self._register_role(candidate, target(element.get('sourceURL', '')), 'Initialization Segment')
                if base_element is not None and not urlsplit(base).path.endswith('/'):
                    self._register_role(candidate, base, 'DASH Media Segment')
            for child in node:
                if _local(child.tag) in ('Period', 'AdaptationSet', 'Representation'):
                    visit(child, base, attributes, template, segment_list, segment_base)

        visit(root, candidate.final_url, {})
        candidate.metadata['representations'] = representations

    def consume_dom(self, snapshot: dict):
        self._sequence += 1
        snapshot = deepcopy(snapshot)
        frame_id = str(snapshot.get('frame_id', ''))
        self.page_url = str(snapshot.get('page_url') or self.page_url)
        snapshot['videos'] = list(snapshot.get('videos') or [])[:32]
        self._frames[frame_id] = snapshot
        self._frames.move_to_end(frame_id)
        while len(self._frames) > 64:
            removed, _ = self._frames.popitem(last=False)
            self._seek_sequence.pop(removed, None)
            self._seek_events = {key: value for key, value in self._seek_events.items() if key[0] != removed}
        for index, video in enumerate(snapshot['videos']):
            events = [event for event in video.get('events', [])[-64:] if isinstance(event, dict)]
            seek = tuple((event.get('type'), event.get('timestamp')) for event in events
                         if event.get('type') in ('seeking', 'seeked'))
            if video.get('seeking') and not seek:
                seek = (('seeking', snapshot.get('timestamp')),)
            if seek and seek != self._seek_events.get((frame_id, index)):
                self._seek_events[(frame_id, index)] = seek
                self._seek_sequence[frame_id] = self._sequence
            current = video.get('current_src') or video.get('src') or ''
            declarations = [(current, video.get('type', ''))]
            declarations += [(source.get('src', ''), source.get('type', '')) for source in (video.get('sources') or [])[:32]]
            for url, declared_type in declarations:
                target = _http_url(url, self.page_url)
                if not target or target in self._excluded or target in self._excluded_roles or self._role_for({target}):
                    continue
                mime = _mime(declared_type)
                if mime.startswith(('image/', 'text/')) and mime not in _HLS_MIMES:
                    continue
                candidate = self._new(RequestContext(target), 'DOM', frame_id, False)
                if is_verified_resolver(candidate):
                    continue
                candidate.metadata['source_page'] = self.page_url
                candidate.metadata['page_title'] = str(snapshot.get('page_title') or '')[:512]
                if video.get('title'):
                    candidate.metadata['title'] = str(video['title'])[:512]
                if video.get('element_id'):
                    candidate.metadata['dom_video_id'] = str(video['element_id'])[:128]
                if video.get('video_id'):
                    candidate.metadata['video_id'] = str(video['video_id'])[:128]
                if video.get('poster'):
                    candidate.metadata['thumbnail'] = str(video['poster'])[:8192]
                if not candidate.media_type:
                    candidate.media_type = 'HLS' if mime in _HLS_MIMES or urlsplit(target).path.lower().endswith('.m3u8') else 'DASH' if mime == 'application/dash+xml' or urlsplit(target).path.lower().endswith('.mpd') else 'Direct'
                if target == _http_url(current, self.page_url):
                    candidate.confirmed = candidate.confirmed or bool(_number(video.get('duration'))
                                                                  or _integer(video.get('width')) or not video.get('paused', True))
        self._refresh()
        self._bound_candidates()

    @staticmethod
    def _eme(video):
        events = video.get('events') or []
        return bool(video.get('media_keys') or video.get('encrypted')
                    or any(isinstance(event, dict) and event.get('type') == 'encrypted' for event in events))

    @staticmethod
    def _retain_protection(candidate):
        """同一轮同一候选的明确保护证据不能被迟到的普通响应擦除。"""
        manifest = candidate.metadata.get('manifest', {})
        protection = candidate.metadata.get('protection_evidence')

        def merge(old, new):
            values = deepcopy(old)
            for item in new:
                if item not in values:
                    values.append(deepcopy(item))
                if len(values) >= MAX_CANDIDATES:
                    break
            return values[:MAX_CANDIDATES]

        if candidate.drm is True:
            evidence = list(manifest.get('evidence', []))
            if candidate.metadata.get('eme_evidence'):
                evidence.append('对应 DOM 播放器已出现实际 EME 保护证据。')
            if protection is None:
                protection = {'classification': candidate.classification, 'encryption': candidate.encryption,
                              'keys': [], 'evidence': []}
                candidate.metadata['protection_evidence'] = protection
            elif protection['classification'] in ('', 'DRM_UNKNOWN') and candidate.classification.startswith('DRM_'):
                protection['classification'] = candidate.classification
                protection['encryption'] = candidate.encryption
            protection['keys'] = merge(protection['keys'], manifest.get('keys', []))
            protection['evidence'] = merge(protection['evidence'], evidence)
        if protection is not None:
            candidate.drm = True
            candidate.classification = protection['classification']
            candidate.encryption = protection['encryption']
            manifest = deepcopy(manifest)
            manifest.update(drm=True, classification=candidate.classification, encryption=candidate.encryption,
                            keys=merge(protection['keys'], manifest.get('keys', [])),
                            evidence=merge(protection['evidence'], manifest.get('evidence', [])))
            candidate.metadata['manifest'] = manifest

    def _associate_video(self, candidate, video, is_main, blob=False, current=False):
        self._bind_confidence(candidate, video, is_main, current)
        if current and 'subtitle_tracks' in video:
            from core.media_tracks import html_subtitles
            tracks = html_subtitles(video['subtitle_tracks'], candidate.metadata.get('source_page') or self.page_url)
            candidate.metadata['page_subtitles'] = tracks
            for track in tracks:
                self._register_role(candidate, track['url'], 'Subtitle')
        if is_verified_resolver(candidate):
            return
        candidate.sources.add('MSE' if blob else 'DOM')
        for key, value in (('title', video.get('title')), ('dom_video_id', video.get('element_id')),
                           ('video_id', video.get('video_id')), ('thumbnail', video.get('poster'))):
            if value:
                candidate.metadata[key] = str(value)[:8192 if key == 'thumbnail' else 512]
        snapshot = self._frames.get(candidate.frame_id, {})
        candidate.metadata.setdefault('source_page', str(snapshot.get('page_url') or self.page_url))
        candidate.metadata.setdefault('page_title', str(snapshot.get('page_title') or '')[:512])
        candidate.metadata['dom_current'] = candidate.metadata.get('dom_current', False) or current
        candidate.metadata['dom_playing'] = candidate.metadata.get('dom_playing', False) or (not video.get('paused', True) and not video.get('ended', False))
        candidate.metadata['dom_main'] = candidate.metadata.get('dom_main', False) or is_main
        candidate.metadata['dom_visible'] = candidate.metadata.get('dom_visible', False) or video.get('visible', True)
        candidate.metadata['player_type'] = 'BLOB_MSE' if blob else 'HTML5_VIDEO'
        if candidate.http_status in (0, 200, 206, 304) and (not video.get('paused', True) or _number(video.get('duration'))):
            candidate.confirmed = True
        duration = _number(video.get('duration'))
        if duration and (candidate.video_track is None or not candidate.duration):
            candidate.duration = duration
        dimensions = _dimension(video.get('width'), video.get('height'))
        if all(dimensions) and candidate.video_track is None:
            candidate.resolution = dimensions
        if (current and not blob and candidate.context_complete and candidate.metadata.get('partial_resource')
                and not candidate.metadata.get('segment_mode') and _number(video.get('duration')) > 0
                and candidate.content_type.startswith(('video/', 'audio/'))):
            # 普通 video.currentSrc 的完整资源范围，是单次 206 之外的协议证据。
            # MSE/已声明 Segment 不走此分支；部分初始化 Range 也不能通过。
            headers = {str(k).lower(): v for k, v in candidate.metadata.get('response_headers', {}).items()}
            try:
                interval = parse_content_range(headers.get('content-range'))
                full = interval.start == 0 and interval.length == interval.total
                full = full and _integer(headers.get('content-length')) == interval.total
            except (ValueError, TypeError):
                full = False
            if full:
                candidate.media_type = 'Direct'
                candidate.metadata['protocol_validated'] = 'Direct'
                candidate.metadata['direct_player_verified'] = True
                for key in ('fragment_only', 'partial_resource', 'resource_role', 'error_code', 'probe_error'):
                    candidate.metadata.pop(key, None)
        if self._eme(video):
            candidate.metadata['eme_evidence'] = True
            candidate.drm = True
            candidate.encryption = 'CENC'
            system = str(video.get('media_keys', '')).lower()
            candidate.classification = 'DRM_WIDEVINE' if 'widevine' in system else 'DRM_PLAYREADY' if 'playready' in system else 'DRM_FAIRPLAY' if 'fairplay' in system or 'streamingkeydelivery' in system else 'DRM_UNKNOWN'

    def apply_progressive_probe(self, identifier, root):
        candidate = self.get(identifier)
        if (not candidate or candidate.metadata.get('segment_mode') or candidate.metadata.get('parent_manifest')
                or not verified_response(candidate.metadata.get('progressive_mp4'), root, candidate.final_url)):
            return False
        candidate.request_context = full_resource_context(candidate.request_context)
        candidate.metadata.update(protocol_validated='Direct', progressive_direct_verified=True,
                                  response_headers=deepcopy(root.response_headers))
        for key in ('fragment_only', 'partial_resource', 'resource_role', 'error_code', 'probe_error'):
            candidate.metadata.pop(key, None)
        return True

    @staticmethod
    def _intrinsic_match(candidate, video):
        proof = candidate.metadata.get('progressive_mp4', {})
        return bool(proof and video.get('tag', 'VIDEO') != 'AUDIO'
                    and _dimension(video.get('width'), video.get('height')) == (proof['width'], proof['height'])
                    and _number(video.get('duration')) > 0
                    and abs(_number(video.get('duration')) - proof['duration']) <= 0.25)

    def _progressive_player(self, candidate, frame_id, siblings, index):
        if not candidate.metadata.get('progressive_direct_verified'):
            return False
        if any(c.frame_id == frame_id and c.media_type in ('HLS', 'DASH') for c in self.candidates.values()):
            return False
        related = [c for c in self.candidates.values() if c.frame_id == frame_id and 'Network' in c.sources
                   and c.media_type in ('Direct', 'Segment') and not c.is_ad
                   and not c.content_type.startswith('audio/') and not c.metadata.get('parent_manifest')]
        def ruled_out(resource):
            intrinsic = resource.metadata.get('intrinsic_mp4', {})
            if not intrinsic.get('resource_version') or intrinsic.get('unknown_track_count') != 0:
                return False
            if intrinsic.get('video_track_count') == 0 and intrinsic.get('audio_track_count',0) > 0:
                return True
            if intrinsic.get('video_track_count') != 1:
                return False
            dimensions = _dimension(intrinsic.get('width'), intrinsic.get('height'))
            current = siblings[index]
            if all(dimensions) and dimensions != _dimension(current.get('width'), current.get('height')):
                return True
            # 分片的局部 duration 不能被当成整段时长来排除竞争者。
            duration = _number(intrinsic.get('duration')) if resource.metadata.get('progressive_mp4') else 0
            return bool(duration and _number(current.get('duration'))
                        and abs(duration - _number(current.get('duration'))) > 0.25)
        # 完整 moov 已证明是音频或尺寸/时长不匹配的轨道，不是未知竞争项。
        related = [c for c in related if not ruled_out(c)]
        # 未知/未验证竞争资源不能被排除以制造唯一匹配。
        if any(not c.metadata.get('progressive_mp4') for c in related):
            return False
        videos = [(i,v) for i,v in enumerate(siblings)
                  if str(v.get('current_src') or v.get('src', '')).startswith('blob:')]
        if any(not _number(v.get('duration')) or not all(_dimension(v.get('width'), v.get('height')))
               for _,v in videos):
            return False
        matches = [i for i,v in videos if self._intrinsic_match(candidate, v)]
        return (matches == [index] and sum(self._intrinsic_match(c, siblings[index]) for c in related) == 1)

    @staticmethod
    def _bind_confidence(candidate, video, is_main, current=False):
        evidence = candidate.metadata.setdefault('confidence_evidence', {})
        evidence['associated'] = True
        if video.get('tag') == 'AUDIO':
            return
        visible = video.get('visible')
        if visible is True:
            evidence.update(visible=True, hidden=False)
        elif visible is False and evidence.get('visible') is not True:
            evidence.update(visible=False, hidden=True)
        evidence['current_player'] = bool(evidence.get('current_player') or
            current and is_main and visible is not False and video.get('current_src'))

    def _update_confidence(self, candidate):
        enrich_media_details(candidate)
        evidence = deepcopy(candidate.metadata.get('confidence_evidence', {}))
        parent = self.get(candidate.metadata.get('parent_manifest', ''))
        if parent and not evidence.get('associated'):
            evidence = deepcopy(parent.metadata.get('confidence_evidence', {}))
        evidence['resolver_entry'] = bool(is_verified_resolver(candidate) or
            'Resolver' in candidate.sources and candidate.validated and candidate.manifest_text
            and candidate.media_type in ('HLS', 'DASH'))
        path = unquote(urlsplit(candidate.final_url).path)
        role = next((str(candidate.metadata.get(key, '')).strip().lower()
                     for key in ('resource_role', 'role', 'resource_kind') if candidate.metadata.get(key)), '')
        explicit_thumbnail = bool(candidate.metadata.get('is_thumbnail') or
            role in ('thumbnail', 'poster', 'sprite', 'thumbnail_track', 'thumbnail track'))
        thumbnail = bool(explicit_thumbnail or re.search(
            r'(?<![a-z0-9])(?:thumbnail|thumbs|poster|sprite)(?![a-z0-9])', path, re.I))
        if candidate.media_type in ('HLS', 'DASH'):
            thumbnail = explicit_thumbnail
        evidence['thumbnail'] = thumbnail
        evidence['preview'] = bool(candidate.metadata.get('is_preview') or role in ('preview', 'trailer', 'sample') or
            candidate.metadata.get('preview') and not thumbnail)
        rating = score_candidate(candidate, evidence)
        candidate.confidenceScore = rating.score
        candidate.confidenceReasons = rating.reasons
        if candidate.metadata.get('association_kind') == 'unique_intrinsic_mp4':
            candidate.confidenceReasons.append('根据当前页面中唯一匹配的 MP4 内在时长与尺寸推断播放器关联。')
        candidate.metadata['confidence_details'] = rating.details

    def _refresh(self):
        all_videos = [(frame, index, video) for frame, snapshot in self._frames.items()
                      for index, video in enumerate(snapshot.get('videos', []))]
        self.player_type = 'BLOB_MSE' if any(str(video.get('current_src') or video.get('src', '')).lower().startswith('blob:')
                                           for _, _, video in all_videos) else 'HTML5_VIDEO' if all_videos else ''
        for candidate in self.candidates.values():
            candidate.metadata['confidence_evidence'] = {}
            candidate.metadata.pop('association_kind', None)
            if is_verified_resolver(candidate):
                continue
            for key in ('dom_current', 'dom_playing', 'dom_main', 'dom_visible'):
                candidate.metadata[key] = False
            candidate.is_ad = bool(candidate.metadata.get('vast_ad') or candidate.metadata.get('ad_initiator')
                                   or _AD_WORDS.search(_ad_text(candidate.final_url)))
        main_indices = {frame: _primary_video(snapshot.get('videos', [])) for frame, snapshot in self._frames.items()}
        for frame_id, index, video in all_videos:
            siblings = self._frames[frame_id].get('videos', [])
            is_main = index == main_indices[frame_id]
            current = video.get('current_src') or video.get('src') or ''
            if str(current).lower().startswith('blob:'):
                plausible = [candidate for candidate in self.candidates.values()
                             if candidate.frame_id == frame_id and candidate.media_type in ('HLS', 'DASH', 'Direct')
                             and not is_verified_resolver(candidate) and not candidate.is_ad
                             and ('Network' in candidate.sources)]
                for candidate in plausible:
                    candidate.sources.add('MSE')
                    candidate.metadata['player_type'] = 'BLOB_MSE'
                roots = set()
                for candidate in plausible:
                    root = candidate
                    for _ in range(5):
                        parent = self.get(root.metadata.get('parent_manifest', ''))
                        if parent is None:
                            break
                        root = parent
                    roots.add(root.metadata.get('dash_description_id') or root.id)
                if len(roots) == 1:
                    for candidate in plausible:
                        if candidate.media_type != 'Direct':
                            self._associate_video(candidate, video, is_main, blob=True, current=True)
                        elif (not candidate.metadata.get('progressive_mp4')
                              and not candidate.content_type.startswith('audio/')
                              and len([v for v in siblings if str(v.get('current_src') or v.get('src','')).startswith('blob:')]) == 1
                              and not any(c.media_type == 'Segment' and c.frame_id == frame_id for c in self.candidates.values())):
                            self._associate_video(candidate, video, is_main, blob=True, current=True)
                for candidate in plausible:
                    if candidate.media_type == 'Direct' and self._progressive_player(candidate, frame_id, siblings, index):
                        candidate.metadata['association_kind'] = 'unique_intrinsic_mp4'
                        self._associate_video(candidate, video, is_main, blob=True, current=True)
                verified = [candidate for candidate in self.candidates.values() if is_verified_resolver(candidate)
                            and candidate.metadata.get('page_url') == self._frames[frame_id].get('page_url')]
                if len(verified) == 1 and len([v for v in siblings if str(v.get('current_src', '')).startswith('blob:')]) == 1:
                    self._bind_confidence(verified[0], video, is_main, current=False)
            else:
                target = _http_url(current, self._frames[frame_id].get('page_url', self.page_url))
                candidate = self.get(self._urls.get(target, ''))
                if candidate:
                    self._associate_video(candidate, video, is_main, current=True)
        for candidate in self.candidates.values():
            if is_verified_resolver(candidate):
                self._score(candidate)
                continue
            parent = self.get(candidate.metadata.get('parent_manifest', ''))
            if parent:
                for key in ('title', 'page_title', 'video_id', 'resolver_id', 'page_url', 'source_page', 'thumbnail', 'dom_video_id'):
                    if parent.metadata.get(key) and not candidate.metadata.get(key):
                        candidate.metadata[key] = parent.metadata[key]
            if candidate.metadata.get('eme_evidence'):
                candidate.drm = True
            self._retain_protection(candidate)
            candidate.source_type = ' / '.join(name for name in ('Resolver', 'DOM', 'Network', 'Manifest', 'MSE') if name in candidate.sources)
            self._score(candidate)

    def _score(self, candidate):
        if is_verified_resolver(candidate):
            candidate.score = 100
            candidate.score_reasons = [{'points': 100, 'reason': 'Resolver 已按实际响应完整验证媒体协议与正片入口'}]
            self._update_confidence(candidate)
            return
        reasons = []

        def add(points, reason):
            reasons.append({'points': points, 'reason': reason})

        if candidate.confirmed:
            add(20, '已识别的媒体响应或实际视频元素')
        if 'Resolver' in candidate.sources:
            add(35, '当前播放页的站点解析器已验证媒体入口')
        if 'Network' in candidate.sources:
            add(8, '已捕获网络请求上下文')
        if candidate.content_type.startswith('video/'):
            add(15, '响应 Content-Type 为 video/*')
        if candidate.http_status == 206 and candidate.request_context.headers.get('range'):
            add(5, '206 Partial Content 与实际 Range 媒体读取')
        if candidate.media_type in ('HLS', 'DASH') and candidate.manifest_type in ('Master', 'MPD'):
            add(12, '多清晰度或自适应 Manifest')
        if 'Manifest' in candidate.sources:
            add(10, 'Manifest 声明的媒体 Variant')
        if candidate.metadata.get('dom_current'):
            add(40, '当前 video.currentSrc 或唯一 MSE 网络关联')
        if candidate.metadata.get('dom_playing'):
            add(12, '对应视频正在播放')
        if candidate.metadata.get('dom_main'):
            add(10, '来自主视频元素')
        if candidate.duration and candidate.playlist_type != 'LIVE':
            if candidate.duration >= 120:
                add(10, '媒体持续时间较长')
            elif candidate.duration >= 30:
                add(5, '媒体具有稳定时长')
            elif candidate.duration < 15:
                add(-4, '媒体持续时间较短')
        height = candidate.resolution[1]
        if height >= 1080:
            add(12, '实际声明或视频元数据达到 1080p')
        elif height >= 720:
            add(8, '实际声明或视频元数据达到 720p')
        elif height >= 480:
            add(4, '实际声明或视频元数据达到 480p')
        segments = candidate.metadata.get('segment_requests', 0)
        if segments >= 2:
            add(12, '连续分片请求对应此 Manifest')
        if candidate.metadata.get('seek_range'):
            add(15, '用户拖动后继续出现 Range / Segment 请求')
        page_ids = [value for key, value in parse_qsl(urlsplit(self.page_url).query)
                    if key.lower() in ('id', 'v', 'video', 'video_id', 'media_id') and len(value) >= 4]
        target_text = unquote(urlsplit(candidate.final_url).path)
        if any(re.search(r'(?<![\w])' + re.escape(value) + r'(?![\w])', target_text) for value in page_ids):
            add(6, '媒体地址与页面媒体标识关联')
        if candidate.is_ad:
            add(-55, 'VAST、广告请求词或广告 initiator')
        if candidate.metadata.get('preview'):
            add(-20, '预览或缩略图请求特征')
        if candidate.drm is True:
            add(-25, '明确 DRM 证据，仅展示受保护资源详情')
        if not candidate.context_complete:
            add(-15, '尚未捕获完整可复现请求上下文')
        if candidate.http_status and not 200 <= candidate.http_status < 300:
            add(-20, 'HTTP 响应未成功')
        candidate.score_reasons = reasons
        candidate.score = max(0, min(99, sum(item['points'] for item in reasons)))
        self._update_confidence(candidate)

    def apply_probe(self, candidate_id, diagnostic):
        candidate = self.get(candidate_id)
        if candidate is None or is_verified_resolver(candidate):
            return
        if candidate.video_track and candidate.manifest_text:
            # 迟到的裸轨道探测不可以取代已配对清单；真正保护证据仍保留。
            if diagnostic.get('drm') is True:
                candidate.drm = True
                candidate.classification = diagnostic.get('classification', 'DRM_UNKNOWN')
                candidate.encryption = diagnostic.get('encryption', 'CENC')
                self._retain_protection(candidate)
                self._refresh()
            return
        diagnostic = deepcopy(diagnostic)
        if not diagnostic.get('error_code'):
            candidate.metadata.pop('probe_error', None)
            candidate.metadata.pop('error_code', None)
        if diagnostic.get('error_code'):
            # HTTP 403/认证失败不提供 DRM 证据；已有已验证保护结论保留。
            redactor = _context_redactor(candidate.request_context)
            candidate.metadata['error_code'] = str(diagnostic['error_code'])
            candidate.metadata['probe_error'] = redactor.redact(str(diagnostic.get('probe_error') or diagnostic.get('error') or diagnostic.get('message') or diagnostic['error_code']))
            if diagnostic.get('drm') is None and candidate.drm is not None:
                diagnostic.pop('drm', None)
        if 'http_status' in diagnostic:
            candidate.http_status = _integer(diagnostic['http_status'])
        media_type = diagnostic.get('media_type') or diagnostic.get('protocol')
        if media_type in ('HLS', 'DASH', 'Direct'):
            candidate.media_type = media_type
            if not diagnostic.get('error_code'):
                candidate.confirmed = True
        if any(key in diagnostic for key in _MANIFEST_FIELDS):
            self._apply_manifest(candidate, diagnostic)
            if 'drm' in diagnostic and diagnostic['drm'] in (True, False, None):
                # guard 的明确 False 是检查完整 moov / Manifest 的结果。
                candidate.drm = diagnostic['drm']
                if diagnostic['drm'] is False:
                    candidate.encryption = diagnostic.get('encryption', candidate.encryption)
        if candidate.drm is True:
            candidate.confirmed = True
        resolution = diagnostic.get('resolution')
        if isinstance(resolution, (tuple, list)) and len(resolution) == 2:
            dimensions = _dimension(*resolution)
            if _quality(dimensions) > _quality(candidate.resolution):
                candidate.resolution = dimensions
        self._refresh()
        self._bound_candidates()

    def ranked(self, limit=12, include_low=False):
        self._refresh()
        for group in self._orphan_groups.values():
            remaining = [url for url in group if url not in self._excluded_roles and not self._role_for({url})]
            if len(remaining) >= 2:
                for url in remaining:
                    candidate = self.get(self._urls.get(url))
                    if candidate and not is_verified_resolver(candidate):
                        candidate.confirmed = False
                        candidate.metadata['error_code'] = 'ONLY_SEGMENTS_FOUND'
                        candidate.metadata['probe_error'] = '只观察到连续媒体分片，缺少完整 Manifest，无法确定整段视频。'
        # 保留低分证据；下载资格由协议/验证状态决定，不以可信度拒绝手动选择。
        candidates = [candidate for candidate in self.candidates.values()
                      if include_low or not candidate.is_ad and (candidate.confirmed or candidate.drm is True)]
        candidates.sort(key=lambda candidate: (
            candidate.drm is not True,
            candidate.downloadable and not candidate.is_ad,
            not candidate.is_ad, candidate.confidence, is_verified_resolver(candidate),
            _quality(candidate.resolution), candidate.bandwidth), reverse=True)
        return deepcopy(candidates[:max(0, min(_integer(limit), MAX_CANDIDATES))])

    def discovery_diagnostic(self):
        filtered = {}
        media_requests = 0
        for observation in self._network.values():
            aliases = _aliases(observation.context)
            owners = self._role_for(aliases)
            role = next((self._excluded_roles[url] for url in aliases if url in self._excluded_roles), '')
            role = role or next(iter(owners.values()), '')
            if not role and aliases.intersection(self._excluded):
                role = '非媒体响应'
            if role:
                filtered[role] = filtered.get(role, 0) + 1
            if owners or aliases.intersection(self._urls) or role and role != '非媒体响应':
                media_requests += 1
        result = {'statistics': {'page_requests': len(self._network), 'media_requests': media_requests,
                  'dash_descriptions': len(self._dash_descriptions),
                  'dash_inline_descriptions': sum(bool(item.get('embedded')) for item in self._dash_descriptions.values()),
                  'dash_video_tracks': sum(item['video_tracks'] for item in self._dash_descriptions.values()),
                  'dash_audio_tracks': sum(item['audio_tracks'] for item in self._dash_descriptions.values()),
                  'candidates': len(self.candidates), 'filtered': sum(filtered.values()), 'filtered_reasons': filtered}}
        if any(len([url for url in group if url not in self._excluded_roles and not self._role_for({url})]) >= 2
               for group in self._orphan_groups.values()):
            result.update(error_code='ONLY_SEGMENTS_FOUND', message='发现孤立媒体分片，尚未关联到完整清单；其他完整候选仍可手动选择。')
        elif self.candidates and all(candidate.is_ad for candidate in self.candidates.values()):
            result.update(error_code='ONLY_AD_MEDIA_FOUND', message='只发现广告媒体，请播放正片。')
        return result
