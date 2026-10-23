# MediaFlow Resolver 升级报告

> 以下记录此前 NEXT/ev 兼容版的实现与验收。当前版本以 `/api/hls` → 302 Location → PNG(roUd) 为主路线，不猜相邻文件名；完整说明见 `ROU_CURRENT_PROTOCOL.md`，本轮实际结果见发布目录 `TEST_RESULTS.md`。

本次在现有 PySide6、内嵌 Chromium、RequestContext、MediaBridge、yt-dlp 和 FFmpeg 项目上升级。没有另写一套站点下载器。新版位于 `dist-resolver`，旧自动查找版保留在 `dist-discovery`；默认仍同时下载 3 个视频，每视频 4 个分片。

## 1. 修改文件列表

完整逐文件清单见发布目录的 `MODIFIED_FILES.md`，机器可校验清单为 `verification/modified-files.json`。以升级前自动查找版源码 ZIP 作字节比较，记录新增、修改及 SHA256；没有删除既有源文件。

主要涉及 `core/site_resolvers.py`、`resolver_models.py`、`rou_playlist_decoder.py`、`url_policy.py`、`resume_identity.py`、`config_media.py`、`page_tools.py`；原 RequestContext、Manifest、HTTP、Bridge、下载队列、浏览器监听和发现线程；`ui/browser_controls.py`、`discovery_dialog.py`、`main_window.py`；回归夹具、构建和交付校验工具。

## 2. 新增类与模块

| 模块 | 职责 |
| --- | --- |
| `SiteResolver`、`ResolverRegistry`、`RouVideoResolver` | 注册、能力匹配、当前页面解析与失败兜底 |
| `PageContext`、`ResolverRequest`、`ResolveResult` | 页面证据、有限浏览器请求计划、候选和诊断 |
| `RouPlaylistDecoder` | PNG `roUd` 清单解包 |
| `UrlResolutionPolicy`、`resolve_media_url` | 标准 URI 与显式限定的 query 继承 |
| `resume_identity` | 视频身份和分片布局校验 |
| `config_media` | 有界 JSON 配置中的媒体入口发现 |
| `BrowserControls`、`page_tools` | 浏览器导航、缩放、适应窗口、大网页、遮挡和撤销 |

## 3. ResolverRegistry 架构

```text
播放页 → ResolverRegistry → 适用的 SiteResolver
                        ↓ 失败或无适配器
                Generic Media Discovery
                        ↓
            MediaCandidate + RequestContext
                        ↓
             MediaDetector / MediaBridge
                        ↓
             原 yt-dlp → FFmpeg → 输出
```

Resolver 不下载媒体、不持有自己的 HTTP 下载客户端，也不生成凭据。有限请求计划由当前浏览器执行；实际网络响应与完整 wire context 才能生成确认候选。Resolver 在线程中解析，GUI 只负责浏览器和候选展示。未知站点或解析失败继续通用发现；不关联当前入口的 iframe/广告媒体不获得 Rou video ID。

## 4. RouVideoResolver 流程

域名能力匹配仅在此适配器内。`/v/{videoId}` 提取有界 ID，首页返回 `NOT_VIDEO_PAGE`。依次读取当前页面 NEXT 数据、当前同 ID 的 `/api/hls/{videoId}` 实际 JSON/媒体响应；忽略 `kind=thumbs`。由页面或 API 已下发入口生成最多五种同目录候选：`index.png`、`index.jpg`、`index.jpeg`、`index.m3u8`、`master.m3u8`，保留签名，顺序执行并在成功后停止。不遍历未知目录或猜测未下发 VIP 地址。

## 5. __NEXT_DATA__ 解析

兼容 `props.pageProps`、`pageProps` 和带该 script 的 HTML。读取 `video` 及 `ev.d / ev.k`，有界 Base64 解码后按字符码减去页面给出的 `k`，解析 JSON `videoUrl`。不生成服务器 token。非法 Base64、非整数 k、异常 JSON、深度或大小超限返回具体诊断，继续 API / 通用兜底。

## 6. PNG / roUd 解包

先验证 PNG signature，再按大端 length / type / data / CRC 遍历块，校验边界、IHDR、IEND、CRC 和重复 `roUd`。读取 payload flag：bit 0 为压缩时尝试 zlib/raw deflate，未压缩时读取明文。限制 PNG、单块、块数和解压结果大小（清单最多 2 MiB），拒绝截断、尾部垃圾和解压炸弹。最终要求 UTF-8 合法 HLS；必要时为具有有效 HLS 标签的内容补 `#EXTM3U`。普通 PNG 没有有效 `roUd` 不会成为媒体。解码器只附着于明确的清单上下文，不解包 key 或 `.png` 媒体分片。

