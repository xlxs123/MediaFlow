"""AppData 中只保存界面偏好；不保存 URL、cURL 或任意请求头。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from utils.branding import APP_NAME, APP_VERSION

DEFAULTS = {
    'output_directory': 'E:\\download',
    'yt_dlp_path': '',
    'ffmpeg_path': '',
    'ffprobe_path': '',
    'yt_dlp_mode': 'auto',
    'ffmpeg_mode': 'auto',
    'ffprobe_mode': 'auto',
    'user_agent': '',
    'window_width': 1050,
    'window_height': 860,
    'advanced_expanded': False,
    'max_concurrent_videos': 3,
    'concurrent_fragments': 4,
    'segment_max_attempts': 3,
    'max_concurrent_requests': 12,
    'quality_preference': 'auto',
    'browser_body_limit_mib': 16,
    'batch_import': {},
    'organization': {'filename_template': '', 'classification': 'none', 'directory_template': ''},
}


class ConfigStore:
    def __init__(self, path: str | Path | None = None):
        appdata = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        # 独立数据空间：稳定版的历史、恢复快照及登录配置不被升级写入。
        self.data_version = '' if path else APP_VERSION
        self.path = Path(path) if path else appdata / APP_NAME / self.data_version / 'config.json'
        self.legacy_path = None if path else appdata / 'M3U8 Downloader' / 'config.json'
        # 1.8 只在用户预览、确认之后迁移；启动读取不再静默复制旧配置。
        self.legacy_paths = ()
        self.migrated_from = None
        self.error = ''

    def _safe(self, values: dict) -> dict:
        result = DEFAULTS.copy()
        for key in ('output_directory', 'yt_dlp_path', 'ffmpeg_path', 'ffprobe_path', 'user_agent'):
            value = values.get(key)
            if isinstance(value, str) and len(value) <= 8192 and not any(ord(c) < 32 for c in value):
                result[key] = value
        for name in ('yt_dlp', 'ffmpeg', 'ffprobe'):
            if values.get(name + '_mode') in ('auto', 'custom'):
                result[name + '_mode'] = values[name + '_mode']
        for key in ('window_width', 'window_height'):
            value = values.get(key)
            if type(value) is int and 500 <= value <= 4000:
                result[key] = value
        if isinstance(values.get('advanced_expanded'), bool):
            result['advanced_expanded'] = values['advanced_expanded']
        if values.get('quality_preference') in ('auto', 'highest', 'balanced', 'smallest'):
            result['quality_preference'] = values['quality_preference']
        from core.output_organization import normalize_options
        result['organization'] = normalize_options(values.get('organization'))
        for key, maximum in (('max_concurrent_videos', 10), ('concurrent_fragments', 16), ('segment_max_attempts', 10), ('max_concurrent_requests', 64)):
            value = values.get(key)
            if type(value) is int and 1 <= value <= maximum:
                result[key] = value
        body_limit = values.get('browser_body_limit_mib')
        if type(body_limit) is int and 1 <= body_limit <= 128:
            result['browser_body_limit_mib'] = body_limit
        batch = values.get('batch_import')
        if isinstance(batch, dict):
            safe_batch = {}
            for key, allowed in (('naming', ('title', 'index_title', 'date_title')),
                                 ('quality', ('auto', 'highest', 'balanced', 'smallest'))):
                if batch.get(key) in allowed:
                    safe_batch[key] = batch[key]
            for key, minimum, maximum in (('max_concurrent', 1, 10), ('retry_limit', 0, 3),
                                           ('discovery_seconds', 5, 120)):
                if type(batch.get(key)) is int and minimum <= batch[key] <= maximum:
                    safe_batch[key] = batch[key]
            result['batch_import'] = safe_batch
            if batch.get('organization'):
                safe_batch['organization'] = normalize_options(batch['organization'])
            if batch.get('track_preferences'):
                from core.media_tracks import normalize_preferences
                safe_batch['track_preferences'] = normalize_preferences(batch['track_preferences'])
        return result

    def load(self) -> dict:
        self.error = ''
        source = self.path
        if not source.exists():
            source = next((legacy for legacy in self.legacy_paths if legacy.is_file()), source)
        if not source.exists():
            return DEFAULTS.copy()
        try:
            if source.stat().st_size > 65536:
                raise ValueError('配置文件过大')
            data = json.loads(source.read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('配置格式无效')
            values = self._safe(data)
            if source != self.path:
                # 复制安全的偏好到新目录，旧文件始终保留；写入失败仍使用已读到的偏好。
                if self.save(values):
                    self.migrated_from = source
            return values
        except (OSError, ValueError) as error:
            self.error = f'无法读取设置，已使用默认值：{error}'
            return DEFAULTS.copy()

    def save(self, values: dict) -> bool:
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='settings-', suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(self._safe(values), handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
            return True
        except OSError as error:
            self.error = f'无法保存设置：{error}'
            return False
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)
