# SignPath Foundation 免费签名资格审查

核查日期：2026-10-28。结论是**可以准备申请，但当前不能认定整个 Windows 发布包已满足免费签名条件**。项目自身的 MIT 源码、持续维护、公开构建和已有发布有实际证据；Qt 许可政策的适用范围、再分发运行库的系统库例外、许可证据缺口、项目声誉和所有者账号信息仍需补充或由 SignPath 确认。本次没有提交申请、签名请求或接受服务条款，没有获批，也没有完成正式 Authenticode 签名。

## 审查基线和方法

- 源码：`xlxs123/MediaFlow`，主分支基线 `c3fa7bb579149366aa6c4f71ab7537b22727e3ec`；稳定版仍为 [v2.1.0](https://github.com/xlxs123/MediaFlow-Releases/releases/tag/v2.1.0)。本报告不把新分支的准备代码冒充已发布的版本。
- 实际 Windows 构建：[run 40512840774](https://github.com/xlxs123/MediaFlow/actions/runs/40512840774)，读取此前独立下载、验证并保留的 `ci-build/dist-mediaflow-ci` 和同次 `ci-sbom`。没有重新下载大型源码资产，也没有改写已有包、SBOM 或验收报告。
- 实际读取 `LICENSE`、`THIRD_PARTY_NOTICES.md`、`requirements.txt`、`MediaFlow.spec`、`build.ps1`、两个原生宿主构建脚本、发布包的工具来源和许可证、原生库存与严格校验结果。仅检查静态构建资料，不读取浏览器配置、用户数据库、媒体库或私钥。
- 对照 [SignPath Foundation 条件](https://signpath.org/terms.html)、精确版本的上游许可和发行资料。第三方文件名、证书存在、SBOM 标签及项目根 MIT 均不单独作为完整许可结论。

实际 CycloneDX 1.6 文档 SHA-256：`ccfc15754565dde44d4e984153c105a23fba9791de27598d5fa0214fbbbcf5c3`。对应 [SBOM CI 资产](https://github.com/xlxs123/MediaFlow/actions/runs/40512840774/artifacts/12737748835) 有保留期限，下载可能需要 GitHub 登录。

## 资格矩阵

下列“已满足”仅说明本轮观察到对应事实，不能替代服务方批准。官方条件页面当前仍标为草案，申请时应再次核对。

| 核对项 | 状态 | MediaFlow 实际证据及下一步 |
| --- | --- | --- |
| 自编源码开源 | 已满足 | 根 `LICENSE` 为 MIT；不把该许可扩展到全部捆绑软件。 |
| 维护、公开发布、功能说明 | 已满足 | 近期主分支提交、公开 CI、v2.1.0，以及 README 和下载页对实际功能的描述。 |
| 整包许可和商业双许可边界 | 需要 SignPath 官方审核确认 | Qt/PySide 的开源选项与商业发行并存；另有运行库例外及证据缺口，见下表。 |
| 非恶意软件／禁止项目政策 | 需要 SignPath 官方审核确认 | 本轮未发现漏洞探测或 DRM 密钥提取入口；媒体发现、合法会话使用及站点协议解析须完整披露，不能以有限代码检查保证不存在任何问题。 |
| 构建来源 | 已满足现有技术证据，仍需服务方接入 | Windows 托管 CI、构建收据、完整包摘要、SBOM 和独立官方来源证明已有实测；SignPath 自己的 GitHub 来源核验尚未建立。 |
| 拟签第一方范围 | 已明确，待配置审核 | 仅三个第一方 PE；不重新签 FFmpeg、yt-dlp、Qt、Microsoft 或 Python 上游二进制。 |
| 第一方 PE 产品元数据 | 基线尚未满足；新构建待验证 | 两个 C# 宿主在本次实际基线缺少 ProductName、版本为 0.0.0.0。新分支已准备编译期元数据修正；不得修改旧包或在签后修改 PE 来掩盖。 |
| MFA、角色和逐次人工批准 | 需要项目所有者提供信息 | 不能从公开仓库证明 GitHub／SignPath MFA、具名作者、审阅者、批准者及其权限；不虚构成员或审批。 |
| 生效的签名政策和隐私说明 | 尚未满足 | 本轮准备草案；免费服务归功说明不得在未启用前作为正式事实发布。 |
| 项目声誉、最终受理 | 需要 SignPath 官方审核确认 | 公开提交和发布可查，不设想不存在的最低星数要求，也不保证新项目被受理。 |
| 申请账号、条款、凭据 | 需要项目所有者操作 | 没有代注册、身份提交、合同承诺或配置真实 Token；只接受免费服务。 |

## 实际分发组件及许可边界

`MediaFlow.exe` 是 PyInstaller one-file 程序；只查看发布目录中的散装 DLL 会漏掉归档内依赖。本次读取的库存覆盖 190 个静态发布文件、2,996 个已列出的归档成员、440 个实际 PE：8 个物理文件和 432 个内嵌 PE。这里的“覆盖”是库存和字节身份检查，不是所有组件均已通过法律审查。

| 组件／实际位置 | 已核对的许可、来源与分类 | 免费签名资格处理 |
| --- | --- | --- |
| MediaFlow 三个第一方 PE | `MediaFlow.exe`、`tools/MediaFlow.Recycle.exe`、`tools/webview2/MediaFlow.WebView2Host.exe` 来自本仓库 Python／C# 和构建脚本；自编部分 MIT。 | 可作为申请签名范围；嵌入的上游软件仍保留其独立许可。 |
| Qt／PySide6／shiboken6 6.11.2 | `MediaFlow.exe!/PySide6/...`、`!/shiboken6/...` 和实际分发元数据。精确 [PySide6](https://pypi.org/pypi/PySide6/6.11.2/json)、[Essentials](https://pypi.org/pypi/PySide6_Essentials/6.11.2/json)、[shiboken6](https://pypi.org/pypi/shiboken6/6.11.2/json) 官方 PyPI 元数据声明 LGPL-3.0-only／GPL-2.0-only／GPL-3.0-only 选项；[Qt 6.11 官方许可](https://doc.qt.io/qt-6.11/licensing.html) 与 [Qt for Python 商业说明](https://doc.qt.io/qtforpython-6/commercial/index.html) 也明确存在商业发行。 | **商业双许可条款的适用范围需书面确认**；不能由应用 MIT 推定符合。Foundation 的 [ScientoPy 项目页](https://signpath.org/projects/scientopy/) 实际列有 MIT、PySide6/Qt 项目，说明也不能仅凭使用 Qt 就断言自动拒绝；该案例不代表 MediaFlow 获批。 |
| Qt WebEngine／Chromium | 实际归档内 QtWebEngine PE；发布包 `licenses/QtWebEngine/` 含来源和第三方许可页；[对应 Qt 6.11 文档](https://doc.qt.io/qt-6.11/qtwebengine-licensing.html)。 | 按实际模块及嵌入第三方分别确认许可和对应源码；不把 Chromium 全部称为 LGPL，也不因有许可网页就推定所有静态依赖已枚举。 |
| FFmpeg／ffprobe | 实际 `tools/ffmpeg.exe`、`tools/ffprobe.exe` 的 SHA 与 `tools/provenance.json` 对应 BtbN `n9.0.2-33-g4cf298e66a-20261022`；包内实际配置启用 GPL、version3、libx264、libx265，未见 enable-nonfree。GPLv3+ 和源码资产索引随包提供。[FFmpeg 官方许可](https://ffmpeg.org/legal.html)。 | 属开源上游工具，保留原字节、不用 MediaFlow 名义重签。配置选项不是所有静态库的版本／许可证明，GPL 对应源码义务与 Foundation 资格分别审核；不声称一次阅读索引完成全部许可证合规认证。 |
| yt-dlp 2026.08.19 standalone | 实际 `tools/yt-dlp.exe` 摘要对应固定上游发行 URL；`tools/yt-dlp-LICENSE.txt` 和 `yt-dlp-THIRD_PARTY_LICENSES.txt` 随包。[固定版本说明](https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/README.md) 及其第三方清单区分 Unlicense 源码与含第三方组件的组合程序。 | 不能把整个 EXE 简化为 Unlicense。保留组合分发 GPLv3+ 等适用义务，不重签上游 EXE，不把其内嵌 Python、原生库和 VC Runtime 遗漏。 |
| WebView2 SDK 1.0.4258.31 | `tools/webview2/Microsoft.Web.WebView2.Core.dll`、`WebView2Loader.dll`。构建脚本锁定官方 NuGet 包 SHA-256，直接复制该包 LICENSE／NOTICE；[精确 NuGet 许可证页](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4258.31/License) 的 BSD 3-Clause 条件与实际 LICENSE 文本相符。 | **不能凭 Microsoft 名称将本版 SDK 直接认定为专有阻塞**，也不能因 Microsoft 签名就认定开源。向审核方提交这两文件、精确包版本及许可，确认服务方接受范围；Core DLL、loader 与外部 Runtime 分开说明。 |
| Evergreen WebView2 Runtime、.NET Framework | MediaFlow 使用用户系统的 Runtime／Framework，当前 190 文件清单没有捆绑整个 Edge／Evergreen Runtime。微软官方说明区分 [Runtime 分发与 SDK 所需文件](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)。 | 属外部前置，不冒充本项目 OSS；Foundation 是否认可相应系统库边界由官方确认。保留用户安装与升级行为说明。 |
| 实际捆绑 Microsoft VC Runtime | 主 EXE、PySide6、shiboken6、yt-dlp 归档中存在 VCRUNTIME140／VCRUNTIME140_1／MSVCP140 等实际字节及 Microsoft 产品版本；不同位置有不同版本。 | 不属于“仅导入但未随包”的操作系统 DLL。官方 [VS 2022 再分发清单](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution) 附有授权条件；当前精确版本的再分发权及 Foundation System Libraries 例外须补证，不能归到 MIT／Qt 许可，也不能由文件名认定系统例外。 |
| CPython 及内嵌标准扩展 | `MediaFlow.exe` 和 yt-dlp 归档内实际 CPython／PYD；包有 `licenses/Python/LICENSE.txt`。[官方 Python 许可说明](https://docs.python.org/3/license.html) 包含 PSF 和纳入组件的独立许可。 | 已有 OSS 许可依据；OpenSSL、SQLite、libffi 等原生子组件仍需与实际版本、来源和许可逐项绑定，不据 PSF 许可包揽全部。 |
| cryptography 50.0.2、cffi 2.1.1、packaging 26.3 | 真实分发／模块字节证据。随包文本与精确官方 PyPI 元数据交叉核对：[cryptography](https://pypi.org/pypi/cryptography/50.0.2/json) 为 Apache-2.0 或 BSD-3-Clause，[cffi](https://pypi.org/pypi/cffi/2.1.1/json) 为 MIT-0，[packaging](https://pypi.org/pypi/packaging/26.3/json) 为 Apache-2.0 或 BSD-2-Clause。 | 目前没有已确认的商业许可阻塞；仍以实际进入包的模块与原生依赖为准。两个开源许可之间的任选不等于商业双许可。 |
| PyInstaller 6.22.3 | 既是构建工具，也有实际启动支持／bootloader 字节；随包 `licenses/PyInstaller/COPYING.txt`。[官方许可和 bootloader 例外](https://pyinstaller.org/en/stable/license.html)。 | 按 GPL 加例外处理，不能误把所有开发依赖均列为运行时。SBOM 的 6 个构建工具单列，不表示都安装给终端用户。 |
| pycparser 3.11 | requirements 中存在且分发了许可文本；当前实际运行时分发组件和模块记录没有它。 | 仅安装或复制许可不能证明进入程序。继续区分构建／开发依赖，不能为了凑齐审计表将它误报为已打包运行时组件。 |

## 明确保留的证据缺口

本轮另对实际 `yt-dlp.exe`、`ffmpeg.exe`、`ffprobe.exe` 重新计算完整 SHA-256，三项均与该构建的固定来源记录一致；只读取字节，没有启动未知工具或改变文件。这个结果确认审查的工具身份，不独立证明全部许可证义务已满足。

完整 BOM 为 **457 个组件、6 个独立构建工具**。440 个原生对象中，415 个仅有 `bundled-notice-evidence-only`，25 个为 `unknown`；前者不是自动 SPDX 许可判定，后者不是自动认定闭源或违法。实际 25 个未关联许可对象包括主归档的 `VCRUNTIME140.dll`、`VCRUNTIME140_1.dll`、`libcrypto-3.dll`、`libssl-3.dll`、`libffi-8.dll`、`sqlite3.dll` 和若干 CPython 标准扩展。公开文件名清单及原始证据仍保存在本机审计 JSON，未改写 SBOM 状态。

已实读的 PySide6／shiboken6 `licenses/*dist-info/LicenseRef-Qt-Commercial.txt` 是商业授权通知；**其存在不证明使用了商业轮子，也不能代替适用 LGPL／GPL 全文和许可映射**。官方 PyPI 元数据确认存在开源许可选项，下一步应从精确发行来源补齐适用许可全文、实际 Qt 模块说明和可复核的源码／重建路径，不能自行给上游改许可证。

240 项既有识别缺口保持原义：73 个版本资源缺口、3 项未展开 CLR 引用、163 个同名导入解析歧义、1 个 `icuuc.dll` 未解析。它们不等于 240 个漏洞，也不等于上面 25 项许可证关联缺口；不同维度不能相加为“问题总数”。静态传递依赖仍 `incomplete`。`kernel32.dll`、`user32.dll`、`advapi32.dll` 等未随包的系统导入继续作为外部前置，不能与实际捆绑的 VC Runtime 混计。

## 实际功能与禁止项目政策核对

MediaFlow 是媒体下载与本地管理软件，不改称仅播放器或仅研究工具。README 说明 HTTP／HLS／DASH、WebView2/CDP 请求观察、批量、断点恢复、媒体库、音轨字幕及站点兼容处理。

本轮代码核对确认：`core/media_guard.py` 拒绝已识别受保护媒体；`core/downloader.py` 向 yt-dlp 传递 `--no-allow-unplayable-formats`；候选与重试逻辑保留 DRM 拦截。标准 HLS AES-128 identity 密钥按协议处理，与 DRM 密钥提取区分。`core/rou_playlist_decoder.py`、`rou_segment_decoder.py` 读取有界 PNG/roUd 载荷并做 CRC／deflate 检查，不能把该站点兼容功能从申请中省略。使用已登录浏览器请求上下文、来源站点请求、合法服务器提供的密钥和第三方工具功能也应披露。

这些是有限的静态证据，不是所有网站授权状况或整个软件“绝无绕过行为”的认证；是否触及服务方禁止项目政策仍由 SignPath 审核。没有为通过审核删组件、隐藏功能、关闭安全检查或购买任何服务。

## 申请前的具体待办

1. 让所有者向 SignPath 提交真实组件说明，询问 Qt 社区版与商业双许可条款、实际捆绑 VC Runtime 的系统库例外，以及本版 WebView2 SDK BSD 许可和外部 Runtime 的接受范围。
2. 补齐或明确剩余许可证映射及对应来源；不将上游再分发许可和免费服务的准入条件混为一谈。FFmpeg 静态组件、CPython 原生依赖与未知对象继续保留限制。
3. 使用本轮新编译验证两个第一方 C# 宿主的 ProductName／ProductVersion，随后由服务方设置文件元数据约束；不得替换本报告中旧 CI 的真实缺口。
4. 所有者确认 MFA、真实角色与人工作业批准安排，审阅政策／隐私草案，亲自处理免费申请所需账号、条款和授权信息。官方批准前保持集成关闭，不提交真实签名请求。

**最终状态：资格审查与申请准备可以推进；服务申请、官方批准和正式签名均未完成，不能作为“已获得免费签名资格”展示。**
