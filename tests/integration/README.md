# Integration tests

Use only project-isolated databases and synthetic data. Never point these tests
at shared or production services: the test administrator needs namespace/role
creation rights; these are not runtime credentials.

The two suites exercise different assurance boundaries:

- `test_cluster_coordination_schema.py`: 53 historical PG-only checks apply
  `tests/fixtures/coordination/legacy_postgresql.sql`. They preserve the original
  database-enforced direct-SQL negatives and the trigger security fixes.
- `test_coordination_transactions.py`: one shared controlled-transaction oracle
  applies the active logical `migrations/0001_cluster_coordination_draft.sql` on PG and
  MySQL/InnoDB. Its sole type mapping is TEXT to LONGTEXT for MySQL, including
  complete canonical requests that can legitimately exceed 64 KiB.
  No business procedures, triggers, GiST, partial indexes or
  `SKIP LOCKED` are used. Both engines execute the same application rules.

The historical PG suite is transitional. [ADR 0007](../../docs/decisions/0007-legacy-coordination-retirement.md)
defines its exit conditions: migrate valid invariants into current-path tests,
then remove the legacy CI gate, SQL, tests and obsolete design from the working
tree. Git history preserves provenance; the shared transaction gate remains.

Each test creates a unique namespace, installs the DDL, and drops it afterwards.
Permission checks create fresh runtime roles/users and remove them after testing.
PG uses a schema in the explicitly configured test database; MySQL creates a
separate database on the explicitly configured isolated server. Prototype DDL
remains outside the product packages and is not shipped in application wheels.

`test_production_jobs.py` independently exercises the production Job/outbox
unit: PG uses its packaged Alembic migration and actual runtime adapter; InnoDB
uses the same explicit SQL and domain replay decisions through test-only driver
plumbing, with the column collation mapped from PG `C` to `utf8mb4_0900_bin`.
Both configured engines are mandatory for the shared behavior suite. It proves
atomicity, complete immutable identity conflicts, observed unique-key contention,
minimum column grants and read-only unknown recovery. PG-only transport/migration
tests additionally terminate a real session or simulate a lost reply after an
actual commit; the latter is not packet-loss or HA qualification. See the
[production unit and deployment instructions](../../docs/architecture/production-jobs.md).

`test_coordination_foundation.py` verifies the packaged `0002_coordination`
storage baseline, core foreign keys, bounds and minimum A/B role permissions.
PG applies actual migrations; InnoDB applies the same product DDL with only
binary-collation and TEXT-to-LONGTEXT mappings. PG-only upgrade tests verify
transactional DDL rollback, retained Job/outbox rows and grants, repeat upgrades,
and observed advisory-lock waits in two separate migration processes. Synthetic
admin fixture rows do not qualify scheduling/admission/dispatch or cleanup
ordering; those production transactions remain their owners' work.

`test_production_owners.py` qualifies owner acquire/renew/activate and pool
queries using the actual PG adapter and the same product SQL/domain rules on
InnoDB. It observes both scheduler lock waits, then checks a single new epoch;
renew/activate must reject a lease that expires while blocked. Barrier rows are
synthetic owner fixture facts, not proof of real gateway fencing. PG-only tests
exercise pre-commit termination and a synthetic lost reply after actual commit,
with read-only current-authority recovery and no automatic new token.

```bash
SPEECHNEXUS_TEST_DATABASE_URL=postgresql://speechnexus@127.0.0.1:55432/speechnexus_test \
SPEECHNEXUS_TEST_MYSQL_URL=mysql://root:speechnexus_test@127.0.0.1:55434/ \
  make coordination-test
```

This target fails immediately unless **both** DSNs are set, and
`make integration-test` additionally requires explicit isolated MinIO endpoint/test keys (see below). The shared transaction suite
also reports a failure, not a skip, when exactly one engine is configured:
one engine alone is never dual-database evidence. A normal pytest run with
neither DSN skips the integration cases; a skip is not qualification evidence.
Run the historical PG suite alone with
`uv run pytest -m integration tests/integration/test_cluster_coordination_schema.py -q`
when no MySQL instance is available. CI provisions disposable PG 16.15 and
MySQL 8.4.8 services and supplies both DSNs for its integration step.

