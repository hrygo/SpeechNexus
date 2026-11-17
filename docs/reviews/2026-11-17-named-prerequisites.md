# 缺前置失败面点名的非作者审查（PR #924）

审查对象：`fix/named-prerequisites`，head `ad134e56`（父 `c83ac803`，即与 `origin/main` 的分叉点）。

**两点口径的前提已变，先记事实。** 审查者给出的 `git diff origin/main ad134e56` 在本次执行时 `origin/main`
已从 `c83ac803` 前进到 `3b1bbf5e`（#922 落主干）。该两点实测为 **22 文件 +1037/−263**，其中 `deploy/private/README.md`、
`docs/operations/async-job-runbook.md`、`docs/operations/builtin-voice-qualification.md`、
`docs/operations/compose-minimal-from-zero.md` 的删除以及 `compose-minimal.md` 的部分段落是 #922 的**反向**，
不属于本 PR；本 PR 自己的 delta 是 `git diff c83ac803 ad134e56` = **18 文件 +1037/−88**，与 PR 描述一致。
本报告全部结论按 18 文件口径，报告里出现的「本 PR 删除」均指该口径。两种口径都不改变合并判断：
`git merge-tree --write-tree origin/main ad134e56` 退出码 0、无冲突（合并树 `c39f63b`），
`jobs.py:805` / `app.py:815` 两处文档行号在合并树上也仍然指向 `@router.post(PREFIX, operation_id="createJob")`
与 `create_job_router(`。

**结论：不能合（当前）。** 只有两处**低成本必须修**：M1 的「启动期打印一行 `FAIL:`、不产生 traceback」是
可执行承诺，实测不成立（真实入口仍是 traceback + 退出码 3，且 `main.py` 新增的 `except JobSchemaNotMigratedError`
在该 raise 点不可达）；M2 的守卫测试自述「every converting route」，实际只打到 6/12 个出口且**整条 `_read`
路由未覆盖**。两处都是「声称与事实不一致」，改文案或补参数即可，不必返工设计。被审 head 的 CI 本身是全绿的（30 项无失败，见 §0），
所以阻塞项只剩这两处，不是 CI 或合规问题。其余为可接受但需记录（§8）。
本 PR 的**设计方向与请求期行为是对的**：12 个异常出口确实收敛到单一决定点，真实 PostgreSQL 上四种缺前置
（writer 空 / admission 空 / 未迁移 / 缺授权）都能从响应文案区分，且没有泄露 DSN、主机、库名、用户名或 `postgresql://` 前缀。

---

## 0. 复核记录

审查在独立 worktree `/private/tmp/sn925` 完成：分支 `review/924-named-prerequisites` 从 `origin/main`
（`3b1bbf5e`）新建，`HEAD` 仅为阅读被审查 head 检出过；本报告是该分支唯一改动。依赖
`pnpm install --frozen-lockfile` + `UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`（新 worktree
必须两项都跑，否则 8 个 `[typescript]` 假红）。隔离 PG `127.0.0.1:55480/sn892`、MySQL `127.0.0.1:55434`。
未提交 `uv.lock`，未改契约、迁移、部署文件或 `tests/support/`。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 两点口径核对 | `git diff --stat origin/main ad134e56` / `git diff --stat c83ac803 ad134e56` | 22 文件 +1037/−263（含 #922 反向）/ **18 文件 +1037/−88**（本 PR 真实 delta） |
| 合并不冲突 | `git merge-tree --write-tree --name-only origin/main ad134e56` | exit 0，无冲突项（树 `c39f63b`） |
| 新增/受影响单测 | `pytest tests/unit/backend/test_job_dsn_naming.py` | 3 passed |
| 新增单测 | `pytest tests/unit/backend/test_job_unavailable_prerequisites.py` | 20 passed |
| 映射表守卫 | `pytest tests/ci/test_job_prerequisite_map.py` | 6 passed |
| 文档行号守卫 | `pytest tests/ci/test_doc_statements.py` | 9 passed |
| 真实 PG 集成 | `pytest tests/integration/test_public_job_prerequisites_pg.py`（无 skip） | 7 passed |
| 广扫（含 collateral） | `pytest tests/ci tests/unit/backend tests/contract/test_job_admission_readiness.py -q` | **4084 passed, 2233 subtests passed**（62.4 s） |
| 格式 / 静态 / 类型 / 边界 | `ruff format --check .`、`ruff check .`、`mypy`、`lint-imports` | 1979 files already formatted / All checks passed / Success: no issues found in 1535 source files / Contracts: 9 kept, 0 broken |
| head 的 CI（PR #924） | `gh pr checks 924`（run `43945227280`，head `ad134e56`） | 30 项无失败：`Python: lint, types and contract compatibility`、`Python: tests, coverage and build (a/b)`、`Integration 1–12`、`Web`、`Route: scope and review boundary` 均 pass，其余 skipping |
| 反例重做（七组） | 见 §6 表 | 7/7 全部变红，逐项按 sha256 还原 |
| 篡改还原 | `git status --porcelain` | 空（含探针文件、临时 schema、临时 worktree 清理后） |

