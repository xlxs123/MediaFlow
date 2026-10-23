"""将普通 JSON DASH 描述转换为有界、可验证的 MPD，不请求任何网络资源。"""
from __future__ import annotations

from dataclasses import replace
import json
import math
import re
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from core.dash_models import DashDescription, DashTrack
from core.errors import MediaCheckError, ProtectedMediaError
from core.request_context import validate_url


MAX_DESCRIPTION_BYTES = 16 * 1024 * 1024
MAX_CONFIGURABLE_BYTES = 128 * 1024 * 1024
MAX_DEPTH = 24
MAX_NODES = 100_000
MAX_TRACKS = 256
MAX_BACKUPS = 16
_MPD_NAMESPACE = 'urn:mpeg:dash:schema:mpd:2011'
_DRM_IDENTIFIERS = {
    'edef8ba9-79d6-4ace-a3c8-27dcd51d21ed': 'DRM_WIDEVINE',
    '9a04f079-9840-4286-ab92-e65be0885f95': 'DRM_PLAYREADY',
    '94ce86fb-07ff-4f43-adb8-93d2fa968ca2': 'DRM_FAIRPLAY',
    'com.widevine.alpha': 'DRM_WIDEVINE',
    'com.widevine': 'DRM_WIDEVINE',
    'widevine': 'DRM_WIDEVINE',
    'com.microsoft.playready': 'DRM_PLAYREADY',
    'playready': 'DRM_PLAYREADY',
    'com.apple.streamingkeydelivery': 'DRM_FAIRPLAY',
    'fairplay': 'DRM_FAIRPLAY',
}


def _field(value, *names):
    for name in names:
        if name in value and value[name] not in (None, ''):
            return value[name]
    return None


def _text(value, limit=512):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ''
    value = str(value)
    if len(value) > limit or any(ord(char) < 32 and char not in '\t\n\r'
                                or 0xD800 <= ord(char) <= 0xDFFF
                                or ord(char) in (0xFFFE, 0xFFFF) for char in value):
        return ''
    return value


def _url(value, base_url):
    # 仅处理明确的轨道 URL 字段，不递归把编码、Range 或普通短字符串当 URL。
    if (not isinstance(value, str) or not value or len(value) > 65536
            or _text(value, 65536) != value or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or '\\' in value):
        return ''
    try:
        scheme = urlsplit(value).scheme
        target = value if scheme else urljoin(base_url, value)
        validate_url(target)
    except (ValueError, TypeError):
        return ''
    return target


def _positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _integer(value):
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if 0 <= number <= 2**63 - 1 else 0


def _invalid_constant(_value):
    raise ValueError('JSON 包含非有限数值。')


def _byte_range(value):
    if value in (None, ''):
        return ''
    if not isinstance(value, str) or len(value) > 48 or not re.fullmatch(r'[0-9]+-[0-9]+', value):
        return None
    start, end = value.split('-', 1)
    return value if int(start) <= int(end) and int(end) <= 2**63 - 1 else None


