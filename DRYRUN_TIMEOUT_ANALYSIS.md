# 发布 DryRun 超时根因与验收记录

审计基线：`80a895ad8fa4cbe47177308b78b20319e2a29e54`。本阶段保持产品版本 2.1.0，不发布软件、不改稳定资产。下列时间是分别执行的实测，不将测试轮次相加为唯一用例数量。

## 历史现场与可确认的边界

原只读演练依次遇到运行目录门禁、缺少 `MEDIAFLOW_GITLEAKS`，以及真实扫描器配置后 core 组 900 秒终止。旧目录、报告和失败输出均保留。第二次演练没有完成 core JSON，也没有逐项计时；不能从总超时反推唯一卡住的子测试。

900 秒由 `tests/run_update_tests.py::run()` 设置；发布器 `prepare()` 对整个增量测试命令的外层限制是 1800 秒。core 的 stdout/stderr 是普通文件；发布器使用 `communicate()` 同时排空两条管道，未找到管道死锁证据。原 unittest 文本先写入内存 `StringIO`，到组结束才落盘，因此超时时逐项证据丢失。这是诊断缺陷，并非已证实的死锁原因。

旧缺扫描器轮次 core 453 项，609.418 秒，10 项跳过并被门禁拒绝；补齐扫描器轮次 900.064 秒终止。保留的 Authenticode 子报告显示：前轮进入 Auth 类前约 532 秒，后轮约 796 秒；后轮保留的最后成功报告对应第三方文件保持测试，随后无完整 case index。旧证据不足以确认其后哪一个 subcase 未完成，也不能把额外 264 秒全部归因于秘密扫描。

本阶段对未修改基线的同 453 个 core 用例增加外置逐项时间与栈采样后，真实运行完成：套件 709.7096 秒，父进程 711.603 秒，零失败、零错误、1 项符号链接权限跳过。**本轮未再次触发 900 秒**。重复测量和 cProfile 可以确认下述累积热点；不能声称精确重现了历史轮次的所有额外耗时。

## 确认的性能缺陷与最小修复

1. `native_sbom._read_bound()` 已有收据精确大小，却给每个小文件调用 `read(512 MiB + 1)`。44 字节真实文件的分配实验峰值约 537 MB；改为收据大小加一个增长哨兵字节后约 132 KB，内容 SHA-256 一致。仍执行最大长度约束、完整长度比较、完整 SHA-256、冻结读取及前后状态校验。追加、截断、同长度替换负例保持拒绝。
2. `package_update._plain()` 对同一路径及每个祖先先 `exists()` 再 `lstat()`，造成重复 Windows 文件系统查询。改为每层一次 `lstat()`，仅允许预期的不存在路径；符号链接、重解析点、权限及非目录错误仍拒绝。没有缓存或省略任何祖先校验。

同一 SBOM 用例 `test_pyinstaller_bootstrap_support_is_not_the_full_runtime_build_package` 的连续隔离测量：

| 代码状态 | 总秒数 | `_read_bound` 累计秒数 | `_plain` 累计秒数 |
| --- | ---: | ---: | ---: |
| 原实现 | 38.641 | 15.139 | 18.075 |
| 仅精确有界读取 | 26.284 | 1.049 | 19.044 |
| 两项修复 | 15.297 | 0.616 | 9.100 |

上述结果包含文件系统和调度波动，不是性能保证。原基线 core 的主要模块实测为 SBOM 238.524 秒、Authenticode 73.853 秒、构建收据 70.966 秒、真实 Gitleaks 用例 55.555 秒。

将首轮修复后的结果按测试 ID 与原基线匹配，同名的453项累计用例时间由706.1969秒降至490.7171秒；其中14项 SBOM 由238.524秒降至118.2442秒。秘密扫描模块的28项反而由55.5554秒变为65.5055秒，表明本轮节省没有来自跳过真实扫描。新增17项单独保留，不混入同名用例对比。

## 其他排查与诊断补齐

