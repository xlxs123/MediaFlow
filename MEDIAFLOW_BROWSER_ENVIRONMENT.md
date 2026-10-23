# MediaFlow 内嵌浏览器诊断与增强

> 本文保留 Qt WebEngine 版本的历史诊断。当前 Windows 默认已迁移 WebView2 并启用本地持久 Profile，见 [MEDIAFLOW_WEBVIEW2.md](MEDIAFLOW_WEBVIEW2.md) 和 [MEDIAFLOW_BROWSER_LAYER.md](MEDIAFLOW_BROWSER_LAYER.md)。

入口：`dist-mediaflow-browser\MediaFlow.exe`。在“自动查找”浮窗点击“浏览器诊断”。请携带整个发布文件夹，包括 `tools`。

## 已确认的架构与问题

- 内核是 PySide6 的 `QWebEngineView / QWebEnginePage`，不是 Electron、CEF 或 WebView2。
- 实际 PySide6 / Qt WebEngine 为 **6.11.2**，Chromium 为 **140.0.7339.225**；版本由安装的 Qt API 读取。
- 页面使用匿名、off-the-record `QWebEngineProfile`，内存缓存与非持久 Cookie；已验证任务可在应用内保留原会话。关闭应用后不保存登录状态。
- `video`、`HTMLVideoElement`、`MediaSource`、`navigator.mediaCapabilities` 均存在。但本次构建的 H.264 / AAC `canPlayType` 为空，对应 MSE 格式为 false。VP9 / Opus WebM、AV1 的格式查询返回支持；格式声明与实际播放分别记录。
- HLS / DASH 清单没有原生 `canPlayType` 支持。网页播放器仍可自行解析清单，再通过 MSE 提交受支持编码的媒体；“MSE 存在”不能证明 HLS / DASH 一定可播放。
- 原网络监听已经采用 CDP，包含 `Network.requestWillBeSent`、`Network.responseReceived`、ExtraInfo、重定向、子框架与有界响应正文读取。此次复用现有实现，新增诊断接口与事件计数。

