# MediaFlow 自动化安全与供应链加固

本报告随实现提交。远程运行及密码学证明只能在 GitHub Actions 实际运行后补充；本地检查不替代远程验收。正式稳定版本保持 2.1.0，本阶段不创建稳定 Release。

## 实施范围

- 独立安全工作流：默认分支推送、PR、每周一及手动触发；官方 Python CodeQL、完整解析依赖审计、完整 Git 历史及 index 秘密检测、工作流安全策略。
- Actions 全部固定为经官方标签、提交及入口文件核对的完整 SHA；归档工具同时核验发布方归档摘要和实际二进制摘要。
- Dependabot 定期提出 Python 与 Actions 更新 PR，无自动合并或正式签名权限。
- Windows 构建后从实际 EXE、TOC、模块字节和 wheel RECORD 生成 CycloneDX 1.6 SBOM，并在独立环境执行官方 schema 校验。
- 默认分支通过 Windows 构建、EXE 启动、结构及 SBOM 检查后，独立最小权限 Job 使用官方 Artifact Attestations 生成来源证明；PR 无证明写权限。
- 正式发布器在读取保险库之前要求同一默认分支提交最新安全运行通过；消费构建时必须再通过官方 GitHub CLI 密码学验签，不把本地收据当作签名。

## 本地实际证据

- 初始 Git remote、默认分支、HEAD、干净工作区及完整历史均已检查，目标为 `xlxs123/MediaFlow`。
- 原应用六个依赖 pin 未变；仅将构建 bootstrap pip 从 25.2 更新至 26.2，修复实际审计发现的六个 CVE；复审覆盖 15/15 个解析依赖，零漏洞、零漏包。
- 完整历史秘密检测首轮命中逐项复核为合成测试夹具；原始失败报告保留，例外按不可变内容及具体位置绑定，不忽略整目录或官方提供商规则。
- 实际 gitleaks 集成、依赖审计、SBOM 归档与篡改边界、独立 schema、来源证明门禁及原发布器增量测试已执行。完整回归的最终统计在远程验收补充前更新。
- 本次开始时，原 2.0.0、2.0.1、2.1.0 既有文件保护清单共 1519 个文件、2509943319 字节，未发生变化；结束时重新核验。

## 远程验收状态

首轮真实 [PR #1](https://github.com/xlxs123/MediaFlow/pull/1) 验收中，[Windows 构建](https://github.com/xlxs123/MediaFlow/actions/runs/40150391740) 已通过打包、实际 EXE 启动、更新/回退回归与 SBOM 校验。[安全工作流](https://github.com/xlxs123/MediaFlow/actions/runs/40150391793) 的依赖、完整历史秘密及策略检查通过；CodeQL 如实阻断两项发现，没有忽略规则。

两项最小修复分别是 localhost HTTPS 夹具显式设定 TLS 1.2 下限，以及命名元数据提取使用精确域名/点边界子域判断。后者只调整命名来源判断，不改媒体发现或下载。相关 41 项增量及真实 TLSv1.3 证书校验握手通过。修复后的 CodeQL 和完整回归正在重新验证，不能提前计为通过。

真实 Windows SBOM 已下载并独立复验，42 个实际组件、6 个构建工具，官方 schema 零错误。PR 来源证明按设计跳过；只有默认分支合入后实际生成并经 `gh attestation verify` 验证，才能报告密码学来源证明成功。

## 边界及使用

详见 [自动化安全说明](docs/SECURITY_AUTOMATION.md)、[扫描结果](SECURITY_SCAN_RESULTS.md) 与 [SBOM 验证](SBOM_VERIFICATION.md)。不读取用户浏览器配置、不迁移或清理媒体、不访问生产私钥、不修改正式更新源、公钥或既有发布资产。原下载核心、媒体评分和 UI 不在变更范围内。

SBOM 对无法从实际构建证明版本和归属的原生/静态链接传递组件保留明确 incomplete 标记。CI 来源证明不等于发布者 Ed25519 签名或 Windows Authenticode。CI 证据有保留期限，下载可能要求 GitHub 登录。