def _name(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def _active(value):
    if value is None or value is False or value == 0:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ('', 'none', 'false', '0', 'clear', 'identity')
    return bool(value)


def _known_drm(value):
    if not isinstance(value, str):
        return ''
    identifier = value.strip().lower().removeprefix('urn:uuid:').strip('{}')
    if identifier.startswith('com.microsoft.playready.'):
        return 'DRM_PLAYREADY'
    return _DRM_IDENTIFIERS.get(identifier, '')


def _protection(payload):
    """明确的保护字段才是证据；AES、key、加密分片等字样本身不是 DRM。"""
    classes = set()
    stack = [(payload, 0, False)]
    visited = 0
    while stack:
        value, depth, protection_context = stack.pop()
        visited += 1
        if depth > MAX_DEPTH or visited > MAX_NODES:
            return None
        if isinstance(value, dict):
            if len(stack) + len(value) > MAX_NODES - visited:
                return None
            for key, child in value.items():
                name = _name(key)
                protected_field = name in ('contentprotection', 'pssh', 'cencpssh', 'drm', 'drmtype',
                                           'keysystem', 'keysystems', 'licenseurl', 'licenseserver', 'licenseserverurl')
                if protected_field and _active(child):
                    classes.add(_known_drm(child) or 'DRM_UNKNOWN')
                elif name in ('widevine', 'playready', 'fairplay') and _active(child):
                    classes.add(_DRM_IDENTIFIERS[name])
                elif name in ('keyformat', 'schemeiduri', 'encryptionscheme', 'encryption', 'protectionscheme'):
                    known = _known_drm(child)
                    if known:
                        classes.add(known)
                    elif isinstance(child, str) and (child.lower().startswith('urn:uuid:')
                              or child.lower() in ('cenc', 'cbcs', 'cens', 'cbc1')
                              or name == 'keyformat' and _active(child)):
                        classes.add('DRM_UNKNOWN')
                stack.append((child, depth + 1, protection_context or protected_field))
        elif isinstance(value, list):
            if len(stack) + len(value) > MAX_NODES - visited:
                return None
            stack.extend((child, depth + 1, protection_context) for child in value)
        elif isinstance(value, str) and protection_context:
            known = _known_drm(value)
            if known:
                classes.add(known)
    for classification in ('DRM_WIDEVINE', 'DRM_PLAYREADY', 'DRM_FAIRPLAY', 'DRM_UNKNOWN'):
        if classification in classes:
            return classification
    return 'CLEAR_DASH'


def _frame_rate(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ''
    text = str(value)
    if len(text) > 64 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:/[0-9]+(?:\.[0-9]+)?)?', text):
        return ''
    numerator, _, denominator = text.partition('/')
    return text if _positive_number(numerator) and (not denominator or _positive_number(denominator)) else ''


def _track(value, role, index, base_url):
    if not isinstance(value, dict):
        return None
    url = _url(_field(value, 'baseUrl', 'base_url', 'url'), base_url)
    backups = _field(value, 'backupUrl', 'backup_url', 'backupUrls', 'backup_urls') or ()
    if isinstance(backups, str):
        backups = (backups,)
    if not isinstance(backups, (list, tuple)):
        backups = ()
    backup_urls = tuple(dict.fromkeys(target for item in backups[:MAX_BACKUPS]
                                     if (target := _url(item, base_url)) and target != url))
    if not url and backup_urls:
        url, backup_urls = backup_urls[0], backup_urls[1:]
    if not url:
        return None
    segment = _field(value, 'SegmentBase', 'segment_base', 'segmentBase') or {}
    if not isinstance(segment, dict):
        return None
    initialization = _field(segment, 'Initialization', 'initialization')
    if initialization is None:
        initialization = _field(value, 'initialization', 'Initialization')
    if isinstance(initialization, dict):
        source = _field(initialization, 'sourceURL', 'source_url')
        if source and _url(source, url) != url:
            return None  # 当前模型不能准确表达另一个资源中的初始化数据。
        initialization = _field(initialization, 'range')
    initialization = _byte_range(initialization)
    index_range = _byte_range(_field(segment, 'indexRange', 'index_range')
                             or _field(value, 'indexRange', 'index_range'))
    if initialization is None or index_range is None:
        return None
    mime = (_text(_field(value, 'mimeType', 'mime_type'), 128).split(';', 1)[0].lower()
            or role + '/mp4')
    if not mime.lower().startswith(role + '/'):
        return None
    frame_rate = _frame_rate(_field(value, 'frameRate', 'frame_rate', 'fps'))
    return DashTrack(
        id=_text(value.get('id'), 128) or f'{role}-{index + 1}', url=url,
        backup_urls=backup_urls, bandwidth=_integer(value.get('bandwidth')),
        width=_integer(value.get('width')), height=_integer(value.get('height')),
        codecs=_text(value.get('codecs')), mime_type=mime,
        initialization=initialization, index_range=index_range,
        frame_rate=frame_rate, role=role,
        language=_text(_field(value, 'language', 'lang'), 40),
        name=_text(_field(value, 'name', 'label', 'title'), 256),
        channels=_integer(_field(value, 'channels', 'audio_channels')),
        sample_rate=_integer(_field(value, 'sample_rate', 'audioSamplingRate', 'asr')),
        default=value.get('default') is True,
    )


def parse_dash_description(data: bytes, base_url: str = '', *,
                           max_bytes: int = MAX_DESCRIPTION_BYTES) -> DashDescription | None:
    """解析 data.dash / dash / result.dash；无完整视频描述时返回 None。"""
    if (not isinstance(data, bytes) or not data or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool) or max_bytes <= 0
            or len(data) > min(max_bytes, MAX_CONFIGURABLE_BYTES)):
        return None
    try:
        payload = json.loads(data.decode('utf-8-sig'), parse_constant=_invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    wrapper, dash = None, None
    for owner in (payload.get('data'), payload, payload.get('result')):
        if isinstance(owner, dict) and isinstance(owner.get('dash'), dict):
            wrapper, dash = owner, owner['dash']
            break
    if dash is None:
        return None
    videos, audios = dash.get('video'), dash.get('audio') or []
    if (not isinstance(videos, list) or not videos or len(videos) > MAX_TRACKS
            or not isinstance(audios, list) or len(audios) > MAX_TRACKS):
        return None
    classification = _protection(payload)
    if classification is None:
        return None
    video_tracks = tuple(track for i, item in enumerate(videos)
                         if (track := _track(item, 'video', i, base_url)))
    audio_tracks = tuple(track for i, item in enumerate(audios)
                         if (track := _track(item, 'audio', i, base_url)))
    if not video_tracks:
        return None
    duration = _positive_number(dash.get('duration'))
    if duration is None:
        for owner in (wrapper, payload, dash):
            milliseconds = _positive_number(_field(owner, 'timelength', 'time_length'))
            if milliseconds is not None:
                duration = milliseconds / 1000
                break
    drm = classification != 'CLEAR_DASH'
    if drm:
        video_tracks = tuple(replace(track, drm=True) for track in video_tracks)
        audio_tracks = tuple(replace(track, drm=True) for track in audio_tracks)
    return DashDescription(video_tracks, audio_tracks, duration, drm, classification)


def choose_audio(tracks) -> DashTrack | None:
    """优先通用 AAC / mp4a，其次才比较码率；不请求或解密音频。"""
    best, best_key = None, None
    for index, track in enumerate(tracks):
        if index >= MAX_TRACKS:
            break
        if not isinstance(track, DashTrack) or track.role != 'audio':
            continue
        aac = (bool(re.match(r'^(?:mp4a(?:\.|$)|aac(?:[ ._-]|$))', track.codecs.lower()))
               or track.mime_type.lower() == 'audio/aac')
        key = (aac, track.bandwidth)
        if best_key is None or key > best_key:
            best, best_key = track, key
    return best


def build_dash_manifest(video: DashTrack, audio: DashTrack | None,
                        duration: float | None) -> str:
    """为一个视频版本生成 MPD，完整资源 URL 和 SegmentBase 范围不改写。"""
    if not isinstance(video, DashTrack) or video.role != 'video':
        raise MediaCheckError('DASH 描述缺少有效视频轨道。')
    if audio is not None and (not isinstance(audio, DashTrack) or audio.role != 'audio'):
        raise MediaCheckError('DASH 描述中的音频轨道无效。')
    tracks = [video] + ([audio] if audio is not None else [])
    if any(track.drm for track in tracks):
        raise ProtectedMediaError(diagnostic={'media_type': 'DASH', 'drm': True})
    attributes = {'xmlns': _MPD_NAMESPACE, 'type': 'static', 'minBufferTime': 'PT1.5S'}
    attributes['profiles'] = ('urn:mpeg:dash:profile:isoff-on-demand:2011'
                              if all(not track.mime_type or track.mime_type.endswith('/mp4') for track in tracks)
                              else 'urn:mpeg:dash:profile:full:2011')
    seconds = _positive_number(duration)
    if seconds is not None:
        attributes['mediaPresentationDuration'] = f'PT{seconds:.9f}'.rstrip('0').rstrip('.') + 'S'
    root = ET.Element('MPD', attributes)
    period = ET.SubElement(root, 'Period', {'start': 'PT0S'})
    for index, track in enumerate(tracks):
        if _url(track.url, '') != track.url:
            raise MediaCheckError('DASH 轨道没有有效 HTTP 媒体 URL。')
        mime = track.mime_type or track.role + '/mp4'
        if not mime.startswith(track.role + '/') or _text(mime, 128) != mime:
            raise MediaCheckError('DASH 轨道的媒体类型无效。')
        adaptation = ET.SubElement(period, 'AdaptationSet', {
            'id': str(index), 'contentType': track.role, 'mimeType': mime,
        })
        if track.language and re.fullmatch(r'[A-Za-z0-9_-]{2,40}', track.language):
            adaptation.set('lang', track.language)
        if track.name:
            ET.SubElement(adaptation, 'Label').text = _text(track.name, 256)
        if track.default:
            ET.SubElement(adaptation, 'Role', {'schemeIdUri': 'urn:mpeg:dash:role:2011', 'value': 'main'})
        representation = {'id': track.role + '-' + (_text(track.id, 128) or str(index)),
                          'bandwidth': str(max(1, _integer(track.bandwidth)))}
        if track.codecs:
            if _text(track.codecs) != track.codecs:
                raise MediaCheckError('DASH 轨道的编码信息无效。')
            representation['codecs'] = track.codecs
        if track.role == 'video':
            for name, value in (('width', track.width), ('height', track.height)):
                if _integer(value):
                    representation[name] = str(_integer(value))
            if track.frame_rate:
                if not _frame_rate(track.frame_rate):
                    raise MediaCheckError('DASH 轨道的帧率信息无效。')
                representation['frameRate'] = str(track.frame_rate)
        node = ET.SubElement(adaptation, 'Representation', representation)
        if track.role == 'audio':
            if track.sample_rate:
                node.set('audioSamplingRate', str(_integer(track.sample_rate)))
            if track.channels:
                ET.SubElement(node, 'AudioChannelConfiguration', {
                    'schemeIdUri': 'urn:mpeg:dash:23003:3:audio_channel_configuration:2011',
                    'value': str(_integer(track.channels))})
        ET.SubElement(node, 'BaseURL').text = track.url
        initialization, index_range = _byte_range(track.initialization), _byte_range(track.index_range)
        if initialization is None or index_range is None:
            raise MediaCheckError('DASH 轨道的 SegmentBase 字节范围无效。')
        if initialization or index_range:
            segment = ET.SubElement(node, 'SegmentBase', {'indexRange': index_range} if index_range else {})
            if initialization:
                ET.SubElement(segment, 'Initialization', {'range': initialization})
    return ET.tostring(root, encoding='unicode')
