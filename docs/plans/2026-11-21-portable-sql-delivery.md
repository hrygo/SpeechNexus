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
