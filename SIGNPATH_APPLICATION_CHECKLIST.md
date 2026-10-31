# SignPath Foundation 免费签名申请清单

核查日期：2026-10-28。当前状态为**材料与安全集成准备**；尚未提交申请、获得批准、提交真实签名请求或获得 Foundation 正式签名。本清单不触发账号注册、条款接受、身份资料上传、购买或签名批准。

## 已准备的项目信息

| 项目 | 申请材料及当前边界 |
| --- | --- |
| 名称与功能 | MediaFlow：Windows 媒体下载与本地媒体管理工具，不是泛化的无网络播放器；功能和受限项见下方申请摘要 |
| 源码 | <https://github.com/xlxs123/MediaFlow>；自编源码 MIT，不等于整个分发包只有 MIT |
| 已有正式下载 | <https://github.com/xlxs123/MediaFlow-Releases/releases/tag/v2.1.0>；不在申请阶段替换已有资产 |
| 发布仓库 | <https://github.com/xlxs123/MediaFlow-Releases> |
| 隔离发布验收仓库 | <https://github.com/xlxs123/MediaFlow-Release-Acceptance>；不是稳定更新渠道 |
| 组件许可与资格 | [SIGNPATH_ELIGIBILITY_REPORT.md](SIGNPATH_ELIGIBILITY_REPORT.md)，保留 Qt/PySide 商业双许可等需要官方解释的疑点，不隐去组件 |
| 政策草案 | [SIGNPATH_CODE_SIGNING_POLICY_DRAFT.md](SIGNPATH_CODE_SIGNING_POLICY_DRAFT.md)，归功文字未生效，角色与 MFA 待确认 |
| 构建与签名准备 | [SIGNPATH_INTEGRATION_READINESS.md](SIGNPATH_INTEGRATION_READINESS.md)，区分本地检查、真实 CI、TestOnly 与真实服务状态 |
| 安全问题入口 | [SECURITY.md](SECURITY.md)；私密漏洞入口是否实际启用须由所有者确认，不虚构联系邮箱 |

提交申请时可使用以下**如实描述的摘要**，由所有者审阅后自行填写官方表单：

> MediaFlow 是使用 Python、PySide6 与 WebView2 的 Windows 桌面媒体下载和管理工具。它支持 Direct HTTP、HLS、DASH、用户网页会话中的网络媒体发现、批量队列、恢复、音轨字幕与本地媒体库。用户可以在内嵌浏览器登录，也可以主动导入当前站点的 Cookie；站点解析包括对部分非标准媒体容器的解码。代码对明确的 DRM 保护标记执行拒绝，不提供 DRM 密钥提取或许可证绕过。用户必须有保存内容的权利。项目具有公开源码、构建和验收记录，发行包还包含第三方组件；具体许可与 Foundation 条件的疑问随组件报告一并披露，请官方审核。

上述最后一句是审核请求，不是已经满足所有条件的声明。不能因项目 MIT、已有 Ed25519 更新签名或 GitHub 来源证明就填报“已获得正式 Windows 签名”。

## 正式申请前的逐项检查

| 项目 | 当前状态 | 完成条件 |
| --- | --- | --- |
| 自有源码、源码仓库与实际发行记录 | 已具备可核对材料 | 所有者确认控制权及申请身份，与真实公开仓库一致 |
| 全包许可是否符合免费条件 | 待解决/官方确认 | 提交实际组件、许可和来源，不仅提供根 LICENSE；特别说明双许可、外部 Runtime 与尚不能完整识别的依赖 |
| 禁止项目、隐私与网络行为 | 已准备源码说明，需官方审核 | 披露登录上下文、Cookie 导入、CDP、站点解析和普通 HLS 解密，不能只选有利功能描述 |
| 作者、审阅者、批准者 | 需要所有者信息 | 提供真实成员或权限组、职责；当前只能核实公开仓库所有者 xlxs123，不能虚构团队 |
| GitHub 与 SignPath MFA | 需要所有者确认 | 所有实际参与者均启用所需 MFA，不要求向聊天发送恢复码或安全凭据 |
| 每次发布人工批准 | 待账号/政策配置 | 在 SignPath 配置真实 approvers；自动提交请求不能代替批准 |
| 可验证来源与 GitHub 托管构建 | 已有工程准备，服务端待配置 | 将官方 GitHub trusted build system 关联到获批项目，按其要求安装/授权 GitHub App；签名输入必须是来源可核对的 artifact |
| 第一方 PE 元数据与签名范围 | 待正式服务核对 | 项目名、版本、三个第一方 PE 及产物配置匹配，第三方组件不改签 |
| 有效的政策链接与第三方隐私说明 | 当前仅草案 | 获批并真实启用后，才通过审阅后的变更将 Code signing policy 链接放到主页和下载/发布页 |
| 真实免费签名与最终包验收 | 尚未执行 | 请求、人工批准、签名返回、独立 Authenticode 验签及最终 ZIP/SBOM/清单一致性均实测成功 |

