---
title: PR #934 返修的增量审查：三处必修是否真的修好
status: active
---

# PR #934（`fix/prerequisite-gaps-closeout`）返修的增量审查

被审对象：PR #934《fix: 七类前置的缺项点名收口审计》，分支 `fix/prerequisite-gaps-closeout`，
**旧 head `70a8c892`（PR #937 的审查对象）→ 新 head `8ae2edda`**。本次只独立验证返修，不重做整份审查；
三条必修的原文见 `docs/reviews/2026-11-17-prerequisite-gaps-closeout.md`（PR #937）。

**口径（本卡的关键）**：增量一律用 `git diff 70a8c892 8ae2edda`，不用两点 `origin/main` 口径——
`origin/main` 在审查期间已由 `1721ddb2` 前进到 `b1b8912b`，两点口径会把别人的提交算进本 PR。
测量写集时另外给出「整 PR 对分叉点」`git diff 1721ddb2 8ae2edda`，两者语义不同、不混用。

本审查分支 `review/934-delta` **从 `origin/main`（`b1b8912b`）新建，只放本报告**；被审 head 在独立
worktree `/private/tmp/sn942-exp` 上逐 commit 签出做变异，变异全部还原（§附录 B）。
环境：macOS arm64、Python 3.14.9（`uv sync --locked --all-packages --all-groups`）、pnpm 12.14.0
（`pnpm install --frozen-lockfile`，新 worktree 必须先装，否则 8 个 `[typescript]` 假红）、
隔离 PG `127.0.0.1:55480/sn892`（`PostgreSQL 16.16`）。核验时间 2026-11-17。

## 结论

**能合。** 三处必修都实测修好了，不是「在原本就会红的路径上补重复用例」：零状态句与 fail-open 两条变异
在旧 head `70a8c892` 上全绿、在新 head 上分别 `2 failed, 7 passed` 与 `1 failed, 8 passed`；增量
**只含测试**（1 文件 +67/−1，产品代码/契约/CI/文档零改动），作者「不改产品行为」的声明成立。M2 那句改写后的
逐条基线四个数字（`6 passed` / `7 passed` / `1 failed, 14 passed` / `15 passed`）我逐项重测，**全部与实测一致**，
原「补前基线全绿」的不实陈述已从 PR ③ 消失。

遗留只有一处**描述性数字**：PR ③ 第 2 行写「删掉 `publish the binding and ` → `1 failed`」，实测是
**`2 failed, 7 passed`**（fail-open 用例也断言了同一子句）。方向没错（确实变红），但证据表里的数字应与
最终树一致；改这一行、不需要改代码，可与合入同轮完成。

其余为「可接受但需记录」（§7）：#877 §4 头句「补前全绿」与第 4 条自述并置时读起来矛盾（第 4 条本身是对的）；
fail-open 用例的负向断言因 stub 带了 detail 而空转（一行可修）；「同义改写不会红」的自述不实（四个必答子句
是精确子串，`publish a binding revision` 这种等价改写会红）。

---

## 0. 复核记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 增量写集 | `git diff --stat 70a8c892 8ae2edda` | **1 文件 +67/−1**，全部在 `tests/unit/backend/test_job_precondition_naming.py` |
| 增量提交 | `git log --oneline 70a8c892..8ae2edda` | 单提交 `8ae2edda` |
| 整 PR 写集（对分叉点） | `git diff --stat 1721ddb2 8ae2edda` | 2 文件 +185/−1（与 PR ④ 自述一致） |
| 产品代码是否被夹带 | `git diff --name-only 70a8c892 8ae2edda \| grep -v ^tests/` | 无输出 |
| 新 head 目标文件基线 | `pytest tests/unit/backend/test_job_precondition_naming.py -q` | **9 passed**（旧 head 7 条 + 新 2 条） |
| 旧 head 目标文件基线 | 同命令（签出 `70a8c892`） | **7 passed** |
| 分叉点目标文件基线 | 同命令（签出 `1721ddb2`） | **6 passed** |
| 单测范围 | `pytest tests/unit -q` | **7512 passed, 3 skipped, 6 subtests passed**（171 s） |
| 单测+CI+契约 | `pytest tests/unit/backend tests/ci tests/contract -q` | **8065 passed, 2261 subtests passed**（400 s） |
| 集成用例基线 | `pytest tests/integration/test_tts_preflight_unmigrated_pg.py`（真 PG） | **1 passed** |
| 邻近单测基线 | `pytest tests/unit/backend/test_lease_settlement.py -q` | **15 passed** |
| 收尾 | 两个 worktree `git status --porcelain` | 空 |

## 1. 增量 diff 是否只含测试

