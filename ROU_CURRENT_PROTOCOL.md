# 当前 RouVideo 协议实现与交付说明

> 下方验收数量和 EXE 哈希记录此前正式解析版。最新清单正文传递与输出容器修复见 [ROU_PIPELINE_FIX.md](ROU_PIPELINE_FIX.md)，当前结果以发布包 `TEST_RESULTS.md` 为准。

本次在现有流媒体下载器内更新 `RouVideoResolver`，复用 RequestContext、HLS/DASH Parser、MediaBridge、yt-dlp、FFmpeg 和下载队列。当前正片来源按 API 实际响应确定，成功后不再与 DOM / MSE / Direct 猜测竞争。

本文描述当前实现。`RESOLVER_UPGRADE.md` 是早期升级记录；其中 NEXT 优先、猜同目录文件、补缺失 `#EXTM3U` 和 Rou 全局签名继承等描述不适用于本次主流程。

## 证据与验证范围

用户提供了三个真实视频的抓包和拆包结论：入口均为 `/api/hls/{id}` 的 302 Location，PNG 内含 `roUd`，flag bit 0 表示压缩，解包为 HLS VOD。三个样本分别约为 106 分片 / 860.333 秒、151 分片 / 1191 秒、26 分片 / 259.493 秒；CDN、目录和文件名存在差异。这些是用户提供的协议证据，不是本开发环境的在线实测结果。

本地回归使用合成 HTTP 观测、内存 PNG/HLS 数据，以及 FFmpeg 自生成媒体和 localhost 服务器。测试专用 Resolver 将同一协议映射至回环地址，生产域名匹配保持不变。

已用临时内存浏览器实际解析用户提供的 `https://rou.video/v/cm5nlwtpm0000jpseusy9ni66`：API 302、Index HTTP 200、真实 PNG 的 IHDR/IDAT/roUd/IEND、flag 0x01、zlib、HLS VOD；106 个分片、860.333302 秒、Encryption None、DRM No、来源 RouVideoResolver、已验证。脱敏记录为 `verification/rou-live-metadata.json`。本次没有在线下载整段 rou.video 视频；完整下载、AES、并发和签名刷新续传在自生成媒体上实测，二者分别报告。

## 1. 修改文件

协议实现主要涉及 `core/site_resolvers.py`、`core/rou_playlist_decoder.py`、`core/resolver_models.py`、`core/discovery_models.py`、`core/browser_observer.py`、`core/media_discovery.py`、`core/discovery_worker.py` 和 `core/errors.py`。

下载复检、队列及续传涉及 `core/media_guard.py`、`core/download_queue.py`、`core/download_manager.py`、`core/resume_identity.py`；界面涉及 `ui/discovery_dialog.py`、`ui/dialogs.py`、`ui/main_window.py`。新增当前协议与 UI 回归、当前协议 localhost 夹具；更新旧 `tests/resolver_fixture.py`、`tests/resolver_smoke.py`，使 legacy 验收也不再要求猜邻近文件。

本轮完整修改及新增清单、字节差异和最终发行摘要由 `MODIFIED_FILES.md`、`verification/modified-files.json`、`verification/release-results.json` 记录；应核对报告对应的实际源码与 EXE 哈希。

## 2. 类与接口

| 类或接口 | 本次作用 |
| --- | --- |
| `RouVideoResolver` | 更新现有适配器，优先请求当前正片 API，使用真实 Location 建立正片身份 |
| `RouPlaylistDecoder` | 更新现有独立解包器，严格解析 PNG / roUd / HLS |
| `DecodedRouPlaylist` | 保存解出的 bytes、容器、flag、压缩方式和 Chunk Trace |
| `RouPlaylistError` | 保留明确的 Rou 容器、解压和清单错误码 |
| `ResolverTracePanel` | 新增可折叠完整 Trace，展示内容脱敏 |
| `ResolverRequest.headers` | 在兼容原有字段的基础上增加已观察请求头 |
| `MediaDiscoveryService.exclude_resource_role` | 通用消费 Resolver 确认的非正片角色，不依赖域名或 URL 路径 |
| `DiscoveryDialog.retain_session_profile()` | 把合法内存浏览器会话交给主窗口，供当前任务刷新使用 |

