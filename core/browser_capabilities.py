"""当前浏览器的有限能力诊断；不请求媒体，不收集页面内容或凭据。"""
from __future__ import annotations

import json
import unicodedata


_FORMATS = {
    'h264_mp4': 'video/mp4; codecs="avc1.42E01E"',
    'aac_mp4': 'audio/mp4; codecs="mp4a.40.2"',
    'aac_adts': 'audio/aac',
    'h264_aac_mp4': 'video/mp4; codecs="avc1.42E01E,mp4a.40.2"',
    'vp9_webm': 'video/webm; codecs="vp9"',
    'opus_webm': 'audio/webm; codecs="opus"',
    'vp9_opus_webm': 'video/webm; codecs="vp9,opus"',
    'av1_mp4': 'video/mp4; codecs="av01.0.05M.08"',
    'av1_webm': 'video/webm; codecs="av01.0.05M.08"',
    'hls_native': 'application/vnd.apple.mpegurl',
    'dash_native': 'application/dash+xml',
}
_DECODE_FORMATS = ('h264_mp4', 'aac_mp4', 'vp9_opus_webm')
_STATUSES = ('ok', 'timeout', 'error', 'blocked', 'unavailable', 'unknown')


# CDP Runtime.evaluate: awaitPromise=True, returnByValue=True.
# All decodingInfo calls start together and each settles within one second.
CAPABILITY_SCRIPT = r'''(async () => {
    const formats = __FORMAT_MAP__;
    const read = (fn, fallback = null) => { try { return fn(); } catch (_) { return fallback; } };
    const flag = value => typeof value === 'boolean' ? value : null;
    const unknownDecode = status => ({status, supported:null, smooth:null, power_efficient:null});
    const randomKey = () => {
        const bytes = new Uint32Array(4);
        try {
            crypto.getRandomValues(bytes);
            return '__mediaflow_cap_' + Array.from(bytes, value => value.toString(16).padStart(8, '0')).join('');
        } catch (_) {
            return '__mediaflow_cap_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2)
                   + '_' + Math.random().toString(36).slice(2);
        }
    };
    const failure = error => error && (error.name === 'SecurityError' || error.name === 'NotAllowedError')
        ? 'blocked' : 'error';
    const storageProbe = name => {
        const result = {available:null, writable:null, cleanup:null, status:'unknown'};
        const key = randomKey();
        let storage;
        try {
            storage = window[name];
            result.available = storage !== undefined && storage !== null;
            if (!result.available) { result.status = 'unavailable'; return result; }
            storage.setItem(key, '1');
            result.writable = storage.getItem(key) === '1';
            result.status = result.writable ? 'ok' : 'blocked';
        } catch (error) { result.status = failure(error); }
        finally {
            if (storage) {
                try { storage.removeItem(key); result.cleanup = storage.getItem(key) === null; }
                catch (error) { result.cleanup = null; if (result.status === 'ok') result.status = failure(error); }
            }
        }
        return result;
    };
    const cookieProbe = () => {
        const result = {enabled:flag(read(() => navigator.cookieEnabled)), writable:null, cleanup:null, status:'unknown'};
        const key = randomKey();
        // Only compare our own random entry. No cookie names or values leave this function.
        const hasOwn = () => document.cookie.split(';').some(item => item.trim() === key + '=1');
        try {
            document.cookie = key + '=1; Path=/; SameSite=Lax';
            result.writable = hasOwn();
            result.status = result.writable ? 'ok' : 'blocked';
        } catch (error) { result.status = failure(error); }
        finally {
            try { document.cookie = key + '=; Max-Age=0; Path=/; SameSite=Lax'; result.cleanup = !hasOwn(); }
            catch (error) { result.cleanup = null; if (result.status === 'ok') result.status = failure(error); }
        }
        return result;
    };
    const video = read(() => document.createElement('video'));
    const mediaSource = read(() => window.MediaSource);
    const mediaCapabilities = read(() => navigator.mediaCapabilities);
    const report = {
        schema_version:1,
        video_tag:read(() => video ? video.tagName === 'VIDEO' && typeof video.play === 'function' : null),
        html_video_element:read(() => typeof window.HTMLVideoElement === 'function'),
        secure_context:flag(read(() => window.isSecureContext)),
        can_play_type:{},
        mse:{available:read(() => window.MediaSource !== undefined && window.MediaSource !== null), is_type_supported:{}},
        media_capabilities:{
            available:read(() => navigator.mediaCapabilities !== undefined && navigator.mediaCapabilities !== null),
            decoding_info_available:read(() => !!mediaCapabilities && typeof mediaCapabilities.decodingInfo === 'function'),
            decoding_info:{}
        },
        browser:{
            user_agent:read(() => navigator.userAgent), platform:read(() => navigator.platform), vendor:read(() => navigator.vendor),
            user_agent_data:{available:read(() => navigator.userAgentData !== undefined && navigator.userAgentData !== null),
                             mobile:null, platform:null, brands:[]}
        },
        cookie:cookieProbe(), local_storage:storageProbe('localStorage'), session_storage:storageProbe('sessionStorage'),
        video_elements:{count:null, states:[]}
    };
    for (const [name, mime] of Object.entries(formats)) {
        report.can_play_type[name] = read(() => video && typeof video.canPlayType === 'function' ? video.canPlayType(mime) : null);
        report.mse.is_type_supported[name] = flag(read(() => mediaSource && typeof mediaSource.isTypeSupported === 'function'
            ? mediaSource.isTypeSupported(mime) : null));
    }
    const hints = read(() => navigator.userAgentData);
    if (hints) {
        report.browser.user_agent_data.mobile = flag(read(() => hints.mobile));
        report.browser.user_agent_data.platform = read(() => hints.platform);
        const brands = read(() => hints.brands);
        if (Array.isArray(brands)) report.browser.user_agent_data.brands = brands.slice(0, 8).map(
            item => ({brand:read(() => item.brand), version:read(() => item.version)}));
    }
    try {
        const videos = document.querySelectorAll('video');
        report.video_elements.count = videos.length;
        report.video_elements.states = Array.prototype.slice.call(videos, 0, 8).map(element => ({
            ready_state:read(() => element.readyState), error_code:read(() => element.error === null ? 0 : element.error.code)
        }));
    } catch (_) {}
    const videoConfig = mime => ({contentType:mime, width:1280, height:720, bitrate:2000000, framerate:30});
    const audioConfig = mime => ({contentType:mime, channels:'2', bitrate:128000, samplerate:48000});
    const configurations = {
        h264_mp4:{type:'file', video:videoConfig(formats.h264_mp4)},
        aac_mp4:{type:'file', audio:audioConfig(formats.aac_mp4)},
        vp9_opus_webm:{type:'media-source', video:videoConfig(formats.vp9_webm), audio:audioConfig(formats.opus_webm)}
    };
    const decode = async config => {
        if (!report.media_capabilities.decoding_info_available) return unknownDecode('unavailable');
        let timer;
        try {
            return await Promise.race([
                Promise.resolve().then(() => mediaCapabilities.decodingInfo(config)).then(info => ({
                    status:'ok', supported:flag(info && info.supported), smooth:flag(info && info.smooth),
                    power_efficient:flag(info && info.powerEfficient)
                })),
                new Promise(resolve => { timer = setTimeout(() => resolve(unknownDecode('timeout')), 1000); })
            ]);
        } catch (_) { return unknownDecode('error'); }
        finally { clearTimeout(timer); }
    };
    await Promise.all(Object.entries(configurations).map(async ([name, config]) => {
        report.media_capabilities.decoding_info[name] = await decode(config);
    }));
    return report;
})()'''.replace('__FORMAT_MAP__', json.dumps(_FORMATS))


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _bool(value):
    return value if type(value) is bool else None


