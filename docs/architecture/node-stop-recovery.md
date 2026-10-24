---
title: "节点停止检查点与进程崩溃恢复"
status: active
version: "0.1.0"
date: 2026-10-24
---

# 节点停止检查点与进程崩溃恢复

控制器删除容器后、保存最终回执前可能崩溃。仅有“容器不存在”无法证明它退出过，旧流程因此会
永久保留未知占用。本单元在节点私有 SQLite 中保留停止检查点，恢复已接受的原 stop，
使退出事实在删除前落盘。它不拥有调度预算，不向共享 PostgreSQL 添加表、函数或触发器。

## 契约与所有权

内部 HTTP 仍遵循 [runtime-control](../../contracts/internal/runtime-control.openapi.yaml)，
请求、响应、错误码和同键重放没有新增字段。节点私有格式由
[stop-checkpoint.schema.json](../../node-controller/contracts/stop-checkpoint.schema.json) 定义，
`request` 引用原 HTTP 请求 schema，并额外要求 `action=stop`，不复制身份字段定义。
该格式由节点包维护，正负契约测试进入 CI；不修改 A 的共享生成器、生成产物或预算表。

`StopCheckpointStore` 独立于 `IntentStore`，只提供按原请求读取、保存单调检查点两个端口。
生产 Docker adapter 必须显式注入它；组合根把同一个本地 `SqliteIntentStore` 装配到两种端口。
内存实现仅用于测试，CPU 探针也使用真正的 SQLite。

每个 `intent_id` 只允许一个不可变完整原请求和固定容器 ID：

| 字段 | 语义 |
| --- | --- |
| `format_version=1` | 私有记录格式，与本地数据库 schema 版本分开 |
| `request` | 完整原 stop；身份、预算、fencing 和 deadline 均沿用原协议 |
| `container_id` | daemon 返回的固定容器身份，不能改为替代实例 |
| `exit_code`、`exited_at` | 共同为空或共同存在，表示固定容器已经退出 |
| `released_at` | 已提交退出事实且确认删除后才可存在，生成稳定 `verified_at` |

记录拒绝未知字段、重复 JSON 字段、错误版本、无时区时间、布尔退出码和缺半边的证明。
相同身份重复写保留第一次确认时间；不同请求、目标或退出码不能覆盖原事实。

## 执行顺序与恢复

| 持久阶段 | 已有事实 | 下一步 |
| --- | --- | --- |
| `targeted` | 原 stop 已持久接受，目标已核对并固定 | 停止同一容器、wait，并重新 inspect 核对退出码 |
| `exited` | 退出事实已经同步提交 | 删除原容器，再确认其缺失，且该 generation 没有替代容器 |
| `released` | 退出、删除确认均已同步提交 | 以原 `released_at` 生成证明，CAS 更新最终意图回执 |

阶段由字段推导，不另存一份可漂移的枚举。每个检查点是独立短事务；stop、wait、
inspect、delete 均在事务外。首次必须先写 target；不能跳过已提交 exit 直接写 release。
提交失败会尝试回滚，调用方拿不到成功检查点，也不能进入下一步破坏性操作。

节点循环区分 prepare 与 stop：prepare 继续 inspect；stop 调用 `recover_stop`，恢复已经
持久接受的原请求。它可能重试固定目标的 Docker stop/wait/delete，不创建新 stop、不重放
prepare、不外发业务执行。GET 和同键 POST 仍只读持久回执，不驱动容器动作。

- 停止前崩溃：有 target 时只操作原容器；首次认领未完成而容器仍在时，先核对并持久固定目标。
- 退出后、写 exit 前崩溃：容器未删除，重新 wait/inspect 取证，再提交 exit。
- exit 后、删除前崩溃：读取原退出事实，确认容器未被重新启动，再继续删除。
- 删除后、写 release 前崩溃：必须已有持久 exit，再确认原容器及该 generation 缺失，补 release。
- release 后、最终回执前崩溃：复用原证明时间；回执 CAS 失败保留竞争者状态，下轮重新读取。

没有持久 exit 而容器消失时仍无证明。同 generation 换容器、固定容器重新运行、退出码或身份
矛盾时不能删除替代目标或生成成功证明。另一 generation 的容器不能被原 stop 误停。
撤销模板、节点不符、存储或 daemon 不可用继续保持未决/隔离；TTL、取消和丢响应均不是释放证据。
本地历史中的 deadline 已过不等于一个已接受 stop 被撤销，也不能替代物理证明。

## 存储升级与回退

SQLite schema 从 1 原子升级到 2，新增 `runtime_stop_checkpoints(intent_id, record)`；
原意图 JSON、版本、终态与 HTTP 形状保持不变。外键绑定原意图，保存时再次核对完整原请求。
未知 schema 拒绝启动，升级失败回滚。旧控制器只支持 schema 1，会拒绝 schema 2，
不能通过改版本号、清空回执或删除检查点强行回退。

升级前在控制器停机后保存一致本地快照。回退代码时使用匹配快照和版本，或继续使用能够读取
schema 2 的代码；回退不能伪造已经丢失的证明，未知占用仍由 A 保留。
本地格式与 A 的共享数据库迁移分开，没有引入数据库专有协调能力。

## 验证与当前范围

- 机器契约正负例覆盖三个阶段、闭合原请求、版本、时间与部分证明。
- 真正 SQLite 覆盖重开、两个连接、阶段不可跳越、身份/退出事实不可覆盖、提交失败与 v1 升级。
- 原生应用与 Docker adapter 在 fake daemon 上覆盖五个崩溃点、存储失败、CAS 竞争、
  无 exit 的消失、替代 generation、同 generation 替代容器及固定容器被重启。
- 原生 app/SQLite/TLS 与 PG/InnoDB 验证：只有 exit 时占用不退，重启恢复双证据后由 A 事务归还。
  daemon/资格为合成事实，不证明真实 GPU 已停止或模型资格。
- [真实 CPU 进程探针](../audits/2026-10-24-b02-stop-recovery-probe.md)使用生产入口、真实 daemon、
  SQLite 和 SIGKILL；故障闸只在探针子进程包装原方法，生产没有故障开关。

仍待：真实 ASR/TTS 的同类故障资格、ready 实例持续监控、可信整实例事实接网关、网关恢复周期驱动、
完整执行/lease/预算联合闭环和物理多主机。外部 reconcile 调用方证明、节点级 fencing 作用域仍是
原有缺口，本地检查点不能替该入口背书。