没有新增站点专用 Segment Downloader、AES Downloader、Remux 或 FFmpeg 实现。

## 3. 完整调用链

```text
/v/{videoId}
    → ResolverRegistry
    → RouVideoResolver
    → 当前浏览器会话 GET /api/hls/{videoId}
    → 保存实际 301 / 302 / 307 / 308 Location 与 redirect_chain
    → 使用服务器返回的完整 signed index URL
    → RouPlaylistDecoder
    → PNG Signature / Chunk / roUd / flag / 有界解压
    → 合法 #EXTM3U
    → 标准 HLS Parser
    → MediaCandidate + RequestContext
    → MediaGroup / DownloadQueue
    → MediaGuard / MediaBridge / yt-dlp
    → 原分片重试、断点续传、AES-128、FFmpeg 合并
```

当前 API 未给出合法 Location 时，确定性正片解析失败。显式已下发的旧 NEXT/JSON 入口可进入 legacy fallback；未知站点或解析失败保留通用发现。`?kind=thumbs` 明确归为 `THUMBNAIL_TRACK`，即使返回 302 或合法 HLS，也不能成为正常下载候选。

## 4. 解码器输出与 HLS 校验

`decode(bytes)` 保留 bytes 返回，避免破坏原下载接口；`decode_text` 返回文本，`decode_with_trace` 返回 `DecodedRouPlaylist`。普通文本 HLS 可以直接进入校验；其他内容必须是合法 PNG 容器。

输出必须是有效 UTF-8、首个有效行是 `#EXTM3U`，并含合法 Master 变体声明，或 `#EXTINF` 与后续 Media Segment URI。使用现有 Manifest Parser 复检属性和结构。不会补造缺失的 `#EXTM3U`，不会把普通 PNG、MPD、MP4 或解压成功的任意文本当作 Rou HLS。

## 5. PNG Chunk 边界检查

验证 8 字节 PNG Signature 后，按大端 `Length(4) + Type(4) + Data(length) + CRC(4)` 顺序遍历，读取前检查剩余长度。校验 Chunk Type、保留位、CRC、首个 IHDR 属性、重复 IHDR / roUd，以及完整且终止文件的 IEND。不使用 `bytes.find(b"roUd")` 截取内容。

当前默认限制：PNG 总输入 16 MiB，单 Chunk 8 MiB，最多 4096 个 Chunk，roUd 输入最多 2 MiB 加 1 字节 flag，解出 Playlist 最多 2 MiB。CRC、越界、截断、重复块和尾部垃圾均返回具体错误。

浏览器发现的单响应正文捕获上限为 2 MiB，全部正文累计 16 MiB；下载前 PNG 容器读取上限为 16 MiB，解出的 HLS 仍限制 2 MiB。

## 6. roUd flag

`chunk_data[0]` 是 flag，`chunk_data[1:]` 是 payload。只判断 `flag & 0x01`：bit 0 置位则解压，否则按明文处理。flag 可包含其他位，不限制它必须恰好等于 1。空 flag / payload 返回 `ROU_PAYLOAD_INVALID`。

Trace 保存完整 flag，例如 `0x01`，并区分 `none`、`zlib` 与 `deflate`。

## 7. 有界解压

使用 `zlib.decompressobj`，先尝试 zlib 包装，再尝试 raw deflate；单次输出限制为 `max_size + 1`，默认清单上限 2 MiB。超过上限或仍有待消费压缩数据时返回 `ROU_PLAYLIST_TOO_LARGE`；未到 EOF、尾随垃圾或附加压缩成员返回 `ROU_DEFLATE_FAILED`。不无限制调用 `zlib.decompress()`。

