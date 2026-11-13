# 参考部署验收脚本返修的非作者审查（#653 / #572）

- **审查对象**：`8281c2bb`（`fix: 参考部署验收脚本按审查返修（假通过、ACL 比对、步骤顺序）(#572) (#653)`），已合入 `origin/main`。
- **基线**：`origin/main` head `035e3c16`（受命时即此值）。事实取自 `git show origin/main:<path>` 与新 worktree，未读主检出（停在陈旧分支 `a7508635`）的工作树。
- **上一轮审查**：`9398627f`，报告 [2026-11-13-reference-acceptance.md](2026-11-13-reference-acceptance.md)，判**不通过**，给出 B1（`stack` 不看 `compose up` 退出码）、B2（幂等分支声称「221 条与已装面一致」却只核 8 条探针）、B3（默认 compose 模式 `gate` 在 `stack` 之前，从零必失败且归因错误）三条阻塞与 D1–D5、N1–N13 非阻塞项。
- **基线漂移**：`8281c2bb..origin/main` 对四个受审文件为空（`git log --oneline 8281c2bb..origin/main -- scripts/dev-env/reference-acceptance.py tests/ci/test_reference_acceptance.py docs/operations/reference-acceptance.md docs/operations/compose-minimal.md` 无输出）；漂移只发生在单元测试（`035e3c16` 改 `tests/unit/backend` 与 `tests/unit/gateway`）。故逐条结论对 `035e3c16` 成立。
- **审查分支 / worktree**：`review/667-acceptance-rework`，`/private/tmp/sn667`（`git worktree add /private/tmp/sn667 -b review/667-acceptance-rework origin/main`）。
- **审查者**：非作者。只读代码、测试、文档、上一轮报告、同轮 `1e9f2b12` 的 `compose-minimal.md` 改动与 `deploy/job-startup-gate.py`；其余结论由本机实测得出。
- **审查日期**：2026-11-13。
- **审查方式**：静态逐行审查 + 39 项现有测试 + 7 组测试级变异（自己删守卫、自己跑、确认红）+ 真实 PostgreSQL 16.15（真迁移 head `0053_voice_draft_publication`、83 张表）+ 真实 `provision-job-roles.py` 全链跑通 `roles` 步 + 真实 Docker Compose 构建失败语义 + 真实 `--dry-run`（真 `deploy/compose.yaml`、真 `docker compose config`）+ 与 `compose-minimal.md` / `job-startup-gate.py` / `provision-job-roles.py` / `init-object-storage.py` 的关系核对。
- **产品代码零改动**：只新增本报告。`uv.lock` 未改动（`git status --porcelain` 与 `git diff --stat uv.lock` 为空）；`.venv/` 为 gitignore。隔离 compose 项目 `sn667rev`、`sn667b1`、`sn667bf`、`sn667bf2`、`sn667bf3` 的容器/网络/卷/镜像与 `/tmp/sn667probe` 已在收尾时删除。

---

## 结论

**有条件通过。**

返修是**真返修**，不是措辞返修：

- **B1 修好了**：`step_stack` 现在先判 `started.ok`，`up` 非 0 即抛带退出码的 `StepFailureError`；我在真实 Docker 上复现了「坏 Dockerfile 重建失败 + 旧容器仍 `running/healthy`」的原始场景（`up` 退出 1、`ps` 仍 healthy、容器 ID 未变），脚本在该输入下整轮停在 `stack`（exit 60），不打印最终 `PASS`。
- **B2 修好了**：`roles` 步把 `--print-statements` 的每条 `GRANT` 解析成 `has_schema_privilege`/`has_table_privilege`/`has_column_privilege` 检查，与已装 ACL 做包含比对。真实渲染 **221 条语句 → 596 项检查**（20 schema / 112 table / 464 column，38 条重复标签去重后 558）。上一轮的两条反例中，探针外撤权的那条已实测转红；只装 8 条探针的极端场景按 596−8=588 项缺失推导必红（未重跑该极端场景，机制由「缺 1 项即红」实测证明），`REVOKE` 探针外授权也红并点名。列级与 schema 级检查都有牙（撤单列 `UPDATE`、撤 schema `USAGE` 均红）。
- **B3 修好了**：`STEPS` 顺序改为 `… → stack → gate → health`，`gate` 的 gateway healthy 前置**保留**；文档、`docs/operations/README.md`、`--help` 的步骤表与顺序说明同步；`--dry-run` 与真实 dry-run 显示的步骤序一致；没有哪一步的前置检查依赖了更晚的步骤。

**没有发现返修新引入的「该红却绿」硬路径**：所有 `ctx.mutate/ctx.read` 调用都判了退出码（逐处核对，见 §1c）；`run_steps` 的兜底异常仍是 FAIL + 步骤退出码，不是放行；`--build` 只增加红的机会；`surface_command_output` 不参与判定。

