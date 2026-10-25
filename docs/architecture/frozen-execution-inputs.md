---
title: "原执行命令与核验文件输入"
status: active
version: "0.3.0"
date: 2026-10-25
---

# 原执行命令与核验文件输入

这是 [B01 执行网关](execution-gateway.md)与 [B03 文件识别](file-asr-recognition.md)的输入接入单元。
已提供原命令只读端口、一次提交路径的命令桥，以及固定 MinIO/S3 的核验输入适配器。
另有原命令驱动的文件 ASR 消费者，复用同源校验并映射已支持的参数。
默认组合根仍拒绝业务执行；真实引擎生命周期、其余参数/能力、最终制品发布与公开消费者待接入。
不能因为读取了原命令或成功消费合成文件，就宣称真实执行、停止或模型资格完成。

## 原命令

`OriginalExecutionCommandSource.read_request(intent, max_document_bytes=...)` 只读取
`submit_intents` 已持久化的完整请求。PostgreSQL 使用原网关角色和有期限的短只读事务，
没有新表、DDL、授权、预算或另一份命令权威。同 SQL 的 InnoDB 适配仅存在于测试 oracle。

- 按原提交 ID 读取，核对完整身份、请求摘要与版本；版本变化拒绝读取。
- SQL 的 `OCTET_LENGTH` 与 `CASE` 在传回正文前实施字节上限。调用方显式指定上限，
  进程读取最多 32 MiB；这不是公共协议默认值或新的调度配额。
- 拒绝已清理或部分 tombstone、缺失/超限正文、重复 JSON 键和非有限数值。
- 用既有生成的 `SubmitExecutionRequest` 校验；复用 `SubmitRequest` 原序列化器，
  要求规范正文、全部身份、完整请求摘要、command digest、输出流身份完全一致。
  不补当前默认值，不用摘要重建历史请求，不把正文放入公开结果或错误。
- 此读取本身不授予再次外发的权限。停止/恢复查询不会调用命令桥。

`OriginalCommandDispatch` 兼容既有 `EngineDispatchPort`，仅放在原
`SubmitExecutionService` 的“持久化新 intent 后调用一次”路径上，转给消费完整
`SubmitRequest` 的 `CommandEngineDispatchPort`。重复提交只查原事实；已观察/恢复的
intent 不能进入桥。桥本身不是新的持久幂等权威，不能绕开原服务单独注册为入口。

读取失败时本次没有调用下游，返回 `NOT_SENT`；调用下游后出现不确定异常则返回
`RESPONSE_LOST`，沿用原 unknown 隔离与占用，不伪造清洁失败或停止事实。
引擎适配器仍须核验原绑定、generation、fencing、资格和完整参数；B 负责该实现。
A 的预算仅由原准入/释放事务裁决。

## 对象输入

`ExecutionObjectInputs.open_verified` 是异步上下文端口，调用方使用 `async with`；
只在长度、摘要与完整响应 EOF 校验后给出可 seek 的流。
`MinioExecutionInputs` 由服务端显式配置固定私网 IP 字面量 origin、bucket、独立凭据及
当前用户拥有、绝对路径、权限不含 group/other 的临时目录。只允许 HEAD/GET，不使用
DNS/环境代理或重定向。HTTPS 验证证书；传入的 SSLContext 必须启用证书/主机名校验，
最低 TLS 1.2。地址校验本身不证明部署网络已实施出站隔离。

每次读取使用独立的异步 HTTPX 客户端和连接池，只复用锁定 MinIO SDK 的 SigV4 签名器，
不调用 SDK 的同步网络入口。输入与结果写入复用固定来源、签名和 I/O 总期限工具，
各自保留方法白名单与错误语义；不自动重试。HEAD/GET 都采用流式响应、禁用自动解压，
只有 HTTP 200 进入后续处理。其他状态在读取正文前关闭响应，不解析供应商错误正文。
正常 HEAD 只检查响应头并立即关闭，正常 GET 由有界对象消费者核验；响应或客户端清理失败
同样不暴露输入。对象键和版本作为不透明值分别转义，空格、Unicode 和字面量 `%` 不改写成另一键。

引用沿用机器契约的六个字段：资产或制品 ID、workspace、bucket、key、SHA-256、长度。
`FileExecutionInputs.open_asr` 同样使用 `async with`，要求文件 ASR 的原命令，
检查引用 workspace 与执行身份相同。
对象路径采用 A 既有 `private/assets/...` 形态，不发明 workspace 路径前缀，不读取 A 业务表
建立另一份所有权判断；引用的所有者许可须在命令生产/准入边界完成。

原引用没有 S3 version ID。适配器先 HEAD 取得真实非 null 版本及长度，再固定该版本 GET；
覆盖当前对象不能让本次读取漂移到新版本。响应版本、Content-Length、编码、实际长度及
逐字节 SHA-256 必须全部匹配原引用，不信任 metadata 中的摘要。禁用版本化、找不到对象、
错 bucket、路径歧义、损坏、超限、取消或期限耗尽都不向媒体消费者暴露输入。

输入先进入进程私有 `SpooledTemporaryFile`，小文件保留内存，超过显式阈值落到私有临时目录。
远端连接在 yield 之前关闭，消费者退出后临时流关闭；下游异常不被翻译成文件读取错误。
这不是跨容器共享目录。`ExecutionInputLimits` 限定总输入、spool 阈值与消费期限；
单次读取另有至多 64 KiB 缓冲，阈值不等于进程 RSS 硬上限。网络连接/读/pool wait 各有
3 秒无活动界限。另有 monotonic 总期限，覆盖 HEAD/GET 的连接、状态行、响应头、正文与
网络清理；持续缓慢发送数据不能延长总期限。取消标志在同一事件循环内每 10ms 检查，
协程取消或期限到达先取消本次异步网络任务，再等待其专属连接清理；不会遗弃活动同步网络线程，
也不关闭其他并发读取的客户端。输入完整核验且网络清理结束后才 yield。
这仍不是磁盘、媒体解码或 GPU 的硬停止证明；私有 spool 写入及后续解码各遵守自身边界。