## 8. 浏览器请求上下文

API 和 Index 获取优先使用当前 off-the-record Chromium Profile。Cookie、UA、Referer 和 Origin 由浏览器会话按实际请求规则提供；不自行生成授权、签名或 token。已有图片响应可读取其已观察的正文，避免用另一种 Fetch 替代请求改变 Origin / CORS 行为。

同源 API 默认使用 `credentials=same-origin`，在原会话中发送正常同源 Cookie；跨源重定向遵循浏览器默认，不强制把凭据带到允许通配 CORS 的 CDN。只有该跨源目标已实际观察到 Cookie 时才启用 include。尚在加载的已授权 Image 等待 loadingFinished 后通过 CDP 读取正文，不转换成另一种 Fetch；等待和正文读取均有尺寸、并发及超时限制。相同 URL、相同字节的缓存重放可复用已有真实完整上下文，缓存内容改变或新 403 不会沿用旧成功结果。

`ResolverRequest.headers` 传入 `observer.fetch_url`；只显式重放该 URL 已观察到的 Authorization、Range、Accept 和 X-* 字段，不向另一 CDN 手工复制 API 凭据。Network / ExtraInfo 保存最终实际请求上下文。

HLS 的 Key、Segment 和子清单复用已有 RequestContext 派生规则，继承实际会话字段；Origin 不凭空补造。Range 根据现有设置及实际观测继承，资源自身 byte range 优先。Cookie、Authorization 和签名保留于任务内存，普通 UI / 日志 / Trace 脱敏，程序退出后不持久化浏览器凭据。

## 9. Redirect 与签名保存

保存 `original_url`、每跳 `status/from/to/location`、`final_url` 和 `redirect_chain`，不因为客户端自动跟随而丢掉原始 302 证据。只有实际服务器 Location 可建立当前正片身份。

API 原始 URL、Manifest URL、Location、发现时间、`possible_signed_url`、`signed_url` 和可解析的 `exp` 进入候选元数据。URL query 的顺序、重复参数、百分号编码及签名原值保持完整；UI 查询值脱敏。不会清洗 auth、猜 exp 或重新生成服务器签名。

## 10. CDN 与 URI 处理

生产实现没有硬编码 CDN host。绝对 Segment URI 使用 Manifest 原值；相对 URI 按最终 Manifest URL 和标准 `urljoin` 解析。当前 `api_redirect` 主路线使用 `STANDARD_URLJOIN`，不自动把 Index 的 auth / exp query 复制到相对 Segment。

旧 legacy 的显式限定 query 策略只保留给已有兼容路线，不成为当前 Rou 主路线或其他站点的全局规则。

## 11. 目录与清晰度

生产实现不根据 `-402`、`-720` 或猜测清晰度构造 CDN 目录。目录可能是任意字符串，由 Location 原样决定。

测试夹具包含这些目录只是复现用户提供的不同结构；它们不参与生产解析决策。相邻 `index.jpg / index.jpeg / index.m3u8 / master.m3u8` 不再由解析器猜测。

## 12. Segment 文件名

生产实现不按 `CLS-`、`C-` 或固定编号判断分片。Manifest 中 `#EXTINF` 后的 URI 就是 Media Segment，`.png / .jpg / .dat / .bin / 无扩展名` 均遵循 HLS 语义。

Manifest 语义优先于 MIME、内容探测和扩展名。普通 PNG 无 roUd 不会变成清单；已声明的 `.png` Segment 不会因为后缀被当作图片丢弃。

## 13. NEXT 与旧路线

当前主流程从 `/api/hls/{videoId}` 开始，即使页面存在旧 `__NEXT_DATA__` 也不会抢先使用它。API 明确失败后，现有 NEXT / `pageProps.ev` 和 API JSON 可作为 legacy fallback。

