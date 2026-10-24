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

本次真实 Windows SBOM 下载地址、组件统计、schema 校验摘要、官方证明束、官方 CLI 验签结果与对应 CI 链接，须在 PR 合入默认分支并实际生成后记录。目前不声称这些远程结果已通过。
