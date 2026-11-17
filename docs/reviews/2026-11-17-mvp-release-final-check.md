# 发布说明终核（基线 `767d36e4`）的非作者审查（PR #961，对应 issue #954）

审查对象：PR #961（`docs/mvp-release-final-check`，head `d7c7016068de64ea3b82d5f50274a6bf15940570`，父 `767d36e4`），对应 issue #954。审查工作树 `/tmp/sn961-review`（分支 `review/954-release-notes`，自 `origin/main` 新建）；被审工作树 `/tmp/sn961-subject`（detached HEAD = `d7c70160`）。我不是作者；未改被审文件，未合并任何 PR，被审工作树收尾 `git status --porcelain` 为空。行号口径：发布说明按被审 head `d7c70160` 计；判定表与仓库其余文件按 `origin/main` `767d36e4` 计。

## 结论

**不能合（1 项阻断，改一句话即可转可合；另有 1 处措辞需改、7 条非阻断建议）。**

事实主体成立：核验基线的 SHA 是完整 40 位、日期 2026-11-17；`contract_digest` 离线重算仍为 `sha256:ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7863`；8 个 `pyproject.toml`、5 个 `package.json`、8 个 OpenAPI `info.version` 均未变；迁移链 55 个文件、唯一 head `0056`；§6 第 3 / 5 / 13 条与 `#951` 的两列授权、判定表第 5 行、结构性不符的判定一致；§7 的 9/2/1 计数与 §7.4 的删除/保留项与判定表逐行对得上；门禁 `RESULT PASS`（`terminal problems=0`）、`check-doc-statements` `RESULT OK`、`git diff --check` 干净，改集只有 `docs/releases/mvp-0.1.0.md` 一个文件。

唯一阻断是 **B1**：§7.1 的 `DEL-537` 条目把第 2–4 行（F-JOB / F-ASYNC / F-ARTIFACT）列为本层覆盖，而判定表 §1 明确写「第 2–4 行仍按 `DEL-535`」。这是证据归属冲突，不是事实造假，但按本次审查「§7.1 清单不得与表互相矛盾」的口径必须改。另有 **M1**（§6 第 6 条把读面写成能区分「没有推进者」，与 `production-jobs.md` 的「探针看不到进程是否在跑」冲突）建议合并前改。

## 阻断项

### B1（证据归属）§7.1 把第 2–4 行归给 `DEL-537`，判定表 §1 写的是「第 2–4 行仍按 `DEL-535`」

**位置**：`docs/releases/mvp-0.1.0.md:375-377`（§7.1 第一项 `DEL-537` 的末句）。

**原文**：「因此「空卷 → 发布 binding → Job `succeeded`」是**同一次连续运行**。这一层覆盖 F-JOB（窄口径：创建/读取/派发/终态）、F-ASYNC、F-ARTIFACT（窄口径：发布/下载）、Gate `#572` 与部署入口。」

**对照**：`docs/plans/mvp-release-readiness.md:19` 写：「**部署入口**的 A 档由两层给出：`DEL-537` … 与 `#935`/`#876` …；**第 2–4 行仍按 `DEL-535`**（2026-11-16 同一 MegaCube 窗口的从零栈 Job）。」同表第 2/3/4 行的证据列也都是 `DEL-535` 的 `job-6d2c1d0f…`、`artifact-5b93ea73…` 与 03:29 时间线（`docs/releases/mvp-0.1.0.md:86-88` 引的正是 `docs/operations/compose-minimal-from-zero.md:403/404/411/413`）。

**复现**：

```bash
grep -n "第 2–4 行仍按" docs/plans/mvp-release-readiness.md
grep -n "这一层覆盖" docs/releases/mvp-0.1.0.md
```

