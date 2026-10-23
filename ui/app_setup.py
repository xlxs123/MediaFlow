"""字体设置；后台渲染平台需显式注册 Windows 中文字体。"""
import os
from pathlib import Path
from PySide6.QtGui import QFontDatabase, QFont, QColor, QPalette
from utils.branding import APP_NAME, APP_VERSION


def configure_app(app):
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_NAME)
    app.setStyle('Fusion')
    # 窗口使用浅色样式，原生深色主题的控件底色不能混入浅色文字布局。
    palette = app.palette()
    for role, color in ((QPalette.ColorRole.Window, '#f4f6f9'),
                        (QPalette.ColorRole.WindowText, '#243347'),
                        (QPalette.ColorRole.Base, '#ffffff'),
                        (QPalette.ColorRole.AlternateBase, '#f4f6f9'),
                        (QPalette.ColorRole.Text, '#243347'),
                        (QPalette.ColorRole.Button, '#ffffff'),
                        (QPalette.ColorRole.ButtonText, '#243347'),
                        (QPalette.ColorRole.Light, '#ffffff'),
                        (QPalette.ColorRole.Midlight, '#eaf2fd'),
                        (QPalette.ColorRole.Mid, '#d7dee7'),
                        (QPalette.ColorRole.Dark, '#b0bac6'),
                        (QPalette.ColorRole.Shadow, '#9aa6b5'),
                        (QPalette.ColorRole.Highlight, '#2878d8'),
                        (QPalette.ColorRole.HighlightedText, '#ffffff'),
                        (QPalette.ColorRole.ToolTipBase, '#ffffff'),
                        (QPalette.ColorRole.ToolTipText, '#243347')):
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor('#9aa6b5'))
    app.setPalette(palette)
    if os.name == 'nt' and app.platformName() == 'offscreen':
        fonts = Path(os.environ.get('SystemRoot', 'C:\\Windows')) / 'Fonts'
        for filename in ('msyh.ttc', 'msyhbd.ttc', 'segoeui.ttf', 'consola.ttf'):
            if (fonts / filename).is_file():
                QFontDatabase.addApplicationFont(str(fonts / filename))
    app.setFont(QFont('Microsoft YaHei UI', 10))