## 1. 作者自述逐条核对

| 自述 | 判定 | 依据 |
| --- | --- | --- |
| 单一来源 `JOB_PREREQUISITES`，五行 `writer_dsn_blank` / `admission_dsn_blank` / `schema_not_migrated` / `role_privileges_missing` / `store_unavailable` | **成立** | `speech/domain/job_prerequisites.py:41-77`，五行齐全，`_BY_CONDITION` 拒重复（`:89-95`） |
| `jobs.py` 六个处理器 12 个出口全部走 `job_storage_error()` | **成立（口径需补一句）** | `grep -c job_storage_error( jobs.py` = 12，行号 518/520/551/573/594/626/628/679/683/685/705/707；另有 6 个「service 为 None」出口走 `_unavailable()` → `job_error()`（`jobs.py:773-775`，调用点 809/817/825/833/861/873），文案由 `app.py:571` 的闭包给出，仍是表中同一行，但**不经过** `job_storage_error` |
| 改前 8 个出口丢掉下层已算出的原因 | **成立** | 旧文件 `job_error("job_unavailable", request_id)` 无 detail 者 8 处、带 `detail=error.detail` 者 4 处 |
| 契约零改动；可区分性全靠 `detail` | **成立** | 18 文件里无 `contracts/` 改动；线上 `ErrorResponse.error.message` 是自由字符串（`contracts/shared/errors.schema.json`，`minLength:1 / maxLength:2000`），`detail` 是内部名，落在 `error.message` 上 |
| `_unavailable()` 的 detail 从「列出两个空键」收窄为只点名 writer | **成立** | `app.py:548-571`；旧文案 "these keys are blank: …" 已在仓库内无任何引用（`grep` 空） |
| 未迁移识别：请求期沿 `__cause__`/`__context__` 读 `sqlstate`（`42P01`/`3F000`）；启动期在角色守卫之前先跑 `require_migrated_schema()` | **成立** | `job_prerequisites.py:112-138`；`job_runtime.py:239-244`（preflight 在 `_verify_privileges` 之前）、`:334-339`（admission 池同类） |
| `deploy/job-startup-gate.py` 的 `migration_findings` 未动 | **成立** | 18 文件里不含 `deploy/` |
| `compose-minimal.md` §9.1 映射表 + `test_job_prerequisite_map.py` 用 `ast.literal_eval` 逐行比对 | **成立** | `compose-minimal.md:2269-2289`；`tests/ci/test_job_prerequisite_map.py:39-55`（`ast.parse` + `literal_eval` 读**产品源码**，见 §6） |

## 2. 启动顺序风险（要求 1）：没有新的失败面，但承诺的那条 `FAIL:` 不存在

