# 服务端提词状态持久化计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans 内联实施，整个单元一次 fresh 终审；不提交、推送或删除证据。

**Goal:** 把 C05 已声明但未接入的两项提词操作装配到独立 PostgreSQL：按项目保存稿件版本、可见位置与手动接管，旧稿件 revision 的匹配不能推动当前稿。

**Architecture:** 新增第六份 owner-only C schema 片段（`teleprompter_states` + `teleprompter_receipts`），由既有 `bootstrap_sql` 组合、A 协调迁移顺序；`PostgresTeleprompter` 复用 `PostgresVersionUnitOfWork` 的短事务与项目行锁实现读改写和原键回放；HTTP 边界沿用生成契约校验，未实现能力不暴露。提词匹配、stream epoch 和阅读策略留在浏览器，不进持久状态。

**Tech Stack:** 现有 Pydantic Settings / FastAPI / psycopg、生成 `workbench_http`/`workbench` 绑定、`PostgresVersionUnitOfWork`。

**Spec:** [三队计划](2026-10-23-three-team-delivery-plan.md) C05；[产品设计 §9.2](../speechnexus-product-architecture.md#92-提词与助手)；[C05 提词跟随交接](2026-10-24-team-c-c05-prompter-handoff.md) 记为待完成的服务端持久化。

## Global Constraints

- 基线固定 `4b08c396c0fe8fc43c7e85683e534befab6bb22b`；保留 347 路径累计包与全部并行改动，不追逐无关分支。
- 既有五份 SQL 片段字节不变，只新增第六份；`SCHEMA_REVISION` 由 1 升至 2，使 stamp 能区分 20 表与 22 表两种组合（启动检查的表集合相等仍是独立漂移检测，不制造升级路径）。
- 位置不是内容：不存稿件正文、hypothesis、stream epoch、音频或设备事实；不接入实时事件、LLM 或引擎。
- 运行角色只获 SELECT/INSERT 两张新表及 `teleprompter_states` 四列 UPDATE；不得改写回执、项目身份或删除行。
- 原键返回原接受快照，不同意图 409；旧 `script_revision` 409；项目不存在或已删除（含 tombstone）404。
- 不改 Web 源码、共享 root/生成器/deploy/锁文件；生成产物由既有流水线重建。

## Review Focus

1. 旧稿 revision/tombstone 不能移动当前稿：409 与 404 都由真实 PG 事务而非 fake 决定。
2. 原键回放返回原快照（含 `updated_at`），不同意图不得覆盖已接受状态。
3. owner bootstrap 原子、运行角色最小权限、启动检查能拒绝缺表/多余 UPDATE/缺 INSERT。
4. 删除后读取与迟到写入均 404，cleanup 后状态与回执随项目级联清除。
5. 契约封闭：新增两项操作的正负样例、状态码与同类操作一致，未把未实现能力伪装成成功。

### Task 1: 契约选择、样例与生成

**Files:** 修改 `contracts/workbench-openapi.yaml`、`contracts/bindings.json`、`tests/fixtures/contracts/workbench-http.json`、`tests/contract/test_workbench_http_bindings.py`；重建 `packages/contracts` 生成产物。
**Interfaces:** workbench surface 选择 teleprompter 路径（15→16 路径、18→20 操作）；profile 增加 `TeleprompterUpdateRequest`/`TeleprompterState`；GET 明确“无状态即 404”，PUT 补齐同类操作的 400/408/413/415/422/500/503。

- [x] RED/GREEN：样例先失败于未声明的 400/422，补齐声明后 `tests/contract` 1491 项通过；生成产物与 manifest 一致。

### Task 2: 领域、应用与真实 PG 适配

**Files:** 新建 `workbench/domain/teleprompter.py`、`application/teleprompter.py`、`infrastructure/teleprompter_schema.py`、`infrastructure/postgres_teleprompter.py`、`interfaces/teleprompter_views.py`、`interfaces/http_teleprompter.py`；修改 `app.py`、`runtime.py`、`infrastructure/runtime_schema.py`。
**Interfaces:** `TeleprompterState.advance/initial`；`WorkbenchTeleprompter`；`PostgresTeleprompter`；`create_teleprompter_router`；`bootstrap_sql`/`runtime_grants_sql` 纳入第六片段。

- [x] RED/GREEN：领域边界、同 revision 后退、旧 revision 409；HTTP 两项操作、稳定错误与取消/超时不经 store。
- [x] 真实隔离 PG：持久化、原键回放、旧稿拒绝、重建 factory 重读、删除 tombstone 404 与 cleanup 级联清除；启动检查拒绝缺表/多余 UPDATE/缺 INSERT。

### Task 3: 资格、审查与交接

**Files:** 新增 plan/handoff；更新 `docs/plans/README.md` 与目标文档；本机 ledger/累计包。

- [x] Python unit/contract 与隔离 PG 探针通过；未宣称实时接线、真实语音跟随或完整 C05 资格。
- [x] 一次 fresh 终审（Critical 0 / Important 0，Minor 4）；按 Minor 收紧 M1/M2/M3/M5 并回归，M4 记为接受的 Minor 并回退到上一包字节。
- [x] 累计补丁干净 apply/全字节/index/链接核对；Goal active。
