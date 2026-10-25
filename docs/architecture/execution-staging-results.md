---
title: "执行结果回执的持久登记与查询"
status: active
date: 2026-10-25
---

# 执行结果回执的持久登记与查询

本单元接在[文件 ASR 私有结果组件](file-asr-results.md)之后，将已核验回执保存在原
`submit_intents` 记录中，供原身份恢复与 A 发布消费。它复用
`record_execution_observation` 和原锁序，没有新增任务表、协调服务或预算权威。
真实引擎的持久接收/唯一执行仍须另行装配，不能把本单元当作完整业务链。

## 所有权与协议

| 事实 | 权威与消费者 |
| --- | --- |
| 原命令、执行关联、私有结果回执 | B 网关维护；回执通过现有观察事务登记 |
| 固定对象版本、长度和摘要核验 | B 私有对象适配器；登记者只能消费可信执行者的完整回执 |
| 公开 Artifact、业务 Job 结果、发布与下载 | A 按当前任务、取消、版本和发布规则裁决 |
| 执行终态、停止与资源释放 | 原 `record_stop` 及可信证据生产者；结果登记不签发这些事实 |
| 预算与租约归还 | A 原事务；回执、EOF 或 HTTP 成功均不是释放许可 |

机器源为 `execution-control.openapi.yaml` 的 `ExecutionStagingResult`、
`ExecutionStagingResultResponse`、独立的 `ExecutionAudioMetadata` /
`ExecutionAudioStagingResult` / `ExecutionAudioStagingResultResponse`，
以及协调观察请求里两个互斥的可选属性 `staging_result`（JSON）与
`audio_staging_result`（音频）。音频是新增属性而不是把旧属性改成 `oneOf`，
旧属性的形状和约束因此逐字不变。
Python/TS 从原生成器生成，不维护独立 wire 类型。

回执固定原 `request_digest`、现有六字段对象引用、非空且非 `"null"` 的 `storage_version`
和 `media_type`。旧 JSON 类型继续只接 ASR 的 `application/json`，正文为
11–4,194,304 字节；新音频类型接 WAV/PCM/MP3、24 kHz mono，源 PCM 与交付
各最多 64 MiB。源样本时钟、摘要与派生时长必须一致，不能用 MP3 padding 计量。
引用不携带凭据、下载地址或业务正文；这些协议/组件不等于真实音频或完整链资格。

现有状态响应只投影已登记回执的六字段引用；历史 staging key 不能投影为完整引用。
固定存储版本通过新查询返回，没有给旧状态响应增加未知字段。
普通观察可以原样回显已登记引用，但不能借回显登记、覆盖或补齐未知对象。
新登记请求中的观察引用与完整回执引用必须一致。

## 登记与恢复

1. 可信执行者先取得原持久命令和唯一执行资格，在短提交握手之外完成 ASR 与私有对象核验。
   查询或发现“尚无回执”均不授予执行资格。
2. 用原观察事务提交完整回执和 `expected_version`。事务按
   pool → instance head → instance → attempt → lease → submit 锁定，
   等待后重新读取身份、fencing、sealed barrier、实例代次、租约与数据库期限。
3. 首次登记只接受已经 `executing/pending` 的执行；实例须仍为原代次的 ready 或 draining，
   租约和两层期限仍有效。停止、quarantine、取消或旧任期不能新登记结果。
   draining 只允许已有执行收尾，不改变新提交仍要求 ready 的规则。
   音频还须匹配原 PCM 流与水位，并在同一 submit 锁内核对生成区间完整覆盖
   `[0, source_pcm_samples)`；只有 max-end 水位或非生成诊断区间不能补齐缺口。
   登记不追加区间、不修改水位；已登记完整文件后也不能追加超出该结果的生成样本。
4. 一次短事务同时推进原执行版本、保存规范化回执文档和既有 staging 指针。
   事务内没有 HTTP、对象写入、推理或编码；中途失败全部回滚。
5. 相同回执在查询所得当前版本重放是只读操作，即使已有取消/停止，也不倒退状态。
   不同回执不能覆盖原事实，包括正文相同而存储版本不同的情况。
6. 保留原 CAS 规则：旧 `expected_version` 返回冲突；确认丢失先查原身份，
   不能把冲突或空结果解释为再次推理、PUT、发布或删除的许可。

普通观察与停止事务不能改写回执；登记也不能把 execution 改成 succeeded、
完成 cleanup、签发停止证据或修改 A 计数。成功 EOF 只说明结果完整，不证明 GPU 释放。

