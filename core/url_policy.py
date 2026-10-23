"""清单相对 URI 的解析策略；普通媒体始终使用标准 urljoin。"""
from enum import StrEnum
from urllib.parse import urljoin, urlsplit, urlunsplit


class UrlResolutionPolicy(StrEnum):
    STANDARD_URLJOIN = 'STANDARD_URLJOIN'
    ROU_INHERIT_BASE_QUERY_WHEN_MISSING = 'ROU_INHERIT_BASE_QUERY_WHEN_MISSING'


def resolve_media_url(base_url: str, uri: str,
                      policy: str = UrlResolutionPolicy.STANDARD_URLJOIN) -> str:
    policy = UrlResolutionPolicy(policy)
    target = urljoin(base_url, uri)
    if policy == UrlResolutionPolicy.STANDARD_URLJOIN:
        return target
    base, reference, result = urlsplit(base_url), urlsplit(uri), urlsplit(target)
    # Only relative resources on the same origin may inherit a source's raw query.
    # An explicit '?' also expresses a resource-specific (possibly empty) query.
    if (not reference.scheme and not reference.netloc and '?' not in uri
            and base.query and not result.query
            and (base.scheme.lower(), base.netloc.lower()) == (result.scheme.lower(), result.netloc.lower())):
        return urlunsplit(result._replace(query=base.query))
    return target
