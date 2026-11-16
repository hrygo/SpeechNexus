# 外部托管引擎资格路径（运维自证）的非作者审查（PR #878）

审查对象：`feat/operator-attested-qualification`，head `6f118956`（`bc16cf93` 实现 + `6f118956` 私有表清单），
对比基 `origin/main...HEAD`，21 文件 +4202/−33。审查在独立 worktree `/private/tmp/sn883` 完成，
隔离 PostgreSQL `sn876-oa-pg:55470`、MySQL `sn876-oa-mysql:55471`。

> 本报告按轮记：**第一轮** `6f118956`（下至「第二轮」标记之前，保持该时点原样）、**第二轮** `82d55a82`、**第三轮** `82d55a82`（同 head 的复核）、**第四轮** `f77b348b`、**第五轮** `9e492e11`（文末「第五轮：终核」，**PASS**）。一至四轮的结论均为不能合，阻塞理由逐轮收窄：第一轮是「标签可伪造 + 读面没有身份」，第二轮是「标签诚实但准入不读」，第四轮只剩「无 provenance 行的 grandfather 形状可由发布身份伪造」；第五轮删掉该分支后四条全过，只剩两处发布说明/日志文案精度（P3）。

**结论：不能合。** 这条路径的关闭状态、写入边界与迁移都做得很干净，运行态快照也确实是零变化；
但「两种来源在读面上分得开」这个本 PR 的唯一卖点有两条硬缺陷：

1. 新增的最小身份 `sn_operator_attestation` 能用自己**已经拿到的**列级 `INSERT` 权限直接写一条
   probe 形状的 approval，发布后 provenance 记为 `source_kind='controller_observed'`、
   `provenance_recorded=true`，读面与 TTS 准入都当它是真实控制器观测（§4.5）；
2. 承诺用来分辨来源的 `speechnexus-qualification-provenance` **没有任何已预置身份能跑**
   ——`qualification_reader_grants` 与 `builtin_publisher_grants` 都没有拿到两张新表的 `SELECT`，
   两条命令都 `permission denied`（§4.4）。

其余是装配、命名与测试覆盖层面的问题（§10 逐条）。

---

## 0. 复核记录

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 新增单测 | `uv run --all-packages pytest tests/unit/backend/test_operator_attested_qualification.py -q` | 22 passed |
| 新增隔离 PG 集成 | `… pytest tests/integration/test_operator_attested_qualification_pg.py -q` | 19 passed |
| 被改的协调表清单 | `… pytest tests/integration/test_coordination_foundation.py -q` | 16 passed |
| CI 门禁套件 | `… pytest tests/ci -q` | 459 passed, 1645 subtests |
| 文档引用门禁 | `python3 scripts/dev-env/check-doc-refs.py --baseline scripts/dev-env/doc-refs-baseline.json` | `RESULT PASS`，`terminal problems=0 new=0` |
| 文档陈述门禁 | `python3 scripts/dev-env/check-doc-statements.py` | `RESULT OK` |
| `require_native_ready` 未改 | `git diff origin/main...HEAD -- backend/src/speechnexus_backend/speech/domain/qualification_native_observation.py \| wc -l` | `0` |
| `_recorded_source` 无测试覆盖 | 见 §9 M2 | 删掉后 41 项全绿 |

`UV_NO_CONFIG=1`（本机 uv 镜像配置会误报 lockfile 过期，与仓库无关）。

---

## 1. 默认关闭：九种「看起来配好了」的状态，逐条实测

命令一律是全参数形态（缺参数会被 argparse 先挡掉，测不到配置门）：

```
speechnexus-attest-builtin-voice --schema speech --workspace-id wsp-x \
  --attestation-request-file … --artifact-pin … --manifest … --report … --scenario-evidence …
```

| # | 构造的状态 | 实际输出（stderr 末行） | exit | 负责的判断 / 删掉会怎样 |
| --- | --- | --- | --- | --- |
| A | 完全未配置（`env -i`，无任何 `SPEECHNEXUS_*`） | `error: SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL is not set` | 2 | `attest_builtin_voice.py:165` 的显式点名检查。删掉后由 pydantic `min_length=1` 兜住，仍是 2，但**不再点名变量**，只剩 `invalid attestation configuration or input` |
| B | 只有 `SPEECHNEXUS_OPERATOR_ATTESTATION_ROLE_PASSWORD=change-me-…`（口令在文件里、DSN 未导出） | 同上 | 2 | 同上：口令与 DSN 是两条互不相干的输入，口令存在不代表启用 |
| C | `…_WRITER_DATABASE_URL=`（导出空串） | 同上 | 2 | `.strip()` 判空。删掉后 `conninfo_to_dict("")` **返回 `{}`**（实测），会被当合法连接串，pool 用 libpq 默认值（socket + OS 用户 + 同名库）连本机 |
| D | 导出纯空白 `'   '` | 同上 | 2 | 同上；`min_length=1` 挡不住空白，`conninfo_to_dict('   ')` 也返回 `{}`（实测），所以 `.strip()` 是唯一的门 |
| E | 导出 `\t\n ` | 同上 | 2 | 同上 |
| F | 语法合法但指向不可达目标：compose 别名 `…@postgres:5432/…`、`…@host:notaport/db` | `error: trust_declaration_missing` | 4 | 不是 DSN 门挡的，是**声明门**：`prepare_attestation()` 在 `pool.open()` 之前跑完，注入一个不存在的声明文件就得到 4。删掉声明门就会走到连接，解析失败落到「outcome unknown」exit 5 |
| G | 完全不可解析：`not-a-dsn-at-all` | `psycopg.ProgrammingError: missing "=" after "not-a-dsn-at-all" in connection info string` + 完整 traceback | **1** | **没有判断负责**：`conninfo_to_dict()` 的 `psycopg.ProgrammingError` 不在 `attest_builtin_voice.py:198` 的捕获元组里，也不是 `OperatorAttestationError`，直接穿透 |
| H | 键值串缺值：`host=127.0.0.1 password=X dbname` | `psycopg.ProgrammingError: missing "=" after "dbname"` + traceback | **1** | 同 G |

结论：**A–F 没有任何一条能点亮能力，默认关闭成立**；C/D/E 也证明了 `attest_builtin_voice.py:165`
的门是承重的，不是冗余。G/H 是 §10 的 P3：fail-closed（零写入）但违反本设计 §6「参数或配置错误返回 2」
的契约，且 exit 1 无文档、抛完整 traceback。好消息是错误信息只回显**出错的那一段**
（`missing "=" after "X"`），实测带口令的串没有把口令打进 traceback。

「指向另一个 schema（同名表在别处）」按写入侧核对：`record_operator_attestation` 在事务内读
`current_database()/current_schema()/installation_id` 并与声明逐字段比对
（`operator_attested_qualification.py:193`），`--schema` 由 pool 的 `search_path` 决定，
所以「声明 A schema、连 B schema」被 `trust_declaration_target_mismatch` 拒绝。若删掉 `schema`
这一行比较（只比 library+installation_id），把 `speech_publication_installation` 连同
`installation_id` 复制到另一个 schema 即可绕过——这条比较是承重的。**该条按代码核对，未实跑双 schema**
（集成夹具在单 schema 内自洽，造第二套需再跑一次完整 alembic 链，收益不抵成本）。

---

## 2. 声明能否被伪造或顶替

### 2.1 用别人的 attestation 给自己挂资格 —— 挡得住，但靠的是唯一约束不是代码

ADMISSION 侧 `require_builtin_voice` 把 facts 钉死在当前实例身份上
（`backend/src/speechnexus_backend/speech/infrastructure/builtin_voice_qualification.py:52-54`：
`instance_id / generation / host_node_id / gateway_id /
gateway_policy_revision_id` 全部参与 `WHERE`），`generation` 又由
`backend/src/speechnexus_backend/speech/domain/admission.py:485`
（`runtime.head.current_generation != stage.instance_generation` → `stale_instance_generation`）
提前对齐到运行态 head。实测（同一份已发布资格，逐项换参数）：

| 传入值与 facts 不同 | 结果 |
| --- | --- |
| `instance_id` | 拒绝 `policy_or_resource_disabled` |
| `instance_generation` | 拒绝 |
| `host_node_id` | 拒绝 |
| `gateway_id` | 拒绝 |
| `policy_revision_id` | 拒绝 |
| head 提升到 2 后按当前 head 准入 | 拒绝 |
| **`fencing_token` 由 7 换成 12（同 generation）** | **通过** |

所以「拿 A 实例的资格去给 B 实例挂」不成立。但要指出：**真正的拦阻者是
`UNIQUE (voice_id, revision, revision_id, binding_revision_id)`
（`0055_operator_attestation.py:58`）**——它让同一 voice revision 只能有一条 attestation，
所以不存在「第二条 attestation 顶掉第一条」。这个约束同时造成 §2.5 的续期死路，
**修续期时若直接放宽/删除这个唯一约束，顶替能力立刻打开**；两条必须一起设计。

`fencing_token` 只在**写时**绑定：`require_registration`（`operator_attestation.py:568`）比对
`engine_instances.last_fencing_token` 与 `resource_pools.fencing_token`（当前 owner，且未过期），
两者都来自账本而不是请求自带的副本——这一点作者说的是对的。但读时不再复查（上表最后一行），
即池 owner 换成新 token 后旧资格继续可用。这与 probe 路径等价（probe facts 根本没有该字段），
属可接受的设计取舍，但读面把 `fencing_token` 当"当前运行态"打印（§4.1）会让人误读，见 P3。

