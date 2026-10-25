# SBOM 与构建来源证明验证

验收日期：2026-10-25。实现验收快照为 [5470d7679f7f5356ee1fab1f33f959b87bf5d86c](https://github.com/xlxs123/MediaFlow/commit/5470d7679f7f5356ee1fab1f33f959b87bf5d86c)；后续报告提交不改变应用源摘要。

## 实际 Windows 构建

[真实 Windows CI](https://github.com/xlxs123/MediaFlow/actions/runs/40154064054) 完成 431 项更新/发布回归、实际 EXE 启动、完整结构及 SBOM 检查。独立下载的构建归档为 371376710 字节，服务端 SHA-256 为 `5b4af14a1e99d10dec1d5e5d3bba9d4e5c10e4e1fd6921e3963c7f4e7bbf220f`，逐字节核验一致。完整目录 190 文件、575824930 字节；下载的 EXE 又在本机实际启动并通过结构检查。

EXE 版本为 2.1.0，SHA-256 `d83f1c9513b5fd6df82127109c92d754bc578cc1b538fe7b6dffdcacfee66dcc`；这是独立 CI 构建，不是新的稳定 Release。应用源摘要 `7e90e8b99147efca69479f1b95b5da3f48b29a949f7f5d702713ae7b9e93c8c9` 与完整本地回归/当前源码一致。

## SBOM 实测

格式为 CycloneDX 1.6，实际组件 42、构建工具 6，独立官方严格 schema 校验零错误、公开路径/秘密标记零命中。完整归档、TOC、实际模块字节、wheel RECORD 和全发布收据支撑组件归属；安装包列表未被冒充运行时清单。仅构建工具放 metadata.tools；未进入实际归档的 pycparser 不列作运行时，PyInstaller 启动支持文件单独记录。

[SBOM 资产](https://github.com/xlxs123/MediaFlow/actions/runs/40154064054/artifacts/12584415815) 的下载归档 SHA-256 为 `748e9848d098ae6e8286d2d3352c39119cbb193de36672463799b85e4a3faede`，大小 183284 字节，已独立核对服务端摘要。BOM SHA-256 `d910bd3df15c89bd763d9e49af34aa1f906af79b4fcde972343d72f38139f8b6`；inventory SHA-256 `2b140c5f908e05a496ff83a8a962035f8dbfcc9e410678d342b1f197a85d5e44`。原 CI 校验报告与本机独立校验一致，未改写原证据。

## 已实际验签的官方来源证明

[官方证明束](https://github.com/xlxs123/MediaFlow/actions/runs/40154064054/artifacts/12583448109) 已下载，大小 7334 字节，归档 SHA-256 `b8bc7fdbb058e8f73f08d2ec7d04e5bb5f5bc0ca52ffb3b40bc3903e75af9c6f`。官方 GitHub CLI 实际验证以下 7 个对象，全部通过；不是仅检查 Job 名称或比较普通 SHA-256。

| 对象 | 实际 SHA-256 | 官方密码学验证 |
| --- | --- | --- |
| build-attestation.json | `b7587575ee58701b2bed06f82eafffbd4232b2cda5699c422394a552be74f37c` | 通过 |
| MediaFlow.exe | `d83f1c9513b5fd6df82127109c92d754bc578cc1b538fe7b6dffdcacfee66dcc` | 通过 |
| results.json | `1873cd0e71b40351ddbdafe6b4049b6e3fde2e79232a145de383b832405670fc` | 通过 |
| release-structure.json | `a62f15fe0b2be1a4a4fc89900397b0d92c78cf4419ababd498da5a5a856e68df` | 通过 |
| mediaflow.cdx.json | `d910bd3df15c89bd763d9e49af34aa1f906af79b4fcde972343d72f38139f8b6` | 通过 |
| inventory.json | `2b140c5f908e05a496ff83a8a962035f8dbfcc9e410678d342b1f197a85d5e44` | 通过 |
| validation.json | `105ddf557eca8a2aca6ba949de354310d4ccda0c5a1b4f61c2d1bcef82218bac` | 通过 |

验签限制：仓库 `xlxs123/MediaFlow`、工作流 `.github/workflows/ci.yml`、默认分支 `refs/heads/main`、提交 `5470d7679f7f5356ee1fab1f33f959b87bf5d86c`、run ID `40154064054`、SLSA v1 predicate，并拒绝 self-hosted runner。另检查证书、已验证时间戳、确切 subject 摘要及调用运行 ID；验证后复核文件未变。

复核命令格式：

```powershell
gh attestation verify <实际文件> --repo xlxs123/MediaFlow --signer-workflow xlxs123/MediaFlow/.github/workflows/ci.yml --source-ref refs/heads/main --source-digest 5470d7679f7f5356ee1fab1f33f959b87bf5d86c --deny-self-hosted-runners --predicate-type https://slsa.dev/provenance/v1 --format json
```

生产发布器会执行同一官方验证，并在此之前验证 [同提交安全四项](https://github.com/xlxs123/MediaFlow/actions/runs/40154064014)。未签名 build-attestation.json 仍只作为字节绑定收据；其外部官方证明独立核验。SBOM/生成阶段保守的 provenance=false 标记未被事后改写，真实验证结果由外部验签记录给出。

## 证据层次及限制

本地合成归档/schema/篡改测试仅算本地测试。本报告的 42 组件与七对象验签来自上述真实 GitHub 默认分支构建和独立下载复核；PR 阶段证明按设计跳过。

FFmpeg、Qt、OpenSSL、yt-dlp 等原生或静态链接传递组件未全部枚举，composition 明确 incomplete；未知版本保持未知。外部系统 WebView2 Runtime 不冒充内置组件。GitHub 来源证明不等于发布者 Ed25519 签名或 Windows Authenticode。SBOM/证明保留30天，完整CI构建7天，下载可能要求GitHub登录；本机原始证据另行保留。
