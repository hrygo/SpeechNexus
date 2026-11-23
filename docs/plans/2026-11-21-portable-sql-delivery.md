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

## 8. 剩余基线的归类与依赖（基线 950 处 / 405 个 (规则,文件) 对）

逐族核对后的结论：**残余基线不再是「机械替换」的形状**，每一族都卡在一个具体决定或一次协调重构上。按依赖归类如下。

| 规则 | 处数 | 卡在哪里 |
| --- | ---: | --- |
| `sql-string-building` | 327 | 语句选择与文本拼装。需要按语句族重写成常量语句 + 派发表，不能逐文件替换 |
| `runtime-ddl` | 165 | schema bootstrap 的运行时建表；归 #1102 的 bootstrap 设计 |
| `vendor-sdk` | 132 | 品牌驱动导入；仓库边界（`ContractCursor`/`Pool`）已就绪但只接了 2 个消费者，需要仓库边界重构 |
| `vendor-type` | 88 | `TIMESTAMPTZ`/`INTERVAL`/`TEXT`/`BOOLEAN`/`UUID`/`ARRAY`；逻辑类型映射归 #1102 的物理 profile |
| `ddl-identifier-length` | 38 | 标识符长度与命名规则，随物理 profile |
| `advisory-lock` | 33 | 需要协调原语替代方案（与 #1095 bucket fencing 同一族决定） |
| `vendor-json` | 33 | `->`/`->>`；列式短字符串 + 对象存储的读法，随 #1102 |
| `upsert` | 21 | `ON CONFLICT` → 显式「先读后写 + CAS」，需按语句确认幂等语义 |
| `vendor-operator` | 19 | `||`、`@@` 等；随语句族重写 |
| `paging-limit` | 16 | 全是 `LIMIT 1` 的探测/取值，但**多重性未定**：`coordination_jobs` 只保证 `UNIQUE(operation_id, attempt_id)`，`WHERE operation_id = ?` 可能命中多次重试。去掉 `LIMIT` 前必须先定「取哪一次」的确定性排序 |
| `returning` | 15 | `RETURNING` → 写后按主键读回，同一事务内 |
| `cte` | 14 | `WITH` → 拆成多条语句或派生表 |
| `vendor-error-branch` | 13 | 按厂商 SQLSTATE 分支；需标准化的错误分类点（`contract.portable_error` 已存在） |
| `vendor-function` / `vendor-cast` / `vendor-time` / `pyformat` | 21 | 各自剩余个位数；`readiness.py` 的 4 处必须与标记、分页同批迁移（见 §7） |
| `session-tuning` / `vendor-lock` / `ddl-type` / `ddl-default` / `aggregate-filter` / `distinct-on` / `format` / `skip-locked` / `named` | 15 | 零星构造，随所属语句族一起改 |

**可核验的结论**：#1100 的「基线归零」在本轮证据下不可能只靠替换完成——`runtime-ddl`(165)、`vendor-type`(88)、`vendor-json`(33)、`ddl-identifier-length`(38)、`ddl-type`/`ddl-default`(3) 共 327 处直接依赖 #1102 的物理 profile 与逻辑类型映射；`vendor-sdk`(132) 依赖仓库边界重构；`advisory-lock`(33) 与 #1095 的协调原语决定同族。这三块合计 492 处，占基线的一半以上。

因此 #1100 的验收判据应表述为：**可在 #1101/#1102 之前收敛的部分归零，剩余部分逐条登记为「已归类的未覆盖项」，并给出归属卡与解除条件**；把「基线归零」写成 #1100 单独的完成条件会让这张卡与 #1102 互相等待，永远不能先绿。

### 8.1 三个「看着机械、实际是决定」的取样

**派发队列的领取语句**（`job_dispatch_postgres`）一条语句里叠了 `WITH`、`FOR UPDATE OF c SKIP LOCKED`、
`LIMIT %s`、`UPDATE ... RETURNING`、`ANY(?)` 与 `MAX(...) FILTER`。核对过 #1095 的分支
（`feat/claim-bucket-event-seq`）后确认：那条 `SKIP LOCKED` 在该分支上同样存在，说明 #1095 的
claim bucket 改的是准入侧的领取，**没有覆盖派发队列**。因此这里的可移植重写必须沿用 #1095
的 bucket fencing 决定，而不是另起一套「先读后写 + CAS」——两套领取语义并存会比一条
`SKIP LOCKED` 更难维护。

**出版物单例探测**（`publications_postgres`）用 `LIMIT 2` 表达「这个单例表是否被写坏」：
超过一行就报不可用。可移植写法要把它拆成三条各自 `fetchone()` 的查询（连读两次才能发现重复），
把「有界」从 SQL 挪到应用。代价是热门读路径从 1 次往返变 3 次，收益是去掉 `LIMIT`。
这是**代价取舍**，不是替换。

**资源池读模型**（`resource_pools_postgres`）用 `array_agg(DISTINCT ... ORDER BY ...)` 与
`count(*) FILTER (WHERE ...)` 一次算完。按设计的「列式短字符串」约定，`node_ids` 应当是写入期
就定型的定界字符串，聚合则要在应用侧分组——这改的是读模型的形状，随 #1102 的类型映射。

### 8.2 需要用户裁决的两条路

1. **接受「残余基线登记为已归类未覆盖项」**（见 §8 结尾的口径改写）。#1100 可在 #1101/#1102
   之前先绿，剩余部分按族挂着，解除条件写在案。
2. **授权我替这些跨卡决定拍板**：派发队列领取沿用 #1095 bucket、单例探测拆成三次有界读、
   `node_ids` 改成写入期定界字符串。这三条都会动协调语义或读模型，超出「收敛存量 SQL」的范围，
   没有授权我不做。

### 8.3 判据自检：`ruff check --fix` 不能替代末次 `ruff check`

`assets_postgres` 的 `try/except/pass` 在本地全绿、在 CI 判红(SIM105 → `contextlib.suppress`)。
事由不是版本差异:本地与 CI 都是 ruff 0.17.2。真正的原因是本地的判据顺序用了
`ruff check --fix .` 之后只读 mypy / pytest / 守卫的输出,**没有再跑一次不带 `--fix` 的
`ruff check .`**;SIM105 的修复被标为 unsafe fix,`--fix` 不应用它,而它的告警又被我跳过了。

因此本地判据的固定顺序是:先 `ruff format`,再 `ruff check --fix`,最后**必须**跑
`ruff check .` 与 `ruff format --check .` 并读取其输出。只有 `--fix` 跑过不算通过。

### 8.4 `upsert` 不能一律替换:两种形态只解了一种

`ON CONFLICT` 的 21 处只有两种形态,而它们的可移植写法不同。

**锁锚点/家族根**(可以替换)。命令行已经选出唯一赢家,只有赢家会写入家族根,所以「已存在就跳过」
不需要处理竞争。`asr_settlement` 的根也一样:同一 lease 的两次准入在本事务里已经串行化
(既有操作在 `admission_operations` 上取 `FOR UPDATE`,新操作先撞操作主键)。这些改成
`INSERT ... SELECT ... WHERE NOT EXISTS` 是等价的,已替换 5 处,守卫 949 → 944。

**唯一赢家**(还不能替换)。`speech_binding_commands`、`speech_profile_commands`、
`speech_pronunciation_commands` 这类先 `INSERT ... ON CONFLICT (key_digest) DO NOTHING`
再用 `rowcount` 判「我是不是赢家」的写法,是幂等重放的**仲裁原语**:N 个并发同命令里恰好一个
拿到 `rowcount=1`,其余读到已存在行并返回原结果。换成 `WHERE NOT EXISTS` 后两个事务可以同时
通过谓词,唯一索引拒掉后到的那个 —— 如果不捕获,落败者拿到的是硬错误而不是「重放」,
幂等语义就丢了。

捕获需要驱动把唯一约束冲突翻译成契约错误(`portable_error` / `ConflictError` 已有,但
`ContractCursor` 还没接到这些模块)。在驱动接入前替换这 5 处,等于把一次可容忍的竞争
变成用户可见的 500。因此它们保留在基线里,解除条件写明:驱动适配点接入后一并替换。

### 8.5 `RETURNING` 的两类,与一处被守卫拦下的替换

`RETURNING` 的 15 处分成两类。

**自证命中**(已替换 9 处)。写语句本来就带条件谓词,`rowcount` 已经是 CAS 判据,读回只是为了
把写后的值取出来:工作区创建/更新、TTS 中止原因、发音草稿改名、公开 Job 取消与确认、
单条草稿修订删除。这些改成「`rowcount != 1` 判命中 + 同一事务内按主键读回」,语义不变,
而且把「命中」与「取值」两件事分开了 —— 原来的 `RETURNING` 用一个动作同时承担两件事。

公开 Job 的取消语句还顺手去掉了 `WITH clock AS (SELECT CAST(? AS BIGINT) AS now_us) ... FROM clock`:
`now_us` 本来就是参数,`RETURNING clock.now_us` 返回的就是刚传进去的那个数,不需要再取一次。

**与协调原语耦合**(保留)。派发队列领取的 `WITH ... FOR UPDATE SKIP LOCKED ... RETURNING`、
取消对账的 `WITH ... UPDATE ... RETURNING job_id, attempts, next_check_at_us`,以及删除账本
`claim_purge`/`release_purge` 的 `RETURNING <列清单>`。前两者与 #1095 的 bucket fencing 同族;
后者是列清单用 `f"..." + _LEDGER_ROW_COLUMNS` 拼出来的,换掉要新增两处拼接式 SQL 文本,
守卫实测 `NEW sql-string-building (2)` 直接判红。

**这就是那条被守卫拦下的替换**:我先换了 `claim_purge`/`release_purge`,守卫判红,于是撤回,
并把原因写在代码注释里 —— 列清单先冻结成字面量常量,这三处才能一起换。守卫拦住的是我制造
新违规,不是它误报。

### 8.6 错误分类:SQLSTATE 只在适配点被读一次

`vendor-error-branch` 的 13 处原本分散在三层:领域(`job_prerequisites` 按 `sqlstate` 分类)、
基础设施(`admission_postgres` 把 SQLSTATE 写进日志、`postgres_versions` 把
`error.sqlstate` 塞进 `VersionStoreError`)、应用(`version_ports` 把它当诊断字段透传)。

收敛后的形状:

- `contract.StorageError` 带唯一诊断维度 `reason ∈ {unavailable, schema_missing, permission_denied}`。
- `psycopg_driver._reason_of` 是**唯一**读 SQLSTATE 的地方(`42P01`/`3F000` → `schema_missing`,
  `42501` → `permission_denied`,其余 `unavailable`)。
- 领域只读 `reason`;存储实现在抛自己的拒绝之前先过一遍 `portable_error`,把驱动失败翻成契约类别。

**注入即红**:`test_a_vendor_sqlstate_alone_does_not_reach_the_domain_any_more` 造一个只带
`sqlstate="42P01"`、没有 `reason` 的异常,断言分类给不出结论。把领域改回读 `sqlstate` 时这条
立刻变红(实测过),所以它盯的是「领域不再碰厂商码」这件事本身,不是某次分类结果。

workbench 版本边界上剩下的 3 处在 §8.79 按同一形状收口:应用层不再声明 `sqlstate`,
基础设施只按契约类别判定。

顺带修了一个真实缺陷:存储原来用 `raise ... from None` 抛出,链被切断,分类拿不到任何事实
——这也是为什么三个 PG oracle 用例(`test_public_job_prerequisites_pg`)在改成读 `reason` 后
一开始是红的:它们真的在未迁移库/被撤权角色上跑,暴露出翻译点没接上。

### 8.7 `paging-limit` 16 处:先分「存在性」与「取哪一行」

| 站点 | 形态 | 处置 |
| --- | --- | --- |
| `model_catalog_runtime` | 有没有 `SECURITY DEFINER` 函数 | 删 `LIMIT`,只 `fetchone()` |
| `asr_settlement_preflight` | 有没有「持有租约却没结算根」的行 | 删 `LIMIT`,`fetchone()` 判空 |
| `output_interval_sql` | 区间是否重叠 | 删 `LIMIT`,`fetchall()` 判空 |
| `public_job_admission._instance` | 取一台就绪实例 | 删 `LIMIT`,`ORDER BY` 已确定第一行 |
| `admission_sql`(2) | `WHERE operation_id=?` 取 attempt/job | **未动**:`coordination_jobs` 只保证 `UNIQUE(operation_id, attempt_id)`,同一操作有多条重试,取哪条没有定义 |
| `publications_postgres` | 单例表 `LIMIT 2` 做写坏检测 | **未动**:可移植写法要拆成三条各自 `fetchone()` 的读,热门路径 1 次往返变 3 次,是取舍不是替换 |
| `postgres_meetings` | 收据按 `(meeting_id, minutes_id, operation)` 取一条 | **未动**:主键是 `(workspace_id, operation, scope_id, key_digest)`,这条谓词落在非唯一索引上,多条时可返回任意一条 |
| `postgres_versions` | `ORDER BY erased DESC LIMIT 1` | **未动**:并列时的取舍是业务判断 |
| `voice_evidence_worker`、`admission_recovery_sql`(65)、`postgres_render_units`(2)、`workbench/runtime`、`asr_settlement_debt` | 批量取行或相关子查询取最新一条 | **未动**:批量要 `fetchmany`、相关子查询要去掉后被 N+1 取代,各自需要单独验证 |

**这一处改动暴露了一个真判据**:删掉 `public_job_admission` 的 `LIMIT 1` 后,
`test_public_job_admission_readiness_pg` 立刻转红 —— 原查询后面那行 `if len(rows) != 1:
raise ... job_unavailable` 在 `LIMIT 1` 下是死代码,去掉 `LIMIT` 后变成「一个 binding 有多台
就绪实例就拒绝」。而多台就绪实例(健康兄弟)是正常状态。改成「按 `(instance_id,
instance_generation)` 排序取第一行」,既保持确定性,又不再把正常拓扑当故障。

### 8.8 `paging-limit` 第二批:标量子查询与批量上界

- `voice_evidence_worker`:同一身份的外来授权存在性探针。删 `LIMIT`,由 `fetchone()` 只取一行。
- `workbench/runtime`:标量子查询取「经由哪个可达角色」的名字。`SELECT rt.name ... ORDER BY rt.name
  LIMIT 1` 与 `SELECT MIN(rt.name) ...` 等价(同一排序规则下取最小),标量子查询因此仍只返回一行。

**批量上界这一支被证明不是本地改动**:把 `RuntimeCursor` 协议补上 `fetchmany` 后,mypy 立刻列出
四个游标包装与三处测试替身都要跟着实现 —— 这与仓库边界重构是同一件事,不属于本卡。所以
`admission_recovery_sql` 的 `LIMIT 65` 保持原样,理由写在这里而不是留给下一次猜。

**一次差点落地的语法错误**:把说明写进 SQL 所在的 Python 三引号字符串里(`#` 行),而 SQL 的注释
是 `--`。守卫的计数没有下降反而暴露了它 —— 那条 `LIMIT 1` 仍被算在字符串里。这类错误 ruff、mypy
都看不见,只有「守卫计数为什么没降」这个问题能发现。注释已移到字符串外。

### 8.9 `vendor-cast` 清零:DDL 里的 `::` 也要换

5 处 `::` 全在 DDL 的 `CHECK` 约束里(`revision::numeric`、`octet_length(x::text)`),不是在 DML 语句里。
换成 `CAST(... AS ...)` 之前先确认了一件事:逻辑 schema 的指纹只比对表/列结构,不比对表达式文本
——`check-logical-schema.py` 在改动前后都是 `tables=10 revision=0005 fingerprint=d99bfdf39af6`。

这意味着换写法不会让「旧库」与「按新代码建的库」产生指纹差异,但**已部署库里约束的表达式文本仍是
`::`**。两者语义相同,`CREATE TABLE IF NOT EXISTS` 也不会重写既有约束,所以没有迁移动作;
记录在这里,以免以后有人把「指纹一致」误读成「库里的约束文本也一致」。

`vendor-cast` 由此归零。

### 8.10 `vendor-function`:把 `extract(epoch ...)` 移到应用侧

3 处把 `timestamptz` 换成 epoch 微秒的 `CAST(floor(extract(epoch FROM x)*1000000) AS bigint)`
改成「读回时间列 + 应用侧换算」:

| 模块 | 读回列 | 换算点 |
| --- | --- | --- |
| `operator_attested_qualification` | `s.expires_at` | `to_epoch_us(source["expires_at"])` |
| `builtin_voice_qualification` | `a.expires_at` | `to_epoch_us(rows[0]["expires_at"])` |
| `qualification_provenance` | `a.expires_at` | `to_epoch_us(row["approval_expires_at"])`,空值保持 `None` |

换算只有一个定义点:`speechnexus_portable_sql.clock.to_epoch_us`,它已经在(时钟路径用它把
`datetime` 换成微秒),这次只是把它用到读回的数据上,不新增第二套换算。

`vendor-function` 余 4 处:`readiness.py` 的 3 处是时钟(与标记、分页同批),`asset_schema.py`
的 1 处是 DDL 的 `DEFAULT`,属于物理 DDL profile,不随 DML 一起迁。

### 8.11 `= ANY(array)` 在固定小集合上换成定元 `IN`

`pronunciation_runtime` 的 6 处权限探针用 `c.relname = ANY(CAST(? AS text[]))` 传一个表名数组。
可移植写法是定元 `IN (?, ?, ?, ...)`,前提是集合大小是常量 —— 这里三个元组都是模块常量:

| 集合 | 元数 | 语句 |
| --- | --- | --- |
| `_READER_TABLES` | 3 | 读身份:非本集合有 SELECT、本集合有写权限都算越权 |
| `_WRITER_TABLES` | 3 | 写身份:本集合的列写权限逐列比对 |
| `_PUBLISHER_TABLES` | 6 | 发布身份:读身份的表 + 追加 + 选定的头 |

元数与 SQL 里的占位符个数是同一件事,所以每个语句执行前过 `_require_arity(names, size)`。

**这个断言当轮就抓到我一次错**:我先按「三个元组都是 3 个名字」把 `IN (?, ?, ?)` 套给了全部
6 处,`_PUBLISHER_TABLES` 实际是 **6** 个名字。测试先红(`the fixed IN list ... holds exactly
three names`),随后真实 PG 用例 21 条全红 —— 参数个数与占位符不匹配,探针把「参数错位」读成了
「越权」。定元 `IN` 的代价就是这个:元数写死在 SQL 里,必须有一处断言盯着。