### 2.2 generation 绑定的是账本还是请求副本

两处都核对过：写时 `require_registration` 用 `runtime_instance_heads.current_generation` 与
`engine_instances.instance_generation` 双查；`lifecycle_document` 里的
`instance_generation/fencing_token/image_digest/template_id` 与请求字段相等，而这两者对攻击者是
**同一个自写文档**——所以那条等式对自洽的伪造者恒真，独立证据只有注册账本那一路。
`require_lifecycle_identity` 的价值在于「同一份原件不能声明两套身份」，不是独立事实。

### 2.3 `source_approval` 复用与过期续用

- 写入：`s.expires_at > clock_timestamp()`、`native_speaker_id='Vivian'`、`voice_id` 相等、未撤销
  （`operator_attested_qualification.py:203-214`），且窗口 `expires_at` 不得晚于来源批准
  （`require_live` 的 `attestation_window_too_long`）。
- 读取：`require_builtin_voice` 再查一次 `s.expires_at > clock_timestamp()`（`:55`）。
  实测：把来源批准改成已过期后，**attestation 窗口内的资格立刻被拒**——过期无法续用，
  而且比 attestation 自己的窗口更早失效。
- 同一个来源批准可被同一 `voice_id` 的多次声明复用，这是设计意图（来源批准约束「声音来源」，
  不是「每次资格」），且被唯一约束压成实际只有一条。

### 2.4 声明记录能不能被发布身份修改或删除：`writer_identity_forbidden` 守的是哪一层

**守的是写入者自己那一层，不是发布者。** `_FORBIDDEN_IDENTITY`
（`operator_attested_qualification.py:64`）在**声明事务内**对 `SESSION_USER` 求值，看它是不是
owner/superuser、有没有库/模式 CREATE、有没有对 13 张账本表的 `UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER`、
以及是否通过角色成员继承了这些（第二个 `EXISTS`）。实测：

| 用哪个身份跑声明 | 结果 |
| --- | --- |
| 只拿 `operator_attestation_writer_grants` | `committed` |
| 发布者凭据（有 `speech_publication_heads` 的 `UPDATE/DELETE`） | `writer_identity_forbidden` |
| 最小身份 + 被 `GRANT` 一个能 `UPDATE approvals` 的角色（成员继承） | `writer_identity_forbidden` |
| 最小身份尝试 `SET ROLE` 到别的角色 | 数据库直接拒绝（该角色没被 grant） |

发布者能改/删声明记录吗？不能，靠授权而不是这个检查：`builtin_publisher_grants`
只给 `SELECT ON speech_builtin_voice_operator_attestations` + `INSERT ON provenance`，没有
`UPDATE/DELETE`。声明身份同理（`SELECT` 而已，实测矩阵见 §7）。所以「声明只能追加」成立，
但要说清：`writer_identity_forbidden` 防的是**误用宽身份去写声明**，不是防发布者篡改。

### 2.5 续期死路（P2）

声明窗口到期后，设计 §10 与读面提示都写「re-attest and republish」。实测这**做不到**：

```
首次: committed att-tts-1
同 id 重放: replayed expires_at_us=1794799229000000   ← 只返回旧窗口，不延长
账本行: {'attestation_id': 'att-tts-1', 'expires_at_us': 1794799229000000}
续期（新 attestation_id/approval_id，同 voice revision）: PublicationUnavailableError
attestations 行数 = 1
```

原因是 `UNIQUE (voice_id, revision, revision_id, binding_revision_id)`；异常又被
`PostgresPublications._run` 统一折成 `PublicationUnavailableError`，在
`attest_builtin_voice.py:229` 落到泛化分支 → **exit 5 + “attestation outcome unknown; query or
replay the identical attestation with the same originals”**，即把一个确定性的约束拒绝报成
「结果未知、请重放」，而重放永远不会成功。对一个 30 天上限的信任声明来说，这是一条会周期性
撞上的运维死路。

---

## 3. 下游消费者：有没有地方不区分来源就当成控制器观测用了

先把两张凭证表的**全部**读取点列全（`backend/src` 全量 grep，排除 migrations）：

| # | 消费者 | 读什么 | 区分来源？ | 会不会把 operator 事实当运行态语义 |
| --- | --- | --- | --- | --- |
| 1 | `speech/infrastructure/builtin_voice_qualification.py:17` `require_builtin_voice` | qualifications ⋈ approvals ⋈ source_approvals 的 facts + 时效 | 否（只做身份/时效/能力比对） | **否**。只用 `facts` 做准入比对与能力上界；不写任何表 |
| 2 | `speech/infrastructure/qualified_speech_facts_postgres.py:85/176` | 同上，取 `gateway_policy_digest`、`native_speaker_id` | 否 | 否，只服务同步 TTS 准入与启动闸门 |
| 3 | `speech/infrastructure/public_job_admission.py:302` | `SELECT 1 FROM speech_builtin_voice_qualifications …` | **否，且不看时效** | 见下 §3.1 |
| 4 | `speech/infrastructure/voice_catalogue_schema.py:15` / `voice_publication_schema.py:130,149` | `q.document`（public_document）投影给公开目录视图 | 否 | 否，只投影已发布文档 |
| 5 | `qualification_probe_approval.py:180,400` | 按 `approval_id` 读 approvals 的 facts/probe 列 | 是（比 `probe_authorization_id`、`probe_evidence_digest`） | 否：operator approval 的 `probe_authorization_id` 为 NULL → `probe_approval_conflict`，只读不写 |
| 6 | `builtin_voice_publication.py:301` `_recorded_source` | attestations ⋈ approvals | 是（逐字段比对） | 否 |
| 7 | `qualification_provenance.py:46` | 四张表只读 | 是（provenance + attestation 明细） | 否 |
| 8 | `builtin_voice_revocation.py` | approvals/source_approvals 撤销账本 | 否 | 否，只追加撤销行 |

**风险面结论（按作者要求逐条核对）**：

- **算力释放 / `workspace_quotas` 归还 / `host_nodes` 预算**：写入者是
  `admission_sql.py:314`、`leases_sql.py:189`、`lease_release_sql.py:216`、
  `engine_registration.py:353/477`；全部只读自己的租约/准入表，没有任何一处读
  `speech_builtin_voice_*`。**声明路径不写这些表**，而且**授权上写不进去**（§7 实测：该身份对
  `workspace_quotas`/`resource_pools` 只有 `SELECT`，对 `host_nodes`、`admission_leases`、
  `execution_stop_evidence`、`runtime_instance_intents` **连 `SELECT` 都没有**）。
- **`execution_cleanup_pending` 结算**：`lease_release` 认 `cleanup_state='complete'` + 三种 stop 事实
  （`engine_terminal_response` / `instance_terminated_by_controller` /
  `operator_verified_instance_terminated`），全部来自 `submit_intents`/`execution_stop_evidence`；
  声明路径对这两张表零权限。作者的 domain 用例
  （`test_operator_delete_cannot_discharge_execution_cleanup_pending`）与我的授权实测一致。
- **实例生命周期收敛（`instance_state`、`generation`）**：`runtime_instance_heads` 与
  `engine_instances` 对该身份只有 `SELECT`；`runtime_instance_intents` /
  `runtime_intent_deliveries` 零权限。
- 因此「快照只能证明这次没写」这个质疑成立，但**更强的不变量是授权**：即使声明身份被完全控制，
  它也没有任何一条写运行态的权限。这一条比快照可靠，建议写进设计文档（现在文档只用快照论证）。

### 3.1 唯一一处「只看存在性、不看来源也不看时效」的地方（P3）

`public_job_admission.py:302`：当一个 `vrev-*` 的公开 voice 在 `speech_voice_revisions` 里没有行时，
它用「存在一条 builtin 资格」来证明这不是丢失的克隆，然后按 builtin 声音继续放行。这条 SQL
**没有 `expires_at_us` 条件、也不区分 `source_kind`**。资格行在过期后永久留在表里，于是：
运维自证的资格过期后，同步 TTS 会 503（`require_builtin_voice` 查时效），而 public job 路径仍把这
个 voice 视为合格 builtin。这条形状是 0055 之前就有的（probe 资格过期同样满足它），但本 PR 让它
多了一类生产者，并且 §2.5 的续期死路让过期行变成**永久**状态。**按代码阅读，未实跑 public job 路径。**

---

## 4. 读面可分性：实际读到的字段

### 4.1 CLI 实际输出（同一份账本里两种来源 + 一条 legacy）

```
{"approval_expires_at_us": 1794799470000000, "binding_revision_id": "binding-rev-1",
 "evidence": "provenance_row", "instance_generation": "1", "instance_id": "instance-tts-1",
 "provenance_recorded": true, "revision": "1", "revision_id": "voice-rev-probe",
 "source_kind": "controller_observed", "voice_id": "voice-probe"}

{"approval_expires_at_us": 1794799410000000, "attestation_expires_at_us": 1794799410000000,
 "attestation_id": "att-tts-1", "attestation_observed_at_us": 1794795810000000,
 "attestation_state": "live", "binding_revision_id": "binding-rev-1",
 "controller_artifact_digest": "sha256:c7bd…", "declaration_digest": "sha256:326a…",
 "declared_by": "ops-oncall", "evidence": "provenance_row", "fencing_token": "7",
 "image_digest": "sha256:61cc…", "instance_generation": "1", "instance_id": "instance-tts-1",
 "note": "operator-attested external engine: not a controller observation",
 "provenance_recorded": true, "revision": "1", "revision_id": "voice-rev-1",
 "source_kind": "operator_attested", "template_digest": "sha256:ba44…", "voice_id": "voice-vivian"}
```

