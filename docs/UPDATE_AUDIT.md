# 2.1.0 更新体系审计

复用 2.0.1 的格式 2 Ed25519 签名、规范化 JSON、完整 SHA-256、ZIP 安全检查、独立版本部署、真实 EXE 健康检查与旧版回退。

源码仓库：https://github.com/xlxs123/MediaFlow 。独立发布仓库：https://github.com/xlxs123/MediaFlow-Releases 。固定清单为发布仓库 `releases/latest/download/manifest.json`。

2.1.0 增加限定 GitHub Releases CDN 策略：仅内置批准渠道所属仓库、同一资产、GitHub 官方 release-assets 主机及规定路径；其他 HTTPS 来源保持同源限制。清单不能更换信任根或选择重定向白名单。

正式私钥位于项目、发布文件和 CI 之外；加密 PKCS8 密码由 Windows DPAPI CurrentUser 与受限 ACL 保护。客户端只嵌入公钥。普通 CI 不签正式发布包。

应用更新不迁移或清理用户数据，失败后保留旧版本。启动新版与迁移数据仍为不同操作。单元测试、本地 EXE 验收与公网验收分别记录，不由本文件宣称验收完成。
