# 存储 UI 原生崩溃：根因与最小修复

调查日期：2026-11-04。基线为已合并 PR #13 的 `47e2d62f005c323d1e3c1b070ffdff1bae29bdc8`；工作分支为 `fix/storage-ui-native-crash`。开始时工作区干净，远端 main 与该提交一致。本次不修改运行时代码、数据格式、下载器或签名策略。

## 结论与范围

已确认一项**测试夹具的 Qt 对象生命周期缺陷**：大量文件扫描超时后，无父对象的活动 `QTimer` 没有停止；后续后台 Python GC 在错误线程析构它，Qt 主线程随后仍向该接收者投递计时器事件，产生原生访问冲突。

这与先发生的 Python `AssertionError` 是两个事件。修复必须使失败正常报告并安全清理，不能通过扩大扫描期限、改变测试顺序或跳过扫描来获得成功。当前本机的扫描超时仍存在，不能宣称完整回归全绿，也没有证据证明生产存储服务存在相同缺陷。

## 原始证据与复现

所有证据保留在本机忽略目录 `.release-runs/storage-ui-native-crash-20261104/`。此前 `.release-runs/pr13-merge-acceptance-20261102/` 的失败日志、补测结果及报告未覆盖；调查开始时已记录 SHA-256。原始 minidump 保留在 Windows 原目录，未放入 Git 或 CI 工件。

1. 原完整入口先记录 `test_actual_many_files_scanned_in_background_and_rendered_paginated` 失败，第二例通过，随后在取消扫描例的 `processEvents()` 中进程结束。Windows Application Error 1000 确认 `0xC0000005`，2026-11-02 的故障进程为 PID 168776，Qt6Core.dll 6.11.2。
2. 不改原七例顺序、303 个真实合成文件或 15 秒等待期限，使用原 `tests/run_unit_tests.py --group test_storage_ui` 再次得到相同的失败后原生崩溃模式。原始重现外层进程结果为 `0xC0000005`，不是 Python 测试失败的退出码 1。
3. 在独立诊断运行中即时保存首个失败 traceback：`self.scan()` 的 15 秒断言在 `timer.stop()` 之前抛出。前例 teardown 后 heartbeat 仍有效、活动、5ms、无 parent；前例 worker 已退出，面板仍未 DeferredDelete。
4. 仅为调查增加地址、timer ID、线程和 `destroyed` DirectConnection 记录。PID 266512 的 UI 原生线程为 272424，heartbeat 的实际 C++ 析构发生在线程 274856 的 GC 中。转储中 `notifyInternal2` 的接收者地址与该 timer 的创建/析构地址完全一致；timer ID 同为 `0x38000003`。
5. 用系统自带 DbgEng 读取指定测试进程转储。精确 Qt 二进制的反汇编证明 receiver 参数保存在 RDI；栈上的事件类型为普通 `Timer=1`，并非 ZeroTimerEvent。新转储中 Python `Py_DumpStack` 的访问冲突发生于 faulthandler 处理更早的 Qt 异常时，不能把它误当成最初故障。

`root-cause-proof.json` 对以上交叉核对作了程序断言，并列出 18 份已保存证据的大小和 SHA-256。关键材料包括 `failure-diagnostic-r1/immediate-failure.txt`、`lifecycle-finalizer/chronology.jsonl`、`native-finalizer-r3/native-stack.txt` 和 `native-old-r5/native-stack.txt`。

### 正交对照

以下诊断均保留首例扫描失败、原顺序和原期限；不是通过改变调度来掩盖失败。

| 诊断变体 | 真实结果 | 能证明的范围 |
| --- | --- | --- |
| 未修改基线 | 原生 AV | 原问题可复现 |
| 仅处理面板 DeferredDelete | 仍 AV | 面板删除本身不足以修复无父 timer |
| 仅给 timer 设置 panel parent | 七例执行到底；一项普通扫描失败 | 所有权因素被隔离，但活动 timer 的异常清理仍不足 |
| 仅将 stop 放入 finally | 七例执行到底；一项普通扫描失败 | 停止活动 timer 可切断已观察的事件残留路径 |
| parent 与 DeferredDelete | 七例执行到底；一项普通扫描失败 | 进一步支持生命周期归因；单轮无 AV 不是完整验收 |

### 其他假设与证据限制

- 没有观察到本次崩溃前例的 worker 存活；不能把这个已复现的故障归因于未 join。
- 两个 Python 环境中的 Qt6Core.dll、QtCore.pyd、Shiboken.pyd、shiboken6.abi3.dll 分别字节一致。不同路径本身不是 DLL 混装证据。
- `HistoryStore` 由同一 worker 创建和关闭；没有该路径的 SQLite 跨线程误用证据。文件身份 API 的 ctypes 布局核对也未发现对应的越界写证据。
- 首次未修改重现的 Windows venv 启动器 PID 与实际 Python fault PID 不同，历史父子关系查询受权限限制；没有伪称已证明该映射。决定性后续诊断直接记录实际 `os.getpid()`，与其转储 PID 绑定。
- WER `Report.wer` 内容受 ACL 限制；没有提权、修改 WER/注册表或安装调试器。使用导出符号、PE unwind 信息与精确二进制反汇编，没有私有 PDB，也未从旧转储恢复不存在的 timer 创建记录。

## 最小修复

`tests/test_storage_ui.py`：

