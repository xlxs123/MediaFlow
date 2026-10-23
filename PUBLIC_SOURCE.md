# 公开源码审计与发布边界

源码仓库是 [xlxs123/MediaFlow](https://github.com/xlxs123/MediaFlow)，发布文件使用独立的 [xlxs123/MediaFlow-Releases](https://github.com/xlxs123/MediaFlow-Releases)。本次公开采用经审计的新 Git 初始历史，不导入旧工作区、用户数据和历史构建记录。

## 允许公开的内容

应用的 `core`、`ui`、`utils`，本地合成测试，构建和发布脚本，原生 C# 宿主源码，应用图标、版本资源及公开信任配置，文档与工作流。

公开信任配置只包含真实发布源、Ed25519 公钥、密钥标识及状态。不得包含正式私钥、私钥密码或 DPAPI 密文。工具 EXE/DLL 和发行 ZIP 不放在源码 Git 历史中；其许可证与来源说明仍应保留。

禁止公开 BrowserProfile、Cookie、密码、Token、带有效授权的媒体 URL、下载任务与恢复数据、媒体库数据库、实际媒体、日志、缓存、构建输出、私钥及个人绝对目录。仅根据文件名不能证明文件可公开，也不能只检查 `.gitignore`。

## 可重复的暂存区审计

在准备提交的工作区运行：

```powershell
python -B scripts/audit_public_source.py --root . --index --reviews docs/public-source-reviews.json --report public-audit-result.json
```

`--index` 直接读取 `git ls-files --stage` 与不可变的 Git blob，审计真正将要提交的字节。即使有人随后把工作区里的敏感内容替换为空文件，已暂存的秘密仍会被检查。审计结束时会再次核对暂存区清单，发生变化即失败。

`--tracked` 检查已经跟踪文件当前工作区的内容，适用于 CI checkout；`--candidates FILE.json` 检查显式 JSON 数组中的相对文件名，适用于尚未建立 Git 的导出清单。这两种模式与 `--index` 的证据范围不同，不可混称。

报告只记录相对文件路径、行号、规则、字节数、逐文件 SHA-256 与整体清单 SHA-256，不输出命中的秘密内容或个人绝对路径。审计本身不上传数据、不修改 Git 索引、不删除或移动文件。报告放在 Git 以外或已排除的本地验收目录，不应误加入下一次提交。

## 合成测试数据的人工复核

脚本会拒绝真实形态的完整私钥、GitHub 凭据、用户数据目录、媒体、数据库、编译产物和个人路径。它还会把 Cookie/Authorization 字面值、带敏感参数的测试 URL、私钥格式标记和密钥生成代码列为需要复核的项目，不自动豁免整个 `tests` 目录。

`docs/public-source-reviews.json` 保存已经检查过的合成测试或安全解析代码；每条记录包含相对路径、整文件 SHA-256、分类、复核者及具体理由。只有文件字节仍与已复核的 SHA-256 一致，相应待复核项才能通过。文件变化后必须重新检查；不能为了消除报错批量批准未知凭据。高可信度私钥或凭据命中不能用复核记录覆盖。

审核记录格式：

```json
{
  "schema_version": 1,
  "reviews": [
    {
      "path": "tests/test_synthetic.py",
      "sha256": "<该文件实际的64位SHA-256>",
      "classification": "synthetic_test_data",
      "reviewed_by": "maintainer",
      "reason": "已检查输入、临时文件与断言，数据只在本地构造，不来自用户会话。"
    }
  ]
}
```

自动规则无法识别所有任意格式的秘密；通过结果仅覆盖所列的实际字节与规则，不是绝对无泄漏证明。新增公开文件、第三方源码或发行资产需要分别审计。

## 许可与二进制发行

保留原有 MIT `LICENSE`；该许可适用于项目自行编写的代码，不会把第三方组件重新许可成 MIT。已读的 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、实际依赖锁定、工具来源记录和许可证必须与最终发布包对应。

2.1.0 最终发布使用已取得对应缓存和构建配方的 BtbN FFmpeg，附独立的两份源码 ZIP、Python/yt-dlp 组件源码及 Qt 完整源码。实际文件、版本、大小、SHA-256 和上游缓存覆盖结果见 [THIRD_PARTY_SOURCES.md](docs/THIRD_PARTY_SOURCES.md) 与 [验收摘要](docs/RELEASE_VALIDATION.md)。下列 Gyan 项描述原工作区的初始审计边界；不将其当作本次最终使用的工具。

需要分别核对：

- FFmpeg/ffprobe：原工作区的 Gyan 9.0.2 essentials 实际启用 GPLv3 与静态依赖。其主 FFmpeg 源码对应 [946fcce07b](https://github.com/FFmpeg/FFmpeg/commit/946fcce07b)，但这一个链接不能证明所有静态库、补丁和构建资料都已齐全。完整对应源码尚未核实时，不应仅凭 notices 宣称完整二进制满足供源要求。[Gyan 官方构建资料](https://www.gyan.dev/ffmpeg/builds/)。
- yt-dlp：上游源码许可与 standalone Windows 组合程序的许可不同。原工具版本为 2026.08.19，需核对[该次正式 Release](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.08.19)和实际捆绑组件。上游说可以向其维护者请求源码，并不证明本发行者已经完成所有供源义务。
- PySide6、Shiboken 与 Qt：实际锁定为 6.11.2。官方提供 [PySide/Shiboken 对应源码](https://master.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/)及 [Qt 对应源码](https://download.qt.io/archive/qt/6.11/6.11.2/single/)。除许可证外，应提供完整应用源码与构建方式，使用户能使用修改后的兼容 Qt/PySide 重新构建和运行应用；不限制调试这种修改所需的逆向工程。[Qt 官方许可义务说明](https://www.qt.io/development/open-source-lgpl-obligations)。
- WebView2、PyInstaller、cryptography/cffi/pycparser：各自保留原许可、归属和必要资料；不要用主项目 MIT 声明替代它们的实际许可证。

如使用替代 FFmpeg 构建，应重新核对对应二进制、配方版本、所有依赖源码和实际功能回归。[BtbN 官方构建仓库](https://github.com/BtbN/FFmpeg-Builds)提供配方和下载缓存机制，但使用它的二进制本身不等于已经完成来源核对。

这里明确区分“自行编写源码可公开”和“完整二进制可正式发布”的验收门槛。许可证文本、链接与构建说明是必要资料，不能把未取得的对应源码编造为已有资料，也不能代用户作出长期书面供源承诺。
