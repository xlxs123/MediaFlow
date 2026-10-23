# MediaFlow WebView2 迁移

## 可行性与实现边界

原实现为 PySide6 6.11.2 / Qt WebEngine / Chromium 140.0.7339.225。当前 Qt 发行构建缺少 H.264/AAC 播放能力，修改 User-Agent 无法补充解码器。

Windows 默认浏览器改为 `Qt QWidget → 原生子窗口 → Microsoft Edge WebView2`。Qt 继续负责地址栏、按钮、媒体卡片、队列及详情面板。独立 .NET Framework x64 宿主持有 STA WebView2 COM 对象，Python 通过 QProcess 标准输入/输出 JSON 管道调用它；无需 pythonnet 或 UI 框架迁移。

宿主使用 Microsoft WebView2 SDK **1.0.4258.31**，从官方 NuGet 下载、校验固定 SHA256，再以系统 C# 编译器构建。使用本机安装的 **Evergreen WebView2 Runtime**，不复制用户 Edge 程序或配置。开发构建所需 SDK 包缓存于 `native/webview2/.sdk`，不进入发布目录。

## 分阶段迁移

1. 原生宿主嵌入 Qt 子窗口，负责物理像素尺寸、显示与焦点。
2. 页面导航、JS 回调、隔离脚本世界、缩放及后退/前进复用原 Qt 控制层接口。
3. 通过 `GetDevToolsProtocolEventReceiver` 和 `CallDevToolsProtocolMethodAsync` 监听原生 CDP；关联 iframe / Worker 使用 SessionId 及 `CallDevToolsProtocolMethodForSessionAsync`。
4. 原生 CDP 转成原有观察器消息，继续使用同一个 `_NetworkStore → DiscoveryWorker → MediaCandidate`。保留实际请求头、query、重定向、POST body、受限响应读取及旧页面隔离，不改 Resolver、可信度、质量评分或下载核心。
5. 保留 Qt WebEngine 兼容后端。Windows 发布包默认 WebView2；`MediaFlow-Qt.cmd` 可启动 Qt 兼容模式，开发环境可设置 `MEDIAFLOW_BROWSER_BACKEND=qtwebengine`。offscreen 验收继续使用 Qt；原生 WebView2 验收必须使用 Windows HWND。

网络监听独立于 `video.src`，捕获 Fetch、XHR、Media、响应及子框架；同一媒体发现服务继续按响应内容和 manifest 语义判断资源，扩展名不决定下载管线。

## 会话与 Cookie

正式应用共用 `%LOCALAPPDATA%\MediaFlow\BrowserProfile` 及非 InPrivate Profile。隐藏 keeper 保留合法会话；关闭发现窗口仅释放页面。程序退出等待宿主及浏览器关闭，不删除固定目录。持久 Cookie / LocalStorage 可跨程序重启，SessionStorage 和会话 Cookie 不被导出或改写期限。独立测试仍默认使用临时 InPrivate，显式测试配置使用其目录下的 BrowserProfile，不访问真实用户数据。

Cookie 由原生 CookieManager 管理，保留域、路径、Secure、HttpOnly 与有效期。原有主动导入当前页面所属域 Cookie 功能继续使用；Netscape 格式不含 SameSite，按 Lax 处理。默认不读取系统浏览器账号数据，不绕过登录或页面验证。

管道不对网页开放，不建立 CDP TCP 端口；禁用 host objects、WebMessage 宿主桥、自动外部下载与外部协议启动。日志与诊断不输出原始 Cookie、Authorization、签名或网页 console。

## 运行和打包

入口为 `dist-mediaflow-browser-plus/MediaFlow.exe`，移动时携带整个目录，包括 `tools/webview2/`。系统需要 Microsoft Edge WebView2 Evergreen Runtime 和 .NET Framework 4.6.2 或更高版本；本机使用 Runtime 154.0.4258.62。

官方 Runtime 下载：https://developer.microsoft.com/microsoft-edge/webview2/ 。
官方 SDK：https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4258.31 。

构建：`powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 -OutputDirectory dist-mediaflow-browser-plus`。
仅重建宿主：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-webview2.ps1 -Offline`。

配置文件、下载历史路径和已有下载工具保持原样，迁移不修改用户配置。旧发布目录保留。

## 验收记录

迁移基线保留于 `verification/webview2-release-results.json`；本次捕获与持久化详见 [MEDIAFLOW_BROWSER_LAYER.md](MEDIAFLOW_BROWSER_LAYER.md) 和 `verification/browser-plus-release-results.json`，包含完整单元测试、本地真实 H264/AAC / MSE / MP4 / HLS、保留会话与原下载流程、冻结 EXE 和授权公开网页的实际结果。网页验证、HTTP/网络问题和播放器编码问题分别记录；首页没有 video 不被认定为监听失败。

HLS / DASH 播放仍取决于网页播放器、编码和服务端响应；MSE 支持不意味着浏览器原生直接播放所有 manifest。迁移不保证所有网站都能在无需登录或验证的情况下播放。
