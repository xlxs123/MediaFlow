# MediaFlow 1.2.0 智能续传与失败恢复

## 原有能力审计

项目没有独立的 DirectDownloader、HLSDownloader 或 DASHDownloader。三类下载均由 `DownloadWorker`、`MediaBridge` 和现有 yt-dlp 执行，FFmpeg 负责合并及封装。

| 项目 | 修改前 | 本版处理 |
| --- | --- | --- |
| Direct 续传 | 已有 yt-dlp `--continue` 和 `.part`；桥转发 Range | 保留原下载器，严格核对实际 200/206/416、资源标识和长度 |
| HLS 分片 | 已有原生 `.part/.ytdl`、分片并发和 AES-128 | 增加校验缓存；损坏分片重取；不可信组合文件保留并重建 |
| DASH 音视频 | 已有解析、配对、独立轨道和合并 | 保留完整轨道至输出验证完成，失败重试复用原轨道 |
| 暂停、继续、停止、重试 | 已有按钮、队列与地址刷新 | 复用按钮，增加恢复子状态和重试倒计时 |
| 网络重试 | 原 yt-dlp HTTP/分片有限重试 | 增加任务级分类恢复和所有实际远端请求的 Retry-After 冷却 |
| 跨程序重启 | 队列仅在内存 | 新增原子任务快照和 Windows 当前用户 DPAPI 加密 |
| 最终文件 | 下载器先写正式目录，再验证 | 先暂存及 ffprobe 验证，再无覆盖提交；提交意图支持崩溃协调 |

参考实际工具版本 yt-dlp 2026.08.19 的 [HTTP 下载器](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/downloader/http.py)、[分片下载器](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/downloader/fragment.py) 和 [保留中间轨道选项](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/README.md)。本版没有重新实现媒体解析、AES 解密或 FFmpeg 合并。

## 实际修改范围

| 文件 | 用途 |
| --- | --- |
| `core/downloader.py`、`core/media_bridge.py` | 保留原 yt-dlp 管线，增加恢复协调、实际响应检查、分片缓存和验证后提交 |
| `core/http_client.py`、`core/http_ranges.py`、`core/retry_policy.py`、`core/errors.py` | 严格 Range、资源标识、Retry-After、取消等待和临时故障分类；证书错误与传输意外 EOF 分开处理 |
| `core/recovery_workspace.py`、`core/segment_cache.py`、`core/resume_identity.py` | 独立工作区、块/分片哈希、崩溃协调与资源身份核对 |
| `core/task_store.py`、`core/download_queue.py`、`core/download_manager.py` | 加密持久化、恢复后暂停、不自动隐藏解析及终止错误的重试边界 |
| `ui/main_window.py`、`main.py`、`utils/branding.py` | 原界面状态、正常关闭保存、验收入口及 1.2.0 版本 |
| `build.ps1`、`assets/build.json`、`assets/version_info.txt` | 新版说明随包、构建身份与 Windows 版本资源 |
| `tests/recovery_acceptance.py`、`tests/test_*recovery*.py`、`tests/test_cache_streaming.py` | 可控真实 HTTP、异常退出、GUI、持久化、流式缓存与错误分类测试 |
| `tests/test_batch_download.py`、`tests/batch_smoke.py`、`tests/rou_current_smoke.py` | 终止错误回归；旧烟测适配既有多播放器选择策略和新的暂存提交行为 |
| `README.md`、`BATCH_DOWNLOAD.md`、`MEDIAFLOW_RECOVERY.md` | 使用说明、任务保存与验收边界 |

精确文件列表及 SHA256 保存在项目的 `verification/recovery/modified-files.json`，可读列表为同目录 `MODIFIED_FILES.md`。媒体可信度核心、网站 Resolver、DASH 解析器、媒体发现界面和 WebView2 宿主源码均与修改前基线一致；没有新增第三方依赖。

## 文件位置与正常使用

