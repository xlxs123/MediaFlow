# MediaFlow 打包架构与 Authenticode 兼容性决策

日期：2026-10-31。PR：[xlxs123/MediaFlow #13](https://github.com/xlxs123/MediaFlow/pull/13)。审阅基线：`370dd23c6a740154eeb10e4adeecb2b631e10468`；实现提交：`fdddedb5eca77763dd404b10716ca6b277552e9a`。

## 决策

**选择 C：保留现有 One-file 和便携目录布局，增加签后实际归档身份门禁。**

这不是声称任何 Authenticode 追加内容都必然兼容。发布契约改为：每一份返回产物必须在完整文件上，按固定启动器的选择语义选中与原始可信构建相同的 Cookie、PKG、原始 TOC 和 PYZ；否则拒绝，不能截断签名区域后再验证旧归档。独立 Production Verify、原始收据、完整目录及最终更新清单仍然必需。

普通 One-folder 不能独立消除本问题，所以不为此改写目录白名单、资源定位和 SBOM 模型。此结论不否认 One-folder 的启动及调试优势，MediaFlow 本来就需要携带整个发布文件夹。

此前 `PE_IMAGE_RUNTIME_PAYLOAD_STATIC_AUDIT.md` 的 D 有效描述了当时的实现；旧报告不覆盖、不改写。本轮增加的是可执行的逐产物验收条件，没有重试此前被拒的异常签名产物实验。

## 当前构建及资源布局

- `MediaFlow.spec:11–30` 使用 `Analysis → PYZ → EXE`，将 binaries、datas 直接传入 EXE，没有 `COLLECT`。主程序为 One-file，不是因为便携包是文件夹就成为 onedir。
- `requirements.txt:2` 和本机构建环境均为 PyInstaller **6.22.3**。上游源码依据固定提交 **ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5**，不是其他版本的推断。代码中的该提交是审核契约标签，不是对最终 EXE 反汇编后测出的 bootloader 身份。
- `scripts/build-recycle.ps1` 用 .NET Framework x64 `csc` 构建 Recycle 宿主；`scripts/build-webview2.ps1` 用同类编译器及固定 WebView2 SDK 构建 WinForms 宿主。两者不是 PyInstaller 程序，不强加 CArchive 条件。
- 主 EXE 含附加 PKG：Cookie 声明归档长度、TOC 偏移/长度、Python 版本及库名；TOC 按顺序描述引导脚本、PYZ、Python DLL、扩展模块、Qt 和资源。PYZ 会继续按载体内偏移读取，并非所有 Python 模块都先解包。
- 必须一起发布根 EXE、完整 `tools`（三个第三方工具、两个第一方宿主及 WebView2 伴随 DLL/config）、`licenses`、文档和 `build-info.json`。内置 assets 的定位仍为 `_MEIPASS`，工具定位仍为 EXE 所在目录。
- 更新器对完整清单逐文件部署至新的版本目录，保留旧目录；数据目录另行隔离。媒体库、配置、恢复工作区和 BrowserProfile 不加入发布包。本轮没有移动或迁移任何用户文件。

## 固定版本启动器的决定性证据

上游链接均固定到上述提交：

1. [api.py:434–450](https://github.com/pyinstaller/pyinstaller/blob/ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5/PyInstaller/building/api.py#L434)：默认 `exclude_binaries=False`、`append_pkg=True`。
2. [api.py:297–349](https://github.com/pyinstaller/pyinstaller/blob/ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5/PyInstaller/building/api.py#L297)：onedir 只把 binaries/data 转交外部收集，引导脚本和 PYZ 仍在 PKG。
3. [pyi_main.c:198–209](https://github.com/pyinstaller/pyinstaller/blob/ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5/bootloader/src/pyi_main.c#L198)：先解析归档，再据所选归档判断 onefile/onedir。
4. [pyi_main.c:1662–1708](https://github.com/pyinstaller/pyinstaller/blob/ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5/bootloader/src/pyi_main.c#L1662)：先尝试自身归档；`append_pkg=False` 也只是允许随后回退至同名 `.pkg`，不是强制优先外置载荷。
5. [pyi_utils.c:181–240](https://github.com/pyinstaller/pyinstaller/blob/ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5/bootloader/src/pyi_utils.c#L181)：从物理 EOF 向前分块寻找最右侧匹配；没有按证书目录截断搜索。完整字节串的 `rfind` 选择同一位置，原生零偏移的失败语义也予以保留。
6. [pyi_archive.c:416–443](https://github.com/pyinstaller/pyinstaller/blob/ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5/bootloader/src/pyi_archive.c#L416)：以已选 Cookie 计算 `cookie_offset + cookie_size - pkg_length`，再定位 TOC。后续 PYZ 由 PKG 起点加成员偏移定位。

因此，“旧 Cookie 仍在原位置”不能推出“签后必然选中它”。A 不能仅凭旧前缀保持判通过；B 也不能仅凭增加 `COLLECT` 判通过。

## 方案比较

| 评价维度 | 继续普通 One-file | 改为普通 One-folder | 本轮选择：One-file 加签后归档身份门禁 |
| --- | --- | --- | --- |
| 签名兼容性证据 | 旧字节保持未证明选择结果 | 引导归档仍用相同选择算法，缺口仍在 | 对每个接受的返回文件实际选择并核对；不符合即拒绝 |
| 运行时载荷定位 | 自身 EXE 的 Cookie/PKG/PYZ | 自身仍有引导 PKG/PYZ；DLL/data 外置 | 保持原定位方式，验证实际选择及原始有序 TOC |
| 更新与回退 | 现有完整目录流程已支持 | `_internal` 和外置依赖需适配现有严格目录规则 | 清单、部署、回退和数据模型不变 |
| 用户部署体验 | 已要求整个便携目录 | 同样需整个目录，不必然损害体验 | 无新增用户操作 |
| 发布包大小 | 本轮普通构建 EXE 222840102 字节；完整目录190文件/575297781字节；本地 ZIP 371587713 字节 | 本轮未构建，不编造压缩后大小；外置依赖增加磁盘文件数 | 与现有布局相同，证明保存在外部工作目录 |
| 启动时间 | 有临时解包成本；本机构建身份启动一次实测39.427秒，非跨架构基准 | 官方描述省去 onefile 解包，通常启动更快；本轮未测数值 | 不以安全任务扩大为启动性能重构 |
| 维护复杂度 | 无新增控制但保障不足 | 还要修改包白名单、资源/依赖检查及 SBOM 嵌入假设 | 新增有界解析、两个调用点及实际 CI 验收，成本较低 |
| 后续自动签名 | 原检查不足 | 单独换模式仍不足 | SignPath 返回检查与本地派生共用门禁，证书验签保持独立 |

One-folder 的原理与启动成本参考[对应版本官方说明](https://pyinstaller.org/en/v6.22.3/operating-mode.html)。没有把未测的 One-folder 文件数、大小或速度写成实测。

## 已实施的控制

- `scripts/pyinstaller_payload.py:26`：只读完整载体，先选实际最右 Cookie，再解析有界 PKG/有序 TOC/唯一可直接寻址 PYZ。检查原始 Cookie 位于未签名载体 EOF；记录归档、TOC、PYZ 的范围和摘要。不解包、不 unmarshal、不执行数据。
- `scripts/pyinstaller_payload.py:82`：签前与签后的选择结果、区间及原始归档身份必须相同。不能解析失败后回退寻找更早 Cookie。
- `scripts/release_security.py:194`：三个第一方文件仍先过原 PE 字节派生检查，主程序再过归档门禁；C# 宿主保留各自 PE 契约。
- `scripts/signpath_integration.py:125` 与 `signing_derivation()` 共用上述入口，因此远端返回路径和本地签名/恢复路径均接入。
- `scripts/check_packaging_compatibility.py:20`：实际正常构建与固定版本官方 reader 比较选择结果；可只读对照已有正常签名文件，报告完整载体摘要。其 `native_signature_verified`、`signed_execution_verified` 明确为 false，不冒充证书信任或签后运行。
- `build.ps1` 自动检查实际构建。Windows CI 新增普通构建检查和已有 SDK TestOnly 正常签名产物的只读对照，失败阻断 Job；报告随通过构建保留。签后测试文件不执行。
- `scripts/release_pipeline.py` 只增加这两份固定报告的精确路径，不扩大目录白名单。原始收据/SBOM保持；新报告没有自行被称为独立官方 attestation subject。

只要接受的签后文件选择相同 Cookie，Cookie/TOC/成员偏移及所引用原始字节也保持，就能建立相同归档载荷的对应关系。完整 PKG 覆盖原始 TOC 顺序；不是仅比较会归并同名键的字典。这个属性与密码学签名、证书信任及任意运行环境下行为等价仍然不同。

## 验证结果及首次失败

以下轮次有重叠，不能相加作为唯一用例数。

- 定点组：第一轮47项通过；独立复核后增加合法重复引导名称正例并补载体摘要，第二轮48项通过；最终归档模块11项通过。
- 发布器与归档定点组：33项通过。未删除原拒绝断言。
- 已有正常 SDK TestOnly 签名样本只读对照通过：原载体223360546字节，证书表1552字节，原2997成员PKG保持。没有用其测试证书替代生产信任。
- 本机更新/签名/部署/回退分组：550项、0失败、0错误、1项已知符号链接权限跳过；core521/UI17/output12，核心745.084秒，原900秒预算未改，无残留归属子进程。该轮启动后补入的两个用例另有定点结果；最新远端 CI 重新加载最终代码。
- 本机完整项目回归：**2608项、0失败、0错误、2跳过，37个独立分组全部成功，源码摘要保持**。跳过分别是未显式提供localhost专用保存配置的可选验收，以及本机符号链接权限；没有读取真实浏览器登录配置。此轮包含最终归档/发布器用例，不能与550项或定点轮次相加。
- 最初直接运行的相关模块组146项有1失败：`test_no_source_is_not_latest` 读到了已初始化的实际渠道；随后使用项目既有隔离入口通过。保留首轮日志，不修改更新渠道或测试断言。
- 正常 EXE 实际启动、Direct续传、HLS缺片恢复、DASH音轨503后恢复均通过；DASH输出经ffprobe确认H.264/AAC、640×360、8秒。独立完整发布结构检查190文件通过。
- 正常候选实际执行`--update-health`通过：版本/构建/资源及恢复初始化检查使用全新的隔离数据目录，未读取原用户目录。这是启动健康检查，不是不同生产签名版本的端到端更新结论。
- 额外 `dash-merge` 故障注入用例失败：复制为ffmpeg的ffprobe未形成预期合并故障，实际下载成功。版本检测仅接受 `ffmpeg version`，无效自选工具会按既有策略回退可用工具，见 `core/tool_manager.py:74,205–210,236–255`。这是未达预期故障点的旧夹具限制，不能计为合并故障恢复通过；本轮未改下载核心或放宽断言。
- 最初缺少工具 provenance（使用了SkipToolDownload）导致结构检查拒绝；随后用现有工具准备脚本验证已缓存文件的固定摘要、生成来源记录后复测通过，无需重新下载工具或重建主程序。
- actionlint通过；实现阶段工作流门禁27个Action引用通过；最终增加小型摘要工件后28个引用及52项工作流/安全门禁测试通过。公开源码精确暂存审计通过。完整历史/暂存秘密扫描通过；首次失败仅为修改过的测试文件中未变的合成行失去精确blob复核绑定。核对原行字节后，仅追加1条rule/path/blob/line/value-digest记录，保留所有历史条目，未新增宽泛豁免。绑定辅助脚本曾因旧JSON混合换行而在写入前安全停止；修正定位后成功，失败记录保留。

实现提交的真实远端结果：

| 项目 | 实际结果与证据 |
| --- | --- |
| Windows CI | [41194905840](https://github.com/xlxs123/MediaFlow/actions/runs/41194905840) completed/success；实际EXE启动、发布结构、普通归档选择、SDK正常测试签名归档对照、原始收据与SBOM步骤均success |
| Security CI | [41194905832](https://github.com/xlxs123/MediaFlow/actions/runs/41194905832) completed/success；CodeQL、已解析依赖、完整历史秘密、工作流策略四Job均success |
| 独立SBOM复核 | [SBOM工件13040960237](https://github.com/xlxs123/MediaFlow/actions/runs/41194905840/artifacts/13040960237)，489546字节，下载ZIP SHA-256 `2430af7d8a420f63bfd6b410c52ecd63978112b293c1aecb5f7cd22a392ea03a`与GitHub一致；保留原验证文件，在另一个目录复核CycloneDX1.6及原生覆盖成功，457组件/440 PE/240识别缺口 |
| 身份区分 | 该PR构建使用合并提交`56d08aef2b88d4befb940546ff2cd889570001ae`，不是直接使用分支HEAD。API核对其parents；其树与实现提交相同，均为`cf34e064691b636c63702f4372b2e4e19fba05ed` |
| 生产状态 | Official build provenance及SignPath Job按PR条件skipped；没有真实签名请求、新官方来源证明或生产签后启动验收 |

摘要留存改动只将已有公开报告另存为小型CI工件，便于以后独立读取实际测试计数与归档证明，避免为几份JSON重复下载完整371MB构建。它不修改发布资产或新增来源证明声明。验收提交会照常触发原工作流，交付回执提供其实际最新运行结果。

## 使用与安全边界

普通构建继续使用 `build.ps1 -OutputDirectory <全新的输出目录>`。脚本自动执行正常归档检查。手动只读检查：

```powershell
python -B scripts/check_packaging_compatibility.py --release-dir <原始发布目录> --report <新的外部报告.json>
python -B scripts/check_packaging_compatibility.py --release-dir <原始发布目录> --signed-executable <已有正常签后主程序> --report <新的外部报告.json>
```

实际签名仍须经原审批、SignPath/Authenticode身份与信任验证；本轮没有调用服务、读取签名Token、创建正式证书、执行Production签名、合并或发布。SDK测试只在现有隔离测试流程中进行，不让测试产物进入稳定渠道。未将测试证书、普通SHA或本地证明冒充正式身份验证。

PyInstaller升级必须重新审阅相应版本，当前实际构建检查会拒绝版本漂移。既有构建流程下载的bootloader与审核源码的关系仍依赖受信构建工具来源；未宣称逐位可重现编译或覆盖任意第三方运行环境。

## 修改文件

实现提交的15个路径：`.github/workflows/ci.yml`、`build.ps1`、`docs/public-source-reviews.json`、`scripts/check_packaging_compatibility.py`、`scripts/pyinstaller_payload.py`、`scripts/release_pipeline.py`、`scripts/release_security.py`、`scripts/signpath_integration.py`、`security/secret-exceptions.json`、`tests/pyinstaller_archive_fixture.py`、`tests/run_update_tests.py`、`tests/test_pyinstaller_payload.py`、`tests/test_release_pipeline.py`、`tests/test_release_security.py`、`tests/test_signpath_integration.py`。本报告单独补充，未修改应用下载/浏览器/媒体库/恢复核心、更新清单、公钥或生产策略。

## 最终建议与外部限制

架构选择为C，不选择未经证明的A，也不把普通One-folder误称为已解决问题的B。针对本候选，新增门禁将原来未核验的选择前提转为每份返回产物必须满足的条件。真实SignPath/Foundation许可、生产证书、TSA/链/撤销及正常生产签后启动仍需外部审批后验收，当前继续默认关闭，不能把这份决策报告解释为生产发布就绪。

PR保持Open、未合并、无冲突；实现提交时尚无Review。v2.1.0的13项正式资产ID、名称、大小、摘要及时间字段与本轮开始时一致（正常下载计数不作文件身份依据）；更新渠道、公钥、生产策略及应用核心文件与审阅基线无diff。本轮未重新下载全部稳定包，不把元数据对照称为逐包重新验签。

本轮实际运行的是正常未签名候选及隔离的本地下载样本；正常SDK测试签名产物仅作静态比较。不同真实生产签名版本的端到端切换及真实服务兼容性没有实测，不将文件部署/回退单元回归或EXE启动检查冒充生产更新验收。实际交付候选仍为2.1.0开发验收构建，不创建2.1.1或其他稳定版本。
