# 正式 Authenticode 配置前就绪检查

本阶段没有正式 MediaFlow 代码签名证书，**正式 Authenticode 签名、RFC 3161 服务协议验收和生产发布尚未验收**。产品版本仍为 2.1.0，现有 13 个稳定资产及 Ed25519 信任配置不变。Microsoft 的 SignTool 签名、隔离测试证书和 GitHub 来源证明均不替代 MediaFlow 的正式签名。

## 实际检查结果

- 只读查询 CurrentUser/My、LocalMachine/My 的 Code Signing 证书公开属性：正式非测试证书 0，既有明确标记 `MediaFlow Authenticode TEST ONLY` 的证书 4；未导出私钥，未修改任何 Root 信任库。
- 本机 SDK SignTool 10.0.22621.3233 的 Microsoft Windows 签名有效；工具可用仅证明工具身份。
- 缺少正式 thumbprint 的 `Inspect -Mode Production` 必须以 `certificate_not_configured` 非零结束。不存在的 thumbprint、格式非法、测试/自签名、错误 EKU、过期、无私钥或链/撤销不可验证均不能满足生产门禁。
- 配置专用报告明确 `configuration_only=true`、`private_key_operation_tested=false`、`timestamp_service_protocol_tested=false`。URL 形态通过仅标记 `configured_not_probed`，没有实际 TSA 服务成功的声明。
- 本机已执行 Authenticode 参数与拒绝路径测试；正式签名集成只能由配置后的真实发行环境完成。隔离测试证书集成的真实 CI 结果见最终验收记录，不能外推为生产可用。

## 复用的安全门禁

| 项目 | 既有实现要求 |
| --- | --- |
| 证书身份 | 明确指定40位十六进制 SHA-1 thumbprint 和 CurrentUser/LocalMachine 的 My 存储；不自动挑另一张证书 |
| 证书能力 | 显式 Code Signing EKU、有效期、私钥属性；Production 拒绝测试/自签名证书；实际密钥提供程序操作仍须通过真实签署确认 |
| 链和撤销 | Production 使用 X509Chain Online、NoFlag、ExcludeRoot、15 秒 URL 获取限制；链不可信、撤销或无法确认不能放行 |
| 时间戳 | 明确的无凭据 HTTPS RFC 3161 URL；真实签署使用 SHA-256；验签验证 token 签名、与 signer 的绑定、imprint、证书时间窗、TSA EKU 和可信链 |
| 第一方范围 | 仅 `MediaFlow.exe`、`tools/MediaFlow.Recycle.exe`、`tools/webview2/MediaFlow.WebView2Host.exe`；输出到新目录，第三方保持原字节/签名 |
| 独立验签 | 实际 embedded signer、预期 thumbprint、Get-AuthenticodeSignature、SignTool `/pa /all /v /tw` 和 WinVerifyTrust 都必须成功；目录签名不足以替代嵌入签名 |
| 载荷完整性 | 原 unsigned CI 收据/证明保留，仅允许三份 PE 经审核的 checksum/security directory/certificate append 差异；其余载荷及第三方必须一致 |
| 最终产物绑定 | 签后独立 SBOM 派生、最终 ZIP 大小和 SHA-256、Ed25519 清单及 ZIP 内三个实际 PE 的验签保持一致 |
| 发布顺序 | 同提交安全工作流、官方来源证明和正式证书检查通过后，才允许既有 DPAPI 保险库发布路径；普通 CI 没有生产私钥 |

实现入口仍为 `scripts/authenticode.ps1`、`scripts/authenticode_native.cs`、`scripts/release_security.py` 和现有发布器，没有建立第二套签名系统。

## 发行者的一次性配置流程

1. 在受保护、已授权的发行环境中安装来自受信任 CA 的真实代码签名证书及所需 CSP/KSP/HSM 驱动，并为执行签名的账户配置最小私钥使用权限。证书采购、申请和私钥安装由发行者自行完成，本工具不购买、不导入 Root、不导出密钥。
2. 由发行者在证书管理界面独立确认公开 thumbprint、My 存储位置、证书身份、有效期及授权签名账户；用 CA/时间戳服务商确认的 HTTPS RFC 3161 地址。不要把 PFX、密码、HSM PIN 或令牌交给聊天、源码、普通 CI 或命令行日志。
3. 配置只含公开选择信息的环境变量：`MEDIAFLOW_AUTH_THUMBPRINT`、`MEDIAFLOW_AUTH_STORE`、`MEDIAFLOW_AUTH_TIMESTAMP_URL`、`MEDIAFLOW_SIGNTOOL`。这些不是密钥入口；生产发布脚本仍显式接收对应参数，不从远程清单修改信任根。
4. 先运行下面的配置检查，报告必须放在静态发布树外的新路径。缺任何值时先停止，不能用示例地址/虚构证书填充。检查不签文件，也不解锁 Ed25519 保险库。

```powershell
$required = @('MEDIAFLOW_AUTH_THUMBPRINT','MEDIAFLOW_AUTH_STORE',
              'MEDIAFLOW_AUTH_TIMESTAMP_URL','MEDIAFLOW_SIGNTOOL')
foreach ($name in $required) {
  if (-not [Environment]::GetEnvironmentVariable($name)) {
    throw "缺少发行者确认的配置：$name"
  }
}
& pwsh -NoLogo -NoProfile -NonInteractive -File scripts/authenticode.ps1 `
  -Operation Inspect -Mode Production -Report $newPrivateReportPath `
  -CertificateThumbprint $env:MEDIAFLOW_AUTH_THUMBPRINT `
  -CertificateStore $env:MEDIAFLOW_AUTH_STORE `
  -TimestampUrl $env:MEDIAFLOW_AUTH_TIMESTAMP_URL `
  -SignToolPath $env:MEDIAFLOW_SIGNTOOL
if ($LASTEXITCODE -ne 0) { throw '正式签名配置未通过；保留报告，禁止继续生产签名。' }
```

`passed=true` 的 configuration-only 结果只表示这一轮工具、证书属性/链/撤销与 URL 配置检查通过，**不表示私钥操作权限、HSM 交互或 TSA 协议已实测通过**。实际签名必须使用随后经过授权的、同提交经证明构建，在新目录执行既有 Sign→独立 Verify→载荷差异→最终包/SBOM/清单验证；所有步骤通过后再形成正式验收报告。当前缺少正式证书和确认过的服务配置，不执行这一步。

## SBOM 缺口保持

既有真实构建的 457 个组件、440 个 PE、240 项识别缺口经修复后只读重新计算，native 字段全部相等，CycloneDX 1.6 独立验证通过。240 项为73项缺可靠版本、3项未展开 CLR 引用、163项同名 DLL 解析歧义、1项 Qt6Core 对 `icuuc.dll` 的未解析导入。静态链接传递依赖仍无法仅凭 PE 导入表完整确认，标记 `incomplete`。

识别缺口不是漏洞计数；没有填造版本、许可证或依赖。旧报告 `AUTHENTICODE_VERIFICATION.md`、`SBOM_COVERAGE_REPORT.md` 保留历史语境，本轮根因与分层结果见 `DRYRUN_TIMEOUT_ANALYSIS.md`。
