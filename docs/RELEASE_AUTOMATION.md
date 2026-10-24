# 自动发布入口

Windows PowerShell 7.2+ 入口为 `scripts/release_mediaflow.ps1`。它复用现有签名、ZIP 安全检查、草稿发布、独立部署、健康检查与回退模块；不修改应用版本或下载器。

## 发布流程

1. 检查版本与 `APP_VERSION` 一致、Git 工作区干净、实际 GitHub origin、已确认的公开资产仓库和发布权限。正式发布只接受远程默认分支当前提交，不覆盖已有标签或已发布版本。
2. 只读检查同一提交的 Windows CI。必须通过测试、打包、实际 EXE 身份、完整结构检查和产物保存步骤。普通 CI 只有 `contents: read`，不访问正式私钥、不创建 Release。
3. 再运行必要的本地增量测试。默认下载该 CI 的完整 Windows artifact，先核对 GitHub API 的独立 SHA-256，再安全解包、检查收据绑定的全部文件。收据本身不是签名，也不单独证明 GitHub 来源。
4. 实际运行选定 EXE，核对版本、运行源码摘要、完整工具、许可和资源；执行本地协议、轨道、媒体库及重启验收。不会重新下载全部网站样本。
5. 用现有外部 Windows DPAPI 保险库正式签名，生成 ZIP、大小、完整 SHA-256、`release.json`、`signature.json` 及固定渠道别名。普通子进程不继承签名密码或 GitHub token；认证和签名输出不写入普通日志。
6. 正式模式调用既有 GitHub 发布器：创建或恢复同一草稿，上传精确资产，授权重新下载并验签/核对哈希后才发布。草稿不能匿名访问，因此匿名验收安排在正式发布之后。
7. 发布后通过实际 EXE、系统 TLS 和内置正式公钥执行匿名公网下载、篡改拒绝、独立部署、健康检查、新旧程序启动及数据保护验证。各阶段分别记录结果；失败保留版本和证据，不自动删除 Release、旧程序或用户数据。

## 使用

在已安装固定依赖、配置正常 GitHub 授权和既有正式保险库的发布者电脑上，先提交并推送审核通过的源码，等待该提交 CI 成功。`-Version` 应使用当前源码的真实版本。

```powershell
# 完整演练：实际测试、构建产物核验和正式签名；没有 GitHub 写操作
pwsh -NoProfile -File scripts/release_mediaflow.ps1 -Version <当前版本> -DryRun `
  -OldReleaseDirectory <可实际启动的旧版完整目录>

# 只有确有新产品版本、默认分支 CI 通过且准备正式发布时才执行
pwsh -NoProfile -File scripts/release_mediaflow.ps1 -Version <当前版本> -Publish `
  -OldReleaseDirectory <可实际启动的旧版完整目录>
```

`-DryRun` 与 `-Publish` 必须明确选择且不能同时使用。默认复用 `.venv\Scripts\python.exe`、PATH 中的 Git/gh 和唯一匹配的既有正式保险库；可通过 `-Python`、`-Gh`、`-VaultDirectory` 和 `-KeyId` 显式指定。正常 GitHub CLI 授权不可用时，可以复用 Git Credential Manager 的当前用户授权；凭据只在内存和必要子进程环境中传递，不写入报告。

`-CiRunId` 可选中同一源码提交的具体成功 CI；默认自动查找。CI 产物保留七天，过期后必须重新运行 CI，不能跳过门禁。`-CiWaitSeconds` 为有界等待，默认 1200 秒。`-AdditionalAssets` 可附加经审计的第三方源码材料等；已存在资产必须完全一致，禁止覆盖。

对于没有运行时代码变化的发布工具开发，可使用 `-ExistingReleaseDirectory <已验收完整目录>`。仍核对实际 EXE、源码摘要、完整文件及本次验收。只有此模式可用 `-AcceptanceReports` 显式引用同一 EXE 的原始通过报告；新 CI 构建不能沿用旧报告。不要仅为验证工具而增加产品版本。

## DryRun 的证据范围

演练的正式签名包只保存在本机独立目录，不上传、不创建草稿、不创建或推送版本标签。如果该版本已经公开发布，会另外用实际 EXE 检查**现有公开资产**；新生成的演练 ZIP 并未经过公网验收。不存在公开版本时，公网阶段明确记为未执行。

报告将 `public_online_verified`、`staged_package_was_publicly_verified`、`published_by_this_run` 和 `production_chain_verified` 分开记录。只有本次正式发布后的实际公网更新验收通过，才会把 `production_chain_verified` 设为 `true`。DryRun 通过不证明新发布事务已在生产执行。

## 报告与中断恢复

每次创建工作区内独立的 `.release-runs/<版本-时间-随机ID>/`，包含 `report.json`、分阶段日志、验收收据、正式签名资产和实际 EXE 报告；该目录不进 Git。可用 `-RunDirectory` 指定其新的子目录，不能指向已有发布目录或用户数据目录。

签名完成后遇到上传或公网验收失败，可以使用原 `-RunDirectory` 加 `-Resume`，保持相同模式、版本、源码提交、保险库和附加资产参数。恢复前重新核对 CI、签名、完整包和实际文件。已经发布时仅重新核对资产并验收，不能重复覆盖。签名前失败需要新建演练；已完成运行拒绝重入。

同目录并发受独占锁保护。意外退出遗留锁时安全停止，不自动删除锁或推断进程已经结束；先核实原进程状态，再由发布者处理。脚本不清理旧版本、媒体、用户文件或签名备份。

发布之后公网验收失败会标为失败并保留已经公开的状态，不把它伪装为成功或自动删除/降级远程 Release。应用回退只启动旧程序，不倒退用户数据库。没有更新源变更、私钥轮换或静默切换快捷方式。

## 许可与发布材料

完整应用 ZIP 包含工具、许可和必需运行资源；额外第三方源码材料按现有 [THIRD_PARTY_SOURCES.md](THIRD_PARTY_SOURCES.md) 提供。Ed25519 只认证更新发布身份，不等于 Windows Authenticode。密钥和签名规范见 [PRODUCTION_RELEASE.md](../PRODUCTION_RELEASE.md)。正式验证记录与本次演练报告分开保存。