def _text(value, limit):
    if not isinstance(value, str):
        return None
    return ''.join(char for char in value[:limit * 2]
                   if unicodedata.category(char) not in ('Cc', 'Cf', 'Cs')).strip()[:limit]


def _status(value):
    return value if isinstance(value, str) and value in _STATUSES else 'unknown'


def _bounded_int(value, maximum):
    return value if type(value) is int and 0 <= value <= maximum else None


def normalize_capabilities(raw) -> dict:
    """只投影固定诊断字段；False 必须来自明确布尔值，缺失/异常保留未知。"""
    raw = _mapping(raw)
    play = _mapping(raw.get('can_play_type'))
    mse = _mapping(raw.get('mse'))
    supported = _mapping(mse.get('is_type_supported'))
    capabilities = _mapping(raw.get('media_capabilities'))
    decoding = _mapping(capabilities.get('decoding_info'))
    browser = _mapping(raw.get('browser'))
    hints = _mapping(browser.get('user_agent_data'))
    videos = _mapping(raw.get('video_elements'))
    report = {
        'schema_version': 1,
        'video_tag': _bool(raw.get('video_tag')),
        'html_video_element': _bool(raw.get('html_video_element')),
        'secure_context': _bool(raw.get('secure_context')),
        'can_play_type': {name: value if isinstance((value := play.get(name)), str) and
                          value in ('probably', 'maybe', '') else None for name in _FORMATS},
        'mse': {'available': _bool(mse.get('available')),
                'is_type_supported': {name: _bool(supported.get(name)) for name in _FORMATS}},
        'media_capabilities': {'available': _bool(capabilities.get('available')),
            'decoding_info_available': _bool(capabilities.get('decoding_info_available')), 'decoding_info': {}},
        'browser': {'user_agent': _text(browser.get('user_agent'), 512),
                    'platform': _text(browser.get('platform'), 128), 'vendor': _text(browser.get('vendor'), 128),
                    'user_agent_data': {'available': _bool(hints.get('available')), 'mobile': _bool(hints.get('mobile')),
                                        'platform': _text(hints.get('platform'), 128), 'brands': []}},
        'cookie': {'enabled': _bool(_mapping(raw.get('cookie')).get('enabled'))},
        'local_storage': {}, 'session_storage': {},
        'video_elements': {'count': _bounded_int(videos.get('count'), 1_000_000), 'states': []},
    }
    for name in _DECODE_FORMATS:
        value = _mapping(decoding.get(name))
        status = _status(value.get('status'))
        if status == 'blocked':
            status = 'error'
        report['media_capabilities']['decoding_info'][name] = {
            'status': status,
            **{key: _bool(value.get(key)) if status == 'ok' else None
               for key in ('supported', 'smooth', 'power_efficient')}}
    for name in ('cookie', 'local_storage', 'session_storage'):
        value = _mapping(raw.get(name))
        report[name].update(writable=_bool(value.get('writable')), cleanup=_bool(value.get('cleanup')),
                            status=_status(value.get('status')))
        if name != 'cookie':
            report[name]['available'] = _bool(value.get('available'))
    brands = hints.get('brands')
    if isinstance(brands, list):
        for value in brands[:8]:
            value = _mapping(value)
            brand = _text(value.get('brand'), 64)
            if brand:
                report['browser']['user_agent_data']['brands'].append(
                    {'brand': brand, 'version': _text(value.get('version'), 32)})
    states = videos.get('states')
    if isinstance(states, list):
        for value in states[:8]:
            value = _mapping(value)
            report['video_elements']['states'].append({
                'ready_state': _bounded_int(value.get('ready_state'), 4),
                'error_code': _bounded_int(value.get('error_code'), 4)})
    return report


