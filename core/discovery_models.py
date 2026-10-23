"""浏览器采集与媒体候选的内部数据；请求凭据只留在内存。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from copy import deepcopy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.dash_models import DashTrack

from core.request_context import RequestContext
from utils.security import Redactor, redact_data


@dataclass
class NetworkObservation:
    request_id: str
    context: RequestContext = field(repr=False)
    status: int = 0
    content_type: str = ''
    body: bytes | None = field(default=None, repr=False)
    frame_id: str = ''
    resource_type: str = ''
    initiator: str = field(default='', repr=False)
    timestamp: float = 0.0
    redirects: list[dict] = field(default_factory=list, repr=False)
    content_length: int | None = None
    from_cache: bool = False
    body_complete: bool = False
    context_complete: bool = True
    response_headers: dict[str, str] = field(default_factory=dict, repr=False)
    timing: dict = field(default_factory=dict)


@dataclass
class MediaCandidate:
    id: str
    request_context: RequestContext = field(repr=False)
    media_type: str = ''
    source_type: str = 'Network'
    resolution: tuple[int, int] = (0, 0)
    bandwidth: int = 0
    duration: float | None = None
    content_type: str = ''
    http_status: int = 0
    manifest_type: str = ''
    playlist_type: str = ''
    encryption: str = 'Unknown'
    classification: str = ''
    drm: bool | None = None
    score: int = 0
    score_reasons: list[dict] = field(default_factory=list)
    redirects: list[dict] = field(default_factory=list, repr=False)
    metadata: dict = field(default_factory=dict, repr=False)
    sources: set[str] = field(default_factory=set)
    frame_id: str = ''
    is_ad: bool = False
    context_complete: bool = True
    confirmed: bool = False
    # 传输容器与解码清单分开保存；正文和签名资源仅在内存中流转。
    transport_url: str = field(default='', repr=False)
    manifest_url: str = field(default='', repr=False)
    manifest_text: str = field(default='', repr=False)
    manifest_origin: str = ''
    validated: bool = False
    confidenceScore: int | None = None
    confidenceReasons: list[str] = field(default_factory=list)
    # 下载前的可选详情；旧 resolution/bandwidth/duration 调用继续兼容。
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    videoCodec: str | None = None
    audioCodec: str | None = None
    videoBitrate: int | None = None
    audioBitrate: int | None = None
    estimatedSize: int | None = None
    container: str | None = None
    qualityScore: int = 0
    # 描述中的完整 Representation 配对；不是两条孤立的 Segment。
    video_track: DashTrack | None = field(default=None, repr=False)
    audio_track: DashTrack | None = field(default=None, repr=False)

    def __post_init__(self):
        # 兼容旧调用方显式传入 score；发现服务会用实际证据重新计算真实性。
        value = self.score if self.confidenceScore is None else self.confidenceScore
        try:
            self.confidenceScore = max(0, min(100, int(value)))
        except (ValueError, TypeError, OverflowError):
            self.confidenceScore = 0

    def download_payload(self):
        return {'download_protocol': self.media_type,
                'transport_url': self.transport_url or self.url,
                'manifest_url': self.manifest_url or self.final_url,
                'manifest_text': self.manifest_text,
                'manifest_origin': self.manifest_origin}

    @property
    def url(self):
        return self.request_context.url

    @property
    def final_url(self):
        return self.request_context.final_url

    @property
    def video_url(self):
        return self.video_track.url if self.video_track else ''

    @property
    def audio_url(self):
        return self.audio_track.url if self.audio_track else ''

    @property
    def resolution_label(self):
        width = self.width or self.resolution[0]
        height = self.height or self.resolution[1]
        edge = min(width, height) if width and height else height
        return f'{edge}p' if edge else '--'

    @property
    def original_url(self):
        return self.request_context.original_url

    @property
    def source(self):
        return self.source_type

    @property
    def confidence(self):
        return self.confidenceScore

    @property
    def downloadable(self):
        return not self.unavailable_reasons

    @property
    def discovery_state(self):
        return 'DISCOVERED'

    @property
    def unavailable_reasons(self):
        # 与队列共用同一资格检查，避免卡片、详情和实际导入给出矛盾状态。
        from core.media_groups import candidate_unavailable_reasons
        return candidate_unavailable_reasons(self)

    @property
    def requires_confirmation(self):
        return self.confidence < 80 or not self.validated and self.drm is None

    @property
    def validation_state(self):
        if self.unavailable_reasons:
            return 'UNAVAILABLE'
        return 'NEEDS_CONFIRMATION' if self.requires_confirmation else 'VERIFIED'

    @property
    def download_state(self):
        return 'AVAILABLE' if self.downloadable else 'UNAVAILABLE'

    def safe_diagnostic(self):
        values = {**self.request_context.summary(), 'media_type': self.media_type,
                  'source_type': self.source_type, 'resolution': self.resolution_label,
                  'dimensions': f'{self.resolution[0]}x{self.resolution[1]}' if all(self.resolution) else '--',
                  'bandwidth': self.bandwidth, 'duration': self.duration, 'content_type': self.content_type,
                  'http_status': self.http_status, 'manifest_type': self.manifest_type,
                  'playlist_type': self.playlist_type, 'encryption': self.encryption,
                  'classification': self.classification, 'drm': self.drm, 'score': self.score,
                  'confidenceScore': self.confidenceScore, 'confidenceReasons': self.confidenceReasons,
                  'width': self.width, 'height': self.height, 'fps': self.fps,
                  'videoCodec': self.videoCodec, 'audioCodec': self.audioCodec,
                  'videoBitrate': self.videoBitrate, 'audioBitrate': self.audioBitrate,
                  'estimatedSize': self.estimatedSize, 'container': self.container,
                  'detail_sources': self.metadata.get('detail_sources', {}),
                  'size_is_estimate': self.metadata.get('size_is_estimate'),
                  'qualityScore': self.qualityScore,
                  'quality_details': self.metadata.get('quality_details', []),
                  'combined_score': self.metadata.get('combined_score'),
                  'quality_recommendation': self.metadata.get('quality_recommendation', ''),
                  'confidenceDetails': self.metadata.get('confidence_details', []),
                  'score_reasons': self.score_reasons, 'redirects': self.redirects,
                  'sources': sorted(self.sources), 'is_ad': self.is_ad,
                  'context_complete': self.context_complete, 'confirmed': self.confirmed,
                  'discovery_state': self.discovery_state, 'validation_state': self.validation_state,
                  'download_state': self.download_state, 'unavailable_reasons': self.unavailable_reasons,
                  'transport_url': self.transport_url or self.url,
                  'manifest_url': self.manifest_url or self.final_url,
                  'manifest_origin': self.manifest_origin, 'validated': self.validated,
                  'has_manifest_text': bool(self.manifest_text),
                  'video_track': asdict(self.video_track) if self.video_track else None,
                  'audio_track': asdict(self.audio_track) if self.audio_track else None,
                  'manifest': self.metadata.get('manifest', {}),
                  'codecs': self.metadata.get('codecs', ''), 'frame_rate': self.metadata.get('frame_rate', ''),
                  'player_type': self.metadata.get('player_type', ''),
                  'error_code': self.metadata.get('error_code', ''),
                  'probe_error': self.metadata.get('probe_error', '')}
        for key in ('resolver_id', 'video_id', 'page_url', 'signed_url', 'discovered_at',
                    'expires_hint', 'url_resolution_policy', 'response_headers', 'timing',
                    'resolver_confidence', 'resolver_verified', 'resolver_route', 'resolver_trace',
                    'original_api_url', 'manifest_url', 'api_status', 'location', 'exp',
                    'possible_signed_url', 'segment_count', 'title', 'page_title'):
            if key in self.metadata:
                values[key] = self.metadata[key]
        redactor = Redactor(self.request_context.headers, [self.url, self.final_url])
        return redact_data(deepcopy(values), redactor)
