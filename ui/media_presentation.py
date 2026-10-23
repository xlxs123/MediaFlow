"""候选展示的文字格式；只读取证据，不重新评分或改变下载资格。"""
import math


def display_text(value, fallback='未知'):
    if value is None:
        return fallback
    text = str(value).strip()
    return fallback if text.lower() in ('', 'none', 'null', 'unknown', 'undefined', 'n/a', '--') else text


def _positive(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def duration_text(value):
    """按已播放整秒显示；未知、无穷和异常数值不伪造为零秒。"""
    number = _positive(value)
    if number is None:
        return '未知'
    seconds = int(number)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f'{hours}小时')
    if minutes:
        parts.append(f'{minutes}分')
    if seconds or not parts:
        parts.append(f'{seconds}秒')
    return ''.join(parts)


def candidate_state_text(candidate):
    validation = {'VERIFIED': '已验证', 'NEEDS_CONFIRMATION': '需确认', 'UNAVAILABLE': '不可下载'}
    return ('已发现 · ' + validation.get(candidate.validation_state, '未检测到')
            + ' · ' + ('可下载' if candidate.downloadable else '不可下载'))


def confidence_status(score):
    """仅决定展示分区；实际下载资格另由候选验证状态决定。"""
    return '推荐' if type(score) in (int, float) and score >= 80 else (
        '需确认' if type(score) in (int, float) and score >= 40 else '低可信度')


def confidence_diagnostics(candidate):
    metadata = candidate.metadata
    evidence = metadata.get('confidence_evidence') or {}
    verified = evidence.get('resolver_entry') is True or metadata.get('resolver_verified') is True
    current = evidence.get('current_player')
    if 'current_player' not in evidence:
        if 'dom_current' in metadata or 'dom_main' in metadata:
            current = metadata.get('dom_current') is True and metadata.get('dom_main') is True
        else:
            # 子清单的播放器关联可能记录在既有评分明细中，而非其本地 DOM 字段。
            current = True if any(isinstance(item, dict) and item.get('points') == 40 and
                'video.currentSrc' in str(item.get('reason', ''))
                for item in metadata.get('confidence_details', [])) else None
    reasons = []
    if current is False and not verified:
        reasons.append('非当前播放器')
    elif current is not True and not verified:
        reasons.append('未确认与当前播放器的关联')
    if not verified and current is not True and not (evidence.get('associated') is True or metadata.get('dom_associated') is True):
        reasons.append('缺少页面上下文（未关联播放器）')
    if not candidate.context_complete:
        reasons.append('缺少可复现的请求上下文')
    missing = []
    if not (_positive(candidate.duration) or _positive(metadata.get('detail_duration'))):
        missing.append('时长')
    width = candidate.width or candidate.resolution[0]
    height = candidate.height or candidate.resolution[1]
    if not (_positive(width) and _positive(height)):
        missing.append('分辨率')
    if display_text(candidate.videoCodec) == '未知' and display_text(candidate.audioCodec) == '未知':
        missing.append('编码')
    if missing:
        reasons.append('未检测到完整媒体信息：' + '、'.join(missing))
    # 只展示已有明确证据，不拿未绑定播放器的时长进行猜测。
    if evidence.get('duration_mismatch') is True or metadata.get('duration_mismatch') is True:
        reasons.append('时长不匹配')
    return reasons


def reason_text(value):
    text = display_text(value, '未检测到评分依据')
    if text.startswith('时长有效'):
        return '时长检测通过'
    return text


def confidence_reasons(candidate):
    reasons = ['⚠ ' + reason for reason in confidence_diagnostics(candidate)]
    details = candidate.metadata.get('confidence_details') or []
    if details:
        reasons.extend(('✓ ' if item.get('points', 0) > 0 else '⚠ ') + reason_text(item.get('reason'))
                       for item in details if isinstance(item, dict))
    else:
        reasons.extend('✓ ' + reason_text(reason) for reason in candidate.confidenceReasons)
    return list(dict.fromkeys(reasons)) or ['未检测到评分依据']


def encryption_text(value):
    return '无' if value in ('None', 'NONE') else display_text(value)


def diagnostic_text(text):
    """将候选检测详情中的字段值本地化，保留 URL、协议名和技术证据。"""
    lines = []
    for line in text.splitlines():
        name, separator, value = line.partition('：')
        if separator:
            if name == '时长（秒）':
                try:
                    value = duration_text(float(value))
                except (TypeError, ValueError, OverflowError):
                    value = '未知'
                name = '时长'
            elif name in ('Encryption', '加密'):
                value = encryption_text(value)
            elif name == 'METHOD' and value.strip() == 'NONE':
                # HLS METHOD=NONE 是协议值，不能当成 Python 的未知占位值。
                value = 'NONE'
            else:
                value = display_text(value)
            line = name + separator + value
        lines.append(line)
    return '\n'.join(lines)