- 真实 Gitleaks 用例各自使用小型临时 Git 仓库，并未重复扫描 MediaFlow 大型工作区。真正发布前扫描仍覆盖所有 refs 与精确 index，对不可变 path/mode/blob 和相同 blob 去重；没有缩小覆盖范围。
- 未发现重复构建、签名、发布或无限网络重试导致本次热点的证据。完整收据及 SBOM 测试反复读取实际夹具，保留这些真实性检查，仅修复冗余分配和查询。
- 每项测试立即写开始、结束、UTC/单调时间、结果及逻辑结果码；组进程另记真实 OS 退出码。15 秒心跳只包含脱敏栈位置与该 worker 的后代进程名/PID，不记录参数、局部变量或环境值。
- 缺少 `MEDIAFLOW_GITLEAKS` 在 core 启动时明确失败。900/360 秒组预算与 1800 秒发布层预算均未增加，未拆组扩大预算。
- Windows 进程枚举错误为未知/失败，不能当作空列表通过。Job 关闭后仅做有限等待并确认后代退出；不终止无关用户进程。心跳线程必须有限退出。
- 混合 subTest 的先失败后跳过不会抹去失败结果。阶段命令和 CI 产物/SBOM 步骤有终态及耗时；失败时关闭尚未完成的阶段。超时保留已产生的 stdout/stderr，原认证与保险库步骤继续禁止普通日志。

## 分层验证记录