### 2.1 代码位置与三个失败面的实际归属

`JobRuntime.open()` 对每一次启动只走一条通路（`job_runtime.py:214-317`）：建池 → 取连接 → `SET LOCAL statement_timeout` →
**`require_migrated_schema()`（新增，`:239`）** → `_verify_privileges()`（`:244`）→ admission 池（`:334-339`）。
新增的检查是**一个只读目录查询**（`job_schema_preflight.py:51-56`：`pg_catalog.pg_class` ⋈ `pg_namespace`，按 schema 与关系名过滤），
它只在**连接成功且查询成功**之后才可能返回「缺失」。因此：

| 启动失败形态 | 实际得到 | 是否被误报成「未迁移」 |
| --- | --- | --- |
| 端口不通 / DNS 不可解析 / 密码错 / 库不存在 | `ValueError("Public Job writer startup rejected; verify migration and writer role")`，`from None`（`job_runtime.py:315-317`，`__cause__=None`、`__suppress_context__=True`） | **否**（实测四种 DSN 形态，见 2.3） |
| 连得上、schema 为空（未迁移） | `JobSchemaNotMigratedError`，消息就是表里的迁移命令 | — |
| 连得上、schema 已迁移但角色无任何授权 | preflight **通过**（目录行不需要授权），角色守卫拒绝 → 仍是上面那句通用 `ValueError` | **否**（实测，见 2.4） |
| 连得上、角色连 `pg_catalog` 都读不了 | 在 PostgreSQL 里只有被 REVOKE 目录权限这类构造才会出现；若发生，`psycopg` 错误同样落到通用 `ValueError`，**不会**落到「未迁移」 | 否（推断，未构造；见 §8 R2） |

「权限不足会不会被报成未迁移」的答案是**不会**：目录查询不过滤权限，关系存在就能看见。这一点我在真实 PG 上正面验证过（2.4）——
18 个必需关系全部存在、角色无 `USAGE` 且无任何表授权时，preflight 照样通过，拒绝来自角色守卫。

### 2.2 实测 before / after：消息变好，形态没变，`FAIL:` 行没出现

同一个空 schema（`CREATE SCHEMA`，无迁移）、同一 DSN、同一真实入口：

| 时点 | 可执行入口 | 输出尾部 | 退出码 |
| --- | --- | --- | --- |
| 改前 `c83ac803`（`PYTHONPATH` 指向基线 worktree，同一 venv） | `speechnexus_backend.main:main` | `ValueError: Public Job writer startup rejected; verify migration and writer role` + 完整 traceback | **3** |
| 改后 `ad134e56` | `.venv/bin/speechnexus-api` | `JobSchemaNotMigratedError: The Job database has not run the migration profile: … Run docker compose --profile migration run --rm migrate, then restart the api process.` + 完整 traceback | **3** |

两行里的 `ERROR: Application startup failed. Exiting.` 都来自 uvicorn，`FAIL:` 前缀一次都没打印。

**原因（代码位置）**：`main.py:20-38` 的 `try/except` 包的是 `uvicorn.run(...)`；而 Job 写入器的预检跑在
**ASGI lifespan** 内（`app.py:660-661` 定义 lifespan，`:711-713` `await run_in_threadpool(job_runtime.open)`）。
uvicorn 0.54.0 的 `Server.startup()` 先 `await self.lifespan.startup()`，失败即 `sys.exit(STARTUP_FAILURE)`
（`.venv/.../uvicorn/server.py:113-118`），并且 `LifespanOn` 已先把原异常连同 traceback 记日志。
于是异常**永远不会**以 `JobSchemaNotMigratedError` 的身份穿过 `uvicorn.run()`：

* `main.py:30` 新增的 `JobSchemaNotMigratedError` 分支对这条 raise 点**不可达**（`JobRuntime.__init__` 不开池，
  没有任何同步路径会抛它）；
