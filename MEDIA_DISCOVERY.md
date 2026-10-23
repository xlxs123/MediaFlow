# 自动查找媒体源

入口：主界面“自动查找”，粘贴**视频网页地址**，点击“查找”。下方为独立内嵌 Chromium 网页，可以正常登录、播放和拖动。选择候选后点击“确定”，媒体地址和它的实际请求上下文一起送回主界面，再加入队列或开始下载。取消不改变原有输入。

浮窗为主窗口的非模态子窗口，不设置全局 Always On Top。每次查找重新加载网页并清空上一轮结果，监听 60 秒；期间持续捕获播放、拖动及新请求，结束后可再次查找。登录态只保留在当前浮窗的内存 Profile 中，关闭浮窗释放。

## 架构与修改文件

`QWebEngineView / 私有内存 Profile → BrowserNetworkObserver + DOM观察 → 有界事件队列 → MediaDiscoveryService → 候选/评分/详情 → RequestContext → 原有 DownloadQueue / MediaGuard / MediaBridge / yt-dlp`。

| 文件 | 职责 |
| --- | --- |
| `core/browser_runtime.py` | 首次启动前分配本应用私有回环调试端口，显式限制 WebSocket Origin |
| `core/browser_observer.py` | 异步 Qt/CDP 监听实际 request、ExtraInfo、response、redirect 与有限正文，支持 iframe |
| `core/dom_scanner.py` | 读取 video/source/currentSrc/实际分辨率、时长、播放、拖动和具体播放器加密事件 |
| `core/discovery_models.py` | NetworkObservation、MediaCandidate 与脱敏诊断 |
| `core/media_discovery.py` | 内容检测、Manifest资源语义、Variant、去重、Blob关联、广告信号及可解释评分 |
| `core/discovery_worker.py` | 后台解析、有界排队、最多2项现有协议检测、取消与代际隔离 |
| `ui/discovery_dialog.py` | 查找/确定/取消、内嵌网页、默认前12候选、详情及手动选择 |
| `ui/main_window.py` | 新入口，导入候选自身的完整上下文及检测详情，仍使用原下载队列 |
| `core/request_context.py` / `core/http_client.py` | 追加并保留 redirect_chain；派生资源拥有各自独立重定向链 |
| `core/downloader.py` | 追加兼容的 context_snapshot，保留自动查找的原始/最终URL及重定向资料 |
| `core/media_guard.py` | 原有检测详情保留浏览器捕获的重定向链 |
| `main.py` / `M3U8 Downloader.spec` / `build.ps1` | WebEngine启动顺序、资源/进程打包与源码/EXE验收入口 |
| `tests/test_browser_observer.py` / `tests/test_media_discovery.py` / `tests/test_discovery_context.py` | 模拟事件、协议、评分、隐私、取消、管线接口回归 |
| `tests/discovery_fixture.py` / `tests/discovery_smoke.py` | FFmpeg自生成媒体、真实Chromium网页与下载验收 |
| `tools/prepare_browser_notices.py` / `tools/audit_protocol.py` / `tools/package_release.py` | 官方组件许可资料、旧规则/域名分支检查及发布门槛 |
| `README.md` / `THIRD_PARTY_NOTICES.md` / `MEDIA_DISCOVERY.md` | 使用说明、组件许可和本功能交付说明 |

