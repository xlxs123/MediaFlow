# MediaFlow DASH 解析与候选状态修复

交付日期：2026-10-13。新入口：`dist-mediaflow-dash-fix\MediaFlow.exe`；移动时携带整个发布目录，包括 `tools`。旧发布目录、用户配置和 BrowserProfile 保留。

## 根因与修复

原流程只从 JSON 递归收集 URL，丢失了 DASH 视频轨、音频轨、SegmentBase 和组合关系，因而将 `.m4s` 当作孤立分片。现在按 JSON 结构识别 `data.dash`、`result.dash` 和根 `dash`，没有新增 B站 Resolver，也没有按域名硬编码解析分支。

先前两页诊断中的 playurl JSON 实际已完整采集（约 38/49 KB）；超过旧 2 MiB 限制的是部分媒体响应。因此修复同时覆盖“完整 JSON 未被结构化解析”和“较大响应无法采集”，不能将两个样本的根因都归为 JSON 截断。

`DashTrack` 保存主地址、备用地址、码率、尺寸、编码、初始化范围、索引范围和角色。`MediaCandidate` 新增 `video_track / audio_track`，提供 `video_url / audio_url`；下载上下文继续由既有 RequestContext 保存。每个视频版本配一个有效音频，优先 AAC，不生成视频×音频的重复组合。备用地址保存供诊断和后续使用，本次未新增备用 CDN 自动切换。

流程为：

```text
WebView2 原生 CDP 完整响应
  → DASH JSON 结构解析
  → 视频轨 + 音频轨
  → 已验证的内存 MPD（manifest_origin=captured:dash-json）
  → MediaCandidate / 原真实性与质量评分
  → 原队列 / DownloadRequest.manifest_text
  → 既有 MediaBridge / yt-dlp DASH 下载
  → 既有 FFmpeg 合并
  → ffprobe 输出验证 / 本地缩略图
```

内存 MPD 内使用绝对媒体地址，签名参数保持原文；API 地址只是描述来源，下载时不会重新将 API JSON 当 MPD。轨道请求复用浏览器会话上下文，资源 Range 由实际读取决定，不把 API 的 Range 强加给每个轨道。

## Range 与候选状态

单次 HTTP 206、完整的“一次 Range 响应”，或仅有 `ftyp/moov` 初始化数据，都不能证明完整 Direct。默认保留为可能分片。完整资源证据需要响应长度与 Content-Range 一致、完整媒体类型、非分片模式，以及完整容器内容；普通非 MSE 当前播放器还可提供实际初始化、有效时长等补充证据。仅有初始化小范围时，即使 video.currentSrc 相同也不会提升为 Direct。

DASH JSON 到达后，已观察轨道升级为完整 DASH 候选，相关主/备用轨道注册为清单资源。后续同轨道 Range、无正文事件或迟到的普通 URL 探测，不再将已验证 DASH 降级为 Segment/Direct。真正 DRM 证据仍会阻止下载；PSSH 和明确保护信息不会被后续 clear JSON 抹掉。真实性、质量评分算法没有修改。

## 可配置采集上限

“工具与下载设置”新增“浏览器单条响应采集上限”，配置键 `browser_body_limit_mib`，默认 **16 MiB**，范围 **1～128 MiB**。设置用于新打开的自动查找窗口，旧配置自动采用默认值。

CDP、Fetch、观察记录、工作队列和 JSON 解析使用同一配置。正文总预算默认 64 MiB，上限随单条配置增大至最多 512 MiB；超限响应明确标记不完整，不解析截断 JSON。WebView2 原生宿主与 Python IPC 同步扩大有界消息预算，避免正文在中间链路再次被较小限制截断。该设置调整浏览器采集预算；下载核心既有清单/解包安全上限保持原值，DASH JSON 生成的双轨 MPD 很小，不受原始 JSON 大小影响。

## 两个真实页面验收

使用原生 WebView2 154.0.4258.62、独立本地测试 Profile；未导入系统 Cookie，未登录账号。每页监听约 22 秒，之后从实际生成的候选进入原下载队列。计数是当次网络响应结果，不代表网站永远返回相同版本。