旧 `_siblings` 仅返回原已下发入口，不探测同目录其他名字。旧 `variants` 验收现在由 API JSON 明确返回 `index.jpg`；烟测要求实际 CDN 请求只包含该地址。该 legacy 验收不宣称当前 API 已满足 302 确定性流程。

## 14. Resolver 优先级与 UI

当前正片成功结果标记 `resolver_verified=True`、`resolver_route=api_redirect`，来源保持 `RouVideoResolver`，置信度保持 100。迟到 Network、DOM、MSE、Direct probe、HTTP 错误和 legacy 结果不能覆盖其清单类型、加密结论、请求上下文或身份。

默认只展示和勾选已验证正片；通用结果放入高级诊断，不默认进入批量下载。缩略图角色确认后，清理旧候选、子清单和已知资源，并按请求 ID / URL 阻止重放重新生成候选；Resolver 失败时仍保留其他普通媒体 fallback。

界面显示页面标题、HLS、Media / Master Playlist、VOD / LIVE、Encryption、DRM、Segment Count、时长、已验证状态和解析链。完整 Trace 默认折叠，包含 API、Location、Index HTTP、PNG Chunk、roUd、flag、压缩、清单及被排除资源，全部脱敏。

## 15. Signed URL 失效与安全续传

明确过期的 Index 返回 `ROU_SIGNED_URL_EXPIRED`。分片连续失败且含签名线索时保留 `POSSIBLE_URL_EXPIRED`；403 本身不证明过期或 DRM，401 / 认证挑战另外分类。

已验证任务保留原合法内存 Profile：关闭查找窗口只释放本窗口 Page / Worker，当前任务会话可继续用于刷新。任务进入 `waiting_context` 后，使用独立隐藏 Page 重跑原播放页 API，读取新的 Location 和 HLS；不打断用户正在使用的查找窗口。

仅接受同 resolver ID、video ID、page URL 的已验证新候选，再由既有 resume identity 校验媒体布局。匹配后保持任务 ID、文件名、暂存文件和 yt-dlp 断点记录，继续未完成分片；身份、布局或质量改变时返回 `RESUME_MISMATCH`，避免拼接错误视频。没有保存的会话、权限失败或刷新失败时保持待更新状态，用户可提供新合法上下文。绝不自行生成新 auth。

## 16. 新增回归

`tests/test_rou_current_protocol.py` 新增 32 项当前协议回归；`tests/test_rou_current_ui.py` 新增 31 项发现、Qt、批量及生命周期回归；`tests/test_rou_runtime_regressions.py` 新增 11 项正文采集、CORS、明确角色和刷新次数回归。已有 Decoder、Browser Observer、Resolver、Resume Identity 回归也更新，总数再增加 5 项。本轮完整单元测试 **902 项通过**，相对此前 823 项基线新增 **79 项**；零失败、零错误、零跳过，记录在 `verification/unit-results.json`。

覆盖三个样本形状、随机目录、多 CDN、完整签名、缩略图 302、PNG 长度/CRC、flag、明文/zlib/raw deflate、解压上限、非法 HLS、标准 AES-128、真实 DRM，以及 Network 先到 / Resolver 后到且正片失败的顺序。旧 smoke 保留原浏览器辅助、legacy、重试和续传验收，明确地址来源。

## 17. 已运行结果与发行验收

完整单元测试 **902 项通过**，0 失败、0 错误、0 跳过；相对此前 823 项基线新增 **79 项**。本地 HLS / AES-128 / DASH / HTTP 集成回归 **31 项**、视频队列与分片并行回归 **9 项**、实际 Chromium 页面操作回归 **11 项**、协议旧判断与站点规则审计 **7 项**均通过。

以下五组验收均重新执行完成，后台模式使用 offscreen，原生模式使用 Windows Qt，窗口最小化运行。