**是。** `git diff 70a8c892 8ae2edda` 只有 `tests/unit/backend/test_job_precondition_naming.py`
（+67/−1）：一处 import 换成 `(JobCreateResult, PublicJobUnavailableError)`，其余全部是两条新用例。
`backend/`、`contracts/`、`deploy/`、`.github/workflows/ci.yml`、`docs/` 都没进增量，因此三处必修里
「不改产品行为」的声明**成立**，也没有「改产品迁就测试」的倒挂。

两处容易误读、但与事实不冲突的口径：

- 作者说的 **`2 文件 +185/−1`** 是**整 PR 对分叉点**的写集（`1721ddb2..8ae2edda`），不是本卡要的增量；
  增量是 1 文件 +67/−1。两者各自都真。
- `8ae2edda` 的提交标题是「补零状态句与 fail-open 判据，**改正 preflight 缺口表述**」，但 diff 里没有
  preflight 或文档改动——因为 M2 的不实那半句本来就在 **PR 描述 ③**，修复只能落在 PR 正文（以及 `#877` §4）。
  这一点是「修对了地方」，不是漏修；核查见 §3、§6。

## 2. 三处变异：旧 head 对新 head

变异都在 `sn942-exp` 上直接改产品代码、跑目标用例、`git checkout --` 还原；两向都做，方向与作者声明无关。

| # | 变异（我写的） | 旧 head `70a8c892` | 新 head `8ae2edda` | 红的用例 |
| --- | --- | --- | --- | --- |
| **M1** 零状态句 | `public_jobs.py:937` 删 `publish the binding and `（句子保留 “register the engine (engine-registration profile)”） | **`7 passed`**（绿 = 缺口真实） | **`2 failed, 7 passed`** | `test_the_zero_state_answer_names_both_missing_inputs_and_both_repairs` **和** `test_an_unreadable_registration_fact_keeps_the_422_answer` |
| **M3** fail-open | `public_jobs.py:920-925` 删整段 `try/except PublicJobUnavailableError: states = ()`，改回直接调用 | **`7 passed`**（绿 = 缺口真实） | **`1 failed, 8 passed`** | `test_an_unreadable_registration_fact_keeps_the_422_answer`（503 `job_unavailable` 顶替 422） |
| **preflight 文案** | `tts_settlement_preflight.py:96` 把 `run \`{MIGRATION_COMMAND}\`` 换成仍含 “migration profile” 的旧文案 `run the migration profile` | **`1 failed`**（集成用例；该守卫在 `70a8c892` 已存在） | **`1 failed, 15 passed`**（集成红、`test_lease_settlement.py` 全绿） | `test_an_unmigrated_database_names_the_migration_instead_of_the_driver` |
| 参考：`M1` 同函数另一臂 | `if states:` → `if False:`（分叉点 `1721ddb2`，只有 6 条用例） | **`6 passed`**（= ③「第 1 条变异」） | — | — |

读法：

1. **M1、M3 是真新增守卫。** 两条变异在旧 head 上都是全绿，在新 head 上都变红——不是重复用例。
2. **preflight 那条不在增量里。** 它在旧 head `70a8c892` 就已经会红（新集成用例是 `70a8c892` 加的），
   所以本卡要求的「补前是绿的」对**第三条不成立**；但对**分叉点**成立（`1721ddb2` 上该集成文件还不存在，
   同一变异下 `test_lease_settlement.py` **`15 passed`**）。作者修正后的 ③ 正是按这个拆开写的（§3 复核），
   没有把「旧 head 已红」说成「补前绿」。
3. **M1 的数字与 ③ 不一致。** ③ 第 2 行写 `1 failed`，实测 `2 failed, 7 passed`：`fail-open` 用例最后一条
   断言也是 `assert "publish the binding" in error["message"]`，同一句子被两条用例共同依赖。这一处只影响
   证据表的数字，不影响「这个子句现在有守卫」的结论。

## 3. M2 改写后的原文与实测

M2 要求的「逐条实测的四段表述」已落在 PR 描述 ③ 的「补前基线（分叉点 `1721ddb2` 实测，逐条）」段
与 `#877` §4。我把文中引用的数字全部自己重测（不是抽两条）：

