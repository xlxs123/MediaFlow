"""Classify media protection from manifest content, without fetching keys.

Ordinary HLS AES-128 identity keys are not a DRM signal. This module only
parses supplied bytes and reports evidence; enforcement belongs to media_guard.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass, field
import math
import re
import struct
from urllib.parse import unquote_to_bytes, urljoin, urlsplit
import uuid
import xml.etree.ElementTree as ET

from core.errors import MediaCheckError


@dataclass
class KeyInfo:
    method: str
    uri: str = ""
    resolved_uri: str = ""
    iv: str = ""
    keyformat: str = "identity"
    keyformatversions: str = ""
    classification: str = "CLEAR_HLS"
    drm: bool | None = False
    evidence: list[str] = field(default_factory=list)


@dataclass
class MediaResource:
    uri: str
    resolved_uri: str
    role: str = "Media Segment"
    duration: float | None = None
    byte_range: str = ""


@dataclass
class ManifestInfo:
    media_type: str
    playlist_type: str = ""
    encryption: str = "None"
    classification: str = ""
    drm: bool | None = False
    keys: list[KeyInfo] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    hls_playlist_type: str = ""
    resources: list[MediaResource] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["protocol"] = self.media_type
        result["manifest_type"] = self.playlist_type
        if self.hls_playlist_type:
            result["playlist_type"] = self.hls_playlist_type
        return result


_SYSTEMS = {
    "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "DRM_WIDEVINE",
    "9a04f079-9840-4286-ab92-e65be0885f95": "DRM_PLAYREADY",
    "94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "DRM_FAIRPLAY",
}
_FORMATS = {
    "com.widevine": "DRM_WIDEVINE", "com.widevine.alpha": "DRM_WIDEVINE",
    "widevine": "DRM_WIDEVINE", "com.microsoft.playready": "DRM_PLAYREADY",
    "com.microsoft.playready.recommendation": "DRM_PLAYREADY",
    "com.microsoft.playready.hardware": "DRM_PLAYREADY", "playready": "DRM_PLAYREADY",
    "com.apple.streamingkeydelivery": "DRM_FAIRPLAY", "fairplay": "DRM_FAIRPLAY",
}
_HLS_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/mpegurl",
    "audio/mpegurl", "audio/x-mpegurl",
}
_ATTRIBUTE_TAGS = {
    "#EXT-X-KEY", "#EXT-X-SESSION-KEY", "#EXT-X-STREAM-INF",
    "#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-IMAGE-STREAM-INF", "#EXT-X-MEDIA",
    "#EXT-X-MAP", "#EXT-X-PART", "#EXT-X-PRELOAD-HINT", "#EXT-X-RENDITION-REPORT",
    "#EXT-X-SESSION-DATA", "#EXT-X-DATERANGE", "#EXT-X-CONTENT-STEERING",
}
_MASTER_URI_TAGS = {
    "#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-IMAGE-STREAM-INF", "#EXT-X-MEDIA",
}
_KEY_ATTRIBUTE_NAMES = {"METHOD", "URI", "IV", "KEYFORMAT", "KEYFORMATVERSIONS"}


def _parse_error(message: str) -> MediaCheckError:
    return MediaCheckError(message, "MANIFEST_PARSE_ERROR")


def parse_attribute_list(text: str) -> dict[str, str]:
    """Parse HLS attribute lists, including commas inside quoted URIs."""
    if not isinstance(text, str):
        raise _parse_error("HLS 属性列表必须是文本。")
    attributes: dict[str, str] = {}
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            break
        start = index
        while index < len(text) and text[index] not in "=,":
            index += 1
        name = text[start:index].strip().upper()
        if not re.fullmatch(r"[A-Z0-9-]+", name) or index == len(text) or text[index] != "=":
            raise _parse_error("HLS 属性格式不完整，应为 NAME=VALUE。")
        if name in attributes:
            raise _parse_error("HLS 属性重复，无法确定实际值。")
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            raise _parse_error("HLS 属性缺少值。")
        parts: list[str] = []
        if text[index] == '"':
            index += 1
            closed = False
            while index < len(text):
                char = text[index]
                if char == '"':
                    index += 1
                    closed = True
                    break
                if char in "\r\n" or ord(char) < 32:
                    raise _parse_error("HLS 引号中的属性值不能包含换行或控制字符。")
                if char == "\\" and index + 1 < len(text) and text[index + 1] in '\\"':
                    parts.append(text[index + 1])
                    index += 2
                else:
                    parts.append(char)
                    index += 1
            if not closed:
                raise _parse_error("HLS 属性引号不完整。")
            while index < len(text) and text[index].isspace():
                index += 1
            if index < len(text) and text[index] != ",":
                raise _parse_error("HLS 引号属性后缺少逗号。")
            value = "".join(parts)
        else:
            start = index
            while index < len(text) and text[index] != ",":
                index += 1
            value = text[start:index].strip()
            if not value or '"' in value or any(ord(char) < 32 and not char.isspace() for char in value):
                raise _parse_error("HLS 属性值不完整或包含无效字符。")
        attributes[name] = value
        if index < len(text):
            index += 1
            if not text[index:].strip():
                raise _parse_error("HLS 属性列表以逗号结束，请提供完整清单。")
    return attributes


def _ends_with_attribute_comma(line: str) -> bool:
    if not line.endswith(","):
        return False
    quoted = False
    index = 0
    while index < len(line):
        if line[index] == "\\" and quoted and index + 1 < len(line):
            index += 2
            continue
        if line[index] == '"':
            quoted = not quoted
        index += 1
    return not quoted


def hls_lines(text: str) -> list[str]:
    """Normalize physical lines, including the supplied split Key forms.

    An explicitly empty EXT-X-KEY: starts a block of known key attributes.
    Other attribute tags continue only after a comma. Tags and resource URI
    lines end that block; arbitrary NAME=filename lines are not consumed.
    """
    if not isinstance(text, str):
        raise _parse_error("HLS 清单必须是文本。")
    physical = [line.strip() for line in text.removeprefix("\ufeff").splitlines() if line.strip()]
    result: list[str] = []
    split_key_block = False
    for line in physical:
        if result:
            tag = result[-1].split(":", 1)[0]
            if split_key_block and tag in {"#EXT-X-KEY", "#EXT-X-SESSION-KEY"} and _is_key_attribute_line(line):
                separator = "" if result[-1].endswith(":") or _ends_with_attribute_comma(result[-1]) else ","
                result[-1] += separator + line
                continue
            if not split_key_block and tag in _ATTRIBUTE_TAGS and _ends_with_attribute_comma(result[-1]) and re.match(r"^[A-Za-z0-9-]+\s*=", line):
                result[-1] += line
                continue
        result.append(line)
        split_key_block = line in {"#EXT-X-KEY:", "#EXT-X-SESSION-KEY:"}
    return result


def _is_key_attribute_line(line: str) -> bool:
    """Recognize only actual fields in an explicitly split key block."""
    if not re.match(r"^(?:METHOD|URI|IV|KEYFORMAT|KEYFORMATVERSIONS)\s*=", line, re.IGNORECASE):
        return False
    candidate = line[:-1] if _ends_with_attribute_comma(line) else line
    try:
        attributes = parse_attribute_list(candidate)
    except MediaCheckError:
        return False
    if not attributes or not set(attributes) <= _KEY_ATTRIBUTE_NAMES:
        return False
    # Quoted URI/format fields and a hexadecimal IV disambiguate these fields
    # from legitimate bare resource names such as URI=clip.png or IV=clip.ts.
    for name in ("URI", "KEYFORMAT", "KEYFORMATVERSIONS"):
        if name in attributes and not re.search(r"(?:^|,)\s*" + name + r"\s*=\s*\"", candidate, re.IGNORECASE):
            return False
    if "IV" in attributes and not re.fullmatch(r"0[xX][0-9A-Fa-f]+", attributes["IV"]):
        return False
    if "METHOD" in attributes and not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", attributes["METHOD"]):
        return False
    return True


def _known_classification(identifier: str) -> str | None:
    value = identifier.strip().lower()
    if value in _FORMATS:
        return _FORMATS[value]
    candidate = value.removeprefix("urn:uuid:").strip("{}")
    try:
        canonical = str(uuid.UUID(candidate))
    except (ValueError, AttributeError):
        return None
    return _SYSTEMS.get(canonical)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _resolve_uri(base_url: str, uri: str) -> str:
    try:
        # Validate bracketed hosts while preserving every query byte. Network
        # protocol/scheme policy still belongs to RequestContext/transport.
        _ = urlsplit(uri)
        return urljoin(base_url, uri)
    except (ValueError, TypeError):
        raise _parse_error("清单中的 URI 或基准 URL 无效，无法解析相对地址。") from None


def _prefer_classification(values: list[str], default: str = "DRM_UNKNOWN") -> str:
    return next((value for value in values if value in _SYSTEMS.values()), default)


def _pssh_metadata(data: bytes) -> tuple[list[str], list[str]]:
    """Read only structural PSSH SystemIDs, never license data or keys."""
    classifications: list[str] = []
    evidence: list[str] = []
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                break
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            break
        if kind == b"pssh":
            evidence.append("包含结构化 PSSH 保护头。")
            if size >= header + 24 and data[offset + header] in (0, 1):
                system_id = str(uuid.UUID(bytes=data[offset + header + 4:offset + header + 20]))
                classification = _SYSTEMS.get(system_id, "DRM_UNKNOWN")
                classifications.append(classification)
                evidence.append("PSSH SystemID=" + system_id)
            else:
                classifications.append("DRM_UNKNOWN")
        offset += size
    return classifications, evidence


def _inline_pssh(uri: str) -> tuple[list[str], list[str]]:
    if not uri.lower().startswith("data:") or "," not in uri or len(uri) > 131_072:
        return [], []
    metadata, payload = uri.split(",", 1)
    try:
        if metadata.lower().endswith(";base64"):
            data = base64.b64decode(re.sub(r"\s", "", payload), validate=True)
        else:
            data = unquote_to_bytes(payload)
    except (ValueError, binascii.Error):
        return [], []
    return _pssh_metadata(data)


def _parse_hls_key(attributes: dict[str, str], base_url: str) -> KeyInfo:
    method = attributes.get("METHOD", "").upper()
    if not method:
        raise _parse_error("HLS Key 缺少 METHOD，无法判断加密方式。")
    uri = attributes.get("URI", "")
    keyformat = attributes.get("KEYFORMAT", "identity")
    versions = attributes.get("KEYFORMATVERSIONS", "")
    iv = attributes.get("IV", "")
    if not keyformat or (versions and not re.fullmatch(r"[1-9][0-9]*(?:/[1-9][0-9]*)*", versions)):
        raise _parse_error("HLS KEYFORMAT 或 KEYFORMATVERSIONS 格式不正确。")
    if iv and not re.fullmatch(r"0[xX][0-9A-Fa-f]{1,32}", iv):
        raise _parse_error("HLS IV 必须是最多 128 位的 0x 十六进制数。")
    if method == "NONE":
        if any(name in attributes for name in ("URI", "IV", "KEYFORMAT", "KEYFORMATVERSIONS")):
            raise _parse_error("HLS METHOD=NONE 不能同时携带密钥或加密属性。")
        return KeyInfo(method="NONE", evidence=["METHOD=NONE，后续分片为明文。"])
    if not uri:
        raise _parse_error("HLS 加密 Key 缺少 URI。")
    if any(ord(char) < 32 or ord(char) == 127 for char in uri):
        raise _parse_error("HLS Key URI 包含无效控制字符。")
    key = KeyInfo(method, uri, _resolve_uri(base_url, uri), iv, keyformat, versions)
    key.evidence.append("HLS METHOD=" + method + "; KEYFORMAT=" + keyformat)
    known = _known_classification(keyformat)
    if urlsplit(uri).scheme.lower() == "skd":
        known = "DRM_FAIRPLAY"
        key.evidence.append("Key URI 使用 FairPlay skd 协议。")
    pssh_classes, pssh_evidence = _inline_pssh(uri)
    if known or pssh_classes or keyformat.lower() != "identity":
        key.drm = True
        key.classification = _prefer_classification(([known] if known else []) + pssh_classes)
        key.evidence.extend(pssh_evidence)
        if known:
            key.evidence.append("识别到 " + known + " 密钥格式或协议。")
        elif keyformat.lower() != "identity":
            key.evidence.append("显式非 identity KEYFORMAT，属于受保护密钥交付格式。")
    elif method == "AES-128":
        key.classification = "HLS_AES128"
        key.drm = False
        key.evidence.append("AES-128 identity 是普通 HLS 分片加密，不构成 DRM 证据。")
    elif method in {"SAMPLE-AES", "SAMPLE-AES-CTR", "SAMPLE-AES-CENC"}:
        key.classification = "HLS_SAMPLE_AES"
        key.drm = None
        key.evidence.append("仅发现 SAMPLE-AES identity；没有明确 DRM 系统证据。")
    else:
        key.classification = "HLS_ENCRYPTION_UNKNOWN"
        key.drm = None
        key.evidence.append("未识别的 identity 加密方法；无法仅凭 METHOD 判定 DRM。")
    return key


def _aggregate_hls(info: ManifestInfo) -> None:
    methods = _unique([key.method for key in info.keys if key.method != "NONE"])
    info.encryption = "None" if not methods else (methods[0] if len(methods) == 1 else "Mixed: " + ", ".join(methods))
    protected = [key for key in info.keys if key.drm is True]
    if protected:
        info.drm = True
        info.classification = _prefer_classification([key.classification for key in protected])
    elif any(key.classification == "HLS_SAMPLE_AES" for key in info.keys):
        info.drm = None
        info.classification = "HLS_SAMPLE_AES"
    elif any(key.classification == "HLS_ENCRYPTION_UNKNOWN" for key in info.keys):
        info.drm = None
        info.classification = "HLS_ENCRYPTION_UNKNOWN"
    elif any(key.classification == "HLS_AES128" for key in info.keys):
        info.drm = False
        info.classification = "HLS_AES128"
    else:
        info.drm = False
        info.classification = "CLEAR_HLS"
    info.evidence = _unique(info.evidence + [value for key in info.keys for value in key.evidence])


def _hls_duration(value: str) -> float:
    value = value.strip()
    if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value):
        raise _parse_error("HLS 分片时长必须是有效的十进制数。")
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        raise _parse_error("HLS 分片时长必须是有限的正数。")
    return duration


def _hls_byte_range(value: str) -> str:
    if not value:
        return ""
    if not re.fullmatch(r"[0-9]+(?:@[0-9]+)?", value) or not value.split("@", 1)[0].lstrip("0"):
        raise _parse_error("HLS BYTERANGE 必须为正长度或 length@offset。")
    # Keep the literal form, including an omitted offset. The downloader reads
    # the original playlist to resolve any implicit offset across segments.
    return value


def _hls_resource(uri: str, base_url: str, role: str, duration: float | None = None, byte_range: str = "") -> MediaResource:
    if not uri or any(ord(char) < 32 or ord(char) == 127 for char in uri):
        raise _parse_error("HLS 资源 URI 为空或包含无效控制字符。")
    return MediaResource(uri, _resolve_uri(base_url, uri), role, duration, byte_range)


def _preload_byte_range(attributes: dict[str, str]) -> str:
    start = attributes.get("BYTERANGE-START")
    length = attributes.get("BYTERANGE-LENGTH")
    if start is None and length is None:
        return ""
    if start is not None and not re.fullmatch(r"[0-9]+", start):
        raise _parse_error("HLS BYTERANGE-START 必须是非负十进制整数。")
    if length is not None and (not re.fullmatch(r"[0-9]+", length) or not length.lstrip("0")):
        raise _parse_error("HLS BYTERANGE-LENGTH 必须是正十进制整数。")
    # START defaults to 0. '@offset' denotes the explicitly approved open end.
    return (length or "") + "@" + (start if start is not None else "0")


def _parse_hls(text: str, base_url: str) -> ManifestInfo:
    lines = hls_lines(text)
    if not lines or lines[0] != "#EXTM3U":
        raise _parse_error("HLS 清单缺少有效的 #EXTM3U 起始标记。")
    info = ManifestInfo("HLS", "Media", classification="CLEAR_HLS", evidence=["内容以 #EXTM3U 开始，识别为 HLS。"])
    # Determine structure first, so a multivariant URI can never be registered
    # as a media segment, regardless of its filename or the tag's position.
    if any(line.split(":", 1)[0] in _MASTER_URI_TAGS | {"#EXT-X-STREAM-INF"} for line in lines[1:]):
        info.playlist_type = "Master"
    pending_stream = False
    pending_duration: float | None = None
    pending_byte_range = ""
    for line in lines[1:]:
        if not line.startswith("#"):
            if pending_stream:
                info.children.append(_resolve_uri(base_url, line))
                pending_stream = False
            elif info.playlist_type == "Media":
                info.resources.append(_hls_resource(line, base_url, "Media Segment", pending_duration, pending_byte_range))
                pending_duration = None
                pending_byte_range = ""
            continue
        tag, separator, value = line.partition(":")
        if tag in {"#EXT-X-KEY", "#EXT-X-SESSION-KEY"}:
            if not separator:
                raise _parse_error("HLS Key 标签缺少属性。")
            info.keys.append(_parse_hls_key(parse_attribute_list(value), base_url))
        elif tag == "#EXT-X-PLAYLIST-TYPE":
            playlist_type = value.strip().upper()
            if not separator or playlist_type not in {"VOD", "EVENT"} or info.hls_playlist_type:
                raise _parse_error("HLS EXT-X-PLAYLIST-TYPE 必须是单个 VOD 或 EVENT。")
            info.hls_playlist_type = playlist_type
        elif tag == "#EXTINF" and info.playlist_type == "Media":
            if pending_duration is not None:
                raise _parse_error("HLS EXTINF 缺少对应分片 URI。")
            pending_duration = _hls_duration(value.split(",", 1)[0])
        elif tag == "#EXT-X-BYTERANGE" and info.playlist_type == "Media":
            if pending_byte_range or not value:
                raise _parse_error("HLS BYTERANGE 缺少对应分片 URI 或范围。")
            pending_byte_range = _hls_byte_range(value.strip())
        elif tag in {"#EXT-X-MAP", "#EXT-X-PART", "#EXT-X-PRELOAD-HINT"} and info.playlist_type == "Media":
            attributes = parse_attribute_list(value)
            if tag == "#EXT-X-MAP":
                info.resources.append(_hls_resource(attributes.get("URI", ""), base_url, "Initialization Segment", byte_range=_hls_byte_range(attributes.get("BYTERANGE", ""))))
            elif tag == "#EXT-X-PART":
                duration = _hls_duration(attributes.get("DURATION", ""))
                info.resources.append(_hls_resource(attributes.get("URI", ""), base_url, "Partial Segment", duration, _hls_byte_range(attributes.get("BYTERANGE", ""))))
            elif attributes.get("TYPE") == "PART":
                info.resources.append(_hls_resource(attributes.get("URI", ""), base_url, "Preload Segment", byte_range=_preload_byte_range(attributes)))
        elif tag == "#EXT-X-STREAM-INF":
            if pending_stream:
                raise _parse_error("HLS Master 的 STREAM-INF 缺少对应子清单 URI。")
            parse_attribute_list(value)
            info.playlist_type = "Master"
            pending_stream = True
        elif tag in _MASTER_URI_TAGS:
            attributes = parse_attribute_list(value)
            info.playlist_type = "Master"
            if attributes.get("URI"):
                info.children.append(_resolve_uri(base_url, attributes["URI"]))
        elif tag == "#EXT-X-RENDITION-REPORT":
            attributes = parse_attribute_list(value)
            if attributes.get("URI"):
                info.children.append(_resolve_uri(base_url, attributes["URI"]))
    if pending_stream:
        raise _parse_error("HLS Master 的 STREAM-INF 缺少对应子清单 URI。")
    if pending_duration is not None or pending_byte_range:
        raise _parse_error("HLS 分片时长或 BYTERANGE 缺少对应资源 URI。")
    info.children = _unique(info.children)
    _aggregate_hls(info)
    return info


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _xml_pssh(element: ET.Element) -> tuple[list[str], list[str]]:
    encoded = re.sub(r"\s", "", "".join(element.itertext()))
    evidence = ["MPD 包含 PSSH 保护头元素。"]
    if not encoded or len(encoded) > 131_072:
        return ["DRM_UNKNOWN"], evidence
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        evidence.append("PSSH 内容不能解析为有效 Base64；保留保护标记，未提取任何密钥。")
        return ["DRM_UNKNOWN"], evidence
    classes, parsed_evidence = _pssh_metadata(data)
    return classes or ["DRM_UNKNOWN"], evidence + parsed_evidence


def _parse_dash(root: ET.Element) -> ManifestInfo:
    info = ManifestInfo("DASH", "MPD", classification="CLEAR_DASH", evidence=["XML 根元素为 MPD，识别为 DASH。"])
    consumed: set[int] = set()
    for element in root.iter():
        if _local_name(element.tag).lower() != "contentprotection":
            continue
        attrs = {_local_name(name).lower(): value for name, value in element.attrib.items()}
        scheme = attrs.get("schemeiduri", "")
        value = attrs.get("value", "")
        known = _known_classification(scheme) or _known_classification(value)
        encryption = value.upper() if value.lower() in {"cenc", "cbcs", "cens", "cbc1"} else "CENC"
        evidence = ["MPD 包含 ContentProtection。"]
        if scheme:
            evidence.append("ContentProtection schemeIdUri=" + scheme)
        if value:
            evidence.append("ContentProtection value=" + value)
        classes = [known] if known else []
        for child in element.iter():
            local = _local_name(child.tag).lower()
            if local == "pssh":
                child_classes, child_evidence = _xml_pssh(child)
                classes.extend(child_classes)
                evidence.extend(child_evidence)
                consumed.add(id(child))
            elif local == "pro" and "playready" in child.tag.lower():
                classes.append("DRM_PLAYREADY")
                evidence.append("ContentProtection 包含 PlayReady PRO 元素。")
        info.keys.append(KeyInfo(
            method=encryption, keyformat=scheme or "ContentProtection",
            classification=_prefer_classification(classes), drm=True, evidence=evidence,
        ))
    for element in root.iter():
        if _local_name(element.tag).lower() == "pssh" and id(element) not in consumed:
            classes, evidence = _xml_pssh(element)
            info.keys.append(KeyInfo(method="CENC", keyformat="PSSH", classification=_prefer_classification(classes), drm=True, evidence=evidence))
        elif any(_local_name(name).lower() == "default_kid" and value.strip() for name, value in element.attrib.items()) and not info.keys:
            info.keys.append(KeyInfo(method="CENC", keyformat="CENC", classification="DRM_UNKNOWN", drm=True, evidence=["MPD 包含 CENC default_KID 属性。" ]))
    if info.keys:
        info.drm = True
        info.classification = _prefer_classification([key.classification for key in info.keys])
        methods = _unique([key.method for key in info.keys])
        info.encryption = methods[0] if len(methods) == 1 else "Mixed: " + ", ".join(methods)
        info.evidence.extend(value for key in info.keys for value in key.evidence)
    info.evidence = _unique(info.evidence)
    return info


_BMFF_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf", b"mvex", b"mfra", b"sinf", b"schi", b"udta"}


def _bmff_protection(data: bytes) -> tuple[list[str], list[str], bool]:
    classes: list[str] = []
    evidence: list[str] = []
    saw_moov = False
    pending = [(0, len(data), 0)]
    boxes_read = 0
    while pending:
        start, end, depth = pending.pop()
        if depth > 32:
            continue
        offset = start
        while offset + 8 <= end and boxes_read < 100_000:
            size, kind = struct.unpack_from(">I4s", data, offset)
            header = 8
            if size == 1:
                if offset + 16 > end:
                    break
                size = struct.unpack_from(">Q", data, offset + 8)[0]
                header = 16
            elif size == 0:
                size = end - offset
            if size < header:
                break
            box_end = min(offset + size, end)
            complete = offset + size <= end
            content_start = offset + header
            boxes_read += 1
            if kind == b"moov":
                saw_moov = True
            if kind == b"pssh":
                parsed_classes, parsed_evidence = _pssh_metadata(data[offset:box_end])
                classes.extend(parsed_classes or ["DRM_UNKNOWN"])
                evidence.extend(parsed_evidence or ["MP4 包含 PSSH 保护头；所读取头部不完整。"])
            elif kind in {b"sinf", b"encv", b"enca", b"tenc"}:
                classes.append("DRM_UNKNOWN")
                evidence.append("MP4 包含结构化保护标记 " + kind.decode("ascii") + "。")
            elif kind == b"schm" and content_start + 8 <= box_end:
                scheme = data[content_start + 4:content_start + 8]
                if scheme in {b"cenc", b"cbcs", b"cens", b"cbc1"}:
                    classes.append("DRM_UNKNOWN")
                    evidence.append("MP4 保护方案=" + scheme.decode("ascii"))
            if kind in _BMFF_CONTAINERS:
                pending.append((content_start, box_end, depth + 1))
            elif kind == b"meta" and content_start + 4 <= box_end:
                pending.append((content_start + 4, box_end, depth + 1))
            elif kind == b"stsd" and content_start + 8 <= box_end:
                pending.append((content_start + 8, box_end, depth + 1))
            if not complete:
                break
            offset += size
    return _unique(classes), _unique(evidence), saw_moov


def _direct_magic(data: bytes) -> str:
    if len(data) >= 8 and data[4:8] in {b"ftyp", b"moov", b"moof", b"styp", b"sidx", b"free", b"pssh"}:
        return "ISO-BMFF"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "WebM/Matroska"
    if data.startswith((b"OggS", b"FLV", b"ID3")):
        return "直接媒体容器"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] in {b"AVI ", b"WAVE"}:
        return "RIFF 媒体容器"
    if len(data) > 188 and data[0] == 0x47 and data[188] == 0x47:
        return "MPEG-TS"
    return ""


def _parse_manifest(data: bytes, base_url: str, content_type: str = "") -> ManifestInfo:
    """Classify HLS/DASH/direct bytes; protection is evidence, not an exception."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise _parse_error("媒体响应为空或不是字节数据，无法解析。")
    if len(data) > 16_777_216:
        raise _parse_error("媒体清单超出解析大小限制。")
    data = bytes(data)
    mime = content_type.partition(";")[0].strip().lower()
    prefix = data.lstrip(b" \t\r\n")
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:].lstrip(b" \t\r\n")
    if prefix.startswith(b"#EXTM3U"):
        try:
            text = data.decode("utf-8-sig").lstrip(" \t\r\n")
        except UnicodeDecodeError:
            raise _parse_error("HLS 清单不是有效 UTF-8 文本。") from None
        return _parse_hls(text, base_url)
    magic = _direct_magic(data)
    if not magic and (prefix.startswith(b"<") or data.startswith((b"\xff\xfe", b"\xfe\xff")) or mime == "application/dash+xml"):
        # ElementTree does not fetch external resources; explicitly reject
        # DTD/entities as well to avoid expansion and ambiguous XML content.
        xml_probe = data.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in xml_probe or b"<!ENTITY" in xml_probe:
            raise _parse_error("不支持包含 DTD 或实体声明的媒体 XML。")
        try:
            root = ET.fromstring(data)
        except (ET.ParseError, ValueError):
            raise _parse_error("DASH/XML 清单格式不完整或无法解析。") from None
        if _local_name(root.tag) != "MPD":
            raise _parse_error("返回内容不是 MPD 媒体清单，可能是普通网页、登录页或错误响应。")
        return _parse_dash(root)
    if mime in _HLS_CONTENT_TYPES:
        raise _parse_error("响应声明为 HLS，但内容缺少 #EXTM3U 清单标记。")
    direct_type = mime.startswith(("video/", "audio/")) or mime in {"application/mp4", "application/ogg", "application/x-matroska"}
    if magic or direct_type:
        info = ManifestInfo("Direct", classification="DIRECT_MEDIA", evidence=["依据实际媒体字节或 Content-Type 识别为直接媒体。"])
        if magic == "ISO-BMFF" or mime in {"video/mp4", "audio/mp4", "application/mp4"}:
            classes, evidence, saw_moov = _bmff_protection(data)
            info.evidence.extend(evidence)
            if classes:
                info.drm = True
                info.encryption = "CENC"
                info.classification = _prefer_classification(classes)
            elif not saw_moov:
                info.evidence.append("所读取数据未包含 moov；未发现 DRM 证据，不能据此确认完整文件状态。")
        return info
    raise _parse_error("返回内容未识别为 HLS、DASH 或直接媒体；不能仅凭 URL 后缀判断。")


def parse_manifest(data: bytes, base_url: str, content_type: str = '',
                   url_resolution_policy: str = 'STANDARD_URLJOIN') -> ManifestInfo:
    from core.url_policy import UrlResolutionPolicy, resolve_media_url
    info = _parse_manifest(data, base_url, content_type)
    policy = UrlResolutionPolicy(url_resolution_policy)
    if info.media_type == 'HLS' and policy != UrlResolutionPolicy.STANDARD_URLJOIN:
        for key in info.keys:
            if key.uri and not key.uri.startswith(('data:', 'skd:')):
                key.resolved_uri = resolve_media_url(base_url, key.uri, policy)
        for resource in info.resources:
            resource.resolved_uri = resolve_media_url(base_url, resource.uri, policy)
        # children are already absolute; recover their original Manifest URIs.
        manifest_uris = [resource.resolved_uri for resource in info.resources if resource.role == 'Manifest']
        if manifest_uris:
            info.children = list(dict.fromkeys(manifest_uris))
    return info