* 同一分支里的 `SettlementPreflightUnavailableError` 是可达的，因为它在 `build_speech()` 里**同步**执行
  （`speech_runtime.py:89-91`），发生在 uvicorn 的 `config.load()`（factory 调用）阶段，异常能逃出 `uvicorn.run()`
  ——这正是 `main.py:30` 注释说「两个运行时共享一行」的由来，而事实上两者路径不同；
* 我用一个 lifespan 内抛同类型异常的最小 ASGI 应用复现了机制：`EXIT=3`，`ERROR: Application startup failed. Exiting.`，无 `FAIL:`。

即：`compose-minimal.md:2280-2281` 的「`speechnexus-backend` 入口打印一行 `FAIL: ...` 并以非零退出，不产生 traceback」
与 `main.py:34-36` 的注释都**不成立**。参考部署的 api 就是 `command: ["speechnexus-api"]`（`deploy/compose.yaml:136`），
`deploy/job-startup-gate.py` 是 **job-dispatch-worker** 的 entrypoint（`deploy/compose.yaml:466`），不替 api 兜这一层，
所以运维在参考部署里同样会看到 traceback。

### 2.3 四种坏 DSN 的实测（证明不会报成「未迁移」）

`JobRuntime.open()` 分别对 `127.0.0.1:1`（拒绝连接）、错误密码、不存在的库、不可解析主机名执行，四例都得到
`ValueError: Public Job writer startup rejected; verify migration and writer role`，`__cause__=None`、
`__suppress_context__=True`。这条消息本身对「DSN 错」是**误导**（让人去查迁移和角色），但这是**改前既有行为**，
不是本 PR 引入；本 PR 也没有把它变得更差。

### 2.4 权限不足的实测（证明 preflight 不会抢占角色守卫）

一次性 schema `sn_review_grants`：18 个必需关系全部建好（相当于迁移已跑），另建角色只授 `CONNECT`、不给 `USAGE`、
不给任何表授权，用它当 writer DSN：

```
[preflight passed, role guard answered] ValueError: Public Job writer startup rejected; verify migration and writer role
```

即 preflight 没有把「缺授权」说成「未迁移」；顺序改变在这一点上是安全的。（也顺带确认
`require_migrated_schema` 对 `dict.fromkeys` 去重是必要的：`speech_jobs` 同时在 `REQUIRED_SELECT` 与
`_ADMISSION_REQUIRED_SELECT` 里，不去重会重复计数。）

**本节结论**：启动顺序改变**没有**引入新的失败面（目录查询只增加「连接成功后多一次只读查询」这一风险，而它失败时落回
改前同一句通用拒绝）；真实缺陷是**承诺与实测不符**，不是行为退步 → M1。

## 3. 泄露面（要求 2）：表和实测都干净，风险在「边界不过滤」而非「文案」

五行逐条核对（正则同时查 `postgresql://|postgres://|mysql://`、`@`、IPv4/`:端口`、`password|secret|token|credential`、
`.runtime/|/private/|/Users/|/tmp/|.venv`）：

| condition | 命中项 | 结论 |
| --- | --- | --- |
| `writer_dsn_blank` | 无 | 只有键名 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` |
| `admission_dsn_blank` | 无 | 只有键名 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` + 「set it to the same database as the Job writer」（不含库名） |
| `schema_not_migrated` | 无 | 只有 `docker compose --profile migration run --rm migrate`；**不点名**缺失关系（关系名只进日志与 `JobSchemaNotMigratedError.missing`） |
| `role_privileges_missing` | 无 | 只有仓库相对脚本路径 `scripts/dev-env/provision-job-roles.py`，该命令是 `compose-minimal.md:1154` 与 `async-job-runbook.md:289` 的既有运维口径，属可执行命令而非内部路径 |
| `store_unavailable` | 无 | 与改前逐字相同 `Job storage is unavailable.` |

