# 第三方对应源码与重建

MediaFlow 自编源码继续保留 MIT。外部组件使用各自许可；不能将整个 Windows ZIP 标为只适用 MIT。许可证位于完整发布目录 `licenses` 和 `tools`。

本版完整源码入口在 [MediaFlow-Releases v2.1.0](https://github.com/xlxs123/MediaFlow-Releases/releases/tag/v2.1.0)。各独立源码资产的真实大小、SHA-256、地址和说明在同目录 `third-party-sources.json`。BtbN 两份 ZIP 都可独立解压，不是需要拼接的字节分卷。

## FFmpeg / ffprobe

工具固定为 BtbN 的 n9.0.2-33-g4cf298e66a-20261022（Windows x64 GPL）。已核对同一上游 workflow run 39776938430、配方 commit 937868360a21d711ec94a33f5591d345ad9b7754、二进制压缩包及实际工具 SHA-256。

源码包包含 103 份被启用的原始 cache 档案、273 份逐一对照 Cargo.lock 校验的 rav1e crates、FFmpeg 精确源码与配方。真实构建日志使用的98份 cache 全部覆盖，额外5份 curl 子依赖保守保留。全部源文件在两个独立 ZIP 中有逐项清单与 SHA-256。先解压两个 ZIP 至相同的新目录，按其中 README-BtbN-SOURCE.txt 与原配方配置工具链和源码；保留各源项目许可证。

上游 cached 层没有记录 cargo update cc 的历史构建辅助版本，本项目未重建通用工具链，也不承诺逐字节复现上游二进制。提供真实源码、配方及明确限制，不把 notices 或单一 FFmpeg 源码链接当作已取得全部组合源码。

## Qt / PySide6 与应用重建

随 Release 提供 Qt 6.11.2 官方完整 source tar.xz；Python-and-yt-dlp 源码资产含 PySide6 6.11.2 官方源码、CPython 3.14.0/3.10.11、yt-dlp 2026.08.19 及记录的精确依赖源码分发包。

应用仓库提供完整 Python/C# 源码、requirements.txt、MediaFlow.spec 与 build.ps1。用户可以从源码安装自己修改后的兼容 PySide6/Qt，再使用正常构建脚本生成并运行修改后的应用；应用不要求其运行 EXE 通过官方发布签名，不限制为调试库修改而进行的逆向工程。正式更新验签只保护官方更新渠道，不阻止运行用户重建的应用。

Qt 库适用各自 LGPL/GPL/商业条款，完整许可证及 QtWebEngine/Chromium 第三方说明随运行包保留。没有给 Qt/PySide6 增加本项目补丁，不以此承诺跨工具链二进制完全一致。

## yt-dlp 和其他依赖

yt-dlp.exe 固定官方 2026.08.19，SHA-256 与官方发行资产相符。源资产提供其官方源码、mutagen 等实际组件的对应源分发包；各项目分别保留许可证。独立子进程方式调用工具，用户可按现有工具管理界面更换兼容版本。

WebView2 SDK 使用官方允许分发的构件及其 LICENSE/NOTICE，不随程序复制 Edge Runtime；本项目 C# 宿主源码公开。CPython 与 PyInstaller 许可及分发例外在 licenses/Python 和 licenses/PyInstaller。cryptography、cffi、pycparser 的实际分发许可同样保留。
