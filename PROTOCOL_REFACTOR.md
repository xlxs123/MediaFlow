# 站点无关媒体解析重构

核心链路：URL 或 CMD/Bash cURL → RequestContext → ContextHttpClient → ManifestInfo → MediaInspection → MediaBridge → yt-dlp。

## 多视频与分片并行

应用品牌已迁移为“MediaFlow”，覆盖 HLS、DASH 与 Direct。默认 3 个视频同时下载、每个视频 4 个分片，界面当前可分别调整 1～10 / 1～16。分片数发送给本机 yt-dlp 的 `--concurrent-fragments`，普通 Direct 仍按对应协议下载。

`DownloadQueue` 在 Qt 主线程维护 FIFO 和每任务 `DownloadJob`；跨线程信号使用 QueuedConnection。入队时深拷贝 DownloadRequest，单独保存请求和脱敏器。每个运行任务拥有自己的 DownloadWorker、MediaBridge 随机端口/令牌与 Windows ProcessTree，停止一个不影响其他任务。

completed 记录安全结果；finished 才释放线程、腾出槽位并启动下个任务，防止线程收尾时超出并发上限。暂停只停派发；停止全部先暂停再取消，避免取消期间启动等待项；失败/取消重试等待原线程结束后排到队尾。降低并发上限不会杀掉已有下载。

同输出目录使用 Windows 大小写规则区分队列重名，追加安全编号；空文件名自动生成并避开已有 MP4/part。旧低层 DownloadRequest 的默认命名行为保持，GUI 队列入队时才分配具体名称。没有删除输出或部分文件。任务凭据不持久化，移除已结束或退出时释放快照。

本次实际修改：core/downloader.py、新 core/download_queue.py、ui/main_window.py、ui/app_setup.py、utils/config.py、tests/test_downloader.py、新 tests/test_download_queue.py、tests/test_ui.py、tests/test_security.py、tests/hls_fixture.py、新 tests/integration_queue.py、tests/gui_smoke.py、M3U8 Downloader.spec、build.ps1、启动开发版.vbs、README.md、本文、tools/package_release.py。配置新增两个有界整数，改名后读取旧安全偏好并保存到新路径；构建 spec 的 Qt ICU 修复保留。

真实并发验收以自生成 AES-128 PNG 分片为源，记录 1 / 4 分片请求重叠、3 个视频及第四项 FIFO 补位，不同 headers / Range / 缺少 Origin 的任务并行成功；对所有输出检查音视频/时长和解码 SHA256。真实进程树的单项取消、重试与全部停止都有回归。GUI / 原生 EXE / 解压迁移分别执行任务队列验收；报告见 queue-integration-results.json 与 TEST_RESULTS.md。

输入仅解析文本。远端媒体由通用 HTTP 客户端获取并按内容检测；yt-dlp 通过当前任务的随机 loopback 端口/令牌读取已检测的 HLS/DASH/Direct。桥接是为了统一 method/body、请求继承和派生资源诊断，不引入站点 extractor 分支。只接受已注册媒体资源，不提供传入任意目标 URL 的代理接口。Cookie/Authorization/body 和注册表只在进程内存存活；下载结束关闭服务。

## 原误判原因

旧 media_guard 对任何非 NONE 的 EXT-X-KEY/SESSION-KEY 直接抛 ProtectedMediaError，误将标准 AES-128 identity 视为 DRM。旧 friendly_error 又把 `encrypted` 文字当成受保护证据。现已删除这两条规则；ProtectedMediaError 仅由结构化 ManifestInfo.drm == True 或结构化 CENC/PSSH 元数据触发。HTTP 403 的判定不走 DRM 分支。

## 三类站点请求的流程

**场景 A：动态签名 + 浏览器上下文。** 完整 cURL 自动生成 RequestContext；原 sign/t/token/expires/auth/signature query 不重编码、不裁剪。原始 manifest 使用复制出的 method、headers 和 body。重定向保留请求上下文，307/308 保留 body，301/302 POST 与303转GET。最终 manifest 内容决定 HLS/DASH/Direct，变体、key、segment 和 DASH init/fragment 使用 GET、最终 URL 为 base，按下述分类继承会话上下文，Origin 仅保留原有值。403 时显示 HTTP_403、当前缺失字段、签名及跨域检查线索；401/WWW-Authenticate 归 AUTH_REQUIRED，明确过期时间归 EXPIRED_URL。ANTI_HOTLINK/AUTH_REQUIRED/EXPIRED_URL 可以作为待检查原因，不把推测写成事实。