**判断**：`DEL-537` 那一轮确实也跑通了这三行要求的动作（同一次 `202 → succeeded`、派发与终态都在场），所以不是事实造假；问题是**证据归属**。`#953` 的非作者审查（`docs/reviews/2026-11-17-mvp-verdict-recalc.md`）已经就判定表 §1 的同类含混提过「部署入口的 A 档由 `DEL-537` + `#935`/`#876` 两层给出；第 2–4 行仍按 `DEL-535`」，表在 `767d36e4` 已按此改写；发布说明这一句又把「这一层覆盖」写成三行都归它。读者在 §2（三行引 §11.2 / `DEL-535`）与 §7.1 之间会读到两个来源，按「§7.1 清单不得与表互相矛盾」的口径必须改。

**建议**：改成「这一层同时可支撑 F-JOB / F-ASYNC / F-ARTIFACT 的同一段链路；判定表仍按 `DEL-535` 记第 2–4 行」，或只保留「Gate `#572` 与部署入口」。

## 逐节核对表

核对方式里的「读」指读被审 head 的源码/文档；「跑」指在 `/tmp/sn961-subject` 实跑。判定表指 `docs/plans/mvp-release-readiness.md`（`767d36e4`）。

| 节 | 核对方式 | 结论 |
| --- | --- | --- |
| 核验基线（`:16-17`） | `git log -1 --format='%H %aI %cI' 767d36e4`；`git rev-parse origin/main`；`date -Iseconds` | **成立（SHA/日期）**：`767d36e4b18c18119db9ada67e3786ab0e4d8c16` 完整 40 位、`origin/main` 同值、核验日期 2026-11-17。括号内时间见 M2：不是该提交时间（`20:47:54`），也不是合并时间（`20:47:55`）。 |
| §1.1 标识清单（`:23-43`） | 跑 `print-contract-digest.py --expect …`；逐个读 8 个 `pyproject.toml`、5 个 `package.json`、8 个 OpenAPI `info.version`；`git diff --stat b63fe004..HEAD -- contracts/` | **成立**：摘要 `sha256:ad581b46…7863`；包版本与契约版本逐项相同；`contracts/` 自 2026-11-16 基线以来零字节变化；`health.py:36`、`version_identity.py:30`、`__init__.py:3`、`version.openapi.json:81`、`generate-contract-bindings.py:823`、`contract-identity.tsx:1` 均仍在指认位置。 |
| §1.2–§1.3（`:44-78`） | 读 `version_identity.py`、`health.py`、`ModelInfo` 契约；跑 `print-contract-digest.py --expect` 一次 | **成立**：三个取值的来源链未变；`--expect` 通过（退出码 0）。 |
| §2 能做什么（`:79-99`） | 逐行对判定表 12 行与证据源；读 `compose-minimal-from-zero.md` §11.2/§14、`#952` 对应测试、`#951` 读面文档 | **成立**：F-ASSET/F-JOB/F-ASYNC/F-ARTIFACT/F-SHARED/F-CLIENT/F-DELETE 的窄口径与判定表一致；`#952` 明确写「不启动 compose 栈」，`DEL-537` 只写「同一操作者换两台主机」；`#951` 只写读面、不写自愈。O-OBSERVE 新增的三类账本标记来源见 M5（非阻断）。F-ASSET 的「20 个非 owner 角色」见 M3（既有措辞）。 |
| §3 部署（`:100-157`） | 读 `compose-minimal-from-zero.md` §13/§13.2/§13.3、`async-job-runbook.md` §7.1、`compose-minimal.md:2240`、`deploy/README.md:1125`；核四步判定顺序 | **成立**：§13 的四步判定与返回码、三条 admin 路由、`workspace_quotas`/`host_nodes` 只由注册写入、`execution_defaults` 的 `422 job_binding_incomplete` 与预检脚本都对得上；`§13.3` 的「0 条只能手写 SQL」支持「不必手写 SQL」。 |
| §4.1 顺序（`:160-197`） | 读迁移链；读四条自查信号的实现与用例；跑 `job-startup-gate.py`（空环境） | **成立**：唯一 head `0056_debt_probe_live_state_index`；四条信号（启动拒绝、CLI 退出码 3、整探针 `unavailable (permission)`、Job 级降级）都有代码与用例；空环境闸门复现为 5 行 `FAIL:` + 汇总、退出码 1、不回显 DSN。 |
| §4.2 迁移回退（`:198-228`） | 数迁移文件；按 `revision`/`down_revision` 求 head；按 `downgrade()` 复算 16/26/13 与 4 个丢数据迁移 | **成立**：55 个文件、编号 0001–0056 缺 0026、单 head `0056`；无条件拒绝 16、条件拒绝 26（含 `0046` 的 SQL `RAISE`）、无条件纯 DDL 13，其中 4 个会丢数据（`0041`/`0043`/`0049`/`0054`）与文中一致。 |
| §4.3 自查信号（`:229-242`） | 跑闸门与 `check-async-job-prereqs.py`；读 `quarantine_release_runtime.py:137`、`completed_release_cli.py:380-383`、`readiness.py` 的 `unavailable_detail`/`_JOB_LEVEL_DEBT` | **成立**：①②③④ 的文案与退出码/降级形态逐条对得上；③ 的「整体 `unavailable (permission)`」由 `_DEBT` 与 `_JOB_LEVEL_DEBT` 都读两列决定；④ 的 `lease-unread` 形态由 `_JOB_LEVEL_DEBT` 与 `_describe_debt` 的 `missing` 分支给出。 |
| §4.4 既有数据（`:243-259`） | 读 `0056` 的 `upgrade()`/`downgrade()` 与 docstring | **成立**：只 `CREATE INDEX`（非 `CONCURRENTLY`）、`ShareLock` 阻塞写入不阻塞读取、无 `IF NOT EXISTS`、失败整链回滚，均与迁移正文一致。 |
| §5.1–§5.4 回滚（`:260-337`） | 读 `0056`；读 `job-startup-gate.py:353`、`quarantine_release_runtime.py:137`、`artifact_retention_runtime.py:158`、`runtime_schema.py:59`；读 `app.py` 的探针装配 | **成立**：`0056` 不新增列/约束，三处硬拒绝仍成立；`#951` 两列不在启动比对里，回退授权只让 `cancellation_debt` 整体降级且 `/readyz` 仍 200；`workbench` 的 `SCHEMA_REVISION = 11` 未变。 |
| §6 已知限制（`:338-358`） | 逐条读源码/契约/文档；重点 2/3/5/6/10/12/13；跑 `grep` 核 `:4063-4070`、`models.py:42`、`public_job_admission.py:695/:514`、`unimplemented.py:82` | **基本成立，1 处措辞需改**：第 2 条（默认 8/上限 32、`exhausted` 不再领取）、第 3 条（两列、9→12 张关系、四条信号）、第 5 条（按最终第 5 行，`#952` 不启动 compose 栈）、第 10 条（三类账本时刻）、第 12 条（24 行表）、第 13 条（结构性不符、占位）均成立。**M1**：第 6 条的「区分『没有推进者』」与 `production-jobs.md:370/374` 的证词冲突，建议改。M4：`:341` 的「处置只有两种取值」与第 2/5/6/7 行的限定值不一致（既有措辞）。M6：第 13 条的 `:514` 指到 `_audio` 函数，实际 `WHERE workspace_id` 在 `:524-525`（既有引用，不影响结论）。 |
| §7.1 A 档（`:364-409`） | 逐条对判定表 §1/§2 与证据源；跑 `grep` 核 `DEL-537`/`DEL-535` 归属 | **需改（B1）**：9 行达标 / 2 行缺当前 200 / 1 行已后置的计数与表一致；`#952`、`DEL-537`、`#935`/`#876`、两条同步入口的边界都写住了；但 `DEL-537` 条目把第 2–4 行列为本层覆盖，与表 §1 冲突（见 B1）。 |
| §7.2 B 档（`:410-416`） | 核 `tests/integration/test_public_job_event_stream_tcp.py`、`test_artifact_retention_pg.py`、`test_workbench_recovery_postgres.py` 存在 | **成立**：三项与表第 2/4/9 行的 B 档证据一致。 |
| §7.3 C 档（`:417-421`） | 读 `.github/workflows/ci.yml` 的 `reference-acceptance.py --dry-run` 与 compose config 渲染；核 `tests/unit`/`tests/contract`/`tests/fake` | **成立**：列出的 C 档入口都在，且门槛写清「不启动栈」。 |
| §7.4 未验证项（`:422-436`） | 逐行对照基线与 head；对判定表 §8 | **成立**：删掉了「零修正照文档」「第 10 行未达标」「同一次从空卷到 succeeded」三项；保留了 9 类必须保留的项（两条同步 200、保留期、取消自愈、物理多机、受限网络、浏览器矩阵、镜像 provenance、工作台 API 单独升降级、第二位操作者/第三台主机），共 10 行。 |
| §8 相关文档（`:437-447`） | 读链接目标存在性 | **成立**：链接均为仓库内文件或已存在页面。 |

