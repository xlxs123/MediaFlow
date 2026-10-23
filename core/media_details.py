"""Normalize media details from already captured evidence; no I/O or downloads."""
from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET

from core.http_ranges import parse_content_range
from core.manifests import parse_attribute_list


MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_SIZE = 2**63 - 1
MAX_ATOMS = 4096
MAX_TABLE_ENTRIES = 16384
_FIELDS = ('width', 'height', 'fps', 'videoCodec', 'audioCodec', 'videoBitrate',
           'audioBitrate', 'estimatedSize', 'container')
_RANK = {'dom-resolution': 30, 'http-content-type': 40, 'vod-bandwidth-estimate': 20,
         'manifest-codecs': 60, 'manifest-frame-rate': 60, 'explicit-metadata': 60,
         'hls-extinf': 60, 'hls-audio-group': 60, 'dash-representation': 70,
         'dash-duration': 70, 'dash-mime': 70, 'declared-resolution': 80,
         'segment-mime': 80, 'captured-magic': 90, 'mp4-stsd': 100,
         'mp4-tkhd': 100, 'mp4-mdhd': 100, 'mp4-stts': 100, 'mp4-btrt': 100,
         'mp4-ftyp': 100, 'mp4-soun': 100, 'http-content-length': 110,
         'http-content-range': 120}
_MIMES = {'video/mp4': 'MP4', 'audio/mp4': 'MP4', 'application/mp4': 'MP4',
          'video/quicktime': 'MOV', 'video/webm': 'WebM', 'audio/webm': 'WebM',
          'video/x-matroska': 'Matroska', 'audio/x-matroska': 'Matroska',
          'video/mp2t': 'MPEG-TS', 'audio/mpeg': 'MP3', 'audio/aac': 'AAC',
          'audio/ogg': 'Ogg', 'video/ogg': 'Ogg', 'application/ogg': 'Ogg',
          'audio/wav': 'WAV', 'audio/x-wav': 'WAV', 'video/x-flv': 'FLV'}


def _number(value, maximum=MAX_SIZE):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and len(value) > 64:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 < number <= maximum else None
    except (ValueError, OverflowError):
        return None


def _integer(value, maximum=MAX_SIZE):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not re.fullmatch(r'\d{1,20}', value.strip()):
            return None
        value = int(value)
    if type(value) is int:
        return value if 0 < value <= maximum else None
    number = _number(value, maximum)
    return int(number) if number is not None and number.is_integer() else None


def _dimensions(value):
    if isinstance(value, str):
        match = re.fullmatch(r'(\d{1,5})[xX](\d{1,5})', value.strip())
        value = match.groups() if match else None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    width, height = (_integer(item, 65535) for item in value)
    return (width, height) if width and height else None


def _fps(value):
    if isinstance(value, str) and len(value) <= 64 and '/' in value:
        numerator, _, denominator = value.partition('/')
        top, bottom = _number(numerator), _number(denominator)
        return _number(top / bottom, 1000) if top and bottom else None
    return _number(value, 1000)


def _codec(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,95}', value) else None


def _codec_kind(value):
    value = value.lower()
    if value.startswith(('avc1', 'avc3', 'hvc1', 'hev1', 'vp09', 'vp08', 'av01', 'mp4v')) or value in (
            'h264', 'h265', 'hevc', 'av1', 'vp8', 'vp9', 'theora', 'mjpeg'):
        return 'video'
    if value.startswith(('mp4a', 'ac-3', 'ec-3')) or value in ('aac', 'mp3', 'opus', 'vorbis', 'flac', 'alac', 'ac3', 'eac3'):
        return 'audio'
    return ''


def _codecs(value):
    if not isinstance(value, str) or len(value) > 512:
        return {}
    result = {}
    for item in value.split(',')[:16]:
        item = _codec(item.strip())
        kind = _codec_kind(item) if item else ''
        if kind and kind not in result:
            result[kind] = item
    return result