### 8.12 变长 `IN` 的展开原语:`ListMarker`

`= ANY(array)` 在受限方言里没有替代,而 `IN (?, ?, ?)` 的元数写死。运行期才知道大小的集合
(清理批次、按 id 集合读)因此需要一个「一个 `?` 绑一列值」的标记:

```python
cursor.execute("... WHERE id IN (?) AND state = ?", (ListMarker(ids), "ok"))
# 执行前展开成:... WHERE id IN (?, ?, ?) AND state = ?  / 参数 (1, 2, 3, "ok")
```

为什么放在 `packages/portable-sql`:`?` 是这个包拥有的标记语法,展开位置也必须由它算(字符串字面量
里的 `?` 不参与)。业务侧自己拼 `?, ?, ?` 就是 `sql-string-building`,而且每处都会拼错一次 ——
#1100 里已经有两次「元数算错」的实例(§8.11)。

三条判据,都带注入实测:

- **两个列表在同一句里各自展开**且参数顺序不变(`test_two_lists_expand_in_their_own_positions`);
- **字面量里的 `?` 不参与**展开(`test_a_question_mark_inside_a_literal_is_not_a_placeholder`);
- **空集合直接拒绝**(`ListMarker([])` 抛 `ContractError`):展开成 `IN ()` 不合语法,当成
  「匹配不到任何行」会让 `NOT IN` 静默返回空 —— 两种猜测都会把缺陷藏起来。

真实库 oracle:`tests/integration/test_portable_list_marker_pg.py` 用生产驱动跑通「三个值里选两个」
与「空批次在语句发出前被拒绝」两条。

调用点迁移是下一步:13 处 `= ANY(数组)` 分别改成 `IN (?)` + `ListMarker`,不再有定元元数问题。

### 8.13 `= ANY(数组)` 的 13 处调用点迁到 `IN (?)` + `ListMarker`

七个模块,13 处,全部是「运行期才知道大小的集合」:

| 模块 | 处数 | 集合是什么 |
| --- | --- | --- |
| `postgres_deletion_cleanup` | 7 | 本次删除涉及的 transcript / meeting id |
| `assets_postgres` | 2 | 本轮要锁定的 asset id |
| `artifact_retention_runtime` | 1 | 允许 UPDATE 的列名 |
| `job_dispatch_postgres` | 1 | 车道允许的 Job kind |
| `job_schema_preflight` | 1 | 期望存在的关系名 |
| `postgres_exports` | 1 | 一本书里的 render unit id |
| `postgres_render_units` | 1 | 要读的 generation unit id |

`vendor-operator` 由 19 降到 6(`ILIKE`/`FULL JOIN` 家族),`vendor-type` 同时再降 10(所有
`CAST(? AS text[])` 一并消失),基线 897 → 876。

**这次的心脏是「元数不再写死」**:上一轮 §8.11 的定元 `IN` 在运行期集合上写不出来,写出来也
会在集合大小变化时静默错位。迁移后 SQL 里始终是一个 `?`,元数只在展开时决定。

一处测试随之改变:`test_claim_uses_the_instance_lane_not_the_module_default` 原来断言参数等于
一个 list,现在参数是 `ListMarker`,断言改成「是标记且内容等于车道 kinds」——断言落在语义上,
而不是落在某个容器类型上。

### 8.14 一条语句不能混用两种标记:派发领取的 `ANY(?)` 保持原样

迁移 13 处时,`job_dispatch_postgres` 的领取语句判红:

```
ContractError: statement expects 4 parameters, got 5
```

原因是这条语句是**混合标记**:`?` 是便携标记,`LIMIT %s` 还是旧式标记。`?` 的个数永远不等于
参数个数(差的就是那个 `%s` 的位置),展开时无从对齐位置。这不是能靠猜补上的偏差。

因此这一处撤回,保持 `= ANY(?)`;`expand_lists` 遇到混合标记语句时直接给出原因
(「需要纯 qmark 语句」),而不是抛一个「参数个数不符」的模糊错误 —— 后者会让人去数参数,
而真正的原因在标记体系。

`vendor-operator` 由 19 降到 7(这一处 + `ILIKE`/`FULL JOIN` 家族);基线 897 → 877 的降幅来自
其余 12 处。这一处按 `--accept-new` 写回基线:它不是新引入的用法,而是「这条语句暂时留在旧写法」
的记录。

### 8.15 删除清理的 7 处撤回:`IN (?)` 在这一路径上语义不等价

`postgres_deletion_cleanup` 的 7 处 `id = ANY(CAST(? AS text[]))` 改成 `IN (?)` 后,真实库上
`test_workbench_deletion_cleanup` 与 `test_workbench_http_management` 判红,对外表现为
`503 store_unavailable`。

这一处不是标记语法的问题,而是**类型推断**:`= ANY(CAST(? AS text[]))` 明确告诉规划器参数是
`text[]`,比较发生在数组元素与列之间;`IN (?)` 的每个参数单独推断,遇到 uuid/文本混排的列时
比较算子可能落到不同分支。批次为空时还要额外哨兵值才能构造标记。

结论:这 7 处撤回,基线 877 → 891(`--accept-new` 记录原样保留的 7+7 处)。要真正收敛这一路径,
需要的是「按列类型显式 CAST 的定元/变长列表」,由 `ListMarker` 携带元素类型而不是猜 —— 那是下一
个契约变更,不在本卡内硬做。

工作台另外两处(`postgres_exports`、`postgres_render_units`)随后同样判红
(`test_workbench_runtime_export_dispatch` 报 `503`),一并撤回:workbench 的 id 列是 uuid,
`IN (?)` 的文本参数让比较算子落到文本分支。基线 877 → 894。

保留的 4 处都是「参数是文本、列也是文本」或「参数是文本、比较的是名字」的语句:
`artifact_retention_runtime`(列名)、`job_schema_preflight`(关系名)、`assets_postgres`(asset id)。
**workbench 的 uuid 列需要标记携带元素类型才能迁移**,这是下一个契约变更的明确边界。

### 8.16 撤回的 9 处回来了:根因是异步游标没展开,不是列类型

上一节把 workbench 那 9 处的失败归因为「uuid 列 + 文本参数」。**这个归因是错的**,本机起库后
一条最小探针就推翻了它:

```
psycopg.ProgrammingError: cannot adapt type 'ListMarker' using placeholder '%s' (format: AUTO)
```

`psycopg_driver` 里同步游标做了展开,`AsyncPortableCursor` 没有。workbench 走异步连接,标记
对象于是被原样交给驱动,适配失败后被 `VersionStoreError` 包成 `503 store_unavailable` —— 对外
看起来像存储不可用,和类型推断毫无关系。

修法是把同一条规则加进异步游标,并补一条异步真实库 oracle。注入实测:把这条规则去掉,用例立刻
打印上面那行 `cannot adapt type 'ListMarker'`;加回来即绿。

**但这一修不足以救活 workbench 那 9 处**:异步游标补上展开后,CI 的 workbench 用例仍报
`VersionStoreError` → `503`。也就是说 workbench 的存储适配器既不是同步游标,也不是
`AsyncPortableCursor` —— 它有自己的连接/游标层,`ListMarker` 在那里仍然原样落到驱动。这一层
尚未定位,9 处继续留在旧写法(基线 894)。

**下一步要做的第一件事是让这个错误可见**,而不是再猜一次:workbench 的 `VersionStoreError`
用 `from None` 丢掉了原始 `psycopg` 异常与 SQLSTATE,CI 日志里只剩「事务失败」。把 SQLSTATE
记进日志(或保留 `__cause__`)应当先于任何归因。

教训写在判据上:**报错被 `from None` 吃掉时,先让证据可见,再归因**。前一轮我用「撤回二分」
代替了「拿到原始错误」,代价是两轮 CI 和一份错误的根因说明。

本机现在可用的真实库:`PostgreSQL 18.6`,端口 55432,库 `speechnexus_test`。workbench 集成
harness 另有更严的 DSN 守卫(Unix socket + `workbench_test_owner` + `sslmode=on`),本机未满足,
所以 workbench 路径的真实库证据仍只来自 CI。

### 8.17 第三层找到了:workbench 的语句是 `Composed`

上一节留下了「workbench 走第三层,尚未定位」。它就在 driver 的 `_translate_composed` 旁边:
workbench 用 `sql.SQL(query).format(s=schema)` 拼语句,得到的是 `psycopg.sql.Composed`,而
`expand_lists` 只处理 `str`。展开在 `Composed` 上从未发生,标记原样落到驱动。

展开规则:标记只出现在 `SQL` 片段里,`Identifier`/`Literal` 是叶子且不含占位符,所以按片段顺序
消耗参数、逐片段替换即可,片段边界与标识符引用规则都不动。同步与异步游标共用一个
`_expanded(query, params)`,按语句形状分派。

注入实测:`test_a_list_marker_expands_inside_a_composed_statement` 在撤掉分支后立刻打印
`cannot adapt type 'ListMarker'`;恢复即绿。本机 PostgreSQL 18.6 上四条 oracle 全绿(纯文本、
空批次、异步、`Composed`)。

workbench 那 9 处因此恢复,基线 894 → 877。

### 8.18 `vendor-sdk` 收敛:业务层只从 `speechnexus_portable_sql.vendor` 取驱动

`vendor-sdk` 132 处分在 75 个文件,几乎都是同一种形状:`import psycopg`、`from psycopg import sql`、
`from psycopg.rows import dict_row`、`from psycopg_pool import PoolClosed, ...`。

新增 `speechnexus_portable_sql/vendor.py` 只做再导出(`psycopg`、`sql`、`dict_row`、`class_row`、
`Jsonb`、`conninfo_to_dict`、`IsolationLevel`、`ConnectionPool` 与三个池异常),业务侧改的只有
import 行,调用点一字不动:

```python
from speechnexus_portable_sql.vendor import psycopg  # 替代 import psycopg
from speechnexus_portable_sql.vendor import dict_row  # 替代 from psycopg.rows import dict_row
```

这样「品牌依赖只在 portable-sql 内」这条边界从守卫规则变成结构事实。首批 12 个文件、34 处已迁移,
基线 877 → 843。

**第二批 62 个文件、96 处一次迁完**,基线 843 → 747。`vendor-sdk` 归零。

边界现在由结构守住,不再靠扫描:`lint-imports` 新增契约
**"Vendor drivers enter only through the portable SQL package"**(`forbidden`,源是六个业务包,
禁止 `psycopg`/`psycopg_pool`/`pymysql`,`allow_indirect_imports = true` 让 portable-sql 的再导出
作为合法中转)。共 10 条契约。

注入实测:往 `backend/src` 放一个 `import psycopg` 的探针文件,`lint-imports` 立刻
`9 kept, 1 broken`;删掉即恢复 `10 kept`。

### 8.19 负结果:`WHERE NOT EXISTS` 不能替代 `ON CONFLICT DO NOTHING`

15 处 `upsert` 全是「同 key 只写一次」的幂等插入,第一反应是把
`ON CONFLICT (key_digest) DO NOTHING` 改写成 `INSERT ... SELECT ... WHERE NOT EXISTS`。
**这个等价关系不成立,已经用真实 PG 推翻,不要再试。**

改写后三处(profile 草稿、profile 生命周期意图、发音集合草稿)在集成测试上红了:

```
tests/integration/test_profile_drafts.py::test_same_key_blocks_on_original_transaction_then_replays_its_committed_revision
assert ('created' == 'created' and 'unknown' == 'replayed')
```

原因不是行数判断写错,而是事务语义:

- `ON CONFLICT DO NOTHING` 让**语句**在唯一索引上等待并安静返回 0 行,事务继续可用;
- `WHERE NOT EXISTS` 只是少了一个读,竞争下第二个写者仍然执行 INSERT,撞唯一索引后 PG
  **中止整个事务**;在 Python 里捕获异常不会让事务恢复,后续 `SELECT ... FOR UPDATE` 直接
  落到「事务已中止」,调用方于是走了错误分支。

真正的等价写法要 `SAVEPOINT`:`SAVEPOINT s` → INSERT → 冲突则 `ROLLBACK TO SAVEPOINT s`。
`packages/portable-sql` 目前**没有 savepoint 原语**(全仓 `grep SAVEPOINT` 为空),补它是一份
契约变更:事务端口要能表达嵌套恢复点,MySQL/InnoDB 侧同样支持,但两侧的失败边界、与 `commit_unknown`
的关系都要一起定义。

在此之前,这 15 处 `upsert` 是**有理由的例外**,不是待办:它们靠的正是 `ON CONFLICT` 的语句级
静默语义。把它们从基线上抹掉需要先有 savepoint 原语,这属于 #1100 之后的契约工作,不在本轮。

### 8.20 补上 `SAVEPOINT` 原语:§8.19 的前提

§8.19 说「真正等价需要保存点」。本轮把它补上,并在真实 PG 上立了 oracle。

`transaction.savepoint(cursor, name)` 只是三句标准 SQL:`SAVEPOINT` → 执行 → `RELEASE SAVEPOINT`,
失败则 `ROLLBACK TO SAVEPOINT` 再 `RELEASE`。名字只接受普通标识符(SQL 不允许把标识符参数化),
默认 `portable_sql_sp`。PostgreSQL 与 MySQL/InnoDB 都支持这三句,所以这层没有品牌分支。

`insert_if_absent` 暂时没有用户:它需要在冲突时把唯一违反翻成 `ConflictError`,而那一步必须先
分清「业务要处理的冲突」和「检查约束/外键这类编程错误」。

**第一次尝试证明了这个代价**:在 `PortableCursor.execute` 里把所有 `psycopg.Error` 翻成契约类别
后,集成分区红了一片 —— `CheckViolation` 变 `StorageError`、`ForeignKeyViolation` 变
`ConflictError`,而这类违反的既有测试按原类别断言。收窄到**只翻唯一冲突**后再跑,仍然红了
`test_binding_drafts`、`test_cluster_coordination_schema`、`test_workspace_writes` 等按
`psycopg.errors.UniqueViolation` 断言的用例。所以驱动层不做全局翻译,`insert_if_absent` 也随之
撤下,只保留保存点本身 —— 它不改变任何既有错误语义。

真实 PG oracle(`tests/integration/test_portable_savepoint_pg.py`,3 条,本机通过):

1. **跨会话冲突被吸收**:连接 A 先插同键并保持事务打开,B 的插入在唯一索引上等待,A 提交后
   B 的插入在保存点内撞冲突 → `ROLLBACK TO SAVEPOINT`,**事务仍可用**,随后的 SELECT 读到 A 那一行。
2. **注入即红**:同样的跨会话冲突,不走保存点直接 `execute`,后续 SELECT 得到
   `psycopg.errors.InFailedSqlTransaction`(25P02 事务中止)。这条证明第 1 条靠的是保存点,
   不是时序巧合。
3. 保存点内的失败语句不影响事务其余部分。

注意第一条必须用**两个连接**:同一个事务里连插两次不会冲突,`WHERE NOT EXISTS` 看得见自己那行。
早先我用单连接写的「oracle」是绿的但什么也没证明 —— 这是这轮第二个「绿了也不作数」的实例。
### 8.21 剩余基线 746 的结构依赖:清零依赖另外两张卡

把剩余条目按「归谁」分类之后,结论是 #1100 的「基线归零」不能靠继续迁移 DML 达到:

| 规则 | 剩余 | 其中在 DDL 模块(`*_schema.py`/`*_grants.py`/`migrations/`/`.sql`) | 归谁 |
| --- | --- | --- | --- |
| `sql-string-building` | 327 | 199 | #1102 |
| `runtime-ddl` | 165 | 160 | #1102 |
| `vendor-type` | 72 | 36 | #1102 |
| `ddl-identifier-length` | 38 | 38 | #1102 |
| `advisory-lock` | 33 | 1 | #1095 的 claim bucket |
| `vendor-json` | 33 | 8 | #1102 |
| `upsert` | 15 | 0 | 需要冲突翻译(§8.20) |
| `cte` | 12 | 0 | 逐条判断 |
| `paging-limit` | 10 | 0 | 逐条判断 |
| `returning` | 8 | 0 | 逐条判断 |

DDL 模块合计占了 446 条。它们不是「写错了的 SQL」,而是**物理 DDL profile 的定义本身**:
按 ADR 0016,物理 DDL 是部署期的品牌相关产物,受限方言约束的是运行期 DML。现在守卫把两者
放在同一条基线上,所以 #1100 只有在 #1102 把 DDL 收进迁移(而不是在业务包里拼 DDL 文本)
之后才可能归零。

这解释了为什么最近的迁移收益越来越小:`vendor-sdk` 那类「替换式」条目已经清零,剩下的大头
要么等 #1102,要么等 #1095 的 claim bucket。继续硬迁 DML 最多再动十几条,同时把
`cte`/`paging-limit`/`returning` 的风险一点点堆起来。

**本轮唯一迁移**:`model_instances_postgres._FIND` 的「一行常量表」CTE 变成派生表
`FROM (SELECT EXISTS (...) AS instance_exists) AS presence`。同一语义、更小的方言面,
本机真实 PG 的 10 条 oracle 通过(`tests/integration/test_admin_model_instances_pg.py`)。
其他 11 处 `cte` 里,`model_catalog_runtime` 与 `workbench/runtime` 是 `WITH RECURSIVE`
走 `pg_auth_members` 的权限探针,`job_cancellation_reconciliation_postgres` 是
`WITH due ... INSERT ... ON CONFLICT ... RETURNING` 的租约领取 —— 都归各自的卡,不是迁移对象。
### 8.22 把「已声明的 profile 制品」从业务 DML 基线上分开

§8.21 的结论是 446 条 DDL 条目挡住了归零。架构文档其实已经给了口径
(`docs/architecture/database-portability.md` §1.1):

- 第 2 条:表、列、键、约束由同一 canonical 逻辑 schema 定义,**目标库可使用受控的物理 DDL profile**;
- 第 4 条:仅当目标库无法执行同一 SQL 文本时,才允许在 `infrastructure` 内增加**已登记、静态的
  DML profile variant**。

也就是说,「品牌相关的物理产物」本来就要被**声明**,不是被禁止;受限方言约束的是**未声明的**
品牌 SQL。守卫之前把两者算在同一条基线上,所以 §8.21 那个环其实是口径问题,不是工作量问题。

本轮只加机制,不加声明:

