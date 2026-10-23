# MediaFlow 2.1.0 签名发布流程

正式渠道为 [MediaFlow-Releases](https://github.com/xlxs123/MediaFlow-Releases)，固定清单为其 `releases/latest/download/manifest.json`。正式 Ed25519 公钥已编译进客户端，标识为 `mediaflow-stable-2026-01`。私钥仅在发布者外部 Windows 保险目录中使用，不进入 Git、CI、客户端或发布资产。

复用 2.0.1 的签名、收据、完整性和部署协议。2.1.0 完成真实仓库、DPAPI 保护的正式密钥、受限 GitHub CDN 策略及草稿上传后的重新核验。实际验收结果见 [RELEASE_VALIDATION.md](docs/RELEASE_VALIDATION.md)，发布者入口与密钥保护见 [PRODUCTION_RELEASE.md](PRODUCTION_RELEASE.md)。本地临时测试签名始终与正式发布分开。

## 先完成验收

保留经过实际运行验收的完整发布目录，包含 EXE、build-info.json、tools 和许可证。使用独立发布工具 `scripts/publish_release.py`；客户端不负责保管发布私钥。脚本从任意工作目录均可运行。

1. `receipt --release-dir <完整发布目录> --report <正式EXE验收JSON> --output <新收据JSON>`。
2. 报告必须有 `passed=true` 和 `runtime_identity`，且其 packaged、app_version、build_id、source_sha256 与实际发布目录相符；报告登记的实际 EXE 完整 SHA-256 也必须一致。
3. 收据记录原报告路径、大小和哈希；签名前再次读取核对。失败报告、源码测试报告、空报告、变动的 EXE/报告均不能发布。收据是发布者本地流程约束，不是独立安全签名或防管理员伪造机制；不得人工伪造测试结果。

## 正式密钥独立初始化

由发布者在受控环境**明确执行一次** `init-key --directory <项目外的新凭据目录> --key-id <正式密钥标识>`。交互密码不回显；也可用 `--password-env <专用环境变量名>`，参数中传变量名而非密码。该命令生成加密 PKCS8 Ed25519 私钥和单独的公钥 JSON；已存在目录不覆盖。

构建与 publish 命令绝不会自动重新生成密钥。正式密钥已经通过独立初始化建立；后续发布必须复用经过确认的密钥或执行明确的轮换流程。`publisher_windows_vault.ps1` 以系统随机密码、DPAPI CurrentUser 及受限 DACL 保护加密 PKCS8，签名密码仅短时交给本机子进程。发布者应另行备份保险目录和 Windows 用户恢复资料；不能假定换用户或重装后仍能解密。私钥不得放入任何源码项目、历史版本工作区、发布目录、输出目录、客户端配置或日志。

公钥是 `{"trusted_keys":{"<key_id>":{"algorithm":"ed25519","public_key":"<32字节原始公钥的规范Base64>"}}}`。当前客户端已固定正式公钥，正常使用无需再次导入。其他信任根仍须由用户在“更新与版本管理 → 导入可信公钥”中核对并明确确认。公钥可以公开，私钥和密码不能公开。

## 生成待托管文件

`publish` 必填参数：

| 参数 | 内容 |
|---|---|
| `--release-dir` | 已验收完整发布目录 |
| `--output-dir` | 父目录已存在、目标尚不存在的独立目录 |
| `--package-url` | 发布者真实可用的 HTTPS ZIP 目标地址，禁止虚构 |
| `--published-at` | UTC 秒精度 `YYYY-MM-DDTHH:MM:SSZ` |
| `--key-id` | 与独立公钥匹配的密钥标识 |
| `--trusted-keys-file` | 来自发布/输出目录之外的独立公钥 JSON |
| `--acceptance-receipt` | 上一步绑定实际 EXE 的收据 |
| `--private-key-file` 或 `--private-key-env` | 外部加密 PKCS8 PEM 文件，或含加密 PEM 的专用环境变量名，二选一 |

可选 `--password-env`、`--notes`、`--release-page`、`--minimum-app-version`（默认 1.9.0）、`--package-name`。未指定密码环境时交互式读取。API `publish_release` 支持同名下划线参数；不要把密钥或密码打印到命令历史/报告。

工具只在本地执行：核对收据 → 冻结读取发布文件 → 格式2包内清单签名 → 生成ZIP → 计算ZIP完整SHA-256和大小 → 增加真实URL后签署外部清单 → 独立公钥自验 → 提交到新输出目录。不启动EXE、不连接服务器、不上传，也不会仅因指定一个“stable”标签而发布未验收构建。

输出为发布 ZIP、规范 UTF-8 `release.json`、独立 `signature.json` 和 ZIP `.metadata.json`。客户端通过清单内签名验证；独立签名文件方便发布审计。单独 metadata 哈希或 ZIP 内附公钥都不能成为可信根。

## 接入真实托管平台

当前已使用实际创建的独立 [GitHub 发布仓库](https://github.com/xlxs123/MediaFlow-Releases)。`scripts/publish_github_release.py` 接受已正式签名的资产目录、明确的 owner/name 和发布说明：检查真实账号的仓库归属与管理权限，创建草稿，仅上传明确列出的资产，核对服务器大小和 SHA-256，重新下载应用 ZIP、清单和签名后验签。仅指定 `--publish` 才在全部通过后发布；不覆盖已发布版本或不同内容的同名资产。GitHub 正常凭据由既有登录机制提供，脚本不接收密码或私钥。

客户端拒绝 HTTPS 降级和证书错误。普通 HTTPS 源仍限制同来源跳转；仅内置批准的 GitHub 仓库资产可以跳转到代码固定的官方 Release CDN，且必须保持同一资产和允许的路径。清单不能指定新的跳转白名单，普通确认不能绕过限制。

GitHub 草稿资产无法匿名读取，草稿阶段采用正常授权下载并验证。正式发布后再使用实际 EXE 和系统 TLS 匿名获取公网资产、验签、下载、部署及启动新旧 GUI。本地 localhost HTTPS 成功不能替代这一层验收；最新实际结果单独记录。

## 密钥轮换与撤销

轮换时生成新的 key_id，先通过独立可信渠道向客户端分发新公钥，再使用新密钥签署后续清单。一个 ID 不能在 UI 中被悄悄替换为不同公钥。未知 ID、算法不符、损坏或缺失签名均停止更新。旧密钥是否保留由发布者明确决定；撤销会导致使用该密钥的已存版本记录无法再自动验证，应保留完整原目录供手动启动。远程清单不能自行添加公钥。

签名认证不替代代码审查或实际运行验收，也不改变独立部署、启动确认、数据迁移与应用版本回退的边界。