legacy 行（0054 之前发布、无 provenance）：`source_kind=controller_observed`,
`provenance_recorded=false`, `evidence=legacy_pre_0055`, `instance_id=null`。

**在 CLI 读面上分得开，而且写得很清楚**（`note` 直说 "not a controller observation"）。
但两个字段会误导人：

- `controller_artifact_digest`：operator 行上它是**运维自写的 lifecycle 文档**的规范化摘要
  （`canonical_digest(lifecycle_document)`，见 `operator_attestation.py:419`），沿用
  `require_native_ready` 的字段名纯属类比。字段名说"controller artifact"，内容里没有 controller。
- `fencing_token` / `instance_generation`：打印的是**声明当时的**副本，不是当前的运行态。
  ops 很容易读成"现在是 token 7 / gen 1"。
- `provenance_recorded=false` 一律回落到 `controller_observed`（`qualification_provenance.py:67`），
  对 0055 之前是安全推断（当时不存在声明路径），但见 §4.5。

### 4.2 API 响应：完全分不开

同一账本里两条资格通过 `require_builtin_voice` 都放行，`public_document` 键集合逐一相同：

```
operator : ['capabilities','disabled_reason','display_name','model_binding','object',
            'published_at','resource_type','retired_at','revision','source','status',
            'validation','voice_id']
probe    : 同上（键集合相同 = True）
operator validation = {'status':'passed','validation_id':'report-1','checked_at':…}
probe    validation = 同上
```

`source` 字段两边都是 `{"type":"builtin","draft_id":null,…}`——它是"内建 vs 克隆"，
不是"观测 vs 自证"。公开目录视图（`voice_catalogue_schema.py:15`、`voice_publication_schema.py:149`）
只投影 `q.document`，所以经过 HTTP 读的人**没有任何字段能分辨来源**。

这是设计取舍（不再为来源分叉下游读取路径），但它与用户要求「在文档与**读面**上区分开」只对上一半：
**文档 + 运维 CLI 分得开，HTTP 读面分不开**。建议在设计文档 §4 明确写出"HTTP 面刻意来源中立，
审计必须用 `speechnexus-qualification-provenance`"，否则下一个人会以为 API 里有。

### 4.3 数据库：分得开（facts 变体互斥）

| | operator 独有键 | probe 独有键 |
| --- | --- | --- |
| `speech_builtin_voice_approvals.facts` | `attestation`, `declaration_digest` | `probe_evidence_digest` |

`verifier_id` 也分别固定为 `operator-attested-customvoice-v1` / `builtin-customvoice-v1`。
`BuiltinVoiceFacts` 用两个**互斥键集**（`backend/src/speechnexus_backend/speech/domain/builtin_voice_qualification.py:74-75` 与 `:153-154`）分派，
一行 facts 就能判来源，不需要 join。这一层作者做对了。

### 4.4 读面没有可用身份（P1）

`qualification_provenance.py` 的 SQL 需要四张表的 `SELECT`，但两个已预置的读者身份都拿不到
新表。实测（alembic 全量迁移后的真 schema，分别用两个授权函数建身份后执行同一条 `REPORT_SQL`）：

| 身份 | qualifications | approvals | provenance | attestations | 执行 REPORT_SQL |
| --- | --- | --- | --- | --- | --- |
| `qualification_reader_grants`（api/合成读身份） | SELECT ✓ | SELECT ✓ | **✗** | **✗** | `permission denied for table speech_builtin_voice_qualification_provenance` |
| `builtin_publisher_grants`（发布身份） | SELECT ✓ | SELECT ✓ | **✗** | SELECT ✓ | 同上 `permission denied` |

CLI 把 `InsufficientPrivilege` 归入 `except Exception` → `error: qualification_provenance_unavailable`，exit 3。
更糟的是照 runbook 操作时**连接都建立不起来**：`_settings`（`qualification_provenance.py:135`）的回落顺序是
`--database-url` → `SPEECHNEXUS_QUALIFICATION_READER_DATABASE_URL` → `SPEECHNEXUS_DATABASE_URL`，
而这两个裸键在 `deploy/compose.env.example` 里都不存在（前者全仓只被
`qualification_provenance.py:141` 读取，从未被任何 provision 步骤赋值），所以 runbook 里那句
`uv run speechnexus-qualification-provenance --schema speech` 在按 §6 载入 `compose.env` 的 shell 里
必然以同一个 `qualification_provenance_unavailable`/exit 3 结束——先撞的是「没有 DSN」，
补上 DSN 之后才撞「没有 SELECT」。

集成用例为什么没抓到：`_report_rows()` 用的是**夹具的 schema owner 连接**直接跑 `REPORT_SQL`，
从未用预置身份跑过读面。修复很小：把两张新表加进 `qualification_reader_grants`
（或单开一个 provenance reader 授权），runbook §6 明写用哪个 DSN，并让用例用那个身份跑一次。

### 4.5 「controller_observed」是缺席推断，不是证据（P1）

`_recorded_source`（`builtin_voice_publication.py:301`）判定 controller 来源的方式是
「**没有** attestation 行 + facts 的 `verifier_id` 不是 operator」。而 facts 是任何持有
`INSERT … (…, facts, …)` 列权限的身份都能写的自由 JSON。于是：**新身份 `sn_operator_attestation`
自己就能造一条 `controller_observed`**。实测（用该角色自己的凭据，不是 owner）：

```
伪造 approval 由身份执行: sn_oa_r_0ffd3d9b7d09_oa
写入 approval: 无 attestation 行, probe_authorization_id = None
provenance 记录: {'source_kind': 'controller_observed', 'attestation_id': None, 'recorded_by': …}
读面输出: [{… "evidence": "provenance_row", "provenance_recorded": true,
            "source_kind": "controller_observed", …}]
准入读取通过, verifier_id = builtin-customvoice-v1
```

即：一条从没有任何 controller 参与的资格，在库里与读面上都是
「0055 之后由 controller 发布」，并且能被同步 TTS 准入接受。

可用的判别信息就在同一行里、只是没用：probe 路径写 approval 时会写
`probe_authorization_id`（`qualification_probe_approval.py:411`），声明身份**没有**该列的
INSERT 权限（§7），所以真实控制器观测必然 `probe_authorization_id IS NOT NULL`，上述伪造必然为 NULL。
作者自己的集成用例把「`probe_authorization_id` 为 NULL 的 probe 形状 approval」当作合法的
`controller_observed` 断言下来（`test_controller_probe_publication_still_records_its_own_source`），
等于把这个缺口固化成期望行为。

修法（低成本）：provenance 写 controller 时要求 `probe_authorization_id IS NOT NULL`
（或改成记录 `source_evidence='probe_authorization'|'absent'`），并对
`facts.verifier_id='builtin-customvoice-v1'` 且 `probe_authorization_id IS NULL` 的 approval 直接拒绝发布。

---

## 5. `require_native_ready` 一字未改，新路径是并列而非可选

- `git diff origin/main...HEAD -- …/speech/domain/qualification_native_observation.py` → 0 行。
- `BuiltinVoiceFacts` 的 probe 变体校验没有放宽：`_PROBE_CHECKS` / `_PROBE_VERIFIER` 与原来内联的
  集合逐项相同，只是搬成常量；`_FIELDS` / `_DIGEST_FIELDS` 原样用于 probe 分支。
- 并列性：声明路径不写 `native_runtime_evidence`、不签发 probe authorization/admission
  （`qualification_admission_postgres.py` 的三处读取只碰 `probe_authorizations`/`probe_admissions`），
  `require_native_ready` 与 `issue_probe_authorization` 的入参里根本没有 facts。所以「没有自动降级」成立。
- 但作者用来证明这一点的单测 `test_operator_attestation_does_not_unlock_the_native_probe_path`
  第二段是装饰性的：它先 `BuiltinVoiceFacts(_facts())` 造一份 operator facts，再拿**同一份**
  manual-registration observation 调 `require_native_ready`——facts 不是该函数的入参，第二次断言
  与第一次等价。真正的隔离是结构性的（不同表、不同函数），不是这条用例证明的（P3）。

---

## 6. 迁移 `0055_operator_attestation`

- **只加表，不改既有表/行**：实测把 schema 升到 `0054_artifact_deleted_ledger`，写入一条 0054 形状的
  approval+qualification，再升到 `head`，对 `approvals`/`qualifications`/`source_approvals`
  三张表做全行哈希：**三张表都未变**；`provenance` 0 行、`attestations` 0 行 → 无回填。
- **downgrade 真能回**：无 attestation 行时 `DROP` 两张新表成功，0054 三表哈希仍等于升级前；
  再 `upgrade head` 成功（`alembic_version = 0055_operator_attestation`）。有 attestation 行时
  `RuntimeError` 拒绝（作者的用例覆盖，我复跑通过）。