def _atoms(data, start=0, end=None, *, allow_zero=False, partial=False):
    end = len(data) if end is None else end
    result = []
    while start < end:
        if len(result) >= MAX_ATOMS or end - start < 8:
            if partial:
                break
            raise ValueError('invalid atom list')
        size, kind = struct.unpack_from('>I4s', data, start)
        header = 8
        if size == 1:
            if end - start < 16:
                if partial:
                    break
                raise ValueError('incomplete large atom')
            size, header = struct.unpack_from('>Q', data, start + 8)[0], 16
        elif size == 0:
            size = end - start if allow_zero else 0
        if size < header or start + size > end:
            if partial:
                break
            raise ValueError('incomplete atom')
        result.append((kind, start + header, start + size))
        start += size
    return result


def _find(atoms, kind):
    return next((item for item in atoms if item[0] == kind), None)


def _mdhd(data, atom):
    if not atom:
        return None, None
    _, start, end = atom
    version = data[start] if start < end else -1
    if version == 0 and end - start >= 24:
        scale, ticks = struct.unpack_from('>II', data, start + 12)
        unknown = 2**32 - 1
    elif version == 1 and end - start >= 36:
        scale = struct.unpack_from('>I', data, start + 20)[0]
        ticks = struct.unpack_from('>Q', data, start + 24)[0]
        unknown = 2**64 - 1
    else:
        return None, None
    return (scale or None), _number(ticks / scale) if scale and ticks != unknown else None


def _track(data, atom):
    _, start, end = atom
    children = _atoms(data, start, end)
    mdia = _find(children, b'mdia')
    if not mdia:
        return {}
    media = _atoms(data, mdia[1], mdia[2])
    handler = _find(media, b'hdlr')
    kind = ''
    if handler and handler[2] - handler[1] >= 12:
        kind = {b'vide': 'video', b'soun': 'audio'}.get(data[handler[1] + 8:handler[1] + 12], '')
    scale, duration = _mdhd(data, _find(media, b'mdhd'))
    result = {'kind': kind, 'duration': duration}
    tkhd = _find(children, b'tkhd')
    if tkhd and tkhd[1] < tkhd[2]:
        version = data[tkhd[1]]
        offset = 76 if version == 0 else 88 if version == 1 else None
        if offset is not None and tkhd[2] - tkhd[1] >= offset + 8:
            width, height = struct.unpack_from('>II', data, tkhd[1] + offset)
            result['dimensions'] = _dimensions((round(width / 65536), round(height / 65536)))
    minf = _find(media, b'minf')
    stbl = _find(_atoms(data, minf[1], minf[2]), b'stbl') if minf else None
    table = _atoms(data, stbl[1], stbl[2]) if stbl else []
    stsd = _find(table, b'stsd')
    if stsd and stsd[2] - stsd[1] >= 8:
        count = struct.unpack_from('>I', data, stsd[1] + 4)[0]
        entries = _atoms(data, stsd[1] + 8, stsd[2])
        if count > 64 or len(entries) != count:
            raise ValueError('invalid sample descriptions')
        for sample, payload, sample_end in entries:
            codec = _codec(sample.decode('ascii', errors='replace'))
            sample_kind = _codec_kind(codec) if codec else ''
            current_kind = kind or sample_kind
            if not codec or codec in ('encv', 'enca') or current_kind not in ('video', 'audio'):
                continue
            if sample_kind and kind and sample_kind != kind:
                continue
            result.update(kind=current_kind, codec=codec)
            extra = 78 if current_kind == 'video' else 28
            if current_kind == 'video' and sample_end - payload >= 28:
                result['sample_dimensions'] = _dimensions(struct.unpack_from('>HH', data, payload + 24))
            if current_kind == 'audio' and sample_end - payload >= 10:
                version = struct.unpack_from('>H', data, payload + 8)[0]
                extra = 28 if version == 0 else 44 if version == 1 else sample_end - payload
            if sample_end - payload >= extra:
                extensions = _atoms(data, payload + extra, sample_end)
                avcc = _find(extensions, b'avcC')
                if sample in (b'avc1', b'avc3') and avcc and avcc[2] - avcc[1] >= 4 and data[avcc[1]] == 1:
                    result['codec'] = codec + '.' + data[avcc[1] + 1:avcc[1] + 4].hex()
                btrt = _find(extensions, b'btrt')
                if btrt and btrt[2] - btrt[1] >= 12:
                    result['bitrate'] = _integer(struct.unpack_from('>I', data, btrt[1] + 8)[0])
            break
    stts = _find(table, b'stts')
    if result['kind'] == 'video' and scale and stts and stts[2] - stts[1] >= 8:
        count = struct.unpack_from('>I', data, stts[1] + 4)[0]
        if count <= MAX_TABLE_ENTRIES and stts[2] - stts[1] == 8 + count * 8:
            samples = ticks = 0
            for offset in range(stts[1] + 8, stts[2], 8):
                number, delta = struct.unpack_from('>II', data, offset)
                if not number or not delta:
                    break
                samples += number
                ticks += number * delta
            else:
                if ticks:
                    result['fps'] = _fps(samples * scale / ticks)
    return result


