# MediaFlow DASH 发布版修复记录

本文件记录 2026-10-13 的真实源码 / 发布 EXE 对照。最终构建身份、网站下载与输出校验的精确结果由 `verification/dash-release/release.json` 和各次验收的 `result.json` 保存。

## 已确认的根因

旧版已经能解析 API 返回的 `data.dash.video[] / audio[]`，但浏览器不采集 HTML Document 的正文，发现服务也只把直接 JSON 响应交给 DASH 解析器。服务端把播放描述直接写入 HTML 的 JSON 赋值时，播放器可以直接开始请求 `.m4s`，无需独立 playurl / MPD 请求。因此旧流程只能看到 Segment。

本次在修改前，用裸地址 `https://www.bilibili.com/video/BV1fKeg6mEhi` 对源码复现：页面正常播放，3 个 Segment、0 个 DASH、0 个可下载候选，页面有内嵌播放信息。证据：`verification/dash-release/baseline-source/B/result.json` 与 `trace/trace.json`。

此前报告的真实 B站下载使用源码脚本、带来源参数的网址及强制深度查找；发布 EXE 当时只验证了本地样本。那不是同输入、同入口的源码 / EXE 对照。不能用此前源码成功推导发布版对所有网页返回形式都通过，也不能仅凭这两次观察断言来源参数是网站选择响应形式的唯一因素。

## 版本与打包检查

- `dist-mediaflow-dash-fix/MediaFlow.exe` 确有 `core.dash_json / core.dash_models`；关键 Python 执行代码与本次修改前源码一致。旧包的浏览器单条采集上限实际为 16 MiB、总量 64 MiB。
- 更旧的 `candidate-flow / candidate-display` 不含 DASH JSON parser，单条上限为 2 MiB。它们和 dash-fix 的版本号此前都为 1.0.0。
- 没有发现用户此前运行中的进程或相关桌面 / 开始菜单快捷方式，无法反推当时实际启动的路径。本次没有将问题归咎于用户打开错版本。
- 新版为 1.1.1。自动查找窗口显示独立构建标识、模块加载状态及实际正文采集上限；“关于”显示实际运行文件。EXE 文件属性、`--build-info` 和 `build-info.json` 也可识别版本。
- 构建脚本在 Analysis 前写入构建时间及源码 SHA256，并在生成 EXE 后实际运行模块加载检查。解析模块显式加入打包配置。

## 最小修复

1. 原生 CDP 正常采集有界 HTML Document 正文，继续使用原来的 16 MiB / 可配置上限及总量限制。增加正文请求 / 接受 / 拒绝 / 失败计数。
2. 从内联 script 的 JSON 数据或 JSON 赋值提取结构化 DASH 描述；不执行 JavaScript、不扫描 CDN URL 拼凑轨道、不按域名分支。
3. API JSON 和 HTML 内嵌 JSON 复用原 DASH parser、轨道选择和内存 MPD。内嵌来源标记为 `embedded:html-dash-json`，派生 Referer 使用实际文档地址。
4. 保持原候选完整性与 DRM 检查。完整 DASH 可手动确认；孤立 Segment 仍不可下载。新增 HTML 描述 → 双轨候选 → 后续 Range 不降级的回归测试。
5. 下载、队列、评分、Resolver、yt-dlp 参数、FFmpeg 合并与输出校验核心保持原代码。最终文件仍必须通过真实 ffprobe 校验。

## 验收方法

`main.py --live-release-check <目录>` 与 `MediaFlow.exe --live-release-check <目录>` 共用相同入口。B站使用两个裸 URL、独立未登录 BrowserProfile、相同 40 秒采集、不导入系统 Cookie。流程是标准“查找” → 真实候选选择 → 点击“确定” → 原信号导入 → 原下载队列 → 原合并 / 校验 → 再次 ffprobe 检查视频与音频。

Rou 的验收明确手动选择该目标页已经验证的 RouVideoResolver HLS 候选，避免把页面上的广告当作正片。此约束只在测试入口，不改变产品评分或选择算法。

## 最终交付与真实 EXE 验收

- EXE：`<project-root>\dist-mediaflow-dash-release-v2\MediaFlow.exe`
- 版本 / 构建标识：1.1.1 / `20261013T104353Z-809e0bac503c`
- SHA256：`e259354438c47e4bcf8bd80fc6536feee6790b1c870dc7b466d626e8ee592a10`
- 单元测试：1549 项全通过；新 EXE 原生 GUI：18 项通过。
- 最终验收状态：**部分通过**。同一最终 EXE 的两条 B站与 Rou 下载通过；抖音复测未通过正片校验。