- `--profiles <json>`:清单里的文件按通配符匹配,其条目单独报 `profile-ddl=<n> files=<m>`,
  不再进入业务 DML 的基线与 `new/fixed` 比较;不传这个参数时行为完全不变。
- `scripts/dev-env/portable-sql-profiles.json`:**空的** `paths: []`。声明属于策略决定,
  由评审给出,不由守卫作者顺手写。
- 自检里加一条注入:`split_declared` 必须把已声明路径与未声明路径分开,否则报错 —— 这条覆盖
  「声明了就放行、没声明照旧红」的两半。

实测口径(本机,声明 `backend/src/**/speech/infrastructure/*_schema.py` 作样本):

```
baseline=747 new=0 fixed=350
profile-ddl=350 files=39
```

即:全量 747 里 350 条落在这批 schema 模块上,剩下 397 条才是真正需要迁移或单独豁免的业务 DML。
这个数字是「声明之后基线会变成多少」的直接依据,但**清单保持为空**,直到有人明确接受这份划分。

### 8.23 按「废除优先」口径清理:草案 DDL 与无人调用的 ODBC 适配器

用户 2026-11-22 的口径:本项目确定不使用的功能一律**废除**,不做迁移。核实后本轮删除两项。

**1. `migrations/0001_cluster_coordination_draft.sql`(41 条)**
发布说明写明它「不参与产品迁移链」,生产 DDL 由 `coordination_schema.COORDINATION_DDL`(13 表)
与 `job_schema.JOB_DDL`(2 表)生成;它只是这两处的文本副本,被三个测试模块当参考件读取。
删除后测试改经 `tests/support/coordination_ddl.py` 读生成器,15 张表的列集合、索引与授权断言
不变(263 条契约测试通过)。基线 746 → 705。

**2. `packages/portable-sql/src/speechnexus_portable_sql/odbc.py`(127 行)**
证据三条,合起来是「不可能运行」而不只是「暂时没人调用」:

- 全仓没有任何 `import odbc` / `from ...odbc import`;
- `pyodbc` 不在任何 `pyproject.toml`、`uv.lock`、CI 工作流里,`speechnexus-portable-sql[odbc]`
  这个 extra 也不存在 —— 模块即使被导入也只能报「驱动不可用」;
- 产品真实路径是 `psycopg_driver` + `vendor` façade(§8.18)。

架构文档里「`pyodbc`/`aioodbc` 一类统一 ODBC 接口可以保留」这句话已改成事实表述:
仓库当前只有 `psycopg` 适配器,并注明「设计里提过」不等于「已经支持」。

**没有动的**:`pymysql_driver` 继续保留 —— MySQL/InnoDB 是协调路径的验证环境,CI 的集成分区
正在用它,属于「在用」而不是「不用」。RBAC/租户/用户目录这类首期范围外的东西本来就不存在
(`rbac`/`user_directory`/`megacube` 在产品代码里 0 命中),没有可删的。

### 8.24 #1100 的验收判据:没有未登记条目

§8 的结论是「基线归零」会让 #1100 与 #1102 互相等待。本轮把判据落成机器可检的形式:

- `scripts/dev-env/portable-sql-exceptions.json`:**基线里每一条**登记 `rule`/`path`/`count`/
  `card`/`reason`/`release`。当前 293 条(合计 705 处),归属三张卡 —— `#1102` 的物理 profile 族、
  `#1095` 的协调原语族、`#1100` 自己的语句族。
- 守卫 `--exceptions`:基线里有、登记表里没有 → `UNREGISTERED` 判红;登记表里有、基线里已经没有
  → `STALE-EXCEPTION` 判红。两半都要红,自检里各有一条注入。
- CI 同时传 `--baseline` 与 `--exceptions`,于是「静默加一条进基线」这条路也堵死了。

这样 #1100 的完成条件变成可判定的两句话:**业务 DML 里没有未登记条目**;剩余条目逐条写着归属卡与
解除条件。`profile-ddl`(§8.22 的机制)目前清单为空,物理 DDL 的收编仍归 #1102。

实测:

```
exceptions=293 cards=3
baseline=705 new=0 fixed=0
RESULT OK: no new findings outside the portable SQL subset
```

### 8.25 18 个「只被 alembic 消费」的 DDL 模块登记为物理 profile 制品

§8.22 的机制空了很久,因为「哪些文件算 profile 制品」需要证据。本轮把判定写成可复核的规则:

> 该模块在本仓的**唯一消费者**是 `speech/infrastructure/migrations/versions/*`,不与任何业务 DML 共用。

按这条规则逐个核对 147 个基线文件,命中 18 个(`coordination_schema`、`asset_schema`、
`gateway_abort_intent_schema`、`public_job_progress_schema` 等),合计 **168 处**。它们进
`scripts/dev-env/portable-sql-profiles.json`,守卫报 `profile-ddl=168 files=18`,业务 DML 基线
**705 → 537**。

声明的另一半同样是判据:**去掉任何一条声明,守卫立刻变红**。实测把 `coordination_schema.py`
从清单里删掉:

```
UNREGISTERED runtime-ddl .../coordination_schema.py
UNREGISTERED vendor-type .../coordination_schema.py
RESULT FAIL: the baseline carries entries with no owning card
```

也就是说,一个文件不能悄悄从「物理 profile」退回业务基线,也不能悄悄离开业务基线 —— 两边都有账。

注意 `job_schema.py` **不在**清单里:它的 `JOB_DDL` 虽然也被迁移消费,但同一模块还导出运行时授权语句,
属于业务与 DDL 共用,按规则不构成「唯一消费者是迁移」。

### 8.26 迁移树整体是部署期输入,不逐条进产品门禁

守卫原本只排除 `migrations/versions/`,却把同一棵树里的 `env.py` 当产品 SQL 逐条基线化。
`env.py` 是 alembic 的入口:用 SQLAlchemy 的命名绑定执行迁移互斥锁、建 schema、设 `search_path`,
只在部署期跑一次,不在应用运行时路径上。受限方言约束的是**应用运行时 DML**,把它按运行时规则
基线化只会让真正的违规淹在噪音里。

改为整棵树排除(`MIGRATION_INPUT_PREFIXES` 指向 `migrations/`),版本文件计数改用单独的
`MIGRATION_VERSION_PREFIX`,报告里的 `migration_input=62` 含义不变。`env.py` 的 5 条
(`named` 2、`advisory-lock` 1、`vendor-sdk` 1 等)随之退出业务基线:**537 → 532**。

同类判据:这条排除**不覆盖** `*_schema.py` —— 那些模块虽是 DDL,但被业务模块导入表名与列常量,
不满足「只在部署期执行」,仍按 §8.25 的规则逐个判定。

### 8.27 `insert_if_absent` 原语,以及它暂时还没有调用方的原因

§8.19/§8.20 的结论是 `ON CONFLICT DO NOTHING` 的可移植替代必须走保存点。本轮把它做成原语:

`speechnexus_portable_sql.transaction.insert_if_absent(cursor, statement, params) -> bool`
—— 插入包在保存点里,唯一冲突(`SQLSTATE 23505`,或 MySQL errno 1062)时回滚到保存点并返回
`False`,事务保持可用;其它异常原样抛出。判据三组,`tests/unit/portable_sql/`
(插入 / 冲突 / 其它异常)与 `tests/integration/test_portable_insert_if_absent_pg.py`
(同一事务内二次插入后仍能读回已有行,本机 PG 18.6 实跑通过)。

**原语先落地,调用点后移**,因为把一个 upsert 换成它不只是改产品代码:把
`profile_lifecycle_intents_sql.record_intent` 的 `ON CONFLICT (key_digest) DO NOTHING` 换掉之后,
`tests/support/profile_database.py` 里的 MySQL 替身立刻失效 —— 那个替身靠**识别 SQL 文本里的
` ON CONFLICT `** 来决定改写成 `INSERT IGNORE`,并据此读 `SHOW WARNINGS` 判 1062。SQL 一旦不含
该标记,替身就退化成普通 INSERT。同时 `tests/unit/test_profile_publications.py` 的
「失败即整体回滚」用例开始看到意图行残留。

也就是说:upsert 族的迁移必须先改**测试替身的判定依据**(从「找 SQL 标记」变成「按保存点语义判定」),
否则会同时破坏 MySQL 侧的双 DSN 验证与回滚断言。这条已计入 `upsert` 的登记理由,本轮不改调用点。

### 8.28 测试替身必须自己管事务边界,否则保存点会骗过回滚断言

§8.27 把 upsert 的阻塞点定在「替身认 SQL 标记」。再往下挖一层,真正的机制是:**替身用了
sqlite 的隐式事务管理**。`with connection:` 在遇到产品发出的 `SAVEPOINT` / `RELEASE SAVEPOINT`
之后会失去回滚能力 —— `RELEASE` 一执行,那个 `with` 块失败时也不再回滚,于是「失败即整体回滚」
的断言看起来通过,实际什么都没证明。

修法是让替身自己拿事务边界:`isolation_level=None` + 显式 `BEGIN` / `COMMIT` / `ROLLBACK`,
保存点交给 sqlite 原生执行(它支持 `SAVEPOINT`/`RELEASE`/`ROLLBACK TO`)。同时补一条直接压
这一点的用例:`insert_if_absent` 成功(保存点已 RELEASE)之后抛错,快照必须与之前完全一致。

注入即红:把替身退回 `with connection:`,这条用例立刻失败(`1 failed`)。

### 8.29 第一个 upsert 调用点落地:`record_intent` 换成保存点插入

§8.27 记录了两处阻塞,§8.28 修掉了 sqlite 替身那条。MySQL 双 DSN 替身(`tests/support/
profile_database.py`)**不需要改**:`pymysql` 把 `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` /
`RELEASE SAVEPOINT` 原样交给 MySQL,InnoDB 原生执行保存点;它上面那段 ` ON CONFLICT ` 改写只服务
仍在用 `ON CONFLICT` 的另外两个模块。

于是 `profile_lifecycle_intents_sql.record_intent` 改成:

```python
inserted = insert_if_absent(cursor, "INSERT INTO speech_profile_lifecycle_intents … VALUES (?,?,?,?,?,?,?)", (…,))
if inserted:
    return ProfileLifecycleIntentResult("recorded", recorded)
return recover_intent(cursor, command)
```

冲突判定不再依赖 `rowcount` 的 0/1 语义:插入成功就是 `recorded`,唯一冲突就是「别人先写」,
回滚到保存点后原事务继续读到那一行。基线 **532 → 531**,`upsert` 族剩 14 条。

验证范围要说清:本机跑通了 `tests/unit`(8200 passed,含 sqlite 替身那条回归)、守卫与 mypy;
双 DSN 的 MySQL 分区在本机没有实例,只有 CI 能给证据 —— 这一点与 §8.19 的教训一致,
不把「PG 通过」当成「两条 DSN 都通过」。

### 8.30 超时探针不能插在保存点回滚前面

§8.29 的首次 CI 里,`[postgresql-…]` 的「同一命令、对方先提交」用例从 `replayed` 变成 `unknown`。
原因不在原语,而在 `profile_drafts_postgres._Cursor.execute`:它在**每条语句前**发
`SELECT set_config('statement_timeout', ?, true)`。

事务被唯一冲突中止之后,唯一还能执行的语句是 `ROLLBACK TO SAVEPOINT`;探针偏偏抢在它前面,
自己先报 `current transaction is aborted`,于是回滚走不到、`insert_if_absent` 抛出的是可用性错误,
调用方看到 `unknown`。

修法:保存点控制语句(`SAVEPOINT` / `ROLLBACK TO SAVEPOINT` / `RELEASE SAVEPOINT`)跳过探针、
直接下发;其它语句照旧先装 `statement_timeout`。判据是 `tests/unit/backend/test_deadline_cursor.py`
里的一条新用例,注入即红:删掉跳过分支,该用例立刻失败(`1 failed`)。

这条也解释了为什么 §8.19 的「单连接 + 捕获异常」看不出问题:**探针把中止状态提前引爆了**,
真正的保存点回滚根本没被执行过。

### 8.31 剩下两个 upsert 调用点,以及替身里那段改写的退场

`profile_drafts_sql.create_draft` 与 `pronunciation_drafts_sql.create_draft` 都改成
`insert_if_absent`:

```python
inserted = insert_if_absent(cursor, "INSERT INTO speech_profile_commands … VALUES (?,?,?,?,'pending')", (…))
rows = _rows(cursor, "SELECT * FROM speech_profile_commands WHERE key_digest=? FOR UPDATE", (…,))
```

原来的 `inserted = cursor.rowcount == 1` 随之删除:插入是否真的发生由保存点结果直接给出,
不再借 `rowcount` 反推。

这两个模块是 `tests/support/profile_database.py` 里 ` ON CONFLICT ` 改写的**最后两个消费者**。
改完之后那段识别 SQL 文本、改写成 `INSERT IGNORE`、再读 `SHOW WARNINGS` 判 1062 的逻辑就没有
任何输入了,按「确定不使用即废除」直接删除 —— 替身的 `execute` 只剩原样转发,保存点由 InnoDB
原生处理。

基线 **531 → 529**(`upsert` 族剩 12)。

验证范围:`tests/unit` 3991 passed(backend)+ 全量 unit;守卫、mypy、`lint-imports` 全绿。
`create_draft` 的并发/重放语义由双 DSN 集成用例覆盖,本机没有 MySQL 实例,证据只能来自 CI。

### 8.32 保存点控制放行收成一条不变量

§8.30 只在 `profile_drafts_postgres` 上修了「探针不能插在保存点回滚前面」。`pronunciation_drafts_sql`
换成保存点插入后立刻撞上同一个陷阱(PG 用例把 `conflict` 报成 `unknown`),说明按文件逐个修
是把同一判定抄成多份的开始。

收法:

1. 判定只定义一次 —— `speechnexus_portable_sql.transaction` 的 `SAVEPOINT_CONTROL` 与
   `is_savepoint_control(statement)`;
2. **13 个逐语句装 `statement_timeout` 的适配器**全部改为先问这条判定,命中则直接下发、
   不装探针(`rows()` 返回 `[]`、`changed()` 返回 `rowcount`、`execute()` 返回自身/游标);
3. 新增守护用例 `test_every_per_statement_probe_lets_savepoint_control_through`:
   凡是「装了逐语句探针 + 自己实现了 `execute`/`rows`/`changed`」的模块,必须调用
   `is_savepoint_control(`。只看名字不够 —— 只 import 不调用也会被算作通过,所以判据锚在
   调用形参上。

注入即红:删掉 `pronunciation_drafts_postgres` 的跳过分支,守护用例立刻失败
(`1 failed, 1 passed`)。

范围说明:`SET TRANSACTION` 之类的**事务级**语句不走 `execute`(它们必须是事务第一条语句),
因此不在逐语句探针的路径上,也不受这条不变量约束。

### 8.33 `sql-string-building` 的假阳:提示语里出现 select 不算拼接 SQL

基线 529 里,`sql-string-building` 有 18 条其实是**提示语**:

```python
f" no longer select a local inspection process; "
f"configured but SPEECHNEXUS_ASSET_INSPECTOR=text; select audio or unset every …"
```

旧判据只要 f-string 的字面量里出现 `SELECT\b` 就算违规,于是「请选择 audio」这类文案被记成
「用 f-string 拼 SQL」。这 18 条不是待迁移的代码,是判据没落在被测对象上。

收法:拼接 SQL 的真实形状是**以一条语句的关键字开头**,所以新增
`SQL_STATEMENT_START`(锚在 `\A`,接受 `SELECT` / `INSERT INTO` / `UPDATE` / `DELETE FROM` /
`WITH` / `CREATE` / `ALTER` / `DROP` / `REVOKE` / `GRANT` / `TRUNCATE` / `COMMENT ON` /
`MERGE INTO` / `SET` / `SAVEPOINT` / `ROLLBACK` / `RELEASE`),两条判据同时满足才算违规。

判据两边都实测:

| 输入 | 结果 |
| --- | --- |
| `f"SELECT a FROM t WHERE b = {value}"` | 违规 |
| `f"INSERT INTO t (a) VALUES ({value})"` | 违规 |
| `f"GRANT SELECT ON {table} TO role"` | 违规 |
| `f" no longer select a local process"` | 不违规(反例) |
| `f"job_id = {value} AND queue = {queue}"`(纯片段) | 不违规 |

反例写进 `--selftest`:把判据退回只匹配 `SQL_SHAPE`,`SELFTEST FAIL`。

基线 **529 → 511**(18 条假阳归零),涉及 14 个文件的登记条目一并删除。

### 8.34 删掉一个冗余的 `LIMIT`,并说明它为什么是冗余的

`admission_recovery_sql.read_original` 里有一条 `ORDER BY stage_id LIMIT 65`。表面看它给读回
加了上界,实际是多余的:

- 准入契约保证阶段数 `1..64`(`AdmissionRequest.__post_init__`);
- 紧随其后的判定是「读回行数必须等于阶段数,且 stage_id 集合相同」—— 真出现第 65 行,这条
  判定自己就会报 `reconciliation_required`。

我先按「显式判界」加了一个 `MAX_STAGES` 常量和一个 `len(leases) > MAX_STAGES` 分支,注入测试
立刻证明它是**死代码**:只要行数超出,后面的等值判定必然先失败,新分支永远走不到。于是撤回常量,
只删 `LIMIT`,把「上界由等值判定承担」写进注释。

这是「先量自己的基线」那条纪律的实例:去掉 `LIMIT` 不改变任何判定结果,只是删掉一个方言依赖;
新增一条永远走不到的分支,比保留 `LIMIT` 更糟。基线 **511 → 510**。

用例 `test_more_leases_than_the_contract_allows_is_a_reconciliation_failure` 钉住的是
「65 行不会被当成一致」——它说明 `LIMIT` 的结论由等值判定给出,**不**声称它单独证明了这次删除。

### 8.35 派发角色守卫改用便携标记,并把「重复几次」收成一处

`DISPATCH_ROLE_GUARD_SQL` 原来用 psycopg 的命名标记 `%(role)s` / `%(schema)s`。便携标记是 `?`,
但 `?` 没有名字 —— 角色名在同一句里出现四次、schema 三次,谁在哪个位置全靠调用方数。

改法:

- 语句换成 `?`,新增 `role_guard_parameters(role, schema)`,把「按位置重复」这件事收成**唯一
  一处定义**,两个调用点都用它;