**主要限制没有被 UA 调整解决：当前 Qt 构建缺少 H.264 / AAC。** 本次没有伪造媒体能力，也没有通过更改外部 FFmpeg 来宣称浏览器已支持这些编码。Qt 官方说明这些专有编码需要构建时启用 `-webengine-proprietary-codecs`；普通版本更新也不保证会启用。[Qt WebEngine Features](https://doc.qt.io/qt-6/qtwebengine-features.html)

下一步如需让依赖 H.264 / AAC 的页面正常播放，需要提供带相应编码支持、版本和 ABI 匹配的 Qt WebEngine 构建，或另立任务评估 WebView2。替换浏览器还需要适配网络监听与会话接口，本次按“小范围修复”要求保留现有内核。

## 本次改动

1. HTTP UA 移除 `QtWebEngine/…`，保留真实 Chromium 主版本与 Windows 平台。真实 HTTP 请求 UA 与 `navigator.userAgent` 已核对一致；不伪装为未安装的新版本，不改写 `navigator.platform`、Client Hints 或编码检测 API。
2. 显式启用 JavaScript / localStorage，使用 Qt 的桌面播放手势策略。没有关闭 Chromium sandbox、CORS、TLS 或网页访问限制。
3. 增加诊断面板：内核版本、UA / Client Hints / 平台、Video、各格式 canPlayType、MSE、decodingInfo、video readyState / error、Cookie / localStorage / sessionStorage、CDP Fetch / XHR / Media 请求和响应计数、候选数量。
4. 探针仅创建自己的随机存储测试项，读写后清理；不导出已有 Cookie、页面存储内容或请求凭据。解码查询设超时，异常与未知不会被写成“不支持”。CDP 尚未启动时可在 Qt 隔离脚本世界运行同一探针。
5. 增加可选的 Edge / Chrome / Firefox Cookie 导入，以及 Netscape Cookie 文件导入。必须由用户主动点击；仅接受当前页面 host 与有效父域，拒绝公共后缀、无关域、过期条目和异常输入。
6. 系统来源调用现有 yt-dlp 的离线 infojson 流程导出，不访问目标页面、不下载媒体、不改变原下载器参数。临时明文 Cookie 文件读取后清理，进程支持取消和超时，仅终止本次启动的进程树。
7. Netscape 格式不能完整表示 SameSite，当前 yt-dlp 的 Firefox 导出也不能完整保留 HttpOnly 元数据。系统来源统一按 HttpOnly 导入，避免未知属性降为脚本可读；依赖 JS Cookie 的网站可能需要内嵌重新登录。手动文件导入保持文件的显式 HttpOnly / host-only / Secure / path / expiry 语义，由 Chromium 最终决定是否接受。
8. Chrome / Edge 的系统加密或锁库失败会明确提示，不增加破解或绕过处理。可使用内嵌网页登录或用户已有的 Cookie 文件。

系统浏览器 localStorage / sessionStorage 不会导入。导入 Cookie 也不能修复缺失的解码器，不保证登录、验证或媒体播放一定成功。

## 公开页面实测

2026-10-11，Windows 原生 Qt 浏览器视图 1120×840，独立未登录会话，每页监听 45 秒；未读取系统 Cookie，未调用站点 Resolver。为后台验收关闭 GPU 加速，不关闭网页安全策略。本轮没有下载第三方媒体文件。

| 页面 | 页面与播放器结果 | 网络与候选结果 | 结论 |
|---|---|---|---|
| 用户提供的 B站 `BV1CjaQ6NE42` | 文档 HTTP 200、loadFinished=true；实际复现“不支持 HTML5”提示；未观察到 video 初始化 | CDP 收到 169 个响应，其中 Fetch 46、XHR 17；媒体响应 0、候选 0 | 监听正常工作；当前编码能力不足，UA 修改后仍未恢复播放器 |
| 用户提供的抖音 `jingxuan?modal_id=7695103317542967537` | 文档 HTTP 200、loadFinished=true；3 个 video 的 readyState 仍为 0；页面出现验证相关文字 | 实际媒体响应去重后 19 个；生成 11 个候选；存在请求失败 | 媒体请求已捕获，不能把未播放归因于监听失效；未确认指定正片正常播放或所有候选均属于目标视频 |
| `https://rou.video/` 首页 | 文档 HTTP 200、loadFinished=true；当前首页没有 video | 捕获普通请求与 XHR；媒体响应 0、候选 0 | 首页加载测试通过；首页不具备播放页验收条件，不能据此宣称 Rou 在线视频播放已通过 |

三页 `MediaSource` 均存在，H.264 / AAC MSE 查询均为 false。B站修改前与修改后的测试均无媒体请求，不能将此次 UA 优化宣传成 B站播放修复。在线资源数量受页面内容和运行时间影响，不是稳定测试断言。

证据：`verification/browser-environment-baseline.json`、`verification/browser-online-native-before.json`、`verification/browser-online-native-after.json`。旧的无视图初探报告仅作调查记录，以上最终在线报告使用真实浏览器视图、HTTP 状态与去重的媒体响应统计。

## 验证与改动范围

单元测试与原生 / 打包测试的最终数字见 `verification/browser-environment-release-results.json`。

- 能力探针回归：固定字段、类型、未知、超时、隐私过滤、协议与编码区别。
- Cookie 回归：有效父域 / 公共后缀、host-only / IP、HttpOnly / Secure / expiry、数量与体积限制、错误信息、取消 / 超时、临时文件清理。
- Cookie 实际工具测试：现有 yt-dlp.exe + 临时合成 Firefox SQLite，离线导出成功；不接触用户真实浏览器配置。
- 原生浏览器环境验收：首次查找前异步诊断、实际 WebM 播放、HTTP 与 JS UA 一致、Fetch / XHR / Media 捕获与候选生成、存储不被覆盖、主动导入合成 HttpOnly Cookie、H.264 / AAC 实际播放失败诊断、窗口关闭回收。
- 原 MediaFlow 回归：多 video / 隐藏预加载 / 正片切换、真实性评分、媒体详情、手动清晰度选择保持、实际 HLS 下载与 720p 输出、ffprobe 验证及本地 JPEG 封面。
- SHA256 审计：原下载核心、Resolver、HLS / DASH、队列、真实性与质量模块、外部 yt-dlp / FFmpeg / ffprobe 保持原字节；旧发布 EXE 保留。当前改动不需要配置、缓存或历史迁移。

修改 / 新增文件：

| 范围 | 文件 |
|---|---|
| 浏览器设置与 CDP 诊断 | `core/browser_runtime.py`、`core/browser_observer.py` |
| 能力与可选 Cookie 后端 | 新增 `core/browser_capabilities.py`、`core/browser_cookies.py` |
| 诊断 UI | 新增 `ui/browser_environment.py`；修改 `ui/discovery_dialog.py` 的设置、入口与关闭回收 |
| 测试 | 新增 `tests/test_browser_capabilities.py`、`tests/test_browser_cookies.py`、`tests/test_browser_environment.py`、`tests/browser_environment_smoke.py`；更新 `tests/test_browser_observer.py` 的 Profile 测试替身 |
| 打包与说明 | `main.py` 新增验收入口、`build.ps1` 追加新文档和验收、`README.md`、本文档 |

没有新增网站专用解析器、域名匹配规则或浏览器框架；没有修改 MediaCandidate、下载核心、yt-dlp 调用链、FFmpeg 处理、媒体真实性评分或质量推荐算法。

复现命令：

```powershell
.\.venv\Scripts\python.exe tests\run_unit_tests.py
.\.venv\Scripts\python.exe main.py --browser-environment-smoke verification\browser-environment-native --native-smoke
.\.venv\Scripts\python.exe main.py --mediaflow-smoke verification\browser-compatibility-regression-native --native-smoke
```

验收入口只使用本地自生成媒体和独立测试配置；系统 Cookie 导入需要在普通 UI 中主动选择。
