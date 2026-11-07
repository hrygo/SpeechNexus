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

The following production units are deliberately **not** part of this file yet:
`node-controller`, `runtime-delivery-worker`, `job-dispatch-worker`,
`asset-maintain` and `voice-evidence-worker`. Their absence is a scope
statement, not an assertion that those capabilities work elsewhere. They are
excluded because controller lifecycle, runtime delivery, asynchronous Jobs (G5 /
issue #18), retention maintenance and automatic evidence recording (which needs a
recorder DSN plus report object storage) are not on the synchronous request path
or are not assembled here.

Additional profiles are opt-in:

- `migration`: one-shot Alembic migration with the separate migration DSN;
- `engine-registration`: one-shot `speechnexus-register-engine` invocation;
- `web`: the static Admin and Workbench frontends;
- `binding-disable`: explicitly starts the disable worker, which is required to
  process persisted disable intents; it is absent from the base topology.

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
- asynchronous Job submission/dispatch remains blocked by G5 / issue #18;
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

The gateway's private file-ASR/file-TTS object adapter requires a private IP
literal rather than a DNS name, so MinIO receives the fixed Compose-network
address `172.28.0.10`. If `172.28.0.0/24` conflicts with the host, change the
subnet and both private JSON object endpoints together. The examples and
private-file rules are in [deploy/private](private/README.md).

`postgres-data` and `minio-data` are named volumes. `minio-init` creates the
input/result buckets and verifies versioning. It uses the local MinIO root
identity only; a real deployment must provision separately scoped object
credentials and reviewed buckets outside this bootstrap.

## Build and dependency boundary

The Python image is the shared composition for API, gateway, bucket init,
migration and registration. It now includes:

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
`speechnexus-job-dispatch-worker`. It requires explicit worker identity, a
non-owner PostgreSQL role, the execution-gateway origin/credential and every
batch/lease/poll/timeout bound in
[production jobs](../docs/components/production-jobs.md#worker-入口与运行配置del-98).
It is not part of this minimal surface.

预设草稿 HTTP writer 默认关闭。仅在操作者已应用 `0009_profile_drafts` 并独立配置最小非 owner 角色后，设置 `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`；不得复用 Compose bootstrap owner。启动仅验证权限并管理独立连接池，不执行迁移或 grant。原请求/原键重放、固定读取和限额见 [profile drafts](../docs/components/profile-drafts.md#http-runtime-and-controlled-writer)。此配置不发布或激活预设。

绑定草稿选择一个显式模式：`SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` 使用旧三表角色；
`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` 使用预先迁移到 0011 的四表最小角色，
启用原子默认值声明与固定快照 GET。两者互斥、默认均为空；没有通用数据库身份回退，
也不会自动扩旧角色权限。固定 binding GET 另需
`SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL`：一个只读非 owner 角色，可 SELECT
binding 历史、`speech_publications` 与证据表。未配置时 GET 返回
`503 binding_unavailable`，不会把已发布 revision 谎报为 draft。

公开 Job 提交与读取默认关闭。应用 `0017_job_submissions` 并为非 owner 角色授予
`speech_jobs` / `speech_job_events` / `speech_job_submissions` 的最小写入权限及发布事实
只读权限后，设置 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`；启动只验证权限，不执行迁移
或 grant。未配置时 `/v1/nexus/jobs` 返回既有错误包封的 `503 job_unavailable`，不会
伪造 pending Job。

声音发布还需要独立的 publisher 角色：应用 0047 后由 owner 运行
`voice_publication_grants(schema, role)`，设置 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`；
它必须与 Job writer 指向同一数据库和服务器，但不得复用 Job writer 身份。启动按 schema
校验其完整权限面：只允许 `voice_publication_grants` 声明的 14 张表，白名单之外的表级或
列级权限与 schema `CREATE` 一律拒绝；表级与列级分开判定，列级授权不会因表级检查为 false
而漏检。新增同类校验时必须同时补真实角色拒绝用例。启动校验不通过或未配置时发布返回 503。

Artifact reader、binding writer、binding admin reader、public Job writer 与 workbench
runtime 的启动校验遵循同一条权限面规则：配置 schema 内声明表之外不得持有任何表级或列级
权限；可写表拒绝 `DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER`，只读表分别判定表级
`INSERT` 与列级 `INSERT`/`UPDATE`/`REFERENCES`；schema 需要 `USAGE` 且拒绝 `CREATE`。
列级判定一律使用 `has_column_privilege`/`has_any_column_privilege`，不以表级结果为代理；
声明列集之外的列级 `INSERT`/`UPDATE` 同样拒绝，新增列不会自动扩权。拒绝信息点名权限类型
与关系，并给出撤销授权或扩声明表的修复动作。
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
