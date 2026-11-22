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
