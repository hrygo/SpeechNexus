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

## 6. #1100 收敛路径

基线按「规则 × 文件」计数（`scripts/dev-env/portable-sql-baseline.json`），当前 1072 条命中落在
481 个对里。收敛按可验证的切片推进，每片自带「注入即红」与真实 PG/InnoDB 证据：

1. **标记与转写**（已做）：整句与片段的参数标记迁到 `?`，`::` 换成 `CAST`。
2. **分页与存在性**（已做）：读路径去掉 `LIMIT`，页大小交给 `fetchmany`；「有没有行」用 `EXISTS`；
   零行探针用恒假谓词，收敛到 `_ZERO_ROWS`、`_any_asset`、`_has_unreleased_references`。
3. **连接池与异常**（已做）：`Pool` / `ExecutableConnection` 契约、`portable_error` 单一翻译点、
   `create_pool` 把池失败翻成 `PoolUnavailableError`、`PortableCursor` 把元组行包成 `Row`。
4. **适配器迁移**（进行中）：一次一个模块，把 `psycopg` / `psycopg_pool` 的导入与 dict 行访问换成
   契约类型。每迁移一个模块必须同时更新它的测试替身，并保留该模块原有的失败语义
   （尤其「提交未知」与「冲突后重读」不得因为异常类别变粗而退化）。
5. **语句与建表**：`sql-string-building`、`runtime-ddl`、`vendor-type` 三族需要把 SQL 文本收进
   受审查的 statement 模块、把建表移回迁移，属结构改造，排在适配器迁移之后。

单模块迁移的检查表：

- 导入只剩 `speechnexus_portable_sql` 的契约类型；不再出现 `psycopg`、`psycopg_pool`。
- 行访问改成 `Row` 的按名 / 按位读取，列名大小写不再被依赖。
- 异常按 `StorageError` / `ConflictError` / `ConnectionLostError` / `PoolUnavailableError` 分类；
  任何「按厂商错误码分支」的地方改成重读状态。
- 该模块的单测替身与集成用例同时更新，基线 `new=0` 且该文件从基线消失。

## 7. 合并顺序（合并已获授权）

用户明确授权按仓库规则合并（「draft 太多了，允许按照规则合并」）。以下顺序按「一张卡一个边界清晰的 PR」执行，前一张合入后再动后一张：

| 顺序 | PR | 卡 | 合入判断 |
| --- | --- | --- | --- |
| 1 | #1103 `feat/portable-sql-contract` | #1094 契约冻结 | CI 全绿 + issue 正文同步 |
| 2 | #1104 `feat/claim-bucket-event-seq` | #1095 claim bucket + event_seq | 同上 |
| 3 | #1105 `feat/admission-budget-grant` | #1096 持久 grant + 锁后探针 | 同上 |
| 4 | #1106 `feat/realtime-credit-usage` | #1097 credit + usage 区间 | 同上 |

#1100 的收敛 PR（#1108 driver foundation、#1109 marker pilot、#1110 lexer、#1111 mixed-marker guard、#1112 bulk migration、#1113 casts/paging/pool）**不逐个合并**：它们是同一张卡的分段推进，逐个合入只会把 6 个 draft 变成 6 次主干提交。前四张合入后，把这六个分支压成一个 `#1100 收敛存量 PG 专用 SQL` 的 PR 再合。

合并后动作：
1. 以 `origin/main` 为基重建 #1100 分支（`git rebase --onto origin/main <#1106 head> <#1100 head>`），避免把已合入的四个分支再带一遍；
2. 重跑 `check-portable-sql.py --baseline` 与集成 shard，确认基线数字对应新主干；
3. 关闭对应的 draft PR 并删除远端分支（删除远端分支需单独授权）。

### 当前阻塞

GitHub 凭据在本轮失效：`gh auth status` 报 `The token in default is invalid`，`~/.config/gh/hosts.yml` 无 `oauth_token`，keychain 取不到凭据，`git push` 报 `could not read Username`，日志下载接口返 403。合表、推送、issue 同步都需要先 `gh auth login -h github.com` 恢复。恢复前本地已完成但未推送的提交见 `git log origin/feat/portable-sql-casts..HEAD`。

### 时钟收敛的界外项：`readiness.py`

两次尝试给 `readiness.py` 的债务探针换掉内联时钟都撤回了,原因不是行为,而是**它还在用 `%(name)s` 参数标记**:

- 时钟可以直接换(`now_statement()`),但「现在」要进语句就得作为参数,而该模块的参数风格是 `%(name)s`。每加一个 `%(now_us)s` 就是一处新的 `pyformat` 命中;
- 守卫按 (规则, 文件) 计数比较,`new=1` 立刻判红:基线从 954 变成「新增 pyformat」。这不是可以写进基线的变化,基线的意义就是不让标记漂移;
- 把 `LIMIT %(limit)s` 一起改成分页还要动 `fetchmany`,而这属于异步驱动的标记迁移(见 §6)。

结论:`readiness.py` 的时钟、标记与分页必须在**同一次**迁移里改完,单独换时钟会让守卫指标退步。在异步驱动迁移落地前,这两个 `vendor-time` 命中保留在基线里,属于已知的、有归类的未覆盖项,不是「已经支持」。
