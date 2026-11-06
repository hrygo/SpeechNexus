# Public TTS Job 的固定 binding 前置链

本单元允许调用方固定文件 TTS 的 binding，保存公开原意图与有效配置投影。
它没有装配生产 planner，没有构造完整执行命令或完成完整配方的原子准入，
不能据此结算 #235 AC3。Workbench 已有渲染 Job 适配器，但当前
`PublicSpeechRenderJobs.submit` 未把固定的 `model_binding` 放入公开 Job 请求；
该消费者的 pin 传递与端到端验收仍须单独完成。

## 请求与权威选择

`POST /v1/nexus/jobs` 的 TTS 请求可带 `model_binding`，复用
[公开契约](../../contracts/speech-openapi.yaml)的 ResourceReference：

```json
{
  "kind": "tts",
  "model": "narrator-tts",
  "model_binding": {"id": "binding-narrator", "revision": "2"},
  "input": {"text": "synthetic pin"},
  "voice": {"id": "voice-demo", "revision": "1"}
}
```

revision 是 1–19 位 ASCII 正十进制字符串，不转 JS number。model 必须匹配
binding 的 logical_model；binding ID 可以不同名，并且必须支持 tts_file。
服务端精确联接 publication 与 revision 的 `(binding_id, revision, revision_id)`。
JSON 中也携带身份时必须与这些权威列一致，生命周期不能从草稿 JSON 推断。

新请求只接受 `speech_publication_heads` 精确选择且未退役的版本。
head 可以由 2 回切到仍有效的 1，此时 pin 1 可接受、pin 2 拒绝。
head 清空时两个 pin 都拒绝；不回退到最大已发布版本，也不自动更换 pin。
无 pin 请求仍以 model 为 binding object ID，消费当前未退役的权威 head。

响应 `effective_configuration.models.tts` 等于原 pin；内部 revision_id 不公开。
原请求保存在现有私有 admission_operations 中，不能把它当作完整有效执行命令。

| HTTP / code | 含义 |
| --- | --- |
| 422 model_binding_not_published | 无精确 publication/revision 记录 |
| 422 model_binding_retired | 精确版本已退役 |
| 422 model_binding_not_current | 精确版本未被 head 选择，包括 head 清空 |
| 422 model_binding_mismatch | logical_model 与 model 不符 |
| 422 model_binding_task_not_supported | 不支持 tts_file |
| 422 unsupported_parameter | schema 拒绝非法引用或非 TTS pin |
| 409 idempotency_conflict | 同 workspace/key 的规范意图不同，包括变更或移除 pin |
| 503 job_outcome_unknown | 原结果未能确认；保存原请求/key，查询或原身份恢复 |
| 503 job_unavailable | 事实或存储不可用；不证明此前提交不存在；`retryable: false` |

## 同键保护与锁序

应用先查询原 workspace/key；同意图返回原 Job 及完整 submission，不受后来
head、retirement、workspace 或其他可变事实影响。不同意图冲突。
首查为空后，对可变事实准备返回拒绝时，再执行一次同键协调；查询失败转 unknown，
不能用事实拒绝覆盖在途或已提交结果。

生产 find_by_key 与所有带 key 的 create 共用 schema 内事务 advisory lock，
锁身份为无歧义 JSON 元组 `[workspace_id, key_digest]`，不含 pin、kind 或意图摘要。
取得锁后在 READ COMMITTED 中再读，等待先行写者提交或回滚后才判断 absence。
锁内 absence 是拒绝协调相对于同协议 keyed create 的序列化点，不持久占用拒绝键。

本子链顺序为 **public Job key → 共享 lock_binding → 既有插入约束**。
先重放原结果，再取 binding 锁，锁后重查同键，随后精确验证 head、retirement、
revision_id、logical_model、任务及原请求/公开配置的一致性。锁持有到事务结束。
无 pin 创建保留 main 的锁后 head 复核；keyless 请求从 binding 锁开始，不承诺同键恢复。

