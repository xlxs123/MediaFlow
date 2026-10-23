"""Resolver 的纯数据合同；浏览器执行请求，解析器不实现下载。"""
from dataclasses import dataclass, field

from core.discovery_models import MediaCandidate, NetworkObservation
from core.url_policy import UrlResolutionPolicy


@dataclass(frozen=True)
class PageContext:
    url: str
    next_data: dict | str | None = field(default=None, repr=False)
    observations: tuple[NetworkObservation, ...] = field(default=(), repr=False)
    dom_snapshot: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ResolverRequest:
    url: str
    playlist_decoder: str = ''
    url_resolution_policy: str = UrlResolutionPolicy.STANDARD_URLJOIN
    metadata: dict = field(default_factory=dict, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass
class ResolveResult:
    success: bool = False
    candidates: list[MediaCandidate] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    requests: list[ResolverRequest] = field(default_factory=list)
    resolver_id: str = ''
