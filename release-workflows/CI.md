# Windows 持续集成

`.github/workflows/ci.yml` 在 `push`、`pull_request` 或手动触发时执行 Windows 构建检查。普通 CI 仅有 `contents: read` 权限，不调用发布签名工具、不使用正式私钥、不创建 Release，也不自动发布稳定版。

执行顺序：

1. 使用 Python 3.14 x64 和 `requirements.txt` 中已固定的依赖创建独立环境。
2. 检查 Python 源码语法，运行发布结构检查器的本地文件测试。
3. 通过既有工具准备入口下载并校验 FFmpeg、ffprobe 和 yt-dlp。
4. 调用 `tests/run_update_tests.py`，分进程执行更新、清单、签名、部署、回退、相关 UI 和输出组织回归。任一失败、错误或未预期跳过都会阻止后续构建产物上传。仅本地 Windows 用户不能创建符号链接时，允许精确列出的工作区链接能力测试跳过；索引链接拒绝测试仍必须通过。托管 runner 具备权限时执行该测试。
5. 复用 `build.ps1`，生成独立的 `dist-mediaflow-ci` 文件夹，保留原有实际 EXE 构建身份检查。
6. 再次启动刚生成的 EXE 获取实际身份，使用 `scripts/check_release_structure.py` 核对版本、构建、完整工具与许可、工具和 Qt 许可正文 SHA-256，以及公开文件的隐私门禁。
7. `scripts/ci_build_receipt.py` 为完整文件清单、大小与 SHA-256、实际 EXE 构建、通过的分组测试、仓库/提交/run ID 生成收据。收据是文件绑定证明，不能单独充当签名或 CI 来源认证。
8. 只上传通过检查的完整文件夹、收据和不包含个人路径的摘要，保留七天。运行时绝对路径报告放在 runner 临时目录，不进入构建产物。

签名单元测试仅在临时目录生成测试密钥。这些密钥不进入构建产物，不会被固定为客户端正式信任根。

## 发布结构门禁

本地使用时，先让实际打包 EXE 运行 `--build-info`，将原始运行身份保存在发布目录之外，然后执行：

```powershell
python -B scripts/check_release_structure.py `
  --release-dir <完整发布目录> --expected-version <实际版本号> `
  --runtime-identity <本次EXE的原始身份报告> --report <新的外部报告文件>
```

公开的 `build-info.json` 保留构建信息，移除 `executable` 和 `runtime_root` 两个运行时绝对路径字段。检查器要求它与实际 EXE 报告的其余字段完全一致，并拒绝把这两个字段重新放进公开目录。

检查器会核对必需的 EXE、DLL、文档、工具来源记录、PySide6/cryptography 等依赖许可和 QtWebEngine/Chromium 许可正文；拒绝空文件、损坏 PE 头、错误架构、未声明的根目录附加文件、用户数据目录、真实媒体文件、链接、个人路径、授权头及私钥标记。SHA-256 反映实际读取的文件内容。

结构检查不会执行下载、部署或媒体完整解码。`--build-info` 成功只证明程序能够启动并加载其检查的模块。CI artifact 是未签名的构建结果；它不是已经通过生产发布验收或公网更新验收的稳定版本。

统一发布入口会先通过实际 GitHub API 核对同提交成功工作流、固定必要步骤和 artifact 摘要，再下载、核对完整服务器 SHA-256、安全解包并验证收据绑定的全部文件，之后才运行 EXE 和正式签名。Pull Request 的 CI 仍执行测试和构建，但不作为本机正式发布的来源；正式发布要求默认分支当前提交通过 CI。普通工作流没有仓库写权限，也不接触正式私钥。用法见 [RELEASE_AUTOMATION.md](../docs/RELEASE_AUTOMATION.md)。

## Actions 固定提交

以下完整提交通过 GitHub 官方仓库的 tag ref API 核对；工作流使用提交 SHA，不跟随可移动 tag。

| Action | 核对的 tag | 固定提交 |
| --- | --- | --- |
| `actions/checkout` | `v4` | `11d5960a326750d5838078e36cf38b85af677262` |
| `actions/setup-python` | `v5` | `a26af69be951a213d495a4c3e4e4022e16d87065` |
| `actions/upload-artifact` | `v4` | `ea165f8d65b6e75b540449e92b4886f43607fa02` |

官方核对入口：[checkout tag ref](https://api.github.com/repos/actions/checkout/git/ref/tags/v4)、[setup-python tag ref](https://api.github.com/repos/actions/setup-python/git/ref/tags/v5)、[upload-artifact tag ref](https://api.github.com/repos/actions/upload-artifact/git/ref/tags/v4)。对应提交：[checkout](https://github.com/actions/checkout/commit/11d5960a326750d5838078e36cf38b85af677262)、[setup-python](https://github.com/actions/setup-python/commit/a26af69be951a213d495a4c3e4e4022e16d87065)、[upload-artifact](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02)。

升级 Action 时需重新从官方仓库核对完整提交，审查修改并更新上述记录。不能将 tag 名或未经核对的示例 SHA 当作固定来源证明。

## 实际运行的验证

本地结构检查器测试和源码检查不能代替 GitHub 托管 runner 的实际运行。实际运行与失败修复记录见 [RELEASE_VALIDATION.md](../docs/RELEASE_VALIDATION.md)。必须查看真实结果，确认工具和 Qt 许可下载、Windows 打包、原生 EXE 身份检查及 artifact 保存均成功。网络或上游下载失败会停止本次构建，不会改用未验证工具。
