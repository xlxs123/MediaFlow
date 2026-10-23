"""严格解析 HTTP 字节范围；响应范围不等同于完整资源。"""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import re

from core.errors import MediaCheckError


@dataclass(frozen=True)
class ContentRange:
    start: int
    end: int
    total: int

    @property
    def length(self):
        return self.end - self.start + 1

    @property
    def complete(self):
        return self.start == 0 and self.end == self.total - 1

    def to_dict(self):
        return {'start': self.start, 'end': self.end, 'total': self.total,
                'length': self.length, 'complete': self.complete}


def parse_content_range(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError('无效 Content-Range')
    match = re.fullmatch(r'bytes\s+(\d{1,20})-(\d{1,20})/(\d{1,20})', value.strip(), re.I)
    if not match:
        raise ValueError('无效 Content-Range')
    start, end, total = map(int, match.groups())
    if not 0 <= start <= end < total <= 2**63 - 1:
        raise ValueError('Content-Range 越界')
    return ContentRange(start, end, total)


def validate_direct_response(status, headers, request_range='', expected_total=None):
    """Direct 新下载从零开始，续传按下载器指定的区间读取。"""
    values = {str(k).lower(): str(v) for k, v in headers.items()}
    diagnostic = {'http_status': status, 'response_headers': values, 'request_range': request_range}

    def fail(message):
        raise MediaCheckError(message, 'INCOMPLETE_MEDIA_RESPONSE', diagnostic)

    try:
        length = int(values['content-length']) if 'content-length' in values else None
        if length is not None and length < 0:
            raise ValueError
    except (ValueError, TypeError):
        fail('Direct 响应长度无效。')
    if status == 206:
        try:
            interval = parse_content_range(values.get('content-range', ''))
        except ValueError:
            fail('206 媒体响应缺少有效 Content-Range，无法确认完整性。')
        diagnostic['content_range'] = interval.to_dict()
        if expected_total is not None and interval.total != expected_total:
            fail('媒体资源总长度与浏览器观测不一致，请重新查找媒体。')
        if length is not None and length != interval.length:
            fail('Content-Length 与 Content-Range 不一致。')
        match = re.fullmatch(r'bytes=(\d{1,20})-(\d{0,20})', request_range.strip()) if request_range else None
        if request_range and not match:
            fail('Direct 下载器提供了不支持的字节范围。')
        start = int(match[1]) if match else 0
        end = min(int(match[2]), interval.total - 1) if match and match[2] else interval.total - 1
        if (interval.start, interval.end) != (start, end):
            fail('服务器仅返回了不完整或错误的媒体区间，已停止，不能标记下载成功。')
        return interval.length
    if status != 200:
        fail('Direct 媒体响应不是 HTTP 200/206。')
    if expected_total is not None and length is not None and length != expected_total:
        fail('完整媒体响应长度与浏览器观测不一致，请重新查找媒体。')
    return length


def validate_resume_response(status, response_headers, request_range='', expected_headers=None):
    """在媒体字节发送前验证实际响应，绝不把忽略 Range 的 200 当作追加许可。

    expected_headers 来自已检查的媒体响应；探测响应若为 206，Content-Range
    中的 total 才是资源总长度，Content-Length 仅表示本次响应的字节数。
    """
    values = {str(key).lower(): str(value).strip() for key, value in (response_headers or {}).items()
              if value is not None}
    expected = {str(key).lower(): str(value).strip() for key, value in (expected_headers or {}).items()
                if value is not None}
    diagnostic = {'http_status': status, 'response_headers': values, 'request_range': request_range,
                  'stage': 'HTTP 续传校验', 'drm': None}

    def fail(message, code='INCOMPLETE_MEDIA_RESPONSE'):
        raise MediaCheckError(message, code, diagnostic)

    def length_of(headers):
        value = headers.get('content-length')
        if value is None:
            return None
        if not re.fullmatch(r'\d{1,20}', value) or int(value) > 2**63 - 1:
            fail('HTTP Content-Length 无效，已停止不安全的下载。')
        return int(value)

    length = length_of(values)
    expected_total = None
    if expected.get('content-range'):
        try:
            expected_total = parse_content_range(expected['content-range']).total
        except ValueError:
            unsatisfied = re.fullmatch(r'bytes\s+\*/(\d{1,20})', expected['content-range'], re.I)
            if not unsatisfied or int(unsatisfied[1]) > 2**63 - 1:
                fail('原媒体资源总长度无法验证，请重新分析媒体地址。', 'RESUME_MISMATCH')
            expected_total = int(unsatisfied[1])
    else:
        expected_total = length_of(expected)

    range_match = None
    if request_range:
        if not isinstance(request_range, str) or len(request_range) > 128:
            fail('下载器请求的 Range 无效。')
        range_match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', request_range.strip(), re.I)
        if not range_match or not any(range_match.groups()):
            fail('下载器请求的单一字节范围无效。')
        first, last = range_match.groups()
        if ((first and int(first) > 2**63 - 1) or (last and int(last) > 2**63 - 1)
                or (first and last and int(last) < int(first)) or (not first and int(last) == 0)):
            fail('下载器请求的字节范围越界。')

    strong_etag = expected.get('etag', '')
    if strong_etag.startswith('W/') or not re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', strong_etag):
        strong_etag = ''
    last_modified = expected.get('last-modified', '')
    if last_modified:
        try:
            modified_at = parsedate_to_datetime(last_modified)
            if modified_at.tzinfo is None:
                last_modified = ''
        except (ValueError, TypeError, OverflowError):
            last_modified = ''

    def check_identity(total, require_validator=False):
        if expected_total is not None and total is not None and total != expected_total:
            fail('媒体资源总长度已变化，不能向旧文件追加，请重新分析媒体地址。', 'RESUME_MISMATCH')
        if strong_etag:
            if values.get('etag') != strong_etag:
                fail('媒体强 ETag 已变化或丢失，不能向旧文件追加。', 'RESUME_MISMATCH')
            return True
        if last_modified:
            if values.get('last-modified') != last_modified:
                fail('媒体 Last-Modified 已变化或丢失，不能向旧文件追加。', 'RESUME_MISMATCH')
            return True
        if require_validator:
            fail('续传响应缺少可核对的强 ETag 或 Last-Modified，不能安全复用旧文件。', 'RESUME_MISMATCH')
        return False

    if status == 416:
        match = re.fullmatch(r'bytes\s+\*/(\d{1,20})', values.get('content-range', ''), re.I)
        if not match or int(match[1]) > 2**63 - 1:
            fail('416 响应缺少有效资源总长度，不能确认下载完成。')
        total = int(match[1])
        if not range_match or not range_match[1] or int(range_match[1]) != total:
            fail('416 响应与已有下载偏移不一致，已保留旧文件。', 'RESUME_MISMATCH')
        check_identity(total, require_validator=True)
        return {'mode': 'complete', 'expected_length': 0, 'total': total, 'offset': total}

    if status == 206:
        try:
            interval = parse_content_range(values.get('content-range', ''))
        except ValueError:
            fail('206 响应缺少有效 Content-Range，已停止不安全的追加。')
        diagnostic['content_range'] = interval.to_dict()
        if length is not None and length != interval.length:
            fail('Content-Length 与实际 Content-Range 不一致。')
        start, end = 0, interval.total - 1
        if range_match:
            if range_match[1]:
                start = int(range_match[1])
                if range_match[2]:
                    end = min(int(range_match[2]), interval.total - 1)
            else:
                start = max(0, interval.total - int(range_match[2]))
        if (interval.start, interval.end) != (start, end):
            fail('206 返回范围与请求范围不一致，已停止不安全的追加。')
        check_identity(interval.total, require_validator=start > 0)
        return {'mode': 'resume' if start > 0 else 'download', 'expected_length': interval.length,
                'total': interval.total, 'offset': interval.start}

    if status != 200:
        fail('媒体响应不是可验证的 HTTP 200、206 或 416。')
    if values.get('content-range'):
        fail('完整 HTTP 200 响应携带了冲突的 Content-Range，已停止。')
    # 本实现即使安全从头下载也先阻止资源身份变化，要求用户重新分析，
    # 以免 yt-dlp 使用旧文件的 track/layout 状态污染新资源。
    check_identity(length)
    return {'mode': 'restart' if range_match is not None else 'download',
            'expected_length': length if length is not None else expected_total,
            'total': length if length is not None else expected_total, 'offset': 0}
