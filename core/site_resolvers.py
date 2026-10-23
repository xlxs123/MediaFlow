"""只从当前合法页面数据与实际观测生成媒体候选或有限浏览器请求计划。"""
from __future__ import annotations

from abc import ABC, abstractmethod
import base64
import binascii
from copy import deepcopy
from dataclasses import replace
from html.parser import HTMLParser
import json
import math
import re
import time
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from core.discovery_models import MediaCandidate, NetworkObservation
from core.errors import MediaCheckError
from core.manifests import parse_manifest
from core.media_discovery import MediaDiscoveryService
from core.request_context import RequestContext
from core.resolver_models import PageContext, ResolveResult, ResolverRequest
from core.rou_playlist_decoder import DEFAULT_MAX_SIZE, RouPlaylistDecoder
from core.url_policy import UrlResolutionPolicy
from utils.security import Redactor


_MEDIA_FIELDS = frozenset(('videourl', 'url', 'src', 'source', 'stream', 'playlist', 'manifest', 'media', 'hls', 'file'))
_SIGNED_FIELDS = frozenset(('sign', 'token', 'secure', 'expires', 'exp', 't', 'auth', 'signature'))
_MAX_OBSERVATIONS = 512
_MAX_ENTRIES = 16


class SiteResolver(ABC):
    resolver_id = ''

    @abstractmethod
    def can_handle(self, page: PageContext) -> bool:
        raise NotImplementedError

    @abstractmethod
    def resolve(self, page: PageContext) -> ResolveResult:
        raise NotImplementedError


class ResolverRegistry:
    def __init__(self, resolvers=None):
        self.resolvers = list(resolvers) if resolvers is not None else [RouVideoResolver()]

    def register(self, resolver):
        if not isinstance(resolver, SiteResolver):
            raise TypeError('站点解析器必须实现 SiteResolver 接口。')
        self.resolvers.append(resolver)
        return resolver

    def resolve(self, page: PageContext) -> ResolveResult:
        diagnostics = []
        matched = ''
        for resolver in self.resolvers:
            try:
                if not resolver.can_handle(page):
                    continue
                matched = resolver.resolver_id
                result = resolver.resolve(page)
            except Exception:
                diagnostics.append({'error_code': 'RESOLVER_ERROR',
                                    'message': '站点解析器处理失败，继续使用通用媒体发现。'})
                continue
            if result.success or result.requests:
                result.diagnostics = diagnostics + result.diagnostics
                return result
            diagnostics.extend(result.diagnostics)
        if not diagnostics:
            diagnostics.append({'error_code': 'NO_SITE_RESOLVER',
                                'message': '当前页面没有专用解析器，继续使用通用媒体发现。'})
        return ResolveResult(diagnostics=diagnostics, resolver_id=matched)


