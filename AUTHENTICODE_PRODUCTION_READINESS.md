# 正式代码签名就绪状态

基线为正常合并 [PR #10](https://github.com/xlxs123/MediaFlow/pull/10) 后的 main `368f1f00f6cb2c6a97895c1ef34fb3c2930736f1`。版本仍为2.1.0。本阶段不发布稳定版、不改更新渠道、公钥或13个既有资产。

```text
formal_signed = false
production_ready = false
published = false
```

## 实际环境

2026-10-27只读查询 CurrentUser/My、LocalMachine/My，4张具有 Code Signing EKU 的证书全部是明确的 TEST ONLY 自签名证书；另有4张既有错误 EKU 测试夹具。没有非测试、非自签名的正式候选。Process/User/Machine 均未配置正式 thumbprint、store、TSA URL。后续新增隔离夹具不改变这个正式状态。

SDK SignTool `10.0.22621.3233` 的 Microsoft Windows 签名有效。实际 Production Inspect 退出1，`certificate_not_configured`；未导出私钥、修改 Root 或解锁 Ed25519 保险库。首次本地辅助采集的 EKU 属性访问有误，已改用证书扩展 OID 重新采集，旧结果保留且不作为结论；生产实现原已正确检查扩展，无需修复。

隔离测试后再次查询，10张Code Signing EKU证书仍全部为测试夹具（含过期/公钥-only），正式候选仍为0。现有DPAPI保险库公开密钥匹配内置可信配置，三个固定文件存在；这里只查公开元数据和文件存在性，没有做ACL、DPAPI解密或正式私钥操作验收。当前最小外部阻塞仍是发行者的真实证书及确认过的发行/TSA配置。

同提交main `c3fa7bb579149366aa6c4f71ab7537b22727e3ec` 的真实Windows/安全CI已通过，18项SDK隔离集成和本机完整只读DryRun通过；10个官方构建证明对象独立验证通过。它们没有改变上述正式状态，实际证据见本轮验收报告。

## 发行者一次性配置

剩余外部条件是发行者提供受信任 CA 签发、合法授权的真实代码签名证书及可用的 KSP/CSP/HSM 环境，并确认公开 thumbprint、My 存储位置、签名账户权限和 HTTPS RFC 3161 地址。若组织没有证书，申请/采购、身份核验和硬件授权由发行者完成。本工具不购买证书、不申请身份、不改 Root、不导出私钥。

不要向聊天、仓库、普通CI或日志提供 PFX、私钥、密码、硬件 PIN 或令牌。只配置公开选择信息，并复用现有入口：

```powershell
$names = @('MEDIAFLOW_AUTH_THUMBPRINT','MEDIAFLOW_AUTH_STORE',
           'MEDIAFLOW_AUTH_TIMESTAMP_URL','MEDIAFLOW_SIGNTOOL')
foreach ($name in $names) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "缺少发行者确认的配置：$name"
    }
}
# 由发行者明确指定发布树外、尚不存在的本地绝对报告路径。
if (Test-Path -LiteralPath $newPrivateReportPath) { throw '报告已存在，禁止覆盖。' }
& pwsh -NoLogo -NoProfile -NonInteractive -File scripts/authenticode.ps1 `
    -Operation Inspect -Mode Production -Report $newPrivateReportPath `
    -CertificateThumbprint $env:MEDIAFLOW_AUTH_THUMBPRINT `
    -CertificateStore $env:MEDIAFLOW_AUTH_STORE `
    -TimestampUrl $env:MEDIAFLOW_AUTH_TIMESTAMP_URL `
    -SignToolPath $env:MEDIAFLOW_SIGNTOOL
if ($LASTEXITCODE -ne 0) { throw '正式配置未通过，禁止生产签署。' }
```

不要用虚构指纹或示例地址补齐配置。configuration-only 检查通过仍不证明私钥操作成功；`HasPrivateKey` 不等于 KSP/HSM 已实测。时间戳 URL 仅为 `configured_not_probed`，不能声称 TSA 协议通过。正式签署仍必须验证系统链、Online 撤销、精确 signer、Get-AuthenticodeSignature、SignTool、WinVerifyTrust 和与 signer 绑定的 RFC3161 token。

同提交默认分支官方证明与原 CI 收据/SBOM通过后，才在新树签固定三个第一方 PE；第三方不重签。实际顺序为：签后目录Production验签→生成最终ZIP及Ed25519清单→从最终ZIP重新提取三个PE做Production验签→允许创建草稿或上传。目录验签在读取正式Ed25519私钥之前；最终ZIP验签则在远程发布写入之前。测试证书、官方来源证明和SHA-256各有职责，不能替代发行者正式Authenticode。

参见 [本轮验收](AUTHENTICODE_ACCEPTANCE_REPORT.md)、[产物一致性](FINAL_PACKAGE_INTEGRITY_REPORT.md)。测试证书参数依据 [Microsoft 文档](https://learn.microsoft.com/en-us/powershell/module/pki/new-selfsignedcertificate)；公钥-only夹具仅保存公开证书的RawData，见 [X509Certificate2](https://learn.microsoft.com/en-us/dotnet/api/system.security.cryptography.x509certificates.x509certificate2)。没有导出测试私钥或添加信任根。