- **结构**（实测 `pg_constraint`/`pg_indexes`）：PK `attestation_id`；`approval_id UNIQUE` + FK；
  `UNIQUE (voice_id, revision, revision_id, binding_revision_id)`（同时是 §2.1 的顶替拦阻者和
  §2.5 的续期死路）；`UNIQUE (attestation_id, voice_id, revision, revision_id, binding_revision_id)`
  **冗余**（`attestation_id` 已是 PK，只是多一个索引）；六个 digest 列（`gateway_policy_digest`、`image_digest`、`template_digest`、`controller_artifact_digest`、`model_artifact_digest`、`declaration_digest`）的 `^sha256:[0-9a-f]{64}$` CHECK、
  `expires_at_us > observed_at_us`、`fencing_token > 0` 都在。provenance 侧 PK `(voice_id, revision)`、
  FK 到 qualifications 与 attestations、`CHECK ((source_kind='operator_attested') =
  (attestation_id IS NOT NULL))` 都在。
- 小问题：`declared_by NAME`（`0055:54`）只有 63 字节，而 `_identifier` 允许 127 字符；
  实测 text(100) → name 长度 63，即超长 `declared_by` 会被静默截断，读面显示的 `declared_by`
  与 facts 里的原值不一致（P3，改用 `TEXT` 或把 identifier 上限降到 63 字节）。
- `runbook` 的迁移 head 已同步为 `0055_operator_attestation`，CI 守卫
  `test_the_runbook_states_the_current_migration_head` 通过。

---

## 7. 授权最小化：实际落盘的权限

用 `has_table_privilege` / `has_column_privilege` 对真 schema 逐项实测：

| 表 | SELECT | INSERT | UPDATE | DELETE | TRUNCATE |
| --- | --- | --- | --- | --- | --- |
| `speech_builtin_voice_operator_attestations` | ✓ | ✓（**表级**） | ✗ | ✗ | ✗ |
| `speech_builtin_voice_approvals` | ✓ | ✓（**仅 8 列**） | ✗ | ✗ | ✗ |
| `speech_builtin_voice_source_approvals` / `_source_revocations` / `_revocations` | ✓ | ✗ | ✗ | ✗ | ✗ |
| `speech_publication_installation`、`speech_binding_revisions`、`speech_binding_publication_evidence`、`speech_publications`、`speech_publication_retirements` | ✓ | ✗ | ✗ | ✗ | ✗ |
| `engine_instances`、`runtime_instance_heads`、`resource_pools`、`workspace_quotas` | ✓ | ✗ | ✗ | ✗ | ✗ |
| `speech_publication_heads`、`speech_builtin_voice_qualifications`、`speech_builtin_voice_publish_intents`、`speech_builtin_voice_qualification_provenance` | **✗** | ✗ | ✗ | ✗ | ✗ |
| `admission_leases`、`execution_stop_evidence`、`runtime_instance_intents`、`runtime_intent_deliveries`、`submit_intents`、`admission_operations` | ✗ | ✗ | ✗ | ✗ | ✗ |

- approvals 上的列级 INSERT 精确是 8 列：`approval_id, voice_id, revision, revision_id,
  binding_revision_id, source_approval_id, facts, expires_at`——**写不了** `probe_authorization_id`、
  `result_intent`、`approved_by/approved_at`。这正是 §4.5 里「伪造必然留下 NULL」的根据。
- 没有 publication head 权限（`speech_publication_heads` 零权限），`writer_identity_forbidden` 对
  发布者凭据的拒绝实测有效（§2.4）。
- 身份分离成立：CLI 只读 `SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL`，`deploy/compose.yaml`
  不转发该键（全文件 grep 无匹配），API 身份（`sn_api`）不获得该授权函数。
- 两处文档/实现漂移（P3）：`operator_attestation_writer_grants` 的 docstring 说
  「must not read qualifications, publications or publication heads」，实际给了
  `speech_publications`、`speech_publication_retirements` 的 `SELECT`（读不是写，但描述不准）；
  docstring 与设计文档都称「最小**列级** INSERT」，而 attestations 那张是**表级** INSERT
  （append-only 仍成立，因为无 UPDATE/DELETE）。

---

## 8. 部署装配与守卫变异

装配事实（静态核对 + 命令实测）：

- §6 口令生成仍在 `. deploy/compose.env` **之后**（第一处载入在第 409 行，`KEYS = (` 在其后）、
  写回文件（`path.write_text(text)` + `chmod 0o600`）、结束核对 `placeholders remaining: 0`；
  新的 `SPEECHNEXUS_OPERATOR_ATTESTATION_ROLE_PASSWORD` 已进 `KEYS`（实测该 `KEYS = (` 块内 **16** 个键）。
- 守卫是数据驱动的（从 runbook 文本与 `compose.env.example` 反推），所以新键不额外登记即时覆盖；
  下面三条变异确认它们真的咬住。

| 变异 | 位置 | hunk | 结果 |
| --- | --- | --- | --- |
| G1 从 §6 `KEYS` 删掉新键 | `docs/operations/compose-minimal.md` | 1 | **2 红**：`test_the_generated_password_list_covers_every_optional_role`、`test_each_provisioned_role_has_a_password_source` |
| G3 删掉 §6 第一处 `. deploy/compose.env` 块（使首个载入落到生成之后） | 同上 | 1 | **1 红**：`test_the_role_passwords_are_generated_after_the_environment_is_loaded` |
| 缺口探测：把 `compose.env.example` 里新键改成**空值**（不是 `change-me`） | `deploy/compose.env.example` | 1 | **38 全绿** |

第三条是个已存在的守卫盲点：`EXAMPLE_PLACEHOLDER` 只认 `^KEY=change-me`，空值既不算占位也不算
生成来源，于是 `test_each_provisioned_role_has_a_password_source` 看不见它；而空口令会让
`CREATE ROLE … PASSWORD ''` 失败或产生空口令角色。对 16 个键一律成立，**不是本 PR 引入**，
但既然本 PR 正在扩这组键，建议顺手把正则改成 `^KEY=(?:change-me.*)?$`。

**升级路径的默认策略成立**：升级脚本（runbook §6 后半段）创建 `sn_operator_attestation` 并重放
`operator_attestation_writer_grants`，但**不导出** DSN；`deploy/compose.yaml` 不转发；示例文件只有口令占位。
所以「角色预置了、能力仍未启用」在新建与升级两条路径上一致——这与 §1 的 A–E 实测互相印证。

---

## 9. 变异测试（作者未附变异表，由审查者自选三条、单点施加）

每处只改一行/一段，`git diff -U0 HEAD | grep -c '^@@'` 逐次记录；跑新增的单测 + 隔离 PG 套件（41 项）。

| # | 变异 | hunk | 结果 | 判断 |
| --- | --- | --- | --- | --- |
| M1 | `operator_attested_qualification.py`：`if len(identities) != 1 or identities[0]["forbidden"]:` → `if len(identities) != 1:` | 1 | **1 红 / 40 绿**：`test_the_writer_identity_cannot_repair_or_erase_its_own_record`（发布者凭据那半段） | 守卫有效，且**只有一条用例**咬住它 |
| M2 | `builtin_voice_publication.py`：删掉「facts 声称 operator verifier 但没有 attestation 行」时抛 `attestation_missing` | 1 | **41 全绿（存活）** | **该 fail-closed 分支零覆盖**。直接调用复核语义：原码 `attestation_missing`，变异体返回 `None`（即顺利发布成 controller_observed） |
| M3 | `operator_attestation.py`：删掉 `require_registration` 里的 `last_fencing_token`/`owner_fencing_token`/`owner_expires_at_us` 三行 | 1 | **2 红 / 39 绿**：单测 `test_model_and_instance_claims_are_checked_against_registration`、集成 `[…][fencing-stale_fencing_token]` | 守卫有效。注意集成用例在变异后报的是 `lifecycle_digest_mismatch`——说明 lifecycle 文档那一路只是「同一个自写文档的等式」，独立证据只有注册账本 |

三次都用 `git checkout -- <file>` 逐字节还原，`git status --porcelain` 为空；`git diff -U0 HEAD` 无残留。

---

## 10. 必须修清单

### P1（阻塞合并）

1. **声明身份能伪造 `controller_observed`（§4.5）**。修复：发布端对 controller 来源要求
   `probe_authorization_id IS NOT NULL`（或把 provenance 改成记录来源证据种类）；
   拒绝「`verifier_id` 是 probe 但 `probe_authorization_id` 为 NULL」的 approval。
   同时把作者那条把该形状断言为 `controller_observed` 的用例改为期望拒绝。
2. **读面没有可用身份（§4.4）**。修复：把 `provenance` + `attestations` 的 `SELECT` 加进
   `qualification_reader_grants`；runbook §6 明写读面用哪个 DSN；用例改用该身份执行 `REPORT_SQL`
   （现在的 owner 连接掩盖了这个缺口）。

### P2（合并前应修）

3. **声明过期后无法续期，且失败码错误（§2.5）**。要么给续期一条受控路径（例如
   `UNIQUE (voice_id, revision)` 语义下把 renewal 表示为 supersede 的新 revision，
   或新增 `supersedes_attestation_id` 并保留旧行），要么明确写「本条路径一次性、过期即需换
   voice revision」，并把 `UniqueViolation` 映射成具名拒绝（4）而不是 exit 5「结果未知请重放」。
   修这条时**不要**直接放宽唯一约束——它是当前唯一挡住「顶替」的机制。
4. **PR 描述与改动不符**。PR #878 的 body 描述的是「补上发布 binding / 配额 / 容量登记步骤」，
   与已合并的 #879（`docs/binding-quota-bootstrap`）逐字一致；实际 diff 是新增信任根。
   新增信任根的 PR 必须以设计文档为正文（或在 body 里首行指向它），否则审阅者只能从 diff 猜意图。