**没有把 `postgresql://` 之类的前缀漏出**：五行 detail/action 都不含任何 DSN scheme；`@`、凭据词、IP/端口、绝对路径零命中。
集成用例也做了运行期对照：真实沙箱的 `url`、`role`、`schema` 与字面 `postgresql://` 都不出现在两个响应体里
（`tests/integration/test_public_job_prerequisites_pg.py:210`）。

反向追一遍**能到达 `error.message` 的 detail 生产者**（这是本 PR 真正扩大的面：`_list`/`_read`/`_retry`/`_open_stream`
改前丢弃 detail，现在透传）：

* `PublicJobUnavailableError` 的产品侧构造点共 6 处，参数只有三种：`prerequisite_detail(error)`（表文本）、
  `result.detail`、`None`（`public_jobs.py:1596/1624`、`public_jobs_postgres.py:948/992/1021/1048`）；
* `JobCreateResult.detail` / `RevisionCreateResult.detail` 的来源只有三种：`self._admission_unavailable_detail`
  （= `ADMISSION_DSN.detail`，`job_runtime.py:175`）、`prerequisite_detail(error)`，以及 `_AdmissionRefusedError.detail`
  ——后者来自 `AdmissionRejectedError.detail`，`domain/admission.py:293-307` 声明它是「只替换公开 message、不携带 DSN/凭据」
  的策展文本（例如 `source_refusal_detail(str(state))`）。

所以**当前不存在能漏出 DSN 的值路径**。残余风险是**边界不过滤**：`job_errors.py:321-325` 原样透传 `error.detail`，
安全性完全依赖生产者自律；映射表的 `LEAK` 正则只守表，不守生产者。任何未来把原始驱动消息塞进 detail 的改动都会直接在
六条路由上暴露。属可接受但需记录（§8 R4）。

## 4. 「只点名 writer」的可达性（要求 3）：找不到反例，但有一个不可达分支

`job_unavailable_detail()`（`app.py:548-571`）实际有两种返回值，我用三种形状逐一实测：

| 形状 | 结果（实测） | 说明 |
| --- | --- | --- |
| S1 writer 空 + admission 空 | 503，`message == WRITER_DSN.detail`，含 writer 键、**不含** admission 键；`app.state.job_runtime is None` | 唯一可达的「整面未装配」形状；旧文案会同时列出两键 |
| S2 writer 空 + admission 非空 | `create_app` 直接 `ValueError`：`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL is configured but SPEECHNEXUS_JOB_WRITER_DATABASE_URL is blank; …`（`app.py:221-251`） | **进不去** Request 期，所以「writer 空却只点 writer 会漏掉真正该配的 admission」这一形状**不存在** |
| S3 writer 非空 + admission 空 | `job_runtime` 一定被装配（`app.py:537-541`：`jobs is None and 写者非空` → 构造 JobRuntime），`job_unavailable_detail()` 在第一个守卫（`:563`）就返回 `None`；create 由 store 报 `ADMISSION_DSN.detail`，list 仍 200 | 不存在「写者已配、admission 空、而 `create_app` 没拒绝、整面又没装配」的形状 |

结论：**要求 3 想找的反例不可达**。`app.py:567-570` 那个 `if resolved_settings.job_writer_database_url is not None: return None`
分支在 S3 已经被 `:563` 先截住，属于**防御性死代码**（无害、不可测），记为 §8 R3，不算缺陷。

## 5. 兜底误标（要求 4）：`store_unavailable` 只在真的没有可识别原因时使用

`job_storage_error()`（`job_errors.py:301-326`）的判定顺序是「显式 detail → 表分类 → 兜底」，表分类只看 SQLSTATE
（`job_prerequisites.py:118-138`）。逐 SQLSTATE 实测：