生命周期/发布路径不反取 public Job key；拒绝协调不持 binding 锁或在锁内开 facts 连接。
资源锁和 [全局协调锁序](../architecture/coordination-transactions.md#4-锁序隔离与数据库适配)
保持现状，本路径不获取 pool/quota/host/generation，不代表完整准入。
混跑不遵循新 key 锁的旧写者不具备此保证；将来完整准入必须按其全局锁序在唯一事务中整合。

## 事务期限与未知提交

每个 adapter 事务从 pool acquisition 前建立单一 monotonic deadline。
连接排队/配置、key/catalogue/唯一键等待、SQL、fetch、控制语句、有界重试及提交前检查
消耗同一预算。每条 SQL 前按剩余时间设置 statement_timeout，控制往返与 SQL/fetch 后
再次检查；最后一次操作或提交前控制语句耗尽预算时抛错并退出事务回滚。
唯一冲突只在确认回滚后重试一次，不重设 deadline。

这不是整个 HTTP 请求的统一时限，也不是同步网络 I/O 的硬中断保证。
更新 statement_timeout 的控制语句在前一超时设置下执行；其耗时会被后检查计入，
但不能完全消除单次往返的超期。首次 BEGIN/控制往返、网络卡顿、ROLLBACK 或 COMMIT
不能由 Python 时钟保证即时中断。没有使用后台线程取消并声称服务端回滚。

提交前仍有预算才交给驱动 COMMIT；COMMIT 开始后的数据库异常不进入普通唯一冲突重试。
提交回执超期或丢失返回 unknown，可能已经提交，恢复仍查询原 workspace/key/digest，
不能换 key、换 pin、重建命令或释放未知预算。keyless 调用无法获得这项恢复保证。

## 兼容与验证边界

公开源从 0.1.0.dev0 升为 0.1.0.dev1；TTS 仅加可选字段，其他 kind 的必填常量
仍保持互斥，旧闭合请求仍有效，OpenAI 标准子集未变。生成消费者从 canonical 源重建。
旧 strict 服务拒绝 pin 字段，应先升级服务和生成包再启用客户端；失败时不能删 pin 降级。
无 DDL、权限扩展、回填或生产配置变更。

DEL-356 原候选的完整 Speech HTTP 结构门禁保守拒绝 oneOf 分支变化（Make exit 2）。
DEL-377 为[封闭互斥请求](../validation/http-contract-change-checks.md#narrow-request-proof-for-closed-tagged-objects)
增加通用、仅请求方向的可选属性扩展证明；以同一固定 base 重跑原完整门禁已通过。
独立复审发现该证明未核对有效方言，Draft 7 会忽略父层关闭约束并造成漏检。
返修在证明入口核对两侧原始文档及已加载引用文档的方言声明，未知或缺失上下文继续保守拒绝；
回归覆盖根默认、schema 覆盖、引用目标及祖先方言，不改变公开协议。
DEL-393 进一步修复外部裸 schema 中普通 `openapi` 注释造成的文档身份误判：
只有通过本地 OAS 3.1 文档元 schema 的资源才可采用其默认方言，
其余资源仍需显式受支持的 `$schema`；真实文件正反控不依赖扩展名或路径白名单。
这不替代语义或产品验收；独立兼容审查和非作者 Sol 仍须针对固定最终候选另行完成。

真实 PG 测试见
[test_public_job_binding_pin_pg.py](../../tests/integration/test_public_job_binding_pin_pg.py)。
它在任务专用 PostgreSQL 的新 schema/限制角色中验证独立连接锁等待、回切/清空/退役、
同键原结果优先与冲突、事实拒绝协调、SQL 累计耗时、pool 耗尽及真实提交后注入丢回执。
后者是注入丢回执，不是实际断网或 HA。可控时钟单元测试补充控制语句/末次读取/提交前
耗尽、重试总预算和迟到提交回执；这些单元测试不是数据库并发证据。

交接给唯一 SM+PO 安排非作者审查。双库协调、真实网络/进程故障、完整 command/lease/
quota/fencing、正式 Workbench 接入、gateway/引擎、物理多机和产品 Gate 均另行验收。
planner 继续 fail-closed；本候选不授权部署。
