# SignPath 免费签名集成就绪检查

本轮基线为主分支 `c3fa7bb579149366aa6c4f71ab7537b22727e3ec`、应用版本 2.1.0。PR #12 的最终验收报告仍是独立待审阅变更，本分支没有把该 PR 自动合并。这里只准备免费 Foundation 申请及签名桥接，不发布新稳定版。

当前：**未申请、未获批、未配置 Token、未提交真实签名请求、未完成 SignPath 正式签名，production_ready=false、published=false。** 组件资格疑问见 [资格矩阵](SIGNPATH_ELIGIBILITY_REPORT.md)，所有者操作见 [申请清单](SIGNPATH_APPLICATION_CHECKLIST.md)。

## 现有能力与本轮最小修正

复用现有 Windows 构建、隔离 SDK TestOnly 测试、完整更新回归、真实安全工作流、文件收据、原生 SBOM、GitHub 官方 Sigstore/SLSA 证明以及发布器。不修改下载、评分、浏览器登录、恢复或媒体库逻辑。

旧构建中两个 C# 辅助 PE 未设置产品/版本信息。本轮共享 `native-version-info.ps1` 在编译前从 `utils/branding.py` 获取真实 `APP_VERSION`，设置 ProductName=MediaFlow、ProductVersion=APP_VERSION 和四段数值 FileVersion。没有修改旧二进制或在签后补资源。两个脚本新增可选独立输出目录，原调用保持兼容。

`authenticode.ps1` 的 Production Verify 原本要求本地 My 存储存在显式指纹证书。远程签名私钥不应进入本机：现在仅在 Verify/Production 且存储未命中时，允许读取 `MediaFlow.exe` 的嵌入签名者，要求与经审查的 **40位精确 thumbprint** 完全一致。随后仍检查每个第一方 PE、Code Signing EKU、当前有效期、在线撤销/证书链、非测试/非自签、WinTrust、Microsoft SDK SignTool 及 RFC3161 时间戳。Sign 和配置 Inspect 仍保留原私钥/存储规则；没有加入“接受任何 Valid 证书”的选项，没有修改 Root。

现有有效期规则仍拒绝当前已过期的签名者，即使历史时间戳有效；历史包长期验签及证书轮换必须另行审阅，不能把它称为已经验证的 SignPath 兼容性。

## 官方 Action 与输入核验