def _mp4(data, body_complete):
    result = {}
    atoms = _atoms(data, allow_zero=body_complete, partial=True)
    ftyp = _find(atoms, b'ftyp')
    if ftyp and ftyp[2] - ftyp[1] >= 8:
        brands = [data[ftyp[1]:ftyp[1] + 4]] + [data[pos:pos + 4] for pos in range(ftyp[1] + 8, ftyp[2] - 3, 4)]
        result['container'] = 'MOV' if brands[0] == b'qt  ' else 'MP4' if any(
            brand in (b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'mp41', b'mp42', b'avc1',
                      b'M4V ', b'M4A ', b'dash', b'cmfc', b'cmfs', b'msdh', b'msix') for brand in brands) else 'ISO BMFF'
    moov = _find(atoms, b'moov')
    if not moov:
        return result
    try:
        children = _atoms(data, moov[1], moov[2])
        tracks = []
        for atom in children:
            if atom[0] == b'trak':
                if len(tracks) >= 64:
                    return result
                tracks.append(_track(data, atom))
    except (ValueError, struct.error):
        return result
    videos = [track for track in tracks if track.get('kind') == 'video']
    audios = [track for track in tracks if track.get('kind') == 'audio']
    result['has_video'] = bool(videos)
    result.update(video_track_count=len(videos), audio_track_count=len(audios),
                  unknown_track_count=len(tracks)-len(videos)-len(audios))
    if videos:
        video = videos[0]
        dimensions = video.get('dimensions') or video.get('sample_dimensions')
        if dimensions:
            result.update(width=dimensions[0], height=dimensions[1], dimensions_source='mp4-tkhd' if video.get('dimensions') else 'mp4-stsd')
        result.update(videoCodec=video.get('codec'), videoBitrate=video.get('bitrate'), fps=video.get('fps'))
    if audios:
        result.update(audioCodec=audios[0].get('codec'), audioBitrate=audios[0].get('bitrate'), has_audio=True)
    durations = [track['duration'] for track in tracks if track.get('duration')]
    if durations:
        result['duration'] = max(durations)
    media_tracks = videos + audios
    if media_tracks and all(track.get('bitrate') for track in media_tracks):
        result['combined'] = sum(track['bitrate'] for track in media_tracks)
    return result


