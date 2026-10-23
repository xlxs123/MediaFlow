# MediaFlow 品牌迁移与本地视频封面

应用名称为 MediaFlow，中文副标题为“智能媒体发现与下载工具”，英文副标题为“Intelligent Media Discovery & Download Tool”。新版独立发布在 `dist-mediaflow-release/MediaFlow.exe`；移动时携带整个目录。窗口、Qt 应用信息、关于对话框、启动提示、EXE 版本属性、构建名称与日志导出默认名称均统一。日志仅由用户点击“导出日志”保存，默认文件名 `MediaFlow.log`，现有脱敏行为保留。

## 旧配置与用户文件

默认新配置为 `%APPDATA%/MediaFlow/config.json`。首次启动若新配置不存在，依次读取 `%APPDATA%/流媒体下载器/config.json`、`%APPDATA%/M3U8 Downloader/config.json`，将白名单偏好写入新目录。原文件保留，不删除、不改写。输出目录、工具路径、User-Agent、窗口尺寸、界面偏好与并发参数兼容；不迁移请求 URL、Cookie、Authorization、body 等敏感请求数据。新配置已存在时不覆盖，也不导入旧值。

浏览器使用内存 HTTP 缓存与临时 Profile，不存在需迁移的持久浏览器缓存。下载队列、任务历史与日志本来仅保存在当前会话内，没有磁盘历史数据库。下载文件、断点数据、旧发布目录、旧 ZIP 均不移动或删除。显式指定配置路径的测试保持隔离。

## 本地封面流程

原下载器成功完成 → 原 ffprobe 媒体验证通过 → 主界面收到既有完成事件 → 独立后台封面服务 → 本地 FFmpeg 截图 → 真实 JPEG 解码检查 → 同目录同名 `.jpg` → 任务卡片显示。

截图依次尝试 1 秒、3 秒和已验证时长的中点。只处理已验证的真实视频流，排除音频封面流，MP3 等音频跳过。使用既有 FFmpeg，不添加依赖，不请求网站封面。单个封面有总超时与取消机制，封面任务串行，GUI 保持响应。

失败只更新封面提示，不改下载任务状态、不撤销成功事件、不改输出视频。临时 JPEG 独占创建，既有同名封面不覆盖；有效 JPEG 直接保留显示。关闭应用会停止封面子进程并等线程退出。UI 显示“✓ 下载完成”“✓ 媒体验证通过”“✓ 缩略图生成成功”或“⚠ 缩略图生成失败”。

## 修改范围

- 品牌和配置：`utils/branding.py`、`utils/config.py`、`ui/app_setup.py`、`ui/main_window.py`。
- 本地封面和 UI：`utils/thumbnails.py`、`ui/thumbnail_service.py`、`ui/media_cards.py`。
- 构建发行：`MediaFlow.spec`、兼容入口 `M3U8 Downloader.spec`、`assets/version_info.txt`、`build.ps1`、`tools/package_release.py`、`启动开发版.vbs`。
- 验证：`tests/test_brand_migration.py`、`tests/test_thumbnail_service.py`、`tests/test_thumbnails.py`、`tests/integration_thumbnails.py`、`tests/test_security.py`、`tests/test_ui.py`、`tests/mediaflow_smoke.py`、`tests/rou_current_smoke.py`、`tests/discovery_smoke.py`。
- 文档：`README.md`、本文件、`BATCH_DOWNLOAD.md`、`PROTOCOL_REFACTOR.md`、`RESOLVER_UPGRADE.md`（同步当前品牌名称，历史路径保留）。

Resolver、Downloader、MediaCandidate、下载队列数据结构、yt-dlp 调用与原 FFmpeg 合并流程均不修改。完成时使用 `.codex/brand-thumbnail-protected.json` 对 33 个核心源文件与 3 个工具 EXE 的 SHA256 检查。

## 本次验证

完整单元测试 **1168 项通过，0 失败、0 错误、0 跳过**。真实本地 FFmpeg 封面检查 **5 项通过**。源码与打包版 Windows 原生验收合计 **90 项通过**，涵盖实际媒体发现、下载、封面展示、三视频四分片并行、取消重试和 Rou 当前协议。EXE 的 ProductName、OriginalFilename、FileDescription、版本与英文介绍均已从实际 Windows 文件属性读取确认。

| 场景 | 实际检查与结果 |
| --- | --- |
| MP4 | 自生成 4 秒 MP4，第 1 秒生成有效 480×270 JPEG，视频哈希未变 |
| 后备截图 | 0.4 秒视频在第 1、3 秒无帧后，于 0.2 秒中点成功；真实尝试序列 `[1.0, 3.0, 0.2]` |
| HLS | 源码与 MediaFlow.exe 均经真实 Chromium 查找、原队列下载 AES-128 HLS；ffprobe 与画面哈希通过后生成同名 JPEG，UI 显示成功 |
| MP3 | 真实自生成 MP3 无视频流，跳过且不生成 JPEG，音频哈希不变 |
| Rou | 源码与 EXE 各 26 项当前协议夹具检查通过，各 8 个成功视频均生成 JPEG；PNG 清单/分片、AES-128、签名刷新和续传正常 |
| 抖音 | 既有合成请求、播放器 DOM、上下文和候选回归随完整单元测试通过；封面统一处理验证后的本地文件。本次未进行抖音实站在线下载复测 |
| 缩略图失败 | UI 回归确认仍为 completed、原成功事件只发一次、下载结果不变；已有 JPEG 和未知同名文件均保留 |
| 配置迁移 | 中文/英文旧目录、新配置优先、迁移写入失败及隔离配置路径等检查通过，旧文件原字节保留 |

精确结果、27 个修改文件的 SHA256、EXE 哈希、36 个受保护文件及各验收路径见 `verification/brand-thumbnail-release-results.json`，发布目录内附同名报告。封面独立检查见 `verification/brand-thumbnail-local-agent/thumbnail-results.json`。本次浏览器与协议验收使用本地 FFmpeg 视频、真实 Chromium 和现有下载管线，Rou 使用当前协议夹具，抖音使用已有合成请求/DOM 回归；未进行新的 Rou 或抖音实站在线下载。