## 7. Query 继承策略

默认 `STANDARD_URLJOIN` 保持原行为，以最终 manifest URL 作 base，不把父 query 自动传给资源。Rou Resolver 显式启用 `ROU_INHERIT_BASE_QUERY_WHEN_MISSING`，仅相对、同源且没有自身 query 的 URI 继承父清单原始 query；已有 query、显式空 query、绝对 URL 和跨源 URL 都不继承。重复参数、百分号和签名原文保留，不做清洗。不同站点的 token 不会通过全局开关传播。

## 8. RequestContext 策略

原 URL、最终 URL、method、headers、body 和签名完整保存在会话内。派生请求使用 GET，继承实际 UA、Referer、Cookie、Authorization 和已存在的 Origin；从不补造 Origin 或凭据。Range 属于资源字段，默认隔离；复制或捕获完整 `bytes=0-` 请求时可自动启用继承，也能手动开关，分片自身范围优先。浏览器元数据保存用于诊断，派生默认过滤，独立兼容开关可保留。重定向和跨 CDN 仍使用同一上下文规则。

候选附带 allowlist 响应头、数值 timing、resolver/video ID、page URL、发现时间、签名和过期提示。配置、日志和展示脱敏，凭据不持久化。

## 9. Signed URL 刷新与断点续传

失败或停止任务线程退出后，可使用“更新链接并继续”导入新 URL / cURL，或“刷新媒体地址并继续”重开原页面、重跑 Resolver / 通用发现并确认候选。保持任务 ID、文件名、`.part` 和 `.ytdl`。Worker 在写文件或启动 yt-dlp 前校验 resolver/video ID、规范化媒体地址、完整 HLS/DASH 布局；只忽略已知签名参数，保留质量/视频 ID 等资源参数。Direct 校验前缀哈希、强 ETag 和长度。

身份或布局变化返回 `RESUME_MISMATCH`，要求新建任务。匹配后使用 yt-dlp 原断点记录续传。真实夹具已验证两个完成分片不重复请求，更新播放页后通过实际队列按钮也能续传。当前不自动刷新账户，不跨程序重启保存凭据或身份。

## 10. Segment 重试与错误分类

默认总尝试 3 次，包含首次，失败退避 1 秒、3 秒，界面可设 1～10。保留 yt-dlp 的 HTTP / fragment 重试，MediaBridge 按实际资源 URL 和 Range 记录失败请求并封顶，防止混合错误切换重试流程后突破上限；成功后清除该资源失败记录。不会静默跳过不可用分片。

记录 fragment index/total、attempt/max、HTTP 和异常类型。403 本身保持 `HTTP_403`；签名分片连续 403/410 到上限时提示 `POSSIBLE_URL_EXPIRED`，不声称已证明到期。401/WWW-Authenticate 才是认证证据；缺少 Cookie/Authorization 仅用于提示检查。另分 HTTP_ERROR、DNS、ConnectTimeout、ReadTimeout、SSL/Certificate、Proxy、RemoteDisconnected、ConnectionReset、无效页面和 Manifest 错误。AES、key.dat 和 `#EXT-X-KEY` 不构成 DRM 证据。

## 11. Overlay 处理

查找覆盖可视区域大于 40% 的 fixed/absolute/sticky 元素，超过 60% 标为大遮挡。优先高亮 Close/关闭/跳过并滚动到可见位置，从不自动点击。可用关闭按钮存在时禁止隐藏；无可用按钮且不是保护层时，用户可明确点击临时隐藏。动作前再次检查，保存原 style 属性（包括原本没有属性）、根 overflow 和滚动位置，撤销精确恢复。针对 Chromium 延迟 CSSOM 序列化，恢复缺失属性前先完成同步。

保护 video/audio/canvas、播放器及祖先、登录/付款/权限/会员层、iframe；不读取跨源 iframe 文档，不绕过登录、SOP、DRM。适应窗口逐档检测，若缩小到 50% 后仍不可用，会给出真实提示。

## 12. 自动查找

首次输入网页时加载并监听。相同已加载页再次自动查找使用当前 DOM、NEXT、Performance 与有界 Network 历史，不 Reload，不中断播放。DOM / Network / Fetch / XHR / Redirect / MSE 证据进入同一候选表。配置 JSON 扫描有深度、节点、大小、URL 和浏览器请求数量上限，候选必须再经过实际成功响应验证。

