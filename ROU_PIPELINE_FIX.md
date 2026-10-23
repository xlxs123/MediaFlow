# Rou 下载管线与实际输出校验修复

保留现有下载器、并行队列和 UI，修复清单正文传递、PNG 分片媒体载荷解包、成功前的实际输出探测。新入口为 `dist-rou-fix/流媒体下载器.exe`；旧版本、ZIP 和用户原文件保留。

## 1. 为什么之前出现 PNG 伪 MP4

此次只读检查当前配置的下载目录，找到四个坏文件，包含首图尺寸与样本 A/B 一致的 320×192、864×864 文件。它们由连续 PNG 分片容器拼接而成，完整容器数量为 89、54、737、106；`roUd` flag 均为 0，载荷是明文 MPEG-TS。

确定的真实缺陷是：HLS 已按 Manifest 把 URI 识别为分片，但没有解包 PNG 中的媒体载荷，原样拼接了 PNG 容器。不能把这份证据说成单个 `index.png` 被复制。四个现有坏文件已无网络恢复为实际 H.264/AAC MP4，原文件 SHA256 未变。

还存在额外漏洞：候选没有保存完整 `manifest_text`，下载会重新取 signed PNG；如果手动输入丢失 decoder，错误 `video/mp4` MIME 可将图片送入 Direct。旧成功分支只检查 exit code 和文件标记，导致上述错误产物报成功。冻结旧源码复现见 `verification/rou-pipeline-baseline/`。

## 2. 错误发生在哪些类与函数

| 位置 | 修复 |
| --- | --- |
| `RouVideoResolver._validated_candidate()` / `_candidate()` | 保存 transport URL、最终 base、完整正文、来源和已验证标志 |
| `MediaDiscoveryService.consume_resolver()` / UI / `DownloadManager._request()` | 候选复制、单项、批量与刷新均传递 `download_payload()` |
| `DownloadWorker.run()` / `check_media()` | 优先解析正文，声明 HLS 与实际解析不一致即拒绝 |
| `MediaBridge.Handler.serve()` | PNG 分片二进制载荷解包；嵌入式清单重复请求使用内存正文 |
| `DownloadWorker.run()` 成功分支 | ffprobe 验证实际输出；致命分片错误也不能被部分产物覆盖 |

本项目没有独立名为 `DownloadDispatcher`、`HLSDownloader`、`DirectDownloader` 的类，其职责由 `DownloadWorker`、`MediaInspection`、`MediaBridge` 和现有 yt-dlp 承担，没有重写下载器。

## 3. manifest_text 是否真正传入 HLS 下载

`RouVideoResolver` → `MediaCandidate.download_payload()` → `MediaDiscoveryService` → `MainWindow` / `DownloadManager` → `DownloadRequest` → `DownloadWorker.run()` → `check_media(manifest_text=...)` → `MediaSnapshot.data` → `MediaBridge.rewrite_hls()` → yt-dlp 原 HLS 流程。

字段为 `transport_url`、`manifest_url`、`manifest_text`、`manifest_origin`、`validated`，PNG 来源为 `embedded:png/roUd`。正文保存在任务内存，不进入 repr、设置、日志或候选诊断。刷新候选同时替换正文与 base；原始 URL 更新不能误复用旧正文。

## 4. 是否仍会重新把 index.png 当 Manifest 请求

正文存在时直接解析并做 DRM 检查，重复 HEAD/GET 始终使用内存正文。批量真实回归的 transport 重请求数为 0。正文不存在时才走 fetch/decode/parse；Master 普通子清单仍正常获取。

相对 URI 使用最终 `manifest_url` 为 base；signed transport query 原文保留，绝对分片自身的签名不改写。`#EXTINF` URI 始终为 Media Segment，`.png`、无后缀或其他名称不改变角色。

PNG 分片另走 `decode_media_segment()`：检查 PNG 签名、IHDR、边界、CRC、单一 roUd、IEND；flag0 取二进制载荷，flag1 有界 zlib/raw-deflate 解压。明文载荷需识别为 TS、ISO-BMFF 或 ADTS AAC；普通图片、清单、未知 flag、截断、错误 CRC 拒绝。原媒体仍透传，不根据域名或后缀决定解包。