- 新发布目录：`dist-mediaflow-recovery`。请携带整个目录，包含 `tools`、WebView2 宿主及许可证。
- 稳定回退目录：`dist-mediaflow-dash-release-v2`（1.1.1），不覆盖。
- 设置：`%APPDATA%\MediaFlow\config.json`。
- 任务：`%APPDATA%\MediaFlow\tasks.json`，最近有效备份 `tasks.json.bak`。
- 每任务工作区：`<输出目录>\.stream-downloads\<独立任务ID>\`。
- `transfer`：原生下载部分及轨道；`staging`：待验证输出；`cache`：校验分片和资源校验标识；`workspace.json`：身份摘要、校验偏移、哈希和提交记录。
- `retained` / `.corrupt-*`：被隔离的损坏或无法确认的部分，保留供检查；不会作为有效下载数据使用。

退出时暂停并保存未完成任务。重开后，任务不会自动下载或自动打开隐藏解析窗口；选中任务点击原有“继续”。地址已失效时点击“刷新媒体地址并继续”，或主动提供新 URL/cURL 后更新链接。用户点击“移除已结束”仅删除队列记录，失败或取消任务的部分文件不会被隐式清空。

普通任务 JSON 只保存路径、任务ID、状态、时间、错误类别和白名单数值进度。URL、查询签名、Cookie、Authorization、自定义认证头、POST 正文、清单、完整媒体身份和最近错误详情保存在 DPAPI 密文中。只有同一 Windows 用户可以解密；解密失败显示“需要重新分析”，不会使用明文降级。备份损坏时保留原文，禁止用空队列覆盖。浏览器登录仍使用现有 WebView2 Profile。

## 恢复策略

### Direct

复用 yt-dlp 的字节续传。实际响应必须满足请求范围、总长与 Content-Length/Content-Range 一致性；使用原强 ETag 或有效 Last-Modified 设置 If-Range 并核对资源版本。Accept-Ranges 仅作提示。

服务器忽略 Range 返回 200 时交给原 HTTP 下载器安全从头写入，显示“重新下载”；缺少可靠验证器也采用安全重头。错误范围、版本变化或长度变化拒绝追加。416 只有偏移恰好等于资源总长且版本一致时才允许原下载器进一步确认完成。

部分文件按 1 MiB 块记录 SHA256。正常暂停保留精确偏移；异常退出只复用已校验前缀，尚未校验的尾部回退。发现内容损坏时隔离旧文件并重新下载，不能把坏文件直接追加。

### HLS

静态清单身份及分片布局一致时，优先复用已完成且 SHA256、大小、响应长度均通过校验的分片。缓存键包含资源、角色与字节范围；密钥不写入缓存。原 yt-dlp 继续管理分片顺序、初始化片段、AES-128 与拼接。

正常暂停可复用一致的 `.part/.ytdl`；异常退出时不盲信可能不同步的原生组合文件，保留旧部分并从校验缓存重建，只补缺片。PNG roUd 清单与分片解码、FixupM3u8 和 MP4 验证链路保留。直播、滑动窗口或清单布局变化不承诺跨会话无损恢复。

### DASH

保留原 DASH JSON/MPD 解析和配对。已完成视频轨不随音频失败重下，使用 `--keep-video` 保留中间轨道；音频恢复后再合并，合并失败也保留轨道。必需视频和音频都在最终输出中、且实际 ffprobe 验证通过后才提交文件。

小分片使用 SHA256 缓存。大于 32 MiB 或长度未知的 DASH prefix 走原流式 HTTP 下载器，不预先复制整条大轨；用持久资源验证器和独立 `.part` 块检查继续下载。合并和最终输出先在任务工作区内完成，正常最终文件绝不覆盖。

## 错误分类与等待

任务级默认最多 3 次尝试（首次加 2 次恢复），默认指数等待 2、4 秒；现有分片总尝试次数仍生效。所有实际网络请求（包括清单刷新、不同 Range）受服务器冷却时间控制，429/503 有效 Retry-After 优先，等待可通过暂停/取消立即中断。

- 临时 DNS、连接超时/重置、提前 EOF、明确的 TLS 传输意外 EOF，以及 429/500/502/503/504：有限重试并保留有效部分。TLS 连接仍执行正常证书校验。
- 已过期签名、明确需要新上下文的 403/404/410：进入待刷新，使用现有合法浏览器会话与解析入口。
- DRM、权限、磁盘空间、真实404、TLS校验失败、用户取消、坏范围或资源身份不符：不盲目重复请求，返回具体原因。
- 合并/验证失败：保留轨道及暂存文件，允许用户修复工具或环境后手动重试。

任务卡片显示下载、等待网络、重试次数及倒计时、恢复/重新下载、合并、验证、完成或失败；提示文字保留最近失败原因。百分比来自实际字节/分片进度，双轨按已知轨道总量计算；信息不足时保持未知，验证提交前不显示任务已完成。

## 验证与限制

本地验收使用自生成媒体及独立 HTTP 服务器，不使用用户浏览器登录或下载目录。入口：

```powershell
.\.venv\Scripts\python.exe main.py --recovery-acceptance <新建结果目录>
.\dist-mediaflow-recovery\MediaFlow.exe --recovery-acceptance <另一个新建结果目录>
```

最终测试数量、构建身份、EXE 哈希、真实输出和各项结果以 `verification/recovery/release.json` 及对应验收文件为准。源码回退副本及稳定目录哈希在 `.codex/recovery-baseline-20261017-150310`。

限制：DPAPI 任务不能移到另一 Windows 用户后直接使用；缺少可靠 HTTP 验证器的直链会安全重头；直播窗口变化、资源版本变化和过期地址可能需要重新分析；已确认身份但切换 CDN/包装路径时，部分缓存可能无法命中，保留的数据不会不安全混用。分片缓存会增加临时磁盘占用，只有完成验证和最终提交后才清理可安全删除的工作文件。历史 B站、Rou、抖音结果不能替代新版 EXE 的当前验收；不声称所有网站都支持续传。

## 最终验证结果（2026-10-18）

- 构建：`20261018T050654Z-220c7fbf4f9d`；版本 1.2.0。
- 完整单元测试：1646 项，0 失败、0 错误、0 跳过，进程退出码 0。
- 当前冻结源码与最终 EXE：各 13/13 恢复场景通过。错误 Range、资源变化及不完整 416 的通过含义是安全停止，未误报完成。
- 最终 EXE 原功能回归：7 套、99 项通过。评分用例首轮异步状态断言失败，使用同一 EXE 独立复核15项通过；首轮记录保留。
- 真实网站：两个指定 B站样本和 Rou 实际完整下载、ffprobe 音视频及本地缩略图均通过；抖音匿名样本仅孤立分片，未通过。
- 稳定版 1.1.1：171 个文件哈希全部与原基线一致。
- 详细10项交付报告：项目 `verification/recovery/DELIVERY.md`，发布目录内 `MEDIAFLOW_RECOVERY_REPORT.md`；原始结果索引为 `release.json`。
