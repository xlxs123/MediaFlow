"""轨道目录与选择；只处理已获得的元数据，不发请求、不授予下载资格。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import re
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

from core.manifests import hls_lines, parse_attribute_list
from core.request_context import validate_url

MAX_TRACKS = 128
MAX_MANIFEST = 16 * 1024 * 1024
TEXT_FORMATS = frozenset(('srt', 'vtt', 'ass', 'ssa', 'ttml', 'srv3', 'json3'))
LANGUAGES = (('', '自动 / 原有音轨'), ('zh-Hans', '简体中文'), ('zh-Hant', '繁体中文'),
             ('zh', '中文'), ('en', '英语'), ('ja', '日语'), ('ko', '韩语'),
             ('fr', '法语'), ('de', '德语'), ('es', '西班牙语'))
_LANG = {'zh': '中文', 'zho': '中文', 'chi': '中文', 'zh-hans': '简体中文',
         'zh-cn': '简体中文', 'zh-sg': '简体中文', 'zh-hant': '繁体中文',
         'zh-tw': '繁体中文', 'zh-hk': '繁体中文', 'en': '英语', 'eng': '英语',
         'ja': '日语', 'jpn': '日语', 'ko': '韩语', 'kor': '韩语', 'fr': '法语',
         'fra': '法语', 'fre': '法语', 'de': '德语', 'deu': '德语', 'ger': '德语',
         'es': '西班牙语', 'spa': '西班牙语', 'ru': '俄语', 'rus': '俄语',
         'pt': '葡萄牙语', 'por': '葡萄牙语', 'it': '意大利语', 'ita': '意大利语'}
_ALIASES = {'eng': 'en', 'jpn': 'ja', 'zho': 'zh', 'chi': 'zh', 'kor': 'ko',
            'fra': 'fr', 'fre': 'fr', 'deu': 'de', 'ger': 'de', 'spa': 'es',
            'zh-cn': 'zh-hans', 'zh-sg': 'zh-hans', 'zh-tw': 'zh-hant', 'zh-hk': 'zh-hant'}


def text(value, limit=256):
    return ''.join(c for c in str(value or '') if ord(c) >= 32)[:limit]


def language_code(value):
    value = text(value, 40).strip()
    return value if re.fullmatch(r'[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*', value) else ''


def language_name(value):
    code = language_code(value)
    key = code.lower().replace('_', '-')
    name = _LANG.get(key, _LANG.get(key.split('-')[0], '未知语言'))
    if key in ('', 'und', 'zxx', 'unknown'):
        name = '未知语言'
    return name + (f' ({code})' if code else '')


def same_language(left, right):
    def normalized(value):
        key = language_code(value).lower().replace('_', '-')
        return _ALIASES.get(key, key)
    return bool(normalized(left)) and normalized(left) == normalized(right)


def probe_language(value):
    """容器 language 标签使用 ISO 639-2；不从名称猜测语言。"""
    key = language_code(value).lower().replace('_', '-')
    return {'en': 'eng', 'ja': 'jpn', 'zh': 'zho', 'zh-hans': 'zho', 'zh-hant': 'zho',
            'zh-cn': 'zho', 'zh-tw': 'zho', 'ko': 'kor', 'fr': 'fra', 'de': 'deu',
            'es': 'spa', 'ru': 'rus', 'pt': 'por', 'it': 'ita'}.get(key, key or 'und')


def _number(value):
    try:
        return min(2 ** 53, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _url(value, base=''):
    try:
        value = urljoin(base, str(value or ''))
        validate_url(value)
        return value
    except (ValueError, TypeError):
        return ''


def _id(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()[:24]


def _resource_identity(url):
    from core.resume_identity import _asset_url
    return _asset_url(url) if url else ''


def _track(kind, source, identity, **values):
    return {'id': source + ':' + _id(identity), 'kind': kind, 'source': source,
            'language': '', 'name': '', 'codec': '', 'channels': 0, 'sample_rate': 0,
            'bitrate': 0, 'default': False, 'automatic': None, 'format': '',
            'supported': True, 'reason': '', **values}


def hls_catalog(manifest, base):
    audio, subtitles = [], []
    for number, line in enumerate(hls_lines(manifest)):
        if not line.startswith('#EXT-X-MEDIA:'):
            continue
        item = parse_attribute_list(line.partition(':')[2])
        kind = item.get('TYPE')
        if kind not in ('AUDIO', 'SUBTITLES', 'CLOSED-CAPTIONS'):
            continue
        uri = _url(item.get('URI'), base) if item.get('URI') else ''
        group = item.get('GROUP-ID', '')
        target = audio if kind == 'AUDIO' else subtitles
        target.append(_track('audio' if kind == 'AUDIO' else 'subtitle', 'hls',
            (group, item.get('NAME'), language_code(item.get('LANGUAGE')), _resource_identity(uri)),
            language=language_code(item.get('LANGUAGE')),
            name=text(item.get('NAME')), default=item.get('DEFAULT') == 'YES',
            forced=item.get('FORCED') == 'YES', channels=_number(item.get('CHANNELS', '').split('/')[0]),
            url=uri, group=group, line=number, format='hls-vtt' if kind == 'SUBTITLES' else '',
            supported=bool(uri), embedded=not bool(uri),
            reason='' if uri else '清单未提供独立资源；保留下载后的实际轨道，不能单独选择。'))
        if len(audio) + len(subtitles) >= MAX_TRACKS:
            break
    return {'audio': audio, 'subtitles': subtitles, 'known': True}


def _tag(node):
    return node.tag.rsplit('}', 1)[-1]


def _children(node, name):
    return [child for child in node if _tag(child) == name]


def dash_catalog(manifest, base):
    audio, subtitles = [], []
    try:
        if '<!DOCTYPE' in manifest.upper() or '<!ENTITY' in manifest.upper():
            return {'audio': [], 'subtitles': [], 'known': False}
        root = ET.fromstring(manifest)
    except (ET.ParseError, ValueError):
        return {'audio': [], 'subtitles': [], 'known': False}
    periods = _children(root, 'Period')
    def base_url(node, inherited):
        values = _children(node, 'BaseURL')
        return urljoin(inherited, values[0].text.strip()) if values and values[0].text else inherited
    root_base = base_url(root, base)
    for pi, period in enumerate(periods):
        pbase = base_url(period, root_base)
        for ai, adaptation in enumerate(_children(period, 'AdaptationSet')):
            abase = base_url(adaptation, pbase)
            reps = _children(adaptation, 'Representation') or [adaptation]
            for ri, rep in enumerate(reps):
                attrs = {**adaptation.attrib, **rep.attrib}
                mime = attrs.get('mimeType', '')
                kind = attrs.get('contentType') or mime.partition('/')[0]
                codec = attrs.get('codecs', '')
                if kind == 'audio':
                    stream_kind, target = 'audio', audio
                elif kind in ('text', 'subtitle') or codec.startswith(('stpp', 'wvtt')):
                    stream_kind, target = 'subtitle', subtitles
                else:
                    continue
                location = (pi, ai, ri)
                labels = _children(rep, 'Label') or _children(adaptation, 'Label')
                role = _children(adaptation, 'Role')
                channels = _children(rep, 'AudioChannelConfiguration') or _children(adaptation, 'AudioChannelConfiguration')
                standalone = bool(_children(rep, 'BaseURL') or _children(adaptation, 'BaseURL')) and not any(
                    _tag(n) in ('SegmentTemplate', 'SegmentList', 'SegmentBase') for n in (*root, *period, *adaptation, *rep))
                fmt = {'text/vtt': 'vtt', 'application/ttml+xml': 'ttml', 'application/x-subrip': 'srt'}.get(mime, '')
                supported = len(periods) == 1 and (stream_kind == 'audio' or standalone and fmt in ('vtt', 'srt', 'ttml'))
                identity = (pi, adaptation.get('id', attrs.get('lang', '')),
                            rep.get('id', str(ri)), attrs.get('lang', ''), codec)
                target.append(_track(stream_kind, 'dash', identity, location=list(location),
                    language=language_code(attrs.get('lang')), name=text(labels[0].text) if labels else '',
                    codec=text(codec), bitrate=_number(attrs.get('bandwidth')),
                    sample_rate=_number(attrs.get('audioSamplingRate')),
                    channels=_number(channels[0].attrib.get('value')) if channels else 0,
                    default=any(n.attrib.get('value') == 'main' for n in role), format=fmt,
                    url=_url(base_url(rep, abase)) if stream_kind == 'subtitle' and standalone else '',
                    supported=supported, embedded=not standalone and stream_kind == 'subtitle',
                    reason='' if supported else ('多 Period 清单暂不提供独立轨道选择。' if len(periods) != 1 else
                                                '分段或内嵌 DASH 字幕暂不支持独立提取；可在输出后检查实际轨道。')))
                if len(audio) + len(subtitles) >= MAX_TRACKS:
                    return {'audio': audio, 'subtitles': subtitles, 'known': True}
    return {'audio': audio, 'subtitles': subtitles, 'known': True}


def ytdlp_catalog(info):
    """接收现有提取器格式/字幕结果；格式选择仍交给 yt-dlp。"""
    audio, subtitles = [], []
    formats = info.get('formats', [])
    for item in formats[:MAX_TRACKS] if isinstance(formats, list) else []:
        if not isinstance(item, dict) or item.get('vcodec') != 'none' or item.get('acodec') in ('none', None):
            continue
        format_id = text(item.get('format_id'), 128)
        supported = bool(re.fullmatch(r'[A-Za-z0-9_.:-]+', format_id))
        audio.append(_track('audio', 'ytdlp', (format_id, language_code(item.get('language'))), format_id=format_id,
            language=language_code(item.get('language')), name=text(item.get('format_note')),
            codec=text(item.get('acodec')), bitrate=_number((item.get('abr') or 0) * 1000),
            channels=_number(item.get('audio_channels')), sample_rate=_number(item.get('asr')),
            default=item.get('language_preference') == 10, supported=supported,
            reason='' if supported else '提取器格式标识不能安全用于选择表达式。'))
    for key, automatic in (('subtitles', False), ('automatic_captions', True)):
        values = info.get(key, {})
        if not isinstance(values, dict):
            continue
        for language, items in list(values.items())[:MAX_TRACKS]:
            for index, item in enumerate(items[:16] if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                fmt = text(item.get('ext'), 16).lower()
                url = _url(item.get('url'))
                supported = bool(url) and fmt in ('srt', 'vtt', 'ass', 'ssa', 'ttml')
                subtitles.append(_track('subtitle', 'ytdlp', (key, language, index),
                    language=language_code(language), name=text(item.get('name')), automatic=automatic,
                    format=fmt, url=url, supported=supported,
                    reason='' if supported else '该字幕格式尚不能可靠提取为独立文本字幕。'))
                if len(subtitles) >= MAX_TRACKS:
                    return {'audio': audio, 'subtitles': subtitles, 'known': True}
    return {'audio': audio, 'subtitles': subtitles, 'known': True}


def _catalog(candidate):
    metadata = candidate.metadata
    saved = metadata.get('track_catalog')
    if isinstance(saved, dict):
        return deepcopy(saved)
    manifest = metadata.get('track_master_text') or candidate.manifest_text
    base = metadata.get('track_master_url') or candidate.manifest_url or candidate.final_url
    if manifest and len(manifest) <= MAX_MANIFEST:
        if candidate.media_type == 'HLS':
            return hls_catalog(manifest, base)
        if candidate.media_type == 'DASH':
            return dash_catalog(manifest, base)
    if any(key in metadata for key in ('formats', 'subtitles', 'automatic_captions')):
        return ytdlp_catalog(metadata)
    return {'audio': [], 'subtitles': [], 'known': False}


def html_subtitles(tracks, base=''):
    result = []
    for item in tracks[:MAX_TRACKS] if isinstance(tracks, list) else []:
        if not isinstance(item, dict) or item.get('kind') not in ('subtitles', 'captions'):
            continue
        url = _url(item.get('src'), base)
        if not url:
            continue
        language = language_code(item.get('language'))
        result.append(_track('subtitle', 'html', (_resource_identity(url), language), url=url,
            language=language, name=text(item.get('name')), default=item.get('default') is True,
            format='vtt'))  # HTML track 使用 WebVTT；下载后仍验证真实正文，不按扩展名放行。
    return result


def catalog(candidate):
    result = _catalog(candidate)
    extra = candidate.metadata.get('page_subtitles')
    if isinstance(extra, list):
        result = deepcopy(result)
        known = {item['id'] for item in result.get('subtitles', [])}
        result['subtitles'] = (result.get('subtitles', []) + [deepcopy(item) for item in extra[:MAX_TRACKS]
                                if isinstance(item, dict) and item.get('id') not in known])[:MAX_TRACKS]
        result['known'] = result.get('known') is True or bool(extra)
    for key in ('audio', 'subtitles'):
        unique = {}
        for item in result.get(key, [])[:MAX_TRACKS]:
            if isinstance(item, dict) and item.get('id'):
                unique.setdefault(item['id'], item)
        result[key] = list(unique.values())
    return result


def normalize_preferences(value=None):
    value = value if isinstance(value, dict) else {}
    languages = value.get('subtitle_languages', [])
    return {'audio_language': language_code(value.get('audio_language')),
            'subtitle_languages': list(dict.fromkeys(language_code(v) for v in languages
                                       if language_code(v)))[:16] if isinstance(languages, list) else [],
            'subtitle_mode': value.get('subtitle_mode') if value.get('subtitle_mode') in ('none', 'sidecar', 'mux') else 'none',
            'container': value.get('container') if value.get('container') in ('original', 'mp4', 'mkv', 'webm') else 'original'}


def default_selection(values, preferences=None):
    preferences = normalize_preferences(preferences)
    audio = [v for v in values.get('audio', []) if v.get('supported')]
    selected_audio = next((v for v in audio if v.get('default')), audio[0] if audio else None)
    language = preferences['audio_language']
    if language:
        selected_audio = next((v for v in audio if same_language(v.get('language'), language)), None)
        if selected_audio is None:
            raise ValueError(f'没有可选择的{language_name(language)}音轨；请确认本视频实际可用的音轨。')
    subtitles = []
    if preferences['subtitle_mode'] != 'none':
        for language in preferences['subtitle_languages']:
            matches = [v for v in values.get('subtitles', []) if v.get('supported') and same_language(v.get('language'), language)]
            if not matches:
                raise ValueError(f'没有可用的{language_name(language)}字幕；未替换为其他语言，请手动选择。')
            # 同语言只选一个格式，人工字幕优先，不重复下载同一字幕的多个表示。
            matches.sort(key=lambda v: (v.get('automatic') is True, v.get('format') not in ('srt', 'vtt')))
            subtitles.append(matches[0]['id'])
    return {'audio_id': selected_audio['id'] if selected_audio else '', 'subtitle_ids': subtitles,
            'subtitle_mode': preferences['subtitle_mode'], 'container': preferences['container']}


def validate_selection(values, selection):
    if not isinstance(selection, dict):
        raise ValueError('轨道选择格式无效。')
    audio_id = selection.get('audio_id', '')
    subtitles = selection.get('subtitle_ids', [])
    if not isinstance(subtitles, list) or len(subtitles) > 32 or len(set(subtitles)) != len(subtitles):
        raise ValueError('字幕选择无效或重复。')
    for kind, identifiers in (('audio', [audio_id] if audio_id else []), ('subtitles', subtitles)):
        for identifier in identifiers:
            item = next((v for v in values.get(kind, []) if v['id'] == identifier), None)
            if item is None or not item.get('supported'):
                raise ValueError(item.get('reason') if item else '所选轨道不属于当前媒体，请重新选择。')
    if selection.get('subtitle_mode', 'none') not in ('none', 'sidecar', 'mux'):
        raise ValueError('字幕输出方式无效。')
    if selection.get('container', 'original') not in ('original', 'mp4', 'mkv', 'webm'):
        raise ValueError('输出容器无效。')
    if subtitles and selection.get('subtitle_mode', 'none') == 'none':
        raise ValueError('请为所选字幕指定独立文件或封装方式。')
    if selection.get('subtitle_mode') == 'mux' and any(v.get('format') == 'ttml' and v['id'] in subtitles
                                                       for v in values.get('subtitles', [])):
        raise ValueError('TTML 暂支持独立字幕文件；请更换字幕格式后封装。')
    return True


def _hls_selection(manifest, base, audio, variant_url=''):
    lines = hls_lines(manifest)
    result, pending, variants = [], None, []
    for number, line in enumerate(lines):
        if line.startswith('#EXT-X-MEDIA:'):
            attrs = parse_attribute_list(line.partition(':')[2])
            if attrs.get('TYPE') in ('SUBTITLES', 'CLOSED-CAPTIONS'):
                continue  # 字幕单独获取，不把字幕播放列表当作音视频清单。
            if audio and attrs.get('TYPE') == 'AUDIO' and number != audio.get('line'):
                continue
        if line.startswith('#EXT-X-STREAM-INF:'):
            pending = line
            continue
        if pending is not None and line and not line.startswith('#'):
            attrs = parse_attribute_list(pending.partition(':')[2])
            if (not variant_url or urljoin(base, line) == variant_url) and (not audio or attrs.get('AUDIO') == audio.get('group')):
                pending = re.sub(r',?(?:SUBTITLES|CLOSED-CAPTIONS)=(?:"[^"]*"|[^,]*)', '', pending)
                variants.extend((pending, line))
            pending = None
            continue
        result.append(line)
    if not variants and '#EXT-X-STREAM-INF:' in manifest:
        raise ValueError('所选音轨不属于当前视频版本的音频组，请选择兼容音轨。')
    return '\n'.join(result + variants) + '\n'


def apply_selection(candidate, selection=None):
    """返回副本，不改变可信度、候选资格或原始捕获内容。"""
    if candidate.drm is True:
        from core.errors import ProtectedMediaError
        raise ProtectedMediaError()
    result = deepcopy(candidate)
    values = catalog(candidate)
    selection = deepcopy(selection if selection is not None else candidate.metadata.get('track_selection'))
    if selection is None:
        selection = default_selection(values)
    validate_selection(values, selection)
    audio = next((v for v in values.get('audio', []) if v['id'] == selection.get('audio_id')), None)
    selected_subs = [v for v in values.get('subtitles', []) if v['id'] in selection.get('subtitle_ids', [])]
    manifest = result.metadata.get('track_master_text') or result.manifest_text
    base = result.metadata.get('track_master_url') or result.manifest_url or result.final_url
    if audio and audio['source'] == 'dash-json':
        from core.dash_models import DashTrack
        from core.dash_json import build_dash_manifest
        result.audio_track = DashTrack(**audio['track'])
        result.manifest_text = build_dash_manifest(result.video_track, result.audio_track, result.duration)
        result.audioBitrate = result.audio_track.bandwidth
    elif manifest and result.media_type == 'HLS' and (values.get('audio') or values.get('subtitles')):
        result.manifest_text = _hls_selection(manifest, base, audio if audio and audio['source'] == 'hls' else None,
                                               result.metadata.get('track_variant_url', ''))
        result.manifest_url = base
        # 子版本提升为带所选外部音轨的主清单时，以真实父清单作为入口。
        # 否则检查缓存会把 video.m3u8 别名指向合成主清单，误将清单正文下载成视频分片。
        if result.metadata.get('track_variant_url'):
            result.request_context = result.request_context.derive(base)
            result.transport_url = base
        result.manifest_origin = 'selected:hls-tracks'
    elif manifest and result.media_type == 'DASH' and (audio or selected_subs):
        root = ET.fromstring(manifest)
        for pi, period in enumerate(_children(root, 'Period')):
            for ai, adaptation in enumerate(_children(period, 'AdaptationSet')):
                reps = _children(adaptation, 'Representation') or [adaptation]
                kind = adaptation.get('contentType') or adaptation.get('mimeType', '').partition('/')[0]
                if kind in ('text', 'subtitle'):
                    period.remove(adaptation)
                    continue
                for ri, rep in enumerate(reps):
                    attrs = {**adaptation.attrib, **rep.attrib}
                    rep_kind = attrs.get('contentType') or attrs.get('mimeType', '').partition('/')[0]
                    if audio and audio['source'] == 'dash' and rep_kind == 'audio' and [pi, ai, ri] != audio.get('location'):
                        if rep is adaptation:
                            period.remove(adaptation)
                        else:
                            adaptation.remove(rep)
                if kind == 'audio' and not _children(adaptation, 'Representation') and reps != [adaptation] and adaptation in list(period):
                    period.remove(adaptation)
        result.manifest_text = ET.tostring(root, encoding='unicode')
        result.manifest_origin = 'selected:dash-tracks'
    if audio and audio['source'] == 'ytdlp':
        current = result.metadata.get('format_selector', '')
        video = current.partition('+')[0] if current and '/' not in current else 'bestvideo'
        result.metadata['format_selector'] = video + '+' + audio['format_id']
    container = selection.get('container', 'original')
    if container in ('mp4', 'webm'):
        from core.track_pipeline import check_compatibility
        video_codec = result.videoCodec or result.metadata.get('video_codec')
        audio_codec = audio.get('codec') if audio else result.audioCodec
        def codec_name(value):
            value = str(value or '').lower()
            return ('h264' if value.startswith(('avc', 'h264', 'h.264')) else
                    'hevc' if value.startswith(('hev', 'hvc', 'h265', 'h.265')) else
                    'aac' if value.startswith(('mp4a', 'aac')) else
                    'av1' if value.startswith(('av01', 'av1')) else
                    'vp9' if value.startswith(('vp09', 'vp9')) else value)
        summary = {'video_streams': [{'codec_name': codec_name(video_codec)}] if video_codec else [],
                   'audio_streams': [{'codec_name': codec_name(audio_codec)}] if audio_codec else []}
        check_compatibility(summary, selected_subs if selection.get('subtitle_mode') == 'mux' else [], container)
    if container != 'original':
        result.metadata['track_output_extension'] = container
    result.metadata['track_selection'] = selection
    result.metadata['selected_audio'] = deepcopy(audio) if audio else None
    result.metadata['selected_subtitles'] = deepcopy(selected_subs)
    return result


def json_audio_catalog(description):
    from core.dash_json import choose_audio
    preferred = next((track for track in description.audios if track.default), choose_audio(description.audios))
    return {'known': True, 'subtitles': [], 'audio': [
        _track('audio', 'dash-json', (track.id, track.language, track.codecs), track=asdict(track), language=track.language,
               name=track.name, codec=track.codecs, channels=track.channels,
               sample_rate=track.sample_rate, bitrate=track.bandwidth, default=track is preferred)
        for index, track in enumerate(description.audios[:MAX_TRACKS])]}


def selection_identity(selection):
    """恢复身份只含选择 ID 和输出策略的摘要，不包含签名地址。"""
    return hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