5. **读面字段命名会误导（§4.1）**：operator 行打印 `controller_artifact_digest`，内容是运维自写
   lifecycle 文档的摘要；`fencing_token` / `instance_generation` 是声明当时副本。建议改为
   `declared_lifecycle_digest`，并把副本字段加 `declared_` 前缀或在 `note` 里点明"声明当时"。

### P3（可跟进）

6. `attest_builtin_voice.py:176` 的 `conninfo_to_dict` 抛 `psycopg.ProgrammingError` 未被捕获 →
   不可解析 DSN 得到 traceback + **exit 1**，与设计 §6「配置错误返回 2」不符（零写入，fail-closed）。
7. `_recorded_source` 的 `attestation_missing` 分支零覆盖（§9 M2）；`test_operator_attestation_does_not_unlock_the_native_probe_path`
   第二段是装饰性断言（§5）。
8. `public_job_admission.py:302` 只查存在性，不看来源也不看时效（§3.1）。
9. `0055:54` `declared_by NAME` 会静默截断超过 63 字节的 `declared_by`（§6）。
10. `0055:59` 的 `UNIQUE (attestation_id, voice_id, revision, revision_id, binding_revision_id)` 冗余。
11. `operator_attestation_writer_grants` docstring 与实现不符（说不能读 publications / 列级 INSERT，
    实际可读 publications、attestations 是表级 INSERT）（§7）。
12. 「placeholder 不算口令来源」守卫对**空值**无效（§8 第三条变异）。

---

## 11. 覆盖限制

- 未取证真实外部引擎的 TTS `200`；本审查不涉及 GPU 与真实引擎可服务性。
- 「指向另一个 schema」按代码与单 schema 夹具核对，未实跑双 schema（§1 末）。
- public job 路径（`public_job_admission`）的过期资格后果按代码阅读给出，未跑完整异步 job。
- §7 的权限矩阵来自真 schema 上的 `has_table_privilege`/`has_column_privilege` 查询；另外用该身份
  建立了真实连接，实测了 `INSERT approvals`、发布、`SET ROLE` 与 `REPORT_SQL`（§2.4/§4.4/§4.5），
  但没有对 §7 的每一行都构造一次登录失败负例。
- 变异只做 3 处代码 + 3 处文档守卫；不做组合变异，也不据此推断守卫强度。
- 结论时效：核验于 2026-11-16，针对 head `6f118956`。`origin/main` 在本轮期间前进到 `9b2a1219`，
  三方 diff 的 merge-base 未变，结论不受影响。

---

## 第二轮：返修核实（head `82d55a82`）

核验于 2026-11-16 11:20–11:40（Asia/Shanghai），对象 `82d55a82`：在 `6f118956` 之上新增 6 个作者提交
（`1e0da73a` 标签由证据、`56547e37` 只读审计身份、`65dd0780` 单 revision 单声明、`ccc08c0a` 让两条薄弱用例能失败、
`046ed971`/`82d55a82` 文档）。第一轮的记录保持 `6f118956` 时点，不做改写。

### 结论（第二轮）：仍然不能合，但理由换了

第一轮两条 P1 都真的关上了，而且关法比我建议的更硬：

- **P1-①** 改为「标签由证据决定」+ 账本三态 `CHECK`。伪造行现在读成 `unverified/absent`，
  手工改标签被 `CheckViolation` 拒绝，声明身份对 provenance 与 `probe_authorization_id` 都没有写权限。
  这比原建议（发布端一个 `if`）好：不变量写进了数据库，且不必改既有控制器路径用例的列清单。
- **P1-②** 真的用文档身份跑通了：`sn_qualification_reader` 在两条 provisioning 路径都建、
  有口令键与宿主侧 DSN，一份报告同时读出 `controller_observed` 与 `operator_attested` 各一条，
  且该身份 `INSERT provenance` 被拒。

**但返修把问题推到了下一层，那里更严重（本轮新 P1）：`unverified` 只是一个标签，没有任何消费者读它。**
一条既无 probe 授权、也无声明记录的资格，仍会被同步 TTS 准入接受、被启动闸门算作已资格、
被 public job 的 builtin 兜底当成内建声音。于是 fail-closed 只存在于写命令与读面；
「部署方信任声明 + 来源批准 + 发布证据 + 网关策略」这条链在**准入侧不承重**。

### A. P1-① 是否真的关上 —— **PASS**

用 `sn_operator_attestation` 角色自己的凭据，只写它拿到的 8 列（`probe_authorization_id` 空、无 attestation）：

```
A1 最小身份写 8 列 probe 形状批准: 成功（无 probe_authorization_id）
A2 行内 probe_authorization_id = None
A3 provenance: {'source_kind': 'unverified', 'evidence_kind': 'absent',
                'attestation_id': None, 'probe_authorization_id': None}
A4 读面 source_kind = unverified | note = recorded as unverified: neither a probe
   authorization nor an attestation backs a source label
A5 标签是否已不可能读成 controller_observed: True
A6 owner 只改标签: CheckViolation
A6 writer 身份改 provenance: InsufficientPrivilege
A6 writer 身份补 probe_authorization_id: InsufficientPrivilege
A6 writer 身份直接插 provenance: InsufficientPrivilege
```

`provenance` 的三态 `CHECK` 把「标签必须有对应证据行」写进了账本：
`controller_observed` 必须带 `probe_authorization_id`、`operator_attested` 必须带 `attestation_id`、
否则只能 `unverified/absent` 且两列皆空。任何只改标签不改证据的写法都被数据库拒绝——
这条不变量不再依赖任何一层应用代码。

### B. P1-② 是否用文档身份跑通 —— **PASS**

`qualification_reader_grants` 现在放行 provenance、attestation、probe 授权三张只读表；
`sn_qualification_reader` 在 runbook **两条** provisioning 路径都建（第 590、1636 行），
口令键进了 §6 `KEYS`（该块已 17 个键），宿主侧 DSN 导出与命令齐备，
CI 守卫 `test_reference_deploy_dsn_contract` 把它登记为 2 处。实测（用该身份自己的 DSN，不是夹具 owner）：

```
B1 reader 身份读出 source_kind = ['controller_observed', 'operator_attested']
   voice-controller -> controller_observed | evidence: legacy_pre_0055
   voice-vivian     -> operator_attested    | evidence: provenance_row
   note: operator-attested external engine: not a controller observation;
         declared_* values are the declaration's snapshot, not live runtime state
B2 两种来源各一条: {'operator_attested': 1, 'controller_observed': 1}
B3 reader 身份插 provenance: InsufficientPrivilege
B4 CLI exit=0（真入口 speechnexus-qualification-provenance，三行 JSON + summary）
```

一点说明：controller 那一行的 `evidence=legacy_pre_0055`，因为同一份 release 证据只能覆盖一个 voice，
本 schema 内无法用真实发布路径再造第二个 controller 资格（作者新用例用同款造法）。
这不影响 B 的结论——读面确实通过最小身份把两种来源分开读出，且该身份不可写。

### C. 新 P1：`unverified` 仍被准入接受 —— **FAIL**

同一份账本里，A 步骤伪造并发布出来的那条 `unverified` 资格：

```
C1 provenance: {'source_kind': 'unverified', 'evidence_kind': 'absent'}
C2 同步 TTS 准入 require_builtin_voice: 通过, verifier_id = builtin-customvoice-v1
C3 public job builtin 兜底 _custom_voice: 返回 None（当作已验证的内建声音）
C4 has_qualified_voice 存在性 SQL 命中资格行: 2 | _voice 发布查找命中: 1
C5 unverified 是否被准入接受: 是 → fail-closed 只在 CLI 层
```

三个消费者逐个看（**本轮均未被本 PR 修改**，`public_job_admission.py` 的差异只来自已合并的 #880）：

| 消费者 | 判定依据 | 是否看来源 | 结果 |
| --- | --- | --- | --- |
| `speech/infrastructure/builtin_voice_qualification.py` `require_builtin_voice` | qualifications ⋈ approvals ⋈ source_approvals 的 facts、身份绑定、时效 | **否** | 接受（C2 实测） |
| `qualified_speech_facts_postgres.py:56` `has_qualified_voice` | `SELECT voice_id,revision FROM speech_builtin_voice_qualifications WHERE binding_revision_id=%s` + `_voice()` 的发布查找 | **否** | 两条 SQL 都命中（C4），且后续 `_voice` 只查 publications |
| `public_job_admission.py:331` `_custom_voice` 的 builtin 兜底 | `SELECT 1 FROM speech_builtin_voice_qualifications WHERE voice_id,revision,revision_id` | **否，且不看时效** | 接受（C3 实测） |

**影响**：持有 `sn_operator_attestation` 凭据（8 列 INSERT 足够）+ 发布身份凭据的人，可以绕过整个
信任声明链条，产出一条同步 TTS 可用的内建声音资格：

- 不需要 `--trust-declaration-file`，即不需要部署方对「这些实例的生命周期由运维掌握」的显式决定；
- 不需要 release/gateway 证据与来源批准的新鲜度以外的任何声明输入（来源批准仍需在有效期内，
  这是 `require_builtin_voice` 自带的条件；facts 里的实例身份也仍需与运行态 head 一致，
  而这两个表声明身份都能读）；
