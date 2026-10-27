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

本阶段新源码的CI构建、官方证明与完整只读DryRun在实测后追加。未签名CI ZIP、隔离Test ZIP、正式发布ZIP是三个不同对象；没有正式包时不提供虚构的生产包SHA-256。
