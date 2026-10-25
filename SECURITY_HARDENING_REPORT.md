# MediaFlow 安全与供应链加固报告

验收日期：2026-10-25。实现验收快照为 [5470d7679f7f5356ee1fab1f33f959b87bf5d86c](https://github.com/xlxs123/MediaFlow/commit/5470d7679f7f5356ee1fab1f33f959b87bf5d86c)；后续报告提交不改变应用源摘要。

## 实际完成

独立 [安全工作流](.github/workflows/security.yml) 已在 PR 和默认分支实际运行，并配置每周一 01:23 UTC（北京时间 09:23）及手动触发。官方 CodeQL、实际解析依赖、完整 Git 历史/index 秘密检测及工作流语法/权限策略均作为阻断检查。Actions 全部固定经官方来源核验的完整 SHA；归档工具核验发布方归档及实际二进制摘要。

PR 的 CodeQL 差异过滤已关闭，差异 SARIF 或重新开启差异配置均失败。7 个公开验证材料误报按全部数据流、位置、官方指纹、所有源码/验证器完整摘要精确审核，原始 SARIF 与 total/reviewed/unresolved 数量保留；没有整规则或目录忽略。

Dependabot 已覆盖 Python 与 Actions，按周检查、分组低风险更新，不自动合并。初始化的 [Python 检查](https://github.com/xlxs123/MediaFlow/actions/runs/40151749730) 与 [Actions 检查](https://github.com/xlxs123/MediaFlow/actions/runs/40151749736) 均在 GitHub 真实完成；它提出的依赖 PR 未被本任务擅自合并。

Windows 实际构建在独立环境完成，生成实际归档/RECORD 支撑的 CycloneDX 1.6 SBOM；独立官方 schema 校验成功。默认分支专用 Job 使用 GitHub Artifact Attestations，对 EXE、收据、测试/结构报告及 SBOM 核心文件生成来源证明。此 Job 不 checkout 或执行项目命令，仅自身拥有 OIDC/attestation 写权限；普通 CI 没有生产私钥。

## 验证结果

| 层次 | 实际结果 | 证据 |
| --- | --- | --- |
| 本地完整产品回归 | 2444 项，0 失败、0 错误、2 项明确跳过；37 个独立分组 | 本地报告摘要 `f01207a7fbb2a56e609294ce5c933e3bec27b4afaceba26b584eacc0877babca` |
| 修复及安全边界增量 | 41 项 TLS/域名回归、24 项夹具/恢复边界、38 项契约/门禁等增量通过 | 各轮原报告保留，不累加为唯一测试数 |
| GitHub 安全四项 | 全部通过；CodeQL 原始 7 / 已审核公开材料 7 / 未解决 0；依赖 15/15 零漏洞；秘密 0 阻断 | [真实运行](https://github.com/xlxs123/MediaFlow/actions/runs/40154064014) |
| Windows 更新与发布回归 | 431 项，0 失败/错误/跳过；实际打包、EXE 启动及完整结构检查通过 | [真实运行](https://github.com/xlxs123/MediaFlow/actions/runs/40154064054) |
| 独立下载后的 EXE | 官方构建产物大小/摘要、完整收据与 190 个文件核验；本机实际启动及结构复验通过 | [完整 Windows CI 产物](https://github.com/xlxs123/MediaFlow/actions/runs/40154064054/artifacts/12583722596) |
| SBOM | 42 个实际组件、6 个构建工具；官方 1.6 schema 零错误，公开路径/摘要关系验证通过 | [SBOM](https://github.com/xlxs123/MediaFlow/actions/runs/40154064054/artifacts/12584415815) |
| 密码学来源证明 | 官方 `gh attestation verify` 对 7 个实际对象逐项通过，限制仓库、工作流、默认分支、提交、run ID，并拒绝 self-hosted runner | [官方证明束](https://github.com/xlxs123/MediaFlow/actions/runs/40154064054/artifacts/12583448109) |
| 发布失败边界 | 对真实失败的默认分支安全运行 40151743924 调用门禁，正确拒绝；未启动发布器或访问保险库 | 原失败记录与只读拒绝报告保留 |

本地两项跳过为：未指定专用 localhost 浏览器验收配置，以及本机无法创建符号链接。GitHub Windows 增量回归无跳过。本阶段没有重复下载真实网站样本，也没有读取实际浏览器登录资料。

## 最小修复与发布保护

实际依赖审计发现 pip 25.2 的六个已知 CVE，仅将构建 bootstrap 固定为 pip 26.2，原应用六个依赖 pin 不变，复审覆盖 15/15 且零漏洞。CodeQL 的 TLS 夹具和域名边界结果已修复；额外本地验收资源与响应头边界已收紧，媒体 ID 诊断排除认证字段及签名 URL。未修改下载核心、媒体评分、WebView2/CDP、任务恢复或媒体库运行逻辑。

正式发布前必须通过同一默认分支提交最新安全工作流，失败、跳过、缺失或超过八天均在访问正式保险库之前停止。消费构建还须官方密码学验签，不能用未签名本地收据替代；没有绕过开关。源码修改通过正常 [PR #1](https://github.com/xlxs123/MediaFlow/pull/1) 和 [PR #3](https://github.com/xlxs123/MediaFlow/pull/3) 合并，无强推或保护绕过。

## 稳定版本及数据保护证据

正式稳定版仍为 v2.1.0，Release ID 421736487。13 个资产的 ID、名称、大小、摘要和 URL 与任务开始记录完全一致。原 2.0.0、2.0.1、2.1.0 的 1519 个文件、2509943319 字节逐项摘要未变，4 个既有隔离验收 Release 保留。未发布新稳定版本，未删除、移动或清理旧版/用户文件。

- 稳定清单：[固定地址](https://github.com/xlxs123/MediaFlow-Releases/releases/latest/download/manifest.json)；匿名重取 SHA-256 `580c204f3048c3443f51c6199ee25a62bb9f88d158cdced832bc41ae6632bf44`，与原已验签文件字节完全一致。
- 客户端渠道/可信公钥配置 SHA-256：`fd817c8ecd76b8dc9a4e8f341c1ba649b83a5a4bd15ed6af90cf5ae562ceeea9`，未变。
- 原正式包 SHA-256：`ab560859e0b7be22b5b2cfa2f50f9b3fc63c5489c2bd8c4246dd1c0e4b407b6c`，大小 371249271 字节，未变。
- 正式源码标签对象仍为 `4f51a1866aeff9d8b74af0c4fd63b1c776de8cbd`，目标仍为 `4149abf52d522308bfeac9a6ab9e6d34a2e20d96`。

## 使用及限制

日常维护入口见 [SECURITY_AUTOMATION.md](docs/SECURITY_AUTOMATION.md)，精确文件清单见 [MODIFIED_FILES.txt](MODIFIED_FILES.txt)。CI 资产可能需要 GitHub 登录，SBOM/证明保留 30 天，完整构建保留 7 天。原生及静态链接传递组件未穷尽，SBOM 明确 incomplete；漏洞数据库不能覆盖未知漏洞。GitHub 来源证明不等于发布者 Ed25519 签名或 Windows Authenticode；本阶段不声明新增稳定发布或对 Windows EXE 完成 Authenticode 签名。
