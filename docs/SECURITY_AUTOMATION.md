# 自动化安全与供应链检查

安全工作流是 `.github/workflows/security.yml`，在 main 推送、PR、每周一 01:23 UTC（北京时间 09:23）和手动触发时执行。它独立于应用下载功能，不访问用户媒体库、恢复数据或浏览器登录配置。

## 检查与阻断规则

- **CodeQL**：GitHub 官方 Python 分析，使用 security-extended 查询。分析失败、没有有效 SARIF 或存在未解决安全结果均阻断，不把上传 SARIF 成功等同于零发现。
- **依赖**：独立工具环境运行固定版本 pip-audit，读取实际 Windows 构建环境的全部发行包。直接 pin、构建 pin、传递依赖、pip check、完整审计覆盖及环境前后稳定性都必须通过。所有已知漏洞（含高危、严重及未知严重性）、漏包、skip、网络或工具错误均阻断，没有自动修复或忽略漏洞参数。
- **秘密检测**：核验固定 gitleaks 的完整归档及派生可执行文件摘要；扫描所有 Git refs、包括 merge commit 的历史以及精确 index。浅克隆、绕过用 ignore 文件、可疑用户数据文件或未批准的凭据命中均阻断。原始 Secret/Match 不落日志或报告。
- **工作流策略**：actionlint 检查实际 GitHub Actions 语法；另核对官方完整提交 SHA、权限范围及受信任来源证明 Job。普通 CI 没有仓库写权限或正式私钥。

所有依赖与 Actions 更新均进入 PR，不自动合并。Dependabot 将低风险 Python 更新和 Actions 更新分别分组；Actions pin 更新必须同时核对官方仓库的标签、提交和入口文件，并更新 `security/toolchain.json`。不能通过移除检查或修改生产信任配置处理更新失败。

CodeQL 在 PR 默认可能只报告差异范围；本项目已按固定 Action 的官方 feature-flags.ts 设置 `CODEQL_ACTION_DIFF_INFORMED_QUERIES=false`，PR 与默认分支都要求完整结果。SARIF 若声明 diff-informed 会直接失败。配置被改回差异过滤时，工作流策略也会阻断。

`security/codeql-reviews.json` 仅记录已经实际证明不含秘密的公开验证材料误报。每项绑定官方 partial fingerprints、全部完整 codeFlows（保留 source 顺序、数量及全部字段）、主位置和所有相关文件/验证器的完整 SHA-256。不忽略整规则、文件或目录，不把通用 JSON writer 当作安全过滤器。新增 source、不同数据流、位置/指纹或源码改变均不能复用审核。原 SARIF 始终保留，报告分别显示 total、reviewed_public_metadata 与 unresolved；已审核误报不能描述为原始零发现。真实契约测试另验证私钥/密码/Cookie 字段被公钥 schema 拒绝，运行身份仅含公开 key ID/公钥指纹。

## 本地执行

使用两个独立虚拟环境：构建环境安装 `requirements-build.txt` 和 `requirements.txt`，审计环境安装 `requirements-build.txt` 和 `requirements-security.txt`。不要将审计工具装进应用打包环境。当前构建 bootstrap 固定 pip 26.2，修复首次实际审计发现的 pip 25.2 漏洞；应用原有六个依赖 pin 不变。

```powershell
python scripts/security_tools.py --tool gitleaks --directory <全新工具目录>
python scripts/scan_secrets.py --gitleaks <核验后的gitleaks> --output <全新报告目录>
python scripts/audit_dependencies.py --environment-python <构建Python> --audit-python <审计Python> --output <全新报告目录>
<审计Python> scripts/security_gate.py workflows --output <新的策略报告.json>
```

真实 scanner 集成测试需设置 `MEDIAFLOW_GITLEAKS` 为已核验二进制绝对路径，CI 自动准备；缺失时不得把跳过测试计为通过。完整应用回归可用 `tests/run_unit_tests.py --report-dir <新目录> --security-python <审计Python>` 保存新证据，旧目录不会被覆盖。独立 schema 与工作流 parser 测试由该独立解释器执行；正常 build.ps1 自动准备此环境，不把审计工具装入打包环境。

