# C05 服务端提词持久化交接

核验日期：2026-10-24（Asia/Shanghai）。本单元推进 C05 的**服务端持久化**，不构成完整 C05/C04、自动语音跟随或实时接线资格；C00–C08 goal 保持 active。

## 基线与范围

base/head：`4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交工作区）。上一累计交付包 347 路径，SHA-256 `35fbf8474219a90a2a39c045b8c83e726fff068afb3512c8496c81210cea41ad`，逐文件核对后保留。公开契约版本仍为 workbench OpenAPI `0.1.0.dev0`、bindings `0.1.0`（版本号不变，内容变化）。

本单元只做两件事：把已声明但未接入的 `GET/PUT /v1/workbench/projects/{project_id}/teleprompter` 接入独立 PostgreSQL；补齐这两项操作的契约边界状态与正负样例。不修改 Web 源码、共享组合根/生成器/deploy/锁文件，不接入实时事件、模型或对象区。不 commit、push 或发送外部消息。

## 行为与边界

持久状态按项目保存四项事实：`script_revision`、`visible_position`、`manual_hold`、`updated_at`。稿件正文、匹配假设、stream epoch、音频、设备与模型事实都不进入该状态——它们属于浏览器阅读会话（见 [C05 提词跟随交接](2026-10-24-team-c-c05-prompter-handoff.md)）。

领域规则只有一条：**严格更旧的 `script_revision` 不得推动当前稿**，抛 `RevisionConflictError` → 409 `revision_conflict`；相同 revision 允许位置后退（手动 seek）并更新手动接管；更新的 revision 被采纳。`GET` 在项目不存在、已 tombstone 或尚未保存过状态时返回 404——**没有状态不等于位置 0**，不虚构 `script_revision`。

`Idempotency-Key` 语义与 C 其他变更一致：同键同意图返回原接受快照（含原 `updated_at`），同键不同意图 409 `idempotency_conflict`；无键请求不写回执、不假装可重放。项目行 `FOR UPDATE` 是该项目的唯一串行点，同项目并发 PUT 不会交错，因此不引入额外 advisory lock 或自动重试。

契约补齐：两项操作原先只有 200/401/404（GET）与 200/400/401/404/409（PUT），缺少同类变更操作都声明的传输级错误状态，导致 `_error` 可能发出未声明状态。现 GET 增补 400/408/413/500/503，PUT 增补 422/408/413/415/500/503；GET 的 `description` 明确“404 表示项目未知或尚未保存状态”。`contracts/bindings.json` 在某 workbench surface 选择该路径（15→16 路径、18→20 操作），并在 workbench profile 增加 `TeleprompterUpdateRequest`/`TeleprompterState`。

数据库：新增第六份 owner-only C 片段 `teleprompter_schema.py`（`teleprompter_states` + `teleprompter_receipts`，均以 `project_id ... ON DELETE CASCADE` 引用 `projects`），由既有 `bootstrap_sql` 组合。既有五份片段字节不变。`SCHEMA_REVISION` 由 1 升至 **2**：stamp 随组合变化，旧 20 表 v1 与新 22 表 v2 不再共用同一标记值；启动检查仍按**表集合精确相等** fail-closed，因此这不制造现存 schema 的升级路径，也不改变 A 的全局迁移顺序编排。运行角色只获两表 SELECT/INSERT 与 `teleprompter_states` 四列 UPDATE；回执不可改写、行不可删除。删除清理 worker 的完整性自检仍只枚举其拥有的内容表，提词状态由 `ON DELETE CASCADE` 在数据库层擦除，并由真实 PG 用例计数为 0 验证（见终审 M4）。

## 验证

- Python unit/contract **3577 passed**（上一单元 3488），含新增领域 12 项、HTTP 边界 24 项；隔离 PG 探针 **317 passed / 19.79s**（上一单元 310），随后 `pg_ctl status` 非 0 且无 `postmaster.pid`。
- PG 证据为 PostgreSQL 18.6、仅 Unix socket、`listen_addresses=''`、`speechnexus.disposable_workbench_probe='on'` 的私有集群；探针用 `--workbench-runtime` 运行全部 C PG 用例（版本/HTTP/会议/封存/列表/导入/导出/纪要/删除/清理/runtime）。
- 真实隔离 PG 覆盖：首次 PUT 创建；原键重放在**另一次 unkeyed 前进之后**仍返回原接受快照（而非当前状态）；同键不同意图 409；旧 `script_revision` 409；同 revision 后退；更新 revision 采纳；5 路并发 PUT 串行化且终态为最大 revision；重建 factory 后重读一致；项目删除后 GET/PUT 均 404；cleanup worker 运行后两张新表按项目计数为 0。
- 启动检查新增四组真实 PG 反例：删除 `teleprompter_states`、整表 UPDATE、回执表 UPDATE、回执表缺 INSERT，均拒绝启动；stamp 组合版本为 2。
- 静态与契约：`ruff format --check` 611 文件、`ruff check`、`mypy` 452 源文件、`lint-imports` 7 kept/0 broken、`make contracts-check` 28 文件、`make contracts-drift` up to date、`git diff --check` 通过。
- Web 与契约包门禁在**生成 TS 产物变化**后重跑：`pnpm format:check`、`pnpm lint`、`pnpm typecheck`、`pnpm build` 通过；`pnpm test` 为 contracts 1022 + admin 110 + workbench 525 = **1657 passed / 0 failed**。Web 业务源码未变。
- 未验证：真实实时事件/语音跟随、真实设备与麦克风、真实模型/LLM、Linux 后端、GPU、生产迁移与部署，以及真实 PG 上的提交未知（`outcome_unknown`）故障路径。本单元不宣称完整 C05、C08 或生产资格。

可复现命令（仓库根目录）：

```bash
uv run pytest tests/unit tests/contract -q
uv run python scripts/dev-env/workbench-pg-probe.py \
  --pg-bin /opt/homebrew/Cellar/postgresql@18/18.6_1/bin --workbench-runtime \
  --output .runtime/team-c/prompter-4b08/pg-probe.json
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run lint-imports
make contracts-check && make contracts-drift
pnpm format:check && pnpm lint && pnpm typecheck && pnpm test && pnpm build
```

## 终审

独立终审针对 17 个冻结源文件（`.runtime/team-c/prompter-4b08/review-source-hashes.json`）进行，报告见 `.runtime/team-c/prompter-4b08/review-result.md`。结论：**代码层 Critical 0 / Important 0，Minor 4**，17/17 哈希匹配。评审方对 7 项关注点（并发串行化、意图摘要完整性、wire/domain 转换、权限矩阵与启动检查、tombstone/级联、契约闭包、测试可判别性）逐项证伪，并用独立探针确认“重放返回回执快照而非重读当前状态”“项目行锁下并发不丢更新”“两表权限恰为 SELECT/INSERT 与四列/零列 UPDATE”。

评审后按 Minor 收紧如下，并由同一评审方做一次聚焦复核（`.runtime/team-c/prompter-4b08/review-postfix-result.md`：重新冻结的 17/17 哈希匹配，M1 经 `/tmp` 突变探针实证可判别，M2 任意交错终态确定，M4 回退逐字节 MATCH，无新增 Critical/Important）：

| Minor | 处理 |
| --- | --- |
| M1 重放断言不可判别 | 集成用例在首次写入后追加一次 unkeyed 前进，再重放原键，断言等于原快照且不等于当前状态。 |
| M2 无并发用例 | 新增 5 路并发 PUT 集成用例，断言终态恰为最大 revision、每个 409 均为 `revision_conflict`。该断言证明最大 revision 不会被更旧请求覆盖；对更旧 revision 之间的瞬时交错只是概率检出，未做穷尽（见下 D1）。 |
| M3 stamp 未随组合变化 | `SCHEMA_REVISION` 1→2，集成用例坏 stamp 改为 3，正确 stamp 期望改为 2。 |
| M4 清理自检不含级联表 | **不修改，记为接受的 Minor**：把两张提词表加入 worker 自检要求清理角色额外 SELECT，并使只执行版本+删除片段的局部 schema（既有 7 项 cleanup 用例）失败；级联是 FK 的数据库层保证，且真实 PG 用例已断言 cleanup 后两表按项目计数为 0。曾实现随即回退，字节与上一包一致。 |
| M5 键长/位置上限/回执保留 | 应用键长对齐契约 8–128 并加用例；`visible_position` 上限由生产者以已声明 422 稳定拒绝（契约不加 `maximum`，保留单一执行点）。提词回执无 TTL，也**不像** `mutation_receipts` 那样把 intent hash 保留进 `erased_mutation_receipts`：项目删除时它被纯级联擦除；因删除后项目恒 404、原键无法重放，这一差异是安全的（见下 D3）。 |

复核后仍保留的 Minor：

- D1 并发用例对“去掉项目行锁”的突变体是**概率检出**（复核实测 5 次中 4 次失败），且不穷尽更旧 revision 之间的瞬时覆盖；强保证需引入可注入延迟或二次确认。
- D2 上一单元的两份 runtime-root 计划/交接文档按历史时点仍写 `SCHEMA_REVISION=1`，已各加一行指向本单元 v2 的时点说明，未改写原事实。
- D3 提词回执与 `mutation_receipts` 的删除语义不同（纯级联 vs 保留 intent hash），已在上表精确记录。
- D4 `outcome_unknown` 未在真实 PG 上制造提交未知故障；错误映射由 fake store 单测覆盖，真实提交未知路径未复现。

## 未完成与依赖

- 公开实时事件接线、真实语音跟随、服务端位置与浏览器阅读会话的联动仍待完成；本单元只交付服务端持久化与读取。
- A 协调生产迁移顺序时需纳入第六份 C 片段；未部署前不宣称迁移、部署或跨环境资格。
- 完整 C00–C08 范围与退出条件不变，见[团队 C 执行目标](2026-10-24-team-c-goal.md)。
