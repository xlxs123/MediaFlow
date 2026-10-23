"""Progressive MP4 的部分响应证据；不拼接 Range，不替代实际下载校验。"""
from dataclasses import replace
import hashlib
import struct

from core.http_ranges import parse_content_range
from core.media_details import _mp4


def full_resource_context(context):
    return replace(context, headers={k:v for k,v in context.headers.items()
                                    if k.lower() not in ('range', 'if-range')}, inherit_range=False)


def range_proof(observation):
    data = observation.body or b''
    headers = {k.lower(): str(v) for k,v in observation.response_headers.items()}
    if observation.status != 206 or not observation.body_complete or data[4:8] != b'ftyp':
        return None
    try:
        interval = parse_content_range(headers.get('content-range'))
        if (interval.start != 0 or interval.length >= interval.total or len(data) != interval.length
                or int(headers.get('content-length', 0)) != interval.length
                or headers.get('content-encoding', 'identity').lower() not in ('', 'identity')):
            return None
    except (ValueError, TypeError):
        return None
    offset, moov, mdat = 0, False, False
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                return None
            size, header = struct.unpack_from('>Q', data, offset + 8)[0], 16
        if size < header or offset + size > interval.total or kind in (b'moof', b'styp', b'sidx'):
            return None
        if kind == b'mdat':
            # 只接受 moov 在前、最后一个已声明的非空 mdat 延伸至资源末尾。
            mdat = moov and size > header and offset + size == interval.total
            break
        if offset + size > len(data):
            return None
        if kind == b'moov':
            cursor, end = offset + header, offset + size
            while cursor + 8 <= end:
                child_size, child_kind = struct.unpack_from('>I4s', data, cursor)
                if child_kind == b'mvex' or child_size < 8 or cursor + child_size > end:
                    return None
                cursor += child_size
            if cursor != end:
                return None
            moov = True
        offset += size
    details = _mp4(data, True)
    if (not mdat or details.get('video_track_count') != 1 or details.get('unknown_track_count') != 0
            or not all(details.get(k) for k in ('duration', 'width', 'height', 'videoCodec', 'fps'))):
        return None
    count = min(8192, len(data))
    return {'total':interval.total, 'prefix_length':count,
            'prefix_hash':hashlib.sha256(data[:count]).hexdigest(),
            'etag':headers.get('etag', ''),
            **{k:details[k] for k in ('duration', 'width', 'height', 'videoCodec')}}


def resource_version(observation):
    """完整首段容器身份也绑定响应版本，供预加载轨道的否定证据使用。"""
    data=observation.body or b''
    if not observation.body_complete or data[4:8]!=b'ftyp':
        return None
    headers={k.lower():str(v) for k,v in observation.response_headers.items()}
    try:
        if observation.status==206:
            interval=parse_content_range(headers.get('content-range'))
            if interval.start!=0 or interval.length!=len(data):return None
            total=interval.total
        elif observation.status==200:
            total=int(headers.get('content-length',0))
            if total!=len(data):return None
        else:return None
    except (ValueError,TypeError):return None
    size=min(8192,len(data))
    return {'total':total,'etag':headers.get('etag',''),'prefix_length':size,
            'prefix_hash':hashlib.sha256(data[:size]).hexdigest()}


def verified_response(proof, root, expected_url):
    if (not proof or root is None or root.context.final_url != expected_url
            or root.info.media_type != 'Direct' or root.info.drm is not False):
        return False
    headers = {k.lower():str(v) for k,v in root.response_headers.items()}
    if headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
        return False
    try:
        full = root.http_status == 200 and not headers.get('content-range')
        if root.http_status == 206:
            interval = parse_content_range(headers.get('content-range'))
            full = interval.start == 0 and interval.length == interval.total == proof['total']
        if not full or int(headers.get('content-length', 0)) != proof['total']:
            return False
    except (ValueError, TypeError):
        return False
    if proof.get('etag') and headers.get('etag') != proof['etag']:
        return False
    size = proof['prefix_length']
    return len(root.data) >= size and hashlib.sha256(root.data[:size]).hexdigest() == proof['prefix_hash']


def same_version(proof, observation):
    """同 URL 的长度/验证器/头部变化必须撤销旧证明，包括尚在途的探测。"""
    headers = {k.lower():str(v) for k,v in observation.response_headers.items()}
    try:
        if observation.status == 206:
            if parse_content_range(headers.get('content-range')).total != proof['total']:
                return False
        elif observation.status == 200 and headers.get('content-length'):
            if int(headers['content-length']) != proof['total']:
                return False
    except (ValueError, TypeError):
        return False
    if headers.get('etag') and headers['etag'] != proof.get('etag'):
        return False
    data = observation.body or b''
    size = proof['prefix_length']
    if data[4:8] == b'ftyp' and len(data) >= size:
        return hashlib.sha256(data[:size]).hexdigest() == proof['prefix_hash']
    return True