| 项目 | A：BV1CjaQ6NE42 | B：BV1fKeg6mEhi |
|---|---:|---:|
| 已解析 DASH 描述 | 1 | 1 |
| 视频轨 | 4 | 6 |
| 音频轨 | 3 | 3 |
| 最终候选总数 | 6 | 8 |
| 可下载 DASH 候选 | 4 | 6 |
| 保留的未关联分片候选 | 2 | 2 |
| 页面请求 / 媒体请求 | 216 / 23 | 248 / 45 |
| 实际下载 | 成功 | 成功 |
| 输出视频 | H.264，480×854 | H.264，854×476 |
| 输出音频 | AAC | AAC |
| 时长 | 40.681451 秒 | 96.687891 秒 |
| 文件大小 | 3,556,104 字节 | 7,965,030 字节 |
| 容器 | MP4 family | MP4 family |
| 本地 JPG 缩略图 | 第 1 秒生成成功 | 第 1 秒生成成功 |

可下载候选均具有视频/音频配对和完整上下文。当次响应提供的是上述分辨率，未承诺额外登录权限或更高清晰度。两个低可信度分片候选继续保留供诊断，不作为最终下载目标。

真实结果与 ffprobe 记录：[results.json](<<project-root>/verification/dash-fix/live-verified/results.json>)。

实际文件：[DASH-A.mp4](<<project-root>/verification/dash-fix/live-verified/A/downloads/DASH-A.mp4>)、[DASH-B.mp4](<<project-root>/verification/dash-fix/live-verified/B/downloads/DASH-B.mp4>)，同目录保存对应 `.jpg`。

每个样本的 `trace/` 记录下载前的发现阶段，因此其中 `external_downloads_performed=false` 只描述该阶段；实际下载以同级 `result.json`、`download.log` 和输出验证为准。`verification/dash-fix/live` 是验收脚本工具检测等待条件修正前的尝试，最终证据为 `live-verified`。

## 回归与发行验收

完整测试：**1510 项通过，0 失败、0 错误、0 跳过**，命令 `python -B tests/run_unit_tests.py`。结果留存在 `verification/dash-fix/unit-results.json` 和 `.txt`。

新增回归覆盖：DASH JSON 多清晰度、音频选择、主/备用签名 URL、SegmentBase、超过 2 MiB 的描述、可配置上限、JSON 截断拒绝、206 初始化分片、完整 Range、DASH 状态稳定、迟到探测、DRM/PSSH、请求头与 Range 隔离、缓存 MPD 不重新拉取 API、轨道进入既有下载桥接。

打包 EXE 已通过 Windows 原生 GUI 验收 **18 项**，包含启动、cURL、HLS AES-128、三视频并行、停止与重试；WebView2 原生验收 **8 项**，包含 HWND 嵌入、H.264/AAC 实际播放、MSE、CDP 和会话保留；媒体捕获验收 **6 项**，覆盖无后缀 Media / Fetch / XHR / Worker、HLS 与 DASH。新 DASH 模型与解析模块已确认包含在 EXE 内。

这些打包验收使用本地生成的媒体；上表两个真实页面的双轨下载由源码运行原有窗口与队列完成，未将其描述为打包 EXE 的远端下载验收。验收结果与发行 SHA256 见 `verification/dash-fix/packaged-gui`、`packaged-webview2`、`packaged-capture` 和 `release.json`。

## 修改文件

| 范围 | 文件 |
|---|---|
| 新增轨道模型与结构解析 | `core/dash_models.py`、`core/dash_json.py` |
| 候选、状态与上下文整合 | `core/discovery_models.py`、`core/media_discovery.py`、`core/discovery_worker.py` |
| 响应采集与元数据兼容 | `core/browser_observer.py`、`core/config_media.py`、`core/media_details.py` |
| 设置与浏览器消息链路 | `utils/config.py`、`ui/main_window.py`、`ui/discovery_dialog.py`、`ui/webview2_browser.py`、`native/webview2/Host.cs` |
| 新增回归 | `tests/test_dash_json.py`、`tests/test_dash_discovery.py`、`tests/test_dash_download.py`、`tests/test_capture_limits.py`、`tests/test_config.py` |
| 调整原测试 | `tests/test_browser_observer.py`、`tests/test_config_media.py`、`tests/test_media_discovery.py`、`tests/test_mediaflow_discovery.py`、`tests/test_webview2_browser.py` |
| 真实验收脚本与交付文档 | `scripts/verify_dash_download.py`、`README.md`、`build.ps1`、`MEDIAFLOW_DASH_FIX.md` |
| 重新构建的原生宿主 | `tools/webview2/MediaFlow.WebView2Host.exe`，同步至新发布目录 |

`core/downloader.py`、`core/media_bridge.py`、`core/media_guard.py`、`core/media_confidence.py`、`core/media_quality.py`、`core/output_validator.py` 与诊断前基线的 SHA256 一致。本次沿用既有下载、合并、输出验证与评分实现，不升级 yt-dlp/FFmpeg 工具。
