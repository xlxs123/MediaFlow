# 最终产物一致性

正式证书缺失，没有生成正式签名ZIP，也未解锁生产Ed25519保险库。`formal_signed=false, production_ready=false, published=false`。

## 保持的门禁

现有流程先验证同提交默认分支官方来源证明及原始CI收据/SBOM，再签固定三个PE。`signature_only_change`仅允许checksum/security directory/末尾WIN_CERTIFICATE变化；其余载荷和第三方不变。这是字节保全，不是身份认证。

`publication_sbom`从原CI文档派生签后文件身份，绑定最终ZIP大小/SHA-256；原收据、generation及官方证明不重写。派生SBOM不会自动获得GitHub或Ed25519认证。正式远程写入之前，对最终ZIP所有成员清单、大小、hash校验，并重新解出三个PE执行Production独立验签。本轮没有改变生产入口。

## 实际隔离ZIP

SDK集成r2确实产生223,914,932字节的TEST ONLY ZIP，SHA-256 `23214bb31f6a7f52555621c772ebe8f80193a62757500f0af30293f3d42a1354`。它含三个实际第一方PE和一份Microsoft签名工具保全夹具，校验并重新提取后，三个PE通过Test Verify。修改ZIP内PE后，原包摘要检查在提取前拒绝。此包不是生产更新包，没有上传为正式资产。r1独立包SHA-256为 `511d2f8d3b011a19cc67ab2779344f34e90558e1351ef81e7a005c4714728988`，保留历史，不与r2混用。

## SBOM范围

参考构建 `f6227021543e8154bc035887a98c429db6f5a9a0` 的真实SBOM为457组件、440 PE、240项识别缺口：73项缺可靠版本、3项未展开CLR引用、163项同名DLL歧义、1项icuuc.dll未解析导入。静态链接传递依赖仍为incomplete。缺口不是漏洞数，不填造版本、许可证或依赖。

本轮实际对三个Test签名PE执行低层身份rebind及独立schema/coverage验证，通过，26.727秒：457组件/440 PE/240缺口保持，原文档、第三方、嵌入载荷、gaps与依赖图全部相等；真实错误old-hash、第三方重绑定、PE载荷篡改3个负例均拒绝。重新验证原收据和实际源文件，不继承unsigned generation。不调用/绕过正式publication_sbom，不伪造Production auth_report，不声称生成正式签后SBOM或本次重新签发GitHub来源证明。

## 同提交正式CI来源与实际文件复核

本轮实现经PR #11正常合并为main `c3fa7bb579149366aa6c4f71ab7537b22727e3ec`。[Windows构建/官方证明](https://github.com/xlxs123/MediaFlow/actions/runs/40512840774)和[安全工作流](https://github.com/xlxs123/MediaFlow/actions/runs/40512840798)均真实成功。完整只读DryRun通过（866.439秒），没有正式签署或发布；之后又独立重算原生库存并验证10个官方证明对象，通过（100.686秒）。原生字段与本次CI库存全相等，gap和依赖关系与基线相同，原CI收据/generation/文档未重写。

| 实际对象 | 字节数 | SHA-256 | 证据范围 |
| --- | ---: | --- | --- |
| GitHub CI完整构建归档 | 371376179 | fbfdc571713b1674abdc7606e96feb8b818884f74b5a07a4287998894ab2192e | 实际下载、服务器摘要、完整收据与官方证明通过；**不是客户端更新包** |
| 该构建的unsigned MediaFlow.exe | 223360546 | 975d69a8628f95a429b37d5378f09679ec464d0c1a85c8a445383ea93747c371 | 本机实际hash、身份启动、结构与官方subject验证通过；未正式签名 |
| 本机SDK r2隔离Test ZIP | 223914932 | 23214bb31f6a7f52555621c772ebe8f80193a62757500f0af30293f3d42a1354 | 本机真实包/成员hash、重新提取后Test验签、篡改拒绝 |
| CI SDK隔离Test ZIP的已证明报告值 | 223963620 | 48130d976e73fe6eb102c501ac289bafca3e9f674c3767484dbf9dde72829ebb | CI实际18项结果JSON及其官方证明；没有额外下载该Test ZIP做本机重hash |

隔离Test ZIP只含三个第一方PE和一份第三方保全夹具，不是完整应用发布包。CI报告文件实际SHA-256为 `69bdc3de5da52b90fc69e869b122c20d17868984f25e55d5708c2d2d7024a68a`；18个唯一case均通过，原始PE、Root保持且未导出私钥，`timestamp_service_protocol_tested=false`。本机r2测试签名者为 `8F031878AE407C3D233E48448F23101193F20A58`，明确TEST ONLY、不受信任；它不是正式发行者指纹。

[独立SBOM产物](https://github.com/xlxs123/MediaFlow/actions/runs/40512840774/artifacts/12737748835)为489531字节，SHA-256 `2b9f353d8f82ce3073817be485592620e3eb3fd4994220bb3ef0bd266699fefe`；原始CycloneDX文档SHA-256 `ccfc15754565dde44d4e984153c105a23fba9791de27598d5fa0214fbbbcf5c3`，实际schema/native验证通过。GitHub下载可能要求登录。[构建归档](https://github.com/xlxs123/MediaFlow/actions/runs/40512840774/artifacts/12738073427)同样属于CI验收产物，不能直接冒充已签更新ZIP。

10个证明分别关联EXE、完整收据、回归、结构、4份SBOM证据、2份Authenticode证据，官方GitHub CLI核验仓库、c3fa提交、实际run、main ref、固定工作流及SLSA来源。该证明不是发行者Authenticode，也不自动认证本地签后SBOM。

没有正式证书、可信TSA或正式ZIP时，不提供虚构的生产包SHA-256，不生成生产Ed25519清单。现有公网v2.1.0清单实际验签及篡改拒绝通过，清单摘要仍为 `580c204f3048c3443f51c6199ee25a62bb9f88d158cdced832bc41ae6632bf44`；渠道（含公钥）摘要仍为 `fd817c8ecd76b8dc9a4e8f341c1ba649b83a5a4bd15ed6af90cf5ae562ceeea9`，13个稳定资产和来源标签未变。