- 也不需要走 `speechnexus-attest-builtin-voice`，因此不触发 `writer_identity_forbidden`、
  目标安装比对、窗口上限等所有写入侧检查。

也就是说：**「来源可读」做到了，「来源可信」在准入侧没有落地。** 读面会诚实地说"来源未知"，
但 TTS 照样能用这条资格合成。

**为什么不能简单地在准入侧拒 `unverified`**（只陈述事实，不设计修法）：作者自己的用例
`test_rows_published_before_the_migration_are_reported_as_recorded_history` 断言「0055 之前发布、
且批准不带 probe 授权」的历史行合法地读成 `unverified`。任何「准入拒绝 unverified」的写法
都会让这类历史部署在升级后失去同步 TTS；所以修法必须能区分「历史遗留」与「证据缺失」，
这需要比一个标签更多的信息。

### D. 续期处置 —— **PASS**

```
D1 UNIQUE 约束: ['UNIQUE (approval_id)',
                 'UNIQUE (voice_id, revision, revision_id, binding_revision_id)']
D2 首次声明: committed att-tts-1
D3 续期: OperatorAttestationError code = attestation_revision_already_attested
D4 attestations 行数: 1 | 冗余唯一约束已去掉: True
```

反替代约束 `UNIQUE (voice_id, revision, revision_id, binding_revision_id)` **原样保留**（没有为了续期放宽），
冗余的 `UNIQUE (attestation_id, …)` 已删。错误码具名且 CLI 映射到退出码 4，附
`_REMEDIATION` 文案「publish a new voice revision and attest that instead」，不再是「结果未知，请重放」。
作者另附 revision 1 → 2 的集成用例。

### E. 命名 —— **PASS**

`controller_artifact_digest` 已改名 `declared_lifecycle_digest`，读面对声明快照字段统一 `declared_` 前缀
（`declared_by`/`declared_expires_at_us`/`declared_fencing_token`/`declared_instance_generation`/
`declared_observed_at_us`/`declared_lifecycle_digest`），且异常行 `note` 明写
「declared_* values are the declaration's snapshot, not live runtime state」——
第一轮担心的「把旧值当当前状态打印」已经说清。

### F. 另外三项

| 项 | 变异/命令 | 结果 |
| --- | --- | --- |
| `attestation_missing` 分支覆盖（第一轮 M2 存活） | 注释掉 1 处 `raise … attestation_missing` | **1 红**：`test_an_operator_approval_without_its_attestation_is_refused`。覆盖缺口已补 |
| 「native 门无 operator 入参」是否真能红 | 给 `require_native_ready` 加 `operator_declaration: object \| None = None` | **1 红**：`test_the_native_gate_has_no_operator_input`（断言签名恰为四个参数）。装饰断言确实换成了结构不变式 |
| 不可解析 DSN 的退出码（第一轮 P3-⑥） | `not-a-dsn` / `host=… password=X dbname` | **exit 2** + `error: SPEECHNEXUS_OPERATOR_ATTESTATION_WRITER_DATABASE_URL is not a valid connection string`（不再是 traceback + exit 1） |
| PR 描述（第一轮 P2-④） | `gh pr view 878 --json body` | 已改为「设计文档即正文…本 PR 新增一条信任根，不是补文档步骤」 |

本轮基线：单元 24 passed、隔离 PG 集成 24 passed、`tests/ci` 459 passed / 1647 subtests；
两次变异都在还原后确认 `git status --porcelain` 为空。

### G. 第一轮 P3 里仍未处理的

- `public_job_admission.py:331` 的 builtin 兜底仍只看存在性（现在成了 C 这条链上的关键一环）。
- `0055:54` `declared_by NAME` 仍会在超过 63 字节时静默截断（`_identifier` 允许 127 字符）。
- 读面对 operator 行仍打印 `probe_authorization_id`（恒为 NULL），对账时容易读成"有授权但为空"。

### H. 第二轮后的必须修清单（替代第一轮 §10）

**P1（阻塞合并）**

1. **`unverified` 在准入侧没有消费者（§C）**：同步 TTS 准入、启动闸门、public job builtin 兜底
   三处都只读 `speech_builtin_voice_qualifications`，因此一条无证据的资格可被合成使用。
   影响与修法约束（需区分历史遗留行）见 §C。

**P2（合并前应修）**

2. 若短期不修 P1，至少让三处消费者在读面之外也能看见来源：例如 `public_job_admission` 的
   builtin 兜底与 `has_qualified_voice` 的存在性查询同时要求 provenance 证据（这是方向性建议，
   具体做法由作者决定）。
3. 读面 `probe_authorization_id` 对 operator 行应省略或标 `null` 原因，避免"有授权但为空"的误读。

**已关闭**：第一轮 P1-①（§A）、P1-②（§B）、P2-③ 续期（§D）、P2-④ PR 描述（§F）、
P2-⑤ 命名（§E）、P3-⑥ DSN 退出码（§F）、P3-⑦ 两条薄弱用例（§F）、P3-⑩ 冗余唯一约束（§D）、
P3-⑪ 授权函数 docstring（`046ed971`）。

### 覆盖限制（第二轮）

- C 的 public job 分支用「SQL 直接落一条 `vrev-` 资格行」触发（`_custom_voice` 只查资格存在性，
  不看发布表），未跑完整异步 job；同步 TTS 准入那条是真实调用。
- `has_qualified_voice` 按函数体逐句复现其两条 SQL，未实例化 `SpeechPool`/`TtsRecipe`。
- B 的 controller 行用 0054 形状落库（同一 schema 内无法用同一份 release 证据发布第二个 voice）。
- 结论时效：核验于 2026-11-16，针对 `82d55a82`；`origin/main` 在此期间前进了 #879/#880 等提交，
  三方 diff 的 merge-base 随之变化，第一轮记录的基线与上下文保持原样。

---

## 第四轮：终核（head `f77b348b`）

核验于 2026-11-16 12:23–12:50（Asia/Shanghai），对象 `f77b348b`：在 `82d55a82` 之上新增
`0aa55dec`（准入侧读取来源证据、三处共用一份判据）与 `f77b348b`（拒绝点名 + 两条端到端）。
第三轮（`82d55a82`）记录保持原样，本节只记本轮结论。

### 结论（第四轮）：仍不能合，且只卡一项

**A / B / D / E 全部通过**：新授权确实最小且只读；一份判据被两处真实消费者共用（删改即三处转红）；
过期声明与已撤销靠内部码可区分；三条变异都能咬住且全部还原。

**唯一不成立的是 C：grandfather 分支不是「不可伪造的形状」。** 作者用「没有 provenance 行」表示
0055 之前的历史行并予接受，理由是「声明身份对该表没有 `INSERT`，所以谁也造不出这种行」。
但**资格行本身是发布身份写的**（`builtin_publisher_grants` 给的是列清单外的表级
`INSERT ON speech_builtin_voice_qualifications`），发布身份完全可以「写资格行、不写 provenance 行」——
这不是删除，而是根本不需要那条 DELETE 权限。我按这个路子实测成功了：
用第二轮同一对凭据（声明身份 + 发布身份）造出的资格，被同步 TTS 准入和 public Job 兜底当作
合法内建资格接受，读面照旧标 `unverified / legacy_pre_0055`。**第二轮的 P1 由此只被收窄，没有被关闭。**

### A. 新授权是否最小且只读 —— **PASS**

用 `public_job_admission_grants` 给运行角色真实建号、真实登录后逐项测量（`has_table_privilege`）：

```
A1 授权语句条数: 44 | 含 ON ALL TABLES: False | 含 GRANT ALL: False
A2 speech_builtin_voice_qualification_provenance: [s]
A2 speech_builtin_voice_operator_attestations: [s]
A2 speech_builtin_voice_qualifications: [s]        ← 原有只读，未变
A2 speech_builtin_voice_approvals: [-]              ← 没有任何权限，正确
A2 speech_publication_heads: [s]                    ← 原有只读，未变
A2 speech_builtin_voice_probe_authorizations: [-]    ← 没有任何权限，正确
A2 speech_publications: [s]                         ← 原有只读，未变
A3 两张新表只有 SELECT: True
A4 INSERT / UPDATE / DELETE provenance: InsufficientPrivilege
A4 INSERT / DELETE qualification: InsufficientPrivilege
A4 INSERT attestation: InsufficientPrivilege
A4 UPDATE publication_heads: InsufficientPrivilege
```

没有用 `GRANT … ON ALL TABLES`、没有 `GRANT ALL`，也没有顺手把 `approvals` 或 probe 授权表带进来；
运行角色对两张新表确实只有 `SELECT`，且对其它一切仍是原状。

### B. 三处是否真共用一份判据 —— **PASS（但你说的第三处其实不是消费者）**

源码核对：

```
B1 sync TTS 准入 require_builtin_voice: 引用共享判据=True | 自写 source_kind 判据=False
B1 public Job 兜底 _custom_voice:       引用共享判据=True | 自写 source_kind 判据=False
B1 启动闸门 has_qualified_voice:        引用共享判据=False | 自写 source_kind 判据=False
B2 has_qualified_voice 函数体内引用共享判据=False | 引用 require_builtin_voice=False
```

`has_qualified_voice` 仍是「存在性查询 + `_voice()` 发布查找」，它既不用共享判据，也不调
`require_builtin_voice`（该文件里的 `require_builtin_voice` 调用属于 `voice_execution_authority`
与 `_voice` 覆盖）。所以本轮真正共用的只有两处：同步 TTS 准入与 public Job 兜底。

