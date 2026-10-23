# 构建与受控发布

公开源码仓库：https://github.com/xlxs123/MediaFlow 。独立发布仓库：https://github.com/xlxs123/MediaFlow-Releases 。

普通 CI 见 [CI.md](CI.md)：增量测试、独立 Windows 构建、实际 EXE 身份与发布结构检查、短期构建产物。CI 只有 contents:read，不获得正式私钥，也不创建稳定 Release。

正式发布沿用 `scripts/release_production.ps1` 的版本、配置、签名和验收门禁；`scripts/publisher_windows_vault.ps1` 在发布者 Windows 本机加载 DPAPI 保护的外部加密密钥。普通测试和构建不继承签名密码。已有 EXE 验收通过后才生成绑定收据并调用 `scripts/publish_release.py` 签名和自验。

`scripts/publish_github_release.py` 上传至草稿 Release 后，核对所有资产的大小及服务器 SHA-256，并通过正常授权重新下载应用 ZIP、清单与签名进行完整验签。GitHub 草稿不支持匿名公网访问，所以匿名公网验收在正式发布后执行；未执行前不得声明通过。应用程序下载、部署、健康检查、实际新旧 GUI 启动分别记录。任何阶段失败均保留原应用和用户数据。

格式 2 规范、可信公钥和版本目录策略详见 [PRODUCTION_RELEASE.md](../PRODUCTION_RELEASE.md)。