| SQLSTATE | 分类 | 是否会落到兜底 |
| --- | --- | --- |
| `42P01` undefined_table | `schema_not_migrated` | 否 |
| `3F000` invalid_schema_name | `schema_not_migrated` | 否 |
| `42501` insufficient_privilege | `role_privileges_missing` | 否 |
| `42703` undefined_column | `None`（→ 兜底） | **是**（见 R1） |
| `3D000` invalid_catalog_name / `28P01` invalid_password / `08006` connection_failure | `None`（→ 兜底） | 是，且这是期望行为 |
| `57014` query_canceled / `25006` read_only_sql_transaction / `53300` too_many_connections | `None`（→ 兜底） | 是，且这是期望行为 |

配合三条边界用例：`ValueError("decoder defect")`、`None`、`PublicJobUnavailableError()` 都得到
`Job storage is unavailable.`；而 `PublicJobUnavailableError(ADMISSION_DSN.detail)` 的显式文本**优先于**表分类
（`job_errors.py:321-325`），不会被兜底覆盖。所以「运维看到 storage unavailable 就查不出真因」的情形**不是**
`42501`/`42P01` 被误判，而是 R1 那一类没被表覆盖的形态。

## 6. 映射表漂移（要求 5）与反例重做（要求 6）

**它读的是产品源码，不是测试自己的副本**：`tests/ci/test_job_prerequisite_map.py:29-31` 把路径写死为
`backend/src/speechnexus_backend/speech/domain/job_prerequisites.py`，`:39-55` 用 `ast.parse` + `ast.literal_eval`
取模块级 `JOB_PREREQUISITES` 字面量（重命名/改写为函数会 `AssertionError`），`:59-72` 按 `### 9.1` … `## 10.` 切段解析文档。
下面的 X3/X4 直接改**源码**也能让它变红，等于反证了这一条。

七组反例 X1–X7（每组前存 sha256、跑完按备份还原并复核 sha256 一致）：

| # | 注入 | 期望失败 | 实测 |
| --- | --- | --- | --- |
| X1 | 文档把 admission 行 detail 改掉一个分句（删「and cancellations」） | `test_every_shipped_row_is_documented_verbatim` | **1 failed, 5 passed** |
| X2 | 文档凭空加一行 `no_such_condition` | `test_the_map_names_exactly_the_shipped_conditions_in_order` | **1 failed, 5 passed** |
| X3 | **源码**删掉 writer 行里的键名 | 逐行比对（证明读产品源码） | **1 failed, 5 passed** |
| X4 | **源码**把 admission 行 detail 换成 writer 行的 | 逐行比对 + 两行互异 | **2 failed, 24 passed** |
| X5 | 文档整行删掉 writer 行 | 缺行 + 顺序 | **2 failed, 4 passed** |
| X6 | 文档删掉 `store_unavailable` 行 | 缺行 + 顺序 | **2 failed, 4 passed** |
| X7 | 把 `_list` 的一个出口从 `job_storage_error` 退回 `job_error("job_unavailable", …)` | `test_every_converting_route_forwards_the_prerequisite_row[…]` | **1 failed, 19 passed** |

七组全部按预期变红，全部还原，收尾 `git status --porcelain` 为空。**漂移风险已被真正守住**：
实现改一侧、文档改一侧、或某个出口悄悄退回旧答案，都会红。

需要一并记录的守卫强度缺口（→ M2）：`test_every_converting_route_forwards_the_prerequisite_row` 的参数只有
6 条（`tests/unit/backend/test_job_unavailable_prerequisites.py:181-196`：create×2、list、retries、cancellations、events），
用 coverage 在同一 branch 上实测 `jobs.py` 的 12 个出口，**只执行了 6 个**：518/520/551/626/683/705 执行，
573（`_list` 的 `artifacts_page` 出口）、**594（`_read`，即 `GET /v1/nexus/jobs/{job_id}`）**、628/685/707
（三处 `ContractValidationError`）与 679（`_retry` 的 `JobRejectedError`）未执行。也就是说这个文件的模块 docstring
「every converting route」和 `#920` 的「12 个出口」目前**没有测试证据**支撑 `_read` 这一整条路由（该路由的转发写法本身
经 grep 确认是对的）。这是守卫强度问题，不是行为缺陷。

