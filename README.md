# MediaFlow

MediaFlow 2.1.0 是 Windows 桌面媒体下载与本地媒体管理工具，使用 Python、PySide6 和 WebView2。

- [公开源码](https://github.com/xlxs123/MediaFlow)
- [独立发布仓库](https://github.com/xlxs123/MediaFlow-Releases)
- [发布版本](https://github.com/xlxs123/MediaFlow-Releases/releases)
- [2.1.0 正式发布与实际公网验收](docs/RELEASE_VALIDATION.md)

## 使用

从发布页面选择已经发布并验收的 Windows 版本，解压完整文件夹后运行 `MediaFlow.exe`。移动程序时保留 `tools` 和 `licenses`。主窗口顶部“更新与版本管理”提供在线检查、签名验证、独立部署、启动与旧版回退；“数据迁移”独立处理兼容的历史数据。

支持 Direct HTTP、HLS、DASH、Bilibili 音视频合并、浏览器媒体发现、批量链接导入与合集、断点续传、音轨和字幕选择、命名分类、媒体库历史、完整 SHA-256 去重与空间分析。网站可用性取决于其实际媒体结构和合法会话；不支持 DRM 绕过。

应用数据使用 `%APPDATA%/MediaFlow/2.1.0`，更新状态使用 `%LOCALAPPDATA%/MediaFlow/Updates-v2`。更新不自动迁移登录状态、删除旧程序或覆盖现有媒体。首次升级请在“数据迁移”中预览兼容项目。

旧 2.0.1 尚不包含 GitHub Release CDN 适配，首次采用正式渠道先解压并运行 2.1.0；无需修改或覆盖旧客户端。

## 从源码构建

Windows x64、Python 3.14、PowerShell 7.2 或以上：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B tests/run_update_tests.py --report-dir verification/update-tests
pwsh -NoProfile -File build.ps1 -SkipTests
```

`build.ps1` 下载并校验外部工具、构建原生 WebView2 宿主与回收站辅助程序，输出新的 `dist-mediaflow-2.1.0`；非空发布目录会被拒绝。构建门禁和 CI 详见 [CI.md](release-workflows/CI.md)。上述测试不等于公网或实际 EXE 验收，后者单独记录。

## 更新与发布

客户端固定的公开渠道及正式公钥位于 `assets/update-channel.json`。签名协议见 [MEDIAFLOW_UPDATE.md](MEDIAFLOW_UPDATE.md)，发布者操作和密钥保护见 [PRODUCTION_RELEASE.md](PRODUCTION_RELEASE.md)。客户端接受已验证签名的稳定清单后，校验完整包大小、SHA-256 与 ZIP 结构，再复用独立版本部署与健康检查。下载、部署、启动是分别报告的阶段。

普通 GitHub Actions 只测试和构建，不持有正式私钥，不发布稳定版本。正式签名在发布者受保护的本地环境完成。

## 文档和许可证

- [工具自动发现](MEDIAFLOW_TOOLS.md)、[浏览器发现](MEDIA_DISCOVERY.md)
- [批量任务](MEDIAFLOW_BATCH_IMPORT.md)、[音轨字幕](MEDIAFLOW_TRACKS.md)
- [媒体库](MEDIAFLOW_LIBRARY.md)、[命名与分类](MEDIAFLOW_ORGANIZATION.md)
- [去重与空间](MEDIAFLOW_STORAGE.md)、[迁移](MEDIAFLOW_MIGRATION.md)
- [安全政策](SECURITY.md)、[公开源码边界](PUBLIC_SOURCE.md)

应用自编源码保留原有 [MIT License](LICENSE)。外部 Qt、FFmpeg、yt-dlp 等各自适用其许可证；整个二进制发布包不声称仅适用 MIT，见 [第三方声明](THIRD_PARTY_NOTICES.md)。仅保存你有权获取和使用的媒体。