已知封装分片先读取完整 transport，再对解包后的媒体应用下载器 Range；Content-Length/Content-Range 按媒体计算。原会话要求的 `Range: bytes=0-` 仍可继承，未封装普通 HLS BYTERANGE 保留原语义。Key 不接受下载器的分片局部 Range，不伪造 Origin。封装内标准 AES-128 密文先解包，再由原 yt-dlp 使用普通 Key/IV 解密。明文 BMFF 解包后继续检查 PSSH/CENC。

## 5. Direct 为什么不会接到 Rou HLS 候选

声明 HLS 的任务必须实际解析为 HLS，PNG URL 后缀及错误 MIME 不能触发 Direct 回退；不一致返回 `MEDIA_PROTOCOL_MISMATCH`。桥中清单响应变成 Direct 也拒绝。普通 Direct URL 保留，但图片产物同样不能报成功。

## 6. 最终输出如何验证

`core/output_validator.py:validate_output()` 以参数数组、`shell=False` 和隐藏窗口运行实际 ffprobe。`.mp4` 必须属于 `mov/mp4/m4a/3gp/3g2/mj2` family 并有非封面视频流；`png_pipe`、`image2` 等图片容器及非封面 PNG 流返回 `OUTPUT_NOT_VIDEO`。即使 PNG remux 成实际 MP4，也拒绝。

全部输出验证通过才设置 `success=True`，否则队列为 `failed`。工具缺失、超时、探测失败都不能报成功。无媒体载荷的 PNG 分片在转发前就失败，致命解包或 DRM 错误不能被部分有效文件覆盖。失败文件保留但没有成功文件入口。原有音频、WebM/MKV、MJPEG 视频保持兼容。

## 7. 新增测试与验证范围

- `tests/test_rou_pipeline.py`：19 项，含用户要求的五项，及候选复制、UI 单项、批量、刷新、缓存、最终 base、Range、无 Origin、DRM 和协议分流。
- `tests/test_output_validator.py`：49 项，含实际 ffprobe 两种尺寸 PNG 伪 MP4、PNG remux、正常视频/音频/容器、取消、超时与工具缺失。
- `tests/test_rou_segment_decoder.py`：57 项，含 FFmpeg 实际 TS/fMP4/AAC、376-byte TS 小片、flag0/flag1、有界解压、CRC/长度/重复/未知 flag及 AES-128 密文。
- `tests/test_rou_segment_bridge.py`：14 项，含真实桥二进制载荷、解码 Range、HEAD 长度、AES/NONE、PSSH 拒绝、原媒体与无后缀资源。
- `tests/rou_current_smoke.py`：真实 Chromium → 候选 → 原队列 → yt-dlp → FFmpeg → ffprobe，PNG(roUd 二进制 TS / AES-128 密文) 分片下载后画面哈希与源一致。三视频批量不重取索引，纯 PNG 两条负例都 FAILED，保留 CORS、无 Origin、广告过滤、自动刷新及续传。

完整测试数量和源码/EXE/便携迁移结果以 `TEST_RESULTS.md`、`verification/release-results.json` 为准。修改文件列表见 `MODIFIED_FILES.md`。

原文件证据为 `verification/rou-pipeline-original-outputs.json`、`rou-original-container-scan.json`、`rou-original-recovery.json`。恢复副本位于 `verification/rou-recovered-existing/`，匹配 A/B 的副本为 `Rou 恢复样本 A.mp4`、`Rou 恢复样本 B.mp4`。报告只含匿名容器、流、时长、哈希，不保存原文件名、标题或媒体 URL；恢复视频不会收入交付 ZIP。

样本 A 的 54 个真实 PNG 分片还通过 localhost 重放至新版完整管线：Resolver 候选 → DownloadRequest → MediaBridge → yt-dlp → FFmpeg → ffprobe。产物为 H.264/AAC MP4，视频数据包哈希与恢复副本一致；没有重新请求索引，Referer/UA/Range 继承，无 Origin/Cookie/Authorization。证据为 `verification/rou-original-pipeline-replay.json`。这是实际原媒体字节的本地全管线验证，不是新的实站在线下载。

本轮在线 Rou 访问遇到正常 TLS 连接失败，未宣称完成当前实站新下载。真实现有数据已完整解包和 remux 验证，新应用完整下载使用本地同协议 PNG 封装媒体完成。此前在线记录只验证 API/清单元数据，不能替代整段下载证据。

真正 DRM 仍拒绝，不请求许可证、不提取 DRM 密钥、不绕过 CDM；无新增域名分支、地址猜测、签名构造或 TLS 绕过。
