"""为本应用的 Chromium 页面准备仅监听回环地址的调试接口。"""
from __future__ import annotations

import os
import re
import shlex
import socket
import threading


_port: int | None = None
_lock = threading.Lock()


def prepare_browser_runtime() -> int:
    """必须在 QApplication / WebEngine 初始化前调用；不连接普通浏览器。"""
    global _port
    with _lock:
        if _port is not None:
            return _port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        # Qt 未显式设置此参数时会自动使用 '*'。仅允许本进程自己的 WS Origin。
        flags = shlex.split(os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', ''), posix=True)
        flags = [flag for flag in flags if not flag.startswith(
            ('--remote-debugging-port', '--remote-debugging-address', '--remote-allow-origins'))]
        flags.append(f'--remote-allow-origins=http://127.0.0.1:{port}')
        os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = shlex.join(flags)
        os.environ['QTWEBENGINE_REMOTE_DEBUGGING'] = f'127.0.0.1:{port}'
        _port = port
        return port


def browser_runtime_port() -> int | None:
    """只返回由本应用分配的端口，不接受用户浏览器的调试配置。"""
    return _port


def chrome_compatible_user_agent(value: str) -> str:
    """保留真实 Chromium 版本与平台，仅移除嵌入宿主的识别标记。"""
    return re.sub(r'(?:^|\s+)QtWebEngine/[^\s]+', '', value).strip()


def configure_browser_profile(profile):
    from PySide6.QtWebEngineCore import QWebEngineSettings
    profile.setHttpUserAgent(chrome_compatible_user_agent(profile.httpUserAgent()))
    settings = profile.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
    # 使用 Qt 官方提供的桌面 Chrome 播放策略，不关闭 TLS、CORS 或 sandbox。
    settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)


def browser_engine_info(profile=None):
    if getattr(profile, 'backend', '') == 'webview2':
        from PySide6 import __version__
        session = ('本地持久 BrowserProfile；Cookie / 网站存储保留；不上传或同步。'
                   if profile.persistent else 'WebView2 InPrivate；独立临时用户数据目录；关闭会话后清理。')
        return dict(profile.info, pyside=__version__, qt_webengine='', session=session)
    from PySide6 import __version__
    from PySide6.QtWebEngineCore import qWebEngineChromiumVersion, qWebEngineVersion
    return {'engine': 'Qt WebEngine', 'pyside': __version__, 'qt_webengine': qWebEngineVersion(),
            'chromium': qWebEngineChromiumVersion(),
            'session': '内存 Profile；Cookie / 缓存不持久化；页面存储受浏览器同源规则约束。'}
