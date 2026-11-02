# Code signing policy（未生效草案）

核查日期：2026-10-28。**本文件用于免费 SignPath Foundation 申请准备，不是已启用的签名政策。** 尚未获得 Foundation 批准、提交真实签名请求或完成其正式 Authenticode 签名。正式稳定版仍为 2.1.0；本草案不改变既有发布文件、更新源或可信公钥。

## 签名服务与证书身份

拟申请的免费服务提供者为 [SignPath.io](https://signpath.io/)，使用的证书预计属于 SignPath Foundation，而非作者个人或 MediaFlow。Foundation 并非证书颁发机构（CA）；实际 CA、证书 Subject、指纹、有效期和链应在获批后从官方确认的证书与签名产物核对，不在此预设。要求依据：[Foundation 条件及证书说明](https://signpath.org/terms.html)。

下列官方要求的归功文字**仅为获批并实际启用后的待发布文字**，目前不能作为已提供服务的事实放入主页或下载页：

> Free code signing provided by SignPath.io, certificate by SignPath Foundation

启用前须解决 [资格审查](SIGNPATH_ELIGIBILITY_REPORT.md) 的组件许可疑问并由 Foundation 决定是否接受。只使用明确免费的服务；不购买证书、不订阅付费计划、不作付费承诺。

## 角色与审批

目前可公开核实的仓库所有者是 [xlxs123](https://github.com/xlxs123)，尚未取得已确认的 SignPath 团队角色名单或成员 MFA 证明。下表是**待所有者确认的职责安排**，不是已经授予的账号权限或审批记录。

| 角色 | 职责 | 人员状态 |
| --- | --- | --- |
| Authors / committers | 维护项目源码、依赖、构建脚本和签名配置，对变更负责 | 公开仓库所有者 xlxs123；完整授权名单待确认 |
| Reviewers | 审阅外部贡献；重点审阅构建、第三方来源、工作流权限和可信签名身份的修改 | 待所有者指定真实审阅者，不虚构第二名成员 |
| Approvers | 每次核对版本、源码提交、测试、安全扫描、来源证明、SBOM、待签文件及签名范围，再在 SignPath 人工批准 | 待所有者指定并在 SignPath 配置；不得自动替代批准 |

所有实际参与成员均须按 Foundation 要求对 GitHub 与 SignPath 使用 MFA。公开仓库页面不能证明个人 MFA 或 SignPath 权限。官方条件规定职责和逐次批准；本草案不把“必须有两名不同自然人”冒充其现行普遍要求。单人团队的角色安排与可接受性须如实向 Foundation 确认。

变更通过独立分支和 PR 审阅；不绕过保护规则、跳过失败检查或伪造审批。未获授权的分支、fork、未通过必需检查的构建不能提交正式签名。即使未来工作流自动提交请求，也保留 Foundation 要求的人工批准。

## 软件用途与保护边界

MediaFlow 是 Windows 媒体下载与本地媒体管理工具，支持 Direct HTTP、HLS、DASH、浏览器/CDP 媒体发现、批量队列、任务恢复、字幕音轨选择和本地媒体库。用户应只保存有权访问和下载的内容，网站使用条款与内容权利由实际场景决定。本项目不以签名证明内容下载已获得权利。

申请须完整披露下列功能，不用“不支持 DRM”一句话代替实际功能审查：

- 支持在内嵌浏览器登录、使用当前会话请求上下文，并由用户主动导入当前站点的 Cookie。系统浏览器导入调用 yt-dlp；遇加密/锁库错误停止，不实施额外解密绕过。入口及边界见 `ui/browser_environment.py:74`、`core/browser_cookies.py:224`。
- 对 rou.video 等实际实现了站点解析及 PNG/roUd 媒体容器解码；仍重新检查解码后的清单，不提取 DRM 许可证或密钥。见 `core/site_resolvers.py:337`、`core/rou_playlist_decoder.py`、`core/media_guard.py:185`、`:242`。这些功能是否符合 Foundation 对规避安全措施的边界，由其审核，不隐瞒处理。
- 明确的 Widevine、PlayReady、FairPlay 等保护标记会触发拒绝；普通 HLS AES-128 与未知加密方式区分处理，HTTP 403 不被当作 DRM 证据。见 `core/manifests.py:325`、`core/media_guard.py:211`、`core/dash_json.py:300`、`core/downloader.py:180`。
- 开发阶段的 CodeQL、依赖与秘密扫描审计源码和构建产物，不是面向用户系统的漏洞利用或端口扫描功能。有限源码审阅不能证明不存在未知问题或保证 Foundation 必然接受。

## 隐私与网络行为草案

本节描述已阅读源码中的行为，不作“从不联网”“绝不传输数据”或“第三方完全无遥测”的绝对承诺。本轮没有访问真实浏览器 Profile、用户数据库、Cookie 或密码；以下依据源码与第三方官方说明。

| 场景 | 网络与数据 | 控制及限制 |
| --- | --- | --- |
| 网页加载与媒体发现 | 用户提交网页后，WebView2/Qt WebEngine、网页脚本、子框架和站点资源可能继续请求网站、CDN及其他第三方；站点会收到 IP、请求头及适用的登录状态 | 由用户选择页面；站点自己的隐私政策与 Cookie 行为同样适用，不能保证仅访问地址栏同一主机 |
| 下载、Resolver、音轨与字幕 | MediaFlow 原生管线或 yt-dlp 访问解析出的页面、API、重定向及媒体资源；合法请求可能带 Referer、Cookie 或 Authorization | 用户触发下载；上下文派生与重定向有独立处理，不能声称所有 HTTP 请求均不含凭据，见 `core/request_context.py:108`、`:124` |
| 登录与持久化 | 内嵌持久 BrowserProfile 保存站点 Cookie/存储；可选 Cookie 导入会短暂使用本次临时目录的明文文件，再按当前页面域筛选、导入 | 用户主动操作，见 `ui/browser_environment.py:74`、`core/browser_cookies.py:248`；不保证崩溃或操作系统层面从未留下痕迹。浏览器清除、迁移与正常关闭行为分别处理 |
| 恢复与历史 | 应用设置、任务、历史索引及文件路径保存到本机；恢复请求上下文使用当前 Windows 用户的 DPAPI 密文 | 不是所有记录都被整体加密。见 `utils/config.py:34`、`core/task_store.py:141`、`:252`；不将浏览器登录内容自动同步给项目维护者 |
| 应用更新 | 手动检查，或用户启用后在启动时请求配置的 HTTPS 清单；获用户确认后下载发布包，涉及 GitHub 与其资产 CDN | `core/update_manager.py:176` 默认 `startup_check=False`；界面可关闭，见 `ui/update_dialog.py:341`。检查、下载、部署、运行健康检查是分离阶段 |
| 外部组件 | WebView2 Runtime 具有 Microsoft 定义的诊断、安全及更新行为；第三方网站、浏览器引擎和外部工具的行为并非项目可全面保证 | 见下方第三方说明；Windows 可选诊断设置不等于关闭一切必需数据，不能以应用未内置统计模块推导所有组件零通信 |

本轮在项目的 `core`、`ui`、`utils`、`native` 和 `main.py` 中未识别出项目自建统计上传模块；这是有限的源码核查结果，不是全组件动态流量审计。正式启用政策前，所有者仍应核对所签版本的实际配置、网络行为及第三方政策，补充用户可见的隐私说明；需要安装流程披露或退出选项时先落实，再请求批准。

第三方参考：[Microsoft WebView2 数据与隐私](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/data-privacy)、[Microsoft 隐私声明](https://privacy.microsoft.com/privacystatement)、[Qt 隐私政策](https://www.qt.io/terms-conditions/privacy-policy)、[GitHub 隐私声明](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement)。Qt 通用政策的链接不意味着已证明本包包含其全部服务或遥测；实际网站的政策应由访问者另行查看。

## 构建、签名与发布验证

拟签名的输入必须由项目源码在 GitHub 托管 runner 构建，绑定精确提交、工作流运行、已通过的检查和已上传 artifact；按 [官方 GitHub 集成](https://docs.signpath.io/trusted-build-systems/github) 提交该 artifact。签名范围只包含经审查的第一方 PE；不重签 FFmpeg、yt-dlp、Microsoft 或 Qt 等上游二进制。

SignPath 请求权限、批准权限和令牌分别受控。获批后仅将 `SIGNPATH_API_TOKEN` 存入仓库受保护的 `signpath-foundation` Environment Secrets；不得把令牌贴进聊天、日志、源码或普通配置。`security/signpath-policy.json` 中的启用状态、获批状态、项目/策略/产物配置及精确证书指纹须经单独审阅才能更改，远程清单不能自行建立可信身份。

返回的三个第一方 PE 必须通过现有独立 Authenticode 验签：预期的精确证书身份、有效链及撤销检查、代码签名 EKU、RFC 3161 时间戳、PE 载荷与第三方文件保全。证书属于 Foundation 不等于信任任意签名者；不因证书不在本地私钥存储就要求导出其私钥。平台实际证书及配置未确认时保持拒绝。

签名后不再修改 EXE。SBOM 和包摘要绑定返回的真实字节；最终 ZIP 重新提取并独立验签，再核对 SHA-256 与 Ed25519 更新清单。Authenticode、Ed25519、SHA-256、GitHub 来源证明和 SBOM 分别承担身份、清单授权、内容完整性、构建来源和组件描述职责，不能互相替代。正式签名包与未签名 CI 产物也不能共用一个“正式就绪”结论。

不覆盖现有版本、媒体或用户数据；回退应用版本不无条件倒退数据库。不自动发布新稳定版、删除历史版本或修改现有 v2.1.0 资产。具体实施与尚未完成的验证见 [集成就绪报告](SIGNPATH_INTEGRATION_READINESS.md)。

## 系统改动、卸载及安全联系

当前便携版不以申请签名为由安装根证书、关闭安全检查或修改 Windows Root 信任库。若后续提供安装器、系统集成或其他设置修改，应先向用户说明，并提供相应卸载路径。便携版停用可关闭程序后移除其程序目录；下载媒体、用户数据和浏览器配置不因卸载程序而自动删除。需要删除这些数据时，须另行确认与备份，不借卸载执行隐式清理。

安全问题遵循 [SECURITY.md](SECURITY.md)：优先使用仓库实际启用的私密报告入口；入口不可用时，在 [Issues](https://github.com/xlxs123/MediaFlow/issues) 仅说明问题类型并请求私密联系，不公开凭据、利用细节或浏览器配置。当前没有已确认的独立安全邮箱或固定响应时限，不在本草案虚构。团队须配合 Foundation 对违规或签名异常的调查，真实问题出现时停止相关签名和发布并保留证据。

## 生效前条件

本草案只有在组件资格疑问获得处理、所有者确认真实角色与 MFA、Foundation 接受项目、免费服务及证书配置完成、真实隔离签名和独立验证通过后，才可由审阅过的变更改为生效政策。随后才准备在源码 README 和独立发布仓库的下载/发布页增加名为 **Code signing policy** 的链接和实际归功文字；本阶段未修改这些页面。