| 文中数字 | 上下文 | 我的实测 | 一致 |
| --- | --- | --- | --- |
| `6 passed` | 第 1 条（`if states:` 臂）在只有分叉点用例时 | 分叉点删臂 → **`6 passed`** | ✓ |
| `7 passed` | 第 2 条（零状态句）在补到第 7 条用例时 | `70a8c892` 删 `publish the binding and ` → **`7 passed`** | ✓ |
| `7 passed` | 第 3 条（fail-open）同上 | `70a8c892` 删 try/except → **`7 passed`** | ✓ |
| `1 failed, 14 passed` | 第 4 条「归类本身有用例」 | 分叉点把 `except (InvalidSchemaName, UndefinedTable)` 收窄成只 `InvalidSchemaName` → **`1 failed, 14 passed`**（红的正是 `test_the_preflight_names_a_missing_migration_instead_of_a_driver_error`） | ✓ |
| `15 passed` | 第 4 条「换成旧文案时单元用例仍 15 passed」 | 分叉点换旧文案 → **`15 passed`** | ✓ |

原审查的 M2 指的正是「补前基线全绿」这半句不实；改写后的 ③ 已把它换成逐条实测，并且明确写出
「第 4 条要拆开看——归类本身有用例，补前真正缺的是真实驱动证据与消息里的共享迁移命令」。
**结论：M2 修好，改写后的正文与实测一致。**

## 4. 新用例的断言强度：子句级够不够

两条新用例断言的都不是整句相等，而是逐个子句出现（`test_the_zero_state...` 共 4 条正向 + 1 条负向）：

- 两个缺项都点名：`no published binding revision`、`no engine instance is registered`；
- 两个修复都点名：`publish the binding`、`register the engine (engine-registration profile)`；
- 反向区分臂：`has registered engine instance(s)` 不得出现。

我又加了两条**作者没做**的漂移方向来判强度：

| 附加变异 | 结果 | 说明 |
| --- | --- | --- |
| **点错项**：`register the engine (engine-registration profile)` → `register the model (model-registration profile)` | **`1 failed`** | 「点名了但点错项」**抓得住** |
| **等价改写**：`publish the binding` → `publish a binding revision`（仍点名同一修复） | **`2 failed, 7 passed`** | 「同义改写不会红」**不成立**——四个必答子句是精确子串 |

判断：**强度够，而且方向正确。** `#877` item 4 的缺陷是「文案不点名缺项」，这条用例把「缺什么 + 修什么」
逐项钉住，退回成笼统句、只点名一项、点错项都会红；负向断言还挡了「串到另一条臂」。代价是四个必答短语被
锁死措辞，`publish a binding revision` 这种无害改写也会红——这是**偏严**而不是偏松，且锁的正是运维词汇
（`publish the binding` / `engine-registration profile`），可接受；只需把「同义改写不会红」的自述改掉，
别让下一个人以为它可以随便改词。

## 5. fail-open 用例的真实性：stub 够不够

**够证明它要证明的那一件事，但有一处断言在空转。**

- **类型忠实**：应用层只依赖端口 `PublicJobFacts.engine_instance_states() -> tuple[str, ...]`。生产适配器
  `public_jobs_postgres._read()` 把 `psycopg.Error` / `PoolClosed` / `PoolTimeout` / `TooManyRequests` /
  `ValueError`/`TypeError`/`KeyError`/`PublicationUnavailableError` 全部折成
  `PublicJobUnavailableError(prerequisite_detail(error))`——所以 `_unpublished_binding` 能看到的只有
  「tuple」或「`PublicJobUnavailableError`」两种形态，stub 注入这一种异常**是端口契约允许的全部**。
  「不可读的注册事实不得把 422 换成存储答案」这个命题因此被证明。
- **变形不忠实（一处）**：真实池超时/连接中断时 `prerequisite_detail()` 返回 `None`，503 的句子是
  `Job storage is unavailable.`；stub 传了 detail 字符串，于是**变异下 503 的 message 是那句 detail**，
  用例里 `assert "Job storage is unavailable." not in ...` **在变异下根本没有被触发**，真正兜住的只有
  `assert 503 == 422`。我实测：把 stub 改成 `raise PublicJobUnavailableError()`（真实形态），同一变异下
  503 响应正是 `{"code":"job_unavailable","message":"Job storage is unavailable.",…}`——那条负向断言才会生效。
- **真实驱动的代价**：不能只让「第二次读」超时——`latest_binding` 先读，池不可用会一起失败，422 根本走不到；
  要真实驱动就得在适配器边界注入（等价于现在的 stub 再加一层壳），或在 PG 里构造「第一次读成功、第二次读失败」，
  成本中等（真 PG + 迁移 schema + 池耗尽/断连装置）、收益接近零。**不值得**。

建议（一行）：stub 去掉 detail 实参，或参数化「有 detail / 无 detail」两态；这比加集成用例便宜得多，
也能让用例里那句 `Job storage is unavailable.` 不再是空转。

## 6. `#877` 正文回写与最终代码的一致性

