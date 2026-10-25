---
title: "文件 ASR 私有结果交接"
status: active
date: 2026-10-25
---

# 文件 ASR 私有结果交接

`CommandFileAsr.stage_result` 将[原命令消费者](frozen-execution-inputs.md)的完整文件结果
写入私有对象存储。它是 [B-G1 #142](https://github.com/hrygo/SpeechNexus/issues/142)
最终制品交接的一个组件，不是持久接收引擎、执行完成事务或公开下载入口。
调用方仍须先持久化并准入完整原命令，在短提交握手之外运行文件任务。

## 输入、输出与所有权

| 边界       | 约定                                                                                  |
| ---------- | ------------------------------------------------------------------------------------- |
| 输入       | 原 `SubmitRequest`，同源完整校验、规范摘要、绑定/策略身份和已支持参数规则沿用原消费者 |
| 正文       | UTF-8 `application/json`；复用公开 `Transcription` 的最小形状 `{"text":"..."}`        |
| 汇总       | 按原段顺序，以一个换行连接非空正文；保留原文，不作规范化、猜词、字符串去重或语言推断  |
| 空转写     | 完整无语音文件保存 `{"text":""}`，11 字节，不写不符合引用最小长度的零字节对象         |
| 输出上界   | 调用方显式指定 11–4,194,304 字节，包含 JSON 与 UTF-8 转义开销；不是容量准入或业务配额 |
| 交接引用   | 复用内部 `ArtifactStorageReference` 的六个字段；不新增 wire DTO 或路由                |
| 私有元数据 | 原 `request_digest`、固定 S3 `storage_version`；只能由后续受权执行/发布者持久消费     |
| 发布与预算 | B 登记私有执行回执；A 负责公开 Artifact 登记、发布、响应和下载；停止/预算保持原权威     |

`response_format=json/text` 仍按原命令冻结。这里使用同一种私有 JSON 结果容器，
供 A 读取正文后按原请求生成公开 JSON 或文本响应；不能直接把 JSON 对象按 `text/plain`
发布。`verbose_json`、时间戳、对齐、分人及其消费者继续遵守原命令的未支持拒绝规则。
本组件不生成 usage、词级时间戳、语言或其他未实际测得的数据。

## 生成与校验

1. 在输入读取前拒绝非法结果上界，复制并核验原请求。
2. 消费原 `FileAsrTranscription`。检查段序、模型区间不重叠、源时钟及 binding/config/
   ASR/VAD policy 一致。源时钟换算可能有取整覆盖，不能据此作字符串去重。
3. 仅在成功 EOF、完整媒体 summary 与最终源/模型样本结束位置一致后封装正文。
   任一段失败、取消、格式或总量超限，均不写部分转写。
4. 对正文计算 SHA-256。对象身份由原完整请求摘要和正文摘要共同派生：
   `private/execution-results/<identity-digest>/transcription.json`。
   workspace 沿用原命令；调用方不能指定任意对象键。
5. 每次操作创建独立异步 HTTP 客户端，限定固定私有 IP origin、配置 bucket 和 PUT/HEAD/GET；
   复用锁定 MinIO SDK 的 SigV4 签名器。4 MiB 上界保证一次 PUT，不走 multipart、代理、
   重定向或自动重试，不共用其他操作的客户端或连接池。
6. PUT 必须返回非空、非 `null` 版本。HEAD 核对版本及长度，GET 固定该版本，流式核对
   精确长度、SHA-256 和 HTTP 正文结束；响应和操作专属客户端关闭后才返回引用。
7. 对整次 PUT→HEAD→GET 设置一个 monotonic 总期限，覆盖连接、状态行、响应头及正文；
   socket 无活动超时不充当总期限。取消/期限到达先取消该次异步 I/O，再等待其专属连接清理，
   不遗弃活动写线程，不关闭其他并发操作的客户端。取消及绑定检查在同一事件循环中持续检查。
   `CommandFileAsr.stage_result` 还按原绝对期限设置外层定时器，避免存储端口拖过任务期限。

输出正文在内存中有界汇总，单段先按字符长度做保守预检，再检查转义后字节预算；
单次转义的 UTF-8 临时缓冲最多为其字符上限的六倍。结果读回按 64 KiB 块计算摘要，不再创建
结果 spool 或跨进程临时文件；原文件输入仍沿用其私有、有界 spool。内容不通过跨容器共享目录传递。

## 失败与恢复边界

| 情况                             | 结果                                                          |
| -------------------------------- | ------------------------------------------------------------- |
| 原期限/取消/绑定漂移             | 沿用安全 ASR 错误；写入后才发现时也不返回可用结果             |
| 正文/段身份/完整性异常           | `asr_result_invalid` 或 `asr_result_incomplete`，不写部分结果 |
| 正文上界非法或超限               | `asr_result_limit_invalid` / `asr_result_limit_exceeded`      |
| 未得到版本                       | `asr_result_versioning_required`，不能按未版本化对象确认      |
| 读回损坏或不完整                 | `asr_result_readback_failed`                                  |
| 写入与读回版本不同、回执身份错误 | `asr_result_identity_mismatch`                                |
| PUT 超时、连接或供应商异常       | `asr_result_storage_unknown`；不转发供应商正文、凭据或路径    |
| 对象 I/O 总时限届满              | `asr_result_deadline_exceeded`；不生成存储成功回执            |

相同请求和正文派生相同键；再次调用可能创建相同正文的新存储版本，这不等于业务发布
幂等已实现。PUT 确认丢失后可能留下私有对象，本组件不自动重试、不删除未知对象，也不
重新识别来恢复结果。执行所有者仍须实现固定引用的持久登记、原身份查询、发布/取消竞争
与孤儿对象清理。没有登记回执不能反推对象不存在；对象存在也不能反推任务成功。

结果引用不含执行终态、停止或释放断言。本组件不修改 `submit_intents`、Job、lease、
output interval 或预算。[执行结果登记与查询](execution-staging-results.md)通过原观察事务
保存完整、固定版本的私有回执；单独 staging key 或不完整引用仍不能登记结果。
真实引擎接收/恢复、A 发布与 C 消费继续装配。

## 验证范围

单元测试覆盖原命令→真实编解码→测试 VAD/识别→正文汇总，JSON/UTF-8 转义预算、
无语音、晚取消/过期/绑定漂移、错误引用及写入确认丢失。真实 loopback 的 PUT/HEAD/GET
trickle-header 反例覆盖总期限、重复协程取消、取消标志及并发客户端隔离；原实现的八项反例
与原任务期限反例先失败，再通过修复验证。PUT 的非 200、传输或响应关闭异常均保留未知写入，
不进入 HEAD/GET、不重试或删除对象；读回异常单独分类。详细 RED→GREEN 证据见对应 PR。

`tests/integration/test_gateway_asr_results.py` 在项目隔离的版本化 MinIO 上验证有语音/
无语音结果的真实写入、固定引用读回和等长损坏拒绝；识别/VAD 仍是测试替身。
这些结果不证明真实模型、持久执行恢复、公开发布、GPU 停止或资源资格。
