# 参考部署一条命令验收的非作者审查（#572 / #648）

- **审查对象**：`8809a7b4`（`feat: 参考部署一条命令验收（角色/迁移/桶/启动闸门） (#572) (#648)`），PR #648，已合入 `origin/main`。
- **基线**：`origin/main` head `8809a7b4`。事实取自 `git show origin/main:<path>` 与新 worktree，未读主检出（停在 `a7508635`）的工作树。
- **基线漂移（审查期间）**：受命时 `origin/main` = `8809a7b4`；审查过程中 main 前移两次：`2ba4471a`（#649，按 #642 审查返修启动闸门）与 `f96b33f3`（#647）。**本片四个文件在两次前移后均未再改动**（`git log 8809a7b4..origin/main -- <四路径>` 为空、`git diff --stat` 为空），故逐条结论对本片成立；涉及「继承闸门」与「§6 文案」的观察按时点区分（N12 与第 7 条末尾的 220/221 观察）。
- **审查分支**：`review/658-reference-acceptance`，独立 worktree `/private/tmp/sn658`（`git worktree add /private/tmp/sn658 -b review/658-reference-acceptance origin/main`）。
- **审查者**：非作者。只读代码、测试、文档、既有闸门/授权脚本与 issue 文字；其余结论由本机实测得出。
- **审查日期**：2026-11-13。
- **审查方式**：静态审查 + 16 项现有测试 + 4 组测试级变异（删守卫看是否变红）+ 13 组行为探针 + 隔离 PostgreSQL 16.15 / MinIO 实跑（真迁移、真 provision、真桶、真湿跑 dry-run 前后状态对比）+ 与既有 `deploy/job-startup-gate.py`、`scripts/dev-env/provision-job-roles.py`、`deploy/init-object-storage.py` 的逐项关系核对。
- **产品代码零改动**：只新增本报告。`uv.lock` 未被改动（`git diff --stat uv.lock` 为空）；`uv run` 在 worktree 内建 `.venv`（已 gitignore）。隔离容器/网络/卷/镜像与临时目录已在收尾时删除。

> **时点说明（闸门）**：在 `8809a7b4` 时，`deploy/job-startup-gate.py` 仍是 `8404efad` 的版本（`git log -- deploy/job-startup-gate.py` 只有 `8404efad`），也就是 #642 审查（`docs/reviews/2026-11-13-reference-deploy-gate.md`，同一天）发现 B1/B2/B3 三项阻塞的版本；所以本片「闸门 OK 即可信」的继承面在那个时点带着那三项，详见 N12。审查期间 `2ba4471a`（#649）已按该审查返修闸门（迁移头缺失改为 FAIL、写面补 `speech_artifacts`、`main()` 可测）。本报告的闸门实验全部在 `8809a7b4` 的 worktree 上完成，**没有**重跑返修后的闸门；若以当前 main 为基线复核，需重做 R13 与 gate 步结论。

---

## 结论

**不通过。**

