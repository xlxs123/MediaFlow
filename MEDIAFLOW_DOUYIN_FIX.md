# MediaFlow 1.2.1 抖音样本对照修复

独立目录：`dist-mediaflow-douyin-verified`。原 `dist-mediaflow-recovery` 1.2.0 稳定版不覆盖，留存逐文件 SHA-256 清单用于核对。首轮诊断构建 `dist-mediaflow-douyin-fix` 及其失败记录另行保留，不作为最终发布入口。

先使用原有持久化 BrowserProfile 采集 A/B；修改前诊断见 `verification/douyin-compare/DIAGNOSIS-BEFORE-PATCH.md`。B 的可见主播放器正常播放约 19.25 秒；Range 请求已捕获，失败发生于单次 206 被当作孤立 Segment，之后同时失去 MSE 关联和完整资源探测机会。本次同样条件下 A 也复现该问题，未把历史成功或 2.6 秒误选当作当前观测事实。

修复只扩大有充分证据的通用 progressive MP4 补探：首段必须包含完整 moov、实际视频轨道与样本表、正时长及末尾 mdat 的明确资源长度，排除 mvex/moof、初始化片段、TS 和清单所属分片。已有 check_media 使用同一合法请求上下文的去 Range 副本，返回同资源、相同前缀、相同总长且 DRM 检查通过，才恢复 Direct 资格。长度/ETag/前缀变化会撤销旧证明和在途结果。

Blob/MSE 关联另行判断：同一页面与 frame 内，资源的内在时长和尺寸必须与包括隐藏预加载在内的全部播放器形成唯一匹配；存在同规格、未知资源、未知播放器或竞争清单则保留歧义。完整moov已证实为纯音频，或内在尺寸/时长明确不匹配的预加载轨道可以排除，仍保留其Segment资格，不会强制晋升。诊断标注 `unique_intrinsic_mp4`，这是受约束的元数据推断，并非 API 作品 ID 与传输 URL 的确定性映射。沿用原真实性评分；音频不会绑定 VIDEO，隐藏播放器不能覆盖主播放器。SPA 作品身份切换清空旧候选及分析代际。

B站 DASH、rou HLS、评分器、质量推荐、下载、任务恢复及 FFmpeg 核心保持原实现。

## 本地脱敏采集

若启动时提示 yt-dlp 检测超时，展开主界面顶部“工具与下载设置”，点击“选择 yt-dlp.exe”，选择本发布目录的 `tools\yt-dlp.exe`。随包工具已核对 SHA-256；本次没有自动改动原个人工具偏好。

先关闭其他 MediaFlow 实例；以下命令使用已有应用 BrowserProfile，设置与任务隔离，不导出 Cookie/Token/签名 URL。输出目录必须不存在，每个样本独立启动一次：

```powershell
.\MediaFlow.exe --douyin-compare .\diagnostic-A --sample A --seconds 60
.\MediaFlow.exe --douyin-compare .\diagnostic-B --sample B --seconds 60
```

这是临时诊断入口，页面播放数秒后自动暂停以防自动切到其他作品。默认仅采集；加入 `--download` 才通过实际界面选择及原队列下载，并对最终 MP4 执行 ffprobe 与全文件 FFmpeg 解码校验。输出目录含 A/B.json；无需提供账号密码或 Cookie。

## 本次验收

最终独立 EXE 的构建标识、A/B 网络统计、下载及 B站/rou/本地协议/恢复回归见 `verification/douyin-compare/DELIVERY.md` 和 `release.json`。源码测试不作为 EXE 验收替代。
