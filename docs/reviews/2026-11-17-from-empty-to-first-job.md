# 从空库到第一个 `202` 步骤清单的非作者审查（PR #935）

审查对象：PR #935 `docs/from-empty-to-first-job`，head `496274b2`，基线 `1721ddb2`
（`origin/main`；`git merge-base` 与声明的基线逐字相同，历史根 `54f7778e` 与本审查无关）。
本报告只审文档与验证脚本，不改被审分支。

**结论：能合。** 本次审查在本机真实 Compose 栈上从空卷独立复跑到 `202`，作者的核心事实成立；
无阻塞缺陷。必须修 2 条（README 索引行、runbook §7 过期行号，均为单行改动），建议 3 条。

## 1. 我做了什么（复跑，不是读作者记录）

被审分支与写集先按 fork 点核对：`git diff 1721ddb2 496274b2 --stat` 命中三个文件
（`compose-minimal-from-zero.md` +279、`async-job-runbook.md` +2、新增
`check-from-empty-first-job.sh` 664 行），无产品代码，与自述一致。

复跑用作者 worktree `/private/tmp/sn935`（其 `deploy/compose.env` 已按 §1–§3 固定项目名
`sn935`、子网、宿主端口 `8137/8138/55443/59010/59011`），从**已不存在的卷**重建：

| 步骤 | 结果 |
| --- | --- |
| `docker compose -p sn935 up -d postgres minio` | 新建 `sn935_postgres-data`、`sn935_minio-data`（此前项目无任何卷，`docker volume ls` 为空） |
| `minio-init` | `bucket ready: speechnexus-inputs (versioning enabled)` / `…-results…` |
| `migrate` | `speech.alembic_version` = `0056_debt_probe_live_state_index` |
| **空库计数** | `publications=0 binding_revisions=0 quotas=0 host_nodes=0 instances=0 workspaces=1 catalogue_state=1`——与 §14.3「起始事实」逐项相同 |
| §6 建角色 | `roles ready:` 19 个身份，与 §14.2 同一行文本 |
| `provision-job-roles.py` | `job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: 223 statements`——与 §14.2 的 223 相同 |
| `api`/`gateway`/`asset-inspector` | 四个探针 `200`；`api /readyz` 四项（含 `cancellation_debt`/`stop_evidence_debt`），`asset-inspector` 回执 `recipe_version: pcm-decode-1` |

镜像未重建，直接复用作者在同一 SHA 构建的 7 个：`sn935-api f6fb167e6021`、
`sn935-gateway 96ae65bf1de3`、`sn935-migrate d421c1336e4f`、`sn935-engine-register 8fb142486a31`、
`sn935-job-dispatch-worker 51c4034a9365`、`sn935-minio-init ffbe17302a9e`、
`sn935-asset-inspector 38e3cfaf5d66`——与 §14.1 表内的短 ID **逐个相同**，记录的这次实跑是真实发生过的。
测后按 `down -v` 清栈，容器/卷/网络均无残留；作者的
`deploy/.runtime/private/**` 与 `.runtime/from-empty-state.json` 已按复跑前备份还原。

## 2. 「0 条手写 SQL」核实

**成立（写入口），但表述要带上「写入」二字。**

- 三处事实的产生入口全部经产品面，复跑实测：binding 走三条 admin 路由（`201`/`201`/`200`），
  资产走 `POST /v1/nexus/assets`（`201`、`status: ready`），配额/容量/实例走
  `docker compose --profile engine-registration run --rm engine-register`
  （`outcome: registered`，脚本回读 `workspace-default|8|policy-revision-compose-example`、
  `node-local-1|active`、`instance-asr-1|1|ready|mbrev-302a…`）。
- 反查 SQL：脚本内 `sql_scalar`（`:226`）共 4 处调用（`:542`/`:543`/`:544`/`:576`），
  全部是 `select`——只读核对，落在允许范围内；无 `INSERT`/`UPDATE`/`DELETE`，
  无 `psql` 直连宿主或非 `exec -T postgres` 的写路径。`§13.1` 第 13 步与 `§13.3` 表格已把
  「只读 SQL」单独列出，作者自己的表述是准确的。
- 全仓库 `INSERT INTO workspace_quotas` / `INSERT INTO host_nodes` 的非测试命中只有
  `engine_registration.py:359` / `:479`；另有 `scripts/dev-env/del109-local-gateway-fixture.py`，
  该文件自述「local test fixture, not the production node-controller registration path」，
  可归入「其余命中都是测试夹具」。结论无实质偏差。

## 3. 从空卷是否真的从空卷（本卡最重要的一条）

