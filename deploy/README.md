# Deployment bootstrap

This directory currently defines a local, single-node Compose topology for validating
the repository bootstrap. Following
[ADR 0004](../docs/decisions/0004-cluster-ready-architecture-and-single-node-mvp.md),
the architecture is cluster-ready while the MVP deployment runs as a
single-node homomorphic topology (`Node=1, Replica=1`). It is not a production
capacity or multi-node high-availability qualification. Release images must be
locked by digest before publication; the bootstrap uses explicit version tags
for readability.

## Deployment units and platform boundaries

The product targets distributed deployment. See the
[deployment unit and platform plan (Chinese)](../docs/architecture/deployment-units.md)
and [ADR 0011 (Chinese)](../docs/decisions/0011-distributed-deployment-and-platform-boundaries.md)
for service placement, platform requirements, existing entry points and acceptance gaps.
Business/control services and the workbench can be placed independently of compute
hosts. The selected CUDA engine and node execution stack targets Linux; the current
CPU audio inspection sandbox also needs Linux. Native macOS/Linux application support,
container execution and multi-host operation require separate qualification.

The current Compose file starts PostgreSQL, MinIO, Speech API, Gateway and two static
Web applications. The `workbench` container serves the frontend; it does not start the
independent Workbench API. Its `/v2/workbench/` proxy currently points to `api:8000`.
Independent Workbench, Controller, complete background-role and model deployment
assembly remain work items in the plan. Setting `SPEECHNEXUS_PROCESS_ROLE` identifies
a process; it does not switch the backend command to a different service factory.

## Local Compose

```bash
cp deploy/compose.env.example deploy/compose.env
docker compose \
  --env-file deploy/compose.env \
  -f deploy/compose.yaml \
  up --build
```

The Compose development topology binds all published ports to loopback:

- AdminUI: `http://127.0.0.1:8080`
- Workbench: `http://127.0.0.1:8081`
- Speech API: `http://127.0.0.1:8000`
- Execution gateway: `http://127.0.0.1:8010`
- PostgreSQL: `127.0.0.1:55432`
- MinIO: `127.0.0.1:59000`

MinIO is built from checked official release DEB/license bytes rather than the unavailable community image tag. Its runtime uses UID/GID 10001 and disables the browser/update service; API health uses the included Python runtime. Existing data volumes need an ownership review before switching images; no startup command changes existing data ownership. Bucket versioning and dedicated product credentials are separately provisioned before enabling the [asset publication ports](../docs/architecture/production-assets.md). The bootstrap does not create a product bucket. Asset HTTP routes require paired explicit object keys and a non-owner runtime DB role; without the keys they report unavailable.

The repository's systemd development services use the same PostgreSQL and
MinIO host ports. Stop one topology before starting the other.

The Python and Web build contexts include the shared contracts workspace;
`.dockerignore` excludes runtime data, credentials, local dependencies and caches.
When a build runs behind a deployment/session CA, supply its trusted combined
CA bundle as BuildKit secret `proxy_ca`. The optional secret is mounted only in
networked dependency-install steps and is not copied into the final images.
Do not disable TLS verification. The first explicit owner-only Job migration and
minimum runtime grants are documented in
[production jobs](../docs/architecture/production-jobs.md); normal API startup
does not run migrations or install their optional dependencies.

## Configuration authority

Runtime configuration is validated by `pydantic-settings` in each Python
package. Environment variables are explicit, prefixed with `SPEECHNEXUS_` for
the speech backend or `SPEECHNEXUS_GATEWAY_` for the execution gateway, and
contain no hidden fallback service endpoints. Every process declares its node,
role, and instance identity so leases, fencing tokens, and session owners remain
cluster-ready from day one.

| Variable                                     | Process | Meaning                                                                                                                  |
| -------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------ |
| `SPEECHNEXUS_PROCESS_ROLE`                   | Backend | Explicit process role (`api`, `realtime-gateway`, `scheduler`, `job-worker`, `workbench-api`, `workbench-export-worker`) |
| `SPEECHNEXUS_NODE_ID`                        | Backend | Physical / virtual host node identity for parent physical budget and controller affinity                                 |
| `SPEECHNEXUS_INSTANCE_ID`                    | Backend | Unique process instance identity for leases, fencing, and session ownership                                              |
| `SPEECHNEXUS_DATABASE_URL`                   | Backend | Authoritative PostgreSQL DSN                                                                                             |
| `SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT`        | Backend | S3-compatible private object endpoint                                                                                    |
| `SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL` | Backend | Dependency probe URL                                                                                                     |
| `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`          | Backend | Deployment object bucket                                                                                                 |
| `SPEECHNEXUS_ASSET_DEFAULT_TTL_SECONDS`      | Backend/asset worker | Asset default retention in seconds: 604800 (7 days); explicit upload TTL overrides it |
| `SPEECHNEXUS_GATEWAY_NODE_ID`                | Gateway | Host node identity associated with the execution gateway                                                                 |
| `SPEECHNEXUS_GATEWAY_GATEWAY_ID`             | Gateway | Unique execution gateway identity for submit-intent fencing                                                              |
| `SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL`   | Gateway | PostgreSQL control-plane DSN                                                                                             |

Production entry points, Host / Origin policy, shared service credentials,
backup and capacity qualification remain deployment decisions. Asset retention
defaults to 7 days and can be overridden by deployment configuration. The
initial product has no login, user directory, RBAC, or tenant isolation.

## Asset API and retention worker

The [asset configuration and recovery guide](../docs/architecture/production-assets.md)
describes paired object keys, pre-created versioned buckets, non-owner runtime
grants, the 7-day default/explicit TTL policy and Host/Origin guards. Defaults are
resolved only when creating a new Asset; a changed default never extends or
shortens existing records or same-key replays. The public Asset API
shares the backend process; it has no enabled object credentials in the bootstrap.
A separate `speechnexus-asset-maintain` entry point uses `job-worker` plus explicit
node/instance identities. Provision its bucket/role before running it; it never
runs DDL or creates storage policies. It must continue running for retention and
unknown-original polling. The API's one startup pass does not replace that worker.

Browser callers use the same-origin proxy and explicitly approved frontend
Origin. This release does not claim complete backup restoration, production
object IAM/outbound restrictions, arbitrary audio inspection or G1 qualification.

预设草稿 HTTP writer 默认关闭。仅在操作者已应用 `0009_profile_drafts` 并独立配置最小非 owner 角色后，设置 `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`；不得复用 Compose bootstrap owner。启动仅验证权限并管理独立连接池，不执行迁移或 grant。原请求/原键重放、固定读取和限额见 [profile drafts](../docs/architecture/profile-drafts.md#http-runtime-and-controlled-writer)。此配置不发布或激活预设。

绑定草稿选择一个显式模式：`SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` 使用旧三表角色；
`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` 使用预先迁移到0011的四表最小角色，
启用原子默认值声明与固定快照GET。两者互斥、默认均为空；没有通用数据库身份回退，
也不会自动扩旧角色权限。配置示例仅传递操作者已配置的凭据，不执行grant、DDL或部署。
模式、错误恢复与实际入口见 [producer HTTP](../docs/architecture/binding-defaults-producer.md)。

公开 Job 提交与读取默认关闭。应用 `0017_job_submissions` 并为非 owner 角色授予
`speech_jobs` / `speech_job_events` / `speech_job_submissions` 的最小写入权限及发布事实只读权限后，
设置 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`；启动只验证权限，不执行迁移或 grant。
未配置时 `/v1/nexus/jobs` 返回既有错误包封的 `503 job_unavailable`，不会伪造 pending Job。