def _dash(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        return {}
    try:
        root = ET.fromstring(data)
    except (ValueError, ET.ParseError):
        return {}
    if root.tag.rsplit('}', 1)[-1] != 'MPD':
        return {}
    representations = []
    pending, count = [(root, {}, 0)], 0
    while pending:
        node, inherited, depth = pending.pop()
        count += 1
        if count > MAX_ATOMS or depth > 32:
            return {}
        values = {**inherited, **node.attrib}
        if node.tag.rsplit('}', 1)[-1] == 'Representation':
            if len(representations) >= 128:
                return {}
            representations.append({'id': values.get('id', ''), 'width': values.get('width'),
                'height': values.get('height'), 'bandwidth': values.get('bandwidth'),
                'codecs': values.get('codecs', ''), 'frame_rate': values.get('frameRate'),
                'mime_type': values.get('mimeType', ''), 'content_type': values.get('contentType', '')})
        pending.extend((child, values, depth + 1) for child in reversed(list(node)))
    value = root.get('mediaPresentationDuration', '')
    match = re.fullmatch(r'P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?', value)
    duration = sum(float(item or 0) * factor for item, factor in zip(match.groups(), (86400, 3600, 60, 1))) if match else None
    return {'representations': representations, 'duration': _number(duration),
            'playlist': 'LIVE' if root.get('type') == 'dynamic' else 'VOD'}


def _representation_details(representations, dimensions, selected_id=None):
    if not isinstance(representations, list) or len(representations) > 128:
        return {}, None
    videos, audios = [], []
    for rep in representations:
        if not isinstance(rep, dict):
            continue
        codecs = _codecs(rep.get('codecs'))
        dims = _dimensions((rep.get('width'), rep.get('height')))
        mime = str(rep.get('mime_type') or rep.get('mimeType') or '')[:128].lower()
        content = rep.get('content_type') or rep.get('contentType')
        info = {'id': rep.get('id'), 'dimensions': dims, 'codecs': codecs, 'mime': mime,
                'bandwidth': _integer(rep.get('bandwidth')), 'fps': _fps(rep.get('frame_rate') or rep.get('frameRate'))}
        if 'video' in codecs or mime.startswith('video/') or content == 'video' or dims:
            videos.append(info)
        elif 'audio' in codecs or mime.startswith('audio/') or content == 'audio':
            audios.append(info)
    matching = [rep for rep in videos if rep['id'] == selected_id] if selected_id else [
        rep for rep in videos if dimensions and rep['dimensions'] == dimensions]
    video = matching[0] if len(matching) == 1 else videos[0] if len(videos) == 1 and not selected_id else None
    audio = audios[0] if len(audios) == 1 else None
    result, combined = {}, None
    if video:
        if video['dimensions']:
            result.update(width=video['dimensions'][0], height=video['dimensions'][1])
        result.update(videoCodec=video['codecs'].get('video'), fps=video['fps'], container=_MIMES.get(video['mime']))
        if 'audio' in video['codecs']:
            result.update(audioCodec=video['codecs']['audio'], has_audio=True)
            combined = video['bandwidth']
        else:
            result['videoBitrate'] = video['bandwidth']
    if audios:
        result['has_audio'] = True
    if audio:
        result.update(audioCodec=audio['codecs'].get('audio'), audioBitrate=audio['bandwidth'])
        if video and 'audio' not in video['codecs'] and video['bandwidth'] and audio['bandwidth']:
            combined = video['bandwidth'] + audio['bandwidth']
    return result, combined


def enrich_media_details(candidate, data=None, *, body_complete=False, content_length=None) -> None:
    """Enrich optional details only; existing request, scores and payload stay intact."""
    metadata = candidate.metadata
    if not isinstance(metadata, dict):
        return
    sources = metadata.get('detail_sources')
    if not isinstance(sources, dict):
        sources = {}
        metadata['detail_sources'] = sources

    def put(field, value, source):
        if value is None:
            return
        previous = metadata.get(field) if field in ('detail_duration', 'has_audio') else getattr(candidate, field, None)
        previous_source = sources.get(field)
        rank = _RANK.get(previous_source, 100) if isinstance(previous_source, str) else 100
        if previous is None:
            rank = -1
        if _RANK[source] < rank:
            return
        if field in ('detail_duration', 'has_audio'):
            metadata[field] = value
        else:
            setattr(candidate, field, value)
        sources[field] = source

    for field in _FIELDS:
        previous = getattr(candidate, field, None)
        if previous is None:
            continue
        if field in ('width', 'height'):
            valid = _integer(previous, 65535)
        elif field == 'fps':
            valid = _fps(previous)
        elif field in ('videoCodec', 'audioCodec'):
            valid = _codec(previous)
        elif field == 'container':
            valid = previous if isinstance(previous, str) and previous in set(_MIMES.values()) | {'ISO BMFF', 'WebM/Matroska'} else None
        else:
            valid = _integer(previous)
        if valid is None:
            setattr(candidate, field, None)
            sources.pop(field, None)

    captured = bytes(data) if isinstance(data, (bytes, bytearray, memoryview)) and len(data) <= MAX_CAPTURE_BYTES else b''
    prefix = captured.lstrip(b' \t\r\n\xef\xbb\xbf')
    protocol = candidate.media_type
    playlist = candidate.playlist_type
    manifest = {}
    if prefix.startswith(b'#EXTM3U'):
        protocol = 'HLS'
        try:
            text = captured.decode('utf-8-sig')
            lines = text.splitlines()
            if '#EXT-X-ENDLIST' in lines:
                playlist = 'VOD'
                duration = sum(_number(line.partition(':')[2].partition(',')[0]) or 0 for line in lines if line.startswith('#EXTINF:'))
                if not _number(candidate.duration):
                    put('detail_duration', _number(duration), 'hls-extinf')
            elif body_complete:
                playlist = 'LIVE'
            if any(line.startswith('#EXT-X-MEDIA:') and parse_attribute_list(line.partition(':')[2]).get('TYPE') == 'AUDIO' for line in lines):
                put('has_audio', True, 'hls-audio-group')
        except (UnicodeError, ValueError):
            pass
    elif prefix.startswith(b'<'):
        manifest = _dash(captured)
        if manifest:
            protocol, playlist = 'DASH', manifest['playlist']
            if not _number(candidate.duration):
                put('detail_duration', manifest.get('duration'), 'dash-duration')

    declared = 'declared_resolution' in metadata
    dimensions = _dimensions(metadata.get('declared_resolution') if declared else candidate.resolution)
    if dimensions:
        for field, value in zip(('width', 'height'), dimensions):
            put(field, value, 'declared-resolution' if declared else 'dom-resolution')
    for kind, value in _codecs(metadata.get('codecs')).items():
        put('videoCodec' if kind == 'video' else 'audioCodec', value, 'manifest-codecs')
        if kind == 'audio':
            put('has_audio', True, 'manifest-codecs')
    put('fps', _fps(metadata.get('frame_rate')), 'manifest-frame-rate')
    for field in ('videoCodec', 'audioCodec', 'videoBitrate', 'audioBitrate'):
        value = metadata.get(field, metadata.get(re.sub(r'([A-Z])', lambda match: '_' + match[1].lower(), field)))
        put(field, _codec(value) if field.endswith('Codec') else _integer(value), 'explicit-metadata')
    if protocol == 'HLS' and (metadata.get('audio_group') or metadata.get('hls_audio_group')):
        put('has_audio', True, 'hls-audio-group')

    combined = None
    if protocol == 'HLS':
        combined = _integer(metadata.get('declared_bandwidth') if 'declared_bandwidth' in metadata else candidate.bandwidth)
    elif protocol == 'DASH':
        representations = manifest.get('representations', metadata.get('representations'))
        values, combined = _representation_details(representations, dimensions, metadata.get('representation_id'))
        for field, value in values.items():
            put(field, value, 'dash-mime' if field == 'container' else 'dash-representation')

    if captured:
        values = _mp4(captured, body_complete)
        if 'has_video' in values:
            metadata['intrinsic_mp4'] = {key:values[key] for key in
                ('has_video','duration','width','height','videoCodec','audioCodec',
                 'video_track_count','audio_track_count','unknown_track_count') if key in values}
        for field in _FIELDS:
            if values.get(field) is not None:
                source = 'mp4-ftyp' if field == 'container' else values.get('dimensions_source', 'mp4-tkhd') if field in ('width', 'height') else 'mp4-stts' if field == 'fps' else 'mp4-btrt' if field.endswith('Bitrate') else 'mp4-stsd'
                put(field, values[field], source)
        if values.get('has_audio'):
            put('has_audio', True, 'mp4-soun')
        if not _number(candidate.duration):
            put('detail_duration', values.get('duration'), 'mp4-mdhd')
        combined = values.get('combined') or combined
        if not values.get('container'):
            container = 'WebM/Matroska' if captured.startswith(b'\x1aE\xdf\xa3') else 'Ogg' if captured.startswith(b'OggS') else 'FLV' if captured.startswith(b'FLV') else 'MPEG-TS' if len(captured) > 376 and captured[0] == captured[188] == captured[376] == 0x47 else None
            put('container', container, 'captured-magic')

    if protocol == 'Direct':
        mime = candidate.content_type.partition(';')[0].strip().lower() if isinstance(candidate.content_type, str) else ''
        put('container', _MIMES.get(mime), 'http-content-type')
    else:
        mime = metadata.get('segment_content_type')
        put('container', _MIMES.get(mime.lower()) if isinstance(mime, str) else None, 'segment-mime')

    headers = metadata.get('response_headers')
    headers = {key.lower(): value for key, value in headers.items() if isinstance(key, str)} if isinstance(headers, dict) else {}
    encoded = headers.get('content-encoding', '')
    encoded = bool(encoded and (not isinstance(encoded, str) or encoded.strip().lower() != 'identity'))
    exact, size_source = None, None
    http_resource = protocol == 'Direct' or protocol == 'Segment' and metadata.get('partial_resource')
    if http_resource and not encoded:
        if candidate.http_status == 206:
            try:
                interval = parse_content_range(headers.get('content-range'))
                raw_length = headers.get('content-length', content_length)
                length = _integer(raw_length)
                if (raw_length is None or length == interval.length) and (
                        not body_complete or not captured or len(captured) == interval.length):
                    exact, size_source = interval.total, 'http-content-range'
            except (ValueError, TypeError):
                pass
        elif candidate.http_status == 200:
            exact = _integer(headers.get('content-length', content_length))
            if body_complete and captured and exact != len(captured):
                exact = None
            size_source = 'http-content-length'
    if not http_resource and sources.get('estimatedSize') in ('http-content-length', 'http-content-range'):
        candidate.estimatedSize = None
        sources.pop('estimatedSize', None)
    if exact:
        put('estimatedSize', exact, size_source)
    elif playlist == 'LIVE':
        if metadata.get('size_is_estimate') or sources.get('estimatedSize') == 'vod-bandwidth-estimate':
            candidate.estimatedSize = None
            sources.pop('estimatedSize', None)
    elif playlist == 'VOD' or protocol == 'Direct':
        duration = _number(candidate.duration) or _number(metadata.get('detail_duration'))
        if combined and duration:
            estimate = _number(duration * combined / 8)
            if estimate:
                put('estimatedSize', int(round(estimate)) or None, 'vod-bandwidth-estimate')
    size_source = sources.get('estimatedSize')
    if size_source == 'vod-bandwidth-estimate':
        metadata['size_is_estimate'] = True
    elif size_source in ('http-content-length', 'http-content-range') or not candidate.estimatedSize:
        metadata['size_is_estimate'] = False
    else:
        # 已有候选可能携带上游估算值；没有精确长度证据时保留其估算语义。
        metadata['size_is_estimate'] = metadata.get('size_is_estimate') is not False
