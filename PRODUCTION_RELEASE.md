# MediaFlow 2.1.0 正式发布

源码：[xlxs123/MediaFlow](https://github.com/xlxs123/MediaFlow)。独立资产：[xlxs123/MediaFlow-Releases](https://github.com/xlxs123/MediaFlow-Releases)。客户端固定清单：`https://github.com/xlxs123/MediaFlow-Releases/releases/latest/download/manifest.json`。2.1.0 已完成正式发布与实际 EXE 公网验收，分层证据见 [RELEASE_VALIDATION.md](docs/RELEASE_VALIDATION.md)；配置地址本身不作为验收证明。

## 签名和可信根

沿用格式 2 Ed25519：移除仅顶层 `signature` 后，以 UTF-8、无 BOM、无换行、键排序、紧凑 JSON、保留 Unicode、禁止 NaN 得到签名字节；不做 Unicode 归一化。签名覆盖产品、版本、构建、平台、架构、包地址、大小、SHA-256、兼容性、文件列表和 `signing.key_id`。

正式 key_id：`mediaflow-stable-2026-01`。原始32字节公钥 SHA-256：`fa54ae69f7a14184681a810dd33bd28cc4cdbe2870ff58db75df48503caf11d1`。公钥在 `assets/update-channel.json` 中固定并随 EXE 编译，旁置文件或未签名远程内容不能换根。轮换使用新 key_id，经独立指纹确认后发布过渡构建；不复用原ID替换公钥。

## 发布者密钥

正式密钥只在项目和发布目录之外。`publisher_windows_vault.ps1 -Operation Initialize -ConfirmInitialize` 复用既有初始化器生成 Ed25519 加密 PKCS8；高强度随机密码由系统 CSPRNG 产生，使用 Windows DPAPI CurrentUser 和受限用户/SYSTEM DACL 保护。密码不写入脚本、参数或日志，不向普通 CI 提供私钥。已存在的保险目录拒绝重新初始化。

`-Operation Publish` 读取该用户的 DPAPI 保护数据，仅向签名子进程的临时环境提供密码，调用已有 `publish_release.py`，之后清除环境变量。发布者应独立备份保险目录和 Windows 用户恢复资料；DPAPI 不保证换用户或重装系统后可解密。不要把保险目录放进 Git、Release、一般备份分享或 CI artifact。

## 构建与发布门禁

统一的 Windows 发布入口是 `scripts/release_mediaflow.ps1`，支持显式 `-DryRun` / `-Publish`、同提交 Windows CI 产物核验、正式保险库签名、草稿发布与实际客户端公网验收。参数、恢复限制和演练证据范围见 [RELEASE_AUTOMATION.md](docs/RELEASE_AUTOMATION.md)。原有 `release_production.ps1` 继续作为兼容的本地准备工具；它本身不创建 Release。

读取正式保险库之前，同一默认分支提交的最新安全工作流必须全部通过；失败、缺失、跳过或过期证据均阻断。CI 构建还必须用官方 GitHub CLI 验证来源证明，核对源码提交、工作流、实际文件摘要与 run ID；本地未签名收据不能替代。完整扫描、精确误报审核、SBOM 和证明验证说明见 [SECURITY_AUTOMATION.md](docs/SECURITY_AUTOMATION.md)。

1. 在独立工作区固定经确认的渠道、公钥和版本，不修改旧源码或用户数据。
2. 执行相关增量测试，构建到新的完整目录。
3. 实际 EXE 完成本地下载、轨道、媒体库、重启、签名更新、安全部署和回退检查。私人报告不进 Git 或 Release；公开摘要单独脱敏。
4. `publish_release.py receipt` 用真实 EXE SHA-256 与已通过报告生成验收收据。
5. 用外部正式密钥生成并独立自验签最终 ZIP、`release.json` 与 `signature.json`。`manifest.json` 为同一发布清单的字节一致别名，`manifest.sig` 为同一 detached signature JSON 的别名；客户端验证嵌入的签名，旁置签名只用于独立核验。
6. 上传至独立发布仓库的草稿；检查、下载草稿资产并验签/核对最终哈希。草稿受 GitHub 权限保护，无法在发布前进行匿名公开下载。
7. 正式发布后通过匿名真实 HTTPS 再取清单、签名和完整 ZIP，用实际 EXE 执行下载、部署、健康检查和新旧 GUI 启动。只有此层通过才能声明公网端到端验收通过。

`verify_hosted_release.py` 能独立核验远程 ZIP 与已验收目录全部文件，但不代替实际客户端部署和启动。其生产模式只用系统 TLS，不接受测试 CA。

## 下载和数据隔离

仅内置批准的 GitHub 仓库中同一 Release 资产可跳转到固定官方 release-assets CDN；其他 HTTPS 仍只接受同来源跳转，不接收远程白名单。缺失、未知、无效签名与包哈希失败不能通过普通确认绕过。

程序更新部署到独立版本目录，不覆盖运行 EXE、旧版、媒体、恢复文件或浏览器配置。应用回退只启动经登记且摘要一致的旧程序；用户数据迁移仍经现有预览、确认、备份和验证流程，不自动倒退数据库。

旧 2.0.1 尚无此 GitHub CDN 策略；首次采用新渠道需运行新客户端。新客户端对旧安装上下文的升级测试会明确列出真实运行版本，不把它冒充旧 EXE 自身已具备新能力。

## Windows Authenticode 与原生 SBOM 门禁

新的正式稳定发布要求 `-CertificateThumbprint`、`-CertificateStore`、已确认的 `-TimestampUrl`，可通过 `-SignToolPath` 选择经验证的 Windows SDK；`-SecurityPython` 指向安装固定 requirements-security 的独立校验环境（默认 `.security-venv/Scripts/python.exe`）。不传私钥、PFX 或密码。证书未配置时普通 CI 仍可构建，但正式 Publish 在打开 Ed25519 保险库前失败。

无正式证书参数的 `-DryRun` 仅验证已证明 unsigned CI 构建、结构、原生 SBOM 和签名状态，不解锁保险库、不生成正式包、不写 GitHub；报告明确 `production_ready=false`。有正式证书的 DryRun 才完整演练签名与最终包，仍不发布。只读演练无签名恢复点；正式/完整演练 Resume 必须重新验证证书、原 CI/官方证明及最终文件。

原 CI unsigned 收据/BOM/官方证明始终保留。签后采用仅三份第一方 PE 的载荷保留派生；最终 ZIP 形成后生成外部 SBOM sidecar 并关联其实际 SHA-256。第三方不重签，静态/托管依赖未知继续声明 incomplete。参见 [Authenticode 验证](AUTHENTICODE_VERIFICATION.md) 和 [原生 SBOM 覆盖](SBOM_COVERAGE_REPORT.md)。当前生产证书尚未配置，未发布新稳定版。

证书配置前检查、公开参数和未验收边界见 [正式签名就绪](SIGNING_READINESS.md)。本机只读演练的超时诊断与实时日志入口见 [DryRun 根因报告](DRYRUN_TIMEOUT_ANALYSIS.md)；配置检查不能代替实际私钥操作或 RFC 3161 服务验收。
