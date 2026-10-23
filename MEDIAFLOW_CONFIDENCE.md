# MediaFlow 媒体真实性评分

本次仅调整媒体发现、排序、选择和诊断。未新增网站 Resolver，未修改下载核心、yt-dlp 参数、FFmpeg、HLS/DASH、Rou 解码及请求上下文策略。

## 评分与选择

`MediaCandidate.confidenceScore` 为0～100整数，`confidenceReasons` 为中文原因列表。原 `score` / `score_reasons` 保留兼容历史协议诊断；不再把“解析成功”直接等同“当前正片100分”。

| 证据 | 分数 |
|---|---:|
| 当前主播放器 `video.currentSrc` | +40 |
| 对应播放器可见 | +20 |
| 有限且大于0的实际时长 | +20 |
| 时长大于5秒 | +10 |
| 响应 `Content-Type: video/*` | +10 |
| 正常分辨率 | +10 |
| 隐藏video | −30 |
| 时长大于0且小于3秒 | −30 |
| 明确thumbnail资源 | −50 |
| 明确preview资源 | −50 |
| 图片响应 | −80 |

先汇总所有加减分，再截断到0～100。分数是规则证据强度，不是统计概率、授权状态或下载成功保证。纯评分器不发请求、不改候选、不授予下载资格。

流程：采集全部候选 → 重新关联当前DOM/协议证据 → 评分 → 按分数排序 → 同分比较实际清晰度 → 决定是否选择。

- **≥70**：可自动选择唯一最高分且通过既有下载资格检查的候选。
- **40–69**：保留供手动选择，“确定”或“下载全部”时再次确认；取消则不导入、不入队。选择全部清晰度时也检查展开后的候选。
- **<40**：只在高级诊断显示，禁止导入/批量入队；提示播放正片后重新检测。
- 多个同分主播放器无法消歧时，不自动选第一个，也不默认勾选批量下载。用户可以明确选择。
- 明确当前播放器时默认只勾选其正片组；可见推荐视频即使达到70分，也不会被附带勾选。用户仍可使用全选、全不选及复选框控制批量范围。
- DRM、广告、缺失请求上下文、未验证协议或HTTP错误继续受现有检查约束；高分不能覆盖这些限制。

## 播放器与协议证据

DOM观察仅只读采集。当前播放器的判断优先采用唯一焦点、实际播放进度，再采用显著更大的可见区域；同等播放器不按DOM顺序猜测。祖先的display/visibility/opacity及视口相交参与可见性判断。暂停本身不扣分。

每次快照清空旧关联并重建，因此切走的旧 `currentSrc` 不会一直保留40分。晚到网络响应也不会恢复已失效的当前关联。视频poster属于视频的辅助信息，不等于该视频是thumbnail。

已有确定性Resolver对当前播放页的清单入口完成实际协议验证时，可提供**与currentSrc互斥的40分入口关联证据**。这是保留现有Rou能力的通用语义，不增加域名分支。比如Rou清单有有效且大于5秒时长、暂未取得DOM信息时为70分；仍独立保存协议已验证状态。已验证正片保留原先的优先权，其他通用诊断候选在高级列表中不默认勾选。

已解码HLS/DASH的协议语义高于PNG传输MIME或文件后缀。Rou的signed `index.png` 和 `manifest_text` 继续分别传递，HLS中的PNG分片继续是Media Segment。真实性评分不重新解释下载类型。

## 修改文件

| 文件 | 变化 |
|---|---|
| `core/media_confidence.py` | 新增纯评分器、原因及分层门槛 |
| `core/discovery_models.py` | 追加两个字段，诊断同时保留新旧分数，下载payload保持原结构 |
| `core/dom_scanner.py` | 可见性、焦点、进度和主播放器歧义观察 |
| `core/media_discovery.py` | 每次重新评分，按真实性排序，保留低分诊断及协议优先权 |
| `core/media_groups.py` | 组内评分优先于清晰度，40分可用/70分默认门槛 |
| `ui/discovery_dialog.py` | 列表分数、逐项原因、确认弹窗、同分/推荐/批量选择 |
| `main.py` / `build.ps1` | 新增源码及EXE的MediaFlow验收入口与文档打包 |
| `tests/test_media_confidence.py` | 46项评分规则回归 |
| `tests/test_mediaflow_discovery.py` | 30项服务/协议/分组整合回归 |
| `tests/test_mediaflow_ui.py` | 10项Qt选择及确认回归 |
| `tests/test_media_groups.py` / `tests/test_rou_current_ui.py` | 更新新门槛与新列文案，保留已有协议/下载检查 |
| `tests/mediaflow_smoke.py` / `tests/discovery_fixture.py` | 真实Chromium多video、隐藏预加载、切换、暂停、同分、poster测试 |
| `tests/discovery_smoke.py` / `tests/resolver_smoke.py` / `tests/batch_smoke.py` / `tests/batch_fixture.py` / `tests/rou_current_smoke.py` | 历史验收采用显式选择/确认，保留真实下载与容器检查 |
| `README.md` / `MEDIA_DISCOVERY.md` / `BATCH_DOWNLOAD.md` / 本文 | 当前入口和行为说明 |

## 验证范围

完整单元测试、真实Chromium和下载验收分别记录；最终汇总见 `verification/mediaflow-release-results.json`。

Rou使用正式解析器及本地服务复现API302、PNG/roUd/zlib、真实分片和下载输出，下载通过实际ffprobe及解码画面哈希检查。抖音使用普通网页视频形态的合成NetworkObservation/DOM回归，明确不是本轮在线抖音下载实测。多video与预加载采用FFmpeg自生成视频和真实Chromium `HTMLVideoElement`，没有伪造DOM元数据或写入虚构分数。

验收使用独立的 `verification/mediaflow-*` 目录，不删除之前的媒体、报告或发布版本。发布目录为 `dist-mediaflow`，应整体携带。

## 本轮额外发现的既有问题

测试曾用 `.mp4` URL返回真实WebM：单独下载成功，但在输出目录已有同基名 `.mp4` 时，原yt-dlp源文件命名可能把已有文件当作已下载源，再执行跨容器remux。此问题位于既有下载命名链，未按本任务修改下载核心。失败证据保留在 `verification/mediaflow-batch-r3/batch-results.json`。最终批量夹具使用真实VP9/Opus MP4，浏览器和下载端收到同一份内容，并保留了原有MP4同名文件不覆盖的检查。这个额外问题需要单独修复；本轮分数不保证消除下载核心已有的文件命名问题。
