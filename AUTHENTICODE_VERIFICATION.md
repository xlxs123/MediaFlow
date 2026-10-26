# Windows Authenticode 集成与验证

基线为 `510a9da2d98266f6a7ba5686b710d640d3a28ea3`，稳定版本仍为 2.1.0。本阶段不发布稳定版本，也不修改旧 Release、更新 URL 或 Ed25519 信任根。

## 审计与证书状态

本机实际发现 Microsoft Windows SDK SignTool，ProductVersion 为 `10.0.22621.3233`，Windows 验签状态为 Valid、签名者为 Microsoft Corporation。PowerShell 为 7.6.5。开始审计时，CurrentUser/My 与 LocalMachine/My 均未发现代码签名证书。

**正式代码签名证书尚未配置，MediaFlow 正式 Authenticode 签署与正式发布验收未完成。** 本次创建的短期、不可导出的 TEST ONLY 证书仅用于本地和普通 CI；不加入 Root，不导出私钥，不冒充正式发行者。

## 实现边界

统一入口为 `scripts/authenticode.ps1`，支持 Inspect / Sign / Verify 与 CI / Test / Production。`sign_windows.ps1`、`verify_windows_signature.ps1` 是默认 Production 的薄包装。首次运行可以通过无 ReleaseDirectory 的 Inspect 检查工具及证书配置；这不表示任何 EXE 已签名。

仅签署经构建脚本审计的三个第一方 PE：

- `MediaFlow.exe`
- `tools/MediaFlow.Recycle.exe`
- `tools/webview2/MediaFlow.WebView2Host.exe`

Sign 必须输出到不存在的新目录。链接、重解析路径、用户数据、现有输出和已有第一方签名均拒绝；第三方文件保持原始字节，并记录现有签名状态，不批量重签第三方组件。原始源码、CI 产物与旧版本保持不变。

Production 要求明确证书拇指纹、Windows 证书存储和 HTTPS RFC3161 服务，检查用途、有效期、私钥可用性、链及撤销状态；拒绝自签、TEST ONLY、未知或不可验证证书。没有 PFX/密码命令行或私钥导出接口。SignTool 的 SHA-256 文件摘要与 RFC3161 时间戳参数、原生验证依据 [Microsoft SignTool 文档](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool)。

生产验签同时要求实际嵌入签名、预期 signer、`Get-AuthenticodeSignature` Valid、SignTool `/pa /all /v /tw` 成功，以及 Windows WinVerifyTrust 成功。验证程序显式加载 System32 的 wintrust.dll。Catalog 签名不替代第一方文件嵌入签名；[Get-AuthenticodeSignature](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/get-authenticodesignature) 的 catalog 优先行为已单独处理。

RFC3161 不仅检查 OID。使用完整 `TryDecode` 和 [`VerifySignatureForSignerInfo`](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.pkcs.rfc3161timestamptoken.verifysignatureforsignerinfo) 校验 token 签名、TSA 身份、messageImprint 与当前 signer 的绑定、证书有效期及未来时钟界限，并匹配 Windows 返回的 TSA。SignTool 子进程不会继承 GitHub token、密码、私钥或 Secret 环境变量。

## 与发布流水线的关系

1. 验证同提交安全 CI、Windows 构建、完整文件收据和 GitHub 官方来源证明。
2. 下载并验证同一 unsigned build 的原生 SBOM；其收据摘要及完整文件清单必须与输入构建一致。
3. 在新树正式签署三个第一方 PE，Windows 原生验签通过。
4. 独立证明仅 PE checksum、security directory 和新增 WIN_CERTIFICATE 改变，程序载荷与第三方文件不变。
5. 生成签后真实 EXE 验收、最终 ZIP、准确 SHA-256 和既有 Ed25519 更新清单。
6. 从原 CI BOM 独立派生签后文件身份并绑定最终 ZIP；不改原 CI receipt / generation / attestation，不声称派生 BOM 获得原 CI 的密码学证明。
7. 正式 GitHub 发布器再次从实际最终 ZIP 验证三个 PE，之后才可创建草稿或上传。隔离 TestRelease 只镜像原内容，不能修改稳定渠道。

未配置证书的 DryRun 是明确的 unsigned 只读演练：不找、不打开 Ed25519 保险库，不生成正式签名包，不写 GitHub，`production_ready=false`。提供正式证书时才做完整本地签名演练。只读演练不产生签名恢复点；完整恢复会重验原收据/官方证明、BOM、实际代码签名与文件派生，不能仅信任旧 JSON。

## 已实际验证

| 层次 | 结果 | 范围 |
| --- | --- | --- |
| 本地参数与原生 API 回归 | 20/20 通过，零跳过 | 正式未配置、路径、无覆盖、错误证书及 RFC3161 安全边界 |
| 实际 Windows SDK 集成 | 10/10 通过 | 三个第一方测试签名/验证、错误用途、错误 thumb、篡改、重签及生产拒绝 |
| 签名后的载荷 | 三个实际 PE 均通过字节派生证明 | 不将普通摘要叫作签名验证 |
| 完整程序 | 测试签名后的实际 EXE 启动及 190 个静态文件结构通过 | 仅复制原 CI 白名单，不读取或复制用户数据 |
| RFC3161 | 真实 Microsoft SDK 已有 token 解析验证通过；合成 token 移植/篡改/未来时间拒绝 | 不等同 MediaFlow 正式时间戳服务验收 |
| 生产证书签名 | **未完成：证书未配置** | 不以自签证书或 Microsoft 工具签名冒充 MediaFlow 正式签名 |
| 完整本地回归 | 2507 项，0 失败、0 错误、2 跳过 | 未提供专用 localhost 浏览器样本与当前用户无符号链接权限；后续源码披露增量测试另行通过 |
| 远程 GitHub CI | 待实际运行 | 后续记录实际运行链接，不以本地结果替代 |

合法测试签名保持 `0x800B0109`（不受正式信任）；签后篡改实际得到 HashMismatch / `0x80096010`。原 PE 与 Root 信任库未变，测试私钥不可导出。全部失败及通过记录保留。

## 剩余外部条件

发行者需自行提供合法 CA 签发并正确安装的代码签名证书/硬件密钥，以及确认的 HTTPS RFC3161 服务；只需配置公开 thumbprint、存储位置和服务 URL，不向聊天、仓库或普通 CI 提供私钥或密码。未擅自购买证书、初始化生产私钥或修改信任根。

代码签名有效也不保证 SmartScreen 不提示。普通 CI 的未签名构建、测试证书集成、真实正式证书签名与正式稳定发布是四个不同证据层次。