| 页面 | 内嵌 DASH 描述 | 视频轨 / 音频轨 | 可下载组合 | 全部候选 / 可下载 | 实际输出 | 选择 / 下载 |
|---|---:|---:|---:|---:|---|---|
| B | 1 | 6 / 3 | 6 | 7 / 6 | h264 + aac，96.688 秒 | 通过 |
| A | 1 | 4 / 3 | 4 | 5 / 4 | h264 + aac，40.681 秒 | 通过 |
| rou | 0 | 0 / 0 | 0 | 6 / 5 | h264 + aac，311.334 秒 | 通过 |
| 抖音公开页 | 0 | 0 / 0 | 0 | 页面已发现候选 | 2.6 秒探测片，无音轨 | 当前 EXE 未通过 |

B站 B / A 分别生成 6 / 4 个完整双轨 DASH 候选。两者均在新 EXE 中完成候选选择、点击“确定”、原下载队列、FFmpeg 合并和 ffprobe 校验，输出 MP4 含 H.264 视频与 AAC 音频。Rou 新页面 `cmuw7m0kr000cs6fz3ceohcua` 输出 311.333719 秒，对应用户确认的 5 分 11 秒正片；候选为 `RouVideoResolver` 解码 PNG roUd 后的完整 HLS，实际 MP4 同样有视频和音频。

抖音同一公开页面在较早的不同构建中曾真实下载 275.34 秒 H.264 + AAC 正片，两次新 EXE 独立复测则选中短 Direct 候选，输出约 2.6 秒 HEVC 探测片且无音频，被验收脚本拒绝。进一步的只读诊断中，页面请求了约 33.9 MB 视频和 6.7 MB 音频，但两者以 HTTP 206 返回并被保留为不可下载 Segment；不能凭这些请求宣称新 EXE 能下载正片。其变化与网页/CDN 响应形式有关的可能性较高，但没有足够证据确认唯一原因。本次按原任务约束未修改抖音解析器、下载器和评分逻辑，也没有绕过候选完整性检查。

Rou 早期自动验收在正片前广告暂停时重复点击视频，导致页面跳转；新验收入口允许停止自动点击并等待目标 Resolver 完成。旧失败记录保留。源码与最终 EXE 的 DASH 核心执行指纹、验收入口指纹均一致；最终 EXE 独立目录交付，未覆盖旧程序。

旧版 B站失败的第一处链路缺口是浏览器未采集 Document 正文，发现层没有从 HTML 内嵌 JSON 提取 DASH 描述。旧版源码以 B 裸网址复现 3 个 Segment、0 个完整候选；旧 EXE 已有解析模块和 16 MiB 上限，因此不能把失败归因于模块缺失或仍为 2 MiB。新流程解析 HTML 中 `data.dash`，沿原有 DASH 模型配对视频/音频，下载后得到有效 MP4。

## 验收证据

- 构建、输出哈希、源码与 EXE 指纹及失败边界：`verification/dash-release/release.json`。
- 新 EXE 成功记录：`packaged-v2-B`、`packaged-v2-A`、`packaged-v2-rou`；抖音失败记录：`packaged-v2-douyin`、`packaged-v2-douyin-retry`。
- 较早构建的抖音成功记录：`packaged-douyin-long`，其构建标识不同，不能代替本版验收。
- 修改前复现：`baseline-source/B`；源码对照：`source-final-A`、`source-final-B`；Rou 源码下载：`source-rou-new-full`。
- 全量单测：`verification/unit-results.json`；新 EXE 原生 GUI：`packaged-v2-gui/gui-results.json`。

## 修改文件

- `.codex/finalize-dash-release-v2.py`
- `.codex/verify-visible-release-v2.ps1`
- `MEDIAFLOW_DASH_RELEASE_FIX.md`
- `MediaFlow.spec`
- `README.md`
- `assets/version_info.txt`
- `build.ps1`
- `core/browser_observer.py`
- `core/embedded_media.py`
- `core/media_discovery.py`
- `main.py`
- `scripts/stamp_build.py`
- `scripts/verify_dash_download.py`
- `tests/live_release_check.py`
- `tests/test_embedded_media.py`
- `tests/test_live_release_check.py`
- `ui/discovery_dialog.py`
- `ui/main_window.py`
- `utils/branding.py`
- `utils/build_info.py`