- 三个消费方各按自己的驱动处理:运行时(便携游标)直接 `?`;`deploy/job-startup-gate.py`
  是原生 psycopg,用 `qmark_to_pyformat` 现译;`scripts/dev-env/check-async-job-prereqs.py`
  用 psql,没有绑定参数,按同一顺序换成字面量。

判据:`test_role_guard_parameters_follow_the_placeholder_order` 钉住顺序与占位符数量一致
(`DISPATCH_ROLE_GUARD_SQL.count("?") == len(params)`)。注入即红:把参数顺序改成
`(schema, role, …)`,该用例立刻失败(`1 failed`)。

原有的漂移守护(`check-async-job-prereqs.py` 的副本必须等于运行时声明)继续生效,所以三处
不会悄悄分叉。基线 **510 → 509**。

### 8.36 特权制品的判定依据与 schema/DDL 不同,所以声明文件分成两类

`qualification_admission_grants.py` 有 12 条 `sql-string-building`,全是这种:

```python
f'GRANT SELECT, INSERT (authorization_id, …) ON "{schema}".speech_builtin_voice_probe_admissions TO "{role}"'
```

它不是待迁移的 DML:schema、角色、表名、列名都是**标识符**,任何方言都不能把标识符参数化;
`GRANT`/`REVOKE` 也没有可绑定的等价写法。继续「迁移」只会把它改写成另一种字符串拼接。

判定依据(代码证据):

| 制品 | 唯一消费者 | 结论 |
| --- | --- | --- |
| `qualification_admission_grants` | `tests/integration/test_qualification_admission_pg.py` | 部署期/用例供给 |
| `builtin_voice_grants` | `deploy/cluster/provision-roles.py` + 4 个集成用例 | 部署期供给 |
| `gateway_query_grants` | `deploy/cluster/provision-roles.py` + 用例(产品模块只取列名常量) | 部署期供给 |
| `coordination_grants` | `deploy/` 供给脚本 + 用例 | 部署期供给 |
| `public_job_admission_grants` | `deploy/README.md` 记录的部署步骤 + 4 个集成用例 | 部署期供给 |

所以声明文件分两类,各自写清依据:

- `paths` —— 唯一消费者是 alembic 迁移树(schema/DDL);
- `deploy` —— 标识符无法参数化的部署期特权制品,没有任何 DML 路径执行它。

守卫的 `load_profile_patterns` 同时读两类(`DECLARED_PROFILE_FIELDS`);少读一类,另一类就掉回
业务 DML 基线。判据两侧都实测:

- 注入即红:从 `deploy` 删掉一条 `qualification_admission_grants.py`,`baseline=485 new=12`
  → `RESULT FAIL: portable SQL subset regressed`;
- 声明健康:`test_declared_profiles_cover_both_artifact_categories` 断言两类都非空、路径都真实
  存在、同一条目不会被两类重复声明。

基线 **509 → 485**(24 条),`profile-ddl` 从 168/18 变成 192/23。

### 8.37 声明改成可复算的主张:DDL 文本必须真的只有迁移树在消费

前面 18 个 `*_schema.py` 之所以没声明,是因为「唯一消费者是迁移树」这条判据按**模块**看并不成立:
它们同时导出表名、列名、列常量给业务代码。但被问的问题问错了 —— 真正的判据不是「模块是否共用」,
而是「**DDL 文本**是否只有迁移树在消费」。常量被业务用,不等于 DDL 被执行。

这里说的「文本」不只是建表:例如 `LIFECYCLE_DDL` 里还含一条一次性数据回填 `INSERT … SELECT`,
它由迁移执行,同样是迁移树独有的制品。

复算方式(`tests/ci/test_declared_ddl_is_migration_only.py`):对每个声明的模块,取出它的 `*_DDL`
符号,在 `backend/src` 里排除迁移树后找导入点与使用点。20 个模块、37 个 `*_DDL` 符号全部为 0。

于是这一轮把 20 个 schema 模块纳入 `paths` 声明(`bindings_disable_targets_schema` 与
`gateway_abort_intent_schema` 都各自持有一个同名 `ABORT_INTENT_DDL`,但指向**不同的表**,
不是重复定义 —— 它们的表名分别是 `speech_binding_disable_abort_intents` 与 `gateway_abort_intents`)。

关键是这条声明现在**不是一句话,而是一个测试**:

- 注入即红:新建 `backend/src/.../__probe_consumer.py` 导入 `JOB_DDL` 并执行,对应参数项立刻
  失败(`1 failed, 36 passed`);删除后恢复全绿。

影响:基线 **485 → 309**(−176),`profile-ddl` 192/23 → 368/42。这个数字变化要说清口径:
它**不是**「176 条方言问题被修好了」,而是「176 条从来不是业务 DML 的 DDL 文本回到了它该在的
计数里」。业务 DML 基线剩下 309 条,`sql-string-building` 从 252 降到 69 —— 剩下的那些才是
真正的语句拼接。

### 8.38 剩下 12 处 `ON CONFLICT` 全是 `DO NOTHING`,可以统一走 `insert_if_absent`

数完剩下的 upsert:`engine_registration` 2 处、10 个单点文件各 1 处。**这条结论当时写错了**:
多数是 `DO NOTHING`,但至少两处是 `DO UPDATE`(见 §8.40 的更正),它们需要的不是「插入或忽略」
而是「插入或就地更新」,不能用 `insert_if_absent` 顶替。

本轮先转两处:

- `publications_sql.append_publication` —— 原来用 `cursor.rowcount != 1` 判断;
- `binding_publication_evidence_sql.insert_evidence` —— 原来用 `cursor.rowcount == 1` 判断。

两处都改成 `insert_if_absent(...)` 的布尔返回。`ON CONFLICT` 之前**免费**提供的一条性质现在必须
显式保住:冲突之后事务仍然可用。用例把它钉住 —— 重复插入后 `ROLLBACK TO SAVEPOINT` 真的发生过,
且异常不外泄;而检查约束失败(`23514`)必须原样抛出,证明「只有唯一冲突算已存在」。

注入即红:把 `insert_evidence` 退回 `cursor.execute` + `rowcount`,`2 failed`。

顺带修掉两处与 SQL 文本耦合的断言(`ON CONFLICT … DO NOTHING in insert_query` → 断言
「语句里没有 `ON CONFLICT` + 前面有 `SAVEPOINT`」)。这类断言在 §8.29 已经踩过一次。

基线 **309 → 307**。

### 8.39 原语不能假设调用方有外层事务

`insert_if_absent` 第一版无条件开保存点。CI 的 PG 集成分片立刻红:
`psycopg.errors.NoActiveSqlTransaction: SAVEPOINT can only be used in transaction blocks` ——
发布降级用例在自动提交连接上调用 `append_publication`。`ON CONFLICT` 在自动提交下能用,
所以这是转换引入的**语义收窄**,不是测试写法问题。

修法按连接的事务状态决定,而不是让每个调用方自己开事务:

- 自动提交:语句本身就是独立事务,唯一冲突只中止这一条语句 → 直接执行,捕获唯一冲突返回 `False`;
- 否则:包保存点(保持冲突后外层事务可用)。

判据:`_in_autocommit` 取不到状态时**按有事务处理** —— 错开保存点的代价(事务被中止)远大于
多开一个保存点。两条新用例覆盖自动提交下的成功与重复;注入即红(删掉该分支 → `2 failed, 3 passed`)。

这条也顺带说明 #1100 的一个通用风险:把「PG 免费提供的语义」换成显式原语时,要连它**在哪些事务
状态下成立**一起搬过来,否则会在自动提交的调用点上炸。

### 8.40 更正上一条:剩下的 upsert 不是同一形态

§8.38 写「剩下 12 处全是 `ON CONFLICT DO NOTHING`」。那是我按文件 grep 第一条匹配得出的结论,
**错了**。逐条看完整语句后:

| 位置 | 形态 | 能否用 `insert_if_absent` |
| --- | --- | --- |
| `tts_settlement_sql` / `tts_settlement_postgres` / `voice_capability_evidence_postgres` / `voice_publications_postgres` | `DO NOTHING` | 可以,本轮已转 |
| `engine_registration`(2 处) | `DO NOTHING` + 紧跟一次读回 | 可以,但同文件还有列清单拼接待处理 |
| `job_dispatch_postgres._record_cancellation_not_submitted` | CTE 里 `DO NOTHING` | 可以,需先把 `now_us` 从 CTE 挪成参数 |
| `job_cancellation_reconciliation_postgres` | CTE 里 `DO UPDATE … WHERE … RETURNING` | **不可以**:它是「抢占并累加尝试次数」的原子更新,语义不是插入或忽略 |
| `workbench/postgres_teleprompter` | `DO UPDATE SET …` | **不可以**,同上 |
| `sqlite_intent_store`(node-controller) | `DO NOTHING` | 该包不依赖 `portable_sql`,需先决定依赖方向 |

这一轮转掉四处 `DO NOTHING`:`tts_settlement_sql`、`tts_settlement_postgres`、
`voice_capability_evidence_postgres`、`voice_publications_postgres`。基线 **307 → 303**。

注入即红(先 ratchet 再注入,否则会被 ledger 吸收成 `fixed`):
在 `tts_settlement_sql` 把 `ON CONFLICT (lease_id) DO NOTHING` 加回去 →
`findings=304` / `new=1` / `RESULT FAIL: portable SQL subset regressed`。

`DO UPDATE` 那三处留给后续切片:它们要的是「冲突即更新并返回行」,而受限方言里没有
`ON CONFLICT`、`MERGE` 或 `RETURNING`,得用「先 CAS 更新,`rowcount==0` 再插入」的两步原语 ——
那需要一张新卡的设计,不在本轮拍脑袋实现。

### 8.41 `insert_if_absent` 会改写 `rowcount` —— 判定只能用返回值

`voice_capability_evidence_postgres` 原来靠 `cursor.rowcount == 1` 判断「这次是新插入还是重放」。
换成 `insert_if_absent` 之后,保存点路径的最后一条语句是 `RELEASE SAVEPOINT`,**它把 `rowcount`
改掉了**,于是每次新插入都被判成「已存在」:PG 集成用例报
`EvidenceRecordingBatch(... recorded=0, existing=1)` —— 期望 `recorded`,实际 `existing`。

修法:用 `insert_if_absent` 的布尔返回。原语的 docstring 现在显式写明这条(不要用 `rowcount` 判断),
替身也从「设 `rowcount=0` 表示重复」改成「插入时抛 23505」——替身表达的是语义,不是驱动的计数行为。

这是同一个根因的第四次:把「驱动免费给的判定」换成显式原语时,顺手把它的**副作用**也换了。
前三次是替身认 SQL 文本(§8.29/§8.31/§8.39),这次是 `rowcount` 被保存点覆盖。

### 8.42 动态拼出来的占位符会混用两种标记

`engine_registration._ensure_host` 原来这样建语句:

```python
names = ", ".join("allocatable_" + name for name in _DIMENSIONS)
placeholders = ", ".join(["%s"] * len(_DIMENSIONS))
cursor.execute(
    "INSERT INTO host_nodes (…, "
    + names
    + ") VALUES (?, ?, 'active', ?, "
    + placeholders
    + ") ON CONFLICT (host_node_id) DO NOTHING",
    ...,
)
```

同一条语句里既有 `?`(便携标记)又有 `%s`(驱动标记)。它能工作只因为便携游标把 `?` 翻成 `%s`,
换个驱动就崩;而且受限方言本来就不允许动态语句。

改法:列清单写字面量(`HOST_INSERT` / `POOL_INSERT`),插入走 `insert_if_absent`。
字面量的代价是与 `_DIMENSIONS` 漂移,所以用 `tests/unit/backend/test_engine_registration_statements.py`
逐项对齐:列顺序、占位符数量(`3 + len(_DIMENSIONS)`)、语句里不出现 `%s`。
注入即红:删掉一个维度列 → 该用例 `1 failed`。

同一轮还转了 `job_dispatch_postgres._record_cancellation_not_submitted`:
时钟 `WITH clock AS (…)` 改成参数,`ON CONFLICT DO NOTHING` 改成 `insert_if_absent`,
一次消掉 `upsert` 与 `cte` 两条。基线 **303 → 300**。

替身又一次要改:发布/注册的假游标遇到 `SAVEPOINT` 报「unhandled」——这是 §8.39/§8.41 之后
同一个根因的第五次出现,替身必须认可保存点控制语句。

### 8.43 「先更新、再插入」:`DO UPDATE` 的便携写法

`publications_sql` 里更新可变头指针原来是 `INSERT … ON CONFLICT (kind, object_id) DO UPDATE SET …`。
它不属于「插入或忽略」,`insert_if_absent` 顶不了。便携写法是两步:

1. `UPDATE … WHERE kind = ? AND object_id = ?`;`rowcount == 1` 就结束(绝大多数路径,头行已存在);
2. 未命中说明是**第一次发布**:`insert_if_absent` 插头行。插入也失败,说明另一个写者抢先插入
   (这一步前面已经持有 `pg_advisory_xact_lock`,所以只在加锁语义被绕过时才可能),再更新一次。

原来那句 `ON CONFLICT` 只有在 f-string 里才被守卫看见 —— 把表名改成字面量之后,守卫立刻把它报成
`upsert`(`new=1`)。也就是说**守卫此前对这条语句是漏报的**,字面量化让它显形。这是「守卫数字要当
审计线索读」的又一例:数字变小不一定是改好了,也可能是语法形态变了。

同一轮把 `publications_sql` 的 6 条 `sql-string-building` 改成字面量语句(表名常量不再参与拼接,
由 `tests/unit/test_catalog_revision.py` 等用例继续断言表名),并把退休插入也换成 `insert_if_absent`。

PG oracle:`tests/integration/test_binding_lifecycle_pg.py` 已经在真实库上连续发布两次
(`append_publication` 的 head 推进路径),所以这条转换有真实库覆盖,不是只靠替身。

注入即红:把「插入失败后再更新一次」这条兜底短路掉(插入分支永不执行)→ `3 failed, 18 passed`。
基线 **300 → 294**。

### 8.44 第二步:行锁让「先更新、再插入」不需要保存点

`workbench` 的遥提示状态写入原来也是 `INSERT … ON CONFLICT (project_id) DO UPDATE SET …`。
改成两步:先 `UPDATE`,命中即结束;未命中再 `INSERT`。

与 §8.43 的发布头指针相比,这里**不需要**「插入失败再更新一次」的兜底,原因是调用方在更早处
已经对该 project 加了行锁(`_project(..., lock=True)`):锁在事务里,同一 project 的并发插入
不可能插到「更新未命中」与「插入」之间。**兜底的必要性取决于锁的覆盖范围,不取决于语句长相** ——
这一点如果只照抄 §8.43 会看不出来。

`_Session` 原来只暴露 `_execute`(无返回值),这里补一个 `_execute_changed` 返回 `rowcount`,
因为两步法必须靠受影响行数判断。写入被抽成 `_write_state(base, project_id, state)`,好让判据落在
代码上而不是靠集成链路:

- 用例:`test_an_existing_row_is_updated_and_never_inserted`(只发 UPDATE)、
  `test_a_missing_row_falls_through_to_one_insert`(UPDATE → INSERT,且语句里没有 `ON CONFLICT`);
- 注入即红:把 `if changed: return` 短路成 `if False:` → `1 failed, 1 passed`。

基线 **294 → 293**。

### 8.45 复合抢占语句拆成 keyset 游标 + CAS:一次消掉四条规则

`job_cancellation_reconciliation_postgres` 的 `_CLAIM` 一条语句里压着四条被禁构造:
`WITH due AS`, `LIMIT`, `ON CONFLICT … DO UPDATE … RETURNING`,以及 `LEAST`。拆成三层:

1. **候选**:`_CANDIDATES` 用 `(accepted_at_us, job_id)` 的 keyset 游标(`f.accepted_at_us > ?
   OR (f.accepted_at_us = ? AND f.job_id > ?)`)+ `fetchmany(limit)`。`LIMIT` 消失,扫描量仍以
   过滤后的前 `limit` 行为界,和原来的 `LIMIT` 等价。keyset 下界是 `_KEYSET_START = -1`
   (时间戳列非负,`-1` 严格小于任何真实值)—— 第一次实现时我把 `accepted_before_us` 同时当
   上下界,真实库上直接 `claimed=0`,是 PG 集成用例抓到的。
2. **抢占**:`_take()` 先读 `attempts/settled_at_us/last_outcome/next_check_at_us`,再按读到的
   `attempts` 做 CAS 更新(`WHERE … AND attempts = ?`),`rowcount == 1` 才算拿到。
   行不存在就 `insert_if_absent`;插入失败(并发抢先)退回 CAS 路径。
3. **上限**:`LEAST(attempts + 1, ?)` 变成 Python 的 `min(attempts + 1, _MAX_ATTEMPTS)`。
   常量因此**直接**被引用而不是抄进 SQL 文本 —— `test_the_claim_clamps_in_python_through_the_same_binding`
   钉住这一点,并在同一处断言三条语句里不许再出现 `LEAST`、`max_attempts` 或那个数字本身。

两处与驱动有关的事实值得记下来:
- `rowcount` 必须从 `execute()` 的**返回值**取。池里传进来的既可能是连接也可能是游标,
  `connection.rowcount` 不存在 —— `AttributeError` 是在 PG 集成用例里暴露的。
- 语句是多行 f-string,前面有换行,替身若要按前缀分派必须先 `lstrip()`。

判据:9 条 `_take` 用例(插入、CAS 命中、CAS 落选、已结清/已耗尽/未到期跳过、饱和后仍可抢占、
插入失败退回 CAS、次数递增);**注入即红**:去掉 `rowcount == 1` 判据 → `1 failed`;
PG oracle:`test_job_dispatch_settlement_pg.py` 12 passed、`test_cancellation_exhausted_release_pg.py`
(含「耗尽分支保持可重试」)全绿 —— 真实库上 `claimed == 1 and settled == 1` 成立。

基线的诚实注脚:`_CANDIDATES` 起首关键字从 `WITH` 变成 `SELECT`,守卫因此把 2 条
`sql-string-building` 报成 `new`(表名来自 schema 模块、标识符不可绑定)。这 2 条**显式登记进
ledger**并注明理由,而不是让 `new` 悄悄落进基线。基线 **293 → 289**。

### 8.46 死代码判定:`PostgresAuditEvents` 是废除,不是迁移