## §7 与判定表逐行对照

判定表 12 行的判定按 `docs/plans/mvp-release-readiness.md` 逐格读；「发布说明」列指 §2/§7 的对应句。

| 行 | 判定表（`767d36e4`） | 发布说明对应 | 一致性 |
| --- | --- | --- | --- |
| 1 F-ASSET | 达标（`#867`） | §2 第 1 行（`:85`） | 一致；「20 个非 owner 角色」与该窗口的 18 个不符（M3） |
| 2 F-JOB | 达标（窄口径；取消/重试/SSE 仍隔离 PG） | §2 第 2 行（`:86`）、§7.1 `DEL-535` 条 | 一致；§7.1 `DEL-537` 条另称覆盖该行（B1） |
| 3 F-ASYNC | 达标 | §2 第 3 行（`:87`）、§7.1 | 一致；同上 B1 |
| 4 F-ARTIFACT | 达标（发布/下载） | §2 第 4 行（`:88`）、§7.1 | 一致；同上 B1 |
| 5 F-CLIENT | 达标（同版读取；引擎产出未验） | §2 第 6 行（`:90`）、§6 第 5 条（`:350`） | 一致（A 限同版读取，`#952` 不启动 compose 栈） |
| 6 F-TTS | 有 A 档、参考部署未复现 | §7.1 两条同步入口、§7.4 TTS 行（`:428`） | 一致（2 行缺当前 200 之一） |
| 7 F-ASR | 有 A 档、参考部署未复现 | §7.1、§7.4 ASR 行（`:427`） | 一致（2 行之一） |
| 8 F-SHARED | 达标（默认 workspace 与准入侧引用） | §2 第 5 行（`:89`）、§7.1 `#935`/`#876` 条 | 一致 |
| 9 F-DELETE | 工作台侧达标；语音侧不在首发 | §2 第 8 行（`:92`）、§6 第 9 条（`:354`） | 一致 |
| 10 Gate `#572` | 达标（照文档零修正） | §7.1 `DEL-537` 条、§7.4 删除「零修正未做」 | 一致 |
| 11 Gate `#529` | 未达标、已后置 | §7.1 末段、§2 末段（`:98`） | 一致（1 行已后置） |
| 12 部署入口 | 达标（技术要件；「他人」仅同一操作者换两台主机） | §7.1 `DEL-537` + `#935`/`#876`、§7.4 首行（`:426`） | 一致；`DEL-537` 的覆盖句见 B1 |