Manifest 已声明的 key/segment 不显示为独立正片。孤立 TS/fMP4 按实际字节、MIME、frame 和路径族有界分组，只有孤立分片提示 `ONLY_SEGMENTS_FOUND`，不承诺拼出完整视频。Blob 只用于关联真实网络资源；VAST、poster、thumbnail 被排除或降权。

## 13. 深度查找

已实现独立入口，先创建网络监听、等待 ready，再加载网页。新 Page 隔离旧页面的 DOM 和网络回调，Profile 在本浮窗会话内保留。原浏览器前进/后退/刷新也继续监听。取消停止计时、请求队列、Page/Profile/Worker，晚到结果不会修改主窗口。

## 14. 新增测试

升级前完整单测 607 项，本次完整单测 717 项，新增 110 项。另新增真实 Chromium 页面脚本测试 11 项和 Resolver / 浏览器操作 / 更新链接 / 实际下载验收 23 项。包括要求的 NEXT、API 兜底、roUd 明文/deflate、标准 AES-128、PNG 分片、限定 query、普通 PNG 拒绝、Blob、真正 DRM，以及 CRC/长度/解压边界。

## 15. 测试结果

最终结果见 `TEST_RESULTS.md` 和 `verification/release-results.json`。单测、31 项真实 HLS/AES/DASH/HTTP 回归、9 项队列并行回归、原 18 项 GUI、原 29 项自动发现、新 23 项 Resolver 验收均按本次源码和最终 EXE 校验；EXE 另验收 Windows 原生平台和 ZIP 解压迁移，哈希必须对应当前构建。

下载测试全部使用 FFmpeg 自生成视频与 localhost 服务器，实际运行 yt-dlp / FFmpeg / ffprobe 并比对解码画面哈希。第三方真实播放页未提供，所以不把本地 Router 夹具称为 rou.video 线上下载实测。

## 16. GPL 源码

**独立实现协议解析，没有复制第三方 GPL 源码。** 没有引入新的第三方依赖。随包 yt-dlp、FFmpeg、Qt 和 Chromium 的现有许可与来源说明继续附带；独立实现不免除发行这些组件的许可义务。

## 17. Downloader 域名规则

Downloader、HTTP、Manifest、MediaGuard、MediaBridge 与 GenericDiscovery 无网站域名适配分支。生产代码唯一 Rou 域名规则位于 `RouVideoResolver.can_handle`。localhost 分支只处理回环网络/代理；测试专用 loopback Resolver 不用于生产网站匹配。AST 审计报告 `downloader_site_domain_rules = 0`。

## 18. 旧的 EXT-X-KEY 误判

没有 `#EXT-X-KEY == DRM` 残留。旧问题的根因是把“出现加密标签”混同为“DRM”，或把 403 混同为保护证据；现在解析 METHOD / URI / IV / KEYFORMAT / KEYFORMATVERSIONS，并把 HTTP 失败独立分类。AES-128 identity 为 `HLS_AES128 / drm=false`；ProtectedMediaError 的所有抛出位置均要求 `info.drm is True`。Widevine / PlayReady / FairPlay / PSSH 等明确证据才拒绝。错误提示也不再因为标题或文件路径含这些系统名就显示保护提示，只接受明确的保护错误。

## 19. 扩展名与媒体角色

优先级保持 Manifest 语义 > 实际 HTTP 响应 > 字节特征 / 容器验证 > 扩展名弱提示。清单的 `.png/.bin/.dat/.ts/.m4s/.aac/.mp4/无后缀` 都按 Media Segment 处理。Downloader 接收的 `.m3u8/.mpd` 回环名称只表示已验证协议，不用于判定远端内容。普通 PNG 不含有效清单时拒绝；普通 MP4 也不会仅靠扩展名默认成为正片。

## 20. 实测边界与下一步

- rou.video 首页已明确作为首页处理，仍需要用户有权访问的具体播放页进行线上兼容实测；没有进入随机视频或测试他人凭据。
- 第三方内嵌浏览器/CORS/登录策略、站点未来结构和 token 时效不能通过本地测试保证。可用深度查找或最新 Copy as cURL 继续诊断。
- 断点身份校验偏保守；动态直播或 manifest 布局变化可能需新建任务。相同路径但服务端替换内容、且没有可比较版本证据时，无法从 URI 布局证明所有字节相同。
- 不实现 DRM、会员保护绕过、许可证密钥提取或不可访问媒体地址猜测。

普通站点走通用发现与协议检测；需要 Origin/Referer 的签名站点继承实际请求上下文；没有 Origin、只有 Referer/UA/Range 且使用 enc.key 和 PNG 分片的站点仍走相同 HLS AES-128 管线；Rou 只增加清单解析和限定 query 策略，下载、并行、解密与封装继续共用。