**变异实测（把共享判据改成恒真 `"TRUE"`）**，三处断言全部转红，说明两处消费者确实都走这一份：

```
FAILED tests/integration/test_operator_attested_qualification_pg.py::test_an_elapsed_declaration_stops_admitting_work
FAILED tests/integration/test_public_job_admission_http_pg.py::test_a_builtin_qualification_without_source_evidence_is_refused_by_name
FAILED tests/integration/test_tts_factory_pg.py::test_factory_http_uses_real_postgres_qualification_and_admission
```

作者那条读源码的防漂移用例也真实有效：在 public Job 兜底里重内联一份
`source_kind='controller_observed'` 的判据后，`test_every_admission_consumer_uses_the_one_source_evidence_predicate`
转红。两处限制记在此：

1. 它的第三段对 `has_qualified_voice` 的「断言」是空断言——只检查该文件文本含 `require_builtin_voice(`，
   而这句话在该文件里由别的函数满足；注释写的「future edit cannot give the startup gate its own
   existence-only check」因此不成立（它本来就是 existence-only）。后果有限：闸门放行、每个请求仍被
   准入拒掉（fail-closed 在请求侧），但「伪造行能让服务认为有可播内建声音」这件事没人测。
2. 该守卫只认字面 `source_kind='controller_observed'`；换个写法（常量、拼接、别名）就能绕过。

### C. grandfather 分支是否真的不可伪造 —— **FAIL（本轮唯一阻塞项）**

作者的依据（原文）：`test_a_historical_row_without_provenance_stays_admitted` 的 docstring
「A real pre-0055 row has no provenance row at all, which no writer can forge: the declaration
identity holds no INSERT on that table」，以及设计 §「0054 之前的真历史行 | 没有 | 接受（grandfather）」。

实测反证（`writer` = `operator_attestation_writer_grants` 身份；`publisher` = `builtin_publisher_grants`
+ `publication_grants` 身份，都是仓库里已声明的部署身份）：

```
C1 writer 身份写入 8 列伪造批准: 成功（probe_authorization_id 为空、无 attestation）
C2 publisher 身份手工发布（INSERT speech_publications + INSERT speech_builtin_voice_qualifications，
   不写 provenance 行）: 成功
C3 publisher 身份 DELETE provenance: InsufficientPrivilege（不需要删——直接不写就行）
C4 provenance 行数: 0
C5 同步 TTS 准入 require_builtin_voice: 通过, verifier_id = builtin-customvoice-v1
C6 public Job 兜底 _custom_voice: 返回 None（无异常 = 当作内建资格）
C7 读面标签: unverified | evidence: legacy_pre_0055 | provenance_recorded: False
```

**为什么成立**：`provenance` 是 `speech_builtin_voice_qualifications` 的子表（`FOREIGN KEY
(voice_id,revision) REFERENCES …qualifications`），数据库层没有任何约束或触发器要求「资格行必须有
provenance 行」；而写资格行正是发布身份的权限。所以「无 provenance 行」既不等于「历史」，也不等于
「不可伪造」——它只等于「不是 `publish_builtin_voice` 写的」。

**影响**：第二轮 P1（无证据的资格可被合成使用）没有关闭，只是换了入口。最小攻击集与第二轮相同，
仍是「声明身份 + 发布身份」这对凭据；差别仅在于攻击者不能再跑文档里的发布命令，而要手写两条
`INSERT`（发布行、资格行）。审计侧仍会看到 `unverified / legacy_pre_0055`，但准入照收，所以
「读面说实话、准入不承重」这个第二轮的核心问题在这个形状上原样存在。发布身份是本 PR 承认的发布
权威，因此这不算越权提权；但它说明本轮宣称的「准入判据收成一份」只收住了有 provenance 行的形状。

按你的要求，**没有设计修法**；只陈述两条事实供裁决：资格行的 `INSERT` 是发布身份的必需权限
（`publish_builtin_voice` 自己就写这一行），因此「收窄权限」这条路线走不通；作者那条用例把
grandfather 行为写成了预期，说明这不是漏测，而是设计判断需要被重新审。

### D. 过期声明与已撤销 —— **PASS**

```
D1 在线声明准入: operator-attested-customvoice-v1
D2 过期声明: 拒绝 code = qualification_source_unverified
   detail = the builtin voice's qualification has no backing evidence: publish it through the
            controller probe path, or record an accepted operator attestation for it, then send a new request
D3 已撤销: 拒绝 code = policy_or_resource_disabled
D4 两个内部码可区分: True | qualification_source_unverified 在 _DIAGNOSTIC_CODES: True
```

过期声明确实停止准入；「已撤销」与「无证据/已过期」在日志里是两个不同的内部码
（`policy_or_resource_disabled` vs `qualification_source_unverified`），而对外两条都落在既有的
404/`engine_resource_disabled` 面上——这是对的，运维靠日志分。一句措辞提醒：过期场景的对外句子
说的是「no backing evidence」，没有提到「声明已过期」，靠内部码能区分，但句子本身对「过期」不够点题。

### E. 变异与基线

| 变异 | 结果 |
| --- | --- |
| 共享判据改恒真 | 3 红（同步准入 / Job 兜底 / 真 create_app 端到端） |
| 在 Job 兜底重内联判据 | 防漂移用例红 |
| `attestation_missing` 分支（M2） | 1 红：`test_an_operator_approval_without_its_attestation_is_refused` |
| 给 `require_native_ready` 加入参 | 1 红：`test_the_native_gate_has_no_operator_input` |

四次变异全部还原，`git status --porcelain` 为空。本轮基线：单元 27 + 隔离 PG 集成 26 = **53 passed**；
`tests/ci` **459 passed / 1647 subtests**；`test_public_job_admission_http_pg.py` 全量通过（需同时给
PG 与 MySQL DSN，缺 MySQL 时该文件的用例在夹具阶段就 error，不算通过）。

### 关于公开码 `422 voice_not_published` 的判断

可以接受，理由：这个码说的是「这个 revision 不可用」，而证据缺失时它确实不可用，`retryable=False`
也对；message 里同时点了「no backing evidence」和两种修法（探针路径 / 记录一次被接受的声明），
运维不会被引去找一个其实已经存在的发布。唯一的保留是：它和「真的没发布」共用一个码，两者只能靠
message 区分，而这正是 #877 那条规则允许的形态（码表达类别、message 点名具体缺项）。内部码
`qualification_source_unverified` 已进 `_DIAGNOSTIC_CODES`，同步侧的 404 日志能分开两种原因。
**不代理改契约。**

### 覆盖限制（第四轮）

- C 的攻击用真实角色 + 真实权限登录，但用仓库夹具的 schema/ledger 形状，没有跑完整 `create_app`
  链路（端到端那半段由作者的 422/202 用例覆盖，我复跑通过）。
- 我无法从部署侧证明真实环境里发布身份是否只有这套权限；结论基于仓库声明的
  `builtin_publisher_grants` + `publication_grants` 组合，以及设计文档明确要求的组合方式。
- 结论时效：核验于 2026-11-16，针对 `f77b348b`。`#878` 与 `#882` 的 CI 状态未纳入本判断（按你的要求不等）。

### 遗留容器与探针的清理命令（本轮之后不再需要）

```
# 作者/审查用的一次性容器（宿主侧，仓库外）
docker rm -f sn876-oa-pg sn876-oa-mysql
# 探针 schema 与角色（在 sn876-oa-pg 内）
psql "postgresql://sn_oa:sn_oa_pw@127.0.0.1:55470/sn_oa" -c "DO \$\$ DECLARE r record; BEGIN \
  FOR r IN SELECT nspname FROM pg_namespace WHERE nspname LIKE 'sn_oa_%' LOOP \
    EXECUTE format('DROP SCHEMA %I CASCADE', r.nspname); END LOOP; \
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname LIKE 'sn_oa_%' AND rolname <> 'sn_oa' LOOP \
    EXECUTE format('DROP OWNED BY %I', r.rolname); EXECUTE format('DROP ROLE %I', r.rolname); END LOOP; END \$\$;"
# 审查 worktree（报告已推送、内容已在 #882，可删）
git -C /Users/hrygo/Documents/SpeechNexus worktree remove /private/tmp/sn883-r2
git -C /Users/hrygo/Documents/SpeechNexus worktree remove /private/tmp/sn883   # 合并 #882 之后
# 探针脚本（仓库外，无仓库引用）
rm -rf /private/tmp/sn883-oa-probe
```

---

## 第五轮：终核（head `9e492e11`）

核验于 2026-11-16 13:05–13:35（Asia/Shanghai），对象 `9e492e11`（`fix: 准入不再豁免无 provenance 行的资格`）。
第四次记录保持 `f77b348b` 时点原样。本轮按 lead 裁定的四条 + his 追加的夹具判定共五项核。

### 结论（第五轮）：**PASS**

四条全部通过，另加夹具判定：`test_builtin_voice_qualification.py` 的改动是**修正不真实的夹具**且用例变强。
只剩两处**文案精度**问题（P3，见文末），不构成合并阻塞。

### 1. 最小攻击集现在是否被拒 —— **PASS**

原样重放（`writer` = `operator_attestation_writer_grants` 身份写 8 列批准；`publisher` =
`builtin_publisher_grants` + `publication_grants` 身份连上去手写 `speech_publications` +
`speech_builtin_voice_qualifications`，`document` 从 `facts->'public_document'` 复制，**不写 provenance 行**）：

