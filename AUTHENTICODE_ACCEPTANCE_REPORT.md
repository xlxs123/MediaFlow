# 正式签名与隔离验收

正式证书未配置：`formal_signed=false, production_ready=false, published=false`。不存在本阶段的正式签名者指纹或正式签名包；没有修改v2.1.0正式资产、更新URL或可信公钥。

`MODIFIED_FILES.txt`仅列相对本阶段基线`368f1f00`的五个路径，不是`80a895ad`以来的历史累计清单；旧提交与报告保留。

## PR #10 收尾

重新查询实际状态：PR开放、非草稿、可合并，head `14ca8eeac286ffcefcc3c35436e180edda565017`，只改两份报告。所有必需检查通过；官方证明Job对PR事件按既有策略跳过，不冒称已生成证明。main没有生效的保护规则或强制审阅要求，没有未解决审阅线程。报告与本机JSON及真实CI一致，使用普通merge合并为 `368f1f00f6cb2c6a97895c1ef34fb3c2930736f1`，未管理员绕过或伪造审批。

## 本机验证（各组分别记录）

| 测试组 | 实际结果 | 范围 |
| --- | --- | --- |
| 证书环境与Production Inspect | 正式候选0；实际exit1、certificate_not_configured | 正确拒绝，不是签署成功 |
| Authenticode/发布安全单元组 | 30项；0失败/0错误/0跳过；93.109秒 | 合成TSA用例不等于真实服务验收 |
| SDK集成r1 | 18项通过 | 保留最初成功现场，不与r2相加 |
| 最终SDK集成r2 | 18项；0失败；197.937秒；脚本SHA-256 aed55816073b633926c447b0f3a4433459e2793b16f6c194a5762cb283c0ad03 | 实际SDK与真实PE，只用TEST ONLY证书 |
| SBOM/发布器单元组 | 42项；0失败/0错误/0跳过；14.903秒 | 与其他组独立记录 |
| 工作流策略 | 20个Action检查通过 | 未扩大写权限 |
| 本阶段完整DryRun | **通过，exit0，866.439秒**；506项，0失败/0错误/1既定权限跳过 | core477项530.527秒，900秒限制不变；UI17/output12通过 |
| 同提交main真实CI | **通过，506项0失败/0错误/0跳过；另18项SDK集成全部通过** | [Windows/官方证明](https://github.com/xlxs123/MediaFlow/actions/runs/40512840774)、[安全工作流](https://github.com/xlxs123/MediaFlow/actions/runs/40512840798) |
| 演练后独立复核 | 通过，100.686秒；10个官方证明对象，457组件/440 PE/240缺口 | 实际重算原生库存并验证CycloneDX及GitHub官方证明，不替代正式Authenticode |

现有SDK集成从10项扩充为18项：新增实际存在的错误signer、真实过期证书、只有公钥的证书及Production拒绝、最终ZIP全清单/大小/hash检查、重新提取三个PE后独立Test Verify、修改ZIP后在提取前拒绝。原有错误EKU、缺证书、不覆盖、重签拒绝及载荷篡改仍执行。新Sign拒绝同时断言没有创建输出；ZIP失败记录固定阶段名，不输出任意异常或凭据。

实际原始三个PE摘要和Root信任库前后不变；测试私钥不可导出。公钥-only夹具的RSA密钥只在内存创建并释放，只把公开证书加入CurrentUser/My。测试证书依然不受正式信任，不将0x800B0109当成正式Windows信任。

辅助运行器最初因无用导入在测试启动前退出，已修正并保留失败记录；未跳过测试、扩大超时或将失败改记成功。详细命令、逐项退出码、审阅和失败记录保存在新的隔离任务目录，不公开主机路径、密钥或用户数据。

## 剩余验收

真实正式证书私钥操作、CA链/Online撤销、真实HTTPS TSA协议、可信时间戳签署、最终Production ZIP和生产Ed25519清单均未验收。只有发行者完成 [证书配置](AUTHENTICODE_PRODUCTION_READINESS.md) 后才能继续；本阶段不发布稳定版。

## 最终同提交整体验收

[PR #11](https://github.com/xlxs123/MediaFlow/pull/11) 经真实Windows/安全检查与独立审阅后正常合并。完整演练和官方证明均对应main提交 `c3fa7bb579149366aa6c4f71ab7537b22727e3ec`、run `40512840774`；后续报告提交不冒充这个构建提交。SDK的18项结论来自实际下载并经官方证明验证的 `verification/ci/authenticode-tests.json`，18个唯一case全部通过；函数输出在日志中被消费，不能把日志中的0条逐项输出虚报为18条。

| 完整只读演练阶段 | 实际秒数 | 终态 |
| --- | ---: | --- |
| 前置配置与同提交安全/CI检查 | 16.9384 | 通过 |
| 源码披露审核 | 8.5839 | 退出0 |
| 必要增量测试 | 540.4830 | 退出0 |
| 下载、完整收据/摘要和官方来源 | 117.9021 | 通过 |
| 原生SBOM与官方证明/收据绑定 | 53.8681 | 通过 |
| 实际EXE身份启动 | 72.0714 | 退出0 |
| 完整发布结构 | 4.0668 | 退出0 |
| unsigned Authenticode检查 | 49.8117 | 退出0，仅检查未签文件 |

所有阶段都有开始/结束记录；外层exit0、总866.439秒、`remaining_owned_children=[]`。core/UI/output三组也无残留后代。唯一跳过是既有 `test_worktree_symlink_is_never_followed`，本机无创建符号链接权限；真实GitHub为零跳过，真实Gitleaks没有跳过。无保险库/正式签署阶段或签名输出，`remote_writes=false`、`signed_package_created=false`。总耗时不等于core的900秒预算；历史596.894秒属于旧f622702演练，不混用。

实际以干净c3fa7bb分支执行现有 `scripts/release_mediaflow.ps1 -Version 2.1.0 -DryRun`，显式指定同一固定Python与SecurityPython环境、已验证GitHub CLI、`-CiRunId 40512840774 -CiWaitSeconds 1`、2.0.1实际旧发布回退目录及新的 `.release-runs/formal-readiness-20261027-r1`。仅配置固定真实Gitleaks、独立Auth测试根、UTF-8、禁止字节码写入与offscreen环境；没有CertificateThumbprint/Vault参数。该运行目录现在已存在，不能覆盖重跑。原始完整argv和受限环境记录在本轮私有 `dryrun-command-r1.json`，各阶段与测试日志在运行目录，原失败记录保留。

最终匿名公网检查确认稳定清单Ed25519有效、篡改拒绝；13个资产的ID/名称/大小/服务器摘要/地址、源码v2.1.0标签和渠道内容与开始一致。清理前旧白名单1519静态文件完整哈希一致。用户另行明确要求归集闲置旧版本与缓存，该可恢复本地清理单独记录，不改变公开Release、用户数据或历史验收记录。
