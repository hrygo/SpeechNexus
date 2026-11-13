# Deployment bootstrap

This directory defines a local, single-node Compose topology for the **minimal
synchronous speech surface**. Following
[ADR 0004](../docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md),
the architecture remains cluster-ready, but this bootstrap is not a production
capacity, multi-node high-availability or model-qualification claim. Release
images must still be pinned by digest before publication; the bootstrap uses
explicit version tags for readability.

Start with the executable, command-by-command guide:
[Minimal synchronous ASR/TTS Compose deployment](../docs/operations/compose-minimal.md).
It covers environment generation, private-config preparation, migrations,
non-owner roles, readiness probes and one-time engine registration.

## Services and profiles

The base `docker compose --env-file deploy/compose.env.example -f deploy/compose.yaml config --services`
set is intentionally limited to:

- `postgres` and `minio`: private state and object storage;
- `minio-init`: creates and versions the input/result buckets;
- `api`: public SpeechNexus API;
- `gateway`: execution gateway with private file-ASR/file-TTS configuration.

The following production units are deliberately **not** part of this file:
`node-controller`, `runtime-delivery-worker`, `asset-maintain` and
`voice-evidence-worker`. Their absence is a scope statement, not an assertion that
those capabilities work elsewhere. They are excluded because controller lifecycle,
runtime delivery, retention maintenance and automatic evidence recording (which
needs a recorder DSN plus report object storage) are not on the synchronous request
path or are not assembled here.

Additional profiles are opt-in:

- `migration`: one-shot Alembic migration with the separate migration DSN;
- `engine-registration`: one-shot `speechnexus-register-engine` invocation;
- `web`: the static Admin and Workbench frontends;
- `binding-disable`: explicitly starts the disable worker, which is required to
  process persisted disable intents; it is absent from the base topology.
- `job-dispatch`: explicitly starts the public Job dispatcher
  `job-dispatch-worker`; it is absent from the base topology and the base `up`
  never starts it.
