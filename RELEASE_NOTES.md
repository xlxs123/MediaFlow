# MediaFlow 2.1.0

本版建立公开源码、独立发布仓库和正式 Ed25519 稳定更新渠道，补充限定 GitHub Releases 官方 CDN 跳转策略。继续复用 ZIP 安全检查、独立版本部署、启动健康检查与旧版回退；不覆盖旧程序、媒体、登录配置或恢复数据。

下载主 MediaFlow ZIP，解压完整文件夹后运行 `MediaFlow.exe`，保留 `tools` 和 `licenses`。普通用户不需要另行下载所有源码资产。

正式 key_id 为 `mediaflow-stable-2026-01`，原始公钥 SHA-256 为 `fa54ae69f7a14184681a810dd33bd28cc4cdbe2870ff58db75df48503caf11d1`。`manifest.json` 与 `release.json` 字节一致；`manifest.sig` 与 `signature.json` 字节一致。客户端先验签，再校验完整包大小、SHA-256 和目录结构，部署及启动分别确认。

正式 EXE 本地验收：67项媒体流程检查、16项媒体重启；91项本地 HTTPS 签名更新、15项更新重启；提交前后真实进程中断恢复17/15项。复用5个已保存网站样本，不批量重下真实网站媒体。临时测试密钥不作为正式信任根。公网与 CI 的独立实际验收结果见源码仓库 [验收报告](https://github.com/xlxs123/MediaFlow/blob/main/docs/RELEASE_VALIDATION.md)，本清单中的说明不代替该层验收证据。

源码：[xlxs123/MediaFlow](https://github.com/xlxs123/MediaFlow)，采用干净的新初始历史，排除用户数据、实际媒体、浏览器配置和正式私钥。普通 Actions 仅测试与构建，不获取正式私钥。

应用自编源码保留 MIT，外部组件使用各自许可。Release 附两份可独立解压的 BtbN 源码 ZIP、Python-and-yt-dlp 源码包、Qt6.11.2官方完整源码，以及索引、哈希和重建说明。运行包 `licenses/THIRD_PARTY_SOURCES.md` 和 `third-party-sources.json` 记录具体资产与完整性信息。

FFmpeg 固定为可追溯的 BtbN 构建；真实构建日志的98份缓存全部覆盖，另外保留5份启用子依赖，并补273份锁定 rav1e crates及精确 FFmpeg 源码。上游 `cargo update cc` 的历史构建辅助版本未在缓存层记录，不伪造解析记录，不承诺工具逐字节重现。Qt/PySide和完整应用源码允许使用修改后的兼容库重建并运行，不限制为此调试的逆向工程。

2.0.1 尚无本版限定 GitHub CDN 策略，首次采用本正式渠道需要运行新客户端。程序更新与数据迁移分离，兼容数据仍需另行预览确认。本次没有重新测试网站登录或在线媒体发现。