**作者的这次实跑是真的从空卷起**，但**「常驻验证脚本」本身证明不了这一点**，两者要分开说。

真实性的旁证（不是作者文字，是我独立取的）：

- 项目名 `sn935` 此前从未存在，复跑前 `docker volume ls` 无任何 `sn935_*` 卷；按 Compose 的
  项目级命名，该项目的卷只可能由这一轮 `up` 创建，而 §14.7 记的 `down -v` 也与「现在卷不存在」一致。
- 空库计数（§14.3 那段文本）在复跑时**逐项复现**，包括容易忽略的 `workspaces=1 catalogue_state=1`。
- 7 个镜像的短 ID 与 §14.1 表内一致；`revision_id`/`asset_id`/`job_id` 都是运行时生成的
  `mbrev-`/`asset-`/`job-` 实体，且 `.runtime/from-empty-state.json` 与
  `deploy/.runtime/private/transcription-recipe.json` 内的 `binding_revision_id`
  互相自洽（记录里的两次发布 = 先 §14.3 ② 的 revision 1、后 D14 复现的 revision 2）。

但脚本层面有两处**未设门**，构成「绿了 ≠ 从空库到 202」的缺口：

1. 脚本不 `down -v`、不检查起点。头部写的是「The topology itself … comes from
   compose-minimal.md §0–§7; this script refuses to run against a stack whose probes are not 200」，
   `phase_probe` 只验四个探针。没有任何 `speech_publications = 0` / `held_slots = 0` 的前置断言。
2. `phase_create` 是**双分支**：`held_slots = 0` 才断言 `202`，否则断言 `429 job_capacity_exhausted`。
   我实测把它跑成一个「全程没有 202 也 PASS」的绿：同一栈上第一条 Job 仍占着唯一槽位时，
   `--phase create` 输出 `PASS capacity branch holds … RESULT PASS`，退出码 `0`。
   也就是说脚本可以被用来「验证」一个恰好相反的结论，而 §13.4 的措辞是「每步自带断言」。

`§14` 的标题与正文没有声称脚本能证明起点为空（§14.3 的计数是手工记录的文本块），
所以这不构成虚假声明；但把该脚本当作「从空库到 202」的可复跑证据会过头。建议补一个
`--require-empty`（启动时断言 `publications=0 AND quota/host_nodes/instances=0`，或在
`--phase create` 上要求必须走 `202` 分支）；否则至少要在 `§13.4` 写明「起点为空由操作者保证，
脚本不校验」。

## 4. 判定顺序（binding → 资产 → 配额 → 实例）

**源码与实测都支持，作者的纠正正确。**

- 应用层：`create()` 先解析 binding（`latest_binding` 为空 → `raise self._unpublished_binding(model_id)`，
  `backend/src/speechnexus_backend/speech/application/public_jobs.py:835`；helper 在 `:912`），随后 `:863` 调 `_require_inputs`（资产就绪判定，
  `:1490` 的 `AssetNotReadyError`），再是 `:865` 的 catalogue revision 判定。
- 准入 SQL：`build_public_job_admission` 内 `_binding`（`:675`）→ `_quota`（`:695`，无行 →
  `:201 workspace_not_configured`）→ `_instance`（`:696`，无匹配实例 → `:253 job_capacity_exhausted`，
  且 `:253` 的 detail 区分「从未注册」与「注册了不 ready」）。
- 实测：「已发布 binding + `quotas=0` + 不存在的资产」→ `422 asset_not_ready`（不是
  `workspace_not_configured`），直接证明资产判定在配额之前；空库上未发布 binding → `422 model_not_published`。
- §13 表内 `:201`/`:253` 与源码当前行号一致。`backend/src/speechnexus_backend/speech/application/public_jobs.py:912` 指向的是生成该 422 message 的
  helper（`raise` 在 `:835`/`:867`），可接受略偏精确，不算错。

## 5. 反例重做

两反例都是脚本里的**真判据**，我各重做一遍：

| 反例 | 我的做法 | 结果 |
| --- | --- | --- |
| 未发布 binding（`--phase counterexample-unpublished`） | 空库上直接跑 | `PASS … error code model_not_published`，`RESULT PASS` |
| 同一 phase 的**区分力** | 先用 `--phase publish --binding-id model-unpublished-example` 把该 binding 发布，再重跑反例 | `FAIL create Job without the publication step: expected error code model_not_published, got asset_not_ready`，退出码 `1`——产品事实一变就红，不是事后记录 |
| `policy_revision` 冲突（`--phase counterexample-policy`） | 空库 + 已注册配额后跑 | `PASS … policy_revision mismatch is exit 4 and names quota_policy_conflict`，正文 `error: quota_policy_conflict: existing workspace quota policy revision does not match the recipe`；阶段结束配方自动恢复 |
| 删脚本 → 文档引用门禁 | 在 `496274b2` 的独立 worktree 删掉脚本后跑 `check-doc-refs.py --baseline …` | 该门禁的原文是：

