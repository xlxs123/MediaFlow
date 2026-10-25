"""文件组织规则；只规划已确认的本地元数据，不参与媒体发现或传输。"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
from string import Formatter
from urllib.parse import parse_qs, urlsplit

from core.file_names import sanitize_filename, _units


CLASSIFICATIONS = ('none', 'type', 'site', 'date', 'type_site', 'custom')
FIELDS = ('title', 'site', 'date', 'time', 'resolution', 'quality', 'id', 'ext', 'media_type')
DEFAULTS = {'filename_template': '', 'classification': 'none', 'directory_template': ''}
_AUDIO = {'m4a', 'mp3', 'opus', 'ogg', 'wav', 'flac', 'aac'}
_SUBTITLE = {'srt', 'vtt', 'ass', 'ssa', 'ttml'}
_EXTENSION = re.compile(r'^[a-z0-9]{1,10}$')
_FALLBACKS = {'site': '未知网站', 'resolution': '未知分辨率', 'quality': '未知画质',
              'id': '未知ID', 'media_type': '其他'}


def normalize_options(value=None):
    value = value if isinstance(value, dict) else {}
    result = dict(DEFAULTS)
    for key in ('filename_template', 'directory_template'):
        text = value.get(key)
        if isinstance(text, str) and len(text) <= 512 and not any(ord(c) < 32 for c in text):
            result[key] = text.strip()
    if value.get('classification') in CLASSIFICATIONS:
        result['classification'] = value['classification']
    return result


def _timestamp(value=None):
    if isinstance(value, datetime):
        return value.astimezone() if value.tzinfo else value
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return result.astimezone() if result.tzinfo else result
        except ValueError:
            pass
    return datetime.now()


def site_name(site='', source_url=''):
    host = str(site or '').strip()
    if not host and source_url:
        try:
            host = urlsplit(source_url).hostname or ''
        except ValueError:
            pass
    if '://' in host:
        try:
            host = urlsplit(host).hostname or ''
        except ValueError:
            host = ''
    lower = host.lower().removeprefix('www.')
    for domain, name in (('bilibili.com', 'Bilibili'), ('douyin.com', 'Douyin'), ('rou.video', 'rou.video')):
        if lower == domain or lower.endswith('.' + domain):
            return name
    return host or '未知网站'


def make_values(title='', *, site='', source_url='', media_id='', resolution=None,
                quality='', media_type='', timestamp=None, extension='mp4'):
    moment = _timestamp(timestamp)
    ext = str(extension).lower().lstrip('.')
    if not _EXTENSION.fullmatch(ext):
        raise ValueError('实际输出扩展名无效。')
    kind = {'video': '视频', 'audio': '音频', 'subtitle': '字幕', 'other': '其他',
            '视频': '视频', '音频': '音频', '字幕': '字幕', '其他': '其他'}.get(str(media_type).lower())
    if not kind:
        kind = '音频' if ext in _AUDIO else '字幕' if ext in _SUBTITLE else '视频' if ext in {'mp4', 'mkv', 'webm', 'mov', 'avi', 'ts'} else '其他'
    height = resolution[1] if isinstance(resolution, (tuple, list)) and len(resolution) == 2 else resolution
    resolution_label = f'{height}p' if type(height) is int and 0 < height < 100000 else ''
    if isinstance(height, str) and re.fullmatch(r'\d{2,6}p', height):
        resolution_label = height
    identity = str(media_id or '').strip()
    if not identity and source_url:
        try:
            parsed = urlsplit(source_url)
            query = parse_qs(parsed.query)
            identity = next((query[key][0] for key in ('modal_id', 'bvid', 'aid', 'v') if query.get(key)), '')
            host = (parsed.hostname or '').lower()
            if not identity and (host == 'bilibili.com' or host.endswith('.bilibili.com')):
                match = re.search(r'/video/(BV[A-Za-z0-9]+)', parsed.path)
                identity = match[1] if match else ''
        except ValueError:
            pass
    if len(identity) > 100 or not re.fullmatch(r'[\w.\-]+', identity):
        identity = ''
    return {'title': str(title or '').strip() or 'media_' + moment.strftime('%Y%m%d_%H%M%S'),
            'site': site_name(site, source_url), 'date': moment.strftime('%Y%m%d'),
            'time': moment.strftime('%H%M%S'), 'resolution': resolution_label or '未知分辨率',
            'quality': str(quality or '').strip() or '未知画质', 'id': identity or '未知ID',
            'ext': ext, 'media_type': kind}


def values_for_entry(entry):
    values = make_values(entry.get('title') or Path(entry.get('file_name') or entry.get('path') or 'media').stem,
        site=entry.get('source_site', ''), source_url=entry.get('source_url', ''),
        media_id=entry.get('media_id', ''), resolution=entry.get('height', 0),
        media_type=entry.get('media_type', ''),
        timestamp=entry.get('finished_at') or entry.get('started_at'),
        extension=Path(entry.get('path') or 'media.bin').suffix.lstrip('.') or 'bin')
    if not (entry.get('finished_at') or entry.get('started_at')):
        values.update(date='未知日期', time='未知时间')
    return values


def _render(template, values):
    if not isinstance(template, str) or len(template) > 512:
        raise ValueError('模板最多支持 512 个字符。')
    result = []
    try:
        for literal, field, spec, conversion in Formatter().parse(template):
            result.append(literal)
            if field is not None:
                if not re.fullmatch(r'[A-Za-z_]+', field) or spec or conversion:
                    raise ValueError('模板仅支持 {title} 等字段，不支持属性、索引或格式表达式。')
                result.append(str(values.get(field) or _FALLBACKS.get(field) or '未知'))
    except (ValueError, KeyError) as error:
        raise ValueError('命名模板无效：' + str(error)) from None
    text = ''.join(result)
    text = re.sub(r'[_\-\s]{2,}', lambda m: '_' if '_' in m[0] else m[0][0], text)
    return text.strip(' _-.') or 'media'


def filename_limit(directory):
    # 给字幕语言、字幕ID以及冲突续号留出空间，完整输出路径控制在240 UTF-16单位内。
    budget = min(180, 240 - _units(str(Path(directory).expanduser().resolve())) - 1 - 64)
    if budget < 16:
        raise ValueError('输出目录过长，无法为媒体文件名和关联字幕保留空间，请选择较短目录。')
    return budget


def render_filename(template, values, extension, directory=None):
    ext = str(extension).lower().lstrip('.')
    if not _EXTENSION.fullmatch(ext):
        raise ValueError('实际输出扩展名无效。')
    data = dict(values, ext=ext)
    rendered = _render(template or '{title}.{ext}', data)
    if rendered.lower().endswith('.' + ext):
        rendered = rendered[:-len(ext) - 1]
    # 模板中的字面扩展名不能改变实际容器；扩展名只取自最终下载/封装计划。
    name = sanitize_filename(rendered + '.' + ext,
        max_length=filename_limit(directory) if directory else 180)
    name = re.sub(r'\s*_\s*', '_', name)
    name = re.sub(r'[_\-]{2,}', '_', name)
    return name if name.rsplit('.', 1)[0].strip(' _-.') else 'media.' + ext


def classification_parts(options, values):
    settings = normalize_options(options)
    mode = settings['classification']
    if mode == 'none':
        return []
    template = {'type': '{media_type}', 'site': '{site}', 'date': '{date}',
                'type_site': '{media_type}/{site}'}.get(mode, settings['directory_template'])
    if not template:
        raise ValueError('请填写自定义分类目录模板。')
    if template.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', template):
        raise ValueError('分类规则必须使用输出目录内的相对目录。')
    parts = re.split(r'[/\\]', template)
    if len(parts) > 4 or any(part.strip() in ('.', '..') for part in parts):
        raise ValueError('分类目录最多四层，不允许使用 . 或 ..。')
    result = []
    for part in parts:
        if not part.strip():
            continue
        # 元数据中即使含斜线，也只能成为一个安全目录组件。
        rendered = sanitize_filename(_render(part, values) + '.dir', max_length=64)[:-4].rstrip(' .')
        if rendered.casefold() in ('.stream-downloads', 'stream-downloads'):
            raise ValueError('不能把任务临时目录用作分类目录。')
        result.append(rendered)
    return result


def directory_for(root, options=None, values=None, collection_directory=''):
    root = Path(root).expanduser().resolve()
    base = Path(collection_directory).expanduser().resolve() if collection_directory else root
    if not base.is_relative_to(root):
        raise ValueError('合集目录必须位于指定的输出目录中。')
    target = base.joinpath(*classification_parts(options, values or {}))
    return contained_directory(root, target)


def contained_directory(root, directory):
    root = Path(root).expanduser().resolve()
    target = Path(directory).expanduser().resolve()
    if not target.is_relative_to(root) or any(part.casefold() == '.stream-downloads' for part in target.relative_to(root).parts):
        raise ValueError('最终目录越出了输出目录，或指向任务临时目录。')
    return target


def final_directory(request):
    return contained_directory(request.output_directory,
        getattr(request, 'final_output_directory', '') or request.output_directory)


def create_collection(root, name, batch_id):
    """仅在用户确认导入时调用；mkdir独占占名，绝不借用已有目录。"""
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    component = sanitize_filename(str(name or '').strip() + '.dir', max_length=64)[:-4]
    if not str(name or '').strip():
        return ''
    for number in range(10000):
        suffix = f' ({number})' if number else ''
        target = contained_directory(root, root / (component + suffix))
        filename_limit(target)
        try:
            target.mkdir()
        except FileExistsError:
            continue
        try:
            with (target / '.mediaflow-collection.json').open('x', encoding='utf-8') as handle:
                json.dump({'version': 1, 'batch_id': batch_id, 'name': target.name}, handle, ensure_ascii=False)
        except OSError:
            try:
                target.rmdir()
            except OSError:
                pass
            raise
        return str(target)
    raise ValueError('同名合集过多，请更换合集名称。')


def check_collection(root, directory, batch_id):
    target = contained_directory(root, directory)
    try:
        marker = target / '.mediaflow-collection.json'
        if marker.stat().st_size > 4096:
            raise ValueError('合集标记过大。')
        data = json.loads(marker.read_text(encoding='utf-8'))
        if data.get('version') != 1 or data.get('batch_id') != batch_id:
            raise ValueError('合集归属不匹配。')
    except (OSError, ValueError, AttributeError) as error:
        raise ValueError('无法确认合集目录归属，请检查目录是否被移动或替换：' + str(error)) from None
    return target