但有三类**新代码路径上的非阻塞缺陷**（见 [新引入的假通过/新路径清单](#新引入的假通过与新路径清单)），且 D1/D5 只**部分落地**，所以给**有条件通过**而不是通过：

1. `POSTGRES_USER`/`POSTGRES_DB` 是入口直接索引的第三类必需键，既不在「compose 派生」也不在「入口额外 6 个」里：缺它时 `tools` 判 PLAN/PASS，`migrate` 才红并**误归因**到迁移（实测 exit 20）。文档「必需键有两个来源」因此仍不完整（D1 部分落地）。
2. `roles` 步的 `step.expected`（脚本 `:1375`）与 `--help`/步骤标题仍写 `job roles ready: ... grants: N statements` 和「非 owner」，而入口实际打印的是「已创建；grants: N statements；…」且文档已声明不判定非 owner。文档改了，脚本自述没改（D5 部分落地）。
3. 记录路径（新增的 `环境记录`/`镜像记录`）是 best-effort 且**与 PASS 解耦**：`commit=unknown`、`镜像记录: 无` 时仍是 `PASS steps=7`；`print_records` 不包在 try 内，它自己要是有非捕获异常，会丢掉步骤退出码并打 traceback（实测 `PermissionError` 逃逸），与文档「不抛 traceback、不回退成 Python 默认退出码 1」的承诺有洞。

建议的下一轮收口（都不阻塞本片）：

1. 把 `POSTGRES_USER`/`POSTGRES_DB` 纳入 `mandatory_env_keys`（作为第三来源并在文档列出），或让 `psql_argv` 使用 compose 已解析的默认值；
2. 改 `Step("roles", …, expected=…)` 与步骤标题（去掉「非 owner」或改成「三条 Job DSN 身份互异」）；
3. `print_records` 包 try 或捕获 `OSError`，并让最终 `PASS` 行带上「记录不完整」标记；
4. 文档里「三个 Job 角色及其五组授权」改为与 `compose-minimal.md` 一致的组数（writer 8 组 + admission 1 组 + dispatch 5 组 + `alembic_version`/基线 SELECT/UPDATE），或改成不列数字。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | 建独立 worktree `/private/tmp/sn667`（`origin/main` = `035e3c16`） | 与主检出、既有 `sn655`/`sn664`/`sn665`/`sn666` 隔离 |
| T1 | 读 `8281c2bb` 全量 diff（4 文件，+1021/−183）与三份文件现状（脚本 1604 行、测试 1119 行、文档 148 行） | 定位 B1/B2/B3 的新实现与 5 条候选新路径 |
| T2 | 跑 39 项测试（`python3 -m unittest tests.ci.test_reference_acceptance`） | `Ran 39 tests ... OK`（基线 16 → 39，+23 与提交信息一致） |
| T3 | 真实 `provision-job-roles.py --print-statements`（`uv run --all-packages --locked`） | 221 行；用脚本自己的解析器跑出 596 项检查 |
| T4 | 真实 PG 16.15 + 本检出迁移（head `0053`、83 表）+ 真 provision 跑 `roles` 步首跑/重跑 | 首跑 `PASS … grants: 221 statements；596 项…全部在位；8 个探针`；重跑幂等 `PASS` |
| T5 | 真实撤权 4 组：探针外表 SELECT、单列 UPDATE、schema USAGE、表级多授 | 前三条全部按预期红并点名；表级多授（含列级升表级）**绿**（包含比对方向，文档已声明） |
| T6 | 7 组测试级变异（改一行、跑原测试、复原） | 7/7 都让对应用例变红 |
| T7 | 真实 Docker 构建失败语义（单坏构建、pip 失败、双目标失败、旧容器 healthy 重建失败） | `up` 全部退出 1；旧容器重建失败时 `ps` 仍 running/healthy；错误行落在尾 6 行内 |
| T8 | 真实 `--dry-run`（真 `deploy/compose.yaml`、真 env 文件）与缺 `POSTGRES_USER/DB` 的 dry-run | `DRY-RUN PASS steps=7 pending=2`、`commit=035e3c16`、零容器；缺 PG 键时 `tools` PLAN、`migrate` FAIL exit 20 |
| T9 | 假 runner 全绿/`up` 非 0/`git` 抛 OSError 三条整轮跑 | 全绿 PASS（`commit=unknown` 仍 PASS）；`up` 非 0 → exit 60 无最终 PASS；`git` OSError → traceback 逃逸 |
| T10 | 收尾：删隔离容器/网络/卷/镜像与临时目录；`git status --porcelain` 为空 | 只留本报告 |

---

## 环境与复现命令

```text
worktree  /private/tmp/sn667            branch review/667-acceptance-rework（基线 035e3c16）
隔离 PG   compose 项目 sn667rev，postgres:16.15-alpine，127.0.0.1:56632
          （只用于 roles 步：真迁移 + 真 provision + 真 ACL 读）
构建实验  sn667bf / sn667bf2 / sn667bf3 / sn667b1（坏 Dockerfile，端口 56644/56655）
探针目录  /tmp/sn667probe（compose.yaml、review.env、full.env、drive_roles.py、mutate.py；收尾已删，原文见附录）
```

关键命令（可复现）：

```bash
# 1) 现有测试与全量 tests/ci
cd /private/tmp/sn667 && python3 -m unittest tests.ci.test_reference_acceptance -q
python3 -m unittest discover -s tests/ci -q          # 173 tests OK（8281c2bb 时点同为 173）

# 2) 真实渲染 221 条语句并用脚本自己的解析器展开
export SPEECHNEXUS_JOB_WRITER_DATABASE_URL='postgresql://sn_job_writer:pw@postgres:5432/db' \
       SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL='postgresql://sn_job_admission:pw@postgres:5432/db' \
       SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL='postgresql://sn_job_dispatch:pw@postgres:5432/db' \
       SN_GATEWAY_ROLE=sn_gateway
uv run --all-packages --locked python scripts/dev-env/provision-job-roles.py --print-statements \
  > /tmp/sn667_grants.txt      # 221 行

# 3) 真 PG：起库 → 真迁移 → 建 sn_gateway → 跑 roles 步
docker compose -p sn667rev -f /tmp/sn667probe/compose.yaml --env-file /tmp/sn667probe/review.env \
  up -d --wait postgres
SPEECHNEXUS_MIGRATION_DATABASE_URL='postgresql://speechnexus:change-me-local-postgres@127.0.0.1:56632/speechnexus' \
  uv run --all-packages --locked speechnexus-job-migrate --schema speech
python3 /tmp/sn667probe/drive_roles.py        # 必须在仓库根目录跑（uv 需要 project）

# 4) 7 组变异（自动改一行→跑测试→复原）
python3 /tmp/sn667probe/mutate.py

# 5) 真实 dry-run（真 compose 文件）
SN_ADMIN_DSN='postgresql://speechnexus:pw@127.0.0.1:55432/speechnexus' \
  python3 scripts/dev-env/reference-acceptance.py --dry-run --project-name sn667dry \
  --env-file /tmp/sn667probe/full.env
```

---

## 逐条清单结论与证据

行号取自 `origin/main`（`035e3c16`）的 `scripts/dev-env/reference-acceptance.py`。

### 1. B1 是否真修好且无残余

**(a) 失败时任何路径都不会继续到 PASS —— 是。**

- 实现：`:1269` `started = ctx.mutate(compose("up","-d","--build","--wait","api","gateway"))`，`:1270-1277` `if not started.ok: raise StepFailureError(f"栈未起来（docker compose up 退出码 {started.code}）：{started.tail(6)}", …)`。判码在判状态**之前**，`surface_command_output` 在判码**之后**。
- 整轮实测（假 runner：`up` 返回 1、`ps` 返回 api/gateway healthy）：`EXIT=60`、`结论: FAIL — 栈未起来（docker compose up 退出码 1）…`、`未执行: gate, health`、`reference acceptance: FAIL step=stack exit=60`，**没有** `reference acceptance: PASS`（[R1](#r1整轮up-非-0--旧容器健康必红)）。
- 真实 Docker 语义（我自己复现，[R2](#r2真实-compose坏重建--旧容器仍健康up-退出-1)）：先成功起 healthy 容器（容器 ID `24ce2aba974c`），把 Dockerfile 改坏 `COPY no_such_file.py` 后 `up -d --build --wait` → **`SECOND_UP=1`**，而 `compose ps` 仍是 `"State":"running","Health":"healthy"` 且**容器 ID 未变**（旧容器没被替换）。即上一轮 B1 的原始触发条件在 `035e3c16` 仍然真实存在，新代码在该条件下红。

**(b) 「尾 6 行」是否足以定位构建错误 —— 部分足够；根因行会丢。**

三组真实构建失败（[R3](#r3真实构建失败输出形态)）：

| 场景 | 输出行数 | `failed to solve` 距末尾 | 尾 6 行是否含错误行 |
| --- | --- | --- | --- |
| 单坏 `COPY missing_file.py` | 54 | 第 6 行 | 含（**正好在边界**） |
| `pip install` 不存在包 | 79 | 第 6 行（后随 4 空行 + `View build details`） | 含 |
| 双服务都坏（`target worker` 报错） | 62 | 第 4 行 | 含 |

结论：buildx 把「哪条指令失败」的 `failed to solve` 放在接近末尾，尾 6 行**在常见形态下够用**；但**完整构建日志被 `subprocess.run(capture_output=True)` 吞掉后丢弃**，`pip`/编译器/网络的具体原因（例如 `ERROR: Could not find a version that satisfies the requirement …`）落在更早的行，运维拿不到。`run_steps` 只打印 `结论: FAIL — <message>`，`message` 里只有 `tail(6)`。这不是假通过，但「构建报错可见」只到「哪一步失败」这一层，不到「为什么」。文档 `reference-acceptance.md` 的修法表写「看错误信息里的构建输出」，措辞与能力相符，但值得在下一轮考虑 `--verbose` 或落盘完整输出。

**(c) 有没有别的步骤「只跑命令不判码」 —— 没有。**

逐处核对全部调用点：`:457`（`psql_query` 判 `result.ok`）、`:712`（`compose_config` 判）、`:763`（`compose_services` 判）、`:866/:872/:885/:903`（tools 判）、`:937/:940`（migrate 判 `started.ok`/`applied.ok`，`--build` 后）、`:979/:999`（roles 判 `provisioned.ok`/`rendered.ok`）、`:1058/:1061/:1067`（buckets 判三处）、`:1185`（gate 走 `gate_verdict`，同时要求 `result.ok` + `OK` 行）、`:1269`（stack 判，本片修复）、`:1302`（health 判 HTTP 状态）。`:1417`（`git rev-parse`）与 `:1435`（`compose images`）是记录用 best-effort，不参与判定（见 §4）。另外 `privilege_gaps` 从 `WHERE NOT v.ok` 改成 `WHERE v.ok IS NOT TRUE`（`:509`、`:687` 两处，`privilege_gaps` 与 `acl_gap_batch`），把 NULL 从「当作通过」改成「当作缺口」，方向是收紧，顺带修掉一个潜在假通过。

### 2. B2 的 ACL 比对是否真比对

**(a) 解析器对多权限 / 带引号标识符 / 列级 / schema 级 —— 都对（并且我在真实渲染上验过）。**

- 真实 `--print-statements`：**221 条**（writer/admission/dispatch 合计，与 `provision-job-roles.py` 首跑打印的 `grants: 221 statements` 一致）。用脚本自己的 `rendered_grant_checks` 解析：**596 项检查**，其中 `has_schema_privilege` 20、`has_table_privilege` 112、`has_column_privilege` 464；38 条检查标签重复（同一 role/对象/权限被多条语句覆盖），`declared_grant_gaps` 按标签 `setdefault` 去重后 558 项，单批 ≤300 分两批下发。
- 多权限：`GRANT SELECT, INSERT ON …` → 2 项（用例 `test_parses_the_rendered_shapes` 覆盖）。
- 带引号标识符：真实渲染是 `"speech".coordination_jobs` 混排（schema 带引号、关系不带），`unquote_identifier` 只剥一层引号后校验 `^[a-z][a-z0-9_]{2,62}$`，两种形态都能解析；`GRANT UPDATE (claim_owner_id, "claim_version") ON …` 的混合引号列也对。
- 列级：一条语句展开成每列一项（89 条列级语句 → 464 项检查）；我撤掉 `UPDATE (claim_version)` 后，同一语句的 4 列全部报缺（PG 的 `REVOKE UPDATE ON table` 会把列级 ACL 一并撤掉，这是 PG 语义，不是脚本问题，见 [R5](#r5真实撤权矩阵)）。
- schema 级：`GRANT USAGE ON SCHEMA "speech"` → `has_schema_privilege('role','speech','USAGE')`；撤 `USAGE ON SCHEMA speech FROM sn_job_admission` 后红并点名 `sn_job_admission 缺少 SCHEMA speech 的 USAGE`。

**(b) 解析不了的形态会不会被静默跳过 —— 不会，全部抛错。**

`parse_grant_statement` 只接受 `GRANT … ON … TO …;`，`ON SCHEMA`、`schema.relation`、列列表三种目标形态；其它（`ON ALL TABLES IN SCHEMA`、`ON FUNCTION`、`ON SEQUENCE`、`WITH GRANT OPTION`、未知权限名、非 `GRANT` 行、空输出）全部 `raise StepFailureError`。变异 M7（把 `checks.extend(parse_grant_statement(...))` 改成 `try/except StepFailureError: continue`，即还原老行为）让 `test_unknown_grant_shape_fails_loudly` 变红——用例确实守着「不许静默跳过」。

**(c) 包含比对的方向 —— 是「声明的都在已装」（包含），多授会被放过。**

- 用的方向正确：验收要回答的是「部署缺不缺运行时需要的授权」，所以需要 `declared ⊆ installed`；反过来会因基线/公共授权误红。
- **多授确实放过**，包括列级升表级：真实库上 `GRANT UPDATE ON speech.coordination_jobs TO sn_job_writer`（声明只授 4 个列）后重跑 `roles` → 仍 `PASS … 596 项…全部在位`（[R6](#r6多授包含列级升表级被放过)）。这是**最小权限方向不被验证**（安全的「多授」一侧），代码注释与文档都明说「Extra grants are not an error」，且这正是上一轮审查给的最小改法选项 1，因此不算返修引入的假通过，但要在「能证明什么」里按现状理解：它能证明「该有的都在」，不能证明「多授权已收敛」。

### 3. B3 的顺序改动有没有引入新问题

**(a) 文档与实际顺序一致 —— 一致。**

- 代码 `STEPS`（`:1350` 起）：`tools, migrate, roles, buckets, stack, gate, health`；`stack` 的 `exit=60`、`gate` 的 `exit=50` 未改。
- 文档首行顺序改为「工具 → 迁移 → 角色 → 桶 → 栈 → 启动闸门 → 健康」，步骤表顺序同步（`stack` 行在前），并显式解释「退出码标识步骤，不表示行序：栈是 60，闸门是 50，两者相邻且栈在前」；`docs/operations/README.md` 的括注也改成「工具/迁移/角色/桶/栈/启动闸门/健康」。
- `--help` 的 epilog 步骤表与实际一致（`5. stack exit=60 / 6. gate exit=50`），并新增「栈在闸门之前：… gateway 未 healthy 时 compose run 无法启动」。
- 真实 `--dry-run` 输出 `steps=7 order=tools, migrate, roles, buckets, stack, gate, health`，与文档逐字一致。

**(b) `gate` 的 gateway healthy 前置仍存在 —— 存在。**

`:1166-1172` `ctx.require(service_healthy(ctx,"gateway"), "gateway 未 healthy；…")` 保留；`test_gate_without_a_healthy_gateway_is_red` 单独跑 `gate` 仍 exit 50 且点名「gateway 未 healthy」；`gate_verdict` 的 `cannot share network namespace` 兜底文案改成指向「先跑 stack 步」而不是「准备 §3 私有配置」，归因正确。

**(c) 有没有哪一步的前置检查依赖了更晚的步骤 —— 没有。**

逐条核：`migrate` 只依赖自身起的 postgres；`roles` 依赖 `migrate`（更早）与 §6 基线角色（手工前置）；`buckets` 只依赖 minio；`stack` 依赖私有配置（手工前置）、基线角色（手工前置）与 `buckets`（更早，gateway `depends_on: minio-init: service_completed_successfully`）；`gate` 依赖 `stack`（更早，本片刻意为之）；`health` 依赖 `stack`。没有反向依赖。

**顺带核实（B3 的真实收益）**：从零默认模式现在会走到 `stack` 并在 `up` 处按真实原因失败（构建/私有配置/模型），不会在 `gate` 以「gateway 未 healthy + 准备 §3」误归因。上一轮 B3 的核心症状消失。残留的一点是**措辞**：文档「一条命令」的第 2 步注释写「默认 compose 模式即可从零跑」，而真正从零仍需先做 §6 基线角色与 §3 私有配置（前置输入表已列），这句话容易读成「无需前置」。

### 4. 新引入的假通过（重点）

逐条找「返修过程中新增的、可能在该红时变绿」的路径。**没有找到新的硬假通过**；找到 3 条「绿得比该有的多」与 2 条方向相反的（该绿却红/丢结论）新路径，按严重度排列：

1. **记录 best-effort 与 PASS 解耦（新增，软假通过）**。`git_commit`（`:1415`）失败返回 `unknown`、`images_record`（`:1433`）失败返回「不可用」/「无」，两者都不影响判定；`print_records`（`:1460`）在最终 `PASS` 行之前打印。实测：把 `git` 全部返回 128（模拟非 git 检出/拷贝部署）→ 整轮 **`PASS steps=7` + `commit=unknown`**（[R7](#r7记录不可用仍-pass)）。#572 的「记录环境/提交/镜像」要求因此可以「记录为空但判 PASS」，机器读取者只看最后一行会以为记录齐备。文档把它描述成已实现（示例行给的是真实 commit），没有说「可能是 unknown」。
2. **`print_records` 在异常处理内、不在 try 内（新增）**。失败分支里 `print_records` 排在 `reference acceptance: FAIL` 之前，若它自身抛出（`Runner.run` 只兜 `FileNotFoundError`/`TimeoutExpired`，其它 `OSError` 会逃逸），步骤退出码与 FAIL 行都会丢，进程以 traceback + exit 1 结束。实测（假 runner 让 `git` 抛 `PermissionError`）→ `ESCAPED: PermissionError`，违背文档「步骤失败以外的未预期异常也按步骤归口：不抛 traceback、不回退成 Python 默认退出码 1」的新增承诺（[R8](#r8records-打印逃逸)）。方向是「该红却以 traceback 退出」，不是假绿，但它是本轮新增代码引入的**新破口**。
3. **`--build` 的引入没有制造假绿，只有副作用**。`migrate`（`:929`）与 `gate`（`:1087`、`gate_direct_argv`）加 `--build` 后：构建失败 → `compose run` 非 0 → 红；构建成功 → 用的是本检出代码。核对 `compose.yaml`：`migrate` 服务没有把迁移目录 bind-mount（`read_only: true`，无 volume），所以镜像内代码就是迁移代码，`--build` 是 N11/D8 的必要修法；`job-startup-gate.py` 与私有配置是 bind-mount，`--build` 只保证依赖面。真实 `docker compose run --help`（本机 Compose v5.7.0）确认 `--build` 与 `-v` 都是 `run` 的合法参数。副作用：每轮验收都要重建镜像（时间/网络成本），且 `buckets` 步的 `compose run minio-init` **没有**加 `--build`（脚本 mount 了 `init-object-storage.py`，方向可接受但口径与 migrate/gate 不一致）。
4. **`surface_command_output`（新增）不参与判定**。只在 `up` 成功路径打印尾 6 行（`:1278`）；失败路径只把 `tail(6)` 拼进错误消息。没有假绿，但错误消息里的多行输出不缩进（实测 `failed to solve: boom` 顶格），可读性一般。
5. **`run_steps` 的兜底异常（新增）不吞步骤名**。`:1490` 起 `except Exception` 打印 `FAIL step=<id> exit=<code>` 并返回该步退出码；变异 M4（把 `except Exception` 收窄成 `ZeroDivisionError`）让 `test_unexpected_exception_gets_the_step_name_and_exit_code` 变红，说明用例守着。这条兜底把一个原本会 traceback 的编程错误变成「带步骤名的红」，方向正确；唯一的边角是它也会把 dry-run 的 `mutate` 守卫 `RuntimeError` 变成「FAIL」而不是「守卫被触发」，仍是非 0 退出。

### 5. 23 条新用例是否有牙

`tests/ci/test_reference_acceptance.py` 从 16 项涨到 39 项（`grep -c "    def test_"` = 39，父提交 = 16），+23 与提交信息一致。我独立做了 **7 组变异**（改一行 → 跑原测试 → 复原；脚本每次从内存写回并断言复原）：

| 变异（自己删/改守卫） | 结果 |
| --- | --- |
| M1 删 `if not started.ok: raise`（B1 守卫） | 红 1：`test_up_exit_nonzero_with_healthy_ps_is_red` |
| M2 `failures = []`（取消 FAIL 优先） | 红 1：`test_ok_and_fail_together_is_red_even_on_exit_zero` |
| M3 `declared_grant_gaps` 直接 `return []`（B2 守卫） | 红 2：`test_idempotent_branch_detects_a_revoked_declared_grant`、`test_idempotent_branch_verifies_the_installed_acl` |
| M4 `except Exception` → `except ZeroDivisionError` | 红 1：`test_unexpected_exception_gets_the_step_name_and_exit_code` |
| M5 `migrate` 去掉 `--build` | 红 1：`test_migrate_step_builds_before_running` |
| M6 把 `gate` 排回 `stack` 之前 | 红 3：`test_stack_precedes_gate`、`test_fresh_environment_fails_at_stack_not_gate`、`test_behind_migration_stops_before_roles_buckets_and_stack` |
| M7 未知 GRANT 形态改成 `continue`（静默跳过） | 红 1：`test_unknown_grant_shape_fails_loudly` |

特别验证了审查方点名的两条：

- **`StackExitCodeTest`**：`up` 非 0 + `ps` healthy → 必须红。M1 证明它守着；我又用真实 compose 复现了输入形态（[R2](#r2真实-compose坏重建--旧容器仍健康up-退出-1)），二者一致。
- **`IdempotencyTest`（撤销声明授权变红）**：M3 证明它守着；真实 PG 上撤销 `speech.speech_publications` 的 `SELECT` 后 `roles` 步 `FAIL step=roles exit=30` 并点名（[R4](#r4上一轮-b2-反例在真实-db-上转红)）。

不足（非阻塞）：`test_migrate_step_builds_before_running` / `test_gate_compose_step_builds_before_running` 只断言 argv 里出现 `--build`，不验证「构建后运行的是新镜像」；`step_stack` 的「`up` 退出 0 但服务 unhealthy」分支、`step_buckets` 的桶数/版本化分支、`--gate-mode direct` 的 argv 仍没有用例。

### 6. 测试名与断言是否相符

大体相符，发现 3 处「名/断言/自述」不一致：

1. **`test_full_pass_records_steps_environment_commit_and_images`（名字说记录 commit，断言只查 `commit=` 字样）**：假 runner 对未知命令返回空 stdout，`git_commit` 得到 `unknown`，用例仍绿——实测整轮 `PASS` 时该行是 `commit=unknown`。记录 commit 这一性质**实际没有用例守护**。
2. **`test_gate_after_a_healthy_stack_passes`（名字说「stack 之后」）**：实际只把 `ps` 伪装成 healthy，没有先跑 `stack` 步；验证的是 gate 的前置读取，不是顺序。
3. **脚本自身自述未更新（不是测试，但属于「改了断言忘改名」同类）**：`Step("roles", …, expected="job roles ready: ... grants: N statements…")`（`:1375`）与步骤标题「预置三个非 owner Job 角色（幂等）」（`:1371`）仍是旧口径；入口从不打印 `job roles ready`（那是 `provision-job-roles.py` 的行，被解析后丢弃）。文档步骤表已改成实际输出（D5），脚本自述没跟上。

另有一处**提交信息事实偏差**：`8281c2bb` 称「158 项 tests/ci 全绿」，我实测 `python3 -m unittest discover -s tests/ci -q` 在该提交与 `035e3c16` 上都 `Ran 173 tests OK`（`tests/ci/*.py` 的 `def test_` 计数也是 173）。不影响代码，只是记录不准。

### 7. D1–D8 落地情况

上一轮报告用的是 D1–D5（文档逐键比对）+ N1–N13；本次委托把非阻塞项概括为 D1–D8，按括号里的七项 + 「文档措辞」拆成八条对应，逐条核：

| # | 项 | 落地 | 证据 |
| --- | --- | --- | --- |
| D1 | 键分类（「必需键由 `${KEY:?}` 决定」） | **部分落地** | 脚本拆出 `compose_env_keys` 并在 tools 结论里分开计数；真实 compose 派生 **14** + 额外 **6** = **20**，文档「当前 14 个」准确，dry-run 实测「14 个 compose 派生键 + 6 个入口额外键（共 20 个）非空」。**但** `POSTGRES_USER`/`POSTGRES_DB` 仍被 `psql_argv` 直接索引且不在任何一类里：缺它们时 tools PLAN、migrate FAIL exit 20（[R9](#r9postgres_userposgres_db-是第三类未声明必需键)），文档「必需键有两个来源」仍不完整（上一轮 N2 的点只修了措辞与计数） |
| D2 | 桶名键进「前置输入」表 | **落地** | 新增「两个桶名键」行，并说明 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 可省略、省略时等于 Artifact 桶；与 `bucket_names` 规则一致 |
| D3 | 「一条命令从零」的顺序/退出码语义 | **落地** | 顺序改栈在闸门前；文档明确「退出码标识步骤，不表示行序：栈是 60，闸门是 50」；`--help` 同步。残留措辞：「默认 compose 模式即可从零跑」仍需 §6/§3 前置（已在前置输入表列） |
| D4 | 并行性声明 | **落地** | 文档改为「固定 `172.28.0.0/24`，第二个参考栈会 `Pool overlaps`；`--project-name` 用于重跑/清理隔离，不用于并行部署」；脚本 docstring 同步；核对 `deploy/compose.yaml:584` 仍是 `subnet: 172.28.0.0/24` |
| D5 | roles 输出格式（`job roles ready` → 实际） | **部分落地** | 文档步骤表改为首跑/重跑两段实际文案，与真实输出逐字一致（`已创建；grants: 221 statements；596 项声明授权与已装 ACL 逐条核对全部在位；8 个授权探针通过`）；**但**脚本 `step.expected`（`:1375`）与 `--help` 的步骤标题仍写 `job roles ready: …` 与「非 owner」（见 §6.3） |
| D6 | `InvalidURL`/非 `StepFailureError` 兜底 | **落地** | 新增 `host_port`（1..65535 整数校验）+ `http_json` 增捕 `http.client.HTTPException`/`ValueError` + `run_steps` 兜 `except Exception`。用例 `test_malformed_port_is_a_named_step_failure`、`test_http_json_returns_an_error_instead_of_raising` 守护；M4 变异证明兜底有牙。破口见 §4.2（records 路径） |
| D7 | `gate_verdict` 优先级 | **落地** | `FAIL:` 先判，再判 `OK` + `exit 0`；用例 `test_ok_and_fail_together_is_red_even_on_exit_zero`（exit 0 与 1 两种）守护；M2 变异变红。核对 `deploy/job-startup-gate.py` 的实际输出串（`  ok: ` / `  pending: ` / `  FAIL: ` / `job dispatch startup gate: OK`）与解析器一致 |
| D8 | `--build`（旧镜像） | **落地** | `migrate`/`gate`（含 direct）加 `--build`；`migrate` 服务无 bind-mount，镜像内就是迁移代码，这条是必要的；用例 `BuildFlagTest` 两项守护，M5 变红。`buckets` 的 `run` 未加 `--build`（mount 了脚本，影响小，口径不一） |

上一轮 N 项的现状（供下一位维护者）：N3（gate OK/FAIL 优先级）已修；N4（非 StepFailure 逃逸）已修但 records 路径仍可逃逸；N8（并行声明）已修；N10（roles 输出）文档已修、脚本自述未修；N11（旧镜像）已修；N13（环境/镜像/步骤记录）已实现但 best-effort（见 §4.1）；N1（「非 owner」不校验）改为**文档明示不判定**（`reference-acceptance.md:28-30`），步骤标题仍写「非 owner」；N2（键分类）部分修（见 D1）；N5（apply 模式 PASS 行不带 pending）、N6（桶规则第二份权威）、N7（schema 未传给 provision）、N9（`SN_ADMIN_DSN` owner 未校验）**未动**；N12（闸门时点）已被 `2ba4471a` 修复，本入口继承的是收紧后的闸门（`git log -- deploy/job-startup-gate.py` = `2ba4471a`、`8404efad`）。

### 8. 跨片一致性（与 `1e9f2b12`）

同轮 `1e9f2b12`（#654，DSN 覆盖）改了 `deploy/compose.env.example` 与 `docs/operations/compose-minimal.md`（§6 扩到 14 个非 owner 角色 + 透传 DSN 覆盖表），未改 `deploy/compose.yaml`。核对结论：

- **不矛盾的部分**：`reference-acceptance.md` 仍只声明验 3 个基线角色（`sn_api`/`SN_GATEWAY_ROLE`/`sn_gateway_query`）+ 三条 Job DSN，并明写「本入口不重做 §6 的撤权与基线角色」；`compose-minimal.md` §6 的 14 个角色里包含这三个，验收不因新增 11 个身份而增加环境要求（`compose.yaml` 未变，`${KEY:?}` 仍是 14 个，实测 14+6=20，我逐键核对 `compose_env_keys`）。`compose-minimal.md` 的 Job 角色节仍写「三个互不相同的非 owner 角色」，与验收只查「互不相同」不冲突（首跑由 provision 拒绝 `superuser/createdb/createrole`，文档已披露幂等分支不重做）。
- **仍矛盾的一处（文档措辞，非阻塞）**：`reference-acceptance.md:17` 仍写「三个 Job 角色及其**五组**授权」，而 `compose-minimal.md` 与 `provision-job-roles.py` 的授权组是 writer 8 组（`job_runtime`/`job_dispatch`/`public_job`/`voice_draft`/`public_job_submission`/`job_cancellation_fence`/`cancellation_not_submitted`/`cancellation_reconciliation`）+ 基线 `SELECT`/列级 `UPDATE`、admission 1 组、dispatch 5 组（`job_runtime`/`job_dispatch`/`public_job`/`cancellation_reconciliation`/`coordination_runtime`）+ `alembic_version` 只读。本轮返修重写了这句所在的整条 bullet，却没改「五组」，属于**返修漏改**（上一轮就存在，但本片动了这一行）。
- `docs/reviews/2026-11-13-dsn-coverage.md` §4.8 的判断（「与 #653 的 `reference-acceptance.md` 没有直接矛盾」）与本轮实测一致；该判断的前提「验收入口仍只管 3 个基线角色 + Job 三键」在 `035e3c16` 仍成立。
- 另有一处**内部**措辞张力（不是跨片）：文档「不证明…也不判定角色是否非 owner」与 `STEPS` 标题「预置三个非 owner Job 角色」/`--help` 同句并存，读者会以为入口校验了非 owner。

---

## 新引入的假通过与新路径清单

按「是否可能在该红时变绿」排序，逐条给证据与定性：

| # | 路径 | 定性 | 证据 | 建议 |
| --- | --- | --- | --- | --- |
| P1 | `git_commit` / `images_record` best-effort 不进入判定 | **软假通过**：记录要求（#572 第 4 条）可为空而 `PASS steps=7` | [R7](#r7记录不可用仍-pass)：`git` 全 128 + `compose images` 正常 → `PASS` 且 `commit=unknown`；反向（images 失败）→「镜像记录: 不可用」仍 PASS | 最终 PASS 行带 `records=incomplete`，或文档声明该行为 |
| P2 | `print_records` 在 `except` 内且不在 try 内 | 方向相反（该绿却 traceback/exit 1），**新破口** | [R8](#r8records-打印逃逸)：`git` 抛 `PermissionError` → `run_steps` 逃逸，无 `FAIL step=` 行、无步骤退出码 | 把 `print_records` 包进 `try/except Exception`，或 `Runner.run` 兜 `OSError` |
| P3 | ACL 只做包含比对（声明 ⊆ 已装） | **设计内、已声明**，但多授（含列级升表级）绿 | [R6](#r6多授包含列级升表级被放过)：`GRANT UPDATE ON speech.coordination_jobs`（声明只授 4 列）后仍 PASS | 若要证明最小权限，需要反向比对或单独的「多授」报告（可只告警不判红） |
| P4 | `POSTGRES_USER`/`POSTGRES_DB` 第三类必需键未声明 | 方向相反（该在 tools 红，实际 migrate 红，误归因） | [R9](#r9postgres_userposgres_db-是第三类未声明必需键)：dry-run 下 tools PLAN、「20 个键非空」，migrate FAIL `POSTGRES_USER / POSTGRES_DB 为空` exit 20 | 加进 `mandatory_env_keys`（或从 compose 解析默认值），并把失败归到 tools |
| P5 | `parse_grant_statement` 的标识符白名单 `^[a-z][a-z0-9_]{2,62}$` | latent 假红：未来出现 `id` 这类 1–2 字符列名、或 `SN_GATEWAY_ROLE` 用大写，会以「标识符不是安全的小写标识符」失败并归因到渲染器 | 代码静态（`:551-561`）；当前 221 条语句全部通过，故未触发 | 放宽为 `^[a-z_][a-z0-9_$]*$` 或直接按 `psycopg` 渲染结果解析 |
| P6 | `step.expected` / 步骤标题仍是旧口径 | 非判定问题，但每次运行都把「预期输出: job roles ready…」打给运维，与实际不符 | 代码 `:1371`、`:1375`；真实运行输出对照 | 与文档同步 |

**没有**发现：`up` 失败继续到 PASS、`gate` OK/FAIL 同时出现被当通过、未知 GRANT 形态静默跳过、异常被跳过而总结仍 PASS、dry-run 触发写操作、`--build` 把红判绿。

---

## 我独立复现的反例（原始记录）

均为本机实测；脚本本身只在变异矩阵里被临时改过并逐次复原（`restored: True`）。

### R1：整轮 `up` 非 0 + 旧容器健康必红

假 runner：`up api gateway` 返回 1，`ps` 返回 api/gateway `running/healthy`，其余按已部署应答；整轮 7 步：

```text
[5/7] stack — API 与 gateway 起来
      结论: FAIL — 栈未起来（docker compose up 退出码 1）：api Building
failed to solve: boom
      需要输入: 看上面构建输出修复 Dockerfile/构建参数；旧容器仍可能是 running/healthy，ps 的状态不能替代 up 的退出码
      未执行: gate, health
步骤记录: tools=PASS migrate=PASS roles=PASS buckets=PASS stack=FAIL 未运行: gate, health
环境记录: repo=… commit=unknown project=sn-test mode=apply gate=compose schema=speech
镜像记录: api=sn-test-api:latest@sha256:01234
reference acceptance: FAIL step=stack exit=60
EXIT= 60
```

### R2：真实 compose「坏重建 + 旧容器仍健康」，`up` 退出 1

```text
# 项目 sn667b1，先成功构建并 healthy
FIRST_UP=0
{"State":"running","Health":"healthy","ID":"24ce2aba974c","Image":"sn667b1-web"}
# 把 Dockerfile 改成 COPY no_such_file.py 后
SECOND_UP=1
{"State":"running","Health":"healthy","ID":"24ce2aba974c","Image":"sn667b1-web"}   # 旧容器未替换
=== tail6:
failed to solve: failed to compute cache key: failed to calculate checksum of ref …: "/no_such_file.py": not found



View build details: docker-desktop://dashboard/build/default/default/jn52c0wkvrttszarcwrwhdbuo
```

### R3：真实构建失败输出形态（尾 6 行够不够）

```text
单坏 COPY：        54 行，failed to solve 在第 49 行（距末尾第 6 行）
pip 不存在包：     79 行，failed to solve 在第 74 行（距末尾第 6 行，后随 4 空行 + View build details）
双服务都坏：       62 行，尾 6 行含 "target worker: failed to solve: … /absent_worker.py: not found"
```

### R4：上一轮 B2 反例在真实 DB 上转红

真实 PG（迁移 head `0053_voice_draft_publication`、83 表）+ 真 provision 首跑/重跑：

```text
首跑：结论: PASS — sn_job_writer sn_job_admission sn_job_dispatch 已创建；grants: 221 statements；
      596 项声明授权与已装 ACL 逐条核对全部在位；8 个授权探针通过      RUN_EXIT=0
重跑：结论: PASS — 三个角色已存在（幂等：未重复 CREATE ROLE，未新增授权）；
      重渲染 221 条 GRANT、596 项授权与已装 ACL 逐条核对全部在位；8 个授权探针通过   RUN_EXIT=0

# 上一轮 R3 的原样反例：REVOKE 探针外授权
REVOKE SELECT ON speech.speech_publications FROM sn_job_writer;   -- 独立读数 f
结论: FAIL — 已装 ACL 与声明的 221 条 GRANT 不一致：1 项缺失 —
      sn_job_writer 缺少 speech.speech_publications 的 SELECT       RUN_EXIT=30
```

即上一轮「撤销探针外授权仍 PASS」的缺陷**已消失**；上一轮 R2（只装 8 条探针）在新实现下按 596−8=588 项缺失推导必红（未重跑；机制已由上面「缺 1 项即红」实测证明）。

### R5：真实撤权矩阵

```text
REVOKE UPDATE (claim_version) ON speech.coordination_jobs FROM sn_job_writer
→ FAIL：4 项缺失 — claim_owner_id / claim_expires_at_us / claim_version / job_state 的 UPDATE
  （PG 语义：REVOKE UPDATE ON table 会连带撤列级 ACL，列级检查逐列报缺，说明列级展开是真的）

REVOKE USAGE ON SCHEMA speech FROM sn_job_admission
→ FAIL：… sn_job_admission 缺少 SCHEMA speech 的 USAGE  （schema 级检查有牙）
```

### R6：多授（含列级升表级）被放过

```text
GRANT UPDATE ON speech.coordination_jobs TO sn_job_writer;   -- 声明只授 4 列
has_table_privilege(...) = t
结论: PASS — 三个角色已存在（幂等…）；重渲染 221 条 GRANT、596 项授权与已装 ACL 逐条核对全部在位；…
RUN_EXIT=0
```

### R7：记录不可用仍 PASS

```text
假 runner：git 全部返回 128（模拟非 git 检出/拷贝部署），其余全绿
步骤记录: tools=PASS migrate=PASS roles=PASS buckets=PASS stack=PASS gate=PASS health=PASS
环境记录: repo=/tmp/… commit=unknown project=sn-test mode=apply gate=compose schema=speech
镜像记录: api=sn-test-api:latest@sha256:01234
reference acceptance: PASS steps=7
FULL PASS EXIT= 0
```

### R8：`print_records` 逃逸

```text
假 runner：git 抛 PermissionError("git is not executable for this user")，其余全绿
ESCAPED: PermissionError git is not executable for this user
—— 没有 reference acceptance: PASS / FAIL 行，进程以 traceback 结束（Python 默认 exit 1）
```

### R9：`POSTGRES_USER`/`POSTGRES_DB` 是第三类未声明必需键

真实 dry-run（真 `deploy/compose.yaml`，env 文件删掉这两行）：

```text
[1/7] tools … 结论: PLAN — … 14 个 compose 派生键 + 6 个入口额外键（共 20 个）非空；compose 渲染通过
[2/7] migrate … 结论: FAIL — POSTGRES_USER / POSTGRES_DB 为空；无法用 postgres 服务查询
      未执行: roles, buckets, stack, gate, health
reference acceptance: FAIL step=migrate exit=20
```

同文件补上两行后：`reference acceptance: DRY-RUN PASS steps=7 pending=2`（`pending` = 私有配置目录缺失 + gateway 未 healthy），且 `docker ps -a --filter name=sn667dry` 为 0 个容器（dry-run 不写）。

### R10：变异矩阵

见 §5 表；7/7 变红，脚本逐次复原（`restored: True`）。

### R11：真实 dry-run 记录与环境

```text
步骤记录: tools=PLAN migrate=PLAN roles=PLAN buckets=PLAN stack=PLAN gate=PLAN health=PLAN
环境记录: repo=/private/tmp/sn667 commit=035e3c16 project=sn667dry mode=dry-run gate=compose schema=speech
镜像记录: 无（compose 项目还没有构建服务镜像）
reference acceptance: DRY-RUN PASS steps=7 pending=2
```

`order=tools, migrate, roles, buckets, stack, gate, health` 与文档首行逐字一致；`commit` 是真实短哈希（说明记录代码在 git 检出下工作，R7 的 `unknown` 只在 git 不可用时出现）。

---

## 附：复现脚本原文（隔离目录已删，可直接重建）

`/tmp/sn667probe` 已在收尾时删除；下面两段是本轮实际使用的驱动脚本原文，复现时重建即可。

`drive_roles.py`（真实 PG 上只驱动 `roles` 一步；**必须在仓库根目录运行**，`uv run` 需要 project）：

```python
import importlib.util, os, sys
from pathlib import Path

REPO = Path("/private/tmp/sn667")
spec = importlib.util.spec_from_file_location(
    "ra", REPO / "scripts/dev-env/reference-acceptance.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules["ra"] = m
spec.loader.exec_module(m)

env_file = Path("/tmp/sn667probe/review.env")
merged, child = m.effective_env(env_file, dict(os.environ))
merged = dict(merged)
merged["SN_ADMIN_DSN"] = (
    "postgresql://speechnexus:change-me-local-postgres@127.0.0.1:56632/speechnexus"
)
child = dict(merged)
ctx = m.Context(
    env=merged,
    env_file=env_file,
    compose_file=Path("/tmp/sn667probe/compose.yaml"),  # 只含 postgres 服务的临时 compose
    project="sn667rev",
    runner=m.Runner(),
    dry_run=False,
    gate_mode="compose",
    repo_root=REPO,
    child_env=child,
)
role_step = next(s for s in m.STEPS if s.ident == "roles")
code = m.run_steps((role_step,), ctx)
print("RUN_EXIT=", code)
```

`mutate.py`（7 组变异：改一行 → 跑原测试 → 复原并断言复原）：

```python
import subprocess, sys, pathlib

REPO = pathlib.Path("/private/tmp/sn667")
SCRIPT = REPO / "scripts/dev-env/reference-acceptance.py"
ORIG = SCRIPT.read_text()

MUTATIONS = {
    "M1 drop up exit-code check": (
        "    if not started.ok:\n"
        "        raise StepFailureError(\n"
        '            f"栈未起来（docker compose up 退出码 {started.code}）：{started.tail(6)}",\n'
        "            inputs=[\n"
        '                "看上面构建输出修复 Dockerfile/构建参数；"\n'
        '                "旧容器仍可能是 running/healthy，ps 的状态不能替代 up 的退出码",\n'
        "            ],\n"
        "        )\n",
        "",
    ),
    "M2 gate FAIL no longer decides first": (
        "    failures = [\n"
        '        line.strip().removeprefix("FAIL: ") for line in output.splitlines() if "FAIL: " in line\n'
        "    ]\n"
        "    if failures:",
        "    failures = []\n    if failures:",
    ),
    "M3 ACL comparison returns no gaps": (
        "    labels: dict[str, str] = {}\n    for check in checks:",
        "    return []\n    labels: dict[str, str] = {}\n    for check in checks:",
    ),
    "M4 catch-all narrowed to ZeroDivisionError": (
        "        except Exception as unexpected:  # one summary line, never a traceback",
        "        except ZeroDivisionError as unexpected:  # one summary line, never a traceback",
    ),
    "M5 migrate run without --build": (
        '"--profile", "migration", "run", "--build", "--rm", "migrate"',
        '"--profile", "migration", "run", "--rm", "migrate"',
    ),
    "M6 gate ordered before stack": (
        "def git_commit(ctx: Context) -> str:",
        "STEPS = STEPS[:4] + (STEPS[5], STEPS[4]) + STEPS[6:]\n\n\ndef git_commit(ctx: Context) -> str:",
    ),
    "M7 unparsed GRANT silently skipped": (
        "        checks.extend(parse_grant_statement(statement))",
        "        try:\n            checks.extend(parse_grant_statement(statement))\n        except StepFailureError:\n            continue",
    ),
}


def run_tests():
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.ci.test_reference_acceptance", "-q"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    failed = sorted(
        {
            line.split(" (")[0].strip()
            for line in proc.stderr.splitlines()
            if line.startswith(("FAIL:", "ERROR:"))
        }
    )
    return proc.returncode, failed


try:
    base = run_tests()
    print(f"baseline: exit={base[0]} failed={len(base[1])}")
    for name, (old, new) in MUTATIONS.items():
        assert old in ORIG, f"anchor missing for {name}"
        SCRIPT.write_text(ORIG.replace(old, new, 1))
        code, failed = run_tests()
        print(f"{name}: exit={code} red={len(failed)}")
        for item in failed:
            print("    -", item)
finally:
    SCRIPT.write_text(ORIG)
    print("restored:", SCRIPT.read_text() == ORIG)
```

`/tmp/sn667probe/compose.yaml`（只用于 `roles` 步的真 PG）：

```yaml
services:
  postgres:
    image: postgres:16.15-alpine
    environment:
      POSTGRES_USER: speechnexus
      POSTGRES_PASSWORD: change-me-local-postgres
      POSTGRES_DB: speechnexus
    ports:
      - "127.0.0.1:56632:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U speechnexus -d speechnexus"]
      interval: 2s
      timeout: 3s
      retries: 30
```

`/tmp/sn667probe/review.env` 的关键项：`POSTGRES_USER=speechnexus`、`POSTGRES_PASSWORD=change-me-local-postgres`、`POSTGRES_DB=speechnexus`、`SPEECHNEXUS_DATABASE_SCHEMA=speech`、`SPEECHNEXUS_MIGRATION_DATABASE_URL=postgresql://speechnexus:change-me-local-postgres@127.0.0.1:56632/speechnexus`、三条 Job DSN（`sn_job_writer`/`sn_job_admission`/`sn_job_dispatch`，口令任意）、`SN_GATEWAY_ROLE=sn_gateway`。

## 只能依赖作者陈述 / 未验证的点

1. **完整 `stack`/`health` 的真实 PASS 路径**（从零构建 api/gateway 到 healthy、四个探针 200）：需要审批过的私有 recipe、Silero ONNX 与镜像构建，我没有在隔离栈里完成；R2/R3 只验证了失败侧语义与输出形态。
2. **compose 模式闸门打印 `OK` 的真实路径**：真实闸门要有迁移、全量 dispatch 授权、两个桶、gateway 配置文件 `result_bucket` 一致；我只用假 runner 验证了 gate 步的解析与前置，没有在真实栈上构造出完整 `OK`。
3. **`--gate-mode direct` 的真跑**：只核对了 argv（`--build`/`-v`/`-e`/`--no-deps`）与 `compose run --help` 的参数合法性；没有真跑（需要 python 运行镜像与批准模型）。
4. **真实引擎/已注册 ready 实例与一次 asr/tts Job 的端到端**（#572 最终验收）：作者与我都没有跑；脚本把两项列 `pending` 是诚实的。
5. **`SPEECHNEXUS_JOB_DISPATCH_KINDS` 含 `voice_validation`** 时的额外授权组：没有用该配置实跑（默认为 `asr,tts`，221 条即默认面）。
6. **CI 实际结果**：只跑本地（39 项 + `tests/ci` 173 项全绿），没有观察 GitHub Actions；`8281c2bb` 的「158 项」也与实测 173 不符（见 §6）。
7. **镜像可复现性**：真实 PG 实验用的是本机 `postgres:16.15-alpine` 与本检出迁移，没有从 `8281c2bb` 重建 api/gateway 镜像。
8. **PR #653 的讨论/检查结论**：未读 PR 评论与 checks，只基于提交内容与自己的实验。

---

## 收尾

- 新增文件仅 `docs/reviews/2026-11-13-reference-acceptance-rework.md`；产品代码、测试与文档零改动。
- `git status --porcelain` 为空（本报告提交前）；`uv.lock` 未改动。
- 隔离资源已删除：compose 项目 `sn667rev`（容器/网络/卷）、`sn667b1`（容器/网络/镜像）、`sn667bf`/`sn667bf2`/`sn667bf3`/`sn667dry` 的网络与残留，以及 `/tmp/sn667probe`、`/tmp/sn667_grants.txt`、辅助 worktree `/private/tmp/sn667at`。
- 分支 `review/667-acceptance-rework` 推送，PR 标题 `review: 参考部署验收脚本返修的非作者审查`；未合并、未 push main。