- heartbeat 使用 `QTimer(self.panel)`，由 UI 面板拥有。
- `self.scan()` 使用 `try/finally`，扫描成功或失败都停止 heartbeat。
- teardown 等待服务关闭且其 worker 确实退出，随后在 UI 线程定向处理 panel 的 `DeferredDelete` 并验证 C++ 对象已销毁。局部 `processEvents()` 循环不能单独保证 DeferredDelete 已处理。

`tests/test_storage_ui_lifecycle.py` 新增三项测试，直接调用原 fixture 和原大量文件测试方法，使用真实 QTimer 与确定性的扫描异常：

1. 异常抛出后 timer 必须停止且 parent 为当前 panel。
2. 原 teardown 必须真正销毁 panel，并结束 worker。
3. timer 必须先在 owner/UI 线程销毁，再进行后台 GC，不能把销毁推迟到后台线程。

测试临时持有 timer 强引用，使旧实现可以稳定表现为断言失败，避免为了回归而故意让整个测试进程发生 AV。测试自己的 cleanup 同样等待 worker 退出并处理旧实现遗留的对象。

`.github/workflows/ci.yml` 在构建前加入原七例与新增三例的独立步骤，各保留 900 秒步骤上限。非零退出、缺结果、零测试、失败、错误、跳过或源摘要变化均阻断构建；始终保留独立存储 UI 结果工件。没有修改权限、签名输入、并发组或 protected Environment。

## 本机验收记录

| 测试组 | 结果 | 证据 |
| --- | --- | --- |
| 新三例对照旧 fixture | 3 测试，3 失败，0 错误、0 跳过；退出 1 | `old-code-regression/` |
| 初轮修复后新三例 | 3 测试，0 失败/错误/跳过 | `fixed-regression-r1/` |
| 原始七例三轮独立进程 | 每轮 7 测试、1 个原扫描超时、0 错误/跳过；均退出 1，无 AV | `fixed-stability-r1/` |
| 新三例五轮独立进程 | 每轮 3 测试，0 失败/错误/跳过 | `fixed-stability-r1/` |
| 扫描与清理模块定点回归 | 扫描 12 项、清理 15 项分别通过 | `fixed-related-r2/` |
| 最终 cleanup 收敛后三例 | 3 测试，0 失败/错误/跳过 | `fixed-related-r2/` |
| actionlint 与真实工作流策略 | 均退出 0；29 个固定 Action 调用，策略无错误 | `static-validation/` |
| 完整 Git 历史及精确暂存区扫描 | 42 个提交、592 个历史文件版本，0 阻断项；保留既有精确合成夹具例外 | `staged-history-secret-scan/` |

不同轮次与重复测试不相加作为唯一测试总数。完整入口、最终清理收敛后的回归、秘密扫描和远端 CI 的结果在最终交付证据中分别记录，不以旧提交的 CI 成功替代本分支验收。

补测首轮曾把两个非 GUI 模块传入仅接受 GUI 分组的 `--group` 入口，该入口以退出码 2 拒绝、没有执行这两个模块。`fixed-related-r1/` 保留了该命令错误；`fixed-related-r2/` 改用实际 `unittest` 模块入口，上表只报告后者实际执行的结果。

### 仍保留的扫描期限失败

不使用 Qt 事件循环，直接扫描相同 303 个合成文件的本机 profile：默认临时目录完成约 19.926 秒，较短工作区临时目录约 22.043 秒；实际枚举、文件系统安全检查、打开文件及 SQLite hash cache 操作占用时间。它们能够解释本机为何会触发原 15 秒期限，并不证明具体是哪一种外部磁盘或安全软件条件导致速度差异。

本次不削减真实文件、完整哈希或安全检查，不修改 15 秒期限，不禁用 GC，不关闭 faulthandler，也不改下载核心来让本机测试变绿。原生崩溃已经有独立的对象身份及线程证据；扫描性能与完整本机回归全绿仍须如实列为验收缺口。

## 复核命令与生产边界

```powershell
# 原组；在隔离环境运行，QT_QPA_PLATFORM=offscreen
.venv\Scripts\python.exe -B -X utf8 tests/run_unit_tests.py --group test_storage_ui --report-dir <fresh-directory>
.venv\Scripts\python.exe -B -X utf8 tests/run_unit_tests.py --group test_storage_ui_lifecycle --report-dir <fresh-directory>
# 原完整入口；保留原各组 1200/900 秒预算
.venv\Scripts\python.exe -B -X utf8 tests/run_unit_tests.py --report-dir <new-directory> --security-python <existing-security-python>
```

本机 child 环境复用既有固定 gitleaks、隔离 schema/安全测试依赖和 `tools`；没有修改全局 PATH 或用户配置，没有读取 BrowserProfile。完整的实际参数、时间、退出码保存在相应 `runs.json`。

正式 SignPath 配置的 `enabled`、`foundation_approved`、`license_review_accepted` 保持 false，Authenticode 状态仍为 `not_configured`。没有真实签名请求、生产签名、稳定发布或新 PR 自动合并。现有 v2.1.0 的 13 项资产只进行只读元数据核对；稳定清单、公钥及用户数据不属于本次改动。

远端验证只能使用本修复提交对应的真实 Windows CI 与 Security CI。应先验证修复分支的 CI，再创建新的 PR；不得把已有 PR #13 的 552 项结果作为本次修复的验证结果。若本机完整入口仍保留普通扫描超时，最终结论应为 **B：根因明确、已修复，但部分验收未完成**。