计数复核：9 行达标 = 1/2/3/4/5/8/9/10/12；2 行有 A 档但缺当前时点 `200` = 6/7（`#885`/`#957`）；1 行已后置 = 11（`#529`）。与 §7.1 的「9/2/1」逐项相同。

## 门禁与实跑

全部在 `/tmp/sn961-subject`（head `d7c70160`）执行；`UV_NO_CONFIG=1`。

```text
$ uv run python scripts/dev-env/print-contract-digest.py --expect sha256:ad581b46…7863
sha256:ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7863
EXIT=0

$ uv run python scripts/dev-env/check-doc-refs.py
SUMMARY refs=2058 fenced=141 fenced_terminal=4 AMBIGUOUS=130 EXTERNAL=1 MISSING=24 OK=1900 OUT_OF_RANGE=3
SCOPE terminal problems=0 new=0 accepted=0
SCOPE record problems=157 docs/reviews AMBIGUOUS=129 MISSING=24 OUT_OF_RANGE=3; docs/validation AMBIGUOUS=1
RESULT PASS
EXIT=0

$ uv run python scripts/dev-env/check-doc-statements.py
ok   6 public Job routes documented as registered
ok   13 capability statements match the code they cite
RESULT OK
EXIT=0

$ git diff --check 767d36e4..HEAD
（无输出）
EXIT=0

$ git diff --name-only 767d36e4..HEAD
docs/releases/mvp-0.1.0.md
```