## 7. 既有断言是加强还是削弱（要求 7）

`test_job_dsn_naming.py` 把「两键都出现」改成「只出现 writer 键 + 文案逐字等于共享行」，判断如下：

* **对可达形状是加强**。新增的 `assert error["message"] == WRITER_DSN.detail` 把响应绑到唯一来源（第二个副本再也无法漂移），
  `assert ADMISSION not in error["message"]` 把「一次响应里列两个键」这一已被否定的形态钉死；两条都比原来的
  `in message` 子串断言更强。
* **有一处刻意的信息收窄，且可定位，不构成削弱**。S1（两键都空）原本一次响应告诉运维两个键，现在只说 writer。
  之所以可接受：S2 证明「admission 配了而 writer 空」会被 `create_app` 直接拒绝并点名两键，
  所以 writer 空时 admission 键确实**改不动这次拒绝**；S3 证明 writer 一配上，admission 的缺失立刻由 create 通道
  以 `ADMISSION_DSN.detail` 报出，读取仍可用。也就是说「两个都缺」的形状仍然**分两步可定位**，
  且 `compose-minimal.md:2286` 的 admission 行把「writer 已配置、只缺 admission」写清了。
* 需要运维知道的唯一行为变化：**两键都空时的新部署，第一次 503 不再提 admission**。这是对外可见的文案变化
  （非契约变化，`error.message` 是自由字符串），已由 §9.1 与 `test_a_blank_writer_dsn_and_a_blank_admission_dsn_answer_differently`
  共同覆盖。建议在 §9.1 writer 行补一句「只配 writer 后，create 会再点名 admission」——现在这句信息只存在于测试与代码注释里。

## 8. 必须修清单与可接受但需记录

### 必须修（两处，均低成本，改完即可合）

**M1（阻塞）— 「一行 `FAIL:`、不产生 traceback」与实测不符，且新增的 `except` 分支不可达。**
证据见 §2.2：真实入口 `speechnexus-api` + 真实空 schema → `JobSchemaNotMigratedError` → **traceback + 退出码 3**，
没有 `FAIL:` 行；机制上 lifespan 内抛出的异常被 uvicorn 0.54.0 `Server.startup()` 转成 `sys.exit(3)`，
`main.py:30` 永远收不到。二选一（都只需小改）：

1. 修文案（最小）：把 `compose-minimal.md:2280-2281` 改成「入口会以非零退出并打印命名修复的 `JobSchemaNotMigratedError`
   （uvicorn 会连同 traceback 一起打）」，并改 `main.py:31-36` 的注释；同时说明参考部署里 api 容器会
   `Application startup failed. Exiting.` 重启循环，runbook 的应对是跑迁移 profile。
2. 修接线（更彻底）：把 writer 的 schema 预检移到同步装配路径（像 `require_complete_tts_roots` 那样在
   `create_app_from_env`/`build_*` 阶段跑一次），使 `FAIL:` 承诺成立，并给这个 raise 点补一条进程级用例
   （现在 `test_an_unmigrated_database_answers_the_migration_before_the_role_guard` 只断言 `runtime.open()` 抛什么，
   所以从未覆盖 §9.1 的这句承诺）。

**M2（阻塞，低成本）— 「every converting route」的守卫漏了 6 个出口，且整条 `_read` 路由未覆盖。**
证据见 §6 的 coverage 实测（12 出口执行 6 个；594 = `GET /v1/nexus/jobs/{job_id}` 未执行）。二选一：
补齐参数（至少补 `("get", PREFIX + "/job-…")` → `_read`，以及 `_list` 的 `artifacts_page` 出口、`_retry` 的
`JobRejectedError` 出口），或把测试模块 docstring 与 `#920` 的自述收窄为「六个处理器、已覆盖 5 条路由 / 12 出口中的 6 个」。