| 验证组 | 实际结果 | 限制 |
| --- | --- | --- |
| 原基线诊断 | 453 core；0 失败/0 错误/1 环境跳过；709.7096 秒 | 未再次触发历史 900 秒 |
| 首轮修复回归 | core 470 / UI 17 / output 12；共 499；0 失败/0 错误/1 环境跳过；父进程 503.989 / 4.289 / 3.985 秒 | 后续独立审查又补齐诊断边界，最终轮次另列 |
| 路径/实时进度/发布器针对性回归 | 39 项；0 失败/0 错误/0 跳过；3.174 秒 | 是新增诊断拒绝路径验证，不冒充生产签名 |
| 原生产构建 SBOM 只读重算 | 440 PE、240 缺口，全部 native 字段相等；CycloneDX 独立验证通过；20.1214 秒 | 使用既有 b806c405 构建，未冒充本补丁构建 |
| 本机完整组回归 r2 | core 475 / UI 17 / output 12；504 项；0 失败/0 错误/1 环境跳过；core 父进程398.250秒 | 最后两行只读阶段事件另跑21项发布器测试通过 |
| 最终本机完整只读 DryRun r3 | **退出0，通过，596.894秒**；core477/UI17/output12共506项，0失败/0错误/1既定环境跳过；三组及外层后代进程均为空 | 代码与证明同为f622702；core套件263.8191秒、含启动父层264.664秒；预算仍900秒 |
| 本阶段真实远程 CI | 最终[Windows/官方证明](https://github.com/xlxs123/MediaFlow/actions/runs/40432991674)506项0失败/0错误/0跳过；[安全工作流](https://github.com/xlxs123/MediaFlow/actions/runs/40432991740)四Job全部通过 | [PR #7](https://github.com/xlxs123/MediaFlow/pull/7)、[PR #9](https://github.com/xlxs123/MediaFlow/pull/9)分别经检查/审查后正常合并；没有管理员绕过 |
| 测试证书集成 | 同一真实CI产物中的10项集成均通过，原PE及信任根保持，私钥未导出；该结果文件也通过官方证明验证 | 明确TEST ONLY且不受信任；formal_signed=false，不是正式证书验收 |
| 最终独立产物复核 | 本机重新解析同一实际产物、CycloneDX严格验证及10对象官方证明验证通过，87.617秒；457组件、440 PE、240缺口，gap/relations与基线一致 | 在完整演练后另行执行，不将耗时或测试轮次相加 |
| 正式证书/生产发布 | 未配置 / 未验收 | `production_ready=false` |

唯一允许的本机跳过为 `test_worktree_symlink_is_never_followed`：当前用户不允许创建符号链接。真实 Gitleaks 不能跳过。完整失败现场与本阶段 JSONL、pstats、逐项文本、分配实验、独立审查记录保存在项目父目录的 `.mediaflow-dryrun-readiness-task`；不提交本机路径、环境快照或原始认证材料。

## 完整演练进一步发现的实际边界缺陷

独立分支修复提交 `f6f55c619529c3a08fffcd468eb240a39527d1fe` 经审查和所有 PR 检查通过，正常合并为 `25e6d12314b6b81c7c773f6243ca8e816aecf650`。未使用管理员绕过、强推、版本发布或删除分支。

- `dryrun-readiness-20261026-r1`：504项增量通过，core 272.554秒，随后在 CI 产物下载中断。保留文件357,007,360字节，服务器应为371,379,394字节，ZIP不完整；原进程/执行会话已不存在，没有退出码或最终成功记录。旧PID已被无关进程复用，没有操作该进程。中断原因无法由保留记录确认，原目录、部分文件及旧锁均保留，没有恢复或使用该输入。
- `dryrun-readiness-20261027-r2`：重新执行全部504项（0失败/0错误/1环境跳过），core275.568秒。完整CI ZIP大小、SHA-256、190个文件及收据官方证明均通过；四份原始SBOM文件的官方证明也通过。然后在 `verified-native-sbom` 阶段以PublishError安全失败，进程退出1、后代为空，总482.765秒，**未完成DryRun**。

对保留输入直接复现：`ci-sbom/inventory.json` 实际4,317,454字节，已认证SHA-256为 `06498e963390aa9a6f290ec57d5f350f9bbfb9e4e3865a2af991607a939f7a70`。`document()` 原调用普通清单读取器的2MiB默认上限；独立SBOM校验器原本允许32MiB，因此完整原生索引被错误使用了较小的清单边界。不是JSON损坏或官方证明失败。

最小修补仅让**已取得并验证官方证明的该SBOM索引**显式使用既有32MiB上限，普通清单和收据仍默认2MiB。继续使用同一冻结读取、重复字段/非有限数字/非对象拒绝逻辑，不改路径约束、签名和来源门禁。新增真实大文件边界测试，覆盖2MiB普通读取拒绝、32MiB专用读取接受、超上限及非法JSON拒绝。针对性路径/发布器31项零失败、零错误、零跳过。没有将这次失败重标成功，也没有跳过后续完整演练。

该修补提交 `da5a6c89db80aaa5e3c3fbb42c7719a3c6eecf97` 经[PR #9 Windows](https://github.com/xlxs123/MediaFlow/actions/runs/40432078026)506项零失败/零错误/零跳过、全部安全检查和主代理逐行审查后正常合并为 `f6227021543e8154bc035887a98c429db6f5a9a0`。初始性能及诊断补丁有独立代理审查记录；这次专用读取补丁的额外审查是主代理审查，不混称独立审查。

## 最终完整演练实测（2026-10-27）

代码与CI均为 `f6227021543e8154bc035887a98c429db6f5a9a0`，run `40432991674`，工作区保持干净。运行目录为 `.release-runs/dryrun-readiness-20261027-r3`，没有恢复或覆盖前两次目录。全部阶段都有开始和成功终态；命令阶段实际退出0，函数阶段没有虚构独立OS退出码。

| 必需阶段 | 实际秒数 | 结果 |
| --- | ---: | --- |
| 前置配置、同提交安全工作流及CI检查 | 16.7896 | 通过 |
| 源码披露审核 | 6.6940 | 通过，402文件、0待审 |
| 必要增量测试 | 270.4037 | 通过，506项；core477/UI17/output12 |
| 下载、完整摘要/收据和官方构建来源验证 | 218.2436 | 通过 |
| 原生SBOM及官方证明/原收据绑定 | 42.4581 | 通过，实际大索引按专用边界读取 |
| 实际打包EXE身份启动 | 22.7759 | 退出0，对应真实版本/源码/构建 |
| 完整发布结构 | 1.7142 | 退出0 |
| unsigned Authenticode状态检查 | 15.8748 | 退出0，仅检查，未正式签署 |

外层实际退出0，总596.894秒，`remaining_owned_children=[]`。报告级 `production_ready=false`、`formal_signed=false`、`signed_package_created=false`、`published=false`、`remote_writes=false`，签名输出目录不存在，没有保险库/正式签署阶段。未把unsigned检查当成生产就绪。

本轮CI构建artifact为371,376,819字节，SHA-256 `7839bb08761b856319c3f61735e8b515629c2b54ddcb76c58304405c7e77811b`。SBOM artifact为489,522字节，SHA-256 `658ddee5560c52caca712d249314e97bf0e388a32b534c0ac52ea67e56919b18`，可从[真实CI的SBOM产物](https://github.com/xlxs123/MediaFlow/actions/runs/40432991674/artifacts/12702329132)查看（GitHub可能要求登录）。这些是验收用unsigned CI产物，没有上传成正式Release资产。

本机完整历史扫描单独执行：Gitleaks8.30.1，all refs+精确index，25提交、523历史文件版本、402暂存文件、0阻断。它与core中的真实扫描器夹具分别记录；没有声称fixture扫描等于主仓库全历史扫描。扫描的refs/index摘要保存在私有JSON，后续报告提交不伪称已包含在这一时点扫描中；报告PR仍走完整远程安全工作流。

完整本机证据包括：父目录 `.mediaflow-dryrun-readiness-task/dryrun-command-r3.json`（实际argv/工作目录与选定非敏感环境配置）、`dryrun-wrapper-r3.json`、`final-build-evidence-r3.json`、`secrets-final-main-r1/report.json`；工作区运行目录中的 `report.json` 与 `attempt-f4da86dce7/unit/results.json`、三个 `*.progress.jsonl` 和逐项文本。每个用例有UTC开始/结束、真实耗时与逻辑结果码；每组另有真实OS退出码。旧失败文件、部分下载、锁与本轮所有诊断均保留。

## 稳定版本及边界保持

结束时真实API/匿名清单复核确认：v2.1.0仍为13个原资产，ID/名称/大小/digest、来源标签和稳定清单字节摘要均与开始时一致。渠道文件SHA-256仍为 `fd817c8ecd76b8dc9a4e8f341c1ba649b83a5a4bd15ed6af90cf5ae562ceeea9`；匿名正式清单SHA-256仍为 `580c204f3048c3443f51c6199ee25a62bb9f88d158cdced832bc41ae6632bf44`。现有客户端传输/验签代码实测该公网清单签名有效、篡改后拒绝；没有把这一小清单检查说成新版本发布或重新进行整包公网部署验收。

原白名单1519个静态文件、2,509,943,319字节重新完整哈希，全部与旧快照一致；未枚举用户数据。保留的2.0.1实际EXE执行 `--build-info` 退出0，未将其冒充完整GUI或网站下载回归。没有扫描浏览器登录数据、清理/移动用户文件、修改媒体下载核心、更新URL、公钥或v2.1.0资产。

`MODIFIED_FILES.txt` 按本轮起始80a895ad基线列出全部16个改动路径，而非将两次实现PR和报告PR的文件数重复相加。正式证书未配置、真实私钥操作和TSA服务协议仍未验收；240项识别缺口不是漏洞计数，静态/托管/系统传递依赖仍不完整。原900秒现场缺少逐项日志，不能精确还原其所有额外等待；本轮确认并修补的热点及完整演练通过是不同层次的证据。

## 执行方式与顺序约束

先在独立分支完成本地针对性测试和审查，推送 PR 并等待真实安全/Windows 检查。现有生产来源门禁只接受**同提交、默认分支**的 GitHub 官方构建证明；不能在未合并 PR 上用旧 main 的证明冒充。保持该门禁，在检查、审查及正常合并流程完成后，才能对同一 main 提交执行完整发布 DryRun。

以下为最终实际执行配置，以项目父目录变量代替机器盘符；完整解析后的原始argv保存在私有命令记录中。原运行目录现已存在，**不要原样重跑覆盖它**；再次演练应选择新的独立子目录。

```powershell
$workspace = (Get-Location).Path
$projectParent = Split-Path -Parent $workspace
$testPython = Join-Path $projectParent '.mediaflow-security-hardening-task/security-venv/Scripts/python.exe'
$verifiedGh = Join-Path $projectParent '.mediaflow-2.1.0-task/github-cli/portable/bin/gh.exe'
$env:MEDIAFLOW_GITLEAKS = Join-Path $projectParent '.mediaflow-security-hardening-task/tools/gitleaks/windows-x64/gitleaks.exe'
$env:MEDIAFLOW_AUTH_TEST_ROOT = Join-Path $projectParent '.mediaflow-dryrun-readiness-task/dryrun-auth-cases-r3'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:QT_QPA_PLATFORM = 'offscreen'
$preservedOldRelease = Join-Path $projectParent 'm3u8-downloader-2.0.1/dist-mediaflow-2.0.1'
$run = Join-Path $workspace '.release-runs/dryrun-readiness-20261027-r3'
& pwsh -NoLogo -NoProfile -NonInteractive -File scripts/release_mediaflow.ps1 `
  -Version 2.1.0 -DryRun -Python $testPython -SecurityPython $testPython `
  -Gh $verifiedGh -CiRunId 40432991674 -CiWaitSeconds 1 `
  -OldReleaseDirectory $preservedOldRelease -RunDirectory $run
```

无证书参数的演练不寻找、解锁或读取 Ed25519 生产保险库，不签名，不创建正式包，不写 GitHub Release/标签/稳定配置，不清理用户文件。其通过只代表 unsigned 构建、现有身份/结构/原生 SBOM 等真实完成的阶段，不能表示完整正式签名发布已就绪。
