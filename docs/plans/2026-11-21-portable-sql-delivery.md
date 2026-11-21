# 可移植数据契约：实施顺序与验收

本文件排列 [#1094](https://github.com/hrygo/SpeechNexus/issues/1094)–[#1102](https://github.com/hrygo/SpeechNexus/issues/1102)
的实施顺序，说明每张卡的交付物、验收证据和互相阻塞关系。设计冻结见
[ADR 0016](../decisions/0016-database-portability-core.md) 与
[可移植数据契约设计](../architecture/database-portability.md)；本文件只排施工顺序，不重复设计内容。

首发数据库明确是 PostgreSQL：全部验收先在 PG 上成立，非 PG 目标只在显式激活并通过同一 oracle
后声明支持。允许的数据库级差异只有三类——等价物理 DDL、已登记静态 DML profile、部署期驱动与 DSN；
领域、应用、事务、锁序、错误恢复与计量语义不得分叉。

## 1. 现状

| 编号 | 类型 | 状态 | 阻塞来源 |
| --- | --- | --- | --- |
| #1098 | PR（设计冻结） | 已合入 `main` | 无 |
| #1094 | 契约冻结 | PR #1103 | 无 |
| #1095 | claim bucket + outbox + `event_seq` | PR #1104 | #1094 |
| #1096 | 持久 grant + 锁后探针 + 幂等回收 | PR #1105 | #1095 |
| #1097 | 本地 credit + usage 区间 | 实施中 | #1096 |
| #1100 | PG 专用访问收敛 | 未开始 | #1094 |
| #1101 | 目标库资格（按激活顺序，首发只要求 PG） | 未开始 | #1094、#1100；另需目标 ODBC 驱动可用 |
| #1102 | bootstrap / 搬迁 / 切换 / 回退 | 未开始 | #1094、#1100、所激活目标的 #1101 |

现状事实：产品代码仍有 91 个模块导入 `psycopg`，SQL 文本含 `%s` 参数 1237 处，以及 `SKIP LOCKED`、
`ON CONFLICT`、`RETURNING`、`clock_timestamp()`、`statement_timeout`、advisory lock 与品牌 DDL。
这些是迁移输入，不是目标实现；`scripts/dev-env/portable-sql-baseline.json` 记录它们的现有规模。

## 2. 施工顺序

```text
#1098 设计冻结（已合入 main）
   |
   v
#1094 契约冻结 ──┬──> #1095 claim bucket + outbox + event_seq
   |             ├──> #1096 grant + 锁后探针 ──> #1097 credit + usage
   |             └──> #1100 PG 收敛 ──> #1101 目标库资格 ──> #1102 搬迁 / 切换 / 回退
   v
每个新表 / 新路径都直接写在契约之上，不再新增品牌 SQL
```

顺序理由：

1. **先有判据，再动代码。** #1094 建立三类 guard（共同子集、statement manifest、逻辑 schema 指纹）
   与 DB-API 契约后，#1095–#1100 才有统一的「什么算违规」和「什么算通过」。没有判据，#1100 的
   收敛无法证明是收敛。
2. **新代码先可移植。** #1095–#1097 的新表与新路径直接使用共同 SQL / `?` 参数并登记 statement，
   避免「先写 PG 版再改写」的二次成本。
3. **资格与搬迁最后。** #1101 需要 #1100 把产品路径全部收敛后才可能同一产物跑多库；#1102 需要
   所激活目标的 schema 才可能搬迁与切换。两者都不是 #1094 的阻塞项，也不作为首发门禁。

## 3. 每张卡的交付物与证据

| 编号 | 交付物 | 验收证据 |
| --- | --- | --- |
| #1094 | 三类 guard（共同子集棘轮、statement manifest 摘要、逻辑 schema 指纹）+ `--selftest`；DB-API 契约、权威时间探针、分页与类型边界；`schema_meta` + 物理 DDL profile；PG 边界 oracle | 每类漂移的注入式红灯；PG 上锁等待后专用探针仍新鲜；空串 / NULL / UTF-8 / `Decimal(38,0)` / `TIMESTAMP(6)` / `rowcount` 往返 |
| #1095 | `coordination_claim_buckets` + bucket fencing + Job/outbox version CAS + 每 Job `event_seq` + 持久轮询 | 两 worker 并发只有一个赢家；bucket 过期后旧 dispatcher 不能消费 stale 候选；通知全丢仍推进；逆序提交下 SSE 不缺事件 |
| #1096 | `admission_budget_grants` + 固定锁序准入 + 锁后探针 + `(session_id,epoch,renew_seq,meter_kind)` 幂等续租 + 一次回收 | 并发最后额度不超发；旧 epoch 更新 0 行；重放返回原身份；`commit()` 抛错仍保留占用与 unknown |
| #1097 | 网关本地 credit、边界 usage 区间、背压 / 有限 drain、崩溃证据 | credit 不变量；先续租后加 credit 的反例；精确重放返原区间、部分重叠拒绝；证据缺失进入 quarantine |
| #1100 | 全部产品 SQL 收敛到共同子集；品牌异常 / 时间 / 连接池移出业务层；大对象改对象引用 | guard 基线归零或仅剩已登记项；PG 既有行为回归通过；无运行期 DDL |
| #1101 | 已激活目标按同一 statement manifest 与物理 profile 运行 | 同一产物摘要；同一 oracle 逐行一致；驱动不合格的目标标记不支持 |
| #1102 | schema bootstrap、导出 / 校验 / 切换 / 回退工具与演练 | 切换窗口单一权威；源目标行数与摘要一致；空库 / 有数据 / 中断三类演练 |

## 4. 环境与证据位置

- 隔离服务：PostgreSQL 16.15（`127.0.0.1:55432`）、MySQL 8.4（`127.0.0.1:55434`）、
  MinIO（`127.0.0.1:59000`）。集成用例必须显式传入隔离 DSN，缺失即失败，不用 skip 代替。
- 证据目录：`docs/archive/audits/evidence/`，按日期分目录；不含凭据、完整日志与模型内部路径。
- 门禁：`tests/ci` 下的依赖自由用例随每次运行执行；新增 guard 必须同时给出「注入即红」的用例。
  三类 guard 都在 `ci.yml` 的 guards job 中先 `--selftest` 再判真实树。

## 5. 边界与阻塞

- #1101 的 Oracle 与 OceanBase 目标需要对应 ODBC 驱动；驱动不可用时该目标标记「不支持」，
  不添加品牌适配分支，也不宣称资格通过。
- 双机开发环境（#1087、#1088、#1092）是独立程序，验收依赖 MegaCube 上的运行集群与
  Docker daemon，与本文件的切片不共享证据。
