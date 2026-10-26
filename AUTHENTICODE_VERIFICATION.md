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
| 更新/部署/回滚聚焦回归 | 481 项，0 失败/错误，1 个符号链接权限跳过 | 核心、UI、输出均通过；源码审查与工作流变更另有增量测试 |
| 远程主分支 CI | 482 项更新/签名/部署/回滚回归，零失败、零错误、零跳过；SDK 集成 10/10 | 实际 Windows x64 构建、EXE 启动、190 文件结构与 SBOM 校验通过 |

合法测试签名保持 `0x800B0109`（不受正式信任）；签后篡改实际得到 HashMismatch / `0x80096010`。原 PE 与 Root 信任库未变，测试私钥不可导出。全部失败及通过记录保留。

## 剩余外部条件

发行者需自行提供合法 CA 签发并正确安装的代码签名证书/硬件密钥，以及确认的 HTTPS RFC3161 服务；只需配置公开 thumbprint、存储位置和服务 URL，不向聊天、仓库或普通 CI 提供私钥或密码。未擅自购买证书、初始化生产私钥或修改信任根。

代码签名有效也不保证 SmartScreen 不提示。普通 CI 的未签名构建、测试证书集成、真实正式证书签名与正式稳定发布是四个不同证据层次。

## 已完成的远程验收

实现通过 [PR #5](https://github.com/xlxs123/MediaFlow/pull/5) 正常合并，构建提交为 `b806c405b612c9fb31bd49247c60378a663451d5`。[Windows CI](https://github.com/xlxs123/MediaFlow/actions/runs/40273082480) 与 [安全工作流](https://github.com/xlxs123/MediaFlow/actions/runs/40273082383) 实际成功。CodeQL 未处理告警为 0；公开公钥写入的原完整四条数据流经重新复核，只更新改变的发布脚本摘要绑定。

GitHub Windows SDK SignTool 为 `10.0.26100.7705`，Microsoft 原签名 Valid。远程 TEST ONLY 集成 10/10，原三个 PE 与 Root 信任库保持不变，未导出测试私钥，正式证书状态仍为 `not_configured`。

从真实 GitHub API 下载 371381083 字节构建归档，SHA-256 为 `997857b605cdc1b6235256ccdd7efba54e174247e1e3eecd38238aabd21d3cb6`，与服务器摘要一致。下载后实际 EXE 再次启动并通过 190 文件结构检查。

使用官方 GitHub CLI，对同一提交、主分支、实际运行 ID、固定工作流及 GitHub 托管 runner 条件下的 10 个对象分别验证 OIDC/Sigstore/SLSA 来源证明：主 EXE、全文件构建收据、回归结果、结构报告、四份 SBOM 证据、两份 Authenticode 检查报告。它们均通过；这不是 Windows Authenticode 或发行者 Ed25519 签名。

只读 DryRun **未完成**：首次误用工作区外的运行目录被路径门禁拒绝；随后遗漏本地 MEDIAFLOW_GITLEAKS 配置导致真实扫描用例被跳过并阻断，已补上固定版本工具路径；下一轮本机增量组运行到 900 秒上限而停止，具体耗时原因尚未查明。没有跳过检查、增加超时或继续签署/发布，全部失败记录保留。同一实现的远程 482 项回归和独立下载复核已通过，不能以此冒充本机完整 DryRun 通过。无证书条件下，正式生产签名/发布仍未验收。

正式 v2.1.0 的 13 个资产 ID、大小、摘要与地址均未变；匿名稳定清单 SHA-256 保持 `580c204f3048c3443f51c6199ee25a62bb9f88d158cdced832bc41ae6632bf44`，更新配置 SHA-256 保持 `fd817c8ecd76b8dc9a4e8f341c1ba649b83a5a4bd15ed6af90cf5ae562ceeea9`，源码 v2.1.0 标签对象未变。旧源码/发布基线的 1519 个文件完整摘要未变。没有发布新稳定版本，没有清理用户文件；失败记录继续保留。