`VerifiedFileAsrInput` 返回同一完整请求、原媒体格式与已核验流，供既有
`FileAsrAudio` / `FileAsrTranscription` 消费。输入端口本身不选择 ASR/VAD 配方或发布结果。

## 原命令驱动的文件 ASR 消费者

`interfaces.file_asr_command.CommandFileAsr` 显式组合原生输入、解码、VAD 与文件识别。
它先复制原命令，复用生成的 `SubmitExecutionRequest` 校验和既有 canonical digest，
核对原绑定与配置策略身份，再读取核验对象。调用方修改原请求不会改变已打开的执行。
该入口没有独立 wire/schema、业务路由、预算账本或命令权威。

| 命令字段 / 配置 | 消费语义 |
| --- | --- |
| `kind/task_kind/stage_role` | 只接受 `asr/asr_file/asr_batch`；流式和复核阶段分别接线 |
| `language/prompt/temperature` | 原值映射 `AsrPolicy`；null prompt 表示无提示词，转换为空字符串 |
| `turn_detection` | 只接受 `server_vad`；原 `silence_duration_ms` 映射 `VadPolicy` |
| `max_segment_ms` | 原值映射 VAD 硬切段上限，且必须不小于 preview interval，不沿用调用方另填的切段长度 |
| `finalization/rollback_tokens` | 当前只接受 `full_segment/0`，不冒充流式回滚或实时复核 |
| `final_deadline_ms` | 从段关闭到正文 final 的完整窗口；单次 HTTP 另取它与显式部署调用上限的较小者 |
| 原 `deadline_at` | 在输入、解码/VAD、识别及结果检查中合作检查；到期报告 deadline，已可能外发仍保持未知事实 |
| `alignment/diarization` | 当前都必须为 `off`；required 和 best_effort 均不静默忽略 |
| `vocabulary_set_revision/resources` | 当前不支持词表、发音表或声音资源；非空材料/引用在对象读取前拒绝 |
| `response_format/timestamp_granularities` | 当前仅接受 json/text 且不请求时间戳；最终序列化和发布仍由结果拥有方接入 |
| `preview_interval_ms/auxiliary_deadline_ms` | 按同源契约校验；文件入口不产生预览，辅助关闭，因此不启动对应实时/辅助步骤 |
| token 上限、VAD 声学阈值、媒体/对象读取界限 | 由批准部署显式提供；VAD tuning 必须匹配原策略 revision，不从当前默认或示例补值 |

`open(request, cancelled=...)` 返回原 `FileAsrTranscription` 的单次迭代器，退出、调用方异常、
取消和期限耗尽均关闭媒体与私有临时流。绑定在对象读取期间变化也不允许改投另一模型。
`has_dispatched` 只表示可能已有识别调用；不是接收回执、持久引擎状态或停止/释放证明。
本地期限检查不替代数据库的租约准入或 A 的预算裁决。
媒体/VAD 保留段关闭时钟的保守下界（含关闭该段的批次处理），不在延迟交付 pending 段时
重新计时。识别在外发前检查剩余窗口，以剩余时间约束异步调用，且在返回/输出前再次检查；
等待期间过期、引擎迟到或不响应均不产生成功 final。该时钟只用于本进程的期限，不充当源音频
时间戳、可跨进程恢复的事实或 GPU 释放证据。

调用方必须已持久记录并授权原执行；不能把该长迭代器放进原入口锁的短提交握手，也不能
注册成绕过准入的公开接口。真实引擎受控接收/恢复、强停止事实、最终发布、重叠合并及
其他参数/资源能力仍待后续闭环装配；本消费者不宣称这些能力已实现。

## 验证范围

单元入口：`tests/unit/gateway/test_original_command.py`、`test_execution_objects.py`、
`test_file_asr_command.py`。
`test_execution_object_transport.py` 使用实际 HTTPX 客户端与固定来源传输层，只控制底层网络响应，
以正文读取即失败的 guard 验证巨型、分块、压缩和其他错误状态零正文读取及清理，
并覆盖来源/方法拒绝、环境代理隔离及固定版本读取。原同步 SDK 回归迁到这一实际消费边界。
`test_execution_object_deadline.py` 使用真实 loopback TCP 的慢 HEAD/GET 响应头和慢正文，
验证总期限、重复协程取消、取消标志、socket 关闭以及慢请求不阻塞另一读取。
实际隔离服务入口：`tests/integration/test_gateway_frozen_inputs.py`，使用集成门禁规定的
PG/InnoDB/MinIO 环境变量，缺服务不算通过，配置见[集成测试说明](../../tests/integration/README.md)。

集成覆盖原网关角色、原命令往返与篡改/清理/版本拒绝，HEAD/GET 之间覆盖时的版本固定，
以及空格、Unicode、百分号转义等不透明对象键的真实签名与回读，
以及持久原请求→同源命令参数转换→真实 MinIO→真实媒体解码→现有 VAD/文件识别消费者，
核对原语言、提示词、温度、期限、切段与策略摘要，不再手工另填识别策略。
VAD/识别端口使用合成测试替身，不加载模型，不使用私人语料；无预算、停止或发布写入。
真实模型、请求级 abort/status、停止＋释放双证据及物理多机资格另行验收。