**`OUT_OF_RANGE=3` 的归属**：三条都在 `docs/reviews/2026-11-16-job-admission-observability.md`（`:198` / `:600` / `:638` 引 `job_admission_readiness.py` 的 335 / 319 / 308 行，而该文件实际只有 75 行），属 **record 范围**，不改被审文件、本 PR 的 diff 也没碰它们；`SCOPE terminal problems=0 new=0 accepted=0` 说明终态文档零问题、基线清单为空。所以这 3 条是改前既有的过程记录问题，按 record 豁免放行，不是本 PR 引入，也不是通过基线「接受」的。

**其它实跑/抽核**（关键主张逐项）：

| 主张 | 实跑/读法 | 结果 |
| --- | --- | --- |
| 迁移唯一 head `0056`、55 个迁移、缺 `0026` | 解析 55 个版本的 `revision`/`down_revision` | `0056_debt_probe_live_state_index` 单 head；0001–0056 缺 0026 |
| `#951` 给账本加 `last_outcome`/`next_check_at_us` 两列 | `job_cancellation_fence_schema.py:140`；`git show d9d9bbc4^:…` 对比旧授权 | 旧：`GRANT SELECT (job_id, settled_at_us)`；新：多两列；旧授权下两条查询都读不到 → `unavailable (permission)` |
| 隔离释放面 9 → 12 张关系 | 解析 `quarantine_release_schema.py` 的 `FULL_READ`/`READ_COLUMNS`/`FULL_INSERT`/`UPDATE_COLUMNS`，对比 `f1acfec2^` 与 `f1acfec2` | 9 → 12；新增正是 `speech_jobs`、`speech_job_cancellation_fences`、`speech_job_cancellation_reconciliations` |
| 取消重试默认 8、上限 32、`exhausted` 不再领取 | 读 `job_cancellation_runtime.py:116-120`；读 `job_cancellation_reconciliation_postgres.py:49/64` | `default=8`、`le=32`；领取谓词 `last_outcome IS DISTINCT FROM 'exhausted'` |
| §7 的 9/2/1 | 逐格读判定表 12 行 | 达标 1/2/3/4/5/8/9/10/12；6/7 有 A 未达标；11 后置 |
| §7.4 删除项确已覆盖 | 对比 `767d36e4` 与 head 的 §7.4 | 删掉「零修正照文档」「第 10 行未达标」；不再有「同一次从空卷到 succeeded」未验证行；保留 9 类必须保留项 |
| 空环境启动闸门 5 行 `FAIL:`、退出码 1、不回显 DSN | `uv run --package speechnexus-backend python deploy/job-startup-gate.py` | 5 行 `FAIL:` + 末行汇总，退出码 1，无 DSN |
| 发布说明内 53 条 `文件:行号` | 自写脚本逐条核存在性与行数 | 53 条全部存在且未越界 |
| 迁移回退分类 16/26/13 | 按 `downgrade()` 体复算 | 16 个无条件拒绝、26 个条件拒绝（含 `0046` 的 SQL `RAISE`）、13 个纯 DDL（其中 4 个丢数据） |