官方依据为 [Foundation 条件](https://signpath.org/terms.html)、[申请入口](https://signpath.org/apply.html) 和 [GitHub 构建集成](https://docs.signpath.io/trusted-build-systems/github)。当前条款页面自身标为 Draft；不据此假设任何例外或免审资格，申请时重新核对实际条款并由官方决定。

## 需要所有者亲自完成的操作

这些事项不能由现有代码、公开页面或本机有 GitHub 权限推导为已经完成。只有这些外部条件需要所有者操作，不要求重复运行已自动完成的工程检查。

1. **审阅资格疑问并确定联系资料。** 核实组件报告，准备项目所有权、申请联系人及可用联系邮箱、真实角色名单、项目/下载/许可证/构建链接。此处列的是应准备的信息，未声称它们全是当前网页的必填字段。不要提供密码、个人证件、Cookie、SignPath Token 或恢复码到聊天。
2. **使用官方免费申请入口。** 在 <https://signpath.org/apply.html> 自行查看实际表单，决定是否注册所需账号、接受条款以及提交申请或身份/组织材料。页面目前以 HubSpot 嵌入表单加载，本轮仅读取公开 HTML，没有填写/提交，也未核实每个动态表单字段。若页面要求付费或合同付费承诺，停止，不选择付费替代方案。
3. **与 Foundation 确认疑点及真实角色。** 如实提交 Qt/PySide 双许可、实际分发组件及隐私/下载行为说明。确认单人或实际团队的角色安排是否被接受；不把 Codex 或 CI 机器人当作已经接受职责的审阅者或批准者。
4. **账号获批后配置安全策略。** 确认 GitHub 与 SignPath MFA，按官方说明创建/关联项目、trusted build system、GitHub App 授权、产物配置与需要人工批准的签名政策。只授予实际源仓库/签名提交所需权限，不授权未知应用读取私有数据。
5. **填写经审查的公开信任配置。** 根据批准结果确认 organization ID、project slug、signing policy slug、artifact configuration slug，以及实际证书的精确公开身份/指纹。按实现填写 `security/signpath-policy.json`；尚未获批时 `enabled=false`、`foundation_approved=false`。不能从未验证的远程清单自动导入可信身份。
6. **将 Token 直接保存到 GitHub。** 设置位置：源码仓库 **Settings → Environments → signpath-foundation → Environment secrets**，名称为 `SIGNPATH_API_TOKEN`。为该 Environment 设置获授权的部署分支和可行的人工 reviewer 保护。Token 只需有批准项目/政策要求的提交权限；不写进公开 policy JSON、普通 CI、发布 ZIP 或日志。不要把 Token 贴到聊天中。没有获批及真实配置时保持关闭，不创建测试 Token 冒充正式凭据。
7. **批准一次隔离的真实签名验收。** 在获批的 SignPath 政策中由真实 approver 人工批准，核对签名证书、链/撤销状态、时间戳及最终包检查。失败、超时、未知签名者均停止；不发布新稳定版来凑验收。没有这些证据，继续标记真实 SignPath 验收未完成。

## 工程与发布保护检查

- 普通 CI 和 PR 测试不获取 `SIGNPATH_API_TOKEN`，不访问生产私钥或提交签名请求。
- 未获批准、缺少配置或 Token 时，正式请求应明确停止，不能用测试签名、已有 Microsoft 签名或来源证明获得放行；实际测试结果由集成报告记录。
- 不把未签名构建、TestOnly ZIP 和真实签名后的完整发行包混用。返回签名后不修改 PE；第三方文件保持原字节，签名后更新 SBOM/摘要并重新验证最终包。
- Ed25519 更新清单私钥继续留在现有安全发布环境，不复制到普通 CI 或 SignPath Token 配置。
- 本阶段不修改 v2.1.0 的 13 个稳定资产、不改变稳定更新地址/可信公钥、不清理或移动用户文件、不删除历史验收记录。
- 不将 README/下载页的待生效政策说明提前改写成“已提供 SignPath 免费签名”。若未来新增安装或系统改动，先落实隐私说明、必要选择项及卸载流程。

## 证据与状态回填

保留申请编号、官方批准记录、已审阅的公开配置变更、真实签名 request ID、批准者与来源提交、CI 运行链接、签名验证和最终包报告。只公开适当的记录标识与公开证书信息，不公开令牌、个人验证材料或未脱敏日志。

当前所有申请/真实签名状态均为未完成。申请通过、请求提交、人工批准、签名成功、包校验通过和发布就绪是不同阶段，必须根据实际证据逐项更新，不能用“材料准备完成”替代。