**场景 B：普通 HLS AES-128。** 真正解析 Key 的五个属性，缺省 KEYFORMAT 是 identity；METHOD=AES-128 → HLS_AES128、drm=False。用户例子中的逗号后换行 IV 先合并为同一属性列表。key.dat 和分片用最终 manifest URL urljoin，通过原浏览器上下文正常获取；由本机 yt-dlp 完成标准 HLS AES-128 下载，FFmpeg 合并为 MP4。没有网站域名条件。明确 Widevine/PlayReady/FairPlay/未知 DRM KEYFORMAT/CENC/PSSH 时保持拒绝，不提取 DRM 密钥、请求许可证或调用 CDM。

相对 URI 按 URI 标准解析；父 manifest 的 query 原样用于它自身，资源 URI 自带的 query 也原样保留，不盲目把父签名附加到不同资源。检测详情的 URL/query 全部脱敏。

**场景 C：伪装扩展名分片。** `EXT-X-PLAYLIST-TYPE:VOD` + AES-128 `enc.key` + `EXTINF` 后 `abc000.png` / `abc001.png`，输出 protocol=HLS、playlist_type=VOD、encryption=AES-128、drm=false、资源 role=Media Segment。资源 URI 不按后缀过滤，`.bin` / `.dat` / 无后缀 / 其他名字走相同路径。即使 HTTP 标 `image/png`，桥接仍按 HLS 分片语义传输，原始远端 URL 不变；实际解密与封装由本机工具按内容处理。

第三站补充确认三类请求仅含 Referer、User-Agent、Range: bytes=0-，没有 Origin、Cookie、Authorization。因此输入不要求这些缺省字段，跨站也不伪造。GUI 导入 CMD/Bash cURL 的完整 `bytes=0-` 时自动开启 Range 继承，沿子清单/key/分片保留；分片自己声明的范围优先。高级设置可关闭，恢复既有隔离模式。RequestContext / DownloadRequest 新增尾部可选 `inherit_range=False`，旧调用保持原行为。

之前 Range 缺口的根因是两层限制：RequestContext.derive 把所有 Range 当成资源专用头过滤，MediaBridge 又无条件清除 key 的 Range。现两层统一使用继承策略；If-Range/条件头/正文头继续隔离，浏览器元数据开关与 Range 独立，没有按域名判断。

HTTP 响应和错误上下文记录实际发出的 Range，资源局部范围覆盖后不再显示父 Range。派生请求 HTTP/网络失败保留已解析 Manifest 的 DRM 结论；初始 manifest 403 未完成解析仍显示 Unknown。隔离模式丢失原 Range 的 403 会提示检查继承开关，作为线索而非断言服务器拒绝原因。

解析对象保留旧 ManifestInfo.playlist_type 属性 Master/Media/MPD，新增 hls_playlist_type 和 MediaResource 列表。结构化诊断用 manifest_type 表示清单结构，显式 VOD/EVENT 时 playlist_type 表示播放类型；旧调用的构造参数和属性不变。支持用户单独 `EXT-X-KEY:` 与后续 METHOD/URI/IV 多行写法。

## 请求头分类与资源语义

| 类别 | 派生请求策略 |
| --- | --- |
| User-Agent / Referer / Cookie / Authorization | 当前媒体声明资源继承；凭据不写入诊断或磁盘。 |
| Origin | 仅继承原请求显式提供的值，不自行计算或伪造。 |
| Range | 由 inherit_range 决定：开启保留原值用于 key/分片/子清单，关闭隔离。分片自身 Range 优先；key 不接受分片局部 Range。GUI 导入 bytes=0- 自动开启，其他部分范围不自动开启。 |
| If-Range / 条件请求 / Host / 正文头 | 不从父资源复制；GET 派生请求不发送原 body。 |
| Sec-Fetch-* / sec-ch-ua* | 保存并用于原 manifest；派生请求默认过滤。高级设置可显式开启兼容转发，与 Range 策略独立。 |
| 自定义请求头 | 保留用户提供的认证/兼容上下文，避免固定白名单丢失 X-API-Key 等字段。 |

Manifest 语义 > HTTP Content-Type / 响应信息 > 内容特征 / 容器探测 > 扩展名。初始化、完整、部分、预加载分片分别记录；Master 子清单单独记录。字节范围保存清单原文，由下载器计算隐式 offset，不从原 manifest Range 猜测。