```
攻击状态已建成: provenance 行数 = 0
同步 TTS 准入: 拒绝 code = qualification_source_unverified
  detail = the published builtin voice has no qualification source recorded: publish it again
           through the controller probe path, or record an accepted operator attestation for it,
           then send a new request
public Job 兜底: 拒绝 code = voice_not_published（同一 detail）
读面: unverified | evidence: legacy_pre_0055
```

同步侧与 Job 侧都拒绝，且点名「publish it again …」；读面仍如实记 `legacy_pre_0055`（第 5 条的读面要求）。

回归（同一轮实测）：

```
真 operator 行准入: 通过, verifier_id = operator-attested-customvoice-v1 | Job: None（内建资格）
        读面: operator_attested | evidence: provenance_row
真 controller 行准入: 通过, verifier_id = builtin-customvoice-v1 | Job: None（内建资格）
        读面: controller_observed | evidence: provenance_row
```

### 2. `test_builtin_voice_qualification.py` 被改：修正夹具，不是削弱覆盖 —— **PASS**

判定依据用代码与授权实测，不看作者说法：

| 事实 | 证据 |
| --- | --- |
| 真实控制器探测路径必然写 `probe_authorization_id` + `result_intent` | `qualification_probe_approval.py:409` 的 `INSERT` 列清单含这两列 |
| 只有 authorizer 身份能写这两列 | `qualification_authorizer_grants` 的 `GRANT INSERT (probe_authorization_id, result_intent)`；实测 `has_column_privilege` = True |
| 8 列身份（`qualification_writer_grants`）写不了 probe 授权列 | 实测其可写列恰为 8 列，`probe_authorization_id` 不在其中 |
| 账本强制两列成对 | `speech_builtin_voice_approvals` 的 `builtin_approval_probe_intent_pair` CHECK（我第一轮记过） |

所以旧夹具「8 列写批准、期望 controller_observed 资格」是**真实探测路径产生不出来的形状**，改掉它是修正。
用例同时**变强**：新增「有资格行、无 provenance → `AdmissionRejectedError(SOURCE_UNVERIFIED)`、detail 含
`no qualification source recorded`」与「补探针授权 + provenance 后才接受」两段；接受覆盖仍在
（`56 passed`，含该文件全部用例）。

### 3. 状态表达式有没有开出新洞 —— **PASS**

逐态实测（每态各造一行，同一 schema 内逐个验同步 TTS 与 public Job）：

| 状态 | 构造 | 同步 TTS | public Job | 判定 |
| --- | --- | --- | --- | --- |
| `absent`（无 provenance 行） | 手写发布 | 拒绝 `qualification_source_unverified` | 拒绝 `voice_not_published` | 拒绝 ✓ |
| `unverified` | 有行、无证据 | 拒绝 | 拒绝 | 拒绝 ✓ |
| `controller_without_probe` | 试图写 `controller_observed` + probe 空 | **无法构造**：`CheckViolation`（账本 CHECK 要求标签带 probe 授权） | — | 不可达 ✓ |
| `operator_without_attestation` | 试图写 `operator_attested` + attestation 空 | **无法构造**：`CheckViolation` | — | 不可达 ✓ |
| `operator_elapsed` | 真声明发布后把窗口移到过去 | 拒绝，detail 点名「实施例声明窗口已过期：发新 revision」 | 拒绝 | 拒绝 ✓ |
| `evidenced`（operator 窗口内） | 真声明发布 | 接受 | 接受 | 放行 ✓ |
| `evidenced`（controller） | 探针授权 + provenance | 接受 | 接受 | 放行 ✓ |

`CASE` 的 `ELSE` 落 `unverified`，`evidenced_sql` 只是 `state = 'evidenced'`，没有默认放行分支；
两个「差一点」状态被账本 CHECK 挡住，不可达（死代码，不是洞）。日志行还带上状态：
`builtin voice qualification refused: … code=qualification_source_unverified state=absent`。

### 4. `has_qualified_voice` 是不是真消费者 —— **PASS**

判据现在在**候选查询的 WHERE 条件里**（不是算出来不用）：

```sql
SELECT q.voice_id,q.revision FROM speech_builtin_voice_qualifications q
WHERE q.binding_revision_id=%s AND (qualification_source_evidenced_sql('q'))
ORDER BY q.voice_id,q.revision LIMIT 64
```

变异实测：把该 `AND (…)` 换成 `AND TRUE` 后，防漂移用例
`test_every_source_evidence_reader_uses_the_one_predicate` 转红；功能用例仍绿（启动门只在 TTS 装配时被调用，
`_voice` 的逐行复核兜住）——与作者的说明一致，且这正是该用例守护的语义（不许各写一份）。
该用例现在覆盖三处读者（`builtin_voice_qualification`、`public_job_admission`、`qualified_speech_facts`），
不再有第四轮那种「存在性查询 + 空断言」。

### 5. 读面没被误伤 + 发布说明 —— **PASS（附两处 P3 文案精度）**

`test_rows_published_before_the_migration_are_reported_as_recorded_history` 的**读面断言全部保留**：
`source_kind == 'unverified'`、`provenance_recorded is False`、`evidence == 'legacy_pre_0055'`、
note 含 `source unknown`、summary 里 `legacy_pre_0055 == 1` 且 `controller_observed == 0`；
**只有准入那半句**从「仍被接受」改成 `assert _job_fallback_selects_the_row(db) is False`。
这正是「读面记录事实、准入是否决层」的形态，没有被一起改掉。

发布说明写进了**两个面向发布的位置**（不是只在设计文档）：
`docs/operations/builtin-voice-qualification.md` 的「发布说明（已知限制）」与
`docs/plans/mvp-release-readiness.md` 第 9 条。实测核心断言与实现一致：
0055 前无 provenance 行的资格**不再被准入接受**（同步 TTS 与 public Job 都拒绝，见第 1、3 条）。

两处 P3（文案，不阻塞）：

1. **「重新发布即可恢复」对同一 revision 不成立**：实测同 revision 重发被
   `BuiltinVoicePublicationRejectedError("publication_conflict")` 拒绝（`speech_publications` 以
   `(kind,object_id,revision)` 为键、资格行以 `(voice_id,revision)` 为键，都不会让旧行「补上」证据）。
   实际恢复动作是**发布新的 voice revision 并重新取得资格**——运维手册第 49/56 行对续期已经这么写，
   但发布说明那句只写「重新发布」，照字面执行会撞 `publication_conflict`。建议改成「发布新的 voice
   revision 并重新取得资格（探针或声明其一）」。
2. **未来时间的 attestation 落到 `operator_without_attestation`**：实测 `observed_at_us > now` 时状态是
   `operator_without_attestation`，文案说「claims an operator declaration that is not recorded」——
   它不是没记录，是窗口还没开始。拒绝正确，文案不准（P3，lead 已裁定本片不修）。

### 本轮基线与覆盖限制

- 单元 27 + 隔离 PG 集成 29（含被改的 `test_builtin_voice_qualification.py` 全部用例）= **56 passed**；
  `tests/ci` **459 passed / 1647 subtests**。
- 变异：启动门去判据 → 防漂移用例红（已还原，`git status --porcelain` 空）。
- **限制**：本轮的隔离 PG 是宿主上临时起的 PostgreSQL 18.6 集群（端口 55470），不是作者原来的容器；
  宿主 Docker 未运行、本机也没有 MySQL，因此 `test_public_job_admission_http_pg.py` 与
  `test_tts_factory_pg.py`（都在夹具阶段要求同时给 PG 与 MySQL DSN）**本轮未复跑**。第 1/3/5 条的
  准入结论来自直接调用 `require_builtin_voice` 与 `public_job_admission._custom_voice` 的实测，
  HTTP 状态码与 message 那层沿用作者用例的既有断言（上一 head 上我复跑过该文件，11 passed）。
- 结论时效：2026-11-16，针对 `9e492e11`；`#882` 的代码矩阵未等（按 lead 要求）。

### 清理命令（本轮之后不再需要这套环境）

```
# 本轮临时起的隔离 PG（宿主，非仓库）
pg_ctl -D /private/tmp/sn883-pg stop -m fast && rm -rf /private/tmp/sn883-pg
# 探针 schema/角色（如果集群还留着）
psql "postgresql://sn_oa@127.0.0.1:55470/sn_oa" -tAc "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'sn_oa_%'" \
  | xargs -r -n1 -I{} psql "postgresql://sn_oa@127.0.0.1:55470/sn_oa" -c 'DROP SCHEMA IF EXISTS "{}" CASCADE'
psql "postgresql://sn_oa@127.0.0.1:55470/sn_oa" -tAc "SELECT rolname FROM pg_roles WHERE rolname LIKE 'sn_oa_%' AND rolname <> 'sn_oa'" \
  | xargs -r -n1 -I{} psql "postgresql://sn_oa@127.0.0.1:55470/sn_oa" -c 'DROP OWNED BY "{}"' -c 'DROP ROLE IF EXISTS "{}"'
# 审查 worktree 与探针脚本（报告已推送、内容在 #882）
git -C /Users/hrygo/Documents/SpeechNexus worktree remove /private/tmp/sn883-r2
git -C /Users/hrygo/Documents/SpeechNexus worktree remove /private/tmp/sn883   # 合并 #882 之后
rm -rf /private/tmp/sn883-oa-probe
```
