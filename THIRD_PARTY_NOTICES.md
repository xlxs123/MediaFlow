# 第三方组件

本项目自主编写的 Python 源码适用随附 MIT LICENSE。外部组件保留各自许可。

- Microsoft Edge WebView2：Windows 默认浏览器组件，使用 SDK 1.0.4258.31 的 Core.dll / x64 WebView2Loader.dll 及独立 C# 宿主。SDK 按官方 NuGet 地址下载并校验 SHA256；许可与 NOTICE 位于 `tools/webview2/`。Evergreen Runtime 使用用户系统安装的 Microsoft Edge WebView2 Runtime，未随包复制 Edge。官方 SDK：https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4258.31 。集成细节见 `MEDIAFLOW_WEBVIEW2.md`。

- PySide6 / Qt：Qt for Python，适用其 LGPLv3 / GPLv3 / 商业许可选项。构建脚本复制已安装 PySide6 发行包的许可文件至发布目录的 `licenses`。项目：https://doc.qt.io/qtforpython/ 。本项目使用 QtCore、QtGui、QtWidgets、QtNetwork、QtWebSockets、QtWebEngineCore/Widgets。
- Qt WebEngine / Chromium：来自当前 PySide6 发行包，保持原有 sandbox/TLS/网页安全策略。与当前 Qt 6.11 系列匹配的官方浏览器组件许可资料随包附在 `licenses/QtWebEngine/qtwebengine-licensing.html` 及同目录第三方许可页，来源及 SHA256 在 `provenance.json`。Qt 官方许可说明：https://doc.qt.io/qt-6.11/qtwebengine-licensing.html 。Qt WebEngine 对应源码：https://code.qt.io/cgit/qt/qtwebengine.git/ ，Qt for Python 对应源码：https://code.qt.io/cgit/pyside/pyside-setup.git/ 。
- PyInstaller：GPLv2-or-later 带构建分发例外。仅用于打包。项目与许可：https://pyinstaller.org/en/stable/license.html 。
- `yt-dlp.exe`：外部子进程，官方 Windows standalone 发行包包含其他组件，组合发行包适用 GPLv3+；上游源码为 Unlicense。项目、对应发行源码与组件许可：https://github.com/yt-dlp/yt-dlp/releases 、https://github.com/yt-dlp/yt-dlp/blob/master/THIRD_PARTY_LICENSES.txt 。
- `ffmpeg.exe` / `ffprobe.exe`：2.1.0 固定为 BtbN 的 Windows x64 GPL 构建 n9.0.2-33-g4cf298e66a-20261022，原始 ZIP、实际 EXE 及配方均核对 SHA-256。GPL 文本在 `tools/FFmpeg-LICENSE`，实际配置在 `tools/FFmpeg-README.txt`。独立发布页提供对应的103份启用源码缓存、273份锁定的 rav1e crates、FFmpeg 精确源码与配方；真实构建日志的98份缓存全部覆盖。来源：https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-22-13-06 。源码资产、SHA-256、重建说明与明确限制见发布包 `licenses/THIRD_PARTY_SOURCES.md` 和 `licenses/third-party-sources.json`。

工具下载来源与 SHA256 存于 `tools/provenance.json`；复制后的工具仍保持原始二进制。程序通过独立进程调用这些工具，用户可将自己的兼容版本放入 `tools` 或通过界面选择 yt-dlp.exe。

Qt 6.11.2 官方完整源码及实际 PySide6 6.11.2、CPython、yt-dlp 和记录的精确依赖源码分发包随独立 Release 提供。完整应用源码与构建脚本允许使用修改后的兼容 Qt/PySide 重建并运行，不限制这种修改所需的调试和逆向工程；官方更新签名不用于阻止运行用户自建的应用。CPython 与 PyInstaller 的许可和分发例外分别位于 `licenses/Python`、`licenses/PyInstaller`。


## 2.0.0 签名依赖

Ed25519 使用 cryptography 50.0.2，依赖 cffi/pycparser。实际安装分发的许可证随完整发布目录 licenses 下对应 dist-info 一并提供；此依赖只安装于本版独立环境。API依据官方文档：https://cryptography.io/en/50.0.2/hazmat/primitives/asymmetric/ed25519/ 。