## 未验证项

- `check-image-contract-digest.py` **未执行**：它要求已构建的 Web 镜像，本工作区没有；与作者 PR 正文的说明一致。
- `DEL-534` 的证据体不在仓库内（作者与判定表都这样写），本轮只按判定表与 `#952` 审查的转录采信。
- `#952` 的引擎产出那一半不可得（本机无 GPU）；其测试文件在仓库内，但本轮没有起 PostgreSQL/MinIO/Chromium 的组合根去复跑。
- `#951` 的 `unavailable (permission)` 形态只做了源码与用例核对（`tests/unit/backend/test_readiness.py:129`、`tests/integration/test_cancel_failure_slot_pg.py:1087`），没有预置角色实跑 PG。
- `DEL-537` 的一手执行报告（Paperclip）不在仓库内；本轮读的是判定表的转录与 `compose-minimal-from-zero.md` §11.2。
- 第 10 条里 `0056` 索引把 `cancellation_debt` 从 `37.4 ms` 降到 `0.90 ms` 这组数字在仓库内没有出处（`docs/reviews/2026-11-17-debt-index.md` 记的是 `0.216 ms`/`3.228 ms` 两条查询与索引构建 `47.5 ms`）；本 PR 未改该句，标为**无法核对**。
- 当前 head 的两条同步入口公开 `200` 仍未取得（`#957`/`#885`），与 §7.4 的陈述一致。
- MegaCube 当前可用性、真实多机/故障演练未探测；本机未起 Compose 栈。

## 非阻断建议

- **M2（基线时间口径）**：`docs/releases/mvp-0.1.0.md:17` 的 `20:48:37` 既不是 `767d36e4` 的 author/committer 时间（`20:47:54`），也不是合并时间（`20:47:55Z`→`20:47:55+08:00`）。若它是复核时刻，建议写成「复核时刻 2026-11-17T20:48:37+08:00」；若按上一版用提交时间的惯例，应改为 `20:47:54`。
- **M3（既有措辞）**：`docs/releases/mvp-0.1.0.md:85` 的「§6 预置的 20 个非 owner 角色」与 `compose-minimal-from-zero.md` §11.2 那次窗口的 §6 不一致——`c0172a22` 时 §6 写「共十八个独立非 owner 角色」，当前 §6 才是 20 个。该行引的是 §11.2 的窗口，建议改成「当时 §6 的 18 个」或注明「当前 §6 的 20 个」。
- **M4（既有措辞）**：`:341` 说「处置」列「只有两种取值」，但第 2/5/6/7 行实际是「可操作（有界）/（部分）/（一半）+ 无处置（一半）/（各自一半）」。四行语义上仍落在两类里，建议把导语改成「两类（可带范围限定）」或把限定并入说明，避免读者按字面数出四种值。
- **M5（证据档位）**：`:93` 的 O-OBSERVE 行新增「`never claimed` / `never claimable (exhausted)` / `due now` 三类账本状态」，但 §14 的参考部署只观测到 `cancellation_debt: unavailable (not configured)`；三类标记的实测来自隔离 PG（`#951` 审查）。建议在该行补一句「三类标记的实测为隔离 PG；参考部署未配 DSN」或把这一细节标为 B。
- **M6（引用精度，既有）**：`:358` 的 `public_job_admission.py:514` 指向 `_audio` 函数定义，真正的 `WHERE workspace_id=%s` 在 `:524-525`；结论（按 workspace 计）不变，引用可改到 `:524`。
- **M7（PR 正文）**：PR 正文写「+112/−50」，实际 `git diff --stat` 是 `+111/−50`；不影响文档。
- **M8（表侧漂移，供 #959 收口）**：判定表第 2 行仍写「漏跑的可自查信号三条」，发布说明 §4.3/第 3 条已按 `#951` 改成四条；表侧建议在 #959 一并更新，不是本 PR 的阻断项。