### 可接受但需记录（不阻塞）

* **R1 半迁移形态落到兜底**：`42703`（缺少列）不在表里，会答 `Job storage is unavailable.`。经验上「关系在、列不在」意味着迁移
  只跑了一半，`schema_not_migrated` 的修复命令同样适用，但表只认 `42P01`/`3F000`。启动期有列级守卫
  （`REQUIRED_UPDATE_COLUMNS` + `refuse_column_privileges`）能抢先拦住常见的列漂移，所以这条只在运行期才出现，概率低。
  记录即可；若要收，加 `42703` 到 `_NOT_MIGRATED_SQLSTATES` 并补一条用例。
* **R2 启动期的「坏 DSN」不在映射表内**：四类 DSN 错误一律是 `ValueError("…verify migration and writer role")`（`from None`，
  连主机/端口都不留）。这是改前既有行为，本 PR 没有恶化；但 PR 新增的「缺什么 → 看到什么 → 做什么」表很容易被读成
  「启动期失败都已被覆盖」，而这张表实际只覆盖请求期 + 一个启动期迁移分支。建议在 §9.1 的开头一句话说明边界。
* **R3 死分支**：`app.py:567-570` 的 `if job_writer_database_url is not None: return None` 因 `job_runtime` 在写者非空时必被构造
  而不可达（§4 S3）。无害，但它是唯一无法被任何测试覆盖的一行；可留可删。
* **R4 边界不做泄露过滤**：`job_storage_error` 原样透传 `error.detail`，安全性依赖生产者自律；本 PR 让另外四条路由开始透传，
  依赖面变大。当前 6 个生产者全部安全（§3）。建议后续在 `job_error()` 加一条断言/正则（例如拒绝 detail 中出现
  `postgresql://`、`@`、`password`），把「不泄露」从纪律变成门禁。
* **R5 异常链是单链**：`prerequisite_of_error` 用 `frame.__cause__ or frame.__context__` 每跳只走一条边；若某处
  `raise B from C` 且 `B.__context__` 才带 SQLSTATE，会漏判。当前全部包装点都是 `from None`（`__cause__=None`、context 保留），
  所以不可达，记录即可。
* **R6 响应里给仓库相对命令**：`role_privileges_missing` 让运维「Run scripts/dev-env/provision-job-roles.py」，
  而同一张表另一行给的是 `docker compose …` 形式；在 compose 部署里前者需要 host 侧仓库（`compose-minimal.md:1154`
  已是既有口径，不算泄露）。记录即可。
* **R7 `alembic_version` 成为启动硬前置**：`require_migrated_schema` 把迁移账本本身加进必需集合（`job_schema_preflight.py:27,50`）。
  迁移 profile 把 `version_table_schema` 设为同一 schema（`speech/infrastructure/migrations/env.py:29`），且该检查只读
  `pg_catalog`（不需要该表的 `SELECT`），所以对迁移过的库没有新门槛；只有「表都在、账本被删」的异常库会从「能启动」变成「拒绝启动」。
  这是有意的收紧，记录即可（`provision-job-roles.py:135` 的 `SELECT` 授权是给启动闸门/验收用的，与本检查无关）。

## 9. 一句话回答

**不能合（当前）**：M1 的可执行承诺与实测相反（真实入口是 traceback + 退出码 3，`FAIL:` 行不存在，新增 `except` 分支不可达），
M2 的守卫自述覆盖 12 个出口而实际只打到 6 个、`_read` 整条未测。两处都是低成本修改，**修完即可合**；
启动顺序、泄露面、只点名 writer、兜底误标、映射表漂移五项经实测均无阻塞问题，`R1`–`R7` 为可接受但需记录。