- **「两处缺口」→「四处」站得住。** 四处 = `_unpublished_binding` 的三条路径（`if states:` 臂、零状态句、
  fail-open）+ preflight 一条（真实驱动证据与共享迁移命令合并计一条，§4 已明说是「一条缺口的两件事，拆开是两件事」）。
  逐条与我的实测对得上：前三条缺口在旧 head 全绿（M1/M3 见 §2，`if states:` 臂见 ③ 的 6 passed），
  第四条以「归类本身有用例 `1 failed, 14 passed`」明确标注，没有再把「删掉归类仍全绿」写成事实。
- **§1 第 7 行的用例清单与代码一致**：「后三条为 PR #934 新增」指 `test_registered_instances_without_a_publication_name_the_publication_step`、
  `test_the_zero_state_answer_names_both_missing_inputs_and_both_repairs`、`test_an_unreadable_registration_fact_keeps_the_422_answer`，
  确实存在且都在新 head 上通过。
- **§5「未验证项」没有夸大**：本机复现、未在 MegaCube 重跑、没起真实 GPU 引擎这几条与原审查核对一致。
- **一处措辞要加限定（建议）**：§4 头句「四处原本没有用例挡回退（分叉点 `1721ddb2` 实测「补前全绿」）」，
  紧接着第 4 条自己就说「归类本身有用例（删掉 → `1 failed, 14 passed`）」。第 4 条是对的，头句的
  「补前全绿」只对四处缺口各自**定义的那个变异**成立；把「补前全绿」改成「前三处补前全绿，第四处见下」
  即可，避免读者再次把它读成 M2 当年那句。

## 7. 必须修 / 建议 / 未验证

### 必须修（描述，不动代码）

- **M1-数字**：PR 描述 ③ 第 2 行「删掉 `publish the binding and ` → `1 failed`」→ 实测 **`2 failed, 7 passed`**
  （或写明「只跑该条用例」）。同一子句被 fail-open 用例共同依赖，写 `1 failed` 会让人低估这条守卫的耦合面。

### 建议（都不阻塞合并）

- **A1** fail-open 用例的 stub 去掉 detail（§5），让 `Job storage is unavailable.` 那条负向断言真正生效。
- **A2** 删掉/改掉「按子句断言、不锁整句：同义改写不会红」的自述（§4 实测 `publish a binding revision` 会红）。
- **A3** `#877` §4 头句的「补前全绿」加限定（§6）。
- **A4** `#877` §4 第 2 条可以补一句新 head 实测（`2 failed, 7 passed`），与 ③ 数字统一。

### 未验证

- 没有重做整份审查：七行表的其余 11 条判据、启动闸门进程形态、`#877` 的 MegaCube 侧事实都沿用原审查
  （PR #937）的结论，本次只复核三条必修与受它们影响的陈述。
- 未跑 `tests/integration` 全量，只跑受 M2/M3 影响的那一个集成用例（真 PG 16.16）。
- 未在 CI 环境跑；`tests/unit`、`tests/unit/backend`、`tests/ci`、`tests/contract` 为本地实测。

## 附录 A：本次全部变异与实测输出（均已还原）

```
# 新 head 8ae2edda（基线 9 passed）
零状态句删 "publish the binding and "                 → 2 failed, 7 passed
  ├ test_the_zero_state_answer_names_both_missing_inputs_and_both_repairs
  └ test_an_unreadable_registration_fact_keeps_the_422_answer
删 fail-open 的 try/except                            → 1 failed, 8 passed
  └ 503 job_unavailable 顶替 422（message = 注入的 detail）
点错项 register the model (model-registration …)      → 1 failed
等价改写 publish a binding revision                   → 2 failed, 7 passed
preflight 文案换 "run the migration profile"          → 集成 1 failed；test_lease_settlement 15 passed

# 旧 head 70a8c892（基线 7 passed）
零状态句删 "publish the binding and "                 → 7 passed        （绿 = 缺口真实）
删 fail-open 的 try/except                            → 7 passed        （绿 = 缺口真实）
preflight 文案换旧文案                                → 集成 1 failed    （该守卫在旧 head 已存在）

# 分叉点 1721ddb2（基线 6 passed，无集成文件）
if states: → if False:                                → 6 passed
收窄 except 到只 InvalidSchemaName                    → test_lease_settlement 1 failed, 14 passed
preflight 文案换旧文案                                → test_lease_settlement 15 passed（绿 = 缺口真实）
```

## 附录 B：变异还原证据

所有产品/测试文件的临时改动都用 `git checkout -- <path>` 还原；`/private/tmp/sn942-exp` 在 `8ae2edda`
上 `git status --porcelain` **空**，本报告分支 `review/934-delta` 只新增本文件。
未 push 任何被审分支、未合并、未改 `main`。
