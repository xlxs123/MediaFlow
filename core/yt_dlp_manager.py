"""Compatibility entry points; tool_manager owns discovery and validation."""
from core.tool_manager import APP_DIR, ToolInfo, get_tool_manager


def detect_yt_dlp(preferred_path='', *, mode='custom', cancel_event=None):
    return get_tool_manager().detect('yt-dlp', preferred_path, mode=mode, cancel_event=cancel_event)


def detect_ffmpeg(preferred_path='', *, mode='custom', cancel_event=None):
    return get_tool_manager().detect('ffmpeg', preferred_path, mode=mode, cancel_event=cancel_event)


def detect_ffprobe(preferred_path='', *, mode='custom', cancel_event=None):
    return get_tool_manager().detect('ffprobe', preferred_path, mode=mode, cancel_event=cancel_event)