`binding_lifecycle_postgres.PostgresAuditEvents` 读 `speech_publication_lifecycle_events`,带动态拼接的
`WHERE`、`LIMIT`、`%s` 与 `SET TRANSACTION READ ONLY` —— 四条被禁构造。但它**不该被迁移**,因为:

- 生产装配里没有任何引用(`grep PostgresAuditEvents backend/` 只命中它自己的 `__all__`);
- `/v1/nexus/audit-events` 路由走的是 `AuditEventService` → `audit_events_postgres`,
  读的是**同一张** `EVENT_TABLE`、同样按 `kind = 'binding'` 过滤,是它的替代实现;
- 唯一引用者是 `tests/integration/test_binding_lifecycle_pg.py` 的一段断言,而那一层已有
  `tests/unit/backend/test_audit_events_postgres.py` 覆盖。

按「确定不用的功能一律废除」的口径:删类、删测试段落、`__all__` 收敛。没有迁移,也没有为它写新语句。

### 8.47 字面量化的代价用「schema 常量」这个独立事实源来付

同一文件剩下 6 条 `{TABLE}` 插值也改成字面量,于是语句可以静态登记(#1101 的 manifest 需要)。
代价是字面量会与 schema 常量漂移 —— 我在第一次替换时就**拼错了一个表名**
(`speech_binding_lifecycle_events`,真实名是 `speech_publication_lifecycle_events`)。

`tests/unit/backend/test_lifecycle_table_literals.py` 把这件事变成判据:扫描模块里所有
`INSERT INTO` / `DELETE FROM` / `UPDATE` / `FROM` / `JOIN` 后面的名字,要求每个名字都出现在
`*schema*.py` 导出的 `*_TABLE` 常量值或 DDL 模板里。注入即红:把一个表名改成 `…retirementz`
→ `1 failed`。

### 8.48 注入即红会被陈旧 `.pyc` 骗

把源码改回原样、但文件大小与 mtime 落在同一秒内时,Python 仍加载旧字节码 ——
我确实在「注入 → 还原 → 再跑」之后看到类型为 `retirementz` 的报错,而源码里早就没有这个词。
结论:注入实验之后要么 `find . -name '*.pyc' -delete`,要么保证还原发生在不同秒。
这本身也算「断言必须会红」的一个反面教材:红了不代表你测的是当前代码。

### 8.49 头指针:两处实现并成一处

`binding_lifecycle_postgres` 的激活路径原来自己写 `INSERT … ON CONFLICT DO UPDATE`(与
`publications_sql._advance_head` 同一张表、同一语义)。字面量化让守卫看见它(`upsert` `new=1`),
于是把 `publications_sql` 的私有函数改成公开的 `advance_publication_head` 并在此调用 ——
**一个不变量只有一个定义点**,顺手删掉一份重复语句。

同轮把事件插入里的 `CAST(? AS jsonb)` 登记为 `vendor-json`(card #1102):列类型与 JSON 适配
属于物理 profile,不是 DML 收敛能改的;登记理由写进 ledger,不让它悄悄进基线。

基线 **289 → 283**。PG oracle:`tests/integration/test_binding_lifecycle_pg.py` 13 passed。

### 8.50 表名字面量化的通用判据,以及列清单为什么这一轮不动

`test_lifecycle_table_literals.py` 从「只查一个模块」改成参数化的多模块检查:凡是被字面量化的模块,
都要求它语句里 `INSERT INTO` / `DELETE FROM` / `UPDATE` / `FROM` / `JOIN` 后面的表名出现在
`*schema*.py` 导出的常量值或 DDL 模板里。事实源有两类,都是代码:`*_TABLE` / `TABLE` / `JOBS`
这类导出常量,以及 `CREATE TABLE …` 的模板文本(含 alembic 里的 `op.execute("CREATE TABLE …")`)。
注入即红:把 `speech_voice_capability_evidence` 拼成 `…capabilty…` → `1 failed`。

**列清单是另一回事,这一轮没有动。** `deletion_executions_postgres` 的 21 列 `_LEDGER_ROW_COLUMNS`
在 5 处被插值;字面量化意味着把同一份 21 个列名写 5 遍,那比现在更糟。守卫的用意是「语句文本可静态
登记」,而列清单常量在导入时就是一个确定值 —— 真正的分歧在守卫规则与 manifest 的登记粒度,不在
这些调用点。所以本轮只处理**表名**插值,列清单留到能与 #1101 的 manifest 一起判断的时候。

`voice_publications_postgres` 顺带把 `ON CONFLICT (key_digest) DO NOTHING` 换成 `insert_if_absent`,
并让 `rowcount != 1` 的判据改读返回值 —— 又是 §8.41 那条(保存点会改写 `rowcount`)。

基线 **283 → 279**。PG oracle:`test_profile_publications.py` + `test_binding_lifecycle_pg.py` 35 passed。

### 8.51 部署期 DCL 从运行时模块里搬出去,而不是重写

`admission_postgres.py` 里夹着两个授权生产者(`binding_disable_admission_grants` /
`tts_voice_admission_grants`),它们是 `GRANT SELECT (…) ON "schema".table TO "role"` ——
schema 与 role 是标识符,不能绑定成参数。按 DML 口径改造它们没有出路;**按 deploy 口径登记才对**。

问题是 `deploy` 类别按文件登记,而这两个函数住在运行期 DML 模块里。所以做的不是重写,是**搬家**:
新建 `admission_grants.py`,与既有的 `qualification_admission_grants.py` / `builtin_voice_grants.py` /
`gateway_query_grants.py` 同一约定(授权生产者单独成文件,运行时模块不携带 DCL),然后把文件放进
`portable-sql-profiles.json` 的 `deploy` 列表。

调用点跟着改:`deploy/cluster/provision-roles.py`、`qualified_speech_facts_postgres.py`、两个集成用例。
注入即红:把 `admission_grants.py` 从 `deploy` 清单移除 → `UNREGISTERED sql-string-building …admission_grants.py`。

### 8.52 一次测量否掉了一个想改守卫的念头

我本来打算修改守卫规则:允许「只插值模块级字符串常量」的 f-string,理由是这类语句文本仍是确定的。
先量了一遍:当前 99 条 `sql-string-building` 里,**只有 12 条**是常量插值,**87 条是真动态**(列清单、
谓词拼接、`%s`、schema 标识符、`GRANT`)。改规则最多让 12 条消失,却会把「列清单拼装」一起放进来 ——
那正是 `deletion_executions_postgres` 里最该留在守卫视野内的形状。

所以规则不动。这个数字(12 / 87)也解释了为什么这项收敛只能逐个调用点做,不能靠一次规则调整。

基线 **279 → 275**。PG oracle:`test_tts_durable_pg.py` + `test_transcription_disable_barrier_pg.py` 13 passed。

### 8.53 两种身份:分支写字面量,而不是拼标识符

`builtin_voice_revocation` 按 `kind` 在两组表/身份列之间选:审批表 `…source_approvals` /
`…approvals`,撤销表 `…source_revocations` / `…revocations`,身份列 `source_approval_id` /
`approval_id`,而且用的是 `%s`。三条语句拆成六个分支写法,每条都是字面量、都用 `?`。

这不是「重复」:两张表本来就是两个身份,分支把这件事写在代码里;拼标识符则把它藏进字符串。
PG oracle `test_builtin_voice_revocation.py` 的 `[source]` / `[qualification]` 两个参数化用例
正好各走一条分支。

### 8.54 启动探针:一条静态语句替代 N 条动态语句

`admission_watch_runtime` 与 `job_cancellation_runtime` 的启动检查原来对每张表发
`SELECT 1 FROM {table} WHERE 1=0`。改成一条静态语句:

```sql
SELECT 1 FROM speech_workspaces, workspace_quotas, … WHERE 1=0
```

`WHERE 1=0` 让 PostgreSQL 仍然解析每一个关系(缺表或缺 SELECT 权限照样报错),但不返回行、也不做
真实连接工作。N 条动态语句变成 1 条可静态登记语句,失败模式不变。

判据:`test_lifecycle_table_literals.test_the_startup_read_probes_name_only_schema_tables` 要求探针里
列出的每个关系都来自 schema 常量或 DDL。注入即红:把 `…not_submitted` 拼成 `…not_submited` → `1 failed`。

基线 **275 → 270**。PG oracle:`test_builtin_voice_revocation.py` 2 passed(两种身份各一)。

### 8.55 列清单字面量化:用「对着 DDL 核对」偿付重复

上一轮我说列清单要等 manifest 粒度,这一轮换了个不违背那句话的做法:**把列清单写死,再用 DDL
做独立事实源核对**。`deletion_plans_postgres` 的 16 列插入、`deletion_executions_postgres` 的
20 列账本插入都改成字面量语句,`%s` 换成 `?`,删掉 `", ".join(...)` 与 `"[%s]*n"` 两条拼装。

新增两个判据(各自注入即红):

- `test_deletion_plan_insert_statement.py`:语句里的列序 == `DELETION_PLAN_INSERT_COLUMNS`,
  `?` 个数 == 列数,无 `ON CONFLICT`,每列都在 `DELETION_PLAN_DDL` 里(拼 `plan_digset` → red)。
- `test_deletion_ledger_insert_statement.py`:同构,表名对着 `DELETION_LEDGER` 常量,列对着
  `DELETION_LEDGER_INSERT_COLUMNS` 与 `DELETION_LEDGER_DDL` + `DELETION_LEDGER_TARGETS_DDL`
  (拼 `complete_at` → red)。

后一个用例第一次跑就抓到一个真问题:`error` 列不在 `CREATE TABLE` 里,而是 0063 的
`ADD COLUMN IF NOT EXISTS error JSONB`。判据的事实源因此必须同时包含建表与升级两段 DDL ——
否则它会因为「找不到列」把正确的插入判成错的。

这次没有重复的清单:插入语句只有一处,常量仍在自己的文件里供 GRANT 与其它语句使用,两者的关系由
用例钉住。真正难的那部分(5 处 `SELECT` 共享 21 列清单)仍然留着,理由没变:那是登记粒度问题。

基线 **270 → 268**。PG oracle:`test_speech_deletion_plans_pg.py` + `test_speech_deletion_execute_pg.py`
41 passed。旁注:本机 MySQL 未在运行,`tests/integration -k deletion` 里 9 个 `test_workbench_*` 用例
因 `ConnectionRefusedError` 报错,与本次改动无关。

### 8.56 一个文件清零:`deletion_executions_postgres` 从 8 条到 1 条

这条文件原来同时踩 `returning`(3 处)与 `sql-string-building`(5 处)。清零的做法不是逐条改写,而是先把
**列清单冻结成字面量**,再让所有 CAS 都只看 `rowcount`:

- `claim_purge` / `release_purge` / `_finalize` / `_accept_operation_content` / 工件墓碑 五处
  `... RETURNING 列` 改成 `UPDATE|DELETE ...` + `changed()` 的 `rowcount`,命中后按主键读回;
- 按主键读回需要一条只带主键条件的语句,于是 `_SELECT_LEDGER_BY_PK`(1 个参数)与
  `_SELECT_LEDGER_BY_ID`(workspace + id,2 个参数)必须分开。**我第一次合并这两条时留下了参数个数
  不匹配**,PG oracle 立刻红(`503 deletion_unavailable`,19 failed)。类型系统看不见绑定个数,
  只有真库会。

剩下的 1 条是授权探针的 `VALUES` 行:行内容由 schema 常量派生(列级 GRANT 必须跟着 DDL 走),
字面化等于把列清单在这里再写一遍,所以登记进 ledger 而不是改写。

判据 `test_deletion_execution_statements.py`:7 条读语句的投影列序 == `_LEDGER_ROW_COLUMNS` /
`_PLAN_ROW_COLUMNS`,6 条写语句无插值、无 `%s`、无 `RETURNING`。注入即红:给 `_SELECT_LEDGER_FOR_PLAN`
加一列 → `1 failed`。

### 8.57 守卫按「文件 × 规则」计数,不是按「站点」计数

这一轮才发现:把 5 处 `RETURNING` 中的 2 处改掉不会有任何数字变化——只要同文件还有一处 `RETURNING`,
规则就从该文件消失不了。所以**一个文件要么清零,要么不动**;按站点「改造一半」在基线记账上没有意义。
这也解释了为什么前几轮 process 都是「一个文件/一组同构站点整体迁移」。

基线 **268 → 261**。PG oracle:`test_speech_deletion_read_pg.py` + `test_speech_deletion_execute_pg.py`
37 passed,`test_speech_deletion_plans_pg.py` 另 41 passed。

### 8.58 空值哨兵换成「空串 = 第一页」的单点定义

`binding_disable_targets_postgres` 用 `CAST(? AS text) IS NULL OR submit_intent_id > ?` 表达「可选起点」。
这是两件事叠在一起:一个厂商类型转换,和一个把 NULL 当「未设置」的哨兵。改成**空串哨兵**:

- `after or _FIRST_PAGE`,`_FIRST_PAGE = ""` 是这一语义的唯一写法;
- 谓词恒为 `submit_intent_id > ?` —— 任何合法标识符都大于空串,所以「第一页」自然成立。

网关/节点范围不是同一种哨兵(空串参与相等比较会变成「只匹配空网关」),所以那两处按 Python 侧分支:
无范围走不带范围谓词的语句,有范围走带 `gateway_id=? AND host_node_id=?` 的语句。

### 8.59 一个文件清零,代价是同一段投影写三遍

`binding_disable_targets_postgres` 的 5 条 `sql-string-building` 与 2 条 `vendor-type` 一起清零:
投影列从 `", ".join(_FIELDS)` 变成字面量。17 列的投影因此在三条读语句里各写一遍,
`_INSERT_TARGET` 再写一遍 —— 换来的是每条语句可静态登记,而列序的正确性由
`test_binding_disable_targets_statements.py` 对着 `_FIELDS` 钉住(注入加一列 → `1 failed`)。

`ON CONFLICT … DO NOTHING RETURNING event_id` 换成 `insert_if_absent` 的布尔返回:
「这一次是不是我插进去的」正是它要回答的问题,不需要把行读回来。

基线 **261 → 254**。PG oracle:`test_binding_disable_capture_scope_pg.py` +
`test_binding_disable_intent_pg.py` + `test_transcription_disable_barrier_pg.py` 12 passed。

### 8.60 运行期拼谓词的读语句:把 2×2 种形状写成四条完整语句

`audit_events_postgres.statement()` 原来按 `after` / `action` 组合拼 `_BASE + 谓词 + _ORDER`。
组合只有四种,于是写成四条**完整**语句,`statement()` 只做一次查表,不再拼字符串:

| after | action | 语句 |
| --- | --- | --- |
| 无 | 无 | `_PAGE_FIRST` |
| 有 | 无 | `_PAGE_AFTER_ORDER` |
| 无 | 有 | `_PAGE_ACTION_ORDER` |
| 有 | 有 | `_PAGE_AFTER_ACTION_ORDER` |

参数顺序仍是「先 after 再 action」,由构造函数的追加顺序决定,查表不动它。

判据 `test_audit_event_statements.py`:四条语句的投影列序 == `_COLUMNS`、每条恰好一个 `ORDER BY`;
`statement()` 对四种查询各自返回对应那一条。注入即红:从 `_PAGE_ACTION_ORDER` 删掉
`credential_label` → `1 failed`。

同一轮顺手把 `binding_disable_barrier` 的表名改成字面量。改动让守卫**看见**了该文件里既有的
`pg_advisory_xact_lock`(此前被前一条规则掩盖),按 #1095 登记 —— 这不是新引入的构造,是原本就在的。

基线 **254 → 252**。PG oracle:`tests/integration -k audit` 11 passed、`-k audit`(unit)115 passed。

### 8.61 容量列:12 个 SET 赋值写死,顺序对着 `DIMENSIONS` 钉住

`leases_sql` 与 `lease_release_sql` 都按 `held_/quarantined_ × DIMENSIONS` 拼 `SET` 子句,
并用 `%s`。两处都改成 12 个赋值写死的字面量语句,参数顺序不变(`held.values` 然后 `quarantined.values`,
再由语句尾部追加 `node_state` / `version`)。

列顺序不能凭感觉:`test_host_node_update_statements.py` 从 `DIMENSIONS` 派生期望序列,与两条语句里
`SET` 部分的列名逐项比较,并断言语句以 `WHERE host_node_id = ? AND version = ?` 收尾(CAS 仍在)。
注入即红:从 `leases_sql` 删掉 `held_scratch_disk_bytes` 一个赋值 → `1 failed`。

这两条语句在真库上由 `test_completed_release_pg.py` 与 `test_owner_activation_quarantine.py`
(参数化的 `postgres` 分支)覆盖:21 passed。

基线 **252 → 250**。

### 8.62 字面量化会让被掩盖的规则暴露

`assets_postgres` 的候选扫描原本写成 `"SELECT … WHERE " + predicate + f" ORDER BY {polled} … LIMIT ?"`。
字面量化后守卫立刻报出 **`paging-limit`(2)**,而基线里从来没有这两条 —— 也就是说这个文件一直在两条规则上
同时越界,只是 `sql-string-building` 先被记录,`LIMIT` 藏在 f-string 里没被单独看见。

处理方式不变:该留的留、该记的记。这两个 `LIMIT ?` 是有界页(`limit` 参数 1..100 且上游已校验),
去掉它就得在 Python 里取全量再截断,那不是等价改动;所以登记进 ledger(card #1100,理由「先去重定序再决定
取哪一行」),并在 PR 里如实说明**净变化是 −1 而不是 −3**。

同一轮把 `abort_intent_sql` 的回执投影与 `binding_drafts_sql` 的两套插入列集合写成字面量,
后者顺带把 `ON CONFLICT (key_digest) DO NOTHING` 换成 `insert_if_absent` 的布尔返回,
`rowcount == 1` 的判据由它承担。

判据 `test_literalized_statements.py` 三条,各自注入即红:
删 abort 回执一列 / 把两个 asset 扫描的 ORDER BY 列对调 / 删 binding 插入一列 → 都是 `1 failed, 2 passed`。

基线 **250 → 249**。

### 8.63 读回投影与预留容量的又一处字面量化

`artifacts_postgres` 的读回投影 `_COLUMNS + ", deleted_at_us"` 与 `runtime_sql` 的
`held_{name} = %s` 都改成字面量语句。两处都保留原有的 CAS 尾部
(`WHERE artifact_id = ?` / `WHERE host_node_id = ? AND version = ?` 且 `version = version + 1`),
判据逐项核对,注入即红。

`runtime_sql` 这条用的是 `version = version + 1` 而不是写回读到的值:那是「预留」语义(自增),
与 `leases_sql` 的「写回已知新值」不同,改写时没有把两者混为一谈。

基线 **249 → 246**。PG oracle:`-k runtime_instance or engine_instance` 2 passed、
`-k artifact` 相关 `test_artifact_retention_pg.py` 因需要隔离 MinIO 而 skip(本机无端点)。

### 8.64 剩余 244 条的构成与「能不能动」的判定

按规则统计(2026-11-23,基线 196):

| 规则 | 条数 | 归属 | 能否现在动 |
| --- | ---: | --- | --- |
| `sql-string-building` | 53 | #1100 | 能;但字面量化会暴露被掩盖的 `paging-limit` / `vendor-json` |
| `vendor-type` | 26 | #1102 | 不能:厂商类型名(`jsonb`/`text`)要等 bootstrap 与 profile |
| `vendor-json` | 33 | #1102 | 不能:JSONB 运算符与 `->>` 取值的替代需要契约层先定 |
| `advisory-lock` | 32 | 无卡 | **不能**:见下 |
| `runtime-ddl` | 5 | #1102 | 不能:workbench 在运行期建表,bootstrap 改由迁移承担后才消失 |
| `paging-limit` | 12 | #1100(已登记) | 不能等价去掉:都是有界页,去掉要改成取全量再截断 |
| `cte` | 9 | #1102 | 不能:递归/聚合改写要先证明等价 |
| 其余 | 28 | 混合 | 逐条判定 |

`advisory-lock` 的判定(证据):

- 站点是活代码,不是死代码。以 `admission_postgres` 为例,锁在「Voice 目录发布 vs 准入」之间串行化,
  锁后**故意重读一次原身份**(`A duplicate admission can commit while the voice lock is awaited`),
  这是 #1096 的「锁后探针」模式,不是残留。
- 它不是 #1095 的 claim bucket:后者管 Job 的 claim 分配与 fencing,不管 Voice 目录发布。
  所以 #1095 关闭并不使这些锁成为可删对象。
- 受限方言里没有「可移植的跨事务串行化原语」替代 `pg_advisory_xact_lock`。
  要动它必须先设计一个(例如用 bucket 行锁 + 持久 grant),那是新设计增量,
  不在 #1100 的收敛范围内,也不是删代码能解决的。

因此 #1100 的可动面只剩 `sql-string-building` 与已登记的有界页;基线归零需要 #1102 先落地。
这一条写进 issue #1100,避免把「基线没归零」读成「执行不力」。

### 8.65 `ListMarker` 的展开必须由驱动层拥有(已补齐)

`abort_recovery_sql` 的三条语句原本按批次长度拼 `%s`(`IN (%s, %s, …)`)。换成契约里的
`ListMarker`(`IN (?)` 一个标记)之后 CI 红,只有 `mysql` 分支红:

```
FAILED tests/integration/test_gateway_abort_ledger.py::test_due_queue_returns_the_original_origin_and_steps_its_check[mysql]
KeyError: 'intent-a'
```

根因不在语句,在装配:psycopg 驱动的 `PortableCursor` 调 `expand_lists` 摊开标记,而
`pymysql_driver._PortableCursor` 只做标记翻译(`?` → `%s`),**不摊开列表**。于是 `IN (?)` 的 `?`
被当成普通参数绑定,`ListMarker` 对象本身被当作值 —— 两条语句都匹配不到行。失败形态是**静默失配**,
不是报错,所以更难查。

修法:把 `expand_lists` 也接进 `pymysql_driver`,与 psycopg 驱动共用同一条规则(`markers.py` 的注释
早就写明这条规则不该写两遍)。判据 `test_pymysql_list_marker.py` 四条:

- 一个标记摊成 N 个占位符,参数按序摊平(断言最终 `%s` 文本与参数元组);
- 同一个标记对象可以连续服务两条语句(展开是纯函数,不是一次性迭代);
- mixed 语句(`%s` + 列表标记)被 `ContractError` 拒绝;
- 普通参数仍只做翻译,不受影响。

注入即红:把展开分支改成永不执行 → `3 failed, 1 passed`。

契约层面的结论:**「标记必须在驱动层展开」是 `SubmitCursor` 契约的一部分**,不是某个驱动的实现细节。
任何只做标记翻译、不做列表摊开的游标,都会把 `IN (?)` 变成静默失配。

### 8.66 abort 回收语句字面量化(第二次,这次两种方言都过)

驱动补齐后重做:`_DUE` / `_MARK_DUE` / `_READ_RECEIPTS` 三条字面量,`IN` 用
`ListMarker(taken)`,同一对象服务更新与读回两条语句。

`LIMIT ?` 因此被守卫看见(1 条 `paging-limit`,有界页,登记)。

基线 **246 → 244**。

### 8.67 元数由运行期决定的占位符列表:补规则,而不是只改那一处

`runtime_sql` 的 `engine_instances` 插入是仓库里最后一处 `", ".join(["%s"] * len(values))`:
15 个基准列 + peak/steady/per_slot 各 6 个维度 = 33 列,列名和占位符都按长度拼。改成一条 33 列的
字面量语句后,**守卫的数字没有变化(244)** —— 因为这条语句原本就没有被任何规则记录(它是 `+` 拼接,
不是 f-string)。

所以这一轮做的两件事要分开记:

1. 语句本身:33 列写死,`?` 恰好 33 个。判据 `test_engine_instance_insert_statement.py` 从
   `DIMENSIONS` 派生期望列序,注入即红(删 `peak_unified_physical_memory_bytes` → `2 failed`);
2. **新增守卫规则 `dynamic-placeholder-list`**:`["?"] * len(...)` 与 `["%s"] * len(...)` 一律记为
   一条发现。理由与 `ListMarker` 的存在理由相同 —— 占位符的元数是标记文法的职责,由驱动层摊开;
   业务侧乘出长度就是 `sql-string-building` 的兄弟问题,只是不带 f-string 而已。

守卫自检新增该规则的注入样本,`--selftest` 通过;在真实文件上临时回填这一写法,守卫立刻红
(`UNREGISTERED dynamic-placeholder-list … runtime_sql.py`,退出码 1),随后回滚。

**为什么值得加规则而不是只改一处**:这一处能改,是因为我恰好读到了它。规则让「元数拼接」从
「读代码时靠眼力」变成「跑守卫就报」。基线不因它下降,但下一处会在 CI 里被拦下,而不是再等一轮人工巡检。

### 8.68 启动权限探针:`CURRENT_USER` 与具名角色的分支(六个站点一次清掉)

`startup_privileges` 的六个探针都用了同一个 `_role_argument()`:身份为空时插 `CURRENT_USER`,
否则插 `CAST(%s AS name)`,再把结果拼进 `pg_catalog.has_*_privilege(...)`。这是「身份分支藏在文本里」,
与 8.44 记录的同一类问题。

改法是把它变成**成对的完整语句**:`_X_CURRENT` 与 `_X_NAMED`,共 12 条。判据
`test_startup_privilege_statements.py` 的核心不是逐条比对文本,而是**两两等价**:

```
current.replace("CURRENT_USER", "CAST(? AS name)") == named
```

这条断言同时挡住两种退化:一边改了权限面另一边没改(注入把一个 `'CREATE'` 改成 `'USAGE'` → red),
以及有人「顺手统一」两句导致身份参数错位。

另外三处顺带收掉,都是别处的缩减而不是这一卡的发明:

- `LIMIT 1` 去掉:调用方本来就 `fetchone()` 只取一行,`LIMIT` 是可移植子集里的受限写法;
- `<> ALL(CAST(? AS text[]))` 改成 `NOT IN (?)` + `ListMarker`:数组转换消失(驱动层摊开标记);
- `_role_argument()`、`_TABLE_SURFACE`、`_COLUMN_SURFACE`、`_SEQUENCE_SURFACE` 变成死代码后删除
  (权限面已写在字面量里)。

净变化 **244 → 230**(−6 `sql-string-building` −2 `vendor-type`),且这一项没有把任何发现「转化」成新规则。

### 8.69 目录读:两处表名插值,判据落在「已退休修订仍被排除」

`model_catalog_sql` 的两条读取用 `{RETIREMENT_TABLE}` / `{STATE_TABLE}` 插值。改成字面量后,判据
`test_model_catalog_statements.py` 的重点是**语义谓词还在**:

- 已退休修订必须继续被排除,而且四个键都要对上(`kind` / `object_id` / `revision` / `revision_id`);
  只比对 `NOT EXISTS (` 的存在会漏掉「键少了一个」这种退化;
- 状态读仍是单行读(`WHERE singleton IS TRUE`),语句里不得出现聚合 —— 计数器不是行数。

注入即红:从退休谓词里删掉 `retired.object_id = p.object_id` → `1 failed, 2 passed`。

基线 **230 → 228**。

### 8.70 判定:workbench 的 `{s}` 不是债,是配置项(4 条不迁移)

用户口径要求「先判定再动手,判定要给出代码证据」。对 workbench 里的 `{s}.表名` 这类语句,本次判定是
**不迁移**,证据如下:

| 证据 | 位置 |
| --- | --- |
| schema 名是必填配置字段 | `workbench/runtime_settings.py:31` `database_schema: str` |
| 该配置经标识符校验 | `runtime_settings.py:257` `workbench_namespace` 调 `schema_identifier(value)` |
| 且显式拒绝与语音底座同名 | 同处:`if value == "speech": raise ValueError("workbench requires its own namespace")` |
| 语句经标识符组合而非文本拼接 | `workbench/infrastructure/postgres_versions.py:338` `sql.SQL(query).format(s=self._schema)` |
| schema 从配置装配 | `workbench/runtime.py:645,661` `schema=settings.database_schema` |

结论:把这些语句改成字面量,等于把 schema 钉死成某个常量,即**删除「workbench 可配置命名空间」这项已支持能力**。
涉及 4 条(`postgres_saved_assets` 1、`postgres_assistant_turns` 1、`postgres_exports` 2),在 ledger 里以
上述证据为理由登记为不迁移,而不是当作待偿债务。

判定与「暂时没人调用」的区别正在这里:不是没人用,而是**它和第 1098 号设计并不冲突** —— 受限方言限制的是
品牌分支与供应商原语,不是「标识符按配置组合」。真正的约束是「不得按品牌命名构造语句」,这四处都不涉及。

同类的 `{_COLUMNS}` 投影常量仍按 8.5x 的做法字面量化,因为列集合是代码事实,不是部署输入。

### 8.72 #1102 第一刀:workbench 的 DDL 片段进 profile 清单,复算器同时加固

workbench 的 13 个 `*_schema.py` 一直是「部署期制品却按业务 DML 判红」的状态:它们被守卫记为
`runtime-ddl`,而 `workbench/migrate.py` 的模块注释写明

> the workbench fragments … are executed exactly once by the *owner*, on a fresh namespace,
> inside one transaction. This module is that step (``workbench-migrate``), and it is the only
> place the workbench DDL runs. It is not a runtime migration runner and it never repairs drift.

所以正确动作不是「改写成字面量」(DDL 本来就不是 DML),而是**把声明补上,并把复算器做对**:

1. `scripts/dev-env/portable-sql-profiles.json` 的 `paths` 增加 13 个 workbench 片段;
2. `tests/ci/test_declared_ddl_is_migration_only.py` 相应加固 —— 原复算器只认模块级 `*_DDL` 常量,
   而 workbench 有 3 个文件把 `CREATE TABLE` 内联在 `sql.SQL("""…""")` 里、2 个文件只有 `_V1`/`_V2`
   这类名字。新复算器的判据改成**按内容识别**:模块级常量文本出现 `CREATE TABLE`/`CREATE SCHEMA`/
   `CREATE INDEX`/`ALTER TABLE`,或函数体里内联了这些文本,都算制品;
3. 允许的消费者从「迁移树之外一律算违规」改成「迁移树 + `workbench/migrate.py` + 清单内部互相复用」——
   这条放宽是有必要的,因为 `runtime_schema` 就是把各片段组合起来的那一处;但**业务运行时代码仍被排除**。

注入即红:把 `content_writes_migration_sql` 导入进 `postgres_versions.py`(业务侧) → 复算器立刻报
`declared artifact has a business consumer`,退出码 1。

净变化 **228 → 196**(−13 `runtime-ddl` −19 `vendor-type`;profile-ddl 404 条 / 56 个文件单独计数,
不进业务 DML 基线)。剩下 5 条 `runtime-ddl` 全在 `node-controller` 的 SQLite intent store,属单机本地库,
是另一条判定线。

### 8.73 判定:node-controller 的本地 intent store 不迁移(节点本地状态,非产品库)

剩下 5 条 `runtime-ddl` 全在 `node-controller/src/speechnexus_node_controller/infrastructure/sqlite_intent_store.py`,
同文件另有 `sql-string-building` / `upsert` / `vendor-type` 各一条。判定是**不迁移**,证据:

| 证据 | 位置 |
| --- | --- |
| 存储路径来自部署配置 | `deploy/cluster/compute/compose.yaml:72`、`deploy/images/node-controller.Dockerfile:75`(`SPEECHNEXUS_CONTROLLER_INTENT_STORE_PATH`) |
| 装配点唯一 | `node-controller/src/speechnexus_node_controller/app.py:76` `SqliteIntentStore(settings.intent_path)` |
| 参与就绪判定 | `node-controller/src/speechnexus_node_controller/app.py:104` 的 `IntentStoreReadinessProbe(store)` |
| 设置项必填 | `node-controller/src/speechnexus_node_controller/settings.py:53` `intent_store_path: Path` |

为什么不是「暂时没人调用」:它**在部署清单里、在就绪探针里、在必填设置里**,是活的节点本地状态。

为什么仍不迁移:SQLite 的首启建表没有第二个执行点(没有迁移器),`INSERT OR REPLACE` 也是本地库
的写法;受限方言约束的是**产品 DML**(PostgreSQL 权威库)。把节点本地 bootstrap 当作产品 DML
来「迁移」,只会把它变成更绕的本地建表流程,不会减少任何方言风险。

**这与「确定不使用的功能要删除」并不矛盾**:它不是不用的功能,是不能删的节点本地状态。五条在
ledger 里以本表为理由登记。

**勘误(本条判定的有效边界)**:判定适用的九条不变(`runtime-ddl` 五条、`sql-string-building`
一条、`vendor-type` 三条),继续留在 ledger 里;同文件的 `upsert` 一条**已迁走**,因为它不是
bootstrap DDL 而是 DML:`ON CONFLICT (key) DO UPDATE SET value = excluded.value` 是真 upsert,
折成「插入即读」会把新的 `schema_version` 吞掉。现在的写法是同一事务里先 `UPDATE`、
`rowcount == 0` 才 `INSERT`,插入那一步包在 `packages/portable-sql` 的 `savepoint` 里 —— 与
§8.20 是同一个原语。SQLite 没有 `SELECT ... FOR UPDATE`,串行化由 `BEGIN IMMEDIATE` 的库级写锁
承担;把事务改成 `BEGIN` 后,并发写同一个键实测以 `database is locked` 失败。

### 8.74 #1100 剩余 196 条的真实构成:逐类说清「卡在什么上」

基线停在 196 时,最容易犯的错是把「没做完」和「做不了」混为一谈。下表是**每一条都指向一个具体
缺口**的清单,不是待办数量统计:

| 规则 | 条数 | 文件 | 卡在什么上 |
| --- | ---: | ---: | --- |
| `sql-string-building` | 43 | 24 | 写本节时 51 条 / 25 个文件,其中 8 条标识符插值已随 #1194 字面量化。现行构成见 §8.74.1:**42 条标识符/语句文本(必须保留)+ 1 条值插值(已改绑定参数)**;**没有可迁移项** |
| `advisory-lock` | 32 | 24 | 需把这些站点换成已交付的 `claims` 原语(bucket + fence)。原语有 PG oracle(`test_claim_bucket_pg.py` 18 passed),但**生产派发链路仍走自己的 CTE**,尚未改接 |
| `vendor-type` | 32 | 19 | DDL 类型字面量(已在 profile 制品侧)与行工厂里的第三方类型;要落地需要应用层类型映射 |
| `vendor-json` | 28 | 17 | JSONB 运算符;要么在应用层做 JSON,要么给受限方言加 JSON 谓词原语 |
| `paging-limit` | 12 | 9 | 缺少「有界分页」的契约原语(keyset 之外的部分) |
| `cte` + `skip-locked` + `returning` + `format` | 16 | 4 | **同一处**:`job_dispatch_postgres` 的派发领取语句族。与 `advisory-lock` 同一根因——等 `claims` 改接 |
| `session-tuning` / `vendor-lock` / `distinct-on` / `upsert` / `vendor-function` / `vendor-time` / `vendor-error-branch` / `aggregate-filter` / `pyformat` | 20 | 12 | 零散,各自需要一次独立判定;`session-tuning` 缺事务级只读/超时原语 |
| `runtime-ddl` | 5 | 1 | node-controller 本地 SQLite(§8.73,判定不迁移) |

结论:**16 条集中在同一处**,32 条共用同一个原语,51 条已判决。

**但这 48 条不是 #1100 能自己消掉的**。核对后确认:`claims` 原语的语句全部落在
`migrations/0003_portable_claim_buckets.sql` 定义的 canonical 表上(`coordination_jobs` 用
`TIMESTAMP(6)`、`bucket_no` 是列、`job_events` 是连续 `event_seq`),而生产派发链路的
`job_dispatch_postgres` 仍在草案表的 epoch 微秒列上跑。`0003` 的注释把两者之间的换算明确划给
**数据搬迁(#1102)**:

> Claim expiry is UTC TIMESTAMP(6); the draft schema stored epoch microseconds. The conversion
> belongs to the data migration (#1102), not to this schema definition.

所以「派发领取改接 `claims`」是 #1102 的切换动作,不是 #1100 的语句改写;`advisory-lock` 32 条同属
这条切换线。**#1100 的可动面到此为止**:剩下的每一条要么已判决不是债,要么等 #1102 的
canonical 切换。这条边界要认清,否则会拿「改接原语」当 #1100 的进度,而实际是在提前做 #1102。

若将来启动这条切换,它是协调事务变更:先读 `speechnexus-coordination-change`,准备 bucket → Job →
outbox 的锁序 oracle,并在切换前给出草案表到 canonical 表的列级换算与回退演练。

#### 8.74.1 `sql-string-building` 的 43 条:42 条标识符/语句文本 + 1 条值插值(已消)

§8.74 把这一族记成「已逐条判决」,却没有把**标识符插值**与**值插值**分开,所以 43 这个数字读不
出还剩多少真缺陷。逐条重判(每条 f-string 的每个 `{…}` 都追到定义点)后的构成是:

| 分类 | 条数 | 判据 |
| --- | ---: | --- |
| 标识符(name 位置) | 23 | schema/role/表名/视图名/列清单。来自模块常量或 `checked_sql_name`;任何方言都不能把 name 位置换成占位符 |
| 语句文本(片段与占位符形状) | 19 | 投影、时钟、谓词、CASE,以及由词表长度派生的 `?` 个数。每段都是同一判定的单一定义点,内联等于把同一段写 N 遍 |
| 值(值位置) | 1 | 台账表名落进 `c.relname IN (…)`——那是**值位**,不是 name 位 |

即 **43 findings = 42 identifier-only(must stay)+ 1 value(fixed)**。值的判据沿用 #1194 的口径:
被插进值位置的是**数据**而不是 SQL 结构,旧写法靠引号把它变成字面量,于是值里出现 `'` 就改写
语句。逐文件:

| 文件 | 条数 | 分类 | 闭集依据 / 为什么必须保留 |
| --- | ---: | --- | --- |
| `operator_attested_qualification.py` | 1 | **值(已修)** | `_LEDGER_RELATIONS` 是词表,放进 `IN (…)` 是值位;改为绑定参数 |
| `speech/infrastructure/admission_sql.py` | 2 | 语句文本 | `columns` / `assignments` / `placeholders` 由 `DIMENSIONS` 常量派生;列清单与 `?` 个数同源 |
| `speech/infrastructure/asr_settlement_debt.py` | 2 | 语句文本 | `_NOW = "CAST(? AS BIGINT)"` 本身就带占位符,插的是「现在」位置的表达式 |
| `speech/infrastructure/asr_settlement_preflight.py` | 1 | 语句文本 | `_ROOT_ABSENT` 是一条判定(结算根不存在)的单一定义点,不是值 |
| `speech/infrastructure/asr_settlement_schema.py` | 2 | 标识符 | `schema` / `role` 先过 `checked_sql_name`(`[a-z][a-z0-9_]{2,62}`),DCL 里不能参数化 |
| `speech/infrastructure/builtin_voice_qualification.py` | 1 | 语句文本 | 插的是 `qualification_source_state_sql('q')` 的 CASE,该 CASE 是「资格来源状态」的单一定义点 |
| `speech/infrastructure/deletion_executions_postgres.py` | 1 | 语句文本 | `(?, ?, ?)` 的组数由 `_PRIVILEGES` 常量元组长度派生,行内容本身不可字面化 |
| `speech/infrastructure/deletion_plans_postgres.py` | 3 | 2 标识符 + 1 语句文本 | `_PLAN_COLUMNS` 是列清单、`DELETION_PLANS` 是 `deletion_plan_schema` 的表名常量;`VALUES` 占位符组数同 `deletion_executions` |
| `speech/infrastructure/job_cancellation_fence.py` | 2 | 标识符 | `TABLE` = `CANCELLATION_FENCE_TABLE`,来自 `job_cancellation_fence_schema` 的 `FROM`/`INSERT INTO` 位置 |
| `speech/infrastructure/job_dispatch_postgres.py` | 1 | 标识符 | `NOT_SUBMITTED_TABLE` 是 schema 模块常量,内联等于给这张表第二份定义点 |
| `speech/infrastructure/public_job_admission.py` | 1 | 语句文本 | 同上:同一个 `qualification_source_state_sql('q')` |
| `speech/infrastructure/qualified_speech_facts_postgres.py` | 2 | 1 标识符 + 1 语句文本 | 授权行的 `schema`/`role` 过 `checked_sql_name`;另一条是同一个来源状态 CASE |
| `speech/infrastructure/readiness.py` | 5 | 语句文本 | `_SCHEDULE_STATE_SQL` / `_RETRY_COLUMNS_SQL` / `_NOW_CLOCK` 由四条读共用;`_SQL_VERIFIED_STOP_EVIDENCE` 已是词表派生的参数占位符(值早就绑进去了) |
| `speech/infrastructure/tts_settlement_preflight.py` | 1 | 语句文本 | `_TTS_KINDS` / `_TTS_COMMANDS` 是手写谓词,`_ROOT_ABSENT` 是判定,`_PUBLIC_SETTLEMENT_GRACE_US` 是单一定义点的常量 |
| `speech/infrastructure/tts_settlement_schema.py` | 4 | 标识符 | 同 `asr_settlement_schema`:四类 DCL 的 `schema` / `role` 都过 `checked_sql_name` |
| `speech/infrastructure/voice_capability_evidence_postgres.py` | 1 | 标识符 | `EVIDENCE_TABLE` = `speech_voice_capability_evidence`,来自 `voice_publication_schema` |
| `speech/infrastructure/voice_catalogue_postgres.py` | 4 | 标识符 | `VOICE_READER_VIEW` 是 `voice_catalogue_schema` 的视图名常量,`FROM` 位置 |
| `workbench/infrastructure/postgres_assistant_turns.py` | 1 | 标识符 + 语句文本 | `_COLUMNS` 是列清单常量;`suffix` 只有 `""` / `" FOR UPDATE"` 两个取值 |
| `workbench/infrastructure/postgres_exports.py` | 2 | 标识符 + 语句文本 | 同上;`{s}` 是留给 `sql.SQL(...).format(s=…)` 的 schema 占位,不是已发生的插值 |
| `workbench/infrastructure/postgres_saved_assets.py` | 1 | 标识符 | `_COLUMNS` 列清单常量 |
| `workbench/runtime.py` | 2 | 语句文本 | `scope` 只有 `""` / `" AND c.relname = ANY(?) "` 两个取值,两段都自带占位符 |
| `execution-gateway/…/cancellation_fence_sql.py` | 1 | 标识符 | `JOB_CANCELLATION_FENCE_TABLE` 来自 `speechnexus_contracts.cancellation_fence` |
| `execution-gateway/…/reader_authority.py` | 1 | 标识符 | `schema` 先过 `re.fullmatch(r"[a-z][a-z0-9_]{2,62}")`,同一值还作为 `n.nspname=?` 绑定 |
| `node-controller/…/sqlite_intent_store.py` | 1 | 语句文本 | `?` 个数由 `RuntimeIntentState` 枚举里 `is_unresolved` 的成员数派生(§8.73 的本地库判定不变) |

**那 1 条值插值。** `_LEDGER_RELATIONS`(13 张资格台账表)原先用
`",".join("'" + name + "'" …)` 做成 `'a','b',…` 拼进两处 `c.relname IN (…)`。这不是 name 位置:
`c.relname` 是目录表里的文本列,比较的是**值**,所以它能参数化,也应该参数化。现在的形状是
`_SQL_RELATIONS`(占位符个数)与 `_RELATION_PARAMETERS`(绑定值,两处 `IN` 各一遍)都从
`_LEDGER_RELATIONS` 这一份定义点派生,与 `readiness` 里 `VERIFIED_STOP_EVIDENCE_KINDS` 的绑定同形;
`tests/unit/backend/test_operator_attested_qualification.py` 逐条核对「占位符数 = 2×词表」且
「词表名字不出现在语句文本里」,重引号化即红。

真库 oracle(隔离库 `speechnexus_test`,探针角色 `sn_interp_probe` 无任何授权,跑完即删):

| 场景 | 结果 |
| --- | --- |
| shipped 语句 + 真实词表 | `forbidden=False`(无授权身份不应被判为禁止身份) |
| shipped 语句 + 前三个值换成 `O'Brien` / `back\slash` / `100%` | `forbidden=False`,**无报错** |
| 旧形状(把 `O'Brien` 拼进词表) | `SyntaxError: syntax error at or near "Brien"` |
| 旧形状(值改成 `x') OR TRUE OR c.relname IN ('y`) | `forbidden=True`(基线是 `False`:谓词被改写) |
| 绑定值逐个匹配临时表名 `O'Brien` / `back\slash` / `100%` | `(1, 1, 1)`,引号、反斜杠、百分号都按字面比较 |

**为什么守卫基线不因此下降。** 这条修正消掉的是「值进文本」,不是「语句文本可静态登记」:占位符
个数必须跟着词表走,所以 f-string 还在,`sql-string-building` 仍是 1 条。这与 #1194 的 3 条值修复
一致——那三条改成绑定参数后,`readiness` 的 5 条也没有减少。本轮基线保持 **164**,`new=0
fixed=0`;台账里该文件的 `sql-string-building` 条目继续有效(键还在基线里,删掉会让守卫报
`unregistered`),只把 reason 改成本次判决。

**两条读数的边界。** 一是这条规则的视野只有 f-string:`qualification_source_evidence.py` 用
`"THEN '" + STATE_EVIDENCED + "'"` 拼的四个状态标签同样是值位置的字面量,守卫看不见;#1194 已把
那个 CASE 判为「语句文本」(单一定义点),本轮沿用,但它不是「不存在值插值」的证据,只是「这四处
已判决」。二是 `admission_sql` 的 `placeholders` 生成的是 `%s` 而不是 `?`,那是 `format` 族的
标记问题(不在本轮 43 条内),留待 #1102。

### 8.75 #1102 的切换顺序与每个步骤的判据

搬迁不是「跑一个脚本」,而是五步,每步都有独立的、会红的判据。顺序不可交换:bootstrap 提供
`schema_meta`,换算需要它;对账要在提交之后,否则看到的还不是要上线的状态。

| 步骤 | 命令 / 入口 | 判据 |
| --- | --- | --- |
| 1. 建 schema | `scripts/dev-env/portable-schema.py --dsn … --schema …` | `--check` 在空库上非零退出;重跑幂等;某版播种版本与文件名不符时该版整体回滚 |
| 2. 换算行 | `claims.canonical_job_from_draft()` | 0 → `NULL`、`bucket_no = bucket_for(job_id)`、`due_at` 存 **naive UTC**(会话时区不影响结果) |
| 3. 搬事件 | `claims.adopt_historical_events()` | 序号从 1 连续;摘要与 `event_digest()` 逐条相等;对已有事件的 Job 拒绝 |
| 4. 对账 | `scripts/dev-env/portable-reconcile.py --dsn …` | 任一处字段不符即非零退出并点名;`claim_version` 判「只增不减」 |
| 5. 回退 | 撤掉 canonical 侧全部表,重跑 1–4 | 重跑结果与一次跑完**完全相同**;草案侧行数不变 |

三条顺序上的事实:

- **换算返回 naive UTC 不是风格选择。**canonical 列是 `TIMESTAMP(6)`,把 aware datetime 交给
  驱动,存下来的是会话时区下的墙上时间;`+08:00` 会话里 `12:00Z` 会存成 `20:00`。真库演练
  (`tests/integration/test_canonical_job_conversion_pg.py`)刻意用非 UTC 会话证明这一点。
- **`claim_version` 会被搬迁推进。**搬事件时 `+1` 是作废旧系统在飞认领的 fencing 手段,不是
  搬迁错误;对账因此判「不小于草案」,并另有一条反向判据。
- **对账必须能挡住半成品。**只搬了一部分 Job 时,对账以 `missing from coordination_jobs`
  非零退出,这正是「切换不得继续」的信号。

演练入口:`tests/integration/test_portable_switchover_rehearsal_pg.py` 把五步串起来跑,
包括「丢掉 canonical 侧重来」这条回退路径。

#### 8.75.1 `job_events.payload_ref` 仍是未决项,需要产品决定

列级清单里 `payload_ref` 是 `gap`:`speech_job_events` 草案**不存 payload 引用**,所以搬迁没有
可换算的来源。三种可选口径,影响面不同:

| 口径 | 后果 | 需要谁决定 |
| --- | --- | --- |
| 全 `NULL` | snapshot/增量恢复时只能靠 `event_digest` 去重,大 payload 仍留在原处 | 产品 |
| 搬迁时补一列并回填引用 | 需要先确定「原处」是什么(对象存储 key?行内 JSON?),并改草案表 | 架构 + 产品 |
| 不存 payload,事件只表达状态迁移 | 与当前 `job_events` 的列语义一致,但恢复要靠客户端重取 | 产品 |

在这条决定作出之前,搬迁不得「顺手猜一个」:`canonical_source_map` 保持 `gap` 标记,
`test_gaps_and_derivations_carry_their_reason` 保证它不会悄悄变成已解决。

### 8.76 派发改接 claim bucket:先定「哪个 worker 认哪个桶」

协调契约已经冻结了**怎么认领**:`docs/architecture/coordination-transactions.md` 写明
「Job/outbox 使用 claim bucket,bucket 与 Job/outbox 分别以 version / owner / expiry CAS 认领;
每次实际认领都要在同一事务复核 bucket fence;不使用 `SKIP LOCKED`」。本文档只补契约**没写**
的那一半:**一个 dispatcher 一次轮询去看哪些桶**。这是调度策略,不是正确性选择——三种做法在
「不重复认领」上都成立,差别只在公平性、争用和坏 worker 的影响面。

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| A 固定分片 | `bucket_no % worker_count == index` | 不争用;但某个 worker 挂掉后它那片桶**再没人看**,只能等人工重排分片 |
| B 全桶轮询 | 每轮从 `bucket_for(owner_id + 分钟)` 起线性扫满 256 个桶,拿到空闲桶就认领 | 最坏 256 次 CAS;宕机的 worker 的桶租约一过期就能被接走 |
| C 静态偏好 + 溢出 | 先扫自己的偏好区间,剩余额度再扫全表 | 常见路径快;实现与测试多一层 |

**采用 B。**理由:方案 A 的失败模式(整片桶永久无人认领)需要运维介入才能恢复,而项目边界里
没有「调度器再平衡」这一层;方案 C 的收益只在桶远多于 worker 时才明显,当前 worker 数量级
远小于 256 个桶,省下的扫描不构成瓶颈。B 的最坏代价是每轮最多 256 次带版本条件的 `UPDATE`,
与「每轮全表 `SKIP LOCKED` 扫描」同阶。

#### 8.76.1 一次轮询的事务形状

1. 读 `schema_meta` 探针(见协调事务契约「先取得专用探针连接」)确认 schema 与部署代次;
2. 开写事务,按 `bucket_for(owner_id + 分钟)` 起的顺序对每个桶做 `acquire_bucket`
   (`expected_version` 取上一轮读到的值,拿不到就跳过该桶);
3. 拿到桶后**立刻**用该桶的 fence 版本调 `claim_next`/`claim_job` 认领,认领数计入本轮 `limit`;
4. 认领完就 `release_bucket`,不把桶租约带到下一轮:`limit` 是**本轮认领数**,不是桶数;
5. 达到 `limit` 或扫满 256 个桶即结束;整笔提交。

fence 复核与 Job CAS 在同一事务,旧 dispatcher 拿到的过期 fence 版本会被 `require_fence` 拒绝,
不会出现「旧 owner 认领了新候选」。

#### 8.76.2 需要新增的 canonical 语句(都只用 `?` 标记)

现有 `job.candidates` 只按 `bucket_no + job_state + due_at` 取,没有 lane(`kind`)过滤,也没有
准入锚点。生产派发需要两者,所以 B2 要新增两条 manifest 语句,而不是在业务层拼 SQL:

| 语句 | 作用 |
| --- | --- |
| `job.candidates.lane` | 在 `job.candidates` 上追加 `AND kind IN (…)`(列表由驱动层展开) |
| `job.candidates.admitted` | 追加准入锚点:`EXISTS` 于 `admission_operations`(请求文档非空、摘要一致)与 `admission_leases`(reserved/executing) |

两条都必须是可移植子集内的写法:`IN (?)` + `ListMarker`(MySQH 路径已在 #1100 第十九轮补齐展开)、
普通 `JOIN`/`EXISTS`,不用 `LIMIT`/`WITH`/`RETURNING`/`SKIP LOCKED`。

#### 8.76.3 这一步消掉什么、不消什么

按 `scripts/dev-env/portable-sql-baseline.json` 逐条清点(不是按印象估):

`job_dispatch_postgres.py` 一共 **16** 条,并按方法归属清点(AST 定方法,合计与 baseline 的
per-rule 计数完全一致):

| 方法 | 条数 | kinds |
| --- | ---: | --- |
| `PostgresDispatchQueue._claim` | **5** | `cte`、`returning`、`skip-locked`、`vendor-operator`、`format` |
| `_mark_running` | 2 | `cte`、`returning` |
| `_renew` | 2 | `cte`、`returning` |
| `_record_progress` | 2 | `cte`、`aggregate-filter` |
| `_record_cancellation_not_submitted` | 1 | `sql-string-building` |
| `_finish` | 4 | `cte` 2、`returning` 2 |

**所以接 `_claim()` 的那一刀只消 5 条,基线 196 → 191;另外 11 条属 state store 切 canonical
的切片——不是改列名能消的,要新增 manifest 语句。**

初稿写的「16 条 claim 族 + 32 条 `advisory-lock`,196 → 148」是错的:那 32 条 `advisory-lock`
分布在 24 个文件里,主体是发布/资格/绑定/准入/取消 fence(`builtin_voice_publication`、
`qualification_probe_*`、`binding_lifecycle_postgres`、`public_jobs_postgres` 等),
`job_dispatch_postgres.py` 里**一条都没有**。把不相干的条目算进这一步的收益,会让后续每一刀
的预期都虚高。

**不消**:`runtime_delivery_sql` 的 `skip-locked`(另一条 outbox 投递链路,单独切片);
`advisory-lock` 全族(需要各自判断是「继续支持则迁移」还是「确定不用则删除」,属 #1100 的
判定工作,不是本切片)。

#### 8.76.4 判据(真库,注入即红)

| 场景 | 判据 |
| --- | --- |
| 两个 dispatcher 抢同一桶 | 只有一个 `acquire_bucket` 返回 true,另一个跳过且本轮不认领该桶的 Job |
| 过期 fence | 用上一轮读到的旧版本调 `claim_next` 必须被 `require_fence` 拒绝 |
| Job CAS 竞争 | 两条事务读同一候选,只有一个 `claim_job` 返回 true |
| lane 过滤 | 队列里混入别的 `kind`,本轮只认领本 lane 的 |
| 准入锚点 | 缺 `request_document` / lease 非 reserved-executing 的行不被认领 |
| 租约释放 | 认领后桶可被另一 owner 立即拿到,不残留租约 |

#### 8.76.5 实施反馈:三处按字面写会出错的地方

原语落地时按 §8.76 直接实现会撞到三件事,记录在此,接线那一刀按修正后的结论做。

**A. 候选语句是四象限,不是两条。** 生产的 `kinds` 与 `require_admission` 是两个独立开关,
(有 lane ∧ 要准入) 才是生产实际那一档。业务层不许拼 SQL,所以 manifest 里必须同时有
`job.candidates.lane`、`job.candidates.admitted`、`job.candidates.lane_admitted` 三条,
外加读回 Job 字段的 `job.claimed_view`。

**B. §8.76.1 第 3 步的「过期 fence 会被 `require_fence` 拒绝」在 `claim_batch` 内不可达。**
`bucket.fence` 是 `SELECT … FOR UPDATE`,同事务内桶行不会变;跨事务的旧版本由
`acquire_bucket` 的 `version = ?` CAS 拒绝。注入实测:删掉复核后 11 条真库判据全绿,只有语句
形状单测变红。复核仍然保留,但它的定位是**不变量断言**(拦「行锁只覆盖单条语句」的驱动,
以及将来把 acquire 与 claim 拆两个事务的改动),不是并发判据——§8.76.4 里「过期 fence 被拒」
那条只能在 `claim_next` 上观测。写判据时不能把这两件事混成一条。

**C. 候选谓词与生产 `_claim()` 的 WHERE 不等价。** 生产还有两条 canonical
`job.candidates` 没有的条件:`speech_jobs.state IN ('pending','running')` 与
`coordination_jobs.claim_expires_at_us <= now`(到期可重领)。后果有两面:
canonical `job_state='pending'` 但公共状态已终态的 Job 仍会被认领;过期 claim 的回收目前只
体现在 `job_state` 推成 `claimed`,与「到期可重领」不等价。**接线前必须先定这两件事**,
否则是两套语义被当成一套。

**D. `bucket.fence … FOR UPDATE` 会等待。** 「跳过而不是等待」只对**已提交**的租约成立:
持有者事务还开着时本调用会等它的行锁,而 `SKIP LOCKED` 被契约禁止。接线时要么把
「等锁」计入 deadline 并显式超时,要么让桶租约在认领事务之外持有——后者与 §8.76.1 第 4 步
冲突,需要重新决定。

**E. 签名只有一个 `lease_until`。** 它同时做桶租约与 Job claim 到期;桶租约返回前释放所以
不残留,但「桶租约短、Job 租约长」表达不了。生产 `claim_due(lease_ms)` 用的是后者,接线时
要么拆两个参数,要么接受同值。

#### 8.76.6 到期重领:用一个显式语句,不把语义压进候选谓词

§8.76.5 的 C 指出候选语句与生产 `WHERE` 不等价。查清之后,两边的差别其实是一条**语义**,
不是一条谓词:

| | 草案(生产今天) | canonical |
| --- | --- | --- |
| 认领时改什么 | `claim_owner_id`、`claim_version`、`claim_expires_at_us`,**`job_state` 不动** | `job_state = 'claimed'` |
| 什么决定「可再领」 | `claim_expires_at_us <= now` | `job_state = 'pending'`(候选谓词) |

两边都能保证「同一时刻只有一个 owner」,但**存活含义不同**:草案里租约一过期,任一派发就能
重领;canonical 里 Job 停在 `claimed`,没人会再碰它——**崩溃 worker 的 Job 会永久卡住**。

三种收敛方式:

| 方案 | 做法 | 问题 |
| --- | --- | --- |
| 1 保留草案语义 | 认领时也传 `next_state='pending'`,靠 `claim_expires_at_us <= now` 决定可领 | 候选语句要新增到期谓词,canonical 的 `claimed` 状态形同虚设 |
| 2 把到期折进候选 | 候选改成 `pending AND due_at<=now OR (claimed AND claim_expires_at<=now)` | `OR` 与 keyset 游标混在一起,分页边界难证明;一条语句承担两种判定 |
| 3 显式重领语句 | 认领批次开始前,先在该桶执行一条 `job.reclaim` | 多一条语句、多一次写 |

**采用 3。** `job.reclaim`:`job_state='claimed' AND claim_expires_at <= ?` →
`job_state='pending'`,清空 `claim_owner_id`,并**把 `claim_version` 加一**。理由是它把两件事
分开:候选语句继续只回答「哪些 Job 到期可派」,重领只回答「哪些租约已经死了」。

`claim_version` 加一是关键,不是顺手:草案里**过期本身就是 fence**(旧 owner 的 CAS 里带
`claim_expires_at_us <= now`,过期必然失败);canonical 里旧 owner 的 CAS 带
`claim_version = ?` 与 `job_state='claimed'`,若不涨版本,重领后旧 owner 仍可能写成功。
涨版本让「重领」对旧持有者等价于一次 fence 失效。

判据(真库,注入即红):

| 场景 | 判据 |
| --- | --- |
| 过期可重领 | 租约过期的 `claimed` Job 在下一轮被重领,`claim_version` 连涨两次 |
| 未过期不可抢 | 租约仍有效的 `claimed` Job 不被重领 |
| 旧 owner 被 fence | 重领后旧 owner 用原版本 CAS 必须失败 |
| 重领不越桶 | 只在当前持有桶内执行,不改别的桶的 Job |

这条与 §8.76.1 第 2 步的顺序固定成:**读 fence → 拿桶 → 复核 → 重领该桶 → 取候选 → 认领**。

### 8.77 派发认领接线的前置:四条,顺序不可换

`claim_batch` 原语已经完整(§8.76),但把生产 `_claim()` 换过去不是「一行替换」:认领谓词依赖的
三样东西在生产 schema 里都不成立,再加一条授权。按依赖排序:

| 序 | 前置 | 缺什么 | 谁决定 | 不做会让哪条判据红 |
| --- | --- | --- | --- | --- |
| 1 | 桶表与播种(alembic 0066) | `coordination_claim_buckets` 只在 canonical schema;alembic 树到 0065 既没这张表也没 256 行种子。派发跑的是 alembic schema | 实现。种子值取自 Python 的 `BUCKETS`,不在 SQL 里写死 256 | 所有 `claim_due` 判据:`read_fence` 42P01 或返回 `None` → 零认领 |
| 2 | `due_at` 落到写入者 | 只有 0064/0065 的存量回填;两处 INSERT 都不写它 → 新建 Job `due_at IS NULL`,而候选谓词是 `due_at <= ?` | **来源不是待决项**:`canonical_source_map.json` 已冻结为 `speech_jobs.created_at_us`。要评审的只是实现口径:时间戳怎么进 `CreateJob`,以及 `created_at_us` 是否恒等于 `submitted_at_us`(不能假定) | 新 Job 永不被认领 |
| 3 | 租约列与状态切换(**双写边界才是真待决项**) | 原语写 `job_state='claimed'` + `claim_expires_at`;`_renew`/`_record_progress`/`_finish` 读 `job_state='pending'` + `claim_expires_at_us`。且 0065 重跑方向是 canonical ← draft,只写 canonical 会在下次迁移把在飞租约擦成 NULL | 架构。三种边界:(a) 全量双写到草案列退役;(b) 只写 canonical 并把 0065 改成以 canonical 为准;(c) 三个守卫与草案列一起切 canonical | 认领后立刻失权:renew 返回 `None`、finish 抛 `DispatchClaimLostError` |
| 4 | 列级 GRANT 与角色 | 派发角色只有 4 列 `UPDATE`,桶表与 `claim_expires_at` 都不在授权里 | 实现。列清单同时写在 `coordination_grants.py` 与 14 个 fixture 里,改时要收敛到一个定义点 | 42501、授权面清点 |

顺序 1 → 2 → 3 →(4 与接线同一提交)。1/2 只决定「有没有候选」,3 决定「认领后还能不能干活」;
把 3 拖到接线之后,等于让每一次认领都变成一次立即失权。

两条口径澄清(避免把「待决」和「已定」混成一条):

- `due_at` 的**来源不是待决项**,`canonical_source_map.json` 已冻结;待办是把它落到写入者。
- 数据保真的争议**不是来源**,是同步方向:哪一列权威、0065 的重跑覆盖哪个方向——这才是双写
  边界要定的内容。

接线时必须同时记录的三个判断:

- `statement_timeout` 是**逐语句**的,而一次 `claim_batch` 最多扫 256 个桶(每桶 4–6 条语句)。
  §8.76.5 D 接受「桶行等锁由 `statement_timeout` 兜住」,但整轮总预算不等于单条预算:要么显式
  给整轮 deadline,要么承认最坏情况是 256 × `statement_timeout`。
- `ClaimedJob.lease_expires_at_us` 现在由数据库算出;改由 `BatchClaim.expires_at`(naive UTC
  `TIMESTAMP(6)`)换算时必须逐微秒相等,否则 `_renew`/`_finish` 的守卫整体偏移。
- `job.claimed_view.request_document` 可空,而 `DispatchJob.request_document` 是非空 `str`
  (老 SQL 用 `COALESCE(…, '')`);接线要显式映射 `None → ""`,并把 `claimed_jobs` 的顺序还原
  成老语句的 `created_at_us, job_id`。

### 8.78 取消栅栏去掉 advisory lock:判定、替代锁与三条真库判据

用户 2026-11-22 的决定是「不用的机制删除,但删除要有代码证据;有写者不满足就换成可移植的
行锁」。按写者集合与读侧逐条核过后,**结论是保留一把锁,改成 Job 行锁**,不是删除。

**写者只有一个,且已不能并发。** `write_cancellation_fence` 的唯一生产调用者是
`PostgresPublicJobs.cancel`;它进事务后先 `_lock_cancel_target` 取 `speech_jobs` 行锁
(`FOR UPDATE OF j`),写 receipt 后才写围栏。同一 Job 的第二个受理在行锁上等待,醒来先看到
receipt 走 replay,不会再写围栏;`ON CONFLICT (job_id) DO NOTHING` 与主键是第二道。所以
「两个并发取消留一行围栏」不依赖 advisory lock。

**但 advisory lock 不是写者互斥,是写者↔读者排序。** 读侧是
`execution_gateway.infrastructure.cancellation_fence_sql.cancellation_fence_present`:它先取
同一把 advisory lock 再读围栏,让「提交」与「受理」只能一方先提交。这条链上**没有任何一方
持有 Job 行锁**(网关创建执行时只锁 `resource_pools` / `admission_leases` /
`runtime_instance*`),所以删掉 backend 的锁、只留网关那一半等于一把只有一方取的锁——什么
也不排除。删除分支因此不成立;`test_acceptance_lock_waits_for_submit_and_then_refuses` 也
正是这条排序的判据。

**替代锁:两侧都取 canonical Job 行锁。**

- 写者:`PostgresPublicJobs.cancel` 在事务开头调 `lock_cancellation_fence`,它是
  `SELECT job_id FROM coordination_jobs WHERE job_id = ? FOR UPDATE`;`write_cancellation_fence`
  再取一次作直接调用者的兜底(同事务重入不等待)。
- 读者:`cancellation_fence_present` 先取同一行,再读围栏。PostgreSQL 的 `FOR UPDATE` 要求
  行上某列 `UPDATE` 权限(四种行锁子句都一样,实测 PG 18.6),网关角色由
  `coordination_runtime_grants` 拿到 `UPDATE (claim_version)` 行锁许可,不写这一列。它与
  `GATEWAY_UPDATES` 里既有的 `UPDATE (version)` 同族——该函数自己写明「只有 version 列
  UPDATE 跨所有权,专为行锁」。残余能力是**失败关闭**的:误推进 fencing 计数只会让当前
  owner 的 CAS 失配、Job 在租约到期后被重领;`claim_owner_id`/`claim_expires_at`/`job_state`
  仍被拒,网关不能认领、续租或关闭。`test_production_claims.py` 的
  `test_gateway_can_only_bump_the_fencing_counter_on_the_job_row` 按这四列钉住边界。
- 锁序:仍是 bucket → Job → outbox。受理是 canonical Job → `speech_jobs` → receipt → 围栏;
  `_finish`/`_record_progress` 也是 canonical Job → `speech_jobs`;网关是 authority 行 →
  canonical Job。没有路径先持 `speech_jobs` 再等 canonical Job,因此不成环。

**canonical 状态闭合同一提交完成。** claim 只读 `coordination_jobs`,围栏不参与认领谓词;
受理只改 `speech_jobs` 时,`job_state` 仍是 `pending`,围栏已提交的 Job 会被认领、再因公共
状态终态被跳过(实测:受理后 `job_state='pending'`,claim 返回该 Job)。所以受理在写 receipt
后把 `coordination_jobs.job_state` 置 `cancelled`:它是派发处置,不是停止证据(公共状态在
`cancelling` 上等停止证据)。这样 claim 的 `job_state = 'pending'` CAS 在行锁醒来后必然失败,
「围栏已提交的 Job 不再被认领」由认领谓词本身保证,不需要 `claims.py` 认识 backend 表。

**三条判据与注入**(`tests/integration/test_cancellation_fence_claim_pg.py`,真 PG、真角色):

| 判据 | 注入 | 红在哪 |
| --- | --- | --- |
| 受理后不再被认领 | 删 `cancel` 里的 canonical `job_state='cancelled'` 更新 | claim 返回已取消的 Job,`claimed == []` 失败 |
| 领取后被取消,`_finish` 拒绝 | 把 `_finish` 三处状态谓词改成 `… OR TRUE`(canonical 守卫、公共状态 CAS、canonical close) | `pytest.raises(DispatchClaimLostError)` 失败,`_finish` 成功关闭 |
| 两个并发取消只留一行、都收敛到首次受理 | 删 `cancel` 的 receipt replay 短路 | 第二个受理插入第二张 receipt,主键冲突 → `unknown`,线程捕获异常,`errors == []` 失败 |

三注入均实测:第 1 条在 `claimed == []` 红(claim 返回已取消的 Job);第 2 条在
`DID NOT RAISE` 红(`cancelling -> failed` 被成功关闭;`succeeded` 由公共状态触发器拒绝,
所以先试合法的 `failed`);第 3 条在线程捕获 `JobOutcomeUnknownError` 红(第二张 receipt
触发主键冲突)。每次注入后按 sha256 恢复原文件并逐字节核对一致,注入前后清理 `*.pyc`。

围栏行数本身由围栏主键与 `ON CONFLICT` 保证;第三条注入打的是「第二个调用者是否收敛到首个
事实」,不是「能不能多插一行」——后者只能靠删主键制造,不是这段代码的回归形态。

守卫基线因此减 2 条 advisory-lock:
`backend/.../job_cancellation_fence.py` 1 条、`execution-gateway/.../cancellation_fence_sql.py`
1 条,181 → 179,`new=0`。没有新增 advisory lock、vendor 分支或 `SKIP LOCKED`。

### 8.79 workbench 版本边界按契约类别分类:`VersionStoreError` 不再透传 SQLSTATE

§8.6 定义的形状漏在 workbench 版本边界上的最后 3 处,基线 169 里的
`vendor-error-branch` 全部属于这里:应用层 `version_ports.py` 2 处(参数名与
`self.sqlstate` 赋值)、基础设施 `postgres_versions.py` 1 处(`error.sqlstate` 参与
`commit_unknown` 判定)。三层里只有第一层是纯诊断字段,第三层却真的按厂商码分支,而
「同一个逻辑冲突在另一引擎上是否被识别」正取决于这处分支。

**收口后的形状。**

- `VersionStoreError` 只带 `failure: PortableSqlError | None` 与 `commit_unknown`。
  `failure` 是驱动已经翻好的契约类别:重复/冲突写是 `ConflictError`,存储级拒绝是
  `StorageError`(其 `reason` 说明补授权还是重跑迁移)。应用与接口都按这个类型判断,
  不再有 `sqlstate`、驱动类或驱动消息可比。
- `postgres_versions.transaction()` 只调一次 `portable_error(error)`。SQLSTATE 仍然只在
  `psycopg_driver._reason_of` / `portable_error` 被读,业务侧没有第二处。
- `commit_unknown` 的判据随之改成契约类型:`ConflictError` 是唯一能证明服务器中止了事务
  的拒绝;连接丢失、后端关闭、序列化失败/死锁、锁超时、语句完成未知与未分类失败都报
  uncertain。方向是保守的——不确定时报「结果未知」,恢复路径仍是按原 scoped key 重查。
  代价是 COMMIT 阶段才出现的 40001/40P01 由「确定中止」变成「未知」(psycopg 把 class 40
  归入 `OperationalError`,契约只能看到 `ConnectionLostError`);没有用例依赖旧判定,而错误的
  方向不会把不确定说成确定。

**两条真库判据**(`tests/integration/test_workbench_versions_postgres.py`,真 schema、真 runtime 角色):

| 行为 | 构造 | 收口前 | 收口后 |
| --- | --- | --- | --- |
| 重复/冲突的版本写 | 同一 scoped receipt 连写两次,撞主键 | `sqlstate='23505'` | `isinstance(failure, ConflictError)`,`commit_unknown=False` |
| 存储级拒绝 | 从 runtime 角色 `REVOKE INSERT ON project_versions`,与冲突无关 | `sqlstate='42501'` | `StorageError` 且 `reason='permission_denied'`,不是 `ConflictError` |

两种情形抛出的仍是同一个 `VersionStoreError`,接口层仍是 503 `store_unavailable` /
`outcome_unknown`(单测 `test_workbench_http_error_fallback.py` 覆盖),用户可见行为不变。

**注入即红**(改 `psycopg_driver.portable_error` 的分类,改前改后按 sha256 核对一致,
注入前后清理 `*.pyc`):

- 唯一/外键冲突不再返回 `ConflictError`(全部降级成 `StorageError`)→ 冲突判据在
  `assert isinstance(error.value.failure, ConflictError)` 红。
- 所有 `psycopg.Error` 都返回 `ConflictError` → 存储判据在
  `assert not isinstance(failure, ConflictError)` 红。

守卫基线 169 → 166(`version_ports.py` 2、`postgres_versions.py` 1),`new=0`;登记表
107 → 105,两条 `vendor-error-branch` 登记随条目一起删除(留着会 `STALE-EXCEPTION` 判红)。
