# SBOM 与来源证明验证

## 实现及真实边界

标准为 CycloneDX 1.6。生成器依据当前构建的完整发布文件收据、PyInstaller Analysis/PYZ/PKG TOC、实际 EXE CArchive/PYZ、模块实际字节及 wheel RECORD 核实归属。构建环境安装列表不能冒充实际运行组件。

记录 CPython、实际打包的 Python 发行包、可识别原生文件、便携工具与版本、源码提交及 Actions run ID。仅构建工具列在 metadata.tools，未进入归档的依赖不列为运行时；PyInstaller 实际启动支持文件独立记录。无法穷尽的静态链接传递组件明确 incomplete；未知版本不猜测。本机绝对路径与用户数据不得进入输出。

独立固定版本 CycloneDX schema validator 同时验证官方严格 schema、明确的 1.6 版本、文件摘要和组件关系。生成失败、归档被篡改、证据不一致或泄漏本机路径均阻断。

## 已执行的本地验证

- 使用真实可执行归档、字节及 RECORD 的本地测试覆盖模块所有权、共享 namespace、未打包依赖、构建工具区分、收据和并发替换等边界。
- 本地构造归档产生的 SBOM 已通过官方 1.6 schema；这是测试夹具，不能当作本次 Windows 产品构建。
- 独立 validator 五项测试全部通过，覆盖有效清单、摘要一致但 schema 错误、摘要一致但泄漏路径、已有报告不覆盖，以及错误 specVersion。

## CI 产物及密码学证明

Windows CI 在实际 EXE 启动及完整结构检查后生成 SBOM，独立保存 `mediaflow.cdx.json`、`inventory.json`、`generation.json`、`SHA256SUMS.txt` 和 `validation.json`。SBOM 与来源证明资产保留 30 天；完整 Windows 目录保留 7 天，可能需要 GitHub 登录下载。

默认分支的官方证明 Job 仅使用固定官方下载/证明 Actions，不 checkout、不执行项目命令。证明覆盖 EXE、原收据、测试/结构报告及 SBOM 核心证据。它必须先依赖成功 Windows 构建，PR 不获得证明权限。

收据仍是未签名 JSON。后续必须用官方 `gh attestation verify` 限制源码仓库、工作流、默认分支、提交及托管 runner，进一步核对实际文件 SHA-256 与 run ID。只有该步骤成功才可报告密码学来源证明通过。生成文档内保守的 false 标记不会被事后伪造为验签成功。

## 待实际运行后补充

首轮真实 [Windows CI](https://github.com/xlxs123/MediaFlow/actions/runs/40150391740) 已实际生成 [SBOM 资产](https://github.com/xlxs123/MediaFlow/actions/runs/40150391740/artifacts/12581959664)。下载后核对 GitHub 服务端归档大小及 SHA-256，再复制到独立复验目录执行官方 schema 和证据关系校验：42 个实际组件、6 个构建工具、schema 错误 0。原 CI 证据未改写。

- 对应源码为 PR 临时 merge commit `4048b07cd6a30288f5be6ce317c6a6a7e1583f61`。
- 下载归档 SHA-256：`9de81d2b16c56e0ae75f1bb4856835a58a0f2c02666f8d91ac0cd5511b4a9b13`。
- BOM SHA-256：`57dd144394e70022b6945bc8fb93664a9e8020a353e88b14b15780341e4d8e60`。
- inventory SHA-256：`8ff74186794e7976cf88368199725e8b0c8ac6e088eaa7bb40105186f1b99181`。

本次安全修复后的新快照仍需重新构建。PR 证明 Job 按设计跳过，密码学来源证明未计为成功；默认分支生成后的证明束、CLI 验签结果及最终 CI 链接将在实际完成后补充。