脚本本身的骨架是认真的：退出码、步骤表、失败即停、迁移 head 派生、`--print-statements` 重渲染、dry-run 守卫都实现得干净；作者声称的四条反例我逐条变异复现，**测试确实有牙**（见 [变异矩阵](#r10测试级变异矩阵)）。但审查要回答的是「它在什么情况下会给出错误结论」，而实测存在：

- **两条可复现的假通过（阻塞）**：
  - **B1**：`stack` 步忽略 `docker compose up --build --wait` 的退出码。构建失败时旧容器仍 `running/healthy`，该步判 `PASS`，整轮最终 `PASS`。我在真实 Docker 上复现了 compose 语义（`up` 退出 1，`ps` 仍 healthy），并用假 runner 复现了脚本判 PASS。
  - **B2**：`roles` 幂等分支打印「**重渲染 221 条 GRANT 与已装面一致**」，但实际只核对了 8 条硬编码探针。我在真实 PG 上先只装这 8 条（其余 213 条从未安装）→ `PASS`；又在真实全量安装后撤销 2 条探针不覆盖的授权 → 仍 `PASS`。这条「一致」是没有被测过的断言。
- **一条默认模式从零不可完成（阻塞）**：**B3** `gate` 步在 `stack` 步之前，compose 模式却要求 `gateway` 已 healthy；而 `gateway` 只由后面的 `stack` 步启动。全新环境按文档「一条命令」执行，**必然**在 `gate` 步以 exit 50 失败，提示还指向「准备私有配置 + Silero ONNX」而不是顺序问题。

另有 12 条非阻塞问题，集中在「声明了但没守住」与「文档与实测不符」：`非 owner` 没校验、确认行不带 `pending`、非 `StepFailureError` 逃逸、探针面/桶规则是第二份权威、`--project-name 并行` 声明不成立、旧镜像复用等。

修掉 B1–B3 后可以转**有条件通过**；其余非阻塞项建议同轮清掉，否则下一位维护者会继续把「与已装面一致」「可与其它 Compose 项目并行」当成事实。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | 建独立 worktree `/private/tmp/sn658`（`origin/main` = `8809a7b4`） | 与主检出、既有 `sn633`/`sn645` 容器隔离 |
| T1 | 静态读 `reference-acceptance.py`（1298 行）、`test_reference_acceptance.py`（619 行）、新文档，以及 `job-startup-gate.py`、`provision-job-roles.py`、`init-object-storage.py`、`compose.yaml`、`compose-minimal.md` | 定位 3 条候选假通过、2 条文档声明缺口 |
| T2 | 现有 16 项测试 | `Ran 16 tests ... OK`（`python3 -m unittest tests.ci.test_reference_acceptance`） |
| T3 | 4 组测试级变异（删 env 守卫、删 dry-run 守卫、不停机、硬编码 head） | 每组都让对应测试变红，作者的测试有牙 |
| T4 | 13 组行为探针（只最后一步失败、构建失败旧容器健康、OK+FAIL 解析、非 StepFailure、owner 当 Job 角色、新 `${KEY:?}`、迁移图删/分叉） | 确认 B1、B2 机制与 2 条非阻塞缺陷 |
| T5 | 隔离 compose 项目 `sn658rev`（PG 56532 / MinIO 59900，`172.28.0.0/24`）真湿跑 `--dry-run`，前后快照 | 零状态变化；`DRY-RUN PASS steps=7 pending=1` |
| T6 | 用检出自带迁移与 `provision-job-roles.py` 在隔离 PG 上真跑 `roles` 首跑/重跑 | 首跑 `PASS`（221 statements、8 探针）；重跑 `PASS`（幂等） |
| T7 | 部分角色、撤销探针外授权、compose 模式 gateway 缺失、direct 模式真跑 `gate`、`stack` 基线角色缺失 | 部分角色正确 exit 30；撤销授权与 direct 失败点名均按预期；确认 B2/B3 |
| T8 | 真实 Docker 验证 compose 语义：构建失败旧容器、`run` 复用旧镜像、双项目网络重叠 | 三条声明/机制均确认 |
| T9 | 收尾：删除隔离容器/卷/网络/镜像与临时目录；`git status --porcelain` 为 0 | 只留本报告 |
| T10 | 交付时发现 `origin/main` 已前移（`2ba4471a` #649、`f96b33f3` #647）；核对本片四文件未被后续提交改动 | 逐条结论仍指 `8809a7b4`；闸门侧观察改为按时点表述（N12），未重跑返修后闸门 |

---

## 环境与复现命令

隔离资源（全部用独立项目名与端口，未触碰他人容器）：

```text
compose 项目 sn658rev     PG 127.0.0.1:56532 / MinIO 127.0.0.1:59900,59901
env 文件 /tmp/sn658-probe/review.env（由 deploy/compose.env.example 派生并改端口/桶/私有目录）
私有配置 /tmp/sn658-probe/private（asr/tts lane + transcription recipe 占位）
镜像     postgres:16.15-alpine、speechnexus-minio:RELEASE.2025-09-07T16-13-09Z，
         python 镜像复用本机既有的 speechnexus-minio-init:latest（本项目工作区构建物，只用于真跑辅助步骤）
```

关键命令（可复现）：

```bash
# 1) 现有测试与变异
cd /private/tmp/sn658 && python3 -m unittest tests.ci.test_reference_acceptance -q

# 2) 真湿跑 dry-run（隔离栈已起 postgres/minio，DB 里有 pre-state）
SN_ADMIN_DSN=postgresql://speechnexus:change-me-local-postgres@127.0.0.1:56532/speechnexus \
  python3 scripts/dev-env/reference-acceptance.py --dry-run \
  --project-name sn658rev --env-file /tmp/sn658-probe/review.env

# 3) 真迁移（用检出自己的迁移图）
SPEECHNEXUS_MIGRATION_DATABASE_URL=postgresql://speechnexus:change-me-local-postgres@127.0.0.1:56532/speechnexus \
  uv run --all-packages --locked speechnexus-job-migrate --schema speech

# 4) 只驱动一个步骤（真实 Runner，真实 compose exec psql）
python3 /tmp/sn658-probe/drive_step.py 2        # roles
python3 /tmp/sn658-probe/drive_step.py 4        # gate, --gate-mode compose
python3 /tmp/sn658-probe/drive_step.py 4 direct # gate, --gate-mode direct
python3 /tmp/sn658-probe/drive_step.py 5        # stack
```

---

## 逐条清单结论与证据

### 1. 假通过：该红却绿的所有路径

**逐项核对**（`reference-acceptance.py` 行号取自 `origin/main`）：

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| 退出码被忽略 | **发现 1 处（B1）** | 全文只有 `step_stack` 拿到 `started` 后没有 `if not started.ok`（`:1053-1054`）；其余 `started/applied/initialized/checked/provisioned/rendered/docker/compose/uv/render` 都有 `.ok` 判定（`:759`、`:762`、`:799`、`:826`、`:866`、`:869`、`:875`…） |
| `|| true` / 吞错 | 未发现 | `grep -n '\|\| true'` 无命中；`Runner.run` 用 `check=False` 但显式返回 `returncode`（`:195`） |
| 只取 stdout | 未发现 | `CommandResult.output()` 合并 stdout+stderr（`:170-171`），解析（gate 判定、桶 ready、grants 计数）都走 `output()`；`ok` 只看 `code` |
| 异常被跳过、总结仍 PASS | 未发现跳过 | 只有 `StepFailureError` 被 `run_steps` 捕获（`:1191`）；其它异常直接逃逸，不打印任何 `reference acceptance:` 行（见 [R5](#r5非-stepfailureerror-逃逸无步骤退出码无总结)）——是错误方向相反（丢总结），不是假通过 |
| `pending` 与 `PASS` 边界 | **dry-run 正确；apply 模式的机器行不完整（N5）** | `--dry-run` 只有 `require()` 失败会转 `pending`（`:260-268`），最终 `DRY-RUN PASS ... pending=N`；apply 模式 `gate` 的 `pending` 只出现在步骤结论里，最终 `PASS steps=7` 不带 pending 计数（`:1206`） |
| `--gate-mode direct` 是否让「gateway 根本没起」判通过 | **不会** | direct 模式跳过 gateway 健康前置（`:961-970`），但 `stack` 步仍要求 `api`、`gateway` 都 `running/healthy`（`:1060-1073`）；整体不可能因 gateway 缺失而 PASS。direct 模式真实的缺口在别处：它只跑闸门，不验证 worker 服务真能按 compose 启动（`stack` 只 `up api gateway`，不启动 `job-dispatch-worker`） |
| 新增路径：`gate` 解析 OK 优先于 FAIL | **潜在（N3）** | `gate_verdict` 先判 `"job dispatch startup gate: OK" in output and result.ok` 就返回，`FAIL:` 行被忽略（`:937-946`）；当前闸门不会同时打印 OK 与 FAIL，故只是潜在 |
| 新增路径：`非 owner` 未校验 | **是（N1）** | `step_tools` 只检查三条 DSN 用户互异（`:731-737`），但消息写「三个互不相同的非 owner 角色」；实测把 writer 设成迁移 owner `speechnexus` 仍 `PASS` |
| 新增路径：旧镜像复用 | **是（N11）** | `migrate`/`gate` 用 `compose run`，不 `--build`；实测 `compose run` 复用已存在镜像（[R12](#r12compose-run-复用旧镜像)）；本机镜像内包 head `0052`，检出 head `0053` |

### 2. 失败即停与退出码/总结一致性

- 实现是统一循环 + 立即返回（`:1183-1199`），不存在「失败后继续执行」的分支。
- **只最后一步失败**（我构造：1–6 全绿，仅 `/healthz` 返回 500）：`exit=70`，`reference acceptance: FAIL step=health exit=70`，前 6 步均 `结论: PASS`，**没有**最终 `PASS` 行（[R4](#r4只最后一步失败正向对照)）。这一边界现有测试没有覆盖。
- **退出码与总结一致性**：`StepFailureError` 路径始终一致；**`StepFailureError` 之外的异常不一致**（[R5](#r5非-stepfailureerror-逃逸无步骤退出码无总结)）：没有步骤退出码、没有总结行、进程以 Python 默认 1 退出，直接违背文档「执行模式的结论只有两种，机器可判定」。
- 参数错误返回 2 已在 `main` 实现（`:1267-1272`），与文档一致。

### 3. 不复制第二份权威

**环境键：部分派生，不是全部。**

- `mandatory_env_keys` = `compose.yaml` 的 `${KEY:?}`（14 个）+ 硬编码 `EXTRA_REQUIRED`（6 个，`:77-84`）= **20 个**（实测 dry-run 输出「20 个必需 env 键非空」）。
- 派生本身有效：在 scratch 副本里加 `x-sn658-new: ${SN658_NEW_REQUIRED:?}`，脚本自动要求该键，缺它时 exit 10 并点名（[R8](#r8新-key-自动进入必需集)）。
- 但**6/20（30%）不是 compose 派生的**：`SN_ADMIN_DSN`（compose 里根本没有）、三条 Job DSN（compose 是 `${...:-}` 默认空）、两个桶名（compose 是 `${...:-}` 默认值）；另有 `POSTGRES_USER`/`POSTGRES_DB` 被 `psql_argv` 直接索引（`:441-445`）而 compose 给的是 `:-` 默认。文档「必需键由 Compose 的 `${KEY:?}` 展开决定」（`docs/operations/reference-acceptance.md:26`）因此不准确（N2）。
- 硬编码这 6 个键有代码注释解释（调用脚本直接索引），属于合理取舍；问题在文档把两类混成一句，以及桶名键没进「前置输入」表。

**迁移 head：真派生，实测跟着变。**

- 真实迁移图：53 个 revision，head `0053_voice_draft_publication`；用检出自己的迁移在隔离 PG 上真跑 `speechnexus-job-migrate --schema speech` → `alembic_version=0053_voice_draft_publication`，83 张表。
- 把 head 文件从副本删掉 → head 变为 `0052_quarantine_releases`；再造两个子 revision → 正确拒绝「迁移脚本有 2 个 head」（[R9](#r9迁移-head-派生删加文件都跟着变)）。**没有硬编码**。

**授权面：`provision-job-roles.py` 的输出是同源的，但脚本核对的面是第二份硬编码权威（B2 的根因）。**

- `--print-statements` 真渲染 **221 条**；按角色：writer 60 / admission 51 / dispatch 78 / gateway 32。
- 脚本核对的探针只有 **8 条**（`job_role_probes`，`:502-516`）：writer 2、admission 2、dispatch 4、gateway **0**。
- 8 条确实是 221 条的**子集**（逐条在 `--print-statements` 输出里找到，如 `GRANT SELECT ON "speech".speech_jobs TO "sn_job_writer"`、`GRANT SELECT ON "speech".alembic_version TO "sn_job_dispatch"`），所以不会因此误红；但覆盖是点状的：writer 2/60、admission 2/51、gateway 0/32，dispatch 另由闸门的 20 列写面兜底。
- 幂等分支的 `statements = rendered.stdout.count(";")`（`:831`）**只数不改**：从不与已安装的 ACL 比对。真实 PG 上只装这 8 条（其余 213 条全缺）→ `PASS`；真实全量装完再 `REVOKE` 两条探针外授权 → 仍 `PASS`，并打印「重渲染 221 条 GRANT 与已装面一致」（[R2](#r2只有-8-条授权却报221-条与已装面一致)、[R3](#r3撤销两条真实授权幂等分支仍-pass)）。

**桶：初始化复用 `init-object-storage.py`，但「哪些桶」的规则被复制了两份。** `bucket_names`（`:663-671`）与容器内 `BUCKET_VERIFY_PROGRAM`（`:116-145`）各自复述「inputs + artifact + result-or-artifact」的规则，与 `deploy/init-object-storage.py:25-32` 三处同形；`minio-init` 加第四个桶时，核对步会因数量不符报红（方向是误红，不是假绿），但仍是第二份权威（N6）。

**闸门：只转述、不复制判定逻辑——这一点成立。** `step_gate` 只构造 argv、读 ok/FAIL/pending 行（`:928-983`），不重写任何检查；但它把闸门的既有盲区一并继承（N12）。

### 4. dry-run 真的不写（真实容器独立验证）

隔离项目 `sn658rev` 上先构造 pre-state：18 个 role（含 `sn658_probe_role`、`sn_gateway`、`sn_job_writer`、bootstrap owner）、`speech` 下 2 张表、`alembic_version=0001_init`、2 个已版本化桶、2 个运行中容器。跑真 `--dry-run`（exit 0，`DRY-RUN PASS steps=7 pending=1`）后对比快照：

```text
diff before.txt after.txt
2,3c2,3
< sn658rev-minio-1 Up 57 seconds (healthy)
< sn658rev-postgres-1 Up About a minute (healthy)
---
> sn658rev-minio-1 Up About a minute (healthy)
> sn658rev-postgres-1 Up About a minute (healthy)
```

除容器 uptime 文案外**零差异**：role 数 18→18、`speech` 表数 2→2、`alembic_version` 仍 `0001_init`、桶仍 2 个且版本化状态不变、无新容器/卷/网络。另在**全新项目名** `sn658dryfresh` 上跑 dry-run 后，该项目下容器/卷/网络均为空。守卫 `Context.mutate`（`:247-251`）是代码层保证，上述是独立的运行时证据，两者一致。

### 5. 幂等

- **首跑（真 PG + 真迁移 + 真 provision）**：`PASS` — `sn_job_writer sn_job_admission sn_job_dispatch 已创建；grants: 221 statements；8 个授权探针通过`。
- **二跑**：`PASS` — `三个角色已存在（幂等：未重复 CREATE ROLE，未新增授权）；重渲染 221 条 GRANT 与已装面一致；8 个授权探针通过`；`--print-statements` 被真实调用，输出 221 行。
- **部分存在（真 PG：只建 `sn_gateway` + `sn_job_writer`）**：exit 30，`Job 角色部分存在（已存在: sn_job_writer；缺失: sn_job_admission, sn_job_dispatch）；先核对归属再重跑，脚本拒绝覆盖`。方向正确，不覆盖已有身份。
- **但「不漂移」没有被证明**：见 B2 —— 已有角色的授权面只在 8 个点上被检查，撤销其它授权仍 `PASS`。文档「重复执行不重建、不漂移」（`reference-acceptance.md:14`）的「不漂移」应理解为「脚本自己不漂移」，而不是「检测到漂移」；当前措辞会让运维以为后者。

### 6. 测试强度

现有 16 项测试全绿（`tests/ci/test_reference_acceptance.py`，CI 由 `.github/workflows/ci.yml:35` 的 `python3 -m unittest discover -s tests/ci -v` 收集）。我对作者声明的反例做**删守卫的变异测试**（把被测脚本复制到 `/tmp` 改一行，再跑原测试）：

| 变异 | 结果 |
| --- | --- |
| 删掉 tools 的 `env_gaps` 失败分支 | 2 项测试变红（缺键/空值） |
| 删掉 dry-run 的 `mutate` 守卫 | 1 项变红（dry-run 零写） |
| 把 `return step.exit_code` 改成不停机 | 8 项变红（含失败即停） |
| `migration_heads` 返回硬编码 `0002_next` | 2 项变红（含删文件反例） |

结论：**作者的四条反例描述真实**，不是空话。但以下性质**没有用例守护**（也就是我这次靠人工/真机才发现的）：

1. `step_stack` 忽略 `up` 退出码（B1）——没有任何 stack 用例；
2. `gate_verdict` 同时出现 OK 与 FAIL 时取 OK（N3）；
3. `gate_direct_argv` 整个函数没有用例（direct 模式只被文档描述）；
4. `step_health` 没有用例，包括畸形端口导致的非 `StepFailureError` 逃逸（N4）；
5. `main()` 的参数错误/退出码 2 没有用例；
6. 「只有最后一步失败」的总结/退出码边界没有用例（现有失败用例都是 migrate 或单步 gate）；
7. `roles` 幂等分支打印的「与已装面一致」没有用例核对实际 ACL；
8. dry-run「零写操作」的断言按子串过滤 `up/run/build`（`:377-382`），像 `compose rm`、`exec` 这类写命令不会被该断言拦下——方向偏松，不是错。

### 7. 文档可执行性

**能照抄的部分**：命令、参数名、退出码、dry-run 结论格式、失败文案与代码一致（逐步核对 `docs/operations/reference-acceptance.md` 的步骤表与 `STEPS`，退出码 10/20/30/40/50/60/70 与常量一致；`--compose-file/--env-file` 不存在返回 2 与 `:1267-1272` 一致）。

**逐键比对后发现 5 个问题**：

| # | 文档声明 | 实测 |
| --- | --- | --- |
| D1 | 「必需键由 Compose 的 `${KEY:?}` 展开决定」（`:26`） | 20 个必需键里 6 个不是 `${KEY:?}`（`SN_ADMIN_DSN`、3 条 Job DSN、2 个桶名）；`POSTGRES_USER`/`POSTGRES_DB` 也被脚本直接索引（N2） |
| D2 | 「前置输入」表未列两个桶名键 | `EXTRA_REQUIRED` 要求 `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`、`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` 非空；只按表填写的运维会在 `tools` 步被点名，但文档没告诉他 |
| D3 | 「一条命令」从零执行 | compose 模式下 `gate` 在 `stack` 之前，且要求 `gateway` healthy；全新环境 `gate` 必红 exit 50（B3），而提示指向 §3/私有模型，不是顺序 |
| D4 | 「容器与网络按 `--project-name` 隔离，可与同机其它 Compose 项目并行」（`:6-7`） | `compose.yaml` 固定 `172.28.0.0/24`，我起第二个项目 `sn658par` 直接 `Pool overlaps`；两个本参考部署项目不能并行（N8） |
| D5 | roles 首跑预期输出 `job roles ready: ... grants: N statements`（`:60`） | 入口首跑实际打印 `sn_job_writer ... 已创建；grants: 221 statements；8 个授权探针通过`，`job roles ready` 只在 `provision-job-roles.py` 自己的输出里（N10） |

另有两处只影响可读性/时效的观察：`8809a7b4` 的 `compose-minimal.md` §6 仍写「期望输出 `grants: 220 statements`」，当时真实值是 **221**（此前 #642 审查的 S5 已记录；新文档没有复述该数字，故不算本片缺陷；审查期间 #649 已把它改成 `<N>`）；`--dry-run` 的 `pending=1` 来自 gateway 未 healthy，是预期行为。

### 8. 与 #572 验收要求的一致性

issue #572（OPEN）的验收线索与缺口：

1. 「一份可从零执行的步骤清单（含每步前置检查与预期输出）」——**有**，但「从零一条命令」在默认 compose 模式下不成立（B3）。
2. 「实际跑通一次 asr 或 tts Job」——**没有**，文档明写「不证明真实引擎与已注册 ready 实例、真实 ASR/TTS 业务成功」（`reference-acceptance.md:19`）。方向诚实。
3. 「缺任一前置条件时，在 claim 之前失败并点名」——闸门/前六步覆盖了迁移、角色、桶、网络生命周期的一部分；真实引擎与 API 侧 DSN 以 `pending` 行转述（`:20`）。
4. 「记录环境、镜像/提交、实际通过/失败/未运行」——**不满足**：入口只打印 `repo=/… env=… compose=… project=…`，没有 git commit、镜像 ID/标签或每步耗时/未运行记录；`未执行:` 只在失败时出现。作为 #572 的验收记录入口，这一点需要人工补（N13）。
5. 「不得用 fake 引擎或本地隔离 PG 替代真实部署验收」——脚本本身没有用 fake；测试用 fake 属于单测，不冒充验收。

**`pending` 项与 issue 口径一致**：真引擎/ready 实例（缺口 4）与 API 侧 writer/admission DSN（缺口 1 的 API 面）正是入口无法验证的两项，闸门把它们打成 `pending` 而不是 `PASS`。**没有把「未验证」包装成「通过」**——但 apply 模式的最终 `PASS` 行不带 `pending=N`（N5），机器读取者看不到这两项未验证。

---

## 缺陷清单

### 阻塞

#### B1. `stack` 步忽略 `up` 退出码：构建失败被旧容器的 healthy 掩盖

- **位置**：`scripts/dev-env/reference-acceptance.py:1053-1073`。`started = ctx.mutate(compose("up","-d","--build","--wait","api","gateway"))` 之后只读 `compose ps` 的状态，从不看 `started.code`。
- **真实 Docker 语义（我复现）**：小项目 `sn658bf` 先成功起一个 healthy 容器；把 Dockerfile 改坏后 `docker compose up -d --build --wait` → **exit 1**，而 `docker compose ps -a` 仍返回 `State=running, Health=healthy`（旧容器未被替换）。
- **脚本层实测**：假 runner 让 `up` 返回 1、`ps` 返回两个 healthy，`step_stack` 结论 `PASS — api 与 gateway 均 running/healthy`（[R1](#r1构建失败但旧容器健康时-stack-判-pass)）。
- **后果**：改代码后构建失败、旧栈还健康时，`gate`（旧 gateway healthy）→ `stack` PASS → `health`（旧容器 200）→ 最终 `PASS steps=7`。这正是「把红判成绿」的最坏方向；而且「重跑」正是文档主推的使用方式。成功路径上 `started` 的 stdout/stderr 完全不打印（只有失败分支才拼进错误信息），所以构建报错对运维是不可见的——他只看到 PASS。
- **最小改法**：`if not started.ok: raise StepFailureError(f"栈未起来（compose 退出码 {started.code}）：{started.tail(4)}", ...)`，再判状态；或把 `started.code != 0` 并入 `unhealthy` 条件。补一条用例：`up` 非 0 + `ps` healthy → 必须红。

#### B2. `roles` 幂等分支声称「与已装面一致」，实际只核对 8 条硬编码探针

- **位置**：`scripts/dev-env/reference-acceptance.py:825-841`（`statements = rendered.stdout.count(";")`、`privilege_gaps(ctx, probes)`）、`:502-516`（8 条探针）。
- **实测 1（只装探针）**：真实 PG 上手工只建探针涉及的 5 张表与 `alembic_version`、只给三个角色授 8 个探针权限（迁移未跑，因此 221 条中其余 213 条从未安装）；`--print-statements` 可用 → `PASS — … 重渲染 221 条 GRANT 与已装面一致；8 个授权探针通过`。
- **实测 2（真漂移）**：先用 `provision-job-roles.py` 全量安装 221 条，再 `REVOKE SELECT ON speech.speech_publications FROM sn_job_writer, sn_job_admission`（两条都不在探针里，独立读数 `f|f`）→ 同一幂等分支仍 `PASS`，仍打印「与已装面一致」。
- **后果**：授权漂移（尤其是 admission 角色 51 条里除 INSERT operation_id / UPDATE workspace_quotas.version 之外的 49 条）不会被本入口发现；writer 的漂移可能被后面 API 启动校验兜住（`stack`），admission 的漂移在 #572 的 Job 创建路径之前没有任何一步会碰它。文档「五组授权真实安装到运行时身份上」因此只被点状证明。
- **最小改法**（二选一）：
  1. 把 `--print-statements` 渲染结果解析成 `(grantee, 对象, 权限)` 集合并与 `information_schema`/`aclexplode` 实装面做包含比对（只要求「声明的都在」即可，避免比多授）；
  2. 若不打算做全量比对，就删掉「与已装面一致」的措辞，改成「重渲染 N 条 GRANT（未比对已装面），8 个探针通过」，并在文档「能证明什么」里降级表述。
- **配套**：同步给幂等分支补一条「撤销探针外授权必须红或必须被明示为未检查」的测试。

#### B3. 默认 compose 模式下「一条命令从零」不可完成：`gate` 在 `stack` 之前

- **位置**：`STEPS` 顺序（`:1119-1180`）与 `step_gate` 的 `ctx.require(service_healthy(ctx, "gateway"), ...)`（`:964-968`）；`gateway` 只由 `step_stack` 的 `up api gateway` 启动（`:1053`）。`migrate`/`buckets` 只起 `postgres`/`minio`，不会带起 gateway。
- **实测（隔离项目，gateway 不存在）**：`step 4` → `exit 50`，`FAIL — gateway 未 healthy；job-dispatch-worker 的 network_mode: service:gateway 需要它先运行`，`需要输入: 先让 gateway healthy（私有配置 + 批准的 Silero ONNX）`。此时 `stack` 尚未执行，因此**整轮在启动栈之前就结束**。
- **后果**：全新环境照 `reference-acceptance.md` 的「一条命令」执行，默认模式必红；提示把原因归到「私有配置 + Silero ONNX」，而真实原因是顺序。运维若按提示反复准备模型/配置，仍然红；只有先手工 `up` gateway、或改用 `--gate-mode direct`（再重跑）才能过。这与「唯一入口」的定位冲突，也是「把绿判成红」的典型。
- **边界**：如果前置实际上是「已按 `compose-minimal.md` §7 手工起过栈」，则 `gate` 时 gateway 已 healthy，B3 不成立；但新文档的「前置输入」没有把「栈已启动」列为输入，反而在「能证明什么」里声称本入口让 API/gateway 进入 healthy，所以按文档理解的从零场景就是这个失败路径。修复时需要在「把栈已启动写进前置」和「让入口自己完成顺序」之间二选一。
- **最小改法**：三选一：
  1. `step_gate` compose 模式不再预检 gateway，直接 `compose run`（依赖会拉起 gateway 并等待 healthy；需实测 `network_mode: service:gateway` 下 `run` 的行为）；
  2. 把 `stack` 提到 `gate` 之前（顺序变为 … → 栈 → 闸门 → 健康），并同步文档与失败码表；
  3. 明确把「首次运行必须 `--gate-mode direct`」写成 §一条命令的前置（并说明第二次跑 compose 模式才验证部署路径），同时修正失败提示，不要只说「准备 §3」。
- **配套**：加一条用例/文档断言——「fresh env + 默认参数」不是合法输入，或 `gate` 能自行拉起 gateway。

### 非阻塞

| # | 项 | 依据 | 建议 |
| --- | --- | --- | --- |
| N1 | `非 owner` 只在文案里，未校验 | `step_tools:731-737` 只查三条 DSN 用户名互异；实测 writer DSN 指向 bootstrap owner `speechnexus` 仍 `PASS`（[R7](#r7writer-指向迁移-owner-仍-pass)）。docs:12 却把「非 owner」列为「能证明什么」 | 对三条 DSN 用户查 `pg_roles`（非 superuser / 非 DB owner / 不持 schema CREATE），或删掉「非 owner」表述 |
| N2 | 「必需键由 `${KEY:?}` 派生」不准确 | 20 个必需键里 6 个硬编码（`EXTRA_REQUIRED:77-84`），另有 `POSTGRES_USER/DB` 被 `psql_argv` 直接索引；docs:26 | 文档分两栏写：「compose 派生 14 个」+「入口额外要求 6 个（列出）」；桶名键补进前置输入表 |
| N3 | `gate_verdict` 中 OK 优先于 FAIL | `:937-946`：先判 OK 后判 FAIL；实测「FAIL 行 + OK 行」→ 返回 `([], [])` 当通过（[R6](#r6gate-同时打印-ok-与-fail-被当通过)） | 先判 FAIL，或要求 `FAIL:` 行数为 0 才接受 OK；补用例 |
| N4 | 非 `StepFailureError` 逃逸，无总结无步骤码 | `run_steps:1189-1199` 只捕获 `StepFailureError`；`http_json:674-684` 不捕 `http.client.InvalidURL`；实测 `SPEECHNEXUS_API_HOST_PORT=8o00` → traceback、exit 1、无 `reference acceptance:` 行（[R5](#r5非-stepfailureerror-逃逸无步骤退出码无总结)），违背 docs:49-52「结论只有两种」 | `run_steps` 兜 `except Exception` 转成 `FAIL step=<id> exit=<code>` + 输入提示；`http_json` 捕 `(ValueError, HTTPException)` |
| N5 | apply 模式 PASS 行不带 `pending` | `:1202-1207` 只有 dry-run 拼 `pending=`；闸门的 2 个 `pending`（真引擎、API 侧 DSN）不进最终机器行 | 最终行改为 `PASS steps=7 pending=2`（dry-run 与 apply 一致），或文档明确「PASS 不代表 pending 项已验证」 |
| N6 | 桶规则与探针面是第二份权威 | `bucket_names:663-671`、`BUCKET_VERIFY_PROGRAM:116-145` 与 `deploy/init-object-storage.py:25-32` 三处同形；`job_role_probes:502-516` 与 221 条授权声明不同源 | 桶名从 init 脚本的规则里提取成共享函数；探针从 grant 函数派生（至少 dispatch 的写面复用闸门的 `DISPATCH_WRITE_SURFACE`） |
| N7 | schema 未传给 provision | `provision_argv()` 不传 `--schema`（`:519-528`），`SPEECHNEXUS_DATABASE_SCHEMA` 只影响探针/psql；非默认 schema 时授权落在 `speech`、探针查 env schema → 误红/误导。compose `migrate` 也写死 `--schema speech` | `provision_argv()` 追加 `--schema`, `ctx.schema()`；或文档明确「本入口只支持 `speech`」并在 `step_tools` 校验 |
| N8 | `--project-name` 并行声明不成立 | `compose.yaml:574-576` 固定 `172.28.0.0/24`；实测第二项目 `sn658par` 建网失败 `Pool overlaps`（[R11](#r11第二个同名栈项目网络重叠)） | 文档去掉「可与同机其它 Compose 项目并行」，或让 compose 网络不固定 subnet |
| N9 | `SN_ADMIN_DSN 是迁移 owner` 前置未校验 | `tools` 只查非空；首跑靠 provision 自行失败，幂等分支根本不使用 | 首跑前用 `SN_ADMIN_DSN` 做一次 `SELECT current_user`/owner 校验；或把该前置从文档/步骤表降级 |
| N10 | roles 首跑预期输出与实际不符 | 文档步骤表写 `job roles ready: ... grants: N statements`；入口打印 `已创建；grants: N`（provision 的原始行被解析后丢弃） | 文档改成入口实际输出，或入口透传 provision 的行 |
| N11 | `migrate`/`gate` 复用旧镜像，不 `--build` | 实测 `compose run` 复用已存在镜像（[R12](#r12compose-run-复用旧镜像)）；本机镜像内包 head `0052`、检出 `0053`，此时 `migrate` 会先按旧镜像迁移、再被入口的 head 检查判「迁移落后」→ 修法是「再跑 migrate」，循环 | 在 `migrate`/`gate` 前 `compose build` 对应服务，或在 `tools` 步记录/校验镜像与检出的迁移 head 一致 |
| N12 | 闸门侧既有阻塞（仅在 `8809a7b4` 时点成立） | 该提交时 `deploy/job-startup-gate.py` 仍是 `8404efad` 版本（`git log` 只有该提交），#642 审查的 B1（缺 head SELECT 时 `pending` 放行落后库）、B2（写面缺 `speech_artifacts` INSERT）、B3（`main()` 装配/退出码无守护）在该版本成立；本入口的「闸门 OK」因此不是独立证据，`speech_artifacts` 写面缺失不会被本入口任何一步发现。**审查期间 `2ba4471a`（#649）已返修这三项**；在返修后的闸门上，本入口 gate 步会继承更严格的 FAIL 语义（例如缺 `alembic_version` SELECT 将直接红），本轮未重跑 | 在文档「能证明什么」里注明闸门版本/时点；补一条对返修后闸门的 gate 步实跑（含 OK 与 FAIL 两条路径） |
| N13 | 不记录环境/提交/镜像，未满足 #572 的验收记录要求 | 入口头只打印 repo/env/compose/project；无 git commit、镜像 tag/ID、每步耗时 | 头行加 `git rev-parse HEAD`（只读）与 `docker compose images --format json` 摘要；失败行已有「未执行」，可在 PASS 行附 `pending` |

---

## 我独立复现的反例（原始记录）

以下均为本机实测；脚本本身未改动，变异只发生在 `/tmp` 的副本。

### R1：构建失败但旧容器健康时 `stack` 判 PASS

假 runner：`up api gateway` 返回 1，`compose ps` 返回 `api/gateway running/healthy`；其余按已部署状态应答。只跑 `STEPS[5]`：

```text
结论: PASS — api 与 gateway 均 running/healthy
```

真实 compose 语义对照（`sn658bf`）：坏 Dockerfile 下 `up -d --build --wait` → `exit=1`，`ps` → `{"State":"running","Health":"healthy"}`。

### R2：只有 8 条授权却报「221 条与已装面一致」

真实 PG（未跑完整迁移，手工建探针用到的 5 张表 + `alembic_version`）+ 真实 `--print-statements`（221 条）；只授 8 个探针权限后跑 `roles`：

```text
结论: PASS — 三个角色已存在（幂等：未重复 CREATE ROLE，未新增授权）；重渲染 221 条 GRANT 与已装面一致；8 个授权探针通过
RUN_EXIT=0
```

### R3：撤销两条真实授权，幂等分支仍 PASS

全量安装 221 条后：

```sql
REVOKE SELECT ON speech.speech_publications FROM sn_job_writer;
REVOKE SELECT ON speech.speech_publications FROM sn_job_admission;
-- 独立读数：has_table_privilege(...) = f | f
```

再跑 `roles`：

```text
结论: PASS — … 重渲染 221 条 GRANT 与已装面一致；8 个授权探针通过
```

### R4：只最后一步失败（正向对照）

假 runner：1–6 全绿，仅 `http_json` 返回 500：

```text
exit=70
summary: ['reference acceptance: FAIL step=health exit=70']
per-step PASS: 6 | FAIL: ['结论: FAIL — http://127.0.0.1:8000/healthz 返回 500：{"status":"degraded"}']
final PASS printed: False
```

### R5：非 StepFailureError 逃逸，无步骤退出码、无总结

假 runner 抛 `OSError("disk gone")`：`run_steps` 直接抛 `OSError`。真实触发：

```text
http://127.0.0.1:8o00/healthz -> escaped http.client.InvalidURL nonnumeric port: '8o00'
```

即 `SPEECHNEXUS_API_HOST_PORT` 写错一个字符时，最后一步以 traceback + exit 1 结束，没有 `reference acceptance:` 行。

### R6：gate 同时打印 OK 与 FAIL 被当通过

```python
gate_verdict(FAIL: bucket x does not exist\njob dispatch startup gate: OK) -> ([], [])
```

当前闸门不会同时打印两者，故只是潜在；但解析顺序（OK 先判）决定了未来闸门输出形态变化时会假通过。

### R7：writer 指向迁移 owner 仍 PASS

`SPEECHNEXUS_JOB_WRITER_DATABASE_URL=postgresql://speechnexus:…`（bootstrap owner），三条用户互异 → `step_tools`：

```text
结论: PASS — … 6 个必需 env 键非空；compose 渲染通过
```

### R8：新 key 自动进入必需集

在 scratch `compose.yaml` 加入 `x-sn658-new: ${SN658_NEW_REQUIRED:?set it}`：

```text
derived key present: True | total keys: 21
with key supplied    -> code= 0
without new key      -> code= 10 | 结论: FAIL — 环境键缺失或为空: SN658_NEW_REQUIRED
```

### R9：迁移 head 派生，删/加文件都跟着变

真实迁移图：

```text
real head= 0053_voice_draft_publication | revisions: 53
head after deleting the head file: 0052_quarantine_releases | moved: True
branched graph -> 迁移脚本有 2 个 head（sn658_branch_a, sn658_branch_b）；单库只允许一个 head，先合并分支
```

与真实迁移交叉验证：`speechnexus-job-migrate --schema speech` 在隔离 PG 上跑完后 `alembic_version=0053_voice_draft_publication`，83 张表。

### R10：测试级变异矩阵

（方法：复制被测脚本到 `/tmp/sn658-probe/mutants/<变异名>`，改一行，跑原测试文件。）

```text
baseline:                     exit=0 failures=0
M1 remove env-gap check:      exit=1 failures=2
M2 remove dry-run guard:      exit=1 failures=1
M3 continue after failure:    exit=1 failures=8
M4 hardcoded head:            exit=1 failures=2
```

### R11：第二个同名栈项目网络重叠

```text
docker compose -p sn658par … create postgres
Network sn658par_default Error: invalid pool request: Pool overlaps with other one on this address space
```

### R12：`compose run` 复用旧镜像

坏 Dockerfile + 已存在镜像时：

```text
docker compose -p sn658bf -f compose.yaml run --rm -T web true   → run exit=0（未重建，旧镜像）
```

本机 python 镜像内包 head 为 `0052_quarantine_releases`，而检出 head 为 `0053_voice_draft_publication`，即 N11 的真实版本偏差样本。

### R13：真实 `gate`/`stack` 前置失败路径（隔离项目）

```text
# compose 模式，gateway 不存在
结论: FAIL — gateway 未 healthy；job-dispatch-worker 的 network_mode: service:gateway 需要它先运行
reference acceptance: FAIL step=gate exit=50

# direct 模式，真容器真连接（fixture 角色口令不匹配）
结论: FAIL — 启动闸门判定失败: SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL cannot connect to PostgreSQL:
      … password authentication failed for user "sn_job_dispatch" …
reference acceptance: FAIL step=gate exit=50

# stack，基线角色缺失
结论: FAIL — 基线运行角色缺失: sn_api, sn_gateway_query
reference acceptance: FAIL step=stack exit=60
```

R13 同时证明 direct 模式的 env 传递是对的：`docker compose run -e KEY`（无值）取 service 解析后的值，我另用最小项目验证了 `LITERAL_KEY=literal-value`、`FROM_INTERP=interp-default`、shell 覆盖 `SHARED=shell-shared` 三种情况。所以 direct 模式不是「-e 拿不到值」的坏实现。

---

## 只能依赖作者陈述/未验证的点

1. **完整参考部署的 `stack`/`health` PASS 路径**（`docker compose up -d --build --wait api gateway` 从零构建到 healthy、四个探针 200）：需要审批过的私有 recipe、Silero ONNX 与 API/gateway 镜像构建；我没有在隔离栈里完成（本机既有镜像的包内 head 还是 `0052`，与检出 `0053` 不同，用它跑完整栈会得到版本偏差结论，不能当验收证据）。
2. **compose 模式下闸门打印 `OK` 的真实路径**：真实闸门需要迁移、dispatch 全量授权、两个桶、gateway 配置文件 `result_bucket` 一致；我在 direct 模式只得到了「点名 FAIL」的正确路径，没有构造出完整 OK。
3. **真实引擎/已注册 ready 实例与一次 asr/tts Job 的端到端**（#572 的最终验收）：作者与我都没有跑；脚本把这两项列为 `pending` 是诚实的。
4. **API 启动时对 writer 授权的自检**（`Public Job writer startup rejected`）：只读代码与 §6 文字，没有启动 API 实证；因此 B2 中 writer 漂移「可能被 stack 兜住」这一点是推断，不是实测。
5. **CI 结果**：本地 16 项全绿；没有观察 GitHub Actions 实际结果。
6. **镜像/提交的可复现性**：我使用了本机既有 `speechnexus-minio-init:latest`（来源工作区）作为 python 运行镜像；没有从 `8809a7b4` 重新构建镜像，因此「该提交构建出的镜像内 head 是 0053」只能靠检出推断，未实测。
7. **`voice_validation` 车道**：`SPEECHNEXUS_JOB_DISPATCH_KINDS` 含该值时 provision 多两条 SELECT、闸门探针也扩展；我没有用该配置实跑。
8. **PR #648 的讨论/CI 结论**：未读取 PR 评论与检查结果；本报告只基于提交内容与我自己的实验。

---

## 收尾

- 新增文件仅 `docs/reviews/2026-11-13-reference-acceptance.md`；产品代码、测试与文档零改动。
- `git status --porcelain` 为空；`git diff --stat uv.lock` 为空；`.venv/` 与 `__pycache__` 均为 gitignore。
- 隔离资源已删除：compose 项目 `sn658rev`（容器/卷/网络）、`sn658bf`、`sn658direct`、`sn658par` 残留卷，镜像标签 `sn658rev-*`、`sn658bf-web`，临时目录 `/tmp/sn658-probe`（报告内保留命令与输出原文）。
- 分支 `review/658-reference-acceptance` 已推送，PR 标题 `review: 参考部署验收脚本的非作者审查`；未合并、未 push main。