官方文档：[GitHub trusted builds](https://docs.signpath.io/trusted-build-systems/github)。从官方仓库 `git ls-remote --tags` 核对：`v3` 和 `v3.0` 均指向 `f6d04783b4569d051e0c80105fe66e82819d0092`。实际 [action.yml](https://github.com/signpath/github-action-submit-signing-request/blob/f6d04783b4569d051e0c80105fe66e82819d0092/action.yml) 声明 node24；完整 SHA 固定在工作流及 `security/toolchain.json`。

实际下载的 action.yml SHA-256：`9afe14756752bb0b43421bf94e7cebdc3fead29765be4f07fbc60d82421ebc23`；分发入口 index.js SHA-256：`40af2e2581648d7357de3c13ca081ce87ac97f0aa66fdd2c5e3f1468d3232751`。官方仓库该提交没有根级 package.json，未伪造其核验结果。

仅使用该提交实际支持的 `api-token`、organization/project/policy/artifact configuration、`github-artifact-id`、等待/服务/下载超时、`parameters`、输出目录及 `skip-decompress`。不覆盖 Action 的默认 connector URL；官网文字与已固定 Action 的默认 connector 路径存在差异，服务可达性以获批后真实验收为准，不自行猜另一个 URL。

官方参数示例使用 `version` JSON 字符串；模板 [signpath-artifact.xml](security/signpath-artifact.xml) 对 **仅三个第一方 PE** 显式配置项目名/产品版本，不使用通配符重签上游 DLL、FFmpeg、yt-dlp 或 Microsoft 二进制。模板尚未在实际 SignPath 组织导入/验证。

## 默认关闭的实际工作流

既有 `ci.yml` 新增 `workflow_dispatch` 布尔参数 `signpath_request`，默认 false。push/PR 和普通手动 CI 不执行签名 Job、不读取 SignPath Token；它们只运行不需要第三方依赖或凭据的 policy status。

启用前必须完成下列代码门禁及所有者配置；第3项是所有者必须核查的外部前置，当前代码不主动查询或证明其保护规则：

1. 明确从 `xlxs123/MediaFlow` 的 main 手动请求；不是 PR、分叉仓库或未经授权的分支。
2. 既有 Windows 构建和来源证明 Job 均成功，全部上游 Job 使用 GitHub 托管 runner。
3. `signpath-foundation` Environment 由所有者设置仅 main 部署及实际 reviewer 保护；SignPath 每次签名的人工批准仍独立保留。代码仅声明该环境，没有自动创建或验证其保护规则。实际只读查询确认本轮该环境不存在，仓库级也没有 `SIGNPATH_API_TOKEN` 名称；没有读取任何 Secret 值。若未来要证明保护规则也由运行时强制，需要另行审阅只读 API 检查及拒绝测试。
4. 经 PR 审阅的 `security/signpath-policy.json` 中 enabled、foundation_approved、license_review_accepted 都是真正布尔 true，真实 organization/project/policy/artifact slug、精确证书指纹及实际 Subject 均有效。当前全部关闭/为空，不提供示例组织或临时信任根。
5. Environment Secret `SIGNPATH_API_TOKEN` 已安全配置。预检只接收“是否存在”的布尔值；真实值仅传给固定官方 Action。不写入 JSON、输出变量、日志或普通 CI。
6. 提交前再次验证当前完整收据、原生 SBOM、最新同提交 main 安全检查和 **6个实际官方证明对象**：收据、EXE、四份 SBOM/校验文档。旧成功检查不能掩盖新失败。这里6个是桥接实际核验范围，不冒称重新核验旧报告的10对象。

预检在安装签名验证依赖/下载/请求之前停止。配置状态输出与“请求已提交”“签名完成”“可发布”分开；配置为 true 也不会让 status 显示 formal_signed=true。

## 返回产物与已有发布器的边界

签名前只上传三个经过验证的第一方 PE，使用该工作流实际 `upload-artifact` 输出的 artifact ID。Service 的可信构建来源检查和手动批准不能被本地收据替代。

官方 Action 下载原始 ZIP，未自动解压。桥接对三个精确路径、重复、目录、链接、加密和大小作有界检查，读取成员字节而不执行其路径；复用 `signature_only_change` 拒绝任何载荷修改。上游 Action 负责 HTTPS 服务返回及 Content-Disposition 文件名处理；这部分尚无真实服务验收，不声称已审计上游 Action 的全部网络/文件系统实现。

之后创建全新的静态发行树，上游文件保持字节不变，运行原有 Production Verify，核对精确 Subject，生成独立 signing-derivation。原 CI 收据、原始 SBOM 和官方来源证明保持不变；不把签名后的新摘要伪称为原 GitHub 构建证明对象。

工作流最多保留“独立验签后的静态树＋变更证明”CI artifact，**不会创建/编辑 Release，也不访问 Ed25519 生产私钥**。即使此步骤未来通过，仍输出 production_ready=false、published=false。

后续实际发布必须在现有安全发布环境中执行独立包生成、受保护 Ed25519 清单签名、最终 ZIP 重新提取并 Production Verify、`publication_sbom` 派生/独立验证及正式发布门禁。现有 `release_mediaflow.ps1` 的本地 Sign 阶段尚未增加跳转云签名输出的自动选择参数；不得拿此 PR 声称已经完成云签名到正式发布的真实端到端集成。首次获批后应先隔离验收，再审阅该衔接，不通过再签名覆盖云签名来凑通流程。

## 并发隔离修正（以 PR #13 的 67f8ba8 为基线）

原工作流所有运行共用 `mediaflow-ci-{workflow}-{ref}` 并统一设置 `cancel-in-progress: true`。因此 main 上普通 push 或常规手动 CI 可能取消同一 main 上等待审批/服务的签名工作流；只修改子 Job 的并发选项不能保护父工作流。

现在在**工作流级别**使用与 SignPath Job `if` 相同的四条件分类：真实仓库、main、workflow_dispatch、显式布尔 signpath_request。普通 CI 保留原分组及自动取消；授权签名使用固定 `mediaflow-signpath-main`，取消条件为 false。签名组不包含 run_id 或 commit，后续签名运行也不能取消正在进行的同组运行。原 Job 条件、构建/来源证明依赖、Environment、Token 隔离及默认 false 均未改变；分组不是授权替代，请求仍经过原预检。

[GitHub 当前官方并发文档](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency) 明确默认 single 队列只有一个运行中和一个待运行位置；新到的签名 C 会替换待运行的 B，仍不取消运行中的 A。因此本方案**不提供保留所有意图的队列，也不承诺按触发顺序 FIFO**。官网另提供最多100待运行的 `queue: max`，但不能与 `cancel-in-progress: true` 组合。本次保留兼容的默认 single，不为此拆分已验证的 CI 或引入未经验证的动态 queue 配置。

操作要求：一次只提交一个签名意图，确认前一运行和远端请求已结束再发起下一次。待运行被替换的工作流尚未执行其 Job，可在核对后手动重新提交。**已提交远端请求的运行**如果被人工取消、超时、runner/网络故障终止，远端请求仍可能等待审批或已完成；必须先按 request ID、源提交及产物在 SignPath 核对状态，再决定重试，不能将 GitHub 取消等同于远端取消或自动重复申请。

策略门禁锁定上述工作流分组与取消表达式，拒绝 CI Job 级并发覆盖及其他工作流/Job对保留组名的字面占用（含大小写变体），并锁定现有 security 工作流的独立分组。其他工作流和 Job 中未经审阅的动态并发组会失败关闭，避免通过计算组名绕过字面检查；新增动态分组必须先单独审阅并明确扩展策略，普通非保留的固定组名仍可使用。这里不是通用 GitHub 表达式安全分析器。回归测试从实际 YAML 求值规范上下文的授权矩阵，并建立默认单待运行位置的本地状态模型；这不是在真实签名服务执行并发验收。GitHub 表达式的字符串比较忽略大小写，最终严格上下文检查仍由既有请求预检承担，不通过本次分组修改扩大权限。正式签名开关与凭据仍未配置，不因测试触发真实服务。

## 本轮验证记录

本节最终按实际命令和 CI artifact 更新。不能把未启用的 Job、模拟 ZIP 或历史 CI 记为真实 SignPath 签名成功。

- 本地新增桥接最终29项通过，0失败/错误/跳过；仅为配置、实际ZIP/合成PE字节与拒绝路径单测，不能当作真实CA签名。两个独立审阅发现的预检Qt隐式依赖与SBOM/receipt未互绑均已修正并增加拒绝回归；复制上传前也逐文件对原receipt核对size/SHA-256。
- 本地原生元数据4项通过，0失败/错误/跳过：实际构建Recycle并独立读取Win32版本资源；另一项目版本7.8.9也实际编译；11个无效版本子情形不另加测试数。没有缓存WebView SDK，因此本地只测试其真实Offline拒绝路径，不把它算作Host正向构建。
- 既有Authenticode/签后变更/SBOM增量组51项通过，0失败/错误/跳过；工作流/公开源码边界组28项通过、1项既定本机符号链接权限跳过。各组包含不同或重复用例，不能相加为唯一回归总数。
- 固定版本actionlint实际exit0；工作流策略检查27个Action引用通过。全历史和精确暂存区真实Gitleaks扫描33个commit、537个历史文件版本、415个暂存文件，最终0阻断。第一次扫描因原有合成夹具前插8行而拒绝；确认夹具字节未变后，仅追加6个精确Blob/规则/行号/值摘要映射，保留全部历史例外，未加目录或规则忽略。后续提交扫描与CI仍需按实际快照独立核对。
- 完整本地回归最终为540项：core 511、UI 17、output 12，0失败/0错误/1项上述本机符号链接权限跳过；各组分别431.063、3.679、2.480秒，core的900秒预算未改，无残留后代。首次重跑前的core为511项/1错误/1跳过：既有进程树夹具在PID文件创建后、数字写完前读取空文件。仅将子进程夹具改为写完并关闭后原子发布PID文件；针对性10项通过，再完整重跑通过。保留首轮失败记录，不把两轮相加。
- 实际SDK TestOnly集成20项全部通过，原输入PE和Root信任库保持、无私钥导出。包括新增的LocalMachine存储未命中时精确嵌入测试签名者仍被Production拒绝、错误指纹不能替代嵌入签名者两项。SDK来源为先前已验证的c3fa未签名静态树，不假称本地已重建完整新应用；无真实Foundation/受信CA接受路径验收。
- 本分支GitHub Windows完整构建、EXE实际启动、新PE元数据与安全检查的真实结果，以PR关联的[Windows CI](https://github.com/xlxs123/MediaFlow/actions/workflows/ci.yml?query=branch%3Afeat%2Fsignpath-foundation-readiness)及[安全CI](https://github.com/xlxs123/MediaFlow/actions/workflows/security.yml?query=branch%3Afeat%2Fsignpath-foundation-readiness)和产物为准，不用历史main构建代替，也不将PR中按设计跳过的正式来源证明/SignPath Job记为真实签名通过。
- 当前无账号/服务批准、无真实 SignPath 请求、无受信 CA 接受路径验收；TestOnly 签名与合成证书表字节测试分别标识。
- v2.1.0 的13个正式资产、稳定 URL、更新公钥保持原值；本阶段无清理/迁移用户文件行为。

## 所有者一次性操作与下一步

只接受免费：在 [官方申请入口](https://signpath.org/apply.html) 自行核查表单和法律条款，处理 Qt/PySide、实际 MSVC 及许可证据疑问，确认真实联系人/角色/MFA；遇付费承诺立即停止。获批后配置官方 GitHub App/trusted build、需要人工批准的政策、精确证书公开身份和受保护 Environment。Token 直接填入 GitHub Settings → Environments → signpath-foundation → Environment secrets，**不要发送到聊天**。

政策归功声明仍只位于 [未生效草案](SIGNPATH_CODE_SIGNING_POLICY_DRAFT.md)，未放到正式 README/下载页。所有者确认免费服务已启用后，才通过独立审查把 “Code signing policy” 链接加入源码主页及发布仓库下载页。