| 运行方式 | 当前 Rou 协议 | 主界面与下载 | 旧 Resolver 兼容 | 批量下载 | 通用自动查找 |
| --- | --- | --- | --- | --- | --- |
| 源码后台 | 17 | 18 | 24 | 13 | 29 |
| 源码 Windows 原生 | 17 | 18 | 24 | 13 | 29 |
| 打包 EXE 后台 | 17 | 18 | 24 | 13 | 29 |
| 打包 EXE Windows 原生 | 17 | 18 | 24 | 13 | 29 |
| 便携 ZIP 迁移 Windows 原生 | 17 | 18 | 24 | 13 | 29 |

每次当前 Rou 协议验收实际生成 **8 个 MP4**，经 ffprobe 及解码画面 SHA256 验证与自生成源一致；包括三视频同名续号、AES-128 PNG、通配 CORS、既有 Image、HTTP500 重试和签名刷新。401 后使用原内存 Profile 重跑 API；CDN 与目录变化后继续原任务，已完成的 2 个分片零重下。每次旧 Resolver 兼容验收有 4 个输出，批量验收有 15 个输出，通用发现验收有 5 个输出。

原有普通 HLS / DASH / Direct、CMD/Bash cURL、3 视频 × 4 分片、请求头继承、Range 隔离和显式继承、广告与缩略图过滤、质量分组、停止与重试均有回归。旧页面 `index.jpg` 必须由 API 显式下发，普通 PNG 无 roUd 时拒绝，不探测相邻清单。真正 DRM 被拒绝，标准 AES-128 不误判为 DRM。

真实 rou.video 页面元数据解析也已完成：API 302 → Index HTTP 200 → PNG IHDR/IDAT/roUd/IEND → flag 0x01 → zlib → HLS VOD，**106 个分片 / 860.333302 秒**，Encryption None、DRM No、RouVideoResolver 已验证 100。脱敏证据为 `verification/rou-live-metadata.json`。本次未在线下载整段真实站点视频；完整下载、并发、AES 与续传使用 FFmpeg 自生成媒体及 localhost 服务器验证。

打包和迁移报告均核对同一最终 EXE SHA256：`343bda5957dea36be5cbc5cc692a673f03088d53a59a433c5913c4da6ca1b6d1`。最终 ZIP 另外执行 CRC、路径和逐文件内容核对，见 `verification/archive-results.json`。完整报告见 `verification/TEST_RESULTS.md` 和 `verification/release-results.json`；修改清单与字节哈希见 `verification/MODIFIED_FILES.md` 和 `verification/modified-files.json`。此前版本及源文件保留。

开发期间的沙箱权限失败和原生监听超时记录保留在 `verification/qa-failures`。本轮原生批量首次在新建查找页面时监听超时，触发原因未确定；同一 EXE 独立完整复测 13 项通过，未修改代码、断言或等待时限。上表通过结果来自本轮完整串行复测，未复用旧 EXE 的通过报告。

## 18. 其他网站与保护边界

站点域名识别留在 `RouVideoResolver.can_handle`，通用核心根据协议和 Resolver 提供的角色处理，不加入 Rou 域名分支。未知站点、普通 HLS/DASH/Direct、cURL 导入、3 视频 × 4 分片并发、MediaGroup、下载全部及原文件名分配继续复用现有实现。上述通用发现、分组和上下文单测已通过，完整旧功能烟测以本轮发行报告为准。

`METHOD=AES-128` 且无 DRM KEYFORMAT 是标准 HLS AES-128，`drm=False`；只有真实 Widevine / PlayReady / FairPlay、CENC、PSSH、ContentProtection、DRM KEYFORMAT 或明确 EME 证据才阻止下载。不会把 `#EXT-X-KEY`、key.dat、加密分片或 HTTP 403 单独当作 DRM。

本实现只处理当前合法浏览器会话获得的普通媒体，不绕过登录、会员、授权校验或 DRM，不猜未下发资源、不破解 CDM、不提取 DRM Key。服务端没有向当前用户返回合法入口时，Resolver 应失败并报告具体原因。