## 变更文件

- `core/request_context.py`、`core/errors.py`、`core/http_client.py`：统一请求、错误和重定向。
- `core/curl_parser.py`：CMD/Bash method/body 与普通 URL 输入；保留原 tokenizer 和 Shell 安全限制。
- `core/manifests.py`：内容检测、Key 属性、分类、DASH/PSSH/结构化 MP4。
- `core/media_guard.py`：协调有限检查，仅明确 DRM 拦截；保留旧 inspect_manifest/check_media 调用兼容。
- `core/media_bridge.py`、`core/downloader.py`：上下文统一传输、HLS/DASH URI 重写、参数数组及后台诊断；保留重试/停止/文件确认。
- `utils/security.py`：结构化诊断脱敏，保留数字/布尔值和协议分类，相对 Key URI/data URI 凭据隐藏。
- `ui/main_window.py`、`ui/dialogs.py`：统一粘贴入口、method/body 会话状态、检测详情。
- `tests/test_curl_parser.py`、`test_request_input.py`、`test_manifests.py`、`test_request_context.py`、`test_http_client.py`、`test_media_guard.py`、`test_downloader.py`、`test_ui.py`：新增或更新协议回归。
- `tests/test_media_bridge.py`：真实 HTTP 检查分片语义、误标 MIME、key 不携带 Range、当前分片独立 Range 与显式浏览器元数据兼容。
- `tests/test_observed_range.py`：第三站的精确三头组合、206、跨主机、重定向、Master 子清单、分片 Range 覆盖及关闭继承后 key 403 非 DRM。
- `tests/hls_fixture.py`、`tests/integration_hls.py`、`tests/gui_smoke.py`：自建真实 AES/HLS/DASH/PNG 分片服务、按头类别校验、解码哈希、三大 DRM/PSSH 拦截和原生 EXE 验收。
- `README.md`、`PROTOCOL_REFACTOR.md`、`tools/package_release.py`、`verification/*`：使用说明、改动及实际验证/打包报告。

本次 Range 补充实际修改：core/request_context.py、core/http_client.py、core/downloader.py、core/media_bridge.py、ui/main_window.py、ui/dialogs.py、utils/security.py；对应的 test_request_context/test_http_client/test_downloader/test_ui、新 test_observed_range、hls_fixture/integration_hls/gui_smoke；README、本文及 tools/package_release.py、verification 报告。Manifest 解析器与 cURL tokenizer 无需改动。

build.ps1 保留 OutputDirectory 参数，默认 dist 兼容原行为；本次使用 dist-range，旧 dist / dist-protocol / dist-segments 保留。spec 的 Qt ICU 修复保留；没有添加运行库，没有更改配置白名单或进程树取消接口。tools/audit_protocol.py 和 protocol-audit.json 记录旧误判、域名分支与按分片后缀判断的残留检查。

## 验收证据

用户四类回归均有对应测试：精确 AES-128 换行 IV → false；manifest/key/segment 的必要会话 headers 继承；403 → 非 DRM 错误；Widevine/FairPlay/PlayReady → true 且不获取 DRM key。新增初始化分片 PSSH 拦截，明文 DASH Template 与音视频合并，无后缀 API、POST body、跨 origin、重定向以及 HEAD 兼容。第三个场景以 VOD AES-128 PNG 分片验证：三类资源仅凭 Referer/UA/Range 实际下载，206、跨主机、307 和解码哈希匹配；保留旧 Range 隔离服务的真实下载回归。独立分片 Range 与浏览器元数据兼容另有真实 HTTP 回归。

所有网络下载验收使用 FFmpeg 自生成视频及本地服务器。通过 ffprobe 检查视频/音轨/时长，并比较 AES-128 解码画面 SHA256。测试数量和 EXE SHA256 以 `verification/TEST_RESULTS.md`、`release-results.json` 为准。未提供具体网站 URL，因此未声称访问过或验证过用户的实际网站。

## 明确限制

SAMPLE-AES identity/未知普通加密显示 drm=Unknown，目前下载器不支持，使用 UNSUPPORTED_MEDIA，不报 ProtectedMediaError。外部 DASH xlink Period 无法完整检测时明确停止。HTTP403只显示有依据的诊断，不猜测服务器未公开的防盗链或签名算法。