## 精确的合成例外

`security/secret-exceptions.json` 只允许已逐条审核的合成值，绑定规则、文件、内容 SHA-256、行以及具体提交或 index blob。历史 finding 保留真实 commit；只有实际解析该 commit 的 Git tree 得到完全相同 blob 时，才复用该 blob 的精确 index 审核，从而支持正常 merge；不信任检测器自报 blob。官方提供商规则不能被此入口豁免，不接受目录或整规则忽略。新增、移动或修改夹具后必须重新审核对应命中，原例外不会泛化到新的真实凭据。

如果发现疑似真实秘密，检查停止，只记录类型、提交、路径和行。保留证据，由凭据所有者撤销/轮换相应类型；不要导出、使用、打印或通过重写 Git 历史掩盖泄漏。本工具不自动撤销凭据或删除历史。

## 实际构建 SBOM

```powershell
<构建Python> scripts/generate_sbom.py --release-dir <完整发布目录> --build-dir build/MediaFlow --receipt <CI构建收据.json> --output <新SBOM目录>
<审计Python> scripts/validate_sbom.py --directory <SBOM目录>
```

清单为 CycloneDX 1.6。生成依据是实际 EXE 的 CArchive/PYZ、构建 TOC、实际源文件/字节码、wheel RECORD 与完整发布文件摘要；安装环境不是运行时组件清单。Python、打包组件、独立工具、实际 bootstrap 支持文件和仅构建工具分别记录。未进入实际归档的 pycparser 等不会因出现在构建环境中就被列作运行时组件。

输出包含 `mediaflow.cdx.json`、`inventory.json`、`generation.json`、`SHA256SUMS.txt`；独立官方 schema 校验另写 `validation.json`。报告关联源码提交、工作流运行 ID、测试与结构检查摘要。不输出原始 TOC 内的本机绝对路径。未知版本保持未知；静态链接的 FFmpeg、Qt、OpenSSL 等传递组件尚未穷尽，清单明确标记 incomplete。

## 官方来源证明与发布门禁

现有 `ci_build_receipt.py` 仍是未签名的字节绑定收据，不改称数字签名。Windows 构建、测试、实际 EXE 启动、结构检查及 SBOM 验证成功后，独立 Job 使用官方 Artifact Attestations 对 EXE、原收据、测试/结构报告及 SBOM 生成 OIDC/Sigstore 来源证明。

只有此 Job 拥有 `id-token: write` 和 `attestations: write`，且只接受默认分支上的可信运行。它不 checkout 源码、不执行项目命令，仅使用固定官方 Actions 读取本次通过构建产生的资产。PR 不自动获取签名权限。证明束与 SBOM 各自保留为独立 CI 资产，保留 30 天；CI 资产下载可能要求 GitHub 登录。

来源证明必须再由官方 `gh attestation verify` 验证，限制仓库、工作流、默认分支、源码提交并拒绝 self-hosted runner。发布器还核对证明绑定的实际文件摘要和 workflow run ID。普通 SHA-256、未签名 JSON、成功的 Job 名称均不能替代此密码学验证。GitHub 来源证明也不等于 MediaFlow 的 Ed25519 发布者签名或 Windows Authenticode。

正式发布前，必须通过当前默认分支同一提交的最新安全工作流及全部四个 Job；失败、取消、跳过、缺失或超过八天的证据均停止，不能选更早的绿色结果掩盖最近失败。门禁位于正式保险库访问之前，DryRun 也不能绕过。

新 CI 构建消费路径先核验下载资产的服务端摘要、完整收据，再进行官方来源证明验证。正式模式复用已有目录时，还必须具有原 CI 的相邻收据及对应密码学证明；仅本地 EXE 验收不足以替代。旧目录复用的 DryRun 保留原验收边界，不会被当作新的正式构建来源证明。

生产私钥始终留在发布者现有保险库，不进入任何普通或安全 CI。该系统不修改稳定更新地址、可信公钥、旧版资产或用户数据，不发布新稳定版本，也不执行静默删除。
