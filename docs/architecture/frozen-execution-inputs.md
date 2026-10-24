---
title: "原执行命令与核验文件输入"
status: active
version: "0.1.0"
date: 2026-10-24
---

# 原执行命令与核验文件输入

这是 [B01 执行网关](execution-gateway.md)与 [B03 文件识别](file-asr-recognition.md)的输入接入单元。
已提供原命令只读端口、一次提交路径的命令桥，以及固定 MinIO/S3 的核验输入适配器。
默认组合根仍拒绝业务执行；真实引擎生命周期、完整有效参数映射、最终制品发布与公开消费者待接入。
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

`ExecutionObjectInputs.open_verified` 只在长度、摘要与完整响应 EOF 校验后给出可 seek 的流。
`MinioExecutionInputs` 由服务端显式配置固定私网 IP 字面量 origin、bucket、独立凭据及
当前用户拥有、绝对路径、权限不含 group/other 的临时目录。只允许 HEAD/GET，不使用
DNS/环境代理或重定向。HTTPS 验证证书；传入的 SSLContext 必须启用证书/主机名校验，
最低 TLS 1.2。地址校验本身不证明部署网络已实施出站隔离。

引用沿用机器契约的六个字段：资产或制品 ID、workspace、bucket、key、SHA-256、长度。
`FileExecutionInputs.open_asr` 要求文件 ASR 的原命令，检查引用 workspace 与执行身份相同。
对象路径采用 A 既有 `private/assets/...` 形态，不发明 workspace 路径前缀，不读取 A 业务表
建立另一份所有权判断；引用的所有者许可须在命令生产/准入边界完成。

原引用没有 S3 version ID。适配器先 stat 取得真实非 null 版本及长度，再固定该版本 GET；
覆盖当前对象不能让本次读取漂移到新版本。响应版本、Content-Length、编码、实际长度及
逐字节 SHA-256 必须全部匹配原引用，不信任 metadata 中的摘要。禁用版本化、找不到对象、
错 bucket、路径歧义、损坏、超限、取消或期限耗尽都不向媒体消费者暴露输入。

输入先进入进程私有 `SpooledTemporaryFile`，小文件保留内存，超过显式阈值落到私有临时目录。
远端连接在 yield 之前关闭，消费者退出后临时流关闭；下游异常不被翻译成文件读取错误。
这不是跨容器共享目录。`ExecutionInputLimits` 限定总输入、spool 阈值与消费期限；
单次读取另有至多 64 KiB 缓冲，阈值不等于进程 RSS 硬上限。网络连接/读/pool wait 各有
3 秒界限，整体期限与取消按阶段和块合作检查，不承诺瞬时中断底层网络调用。

`VerifiedFileAsrInput` 返回同一完整请求、原媒体格式与已核验流，供既有
`FileAsrAudio` / `FileAsrTranscription` 消费。本单元不自动选择 ASR/VAD 配方、补有效参数、
忽略资源引用或发布识别结果。未实现的参数必须在后续配方装配时明确拒绝。

## 验证范围

单元入口：`tests/unit/gateway/test_original_command.py`、`test_execution_objects.py`。
实际隔离服务入口：`tests/integration/test_gateway_frozen_inputs.py`，使用集成门禁规定的
PG/InnoDB/MinIO 环境变量，缺服务不算通过，配置见[集成测试说明](../../tests/integration/README.md)。

集成覆盖原网关角色、原命令往返与篡改/清理/版本拒绝，stat/GET 之间覆盖时的版本固定，
以及持久原请求→真实 MinIO→真实媒体解码→现有 VAD/文件识别消费者。
VAD/识别端口使用合成测试替身，不加载模型，不使用私人语料；无预算、停止或发布写入。
真实模型、请求级 abort/status、停止＋释放双证据及物理多机资格另行验收。