PySide6 6.11.2 已安装 WebEngine 和 WebSockets，不增加 pip 依赖或下载另一个浏览器。Qt浏览器连接方式来自 [Qt WebEngine 调试文档](https://doc.qt.io/qt-6/qtwebengine-debugging.html)，网络事件来自 [CDP Network 协议](https://chromedevtools.github.io/devtools-protocol/tot/Network/)。只连接本应用 page.devToolsId，不读取普通 Chrome 的页面、登录状态或数据库。

## 请求上下文与协议

每个候选独立保存 URL 原文、原始/最终URL、重定向链、HTTP Method、body 和真实 wire headers。Cookie/Authorization 从 requestWillBeSentExtraInfo 读取，不以 DOM 可见 Cookie 代替实际浏览器请求。未捕获完整正文的 POST、缺少可复现请求头的缓存资源保持“上下文不完整”，不能确定导入。

`secure/sign/token/expires/t/auth/signature` 与重复、百分号编码 query 不删除、不重排、不生成。界面候选、日志和详情脱敏；主输入和内部下载使用原文。`Range: bytes=0-` 自动采用已有显式继承策略；其他局部范围保留用于诊断，默认不传播到 key/segment。Origin 仅从实际请求保留，缺少 Origin 的请求仍正常处理。

HLS/DASH 根据内容或已确认响应类型识别。Manifest 先声明 key/init/segment 角色，随后对早到的伪候选回溯移除；`.png/.bin/.dat/无后缀` 分片都按声明的媒体语义处理。清晰度来自 RESOLUTION、BANDWIDTH、CODECS、FRAME-RATE、DASH Representation 或视频真实元数据，不从文件名生成清晰度。

HLS `METHOD=AES-128`、KEYFORMAT=identity/缺省为 `HLS_AES128`、DRM=No。Widevine/PlayReady/FairPlay/明确 KEYFORMAT/ContentProtection/CENC/PSSH 等证据为受保护媒体，确定按钮禁用，并保留原有下载前检测。206 正常；403 是独立 HTTP/认证/签名诊断。Direct 只有 MIME 或 206 时保护状态保持 Unknown，经过原有 MP4元数据检查后再更新；“确定”不替代下载前检查。

## 评分与广告

MediaFlow 真实性分数为 0～100，**不是统计概率**。当前主播放器 currentSrc +40，可见 +20，时长有效 +20，大于5秒 +10，video MIME +10，正常分辨率 +10；隐藏video −30，小于3秒 −30，thumbnail −50，preview −50，图片 −80。汇总全部加减分后截断到0～100。每个新DOM快照重新计算关联，详情逐项显示原因。已验证的当前播放页清单入口可提供互斥的40分关联证据，保持Rou等确定性解析链的既有能力。旧 score 仅兼容协议诊断，不再决定默认选择。完整设计见 [MEDIAFLOW_CONFIDENCE.md](MEDIAFLOW_CONFIDENCE.md)。

VAST 文档的 MediaFile 引用和 `ad-provider.js` 等通用广告脚本、明确广告请求词显著降权。正常页面地址包含这些词、随机签名值包含 ad/vast、download/shadow 等普通词不应把正片判成广告；跨 CDN 本身不降权。默认隐藏广告和低分候选，勾选“显示低置信度 / 广告候选”可检查。启发式过滤不能保证识别每一种广告，用户可自行选择。

全部候选先评分排序，再决定选择：70分及以上自动选择唯一最高分，40–69分手动选择后确认，低于40分不可导入。同分且不能区分正片时要求手动选择。清晰度仅在可信度相同后比较。受保护媒体可看详情但不可确定。用户手动选择后，持续更新不会改选另一个仍可见的候选。

## Blob / MSE

DOM 为 `blob:` 时记录 `BLOB_MSE`，不创建 blob 下载候选。用相同 frame 的实际网络资源及 Manifest关系关联真实源；唯一媒体族才能获得强 currentSrc/MSE 分数，多个媒体族保持独立候选让用户选择。实际 video.mediaKeys/encrypted 只用于关联的具体播放器证据，能力API存在或失败的能力测试不代表DRM。

只有孤立分片、未观察到 Manifest 或完整媒体请求时，无法承诺还原整个视频。内嵌浏览器中也可能遇到登录限制、浏览器兼容限制、需要用户点击播放或浏览器不支持的编码。提示重新播放/查找；手动 URL / CMD/Bash cURL 入口继续保留。

## 异步、取消和边界

Qt 网络和 Chromium 在 GUI 线程异步工作，内容解析在 QThread；现有 HTTP检测最多2项后台并发。监听连接超时8秒，单个正文2MiB、累计16MiB、512请求、128候选、4096声明资源；GUI事件队列另有32MiB正文预算。取消立即关闭浮窗、停止浏览器导航/监听和新增检测，已在途HTTP按取消事件及原有8秒超时退出；不阻塞GUI等线程。每轮查找新建 PrivatePage/CDP target，复用浮窗内存 Profile 保留登录会话；旧页面/iframe/网络、旧socket回调及旧解析代际不会进入新轮，所有退休页面销毁后才释放Profile。真实慢页面连续查找已有回归。

Profile为off-the-record、MemoryHttpCache、NoPersistentCookies，不导入普通Chrome凭据，不保存请求上下文到配置。凭据只用于用户正在查看的媒体，敏感响应体不写盘，不读取许可证正文或DRM密钥。保留 Chromium sandbox、网页同源策略和 TLS 验证。

## 验证与交付

执行完整旧单元与HTTP/队列回归，并运行 `main.py --discovery-smoke verification\discovery`。新测试至少覆盖 Direct206、HLS Clear、AES128、PNG分片、DASH、Blob/MSE、Master多质量、302、签名、VAST+正片、VTT、poster、多个video、真正DRM、空页面；增加跨源iframe、POST、重复查找、取消及实际下载。

浏览器页面来自 localhost：原生 WebM 播放/拖动验证DOM行为，测试网页主动请求 HLS/DASH 验证协议/网络采集；不把本地测试说成特定第三方网站实测。自动查找导入的 AES/PNG/Direct/DASH/POST 继续实际调用原有 yt-dlp/FFmpeg，输出的解码画面哈希与源视频核对。源码、冻结EXE、Windows原生平台及便携包迁移均须重新验收，最终计数和结果见 `verification/TEST_RESULTS.md` / `release-results.json` / `discovery-results.json`。

本次独立发布目录 `dist-discovery`，便携包包含 EXE、tools、文档及Qt/Chromium许可，旧 `dist-parallel` 保留。没有针对特定网站的规则，没有 `#EXT-X-KEY == DRM` 残留；运行层扫描结果见 `protocol-audit.json`。
