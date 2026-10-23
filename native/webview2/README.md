# MediaFlow WebView2 原生子窗口宿主

这个 helper 使用 .NET Framework 的 STA WinForms 消息泵和 WebView2 Controller，
把网页作为 Qt 提供的 HWND 的子窗口显示。它不创建可见顶层窗口、HTTP/WebSocket
监听端口、网页可调用的 HostObject 或 WebMessage 桥接。

## 构建

在项目根目录执行：

```powershell
.\scripts\build-webview2.ps1
```

构建器使用 Windows 自带的 .NET Framework x64 `csc.exe`，不需要终端安装 dotnet SDK。
锁定 Microsoft.Web.WebView2 **1.0.4258.31** 的官方 NuGet 包，校验固定 SHA256 后，
每次从包中新解压 `net462` Core.dll 和 `win-x64` WebView2Loader.dll。
已有正确缓存时可用 `-Offline`，不再联网。

来源：[官方 NuGet 包](https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4258.31/microsoft.web.webview2.1.0.4258.31.nupkg)

SHA256：`56F7F4B8BF9AEE4B8EFEFBBDD4F67D5F74EBD1B100ED0806DA71BF76AF481AA9`

输出在 `tools/webview2/`：

- `MediaFlow.WebView2Host.exe` 和 `.exe.config`
- `Microsoft.Web.WebView2.Core.dll`
- `WebView2Loader.dll`
- `LICENSE.txt`、`NOTICE.txt`

Windows 需要可用的 Evergreen WebView2 Runtime 和 .NET Framework 4.6.2 或更新版本。
应用应隐藏启动 helper，并重定向 stdin/stdout；helper 是 `winexe`，没有控制台窗口。

## IPC

stdin/stdout 都是 UTF-8，无 BOM，每条消息一行 JSON。stdin 通过 Win32 `ReadFile`
按换行组帧，避免 .NET ConsoleStream 在 Qt QProcess 管道上的连续读取问题。stdout 仅为应用内部协议，
不是日志。网络头、Cookie、脚本结果可能属于敏感数据，不应原样记录。

请求：`{"id":1,"op":"...",...}`，id 为非负整数。

成功：`{"id":1,"ok":true,"result":...}`。

失败：`{"id":1,"ok":false,"error":"STATIC_CODE"}`。
非预期 SDK 失败可额外带固定类型名 `errorType` 和整数 `hresult`，不含异常 message、
堆栈、页面 console 或输入内容。启动失败使用 id=-1、`STARTUP_FAILED`。

### 生命周期和窗口

1. `initialize {userDataFolder,persistent?}` 创建环境，结果为
   `{backend:"WebView2",runtimeVersion,userAgent,persistent}`。
   `persistent` 缺省 false：目录必须是系统 Temp 下的新建或空目录，保持原临时模式。
   `persistent=true`：接受本地磁盘绝对路径且末级目录名为 `BrowserProfile`，允许重新打开非空目录。
   正式路径由 Python 指定为 `%LOCALAPPDATA%/MediaFlow/BrowserProfile`；本地测试可使用
   verification 下独立的同名目录，不能将系统 Edge/Chrome User Data 当作此目录。
   每个 Profile 使用独立 helper；Python 负责共享同一个持久 Profile，避免多个 helper 抢占目录。
   helper 不打开用户的 Edge/Chrome 配置；清除可覆盖 UDF/浏览器参数的进程环境变量，
   并检查实际 UDF 没有被外部策略改到其他路径。
2. keeper 使用 SDK 明确支持的 `HWND_MESSAGE`，始终不可见，不能重挂为可见网页。
   所有控制器均为 `ProfileName="MediaFlow"`，临时模式 `IsInPrivateModeEnabled=true`，
   持久模式 `IsInPrivateModeEnabled=false`；初始化检查实际 Profile 模式与请求一致。
   关闭可见页面不关闭 keeper，因此同一个 helper 内的登录会话仍在。
3. `create {page,parentHwnd,width,height,visible}` 创建 Qt 子窗口，结果为
   `{userAgent,runtimeVersion,persistent}`；初始页为 about:blank，不自动导航。
   `parentHwnd` 是十进制字符串，也接受 `0x` 十六进制。尺寸为物理像素。
   helper 先在本 STA 的隐藏 HWND 上创建 Controller，随后把 wrapper 改为 WS_CHILD
   并挂入 Qt HWND，经 `IsChild` 检查后才允许显示。初始化时隐藏的窗口不会独立显示。
4. `navigate {page,url}` 允许 HTTP(S) 和 about:blank。监听器在创建成功前已订阅；
   Python 应先完成 `Network.enable`，再 navigate，避免漏掉首个请求。
5. `stop {page}`、`resize {page,width,height}`、`visible {page,value}`、
   `zoom {page,factor}`（0.25–5）、`focus {page}` 返回 true。
6. `close_page {page}` 幂等。异步 create 开始前即登记页面，早到的 close 标记取消；
   晚到的 Controller 会立即关闭，同名新页面不会被旧请求移除。
7. `close` 关闭本次全部 Controller 和 keeper，等待自身浏览器退出最多 3 秒，
   回复 true 后结束 helper。若初始化尚未结束，先等待它完成（最多 10 秒）。
   stdin EOF 执行相同关闭流程。不会调用 taskkill 或结束用户浏览器。
   临时 UDF 由 Python 创建者在进程退出后清理；持久目录保留，helper 不递归删除任何目录。
