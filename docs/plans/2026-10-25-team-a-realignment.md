---
title: "SpeechNexus 深度复盘与下一阶段实施路线"
status: active
version: "2.0.0"
date: 2026-10-25
---

# 从已验证组件走到第一条真实文件链路

本次依据用户“进行深度总结整理，继往开来”更新原复盘，日期按北京时间。
冻结主干为 360bb81fa173e9c235abafcb2c10cb2ecc47380b，包含 B 的 #147，尚不包含 A 的 #141。
原规划 #44 已合入 8333e4ba7c722a36cc70545fd5a0ed2beee38eea，首批生成与 create_job 已交付，不重新启动这些里程碑。
原三队方案、A00–A07 与 G0–G5 总范围保持。

## 结论与统计口径

当前处于“已有可靠组件，尚未贯通真实业务链路”的阶段。下一交付目标是：
**同一集成版本上，一次公开文件 ASR 请求从上传走到真实执行、持久结果、受控发布和 SDK/工作台下载，并在重放、确认丢失、取消和重启后保持原身份。之后沿同一协议接 CustomVoice TTS。**

当前 40 条非 PR issue = 原规划 38 条 + B 阶段任务 [#142](https://github.com/hrygo/SpeechNexus/issues/142) + A→C 交接 [#149](https://github.com/hrygo/SpeechNexus/issues/149)。
原25个工作包均open：21个in-progress，4个planned（A05/A06/B05/B07）；完整工作包0/25，Gate 0/6。
#142/#149单列，不改变原分母。步骤勾选、组件合入、作者本机报告与整包AC分别判断，不按累计PR、测试或文件数推算产品完成率。

本次读取当前代码、PR/CI和issue，没有重新执行业务测试、访问GPU或复跑C本机环境。
下表引用各精确版本的既有证据，适用范围随行列明；完整工作包和阶段要求继续由
[跟踪矩阵](2026-10-23-implementation-tracking.md)及各issue的AC维护。

## 可复核交付矩阵

表内SHA为实际合并提交；明确标为head/base的行不属于主干交付。

| 组件与状态 | PR / 精确SHA | 验证与实际成立的行为 | 剩余AC / 边界 |
| --- | --- | --- | --- |
| A00首批生成、所有权：已合入 | [#46](https://github.com/hrygo/SpeechNexus/pull/46) e06501d664ec1b54df74d3f0d4c4aad0898a967c；[#51](https://github.com/hrygo/SpeechNexus/pull/51) 88fdacccc5372b7fdd077441490f40bd1dfc78ba | 锁定生成器、离线闭包、Python/TS正负例、漂移、源码外wheel；首批49组消费者样例 | 不是全部runtime/HTTP/事件覆盖；REST/WS/MCP统一语义、时序及三队真实消费仍待 |
| A00有限兼容门禁：已合入 | #115/#117/#118/#120；后续修复[#122](https://github.com/hrygo/SpeechNexus/pull/122) b03e07f3b09e815c2701d0010f9978c516c9267a | 四份HTTP、18协调请求、TransactionResult和speech路径的有限结构比较；布尔/数字、字面量引用和OpenAPI对象引用反例修复 | 仅声明支持面，不证明任意schema等价或业务时序，A00整体AC开放 |
| A01 Job/outbox与恢复：已合入 | [#49](https://github.com/hrygo/SpeechNexus/pull/49) f210821d655bd7deb326825d750d83584c3ce159；[#128](https://github.com/hrygo/SpeechNexus/pull/128) 75dc96afd497d741311db732ad106659f2746ac0 | 真实PG/InnoDB原子写入/冲突/回滚；#128的59例覆盖实际提交丢回执→读取受压→原身份恢复，无跳过 | 六字段CreateJob/JobFact只是协调元数据，尚非完整公开业务Job/有效配置/结果/用量/Operation |
| A01/A03准入、runtime投递与隔离：已合入 | #87/#88/#99/#100/#102；[#105](https://github.com/hrygo/SpeechNexus/pull/105) 329e7d21689dd8e7be6ae696a03ba6ac1b6f205b | 双库竞争、丢回执、TLS/崩溃/损坏原文、claim/CAS、查询失败保持未知、先隔离记账后激活；新增共享回归无跳过 | 真实批准计划/readiness、完整scheduler/业务quota/结算、18端口跨owner联合及目标拓扑仍待 |
| A02/A07首资源生命周期：已合入 | [#58](https://github.com/hrygo/SpeechNexus/pull/58) d7463fb3e6421d7fe57efdf99566f241a163c7bb；[#61](https://github.com/hrygo/SpeechNexus/pull/61) 01d549f9fbe7617956f23570039eba1544b57d46 | 实际UTF-8文本检查、PG/MinIO版本/长度/摘要、HTTP/Range、7天默认TTL与恢复；存储35例、HTTP/维护12例 | Audio AssetInspector未接入；Artifact/receipt、通用删除/独立副本和备份恢复资格仍待 |
| A02 Profile草稿：已合入 | #107/#108；[#113](https://github.com/hrygo/SpeechNexus/pull/113) fea2def25e7f732a0971c381377cba8117309db9 | 真实PG创建/固定读取、CAS/原键、未知/取消/重启、会话/有效身份及实际Python/TS HTTP；受检集成1034通过/22既有跳过 | draft-only，不代表可信校验、发布/激活或默认指针 |
| A02 Binding/默认快照：已合入 | #125/#126/#129/#131/#133；[#134](https://github.com/hrygo/SpeechNexus/pull/134) a3611c21f2e29308374aadfd9bf0cbc63a9336b9 | 0010/0011私有不可变存储、四表writer真实HTTP；113项相关双库/PG回归无跳过；并发回退保护与原键恢复 | 仅声明/草稿事实，权威来源、资格接纳与最终参数仍待，不回填历史默认 |
| A-CONFIG-01/P1a：已合入 | 契约前置#136；[#139](https://github.com/hrygo/SpeechNexus/pull/139) 78b0528bfdcac68256199c6dec3aa4fbfef245cf | 完整原意图、目标/定义/意图摘要绑定、默认不可用；39新单元/155组合，合后CI40157256453全13项 | 只读观察不是提交许可；真实生产者、原结果优先/事务内重检、发布账本/CAS/HTTP仍待 |
| A00 render generation：已实现未合入 | [#141](https://github.com/hrygo/SpeechNexus/pull/141) head b840ea2d2f6f4b538cafbfe2355ac464a5cc7808；受检base 7b042d59c4870346326da3be4dbb8adbe50c8e69 | CI40159242336 full全14项；3972单元/81.64%，1243集成通过+22既有跳过；六ZIP/互斥全集、29 HTTP/4 DTO、合成回环和wheel已验证 | 仅JSON/DTO候选；自动审核拒绝合并待用户确认。当前main360的组合未验证，不计主干入口或实际公开Job/C路由 |
| B原命令消费：已合入 | [#140](https://github.com/hrygo/SpeechNexus/pull/140) 4b5f9e4d64ceb4eb94a8aabf448e5d38794489bb | 完整原参数、固定输入、未支持能力在I/O前拒绝、迟到不得成功；合成识别回归 | 不等于真实引擎已接持久submit/observe/abort/恢复，不证明停止/释放或GPU资格 |
| B私有ASR结果：已合入 | [#147](https://github.com/hrygo/SpeechNexus/pull/147) 360bb81fa173e9c235abafcb2c10cb2ecc47380b | EOF后有界汇总、独立异步I/O一次PUT/固定版本HEAD/GET/摘要；155定向回归；实际MinIO三例与其余输入用例合计28通过，无跳过 | VAD/识别为替身；持久回执登记/查询、真实执行与A发布未接。输入同步HEAD状态头总期限仍缺，不能外推结果端期限保证 |
| CI路由/Token/页面门禁：已合入 | #143–#145；[#146](https://github.com/hrygo/SpeechNexus/pull/146) 8b94167d494808c73ac45ebc98478195be09f9b1；[#148](https://github.com/hrygo/SpeechNexus/pull/148) 8eb52eb43d0caacbd4c4a7ab7d9d58a09e169b85 | 精确diff分流、full真实分片、39 Token回归、29浏览器回归/504语义色对测量及反例；必需检查保留 | 静态Token不覆盖任意动态JS；局部页面探针不等于完整WCAG/屏幕阅读器/跨设备资格；单次时延不作SLA |

当前主干的[合后CI40165031760](https://github.com/hrygo/SpeechNexus/actions/runs/40165031760)全14项成功：
4003非集成通过、81.88%、9既有warning；1268集成收集=1246通过+22既有PG-only不适用跳过。
这是组件回归，不是新增真实GPU/公开链路资格，也不能与#141旧base的3972/1265混算。
历史失败、取消、RED→GREEN、最小角色与精确测试合并来源保留在原PR/CI，不改写成全范围通过。

## 未集成实现与证据债务

| 候选 | 已有作者证据 | 集成缺口与解除条件 |
| --- | --- | --- |
| C #35–#43 | base 4b08c396c0fe8fc43c7e85683e534befab6bb22b；真实本地PG/HTTP/组合根、纪要本机模型/浏览器、删除重放、章节/单元登记；导出worker默认关闭预接线 | 未提交/推送，无可评审新head，不计共同main。C提供可获取提交或完整补丁/摘要、迁移owner片段、组合根/HTTP注册与生产消费者清单，再同版复核；A不代改C issue或提交其本机代码 |
| B worker观察扩展 | #28/#30/#142报告原工作树候选与受控回归 | 未提交/集成候选没有可作为交付版本的新head；与#140/#147分开。B按真实执行链需要交付版本/回执与故障证据 |
| A→C助手交接 #149 | planned，无该交接真实成功证据 | A04提供真实可查询Speech Job，A06/B05提供已发布声音，再由C验证同键/取消/超时；默认关闭页面或fake ID不消项，不前移为文件G1前置 |

C最新[第69轮清点](https://github.com/hrygo/SpeechNexus/issues/42#issuecomment-6393997645)已纠正旧统计：
按method+path、排除测试文件后，“无直接Web调用”原始条目为6；识别等价消费后，
getTranscriptRevision并非缺陷，章节/单元读取已有列表与实际HTTP覆盖，当前明确待补直接消费包括updateProject与getVoiceRevision。
这是作者本地清点，不等于6个功能缺陷或共同主干验收。不能继续引用“只剩3个”，也不能与下方A00的4项runtime缺口相减。

## 原因、风险与应保留的工程成果

1. **验证层级容易混淆。** 生成样例、事务/HTTP、真实存储、实际模型、公开链路与目标拓扑分别记证据。
   原键/完整命令、单事务预算、最小权限、未知恢复和真实停止后释放规则继续作为业务链底座。
2. **真实来源尚未接到生产者。** ProfileValidation是客户端声明，草稿/快照是声明格式；B部署报告不自动成为Profile资格。
   给来源、owner、固定版本与当前适用条件，不能再靠包装类型解除依赖。
3. **业务Job与协调身份仍有缺口。** 当前app注册健康、Asset、Workspace与草稿资源。
   CreateJob六字段/JobFact不能投影出未持久化的有效配置、正文结果、用量或Operation。
4. **对象与可恢复结果仍有缺口。** #147的固定私有对象不代表执行成功，同键再次调用可能新增对象版本。
   B观察入口仍拒绝非空staging引用；先冻结完整元数据/storage_version及登记/查询，再接持久事实，不能只删除拒绝。
5. **共同版本债务限制联合验收。** C已有真实本机成果，仍需可获取制品与兼容迁移。
   不再称纪要“缺LLM”，也不把本机成功外推到Linux、内容出站、真实设备或生产资格。
6. **共享集成窗口造成重复验证。** #141实现保持15文件不变，却随#140/#143/#144/#145多次更新基线。
   建议汇集共享变更后安排稳定评审窗口，先合受检单元再处理无关main写入；新head仍须正常检查，不复用旧绿灯或改保护。

## 下一阶段最小可验收路线

复用#21/#22/#23/#142/#37，先文件ASR，再CustomVoice TTS，不新建重复工作包。
每轮最多一个主要写入单元；源、生成、锁、迁移与组合根串行，按具体制品解除依赖，不要求整个上游包关闭。
A-CONFIG-01继续作为配置工作流，不能阻塞所有无Profile请求。

| 次序 / owner | 最小交付结果 | 依赖解除与验收出口 |
| --- | --- | --- |
| 下一A单元：A02+B03音频检查 | 在现有AssetInspector/对象发布中真实检查asr_input，保留7天TTL、原键和固定对象版本 | 先确定B有界媒体复用边界/版本，不跨服务导入gateway应用层或复制解码器；真实A HTTP+PG+MinIO验证合法音频、损坏/截断、超限/取消、重放/重启，不据MIME/文件名判ready |
| 并行具名交接：A02+B #142配置 | 固定首条真实ASR的Binding、显式默认、模型/软件/容量及支持参数，产出完整有效配置 | B提供真实版本/上界；A检查绑定域、硬约束、允许覆盖与exact拒绝。无Profile合法；有Profile必须已发布固定引用。缺事实保持不可用，不从草稿/示例猜值 |
| A01/A03/A04业务Job | 固定音频与有效配置，持久业务事实/事件，复用原Job/outbox和完整配方准入 | 真实PG与共享双库竞争/丢回执/取消/接管；公开Job与完整命令一致，旧owner、原身份和父预算规则不变 |
| B #142执行与结果回执 | 从原持久submit查询/恢复唯一真实执行与唯一完整结果回执 | 先冻结内部登记/查询元数据、storage_version与所有权；修复或明确阻断输入HEAD总期限缺口；真实执行、丢确认/重启/取消不重复外发或重写结果，unknown不释放 |
| A02/A04制品发布 | 消费B持久结果，发布Artifact/receipt/Operation和公开查询/下载 | 固定执行/对象版本/摘要；原子发布、迟到/取消/重启/发布确认丢失可恢复。私有JSON按原请求转成JSON/text，不伪造用量/语言/时间戳 |
| A04+B03+C02联合出口 | 标准SDK上传→真实转写→公开下载，C导入/校对/导出 | 同SHA、模型/镜像/语料/拓扑；正常/无语音、重放/未知、取消/重启和双逻辑节点竞争。ASR子链通过不自动放行仍含TTS的G1；物理多机与逻辑集群分列 |

配置工作流保留P1真实可信生产者→P2原结果优先/账本与指针CAS→P3受控HTTP/固定已发布查询→P4最终参数。
#139仅P1a。Profile发布不替代音色发布，不修改历史Job；不为样例启用假预设、回填默认或扩大生产权限。
实时/音色/辅助、通用删除/备份恢复、MCP/完整SDK与目标拓扑仍属于原范围，后续按真实依赖验收，不静默删项。

## A00排队项与审批边界

| 操作 | 当前main的runtime支持 | 候选/后续验证 |
| --- | --- | --- |
| POST generateRenderUnit | 未选择 | #141仅JSON/DTO候选；审批解除后仍需最新main同步、新head审查/CI及合后交接 |
| GET downloadSavedAsset | 未选择；200/206 octet-stream | 明确媒体投影、Range/错误、字节/取消上界与实际消费者 |
| GET downloadBookExport | 未选择；200/206 octet-stream | 同上，保留本操作409/410等声明状态，不复制另一操作错误集 |
| GET streamExportEvents | 未选择；SSE指向六成员ExportEvent | 同源union、Last-Event-ID、410/断线恢复与有界帧；单条样例不冒充时序或C业务成功 |

源当前未将206 Content-Range标必需，也未声明下载416；在源与C实际路由对齐前不自造状态或全局兜底。
A的runtime口径独立于C的生产消费者清点；完整静态类型不代表运行时解析/传输支持。

#141代码/精确版本复审和旧base full CI已完成，管理端合并被自动安全审核拒绝，
理由为实际接口/A00/Gate未完成、局部合并授权前提不足；管理端已请求用户明确确认。
当前保持未合并，不换工具重试；本次总结不是该批准，不改变其head或移入本轮文档交付。
若获确认，先正常同步最新main、复核实际增量/必需检查/未解决意见，再由管理端处理。
跨队Gate、正式发布、生产grant/DDL、GPU与内容使用遵守原授权。

## CI与执行纪律

文档PR轻量路径实测16秒，对照原#138全量821秒；不同runner/排队，仅是两次观测。
#141两个完整组合为285秒和706秒，后者pytest175.18秒；总时延与测试执行分开记录。
当前main合后CI为292秒、pytest233.51秒，不承诺持续五分钟，也不据此判断业务性能。
[CI范围](../architecture/ci-validation.md)保留full/轻量边界，Token与页面门禁不替代业务验收。

每轮先写清“解除哪条业务依赖、固定哪份制品、何种正反例可验收”，再实施。
仅整理文档/issue时复用有效证据，核对链接/声明/diff，不安装依赖或重复全库/硬件测试。
正文维护目标、进展、任务与AC，细节链接原PR/CI，保留失败记录，不堆叠平行历史章节。
C issue由C维护；主包/Gate只有真实AC齐备才关闭，由用户最终放行。