On this host the isolated MySQL instance is a project-managed container, started
like the other development services rather than by hand:

```bash
./scripts/dev-env/mysql-service.sh          # provision and start; idempotent
./scripts/dev-env/mysql-service.sh status   # readiness probe
./scripts/dev-env/mysql-service.sh stop
```

`make dev-bootstrap` and `make dev-up` install and start it, and the test gates
call the same script: `make integration-test` and `make coordination-test`
start the local container while `SPEECHNEXUS_TEST_MYSQL_URL` points at it and
stay out of the way for any other isolated server, while `make verify` always
ensures the local one because it also validates this host's environment. The
dual-database gate therefore never needs a manual prerequisite. The container is
pinned to
`mysql@sha256:2952e3be7807f06fc18de50b3ea1a632d5c70d63482ff7d7376fe3aa8999babf`
(MySQL 8.4.8, InnoDB, `utf8mb4_0900_bin`), binds `127.0.0.1:55434` only, caps
itself at 2 GiB and 2 CPUs, and keeps data in `.runtime/data/mysql`, owned by the
container's `mysql` account after initialisation. It is not a user systemd unit:
this host's user manager has no docker socket access, so docker's
`unless-stopped` restart policy restores it with the daemon instead. The root
password is a synthetic local-test credential in `.runtime/config/mysql.env`;
it never names a shared or production server.

The measured local environment is Linux ARM64, PostgreSQL 16.15 / psycopg 3.3.6,
and MySQL 8.4.8 / PyMySQL 1.2.3, with Read Committed and UTC. Driver versions are
locked in `uv.lock`. The MySQL image used locally has digest
`sha256:2952e3be7807f06fc18de50b3ea1a632d5c70d63482ff7d7376fe3aa8999babf`.

The shared suite covers atomic complete ready recipes and job/outbox creation,
quota/physical dimensions, owner expiry/takeover, conditional claims, submit
identity, unknown execution quarantine, evidence-before-release and interval
accounting, text-only stages and large Unicode request round trips.
Private command erasure preserves coordination evidence and uses a synthetic
trusted deletion permit; it does not qualify the full deletion workflow or its transport.
Deterministic interleavings observe `pg_blocking_pids` or InnoDB
`performance_schema.data_lock_waits` before releasing the blocker; they do not
infer concurrency from sleep or from two sequential calls. Runtime-role tests
exercise the successful owner-separated workflow as well as forbidden writes.

These are independent database connections within one test process, not a
running multi-process cluster. Synthetic stop observations do not prove engine
termination. Pre-commit exceptions and discarded post-commit replies do not
qualify real process kill, packet loss during commit, or database failover.
Production use cases, complete peak/steady lifecycle, all quota dimensions,
physical multi-host/HA, real engines and load remain explicit mandatory work in
the [refactoring plan](../../docs/plans/2026-10-23-cluster-coordination-refactor.md)
and [application handoff](../../docs/architecture/coordination-transactions.md).
Cluster support is mandatory; the current single-node environment does not
relax any of those acceptance requirements. MySQL coordination qualification
does not add full product MySQL support.

## Asset publication

`test_assets.py` exercises the PG product adapter and actual versioned MinIO with synthetic UTF-8 content. It applies migration head `0003_assets`, fresh minimum runtime grants, unique disposable buckets, uncertain write/commit replies, same-key contention, verified Range and reference/retention cleanup. It is PG-only product coverage; InnoDB continues to qualify the shared coordination paths. When database DSNs are configured, missing object-storage inputs fail rather than skip.

```bash
./scripts/dev-env/minio-test-service.sh
export SPEECHNEXUS_TEST_MINIO_ENDPOINT=http://127.0.0.1:59000
export SPEECHNEXUS_TEST_MINIO_ACCESS_KEY=speechnexus_test
export SPEECHNEXUS_TEST_MINIO_SECRET_KEY=synthetic_asset_test_secret
# Set both isolated database DSNs above.
make integration-test
```