- `asset-inspection`: explicitly starts the private audio inspection container
  `asset-inspector`; it is absent from the base topology, publishes no host port
  and joins only a dedicated `internal: true` network. Enabling it requires
  `SPEECHNEXUS_ASSET_INSPECTOR=audio`, `SPEECHNEXUS_ASSET_INSPECTION_URL` and a
  non-empty `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` in `compose.env`. The design and
  the verified assembly facts are in the
  [asset inspection container](../docs/components/asset-inspection-container.md#44-compose-装配).

The `workbench` container serves only the frontend; it does not start the
independent Workbench API. Setting `SPEECHNEXUS_PROCESS_ROLE` identifies a
process; it does not switch the backend command to another service factory.

Both Web images require `WEB_SPEECH_UPSTREAM`, `WEB_WORKBENCH_UPSTREAM` and
`WEB_DNS_RESOLVER`. Origins must be `http://` or `https://` followed by a DNS
name or IPv4 address and an explicit port, without a path or credentials. DNS
resolver is an IPv4 address. Invalid or missing values stop startup; there is no
Workbench fallback. The example Workbench hostname is deliberately unassembled,
so its requests fail until an operator configures an actual service. DNS lookup
occurs on requests so an unavailable Workbench does not prevent Speech startup.
HTTPS verifies the upstream certificate and hostname against the image system CA
bundle; install a trusted deployment CA in that bundle when required. Use HTTPS
for cross-host connections, and separately protect the browser-facing ingress.
Paths and queries pass through unchanged. This proxy configuration alone does not
qualify a real service, cross-host TLS, or a physical topology.


## Current capability limits

`/healthz` and `/readyz` are startup/dependency probes, not business acceptance:

- public synchronous ASR is still blocked by the missing production terminal-state
  writer; a live request can return `504 operation_timeout` even when the gateway
  staged a result;
- public TTS remains blocked by the missing public dispatch path; this Compose only
  prepares the gateway's private file-TTS references;
- public asynchronous Job execution is not qualified here: the opt-in
  `job-dispatch` profile only runs the dispatcher, this bootstrap provides no real
  engine, and submission still needs the Job writer and admission roles below;
- real model quality, CUDA capacity, physical multi-node recovery, content-network
  isolation and final Gate approval require separate evidence.

Do not use a green Compose health check to close any of those items.

## Local endpoints

Published ports bind to loopback only:

- Speech API: `http://127.0.0.1:8000`
- Execution gateway: `http://127.0.0.1:8010`
- PostgreSQL: `127.0.0.1:55432`
- MinIO S3: `127.0.0.1:59000`; console: `127.0.0.1:59001`
- Admin UI (`web` profile): `http://127.0.0.1:8080`
- Workbench UI (`web` profile): `http://127.0.0.1:8081`

The `asset-inspection` profile publishes nothing: `asset-inspector` is reachable
only as `http://asset-inspector:8080` from containers on its `internal: true`
network, so the host has no inspection endpoint to probe. The API deliberately
does not depend on the container being healthy: with `audio` selected, a stopped
or misconfigured inspector answers `503 asset_inspection_unavailable` per request
instead of blocking API startup. A blank token makes the inspector container exit
at startup naming `SPEECHNEXUS_INSPECTION_TOKEN`; the resulting restart loop is a
configuration error, not a ready service.

The gateway's private file-ASR/file-TTS object adapter requires a private IP
literal rather than a DNS name, so MinIO receives the fixed Compose-network
address `172.28.0.10`. If `172.28.0.0/24` conflicts with the host, change the
subnet and both private JSON object endpoints together. The examples and
private-file rules are in [deploy/private](private/README.md).

`postgres-data` and `minio-data` are named volumes. `minio-init` creates and
verifies versioning for the transcription input bucket, the Artifact bucket and
`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` (the gateway staging bucket the dispatcher
reads back). It uses the local MinIO root identity only; a real deployment must
provision separately scoped object credentials and reviewed buckets outside this
bootstrap.

## Build and dependency boundary

The Python image is the shared composition for API, gateway, bucket init,
migration, registration and the Job dispatcher. It now includes:

- the backend migration extra so the opt-in `migrate` profile can run;
- the hash-pinned CPU VAD requirements from
  [engines/cpu-vad/requirements.txt](engines/cpu-vad/requirements.txt), because
  configured file-ASR validates and opens the Silero ONNX model at gateway
  startup.

The build contexts include the shared contracts workspace. `.dockerignore`
excludes runtime data, credentials, local dependencies and caches. When a build
runs behind a deployment/session CA, supply its trusted combined CA bundle as
BuildKit secret `proxy_ca`. The optional secret is mounted only in networked
dependency-install steps and is not copied into the final image. Do not disable
TLS verification.

## Configuration authority

Every Python process validates its own `pydantic-settings` configuration and
declares explicit node, role and instance identity. There is no hidden fallback
service endpoint or implicit migration-owner runtime identity:

- `SPEECHNEXUS_DATABASE_URL` comes from `SPEECHNEXUS_API_DATABASE_URL`;
- `SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL` must name the non-owner gateway role;
- `SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL` must name a separately provisioned,
  non-owner reader with only `gateway_query_grants` column privileges. The default
  control lifespan refuses missing or unsafe authority; it never reuses the writer
  credential. Each original-execution GET uses one repeatable-read, read-only snapshot;
- registration and migration use separate one-shot DSNs;
- file-ASR/file-TTS credentials and configuration are always supplied together;
- private files must be owner-only regular files under the mounted private path.

Production entry points, Host/Origin policy, shared service credentials, backup,
object IAM and capacity qualification remain deployment decisions. The initial
product has no login, user directory, RBAC or tenant isolation.

## Optional writers and background units

Asset HTTP routes can use the local object credentials, but production must
replace them with narrowly scoped credentials. The
[asset configuration and recovery guide](../docs/components/production-assets.md)
describes paired object keys, pre-created versioned buckets, non-owner runtime
grants, the 7-day default/explicit TTL policy and Host/Origin guards. The
`speechnexus-asset-maintain` entry point is not started here and must be
provisioned separately before retention/unknown-original polling is claimed.

The public Job dispatcher is registered separately as
`speechnexus-job-dispatch-worker` and is assembled by the opt-in `job-dispatch`
profile (see below). It requires explicit worker identity, a non-owner PostgreSQL
role, the execution-gateway origin/credential and every batch/lease/poll/timeout
bound in
[production jobs](../docs/components/production-jobs.md#worker-入口与运行配置del-98).
It is not part of this minimal surface.

## Job dispatcher (`job-dispatch` profile)

`job-dispatch-worker` runs the public Job dispatcher entry point
`speechnexus-job-dispatch-worker`. The base `up` never starts it. Prepare the
worker DSN and its grants first; only then start the profile:

```bash
docker compose --env-file deploy/compose.env \
  -f deploy/compose.yaml --profile job-dispatch up -d
```

The service has no healthcheck, and a blank DSN makes it exit before claiming any
lease. With `restart: unless-stopped` that shows up as a restart loop, while
`docker compose ... up -d` (and `--wait`, which has no probe to wait on) still
returns success. A successful `up` is therefore not evidence that the dispatcher
is ready: confirm the worker is running without restarts, for example with
`docker compose --env-file deploy/compose.env -f deploy/compose.yaml ps` and
`logs job-dispatch-worker`, and fix the DSN/grants before retrying.

The container entrypoint is not the worker: it is `deploy/job-startup-gate.py`,
mounted read-only and given the worker's own environment. Before it `exec`s
`speechnexus-job-dispatch-worker`, the gate verifies the prerequisites the worker
could only discover *after* claiming a lease and names each missing one:

- the dispatch DSN is present and its role is not a superuser, does not own schema
  `CREATE`, the database or any schema relation (the same guard the worker runs);
- the worker's own `SELECT` probes pass, and - when
  `SPEECHNEXUS_JOB_DISPATCH_KINDS` contains `voice_validation` - the two
  `voice_validation_worker_grants` relations are readable;
- the post-claim write columns (`speech_jobs`, `coordination_jobs`,
  `speech_job_events`, `speech_job_cancellation_not_submitted`) are granted, so a
  role missing `job_dispatch_grants` stops at container start instead of failing
  after the first claim;
- `speech.alembic_version` exists and its single row equals the packaged migration
  head (a role provisioned before the read-only grant reports the head as `pending`
  instead of failing the upgrade);
- both `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` (Artifact) and
  `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` (gateway staging read-back) exist and have
  versioning enabled; the gateway's private `result_bucket` in
  `gateway-file-asr.json`/`gateway-file-tts.json` equals the latter. This covers
  the staging bucket that `check_ready` alone never probed, so a missing or
  unversioned result bucket no longer fails only after the first publication.

A failure exits non-zero with `FAIL: <named prerequisite>` and the worker never
starts; the real engine and a registered `ready` instance, and the API-side
writer/admission DSNs, are printed as `pending` lines because they are deployment
inputs this container cannot verify - the API already answers `503 job_unavailable`
at creation for a blank Job writer/admission DSN, which is before any claim. The
gate reads the gateway configs through `SPEECHNEXUS_JOB_GATE_PRIVATE_DIR`
(`/run/speechnexus-private` in Compose); it never reads credentials from them.

Provision the three Job roles with
`uv run --all-packages python scripts/dev-env/provision-job-roles.py` after the
migration profile; it reads the role names and passwords from the three Job DSNs
and installs the five dispatch grant groups, the writer baseline, the read-only
`speech.alembic_version` `SELECT` and `voice_validation_worker_grants` when that
kind is selected. The step-by-step path
is [the minimal deployment runbook §6](../docs/operations/compose-minimal.md#异步-job-运行角色与授权).

The command-by-command path from a blank environment to one submitted Job,
including the six prerequisites above and a read-only preflight, is
[the asynchronous Job runbook](../docs/operations/async-job-runbook.md).
Its self-check is `scripts/dev-env/check-async-job-prereqs.py`.

The service shares the gateway network namespace and calls
`http://127.0.0.1:8010`, because `JobDispatchSettings` rejects plain HTTP outside
loopback. That is a single-host Compose property, not a multi-node transport
design. `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` defaults to blank and must name a
non-owner worker role; a blank value fails closed before any lease is claimed.
[production jobs](../docs/components/production-jobs.md#worker-入口与运行配置del-98)
lists the complete variable set and its bounds; `compose.yaml` supplies explicit
reference values for every one, and the worker itself has no in-process default
for them. `SPEECHNEXUS_OBJECT_STORAGE_BUCKET` (Artifact output) and
`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` (gateway staging read-back) must be buckets
`minio-init` created and versioned, and the latter must match the private gateway
configs' `result_bucket`. The reference environment uses `speechnexus-results` for
both. Because `minio-init` receives the same variable, a non-default staging
bucket is created and versioned before the worker starts; if `minio-init` fails,
`depends_on: service_completed_successfully` keeps the worker from starting at
all. The local bootstrap still gives the worker the MinIO root identity
(`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD`), as it does for the API and gateway. That
is a local convenience, not object-layer least privilege: `read_only`, `cap_drop`
and `no-new-privileges` constrain the container process only, not S3
authorization, so production must supply narrowly scoped object credentials.

`SPEECHNEXUS_JOB_DISPATCH_KINDS` selects the kinds this process claims and
defaults to `asr,tts`. It is comma-separated, de-duplicated and must stay inside
the dispatchable union (`asr`, `tts`, `voice_design`, `voice_clone`,
`voice_validation`). `voice_design`/`voice_clone` need the separate voice lanes;
the default queue never claims them. Enabling `voice_validation` is not only a
kinds change:

1. install the dispatcher-role grants — `job_runtime_grants`,
   `job_dispatch_grants` (which already includes the Artifact publisher and the
   cancellation not-submitted INSERT), `public_job_grants`,
   `cancellation_reconciliation_grants` and `coordination_runtime_grants`;
2. additionally install `voice_validation_worker_grants(schema, worker_role)`.
   Beyond `USAGE` on the schema, it grants only two `SELECT`s — on
   `speech_voice_draft_revisions` and `speech_assets` — the frozen draft revision
   and the ready reference Asset the validation reader replays (#553). It grants
   no draft writes, publication tables or object-storage rights. When
   `voice_validation` is in `SPEECHNEXUS_JOB_DISPATCH_KINDS`, the worker probes
   both tables during its startup role check and refuses to start without them;
3. set `SPEECHNEXUS_JOB_DISPATCH_KINDS=asr,tts,voice_validation` in `compose.env`.

The startup check runs before the dispatcher exists, so a missing grant stops the
process (non-zero exit, a restart loop under Compose) instead of claiming a Job
and failing at publication. The same role check rejects superuser, schema
`CREATE`, database owner and membership in any table owner of the configured
schema; it does not audit extra DML grants, so a broader role is not rejected and
minimum privilege remains the deployment's responsibility. Do not treat the API
role's superset as worker evidence. A running dispatcher is still not a real
engine, GPU or end-to-end asynchronous acceptance.

预设草稿 HTTP writer 默认关闭。仅在操作者已应用 `0009_profile_drafts` 并独立配置最小非 owner 角色后，设置 `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`；不得复用 Compose bootstrap owner。启动仅验证权限并管理独立连接池，不执行迁移或 grant。原请求/原键重放、固定读取和限额见 [profile drafts](../docs/components/profile-drafts.md#http-runtime-and-controlled-writer)。此配置不发布或激活预设。

绑定草稿选择一个显式模式：`SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` 使用旧三表角色；
`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` 使用预先迁移到 0011 的四表最小角色，
启用原子默认值声明与固定快照 GET。两者互斥、默认均为空；没有通用数据库身份回退，
也不会自动扩旧角色权限。固定 binding GET 另需
`SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL`：一个只读非 owner 角色，可 SELECT
binding 历史、`speech_publications` 与证据表。未配置时 GET 返回
`503 binding_unavailable`，不会把已发布 revision 谎报为 draft。绑定列表另需
`SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL`：只读非 owner 角色，可 SELECT
`speech_binding_families`、`speech_binding_revisions`、`speech_publications`、
`speech_publication_heads` 与 `speech_publication_retirements`；启动逐表校验
SELECT 且拒绝任何写权限，未配置时列表返回 `503 model_binding_service_unavailable`。

公开读取面另需三个只读非 owner 身份，`compose.yaml` 的 api 环境均以空默认透传，
未配置的既有部署仍可渲染并启动；配置了与声明权限面不符的角色则 API 启动失败。
`SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL` 使用 `model_catalog_reader_grants`：
`speech_publications`、仅已发布的 `speech_published_binding_revisions` 视图、
`speech_catalog_state`、`speech_publication_retirements` 与
`speech_publication_heads` 的 SELECT，不读草稿表，供 `GET /v1/models` 与
`GET /v1/models/{model}`；未配置或读取失败时返回 `503 model_catalogue_unavailable`。
`SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL` 使用 `artifact_reader_grants`：
`speech_artifacts` 与 `speech_workspaces` 的 SELECT，供
`GET /v1/nexus/artifacts/{artifact_id}` 及其内容下载，并继续使用上文的成对对象存储
键；未配置时返回 `503 artifact_service_unavailable`。
`SPEECHNEXUS_AUDIT_READER_DATABASE_URL` 使用 `audit_reader_grants`：仅
`speech_publication_lifecycle_events` 的 SELECT，供 `GET /v1/nexus/audit-events`；
该身份不得复用生命周期写入角色，否则读取路径会获得追加或重放证据的能力。未配置时
返回 `503 audit_service_unavailable`。
`SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL` 只授予 `engine_instances` 与
`speech_binding_revisions` 的 SELECT，供 `GET /v1/nexus/admin/model-instances` 与
`GET /v1/nexus/admin/model-instances/{instance_id}/generations/{generation}`；实例意图、
租约、命令、提交意图与停止证据不在该身份可读范围内，不得复用 owner 或任何写入角色。
该投影目前没有对应的仓库授权函数，按
[最小部署手册 §6](../docs/operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权)
中的显式 `GRANT` 预置。未配置时两个端点返回
`503 model_instance_service_unavailable`。

隔离租约的运维释放另需独立写入身份
`SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL`：由 owner 运行
`quarantine_release_grants(schema, role)` 预置的非 owner 角色，持有 schema `USAGE`
（无 `CREATE`）与九张关系的列级 `SELECT`/`UPDATE`/`INSERT`。它在同一个事务里写
`execution_stop_evidence` 运维证据、关闭唯一提交意图、归还 `workspace_quotas`、
`host_nodes`、`engine_instances`、`admission_leases` 的容量，并追加
`admission_quarantine_releases` 账本；其中只有后两张表有 `INSERT`，账本表没有
`UPDATE`，因为 append-only 触发器负责不可改写。该身份不得复用迁移 owner、`sn_api`
运行时角色或任何 gateway/worker 写入身份。启动时逐列比对实际 ACL 与声明面：多一列、
少一列、schema 内任何序列的 `USAGE`/`SELECT`/`UPDATE`、append-only 触发器缺失或未启用
都会让 API 启动失败并报 `Quarantine release startup rejected`。`compose.yaml` 的 api
环境以空默认透传本变量，未配置的既有部署仍可渲染并启动，此时
`POST /v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases` 返回
`503 quarantine_release_unavailable`（`retryable: true`，须先确认该键是否配置，不要盲目
轮询）。已配置但运行期授权被回收时返回 `503 quarantine_release_privilege_missing`，迁移
未到位时返回 `503 quarantine_release_schema_not_migrated`，其余存储拒绝返回
`503 quarantine_release_failed`；这四种 5xx 都不表示证据冲突，只有真实的状态不匹配才返回
`409 quarantine_release_conflict`。

公开 Job 提交与读取默认关闭。应用 `0017_job_submissions` 并为非 owner 角色授予
`speech_jobs` / `speech_job_events` / `speech_job_submissions` 的最小写入权限及发布事实
只读权限后，设置 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`；启动只验证权限，不执行迁移
或 grant。未配置时 `/v1/nexus/jobs` 返回既有错误包封的 `503 job_unavailable`，不会
伪造 pending Job。

应用 `0053_voice_draft_publication` 后，Job writer 还必须只读
`speech_voice_draft_publications`：这是发布后草稿读投影的事实来源（`draft_id`、草稿
revision、`voice_id`、voice revision、`validation_job_id`）。视图由迁移 owner 持有并以
`security_invoker = false` 执行，因此该身份只读视图，仍不持有
`speech_voice_capability_evidence` 或 `speech_voice_revisions` 的任何权限；
`voice_draft_grants` 已包含这条授权。缺少它时已发布的草稿无法投影出契约要求的字段，
启动按声明读面直接失败，不会带着半个读模型上线。

工作区写操作默认关闭。执行 `0049_workspace_commands` 后，由 owner 运行
`workspace_writer_grants(schema, role)` 并为非 owner 角色设置
`SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL`；不得复用 Compose bootstrap owner 或资产
运行时角色。读取继续使用资产运行时角色，该角色对 `speech_workspaces` 只被授予
`UPDATE (metadata_revision)`，因而无法改写显示名或状态；写操作使用本配置的独立身份。
启动按 schema 校验其权限面：仅 `speech_workspaces` 与 `speech_workspace_commands`
两张声明表，`speech_workspaces` 只接受列级 `INSERT (workspace_id, display_name,
workspace_state, created_at_us)` 与 `UPDATE (display_name, workspace_state,
metadata_revision)`，命令表只接受列级 `INSERT`，表级 `INSERT`/`UPDATE` 与任何
`DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER` 一律拒绝；校验不通过时进程启动失败，
不执行迁移或 grant。同一 `Idempotency-Key` 的相同请求返回首次接受的身份，不同请求体、
目标或操作返回 `409 idempotency_conflict`，记录的接受结果已被删除时返回 503 而不重建；
`expected_metadata_revision` 与存储值不一致返回 `409 configuration_changed`。
`policy_id` 在尚无策略注册表的部署中返回 `422 workspace_policy_unsupported`。
未配置本变量时两个写操作返回 `503 workspace_service_unavailable`，读操作不受影响。
`compose.yaml` 的 api 环境以空默认透传本变量，未配置的既有部署仍可渲染并启动；
配置了与声明权限面不符的角色则 API 启动失败，请求不会带着错误身份上线。
进程内还要求资产运行时已装配（成对的 `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` /
`_SECRET_KEY`，Compose 固定提供），只设置本变量而关闭资产运行时不会装配写端口。

声音发布还需要独立的 publisher 角色：应用 0047 后由 owner 运行
`voice_publication_grants(schema, role)`，设置 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`；
它必须与 Job writer 指向同一数据库和服务器，但不得复用 Job writer 身份。启动按 schema
校验其完整权限面：只允许 `voice_publication_grants` 声明的 14 张表，白名单之外的表级或
列级权限与 schema `CREATE` 一律拒绝；表级与列级分开判定，列级授权不会因表级检查为 false
而漏检。新增同类校验时必须同时补真实角色拒绝用例。启动校验不通过或未配置时发布返回 503。

Artifact reader、binding writer、binding admin reader、public Job writer 与 workbench
runtime 的启动校验遵循同一条权限面规则：配置 schema 内声明表之外不得持有任何表级或列级
权限；可写表拒绝 `DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER`，表级 `INSERT` 与列级
`INSERT`/`UPDATE`/`REFERENCES` 分开判定；schema 需要 `USAGE` 且拒绝 `CREATE`。
列级判定一律使用 `has_column_privilege`/`has_any_column_privilege`，不以表级结果为代理。

public Job writer 的 15 张可写表只接受列级 `INSERT`：表级 `INSERT` 在任何声明表上都被
拒绝，列集必须与 `job_writer_columns.py` 中的声明完全一致，少一列、多一列或新增列都不
会自动扩权。拒绝信息点名权限类型与关系，并给出撤销授权或扩声明表的修复动作。

从旧版本升级时，若 Job writer 角色还持有表级 `INSERT`，必须先撤销再重新授予列级权限，
否则启动会按预期拒绝。对同一 schema 和角色执行：

```sql
REVOKE INSERT ON
  <schema>.coordination_jobs,
  <schema>.coordination_outbox,
  <schema>.admission_operations,
  <schema>.speech_job_intents,
  <schema>.speech_artifacts,
  <schema>.speech_job_cancellation_not_submitted,
  <schema>.speech_jobs,
  <schema>.speech_job_events,
  <schema>.speech_job_submissions,
  <schema>.speech_job_cancellations,
  <schema>.speech_job_cancellation_fences,
  <schema>.speech_job_cancellation_reconciliations,
  <schema>.speech_voice_drafts,
  <schema>.speech_voice_draft_revisions,
  <schema>.speech_voice_metadata_commands
FROM <job-writer-role>;

-- 然后对同一 Job writer 角色重跑以下 grant 函数（逐个）：
-- job_runtime_grants
-- job_dispatch_grants
-- public_job_grants
-- voice_draft_grants
-- public_job_submission_grants
-- job_cancellation_fence_grants
-- cancellation_not_submitted_grants
-- cancellation_reconciliation_grants
-- artifact_publisher_grants
--
-- public_job_admission_grants 属于独立的 admission 角色，不要应用到 Job writer：
-- 它只给 speech_job_events 4/8 列，并额外授予 admission_attempts / requests / leases。
```
独立记录角色使用 `voice_evidence_recorder_grants` 和
`SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL`，通过
`speechnexus-record-voice-evidence` 显式写入证据，或由
`speechnexus-voice-evidence-worker` 周期消费已提交的校验报告并在中断后自愈。该 worker
不在默认 Compose 面内，需要显式提供 recorder DSN、`SPEECHNEXUS_DATABASE_SCHEMA`（无默认
值，缺失即 exit 2）、报告 Artifact 所在对象存储的读取凭据（只需 GET，不需要写入或删除）
与轮询参数；recorder DSN 必须与 Job/Artifact 同一可写主库（指向 logical replica 时，
瞬时不一致可能被 `validation_report_mismatch` 判成永久）。启动会按
`voice_evidence_recorder_grants` 的列级形状拒绝更宽的身份，包括配置 schema 之外的表权限。退休角色需在
0047 后重跑 `binding_lifecycle_grants`，否则生命周期启动校验会拒绝旧授权。

发音集合使用三个独立的非 owner 身份。读取身份设置
`SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL`，仅授予
`speech_pronunciation_families`、`speech_pronunciation_revisions` 与
`speech_publications` 的 SELECT；草稿写入身份设置
`SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL`，由
`pronunciation_draft_grants(schema, role)` 授予草稿最小权限；原子发布身份设置
`SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL`，由
`pronunciation_publication_grants(schema, role)` 授予草稿表、发布账本、选中 head
和 `speech_catalog_state` 的列级权限。发布身份不得复用带 DELETE 的
`publication_grants`；启动会校验列级权限并拒绝更宽的角色。未配置发布身份时
新的 `publish: true` 请求（无已提交回执）返回 422
`pronunciation_publication_not_supported` 且不写入；已提交发布的重放仍以
200 从命令回执恢复，`publish: false` 仍可写草稿。只配置发布身份时草稿写入也可
用，因为其授权是草稿 writer 的超集。

asr/tts 的原子创建还需要独立的 admission 角色：先应用当前迁移，再用
`public_job_admission_grants(schema, role)` 授予列级读写（无 DELETE），设置
`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`，并确保它与
`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 指向同一数据库和服务器；启动会比对
`current_database()` 与服务器地址，不同则拒绝启动。只配 writer 不配 admission 时，
创建返回 `503 job_unavailable`，读取/取消仍可用。

升级前必须确认已发布 binding 带有 `execution_defaults`。不要自动回填；先运行只读预检
`psql "$SPEECHNEXUS_DATABASE_URL" -f scripts/dev-env/check-public-job-execution-defaults.sql`，
对列出的 binding 重新发布并补齐对应任务的执行默认值；旧 binding 会让 asr/tts 创建
返回 `422 job_binding_incomplete`。

自定义声音 TTS 的内部命令在 `TtsEffectiveParameters` 追加可选 `reference_text`。该字段是
前向兼容：**只有 custom 命令包含它**，builtin 命令与旧版逐字一致，旧网关仍可处理 builtin。
未升级的网关对 custom 命令抛基类 `InvalidExecutionRequestError`；worker 只把
`FileCommandInvalidError` 当作确定性未外发拒绝，因此基类异常走未知路径并落
`ENGINE_TRANSPORT_UNKNOWN`，不会静默回退 builtin 或合成错误音频。部署必须先升级所有
execution gateway，再升级 backend/worker；回退时先停用自定义声音 TTS 入口或回退 backend，
不得保留“新 backend + 旧 gateway”的组合。

Release preflight C9 checks only the parsed Compose service set and the presence
of the gateway's private ASR/TTS configuration keys. It does not start these
containers; runtime and business evidence remain separate.

## Binding disable worker

See [the optional worker runbook](../docs/operations/compose-minimal.md#12-按需启用-binding-停用-worker)
for separate scanner and settlement identities, migration, grants and rollout.
The worker shares the gateway network namespace and calls its loopback address;
this is a single-host Compose topology, not a multi-node transport design. A
`disabled` response means selection is blocked and cleanup remains pending or
unknown; it does not establish that the worker has run, an engine stopped, or
capacity was released. The base topology does not start this worker.
