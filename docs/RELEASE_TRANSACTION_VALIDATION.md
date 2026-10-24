# 发布事务真实验收记录

验收日期：2026-10-24。产品稳定版本仍为 **2.1.0**。

本次通过真实 GitHub API 和公开 HTTPS 下载验证了隔离预发布事务，包含实际创建、上传、恢复、发布及拒绝重复执行。没有发布新的稳定版本，没有修改 v2.1.0 正式 Release 或其资产。

## 真实发布环境和结果

- 源码仓库：[xlxs123/MediaFlow](https://github.com/xlxs123/MediaFlow)。
- 正式发布仓库：[xlxs123/MediaFlow-Releases](https://github.com/xlxs123/MediaFlow-Releases)。
- 本次实际创建的独立公开验收仓库：[xlxs123/MediaFlow-Release-Acceptance](https://github.com/xlxs123/MediaFlow-Release-Acceptance)。
- 实际公开测试 Release：[mediaflow-pipeline-test-20261024t042956z-43d9dc-resume](https://github.com/xlxs123/MediaFlow-Release-Acceptance/releases/tag/mediaflow-pipeline-test-20261024t042956z-43d9dc-resume)，ID `422515632`。
- 当前登录者实际具有仓库管理员权限。复用了 Git Credential Manager 的授权，凭据只在内存及必要子进程环境中传递。

公开测试为 **Prerelease**，创建和发布均显式设置 `--latest=false`。验收仓库的 `/releases/latest` 返回 404。客户端更新源、公钥及正式清单内容均保持原样。

| 阶段 | 真实结果 |
| --- | --- |
| 创建草稿及测试标签 | 通过；主场景保留同一 Release ID，另用实际发布器执行空草稿创建检查点 |
| 上传正式包和四个清单/签名资产 | 通过；包为既有正式 2.1.0 的完整产物 |
| 部分资产恢复 | 通过；原 ZIP 资产 ID `671261845` 不变，只新增缺失资产 |
| 发布前认证重下载 | 通过；重新检查实际下载的大小、SHA-256 与签名 |
| 实际发布 | 通过；原草稿转为公开预发布，没有重建 Release 或替换已存在资产 |
| 公开匿名下载 | 通过；五个资产全部实际下载，使用系统 TLS，无授权或代理/CA 覆盖 |
| 正式 Ed25519 验签 | 通过；使用既有可信公钥 `mediaflow-stable-2026-01` |
| 完整 ZIP 校验 | 通过；复用现有本地导入验证器检查完整包结构和成员摘要，未部署或迁移数据 |
| 已发布版本重复执行 | 实际入口返回非零，Release 和资产 ID、摘要、大小保持不变 |

处理数量不等于新增上传数量。部分上传场景总共处理五个资产，实际复用一个 ZIP，新增四个旁文件：`671264647`、`671264703`、`671264781`、`671264837`。

## 公网资产核对

匿名下载来源是上述独立测试 Release 的公开地址，并非本地模拟服务器。下载后逐个计算完整 SHA-256：

| 资产 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `MediaFlow-2.1.0-20261023T075252Z-50a032a34533.zip` | 371249271 | `ab560859e0b7be22b5b2cfa2f50f9b3fc63c5489c2bd8c4246dd1c0e4b407b6c` |
| `manifest.json`、`release.json`，各自 | 32759 | `580c204f3048c3443f51c6199ee25a62bb9f88d158cdced832bc41ae6632bf44` |
| `manifest.sig`、`signature.json`，各自 | 465 | `d69ab42cf40bf808f4a66670ff467f8f18ea3b45671ef07ca7cfc5e7c393e864` |

镜像没有修改签名覆盖的任何字段。签名清单内的包地址仍指向正式仓库的原始 v2.1.0 资产；测试镜像地址单独记录。因此该结果标为 `mirror_public_verified=true`，不会冒充一次新的稳定客户端更新，也不会把 `production_chain_verified` 设为 true。

## 六类真实故障验收

| 故障 | 注入方式 | 实际处理结果 |
| --- | --- | --- |
| 部分资产上传成功 | 草稿先只上传完整正式 ZIP，再运行真实 PowerShell 入口 | 复用原资产，补齐四个旁文件，完成重下载验证 |
| 草稿已创建但发布失败 | 对实际发布 PATCH 提交非法 `tag_name=..`，同时保持 prerelease 和非 latest 设置 | GitHub 实际返回 HTTP 422；草稿快照未改变；正常入口随后成功发布 |
| 上传中断 | 对真实 `uploads.github.com` 发送 2097152 字节后关闭连接，声明完整长度为 371249271 | API 未列出已提交资产；实际入口重新上传完整包、重下载验签，保留为已验证草稿 |
| 重复执行 | 对已经公开的同一测试 Release 再次调用真实入口 | 返回失败，保留原 Release 和全部资产，无覆盖或重复创建 |
| 远程资产摘要冲突 | 在隔离草稿上传明确标识的 73 字节无效包，使用预期 ZIP 文件名 | 发布器在同名资产摘要门禁停止；资产 ID `671766908` 未改变，无覆盖或删除 |
| 签名验证失败 | 修改正式清单的 `release_notes`，保留原签名 | 在本地正式内容检查阶段拒绝，尚未访问发布认证阶段，也未创建对应远程 Release |

73 字节资产是明确的故障注入材料，不能安装；它没有被当作完整产物或成功验收。正向场景使用的是完整正式 ZIP。未生成或发布测试信任根，未重新签署正式内容，未读取或导出正式私钥。

以下检查点均按要求保留，草稿通常仅仓库授权者可见：

| 保留记录 | ID | URL |
| --- | ---: | --- |
| 完整公开预发布 | 422515632 | [公开测试 Release](https://github.com/xlxs123/MediaFlow-Release-Acceptance/releases/tag/mediaflow-pipeline-test-20261024t042956z-43d9dc-resume) |
| 上传中断后的已验证草稿 | 422522831 | [保留的中断草稿](https://github.com/xlxs123/MediaFlow-Release-Acceptance/releases/tag/untagged-31f5559b462405eeea1e) |
| 哈希冲突草稿 | 422524010 | [保留的冲突草稿](https://github.com/xlxs123/MediaFlow-Release-Acceptance/releases/tag/untagged-22d0149447b4870c4734) |
| 发布器空草稿创建检查点 | 422620873 | [保留的创建草稿](https://github.com/xlxs123/MediaFlow-Release-Acceptance/releases/tag/untagged-4e1aed143c0aadc1278c) |

空草稿检查点实际调用发布器的创建分支，明确记录上传零个资产、未发布、`draft_verified=false`，不把创建成功等同于完整验收。

## 稳定渠道和文件保护

- [正式 Latest](https://github.com/xlxs123/MediaFlow-Releases/releases/latest) 始终为 `v2.1.0`，Release ID `421736487`。
- 正式 Release 的 13 个资产名称、ID、大小、服务端摘要及公开地址前后相同。
- [固定稳定清单](https://github.com/xlxs123/MediaFlow-Releases/releases/latest/download/manifest.json) 未改变。
- 实际调用现有客户端更新核心，通过真实 HTTPS 读取正式渠道并验签，结果仍为 `up_to_date`，指向原始构建。
- 原源码 `v2.1.0` 标签保持不变。运行时代码摘要仍为 `50a032a34533e7dac91f71a2a672c9a14cbbead155400ba4c882dda87de3d97f`。
- 对事先枚举的 2.0.0、2.0.1、2.1.0 原源码、发布文件和公开签名产物执行前后完整哈希检查，**1519 个文件、2509943319 字节，零差异**。
- 本次没有清理、移动或删除用户文件、测试记录、旧版本或远程资产。验证器使用独立状态目录，不打开旧版用户媒体库、恢复上下文或 BrowserProfile。

1519 个文件是明确枚举的保护范围，并非全盘或所有用户数据扫描。没有扫描浏览器登录数据或导出凭据。

## 首次失败和安全恢复记录

最终通过报告为本地 `.release-runs/real-transaction-20261024-r4/report.json`。前序报告及失败现场全部保留：

1. R1 实际创建主草稿后，辅助程序按标签查询未返回认证草稿。改为复用现有发布器的认证 Release 列表查询，继续使用原草稿。
2. R2 完成实际发布后，匿名下载 ZIP 在约 279897920 字节处超时。报告仍为失败，部分下载保留；没有增加等待时间或跳过校验。
3. R3 在新目录重新匿名下载并验证全部资产，通过重复执行及真实上传中断恢复；随后新草稿即时回读出现可见性延迟，安全停止。
4. 创建后回读增加最多六次、间隔一秒的有限查询。R4 绑定原发布 ID、资产及稳定快照恢复；对 R3 已完整下载的五个文件重新计算哈希并验签，复用其原始成功报告，避免再下载相同 371 MB 包。中断检查点也按真实远程 ID 和摘要重新确认。
5. R4 完成哈希冲突、签名篡改、发布器创建检查点与稳定保护，最终通过。

`--resume-from` 只接受支持阶段的失败事务。恢复目录必须是新目录；先核对正式内容、可信密钥、原稳定快照、真实已公开 Release 与资产状态。已完成报告拒绝重入，旧报告不会被覆盖。

## 改动、测试及使用入口

相关增量测试实际执行 **76 项，零失败、零错误**，覆盖发布器、发布编排、Ed25519、GitHub 更新和渠道配置。Python 与 PowerShell 语法检查通过；只读复核未发现阻塞问题。

修改文件：

- `scripts/release_mediaflow.ps1`：独立 `-TestRelease` 参数集；严格隔离生产参数。
- `scripts/publish_github_release.py`：复用原正式验证流程；增加受限测试目标、非 latest 预发布及空草稿检查点。
- `scripts/release_test_transaction.py`：真实测试入口及匿名镜像验证。
- `scripts/accept_release_transaction.py`：真实 API 故障验收与有界恢复。
- `tests/test_github_publisher.py`：保留原生产断言，增加隔离、拒绝和恢复用例。
- `docs/RELEASE_AUTOMATION.md`：实际参数、证据范围与恢复说明。
- `docs/RELEASE_TRANSACTION_VALIDATION.md`：本报告。
- `docs/public-source-reviews.json`：仅更新已复核合成测试文件的摘要。

操作参数见 [发布入口说明](RELEASE_AUTOMATION.md#隔离的真实发布事务)。测试入口不访问签名保险库、不重新构建、不重签、不创建产品稳定版本。

本地原始证据：

- `.release-runs/real-transaction-20261024-r4/report.json`：最终真实事务与故障结果。
- `.release-runs/real-transaction-20261024-r3/public-verification/result.json`：五个资产实际匿名下载、验签和 ZIP 检查。
- `.release-runs/real-transaction-20261024-r2/`：真实发布、原部分资产 ID、HTTP 422 及超时证据。
- `verification/release-transaction/incremental-final-v2.txt`：76 项增量测试原始输出。

原始运行目录包含大文件和本机路径，不纳入公开源码。本报告给出可公开复核的地址、摘要及边界，不公开凭据或登录数据。

## 验收边界

这次证明了**真实隔离预发布事务与公网镜像验证可用**。按要求没有执行新稳定版发布、稳定 Latest 切换或新的正式 EXE 部署，因此 `production_chain_verified=false`。没有把创建、下载、验签、发布或源码核心检查当作新 EXE 健康检查。

没有必要增加产品版本或重新构建 EXE；未修改下载核心，不重复下载网站媒体样本。正式新版的 EXE 部署、健康检查和回退仍须在确有新产品版本时，经原发布流水线独立验收。