class _NextScriptParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'script' and dict(attrs).get('id') == '__NEXT_DATA__':
            self.active = True

    def handle_endtag(self, tag):
        if tag.lower() == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def _url(value, base):
    if (not isinstance(value, str) or not value or len(value) > 16384
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return ''
    try:
        target = urljoin(base, value)
        RequestContext(target)
        return target
    except (ValueError, TypeError):
        return ''


def _thumbs(url):
    return any(key.lower() == 'kind' and value.lower() in ('thumbs', 'thumbnail', 'thumbnails')
               for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _urls_from_values(value, base):
    result = []
    pending = [(value, 0)]
    visited = 0
    while pending and visited < 1024 and len(result) < _MAX_ENTRIES:
        item, depth = pending.pop(0)
        visited += 1
        if depth > 8:
            continue
        if isinstance(item, dict):
            for key, child in list(item.items())[:128]:
                if str(key).lower() in _MEDIA_FIELDS and isinstance(child, str):
                    target = _url(child, base)
                    if target and not _thumbs(target) and target not in result:
                        result.append(target)
                elif isinstance(child, (dict, list)):
                    pending.append((child, depth + 1))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item[:128] if isinstance(child, (dict, list)))
    return result


class RouVideoResolver(SiteResolver):
    resolver_id = 'rou.video'

    def __init__(self, legacy_fallback=True):
        self.legacy_fallback = legacy_fallback

    def can_handle(self, page):
        try:
            parsed = urlsplit(page.url)
            host = (parsed.hostname or '').lower().rstrip('.')
            return parsed.scheme.lower() in ('http', 'https') and (host == 'rou.video' or host.endswith('.rou.video'))
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def extract_video_id(url):
        try:
            validate = RequestContext(url)
            match = re.fullmatch(r'/v/([^/]+)/?', urlsplit(validate.url).path)
        except (ValueError, TypeError, AttributeError):
            return None
        return match.group(1) if match else None

    @staticmethod
    def decode_ev(ev):
        if (not isinstance(ev, dict) or not isinstance(ev.get('d'), str)
                or type(ev.get('k')) not in (int, float) or not math.isfinite(ev['k'])
                or int(ev['k']) != ev['k']):
            raise MediaCheckError('当前页面 ev 字段不完整。', 'ROU_EV_INVALID')
        encoded, shift = ev['d'], int(ev['k'])
        if len(encoded) > DEFAULT_MAX_SIZE or abs(shift) > 65535:
            raise MediaCheckError('当前页面 ev 数据超过安全范围。', 'ROU_EV_INVALID')
        encoded = re.sub(r'\s', '', encoded)
        try:
            raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        except (ValueError, binascii.Error):
            raise MediaCheckError('当前页面 ev Base64 不能解析。', 'ROU_EV_INVALID') from None
        text_variants = []
        try:
            text_variants.append(raw.decode('utf-8'))
        except UnicodeDecodeError:
            pass
        text_variants.append(raw.decode('latin-1'))
        for text in dict.fromkeys(text_variants):
            try:
                values = [ord(char) - shift for char in text]
                if any(value < 0 or value > 0x10ffff or 0xd800 <= value <= 0xdfff for value in values):
                    continue
                result = json.loads(''.join(chr(value) for value in values))
            except (ValueError, OverflowError, RecursionError):
                continue
            if isinstance(result, dict):
                return result
        raise MediaCheckError('当前页面 ev 解码后不是有效 JSON。', 'ROU_EV_INVALID')

    @staticmethod
    def _page_props(next_data):
        if next_data is None:
            return {}
        if isinstance(next_data, str):
            try:
                size = len(next_data.encode('utf-8'))
            except UnicodeEncodeError:
                raise MediaCheckError('当前页面数据不是有效 Unicode 文本。', 'ROU_NEXT_DATA_INVALID') from None
            if size > DEFAULT_MAX_SIZE:
                raise MediaCheckError('当前页面数据超过大小上限。', 'ROU_NEXT_DATA_INVALID')
            text = next_data.strip()
            if text.startswith('<'):
                parser = _NextScriptParser()
                parser.feed(text)
                text = ''.join(parser.parts)
            try:
                next_data = json.loads(text)
            except (ValueError, RecursionError):
                raise MediaCheckError('当前页面 NEXT_DATA 不能解析。', 'ROU_NEXT_DATA_INVALID') from None
        if not isinstance(next_data, dict):
            raise MediaCheckError('当前页面 NEXT_DATA 结构不正确。', 'ROU_NEXT_DATA_INVALID')
        props = next_data.get('props', {})
        page_props = props.get('pageProps') if isinstance(props, dict) else None
        page_props = page_props if isinstance(page_props, dict) else next_data.get('pageProps', {})
        return page_props if isinstance(page_props, dict) else {}

    def _diagnostic(self, code, message, video_id='', observation=None):
        result = {'error_code': code, 'message': message, 'resolver_id': self.resolver_id}
        if video_id:
            result['video_id'] = video_id
        if observation is not None:
            result['http_status'] = observation.status
            result['url'] = Redactor(urls=[observation.context.url, observation.context.final_url]).redact(observation.context.final_url)
        return result

    @staticmethod
    def _siblings(entry):
        # Legacy 仅请求页面/API 实際下发的 URL，也不探测相邻文件名。
        return [entry]

    @staticmethod
    def _observation_urls(observation):
        return {observation.context.url, observation.context.original_url, observation.context.final_url}

    @staticmethod
    def _candidate(observation, decoded, page, video_id):
        context = replace(deepcopy(observation.context), playlist_decoder='rou',
                          url_resolution_policy=UrlResolutionPolicy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING)
        service = MediaDiscoveryService()
        service.consume_network(replace(observation, context=context, body=decoded))
        candidate = next((item for item in service.candidates.values() if item.url == context.url), None)
        if candidate is None or candidate.media_type != 'HLS':
            return None
        candidate.id = 'rou-' + video_id
        candidate.source_type = 'RouVideoResolver'
        candidate.transport_url = context.url
        candidate.manifest_url = context.final_url
        candidate.manifest_text = decoded.decode('utf-8-sig')
        candidate.manifest_origin = ('embedded:png/roUd' if observation.body and
                                     observation.body.startswith(b'\x89PNG\r\n\x1a\n') else 'captured:hls')
        candidate.validated = True
        candidate.sources.add('Resolver')
        candidate.confirmed = True
        candidate.context_complete = True
        candidate.score = max(90, candidate.score)
        candidate.score_reasons.append({'points': 30, 'reason': '当前播放页 Resolver 与实际合法媒体响应一致'})
        query = parse_qsl(urlsplit(context.final_url).query, keep_blank_values=True)
        signed = any(key.lower() in _SIGNED_FIELDS and value for key, value in query)
        expiry = next((key for key, _ in query if key.lower() in ('expires', 'expiry', 'exp')), '')
        observed_at = observation.timestamp
        if type(observed_at) not in (int, float) or not math.isfinite(observed_at) or observed_at <= 0:
            observed_at = time.time()
        candidate.metadata.update(resolver_id='rou.video', video_id=video_id, page_url=page.url,
                                  signed_url=True if signed else None, discovered_at=observed_at,
                                  expires_hint='媒体地址包含过期时间参数。' if expiry else '',
                                  url_resolution_policy=str(context.url_resolution_policy))
        try:
            props = RouVideoResolver._page_props(page.next_data)
        except MediaCheckError:
            props = {}
        video = props.get('video') if isinstance(props.get('video'), dict) else {}
        title = video.get('title') or props.get('title')
        if isinstance(title, str):
            candidate.metadata['title'] = title[:512]
        candidate.metadata['page_title'] = str((page.dom_snapshot or {}).get('page_title') or '')[:512]
        allowed_headers = ('content-type', 'content-range', 'accept-ranges', 'content-length', 'etag', 'last-modified')
        candidate.metadata['response_headers'] = {str(name).lower(): str(value) for name, value in observation.response_headers.items()
                                                   if str(name).lower() in allowed_headers}
        candidate.metadata['timing'] = {str(key): value for key, value in list(observation.timing.items())[:32]
                                       if type(value) in (int, float) and math.isfinite(value)}
        return candidate

    @staticmethod
    def api_url(page_url, video_id):
        parsed = urlsplit(page_url)
        return urlunsplit((parsed.scheme, parsed.netloc, '/api/hls/' + video_id, '', ''))

    @staticmethod
    def classify_api_url(url):
        """正片/缩略图由 API 参数语义决定，与是否重定向无关。"""
        try:
            if re.fullmatch(r'/api/hls/[^/]+', urlsplit(url).path):
                return 'THUMBNAIL_TRACK' if _thumbs(url) else 'MAIN_VIDEO_ENTRY'
        except (ValueError, TypeError):
            pass
        return ''

    @staticmethod
    def _same_api(url, api):
        try:
            value, expected = urlsplit(url), urlsplit(api)
            return (value.scheme.lower(), value.netloc.lower(), value.path) == (
                expected.scheme.lower(), expected.netloc.lower(), expected.path)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _location_url(location, original):
        if not isinstance(location, str) or not location or len(location) > 16384:
            raise MediaCheckError('API 没有返回有效 Location。', 'ROU_API_INVALID_LOCATION')
        # 对绝对地址直接返回原始值；不解析重建 signed query，也不猜 CDN 路径。
        target = location if urlsplit(location).scheme else urljoin(original, location)
        try:
            RequestContext(target)
        except (ValueError, TypeError):
            raise MediaCheckError('API Location 不是合法 HTTP 媒体地址。', 'ROU_API_INVALID_LOCATION') from None
        if urlsplit(original).scheme.lower() == 'https' and urlsplit(target).scheme.lower() != 'https':
            raise MediaCheckError('API Location 不允许降级为不安全 HTTP。', 'ROU_API_INVALID_LOCATION')
        return target

    @staticmethod
    def _expiry(url):
        for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
            if key.lower() != 'exp':
                continue
            try:
                expiry = float(value)
                if expiry > 10**12:
                    expiry /= 1000
                if math.isfinite(expiry) and 946684800 < expiry < 253402300800:
                    return expiry
            except (ValueError, OverflowError):
                pass
        return None

    def _validated_candidate(self, observation, decoded, page, video_id, api, location, api_status, chain):
        context = replace(deepcopy(observation.context), original_url=api,
                          redirect_chain=tuple(chain), playlist_decoder='rou',
                          url_resolution_policy=UrlResolutionPolicy.STANDARD_URLJOIN)
        info = parse_manifest(decoded.data, context.final_url, 'application/vnd.apple.mpegurl',
                              UrlResolutionPolicy.STANDARD_URLJOIN)
        segments = [item for item in info.resources if item.role == 'Media Segment']
        vod = info.hls_playlist_type == 'VOD' or '#EXT-X-ENDLIST' in decoded.text.splitlines()
        playback = info.hls_playlist_type or ('VOD' if vod else 'LIVE' if info.playlist_type == 'Media' else 'UNKNOWN')
        duration = sum(item.duration or 0 for item in segments) if segments else None
        observed_at = observation.timestamp
        if type(observed_at) not in (int, float) or not math.isfinite(observed_at) or observed_at <= 0:
            observed_at = time.time()
        trace = {'video_id': video_id, 'api': api, 'api_status': api_status, 'redirect': 'FOUND',
                 'location': location, 'index_url': context.final_url, 'index_http': observation.status,
                 **decoded.trace(), 'manifest_type': info.playlist_type.upper(), 'playlist_type': playback,
                 'segment_count': len(segments), 'duration': duration, 'encryption': info.encryption,
                 'drm': info.drm}
        manifest = info.to_dict()
        manifest['hls_playlist_type'] = playback
        candidate = MediaCandidate('rou-' + video_id, context, media_type='HLS',
                                   source_type='RouVideoResolver', duration=duration,
                                   content_type='application/vnd.apple.mpegurl', http_status=observation.status,
                                   manifest_type=info.playlist_type, playlist_type=playback,
                                   encryption=info.encryption, classification=info.classification, drm=info.drm,
                                   score=100, sources={'Resolver'}, frame_id=observation.frame_id,
                                   confirmed=True, context_complete=True, redirects=deepcopy(chain),
                                   transport_url=context.url, manifest_url=context.final_url,
                                   manifest_text=decoded.text,
                                   manifest_origin='embedded:png/roUd' if decoded.container == 'PNG' else 'captured:hls',
                                   validated=True)
        candidate.score_reasons = [{'points': 100, 'reason': 'API 重定向及 roUd/HLS 清单已验证'}]
        candidate.metadata.update(resolver_id=self.resolver_id, video_id=video_id, page_url=page.url,
                                  original_api_url=api, manifest_url=context.final_url, location=location,
                                  api_status=api_status, signed_url=True, possible_signed_url=True,
                                  discovered_at=observed_at, resolver_confidence=100, resolver_verified=True,
                                  resolver_route='api_redirect', resolver_trace=trace, manifest=manifest,
                                  segment_count=len(segments), body_complete=True,
                                  url_resolution_policy=str(UrlResolutionPolicy.STANDARD_URLJOIN))
        expiry = self._expiry(context.final_url)
        if expiry is not None:
            candidate.metadata.update(exp=expiry, expires_hint='媒体地址包含 exp 到期时间，失效时重新请求原 API。')
        snapshot = page.dom_snapshot if isinstance(page.dom_snapshot, dict) else {}
        title = snapshot.get('page_title') or snapshot.get('title') or ''
        candidate.metadata.update(title=str(title)[:512], page_title=str(title)[:512])
        allowed_headers = ('content-type', 'content-range', 'accept-ranges', 'content-length', 'etag', 'last-modified')
        candidate.metadata['response_headers'] = {str(name).lower(): str(value)
            for name, value in observation.response_headers.items() if str(name).lower() in allowed_headers}
        return candidate

    def resolve(self, page):
        if not self.can_handle(page):
            return ResolveResult(diagnostics=[self._diagnostic('ROU_INVALID_PAGE_URL', '当前 URL 不属于 RouVideoResolver。')],
                                 resolver_id=self.resolver_id)
        video_id = self.extract_video_id(page.url)
        if not video_id:
            return ResolveResult(diagnostics=[self._diagnostic('ROU_VIDEO_ID_NOT_FOUND', '请进入 /v/{非空ID} 播放页。')],
                                 resolver_id=self.resolver_id)
        api = self.api_url(page.url, video_id)
        snapshot = page.dom_snapshot if isinstance(page.dom_snapshot, dict) else {}
        attempted = snapshot.get('resolver_attempted_urls', ())
        attempted = set(url for url in attempted[:48] if isinstance(url, str)) if isinstance(attempted, (list, tuple)) else set()
        inflight = snapshot.get('resolver_inflight_url', '')
        observations = list(page.observations)[-_MAX_OBSERVATIONS:]
        diagnostics = []
        trace = {'video_id': video_id, 'api': api, 'api_status': None, 'redirect': 'PENDING',
                 'index_url': '', 'index_http': None, 'container': '', 'roud': 'PENDING', 'playlist': 'PENDING'}

        def result(requests=(), failure='', message=''):
            if failure:
                diagnostics.append(self._diagnostic(failure, message, video_id))
                trace['error_code'] = failure
            diagnostics.append({'resolver_id': self.resolver_id, 'error_code': '',
                                'code': 'ROU_RESOLVER_TRACE', 'trace': deepcopy(trace)})
            return ResolveResult(diagnostics=diagnostics, requests=list(requests), resolver_id=self.resolver_id)

        def fallback(failure, message):
            failed = result(failure=failure, message=message)
            # 只有确定的 API 主流程失败后才读取旧 NEXT/JSON 数据；不探测相邻 CDN 文件。
            if self.legacy_fallback and (page.next_data or any(
                    self._same_api(obs.context.original_url, api) and obs.content_type.startswith('application/json')
                    for obs in observations)):
                legacy = self._resolve_legacy(page)
                legacy.diagnostics = failed.diagnostics + legacy.diagnostics
                for candidate in legacy.candidates:
                    candidate.metadata['resolver_route'] = 'legacy'
                legacy.requests = [request for request in legacy.requests if request.url != api]
                return legacy
            return failed

        def request(url, role, observation=None):
            headers = dict(observation.context.headers) if observation is not None and observation.context.url == url else {}
            return ResolverRequest(url, 'rou' if role == 'rou-index' else '',
                                   UrlResolutionPolicy.STANDARD_URLJOIN,
                                   {'resolver_id': self.resolver_id, 'video_id': video_id, 'page_url': page.url,
                                    'role': role}, headers)

        current_api = []
        for observation in observations:
            aliases = self._observation_urls(observation)
            chain = list(observation.redirects or observation.context.redirect_chain)
            relevant = any(self._same_api(url, api) for url in aliases) or any(
                self._same_api(hop.get('from', ''), api) for hop in chain if isinstance(hop, dict))
            if not relevant:
                continue
            if any(self._same_api(url, api) and _thumbs(url) for url in aliases) or any(
                    self._same_api(hop.get('from', ''), api) and _thumbs(hop.get('from', '')) for hop in chain):
                diagnostic = self._diagnostic('THUMBNAIL_TRACK', 'kind=thumbs 是缩略图轨道，已排除。', video_id)
                diagnostic['classification'] = 'THUMBNAIL_TRACK'
                diagnostic['request_id'] = observation.request_id
                diagnostic['excluded_urls'] = sorted(url for url in aliases | {
                    hop.get('to', '') for hop in chain if isinstance(hop, dict)} if url)
                diagnostics.append(diagnostic)
                continue
            current_api.append(observation)

        if not current_api:
            if inflight:
                return result()
            if api not in attempted:
                return result([request(api, 'rou-api')])
            return fallback('ROU_API_REQUEST_FAILED', '当前浏览器未取得 API 的有效响应；请检查当前会话和网络。')

        api_observation = current_api[-1]
        chain = list(api_observation.redirects or api_observation.context.redirect_chain)
        api_hop = next((hop for hop in chain if isinstance(hop, dict) and self._same_api(hop.get('from', ''), api)
                        and not _thumbs(hop.get('from', ''))), None)
        if api_hop is None and self._same_api(api_observation.context.url, api):
            if api_observation.status in (301, 302, 307, 308):
                api_hop = {'from': api_observation.context.url, 'status': api_observation.status,
                           'location': next((value for key, value in api_observation.response_headers.items()
                                            if str(key).lower() == 'location'), '')}
            elif api_observation.status >= 400:
                trace['api_status'] = api_observation.status
                diagnostics.append(self._diagnostic('AUTH_REQUIRED' if api_observation.status == 401 else
                    'HTTP_403' if api_observation.status == 403 else 'HTTP_ERROR', 'API 请求未成功；HTTP 错误不表示 DRM。',
                    video_id, api_observation))
                return fallback('ROU_API_REQUEST_FAILED', 'API 拒绝当前浏览器会话；请正常登录或重新播放后查找。')
            elif api_observation.status:
                trace['api_status'] = api_observation.status
                return fallback('ROU_API_REDIRECT_MISSING', 'API 未返回合法正片重定向，未构造任何 CDN 地址。')
            else:
                return result()
        if not api_hop:
            return fallback('ROU_API_REDIRECT_MISSING', '未捕获 API 的原始重定向，请重新查找。')
        api_status = api_hop.get('status')
        location = api_hop.get('location', '')
        trace.update(api_status=api_status, location=location)
        if api_status not in (301, 302, 307, 308) or not location:
            return fallback('ROU_API_REDIRECT_MISSING', 'API 未返回有效重定向和 Location。')
        try:
            entry = self._location_url(location, api_hop.get('from') or api)
        except (ValueError, TypeError) as error:
            return fallback('ROU_API_INVALID_LOCATION', str(error))
        trace.update(redirect='FOUND', index_url=entry)
        if not chain:
            chain = [{**api_hop, 'to': entry}]
        elif chain[0].get('to') and api_hop.get('to') != entry:
            return fallback('ROU_API_INVALID_LOCATION', 'Location 与实际浏览器跳转目标不一致。')
        targets = {entry}
        targets.update(hop.get('to', '') for hop in chain)
        indexes = [obs for obs in observations if obs.context.url in targets and not self._same_api(obs.context.url, api)]
        if not indexes:
            if inflight:
                return result()
            if entry not in attempted:
                return result([request(entry, 'rou-index')])
            return fallback('ROU_INDEX_REQUEST_FAILED', '当前浏览器未取得 Location 对应清单。')
        index = indexes[-1]
        if index.from_cache and index.body_complete and not index.context_complete:
            # 缓存重放可能没有 wire headers；仅复用同 URL、同字节且已有实际
            # 完整上下文的观测，不能让一次不完整缓存事件掩盖已验证响应。
            index = next((previous for previous in reversed(indexes[:-1])
                          if previous.context.final_url == index.context.final_url
                          and 200 <= previous.status < 300 and previous.context_complete
                          and previous.body_complete and previous.body == index.body), index)
        trace.update(index_url=index.context.final_url, index_http=index.status)
        expiry = self._expiry(index.context.final_url)
        if index.status in (401, 403) and expiry is not None and expiry <= time.time():
            return fallback('ROU_SIGNED_URL_EXPIRED', '服务器下发的 exp 地址已过期，请通过原 API 刷新。')
        if index.status >= 400:
            diagnostics.append(self._diagnostic('AUTH_REQUIRED' if index.status == 401 else
                'HTTP_403' if index.status == 403 else 'HTTP_ERROR', 'Index 请求失败；未判定为 DRM。', video_id, index))
            return fallback('ROU_INDEX_REQUEST_FAILED', 'Location 对应清单请求失败，请更新当前页面会话。')
        if not 200 <= index.status < 300 or not index.context_complete:
            return result()
        if not index.body_complete or index.body is None:
            if not inflight and entry not in attempted:
                return result([request(entry, 'rou-index', index)])
            if not inflight and entry in attempted:
                return fallback('ROU_INDEX_REQUEST_FAILED', '已取得清单响应头，但浏览器未能读取完整正文；请检查当前会话或重新查找。')
            return result()
        try:
            decoded = RouPlaylistDecoder.decode_with_trace(index.body)
            candidate = self._validated_candidate(index, decoded, page, video_id, api, location, api_status, chain)
        except MediaCheckError as error:
            return fallback(error.code, str(error))
        diagnostics.append({'resolver_id': self.resolver_id, 'error_code': '', 'code': 'ROU_RESOLVER_TRACE',
                            'trace': deepcopy(candidate.metadata['resolver_trace'])})
        if candidate.drm is True:
            diagnostics.append(self._diagnostic('PROTECTED_MEDIA', '清单有明确 DRM 证据，禁止下载和保护绕过。', video_id))
        return ResolveResult(True, [candidate], diagnostics, resolver_id=self.resolver_id)

    def _resolve_legacy(self, page):
        if not self.can_handle(page):
            return ResolveResult(diagnostics=[self._diagnostic('UNSUPPORTED_SITE', '当前页面不属于此站点解析器。')],
                                 resolver_id=self.resolver_id)
        video_id = self.extract_video_id(page.url)
        if not video_id:
            return ResolveResult(diagnostics=[self._diagnostic('NOT_VIDEO_PAGE', '当前页面不是播放页，请进入 /v/ 视频页面。')],
                                 resolver_id=self.resolver_id)
        diagnostics, entries = [], []
        try:
            props = self._page_props(page.next_data)
            if props.get('ev') is not None:
                try:
                    decoded = self.decode_ev(props['ev'])
                    entry = _url(decoded.get('videoUrl'), page.url)
                    if entry and not _thumbs(entry):
                        entries.append(entry)
                except MediaCheckError as error:
                    diagnostics.append(self._diagnostic(error.code, str(error), video_id))
            entries.extend(_urls_from_values(props.get('video', {}), page.url))
        except MediaCheckError as error:
            diagnostics.append(self._diagnostic(error.code, str(error), video_id))
        parsed_page = urlsplit(page.url)
        api = urlunsplit((parsed_page.scheme, parsed_page.netloc, '/api/hls/' + video_id, '', ''))
        observations = list(page.observations)[-_MAX_OBSERVATIONS:]
        current_api = []
        for observation in observations:
            aliases = self._observation_urls(observation)
            if any(urlsplit(url).path == '/api/hls/' + video_id and urlsplit(url).netloc.lower() == parsed_page.netloc.lower()
                   for url in aliases):
                current_api.append(observation)
                if (200 <= observation.status < 300 and observation.body and len(observation.body) <= DEFAULT_MAX_SIZE
                        and not any(_thumbs(url) for url in aliases)):
                    try:
                        response = json.loads(observation.body)
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        continue
                    entries.extend(_urls_from_values(response, observation.context.final_url))
        entries = list(dict.fromkeys(entry for entry in entries if entry and not _thumbs(entry)))[:_MAX_ENTRIES]
        plans = list(dict.fromkeys(url for entry in entries for url in self._siblings(entry)))
        if api not in plans:
            plans.append(api)
        authorized = set(plans)
        snapshot = page.dom_snapshot if isinstance(page.dom_snapshot, dict) else {}
        provided_attempts = snapshot.get('resolver_attempted_urls', ())
        attempted = {url for url in provided_attempts[:48] if isinstance(url, str) and url in authorized} \
            if isinstance(provided_attempts, (list, tuple)) else set()
        ranked_observations = sorted(enumerate(observations), key=lambda pair: (
            next((index for index, entry in enumerate(entries) if entry in self._observation_urls(pair[1])), len(entries)),
            -pair[0]))
        for _, observation in ranked_observations:
            aliases = self._observation_urls(observation)
            # 普通 Image 观测可能只有请求头；尚未由浏览器 Fetch 正文时不能永久跳过入口。
            if observation.body is not None or observation.status >= 400:
                attempted.update(aliases)
            if any(_thumbs(url) for url in aliases):
                if authorized & aliases or observation in current_api:
                    diagnostics.append(self._diagnostic('THUMBNAIL', '缩略图入口不作为正片候选。', video_id, observation))
                continue
            is_other_api = any(re.match(r'^/api/hls/[^/]+$', urlsplit(url).path)
                               and urlsplit(url).path != '/api/hls/' + video_id for url in aliases)
            if is_other_api:
                continue
            entry_observation = bool(authorized & aliases or observation in current_api)
            if not entry_observation:
                # Unrelated iframe/ad media belongs to generic discovery; it
                # cannot establish this page's video identity for safe resume.
                continue
            if not 200 <= observation.status < 300:
                code = 'HTTP_403' if observation.status == 403 else 'AUTH_REQUIRED' if observation.status == 401 else 'HTTP_ERROR'
                diagnostics.append(self._diagnostic(code, '媒体入口未返回成功响应，继续通用媒体发现或更新页面。', video_id, observation))
                continue
            if not observation.context_complete:
                diagnostics.append(self._diagnostic('REQUEST_CONTEXT_INCOMPLETE', '尚未取得完整浏览器请求上下文。', video_id, observation))
                continue
            if not observation.body or not observation.body_complete:
                continue
            try:
                decoded = RouPlaylistDecoder.decode(observation.body)
                info = parse_manifest(decoded, observation.context.final_url, observation.content_type,
                                      UrlResolutionPolicy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING)
                if info.media_type != 'HLS' or not (info.resources or info.children):
                    continue
                candidate = self._candidate(observation, decoded, page, video_id)
            except MediaCheckError as error:
                diagnostics.append(self._diagnostic(error.code, str(error), video_id, observation))
                continue
            if candidate is not None:
                if candidate.drm is True:
                    diagnostics.append(self._diagnostic('DRM_PROTECTED', '检测到真正受保护媒体，仅提供检测详情。', video_id, observation))
                return ResolveResult(True, [candidate], diagnostics, resolver_id=self.resolver_id)
        if snapshot.get('resolver_inflight_url'):
            diagnostics.append(self._diagnostic('RESOLVER_REQUEST_INFLIGHT', '当前浏览器请求仍在进行，等待响应正文。', video_id))
            return ResolveResult(diagnostics=diagnostics, resolver_id=self.resolver_id)
        # 一次只规划当前页面的首个未尝试入口，由合法浏览器执行并再次解析。
        request_url = next((url for url in plans if url not in attempted), '')
        if request_url:
            metadata = {'resolver_id': self.resolver_id, 'video_id': video_id, 'page_url': page.url}
            request = ResolverRequest(request_url, 'rou', UrlResolutionPolicy.ROU_INHERIT_BASE_QUERY_WHEN_MISSING, metadata)
            diagnostics.append(self._diagnostic('RESOLVER_REQUEST_PENDING', '等待当前合法浏览器请求媒体入口。', video_id))
            return ResolveResult(diagnostics=diagnostics, requests=[request], resolver_id=self.resolver_id)
        diagnostics.append(self._diagnostic('ROU_RESOLVE_FAILED', '专用解析未取得有效媒体，继续使用通用媒体发现。', video_id))
        return ResolveResult(diagnostics=diagnostics, resolver_id=self.resolver_id)
