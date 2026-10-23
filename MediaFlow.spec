# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys

root = Path(SPECPATH)
system_root = Path(os.environ.get('SystemRoot', 'C:\\Windows'))
# 保留现有 Qt DLL 收集修复，避免宿主图像工具中的同名库混入。
os.environ['PATH'] = os.pathsep.join([str(Path(sys.base_prefix)), str(Path(sys.executable).parent),
                                    str(system_root / 'System32'), str(system_root)])
a = Analysis(
    [str(root / 'main.py')], pathex=[str(root)], binaries=[],
    datas=[(str(root / 'assets' / 'app.ico'), 'assets'), (str(root / 'assets/build.json'), 'assets'),
           (str(root / 'assets/update-channel.json'), 'assets')],
    hiddenimports=['core.dash_json', 'core.dash_models', 'core.embedded_media'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6.QtMultimedia'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in {'ucrtbase.dll', 'icuuc.dll', 'icuin.dll'}
              and not Path(entry[0]).name.lower().startswith(('api-ms-win-crt-', 'icudt'))]
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name='MediaFlow',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    icon=str(root / 'assets' / 'app.ico'),
    version=str(root / 'assets' / 'version_info.txt'),
)
