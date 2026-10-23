# MediaFlow WebView2 捕获与登录持久化

保持 `Qt UI → QProcess JSON 管道 → .NET Framework STA 宿主 → Edge WebView2`。沿用 SDK 1.0.4258.31 / Evergreen Runtime，未新增 Python 依赖或网站解析器。

## 媒体捕获

- 导航前启用 Network；发现时暂时禁用该页面 HTTP 缓存以采集实际请求头，不清除 Cookie 或网站存储。
- 捕获 requestWillBeSent、responseReceived、ExtraInfo 及完成/失败事件；Media、Fetch、XHR 共用同一存储器。
- 有意义的响应 Content-Type 补充空/generic CDP MIME；晚到的 ExtraInfo 更新 MIME 和长度。后缀只作线索，由原协议/内容解析器确认。
- 关联 iframe、dedicated/shared/service Worker 在监听就绪后恢复执行，捕获早期请求。停止发现会解除等待；不扫描无 document 的 Worker，不伪造播放器关联。
- 实测 WebView2 的 Worker request/response 带 SessionId，但其 ExtraInfo 可能发往主会话。仅在相同 requestId 的唯一关联 Worker 响应明确要求 ExtraInfo、主会话没有实际请求时对齐；歧义或陌生会话不合并。
- DOMContentLoaded 即可送出播放器信息，不等待慢图片；只采集实际 Runtime 默认 frame context，过期上下文回调丢弃，避免早期无 frame 快照被当成第二份旧文档。
- 浏览器诊断显示总请求/响应、Fetch/XHR/Media、媒体相关请求、关联子目标、当前候选数；不输出 URL、Cookie 或 Authorization。

链路仍为 `_NetworkStore → DiscoveryWorker → MediaDiscoveryService → MediaCandidate → 原可信度/质量评分`。不强行提高分数，不降低原选择门槛，下载核心不变。

## 本地 Profile

正式应用固定使用 `%LOCALAPPDATA%\MediaFlow\BrowserProfile`，按需创建，共用一个环境和 `MediaFlow` 非 InPrivate Profile。关闭发现窗口不删除，退出程序等待宿主及浏览器关闭，重启复用。旧配置和下载数据路径不变。

设置 → 工具与下载设置：显示目录和“清除浏览器数据”。先关闭发现并等待下载/刷新结束；确认默认“否”。原生层再次检查无页面，通过 `CoreWebView2Profile.ClearBrowsingDataAsync(AllProfile)` 清除。清理期间拒绝新页面/Cookie 操作，完成后 UI 才提示成功，不递归删除目录。

Cookie 与网站存储仅在本地管理，不上传/同步、不自动读取系统浏览器账号、不启用 OS 单点登录。持久 Cookie / LocalStorage 可保存登录；SessionStorage 按页面会话结束，Session Cookie 与服务端期限不被改写，不能保证网站主动终止的登录仍有效。

官方依据：[WebView2 User data folders](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/user-data-folder)；清理 API 和非 InPrivate 选项已按锁定 SDK XML 文档验证。

## 验证入口

```powershell
.\.venv\Scripts\python.exe tests\run_unit_tests.py
.\.venv\Scripts\python.exe main.py --browser-capture-smoke verification\capture --native-smoke
.\.venv\Scripts\python.exe main.py --browser-profile-smoke verification\profile --native-smoke
.\.venv\Scripts\python.exe main.py --webview2-live verification\online --native-smoke
```

三个独立应用进程使用 localhost 合成 HttpOnly 持久 Cookie 和 LocalStorage，验证重开、重启、取消清除、繁忙拒绝、确认清除及再重启；不登录用户账号。捕获测试使用 FFmpeg 自生成媒体，包含无后缀 URL、慢资源、Worker 第一条 Fetch、HLS/DASH/AES-128 PNG 分片。原 Rou / 评分 / 质量 / 下载 / 封面验收另行保留。

公开网页只使用用户提供的 B站、抖音和 Rou 首页，禁用所有 Site Resolver。捕获到资源不等于确认正片；低于原门槛的候选仍仅在高级诊断显示。Rou 首页没有播放器，不能代替 Rou 单视频实测。实际数字和限制见发布目录 `TEST_RESULTS.md` 以及 `verification/browser-plus-release-results.json`。