## 只读查询与兼容性

`GET /internal/v1/executions/{submit_intent_id}/staging-result` 复用组件认证、请求 ID、
节点/网关范围和 ingress 所有权检查。它只读原数据库事实，不调用引擎、写对象或退预算；
调用方不能指定存储端点、bucket 或 URL。

响应固定 `submit_intent_id`、原 `request_digest` 和 nullable `staging_result`。
回执内摘要必须与原请求一致，由已验证领域记录投影；A 仍须核对自身固定的请求、
attempt、取消状态和发布权限。回执不是公开发布资格。

| 结果 | 含义 |
| --- | --- |
| 200，完整回执 | 已有持久事实；读取固定版本后仍须核对长度、摘要与发布权限 |
| 200，`staging_result=null` | 尚无完整登记；不证明对象不存在、计算未发生或可以重试 |
| 404 | 当前网关范围内未找到原执行，不能创建替代执行恢复 |
| 401 / 422 | 凭据或请求格式无效 |
| 503 | 原事实暂不可读；按原恢复指令查询，不伪装为空结果 |

旧调用方仍可使用原状态接口与不带回执的观察请求。新增能力只交给已部署该版本的网关；
旧网关拒绝新字段或不提供新路径时保持不可用，不回退为直接访问引擎。
此处没有改变公共 REST、SDK 或工作台协议。

音频消费者使用独立的
`GET /internal/v1/executions/{submit_intent_id}/audio-staging-result`，
认证、所有权、错误及只读语义相同。旧 JSON view 对音频返回 null，音频 view 对
JSON 返回 null；已有引用仍可经通用状态读取。任何 null 都不授予重新外发资格。
严格旧 HTTP schema 不增加音频字段或枚举；迁移详见
[ADR 0010](../decisions/0010-private-audio-receipt-compatibility.md)。

## 存储、权限与迁移

`0012_execution_results` 只新增 nullable `staging_result_document TEXT` 与基础形状约束。
应用把回执规范编码为至多 16 KiB 的 UTF-8 文档，并通过领域对象验证；
数据库不用 JSON 专属运算、触发器或跨表存储过程裁决语义。
指针与回执在同一事务中一致，旧记录默认 NULL，不从 key、摘要或可变默认补值。

部署顺序为 owner 升级 → 重发网关列级 UPDATE 权限 → 部署新读写进程；
运行角色不执行 DDL。只有 B 角色新增该列写权限，A 数据库角色仍不能直接读取私有回执。

音频复用该列，无新增迁移或权限。所有相关读取者先升级，再启用音频生产者；
已有音频历史时回退须保留音频解析器，不能通过丢弃回执恢复旧二进制。

回退至 0011 仅在无回执历史时允许。回退使用 READ COMMITTED，
取得 submit 表排他锁后重新计数；有记录、损坏记录或并发刚提交的回执均拒绝删除。
回执没有业务正文，内容保留/删除仍由原删除与对象生命周期裁决。

## 验证与后续接线

测试覆盖字段与字节上界、规范编码、原摘要/工作区、CAS、取消/期限、冲突和状态回显。
隔离 PG/InnoDB 运行相同登记逻辑，验证并发、回滚、当前 authority、独立仓库连接恢复和权限。
隔离 MinIO 与真实编解码接通原命令、结果写入和登记；VAD/识别仍为替身。
PG 升级/旧行保留及回退保护单独测试，不将 InnoDB 对照扩大为产品支持。
PG 确认丢失用例在真实事务提交后注入连接错误，验证只尝试一次、按原身份查询完整回执、
旧 CAS 拒绝、当前版本重放不再写入且预算不变；它只覆盖回执事务的确认丢失，
不证明引擎外发、对象 PUT 或 A 发布的完整恢复链。

下一步接真实引擎的持久接收、唯一执行和 submit/observe/abort/恢复。
执行前查原记录：已有回执就恢复原引用；没有回执也不能让两个 worker 同时重新推理。
须先证明唯一执行资格与确认丢失语义，不以进程内后台任务代替持久接收。
输入读取器同步 HEAD 的总期限缺口亦须处理或明确阻断，不能从结果期限外推安全性。

之后由 A 接发布、SDK 下载与 C 消费，联合验证取消/迟到、发布确认丢失、重启、
基础删除及两个逻辑节点竞争，再接 CustomVoice TTS。真实模型、停止/预算闭环、
物理多机及完整 B00–B07 资格仍开放；本单元不关闭 #142、父工作包或 Gate。
