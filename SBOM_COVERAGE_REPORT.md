# Windows 原生组件与 SBOM 覆盖报告

本阶段沿用 CycloneDX 1.6 和既有 CArchive / PYZ / TOC / wheel RECORD 字节验证，不以 requirements 或整个开发环境代替运行时清单。基线为 `510a9da2d98266f6a7ba5686b710d640d3a28ea3`；本报告区分旧 CI 产物上的只读探针、新代码本地回归，以及尚待运行的新完整 CI。

## 新增的实际证据

- 收据白名单中实际存在的 EXE / DLL / PYD，以及 MediaFlow 和 yt-dlp one-file CArchive 内实际 PE 字节。
- 有界 PE 导入、延迟导入、固定版本资源、证书表观察、原分发包 RECORD 所有者与文件 SHA-256。
- 以真实文件/容器定位原生组件与依赖；同名歧义明确保留，不任选某个 DLL。
- 实际随包的 system-named shim 保留为分发文件；只有未随包的系统导入列为外部前置，不伪造为软件组件。
- 许可证、工具来源和构建记录以实际文档及摘要关联；文本存在不等于已证明某静态库被链接或确定了许可。

现有 pefile 2024.8.26 属于 PyInstaller 构建依赖，直接复用；未添加大型原生扫描框架。独立校验环境不导入应用、PyInstaller 或 pefile。

## 本地实际只读探针

针对先前真实 CI 的 190 个静态文件白名单扫描，未遍历用户数据或浏览器目录，也没有执行扫描得到的未知 PE：

| 指标 | 实际结果 |
| --- | ---: |
| 物理 PE | 8 |
| 主程序 CArchive 内 PE | 316 |
| yt-dlp CArchive 内 PE | 116 |
| 已解析实际 PE | 440 / 440 |
| 原生探针组件 | 447，含 440 PE 和 7 个实际报告的 libav 库 |
| 导入/延迟导入关系 | 3620 |
| 关系分类 | 1615 分发依赖、1841 外部系统、163 同名歧义、1 未解析 |
| 官方 CycloneDX 1.6 schema | 零错误 |

**447 是只读原生探针组件数量，不是尚未运行的新完整 CI BOM 数量。** 新完整 BOM 仍会保留实际 Python 分发包、运行时与构建工具分离。

FFmpeg / ffprobe 的实际二进制先与已证明旧 CI 收据完整 SHA 匹配，再实际执行 `-version`，记录输出摘要和可复核的构建来源。七种报告的库版本为 libavutil 61.1.102、libavcodec / libavformat / libavdevice 63.1.102、libavfilter 12.1.102、libswscale 10.1.102、libswresample 7.1.102。

这些版本只在二进制 SHA 与 `security/native-component-evidence.json` 精确一致时采用。`--enable-lib*` 仅作为编译配置声明，不据此编造外部库具体版本、许可证或逐项静态链接证明。新二进制不能继承旧摘要对应的版本。

## 独立验证与签后派生

新版 inventory 为 schema 2，含 native_inventory；新 CI 和正式流程要求 `--require-native`。历史 schema 1 仍可验证原格式，但明确返回 legacy-not-assessed，不能冒充原生覆盖。

独立校验重算 PE/容器/成员闭合、实际摘要、分发与系统前置分离、唯一 refs、依赖引用、版本/托管/导入缺口，并拒绝仅修改 generation 计数伪造覆盖率。已测试悬空和重复依赖、漏对象、系统前置混入 runtime、主机路径、敏感标记、旧格式冒充新覆盖等负例。

签名仅改变三个第一方 PE 的身份。`rebind_signed_bom` 限制固定路径，核对旧 hash/size，只生成独立副本；不修改原 unsigned CI 证据。签后证书表观察不会沿用成“已经验签”，真实 Authenticode 由 Windows 工具单独验证。

最终 BOM 以外部 sidecar 关联最终 ZIP 的真实大小和 SHA-256，避免 ZIP 自引用。派生 BOM 和清单的摘要不是数字签名；本地派生文件不会被误称为原 GitHub 构建来源证明的 subject。

## 仍明确存在的缺口

原生探针保留 240 项缺口：73 个 PE 缺少固定版本资源（其中两个可保留可靠上游发布标识）、3 项 CLR 托管引用尚未展开、163 个同名解析歧义和 1 个未找到的非系统名 `icuuc.dll`。这不等于 240 个独立漏洞，也不表示所有运行时传递依赖已齐全。

FFmpeg、Qt、OpenSSL、yt-dlp 等静态链接依赖无法全部从 PE 导入表确定；完整来源/版本/许可证不足的部分继续标为未知。WebView2 外部 Runtime 不冒充随包组件。组合仍声明 incomplete，**PE 字节覆盖并不等于全部静态、托管和系统依赖完整枚举**。

## 验证状态

本地已通过 21 项 native/schema 增量、14 项原 CArchive/PYZ/RECORD 回归及 3 项最新生成器集成检查，均零跳过；这些轮次不累加为唯一测试总数。实际只读 native probe 及其独立 schema/闭合校验已通过。

新完整 Windows 构建、完整本地回归、远程 GitHub CI、更新/回滚回归和官方新来源证明仍待本阶段实际运行后补充。正式 Authenticode / 正式发布未完成的原因是生产证书未配置，不以本地模拟替代。