8. `clear_browsing_data {}` 不需要 page 参数，仅清除 keeper 所属的本应用本地 Profile。
   当 `pages.Count != 0`（含正在创建或隐藏的页面）时返回静态 `PROFILE_IN_USE`。
   空闲时等待 `CoreWebView2Profile.ClearBrowsingDataAsync(CoreWebView2BrowsingDataKinds.AllProfile)`
   完成后才回复 true；清理期间拒绝新建页面和 Cookie 操作，同样返回 `PROFILE_IN_USE`。
   它清除该 Profile 的站点数据、Cookie、缓存、历史等，不删除持久目录，不操作系统浏览器 Profile。

导航事件：

```json
{"event":"load_started","page":"page-id"}
{"event":"url_changed","page":"page-id","url":"https://example.test/"}
{"event":"load_finished","page":"page-id","success":true}
{"event":"process_failed","page":"page-id"}
```

跨进程子窗口获得键盘焦点时，Controller 原生 `AcceleratorKeyPressed` 仅转发
Ctrl+加号、Ctrl+减号、Ctrl+0 和 Esc：

```json
{"event":"shortcut","page":"page-id","key":"zoom_in"}
```

key 可为 `zoom_in`、`zoom_out`、`zoom_reset`、`escape`，由 Qt 调用原有控制。
三个缩放键标为 Handled；Esc 同时保留网页关闭菜单或退出视频全屏的原生行为。
这些事件来自原生键盘输入，网页没有可调用的 IPC 接口。

浏览器内自动文件下载被取消，下载业务仍走原应用管线。
新窗口请求被处理为同一 Controller 的 HTTP(S) 导航，其他外部 URI scheme 被取消。
网页不能直接启动外部程序。脚本对话框不弹出独立窗口。

### CDP

请求：`cdp {page,message:{id,method,params,sessionId?}}`。
正常 IPC 回复的 `result` 内是原 CDP id 对应的响应：

```json
{"id":12,"ok":true,"result":{"id":7,"result":{}}}
{"id":12,"ok":true,"result":{"id":7,"error":{"message":"CDP_METHOD_FAILED"}}}
```

订阅事件从 `event:"cdp"` 转发，参数保留实际 CDP 数据，不写磁盘：

```json
{"event":"cdp","page":"page-id","message":{"method":"Network.responseReceived","params":{}}}
```

预先订阅 Network 请求/响应/ExtraInfo/完成/失败/缓存/dataReceived、Runtime 执行上下文、
Target attach/detach/create/destroy/info/receivedMessage、Page frame/loading/lifecycle 常用事件。
不订阅 `Runtime.consoleAPICalled`、console 日志或网页异常日志。

子 frame/worker 通过 `Target.setAutoAttach` 或 `Target.attachToTarget` 的 `flatten:true`
建立 session。命令使用 SDK 的 `CallDevToolsProtocolMethodForSessionAsync`，转发事件使用
`CoreWebView2DevToolsProtocolEventReceivedEventArgs.SessionId`。顶层事件省略 sessionId。
这两个 API 均已从锁定包的 net462 SDK XML 核对。

### JavaScript 和 Cookie

- `eval {page,script,world}` 使用 `Runtime.evaluate`，`awaitPromise=true`、
  `returnByValue=true`，结果是实际 JS 值，undefined 为 null。
  world=0 在主世界执行；其他整数通过 `Page.createIsolatedWorld` 建立隔离世界。
  不授予 universal access。导航或 executionContextsCleared 后重建上下文。
- `add_script {page,script}` 使用 `AddScriptToExecuteOnDocumentCreatedAsync`，返回 script id。
- `set_cookie {name,value,domain,path,secure,httpOnly,session,expires,sameSite}` 使用共享
  Profile 的原生 CookieManager。session=true 设置 `DateTime.MinValue`；其他情况 expires
  是 UNIX 秒数。sameSite 允许 None/Lax/Strict，缺省 Lax。返回是否实际读回匹配条目。
- `get_cookies {url}` 仅接受 HTTP(S) URL，返回原生匹配 Cookie 数组，字段与 set_cookie
  相同，session Cookie 的 expires=null。保留原生 HttpOnly、Secure、SameSite。

Netscape 文件和部分系统浏览器导出无法恢复全部 SameSite/HttpOnly 元数据，Python
导入层负责注明限制并执行保守映射（未知 SameSite 使用 Lax；系统导出缺失 HttpOnly
时按 HttpOnly 导入）。宿主不会从缺失信息臆造原浏览器属性。

持久模式由 WebView2 按浏览器语义保存允许持久化的 Cookie、localStorage、IndexedDB 等数据。
不会自行导出或重放 sessionStorage，也不会把 Session Cookie 改成持久 Cookie；关闭页面或
宿主后，Session 数据是否仍有效由浏览器和服务器决定。持久 Profile 不保证登录永不失效。
两种模式均关闭系统账号 SSO、密码自动保存和表单自动填充，不读取系统 Edge 登录账号或启用自动同步。

## 已执行验证

锁定 SDK 散列校验和 .NET Framework x64 编译通过。真实本地 Runtime
154.0.4258.62 的 keeper-only `initialize → close` 已通过，取得真实 User-Agent，
helper 正常退出。另已运行真实 Qt QProcess 连续请求及 Qt HWND 最小诊断，
initialize/create、后续多条请求及关闭均通过。该验证没有访问站点或读取用户系统
浏览器 Profile；媒体业务验收由应用的 `tests/webview2_smoke.py` 执行，
不能把编译结果等同于这些验收。
