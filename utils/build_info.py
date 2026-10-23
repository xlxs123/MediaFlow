"""运行身份来自 EXE 内嵌的构建信息，不读取旁边旧版本的 Python 文件。"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib
import json
from pathlib import Path
import sys

from utils.branding import APP_VERSION


DASH_FEATURE_REVISION = 2
TRACK_FEATURE_REVISION = 1
ORGANIZATION_FEATURE_REVISION = 1
STORAGE_FEATURE_REVISION = 1
MIGRATION_FEATURE_REVISION = 1
UPDATE_FEATURE_REVISION = 4


def source_digest(root):
    root = Path(root)
    paths = [root / 'main.py', root / 'MediaFlow.spec', root / 'native/webview2/Host.cs',
             root / 'native/recycle/Recycle.cs', root / 'assets/update-channel.json']
    paths += [p for name in ('core', 'ui', 'utils') for p in (root / name).glob('*.py')]
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def runtime_identity():
    packaged = bool(getattr(sys, 'frozen', False))
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    stamp = {}
    if packaged:
        try:
            stamp = json.loads((root / 'assets/build.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    digest = stamp.get('source_sha256') or (source_digest(root) if not packaged else '')
    modules = {}
    for name, function in (('core.dash_json', 'parse_dash_description'),
                           ('core.embedded_media', 'embedded_dash_json')):
        try:
            module = importlib.import_module(name)
            modules[name] = callable(getattr(module, function, None))
        except (ImportError, OSError):
            modules[name] = False
    tracks = {}
    for name, function in (('core.media_tracks', 'apply_selection'), ('core.track_pipeline', 'process_outputs')):
        try:
            tracks[name] = callable(getattr(importlib.import_module(name), function, None))
        except (ImportError, OSError):
            tracks[name] = False
    organization = {}
    for name, function in (('core.output_organization', 'render_filename'), ('core.output_relocation', 'apply_completed_paths')):
        try:
            organization[name] = callable(getattr(importlib.import_module(name), function, None))
        except (ImportError, OSError):
            organization[name] = False
    storage = {}
    for name, function in (('core.media_storage_scan', 'StorageScanner'), ('core.storage_cleanup', 'StorageCleanup'),
                           ('core.windows_recycle', 'recycle_file')):
        try:
            storage[name] = callable(getattr(importlib.import_module(name), function, None))
        except (ImportError, OSError):
            storage[name] = False
    migration = {}
    for name, function in (('core.data_migration', 'MigrationEngine'),
                           ('core.migration_tasks', 'inspect_tasks'), ('core.migration_browser', 'copy_profile')):
        try:
            migration[name] = callable(getattr(importlib.import_module(name), function, None))
        except (ImportError, OSError):
            migration[name] = False
    update = {}
    for name, function in (('core.update_manager', 'UpdateManager'),
                           ('core.update_manifest', 'validate_manifest'), ('core.update_transport', 'fetch'), ('core.update_health', 'health_report')):
        try:
            update[name] = callable(getattr(importlib.import_module(name), function, None))
        except (ImportError, OSError):
            update[name] = False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        update['ed25519'] = callable(Ed25519PublicKey.from_public_bytes)
    except ImportError:
        update['ed25519'] = False
    try:
        from core.update_channel import load_channel, key_fingerprint
        channel = load_channel()
        channel_state = channel['key_state']
        channel_identity = {'key_state': channel_state,
            'source_sha256': hashlib.sha256(channel['source_url'].encode('utf-8')).hexdigest(),
            'key_fingerprints': {name:key_fingerprint(key) for name,key in channel['trusted_keys'].items()}}
        update['trusted_channel'] = True
    except (ImportError, ValueError, OSError, RuntimeError):
        channel_state = 'invalid'
        channel_identity = {'key_state': 'invalid', 'source_sha256': '', 'key_fingerprints': {}}
        update['trusted_channel'] = False
    return {'app_version': APP_VERSION, 'build_id': stamp.get('build_id') or f'{APP_VERSION}-source-{digest[:12]}',
            'source_sha256': digest, 'built_at_utc': stamp.get('built_at_utc', ''),
            'packaged': packaged, 'executable': str(Path(sys.executable).resolve()),
            'runtime_root': str(root), 'dash_feature_revision': DASH_FEATURE_REVISION,
            'dash_modules': modules, 'dash_module_loaded': all(modules.values()),
            'track_feature_revision': TRACK_FEATURE_REVISION, 'track_modules': tracks,
            'track_module_loaded': all(tracks.values()),
            'organization_feature_revision': ORGANIZATION_FEATURE_REVISION,
            'organization_modules': organization, 'organization_module_loaded': all(organization.values()),
            'storage_feature_revision': STORAGE_FEATURE_REVISION,
            'storage_modules': storage, 'storage_module_loaded': all(storage.values()),
            'migration_feature_revision': MIGRATION_FEATURE_REVISION,
            'migration_modules': migration, 'migration_module_loaded': all(migration.values()),
            'update_feature_revision': UPDATE_FEATURE_REVISION, 'release_channel_state': channel_state,
            'update_channel': channel_identity,
            'update_modules': update, 'update_module_loaded': all(update.values())}


def runtime_summary():
    identity = runtime_identity()
    return (f'v{APP_VERSION} · {identity["build_id"]} · DASH 解析模块：'
            + ('已加载（JSON + 页面内嵌）' if identity['dash_module_loaded'] else '加载失败'))