def format_capabilities(report) -> str:
    """解释编码、协议、登录状态各自的能力边界，不把 UA 当作解码器。"""
    report = normalize_capabilities(report)
    yes = lambda value: '是' if value is True else '否' if value is False else '未知'
    playable = lambda value: {'probably': '支持（浏览器声明）', 'maybe': '可能支持', '': '不支持'}.get(value, '未知')
    browser, play, mse = report['browser'], report['can_play_type'], report['mse']
    hints = browser['user_agent_data']
    brands = '、'.join(value['brand'] + (' ' + value['version'] if value['version'] else '') for value in hints['brands'])
    labels = {'h264_mp4': 'H.264 MP4', 'aac_mp4': 'AAC / MP4', 'aac_adts': 'AAC / ADTS',
              'h264_aac_mp4': 'H.264 + AAC MP4', 'vp9_webm': 'VP9 WebM', 'opus_webm': 'Opus WebM',
              'vp9_opus_webm': 'VP9 + Opus WebM', 'av1_mp4': 'AV1 MP4', 'av1_webm': 'AV1 WebM'}
    lines = [f'HTML Video：{yes(report["video_tag"])}；HTMLVideoElement：{yes(report["html_video_element"])}',
             f'User-Agent：{browser["user_agent"] or "未知"}',
             f'平台：{browser["platform"] or "未知"}；Vendor：{browser["vendor"] or "未知"}',
             f'UA Client Hints：{yes(hints["available"])}；{brands or "品牌未知"}；'
             f'平台 {hints["platform"] or "未知"}；移动设备 {yes(hints["mobile"])}',
             f'安全上下文：{yes(report["secure_context"])}', '文件播放 canPlayType：']
    lines.extend(f'  {label}：{playable(play[name])}' for name, label in labels.items())
    lines.append(f'MSE 可用：{yes(mse["available"])}（仅字节流/编码能力）')
    lines.extend(f'  MSE {label}：{yes(mse["is_type_supported"][name])}' for name, label in labels.items())
    lines.extend((f'HLS 原生：{playable(play["hls_native"])}；DASH 原生：{playable(play["dash_native"])}',
                  'HLS / DASH 经 MSE 播放仍需页面播放器解析清单，并满足编码支持；MSE 可用不代表 HLS / DASH 一定可播放。'))
    capabilities = report['media_capabilities']
    lines.append(f'MediaCapabilities：{yes(capabilities["available"])}；decodingInfo：{yes(capabilities["decoding_info_available"])}')
    statuses = {'ok': '检测完成', 'timeout': '超时（未知）', 'error': '检测异常（未知）',
                'blocked': '访问受限', 'unavailable': '接口不可用', 'unknown': '未知'}
    for name, value in capabilities['decoding_info'].items():
        lines.append(f'  {labels[name]}：{statuses[value["status"]]}；支持 {yes(value["supported"])}；'
                     f'流畅 {yes(value["smooth"])}；节能 {yes(value["power_efficient"])}')
    cookie = report['cookie']
    lines.append(f'Cookie：浏览器开关 {yes(cookie["enabled"])}；随机项写读 {yes(cookie["writable"])}；'
                 f'已清理 {yes(cookie["cleanup"])}；{statuses[cookie["status"]]}')
    for name, label in (('local_storage', 'localStorage'), ('session_storage', 'sessionStorage')):
        value = report[name]
        lines.append(f'{label}：可用 {yes(value["available"])}；随机项写读 {yes(value["writable"])}；'
                     f'已清理 {yes(value["cleanup"])}；{statuses[value["status"]]}')
    videos = report['video_elements']
    lines.append(f'video 元素数量：{videos["count"] if videos["count"] is not None else "未知"}')
    errors = {0: '无错误', 1: '播放中止', 2: '网络错误', 3: '解码错误', 4: '源或格式不支持'}
    for index, value in enumerate(videos['states'], 1):
        lines.append(f'  video {index}：readyState {value["ready_state"] if value["ready_state"] is not None else "未知"}；'
                     f'{errors.get(value["error_code"], "错误状态未知")}')
    missing = []
    for name, label in (('h264_mp4', 'H.264'), ('aac_mp4', 'AAC')):
        if play[name] == '':
            lines.append(f'{label} 检测未支持：使用该编码的常见 MP4 无法在此浏览器可靠播放。')
            missing.append(label)
        if mse['available'] is True and mse['is_type_supported'][name] is False:
            lines.append(f'MSE 未支持 {label}：使用该编码的常见 MSE 播放路径可能无法工作。')
            missing.append(label)
    if missing:
        lines.append('建议：使用启用所需 H.264 / AAC 编码的 QtWebEngine 构建，或后续接入 WebView2。'
                     '修改 User-Agent 不会补充缺失的编解码器。')
    if cookie['enabled'] is False or cookie['writable'] is False:
        lines.append('Cookie 写读未通过可能影响登录或验证；它不能单独证明媒体解码失败。')
    return '\n'.join(lines)