```text
[MISSING] docs/operations/compose-minimal-from-zero.md:598  ->  scripts/dev-env/check-from-empty-first-job.sh:642  [新增,基线外]
SCOPE terminal problems=1 new=1 accepted=0
RESULT FAIL
```

未删时同一命令是 `new=0`、`RESULT PASS`。 |

一处过头：`§13.4` 结尾把「删掉断言就必须有检查变红」这件事交给文档引用门禁承担，但该门禁只看
**文件存在性与行号是否越界**，不看内容。我保留文件、只删掉 `run_phase` 里的
`counterexample-policy` 分支，`bas…` 基线门禁仍 `RESULT PASS`（`bash -n` 也通过，该 phase 只是变成
「unknown phase」）。所以「删文件会红」成立，「删断言会红」不成立。

## 6. D12–D15 的表述

四条我都现场复现了，措辞没有夸大：

- **D12（catalogue readiness 静态）**：`PostgresModelCatalog` 生产装配处
  （`model_catalog_runtime.py:92`）**不传** `readiness`，`model_catalog_postgres.py:35` 落到
  `ReadinessFacts()` 默认值；全仓库没有第二个 `ReadinessFacts(...)` 生产者。实测：实例 `ready`、
  Job 已 `202` 之后 `GET /v1/models` 仍是
  `{"configured": true, "engine_ready": false, "admission_state": "unavailable"}`。结论准确，
  「runbook §7.1 第 4 步的措辞会误导」也成立（`async-job-runbook.md:480`：
  「未注册实例时是 `unavailable`」，实际永远如此）。
- **D13（`/readyz` 四项 vs `compose-minimal.md:2218` 两项）**：`app.py:_build_readiness` 恒挂四个
  probe；实测 `/readyz` 四行 `ok`。行号 `:2218` 正是那段两项 JSON。文档漂移成立。
- **D14（重发布使实例身份失效）**：实测发布 revision 2 后创建 → `429`、
  message `no engine instance is registered for model 'model-compose-example': …`；
  直接重跑注册 → 退出码 `4`、`error: instance_config_conflict: the ready instance has different
  binding, model plan or capacity facts`。与 `§13.2` 表与 D14 行逐字相符，且作者已自标「轮换路径未验证」。
- **D15（`max_slots=1` 时一个 queued Job 占满容量）**：实测第二次创建 → `429 job_capacity_exhausted`、
  message `Job capacity is exhausted; retry after the indicated delay.`。
  **判定「不是缺陷，是容量语义」成立**：`max_slots=1` 来自仓库自带的
  `deploy/private/transcription-recipe.example.json:32`（部署选型，不是代码硬编码）；
  准入在事务内预留槽位，`coordination_schema.py:145` 有
  `CHECK (held_slots <= max_slots - quarantined_slots)`，槽位随租约释放归还，
  与 AGENTS.md「原子准入」一致。唯一的补充是：本轮没起 `job-dispatch-worker`，
  queued Job 在派发前一直占着这唯一槽位，所以「第二创建即 429」在这个栈上是必然而非偶发——
  建议 §14.5 D15 行补一句「占位持续到租约释放/准入截止」，读者才不会误判成泄漏。

## 7. 文档追加与既有内容是否冲突

- **与 `compose-minimal.md` 的 429 说法不冲突**：该文件只有一处 429 表述
  （`:2251`「虽然 `ready`，准入仍以 `422 job_binding_incomplete` / `429 job_capacity_exhausted` 拒绝」），
  与 §13 判定表相容。
- **与 `async-job-runbook.md` §7.1 无重复到啰嗦的程度**：§13.2（1）只给入口、身份缺口的 503、
  非幂等的两个 409 与 13 字段的指向，细节仍留在 §7.1，并且新增的交叉引用行位置合适。
  `_ASR_REQUIRED` 实测 13 项，`422 job_binding_incomplete`（`:138` 的 `_execution_block`）、
  `409 binding_not_draft` / `configuration_changed`、`503 binding_unavailable` /
  `binding_result_unknown` 均在源码中确认存在。