The helper provisions an isolated synthetic service only; tests enable versioning on their own buckets. The product never creates a bucket or changes its policy. See [production assets](../../docs/architecture/production-assets.md) for recovery limits, explicit TTL configuration, minimum-role boundaries and the subsequent HTTP/maintenance integration.

`test_admission_command_storage.py` covers the `0007_admission_commands` storage foundation in both databases: separate private business/admission records, long bodies, complete tombstones, minimum roles, actual operation-key/row-lock contention and atomic rollback with Job/outbox. PostgreSQL also verifies migration failure rollback and unchanged legacy rows/ACLs. This is storage evidence; complete `admit_recipe`, retry/deletion authorization and actual scheduler consumers remain pending. See [admission storage](../../docs/architecture/production-admission-storage.md).

`test_runtime_delivery.py` runs the same private delivery SQL under dedicated
minimum roles on PG and InnoDB: competing claims, stale holder/version/expiry,
actual outbox lock waits, original identity checks, legacy query-only recovery,
atomic ACK and denied command/budget/history writes. `test_runtime_delivery_commit_loss.py`
loses replies after real commits at claim/start/ACK, without automatic transaction
reexecution. `test_runtime_delivery_migration.py` exercises actual PG 0007→0008,
DDL failure rollback, untouched old rows/ACLs and repeat upgrade.

`test_runtime_delivery_tls.py` uses actual loopback TLS with native B/SQLite,
including accepted POST reply loss followed by GET only, legacy 404 and native
STOP-before-unseen-PREPARE. These shared cases run on both databases without skips.
PG-only subprocess cases exercise the installed worker, SIGTERM and owner-DSN
refusal; the worker does not require S3 or call remote STOP on exit. Daemon and
plans are synthetic; no GPU, deployment TLS, node observation loop or full
scheduler qualification is implied. See [runtime delivery](../../docs/architecture/production-runtime-delivery.md).

`test_admission_recovery_pressure.py` commits a real admission and loses the
commit reply, then prevents the recovery query from reading its original facts:
a genuinely exhausted one-slot PG pool or a native InnoDB metadata-lock timeout.
It verifies unknown/original-query recovery, one admission SQL invocation, and
unchanged command/lease/Job/outbox/quota/budget snapshots before recovering the
same original identity. Commit-reply loss is injected after the actual commit;
read pressure is native. InnoDB uses the existing test-only DBAPI oracle around
the unchanged production SQL, not a product MySQL adapter or engine qualification.

`test_owner_activation_quarantine.py` runs real PG/InnoDB owner and lease ports
with minimum roles. A persisted gateway unknown marker alone cannot activate an
owner: the A quarantine transaction must first commit its lease marker, resource,
slot and quota projections, and threshold disposition. Tests observe actual
host/pool lock waits while activation races quarantine commit or a forced rollback
after the host write; verify old-owner fencing; and preserve total occupancy and
gateway history. Gateway observations are synthetic persisted fixtures, not
physical stop, GPU or deployment-fencing qualification.

`test_profile_drafts.py` covers the A02 process-local draft configuration port on
real PG and the same SQL with a test-only InnoDB DBAPI oracle. Restricted fixture
roles verify original-key replay/conflict, serialized family CAS, immutable reads,
post-write rollback, commit-reply loss and native read pressure. PG verifies
append-only migration 0009 and preservation on failed upgrade/refused downgrade.
This does not qualify profile publication, HTTP routes, default selection or
speech engines. See [profile drafts](../../docs/architecture/profile-drafts.md).


公开 profile 草稿 HTTP 的原生 TypeScript 消费回归也属于完整集成集合，不能因缺少
Node 或 JS 依赖跳过。本机先按 `.nvmrc` / `packageManager` 准备工具并执行锁定安装。
CI 使用 `integration-shards.py requirements` 与测试相同的 SHA256 分片算法，仅给
承载该用例的分片准备锁定 Node/pnpm 和 `@speechnexus/contracts` 生产依赖；其余分片
保留 Python 工具链。该前置选择不改变测试分配、全集报告或失败聚合条件。
