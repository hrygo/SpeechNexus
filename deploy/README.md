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
`node-controller`, `runtime-delivery-worker`, `job-dispatch-worker` and
`asset-maintain`. Their absence is a scope statement, not an assertion that
those capabilities work elsewhere. They are excluded because controller
lifecycle, runtime delivery, asynchronous Jobs (G5 / issue #18) and retention
maintenance are not on the synchronous request path or are not assembled.

Additional profiles are opt-in:

- `migration`: one-shot Alembic migration with the separate migration DSN;
- `engine-registration`: one-shot `speechnexus-register-engine` invocation;
- `web`: the static Admin and Workbench frontends.

The `workbench` container serves only the frontend; it does not start the
independent Workbench API. Setting `SPEECHNEXUS_PROCESS_ROLE` identifies a
process; it does not switch the backend command to another service factory.

## Current capability limits

`/healthz` and `/readyz` are startup/dependency probes, not business acceptance:

- public synchronous ASR is still blocked by the missing production terminal-state
  writer tracked as **DEL-109**; a live request can return `504 operation_timeout`
  even when the gateway staged a result;
- public TTS remains blocked by **DEL-122**; this Compose only prepares the
  gateway's private file-TTS references;
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
- registration and migration use separate one-shot DSNs;
- file-ASR/file-TTS credentials and configuration are always supplied together;
- private files must be owner-only regular files under the mounted private path.

Production entry points, Host/Origin policy, shared service credentials, backup,
object IAM and capacity qualification remain deployment decisions. The initial
product has no login, user directory, RBAC or tenant isolation.

## Optional writers and background units

Asset HTTP routes can use the local object credentials, but production must
replace them with narrowly scoped credentials. The
[asset configuration and recovery guide](../docs/architecture/production-assets.md)
describes paired object keys, pre-created versioned buckets, non-owner runtime
grants, the 7-day default/explicit TTL policy and Host/Origin guards. The
`speechnexus-asset-maintain` entry point is not started here and must be
provisioned separately before retention/unknown-original polling is claimed.

The public Job dispatcher is registered separately as
`speechnexus-job-dispatch-worker`. It requires explicit worker identity, a
non-owner PostgreSQL role, the execution-gateway origin/credential and every
batch/lease/poll/timeout bound in
[production jobs](../docs/architecture/production-jobs.md#worker-入口与运行配置del-98).
It is not part of this minimal surface.

预设草稿 HTTP writer 默认关闭。仅在操作者已应用 `0009_profile_drafts` 并独立配置最小非 owner 角色后，设置 `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`；不得复用 Compose bootstrap owner。启动仅验证权限并管理独立连接池，不执行迁移或 grant。原请求/原键重放、固定读取和限额见 [profile drafts](../docs/architecture/profile-drafts.md#http-runtime-and-controlled-writer)。此配置不发布或激活预设。

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

Release preflight C9 checks only the parsed Compose service set and the presence
of the gateway's private ASR/TTS configuration keys. It does not start these
containers; runtime and business evidence remain separate.