- **但行号自相矛盾（必须修）**：同一个「无配额行 → `422`」「无实例 → `429`」事实，新 §13 写
  `public_job_admission.py:201` / `:253`（正确），而同一 PR 动过的
  `async-job-runbook.md:352` 仍写 `:196` / `:248` / `backend/src/speechnexus_backend/speech/application/public_jobs.py:822`——我这三处都已过期
  （`:196` 是 join SQL 中间行、`:248` 是 detail 文本、`:822` 是 pinned binding 分支）。
  同一文件内 §12 的 D4 行（`compose-minimal-from-zero.md:301`）仍写 `:219`。三组数字并存，
  正是「照做的人 grep 不到依据」的形态，且 `check-doc-refs.py` 只查越界、不会替人发现这种漂移。
- 一处小的内部不一致：`§13.1` 第 2 步写「`docker images` 里有 `<项目>-api` 等 **6 个**」，
  而 `§14.1` 记录本轮从源码构建 **7 个**（多 `job-dispatch-worker`）。

## 8. 泄露面

脚本输出面干净：只有 `127.0.0.1` 探针 URL、`workspace-default|8|policy-revision-…`、
`node-local-1|active`、`instance-asr-1|1|ready|mbrev-…`、`asset-…`/`job-…`，以及失败时的
响应体与注册容器**最后 3–5 行**；无 DSN、无口令、无容器内网地址、无绝对路径
（唯一的路径输出是错误分支里的 `compose env file not readable: deploy/compose.env`）。

文档侧有一处轻微越界：`§14.1` 写入了宿主绝对路径
`/Users/hrygo/Library/Application Support/SpeechRail/models/vad/silero_vad.onnx`。
它属于「实测记录保留原时点事实」的写法，且仓库另有 13 个文档含同类路径（终态文档占 1 个），
不算新增一类暴露；但 AGENTS.md 对「内部模型路径」的要求更严，建议改成
「本机 SpeechRail 模型目录下的 `silero_vad.onnx`，2327524 字节，sha256 前 8 位 …」并保留摘要
（摘要才是判据）。同段的 `172.31.0.0/24` 是本地 RFC1918 网段，示例文件本来就是 `172.28.0.0/24`，
无需处理。

## 9. `docs/operations/README.md:20`

原文：「[从空库按文档起参考部署的执行记录](compose-minimal-from-zero.md)：2026-11-16 实测到引擎注册前，
含五类缺前置表现与缺口清单。」该行索引的正是本 PR 修改的文件，而 §14 已经把结论推进到 `202`，
`2026-11-16` 也不再是该文件最新的一次实跑（§14 是 `2026-11-17`）。**这行已经过期，且过期原因就是本 PR。**

「不在写集」这个理由我认为不成立：写集纪律的目的是让审查 PR 只承载报告、让改动边界可审，
而不是让「被自己改动证伪的索引行」留在原处。一行改动的爆炸半径为零，留着反而让索引与正文打架。

## 10. 必须修与建议

**必须修（合并前，均为单行级）**

1. `docs/operations/README.md:20`：改成「2026-11-17 实测到 `POST /v1/nexus/jobs` 返回 `202`」这类与 §14 一致的描述。
2. `async-job-runbook.md:352` 的三个行号改成 `backend/src/speechnexus_backend/speech/application/public_jobs.py:835`/`:912`（或 `:867`）、
   `public_job_admission.py:201`、`:253`，与同 PR 的 §13 表统一。

**建议**

3. `§13.4` 写明「脚本不校验起点为空」或加 `--require-empty`；`--phase create` 建议要求必须走 `202`
   （现在是双分支，可以在没有 202 的情况下 `RESULT PASS`）。
4. `§13.1` 第 2 步的「6 个」与 `§14.1` 的「7 个」取齐。
5. `§14.1` 的宿主绝对路径改为相对描述 + 摘要；`§14.5` D15 行补「占位持续到租约释放/准入截止」。
（可顺带：`compose-minimal-from-zero.md:301` D4 行的 `:219` 也一并改成 `:253`，同一 PR 内自洽。）

## 11. 我未验证的

- 真实引擎上的 Job 执行（claim → submit → 制品）：本轮与作者一样只到 `202`，注册针对
  `/health` 替身；`§14.6` 的自述与我的观察一致。
- `artifact_set` 的制品完整性：未运行 `verify-file-asr-artifacts.py`。
- 发布新 revision 后的 generation/实例轮换路径：我复现了 D14 的失败面（`429` + `instance_config_conflict`），
  未验证存在的恢复路径——与作者标注一致。
- 7 个镜像是否确由 `496274b2` 源码构建：我只核对了镜像短 ID 与 §14.1 记录一致（`docker images --format '{{.ID}}'`），
  未逐个校验构建标签与源码摘要。
