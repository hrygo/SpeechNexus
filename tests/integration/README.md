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

Each test creates a unique namespace, installs the DDL, and drops it afterwards.
Permission checks create fresh runtime roles/users and remove them after testing.
PG uses a schema in the explicitly configured test database; MySQL creates a
separate database on the explicitly configured isolated server. DDL remains
outside the product packages and is not shipped in application wheels.

```bash
SPEECHNEXUS_TEST_DATABASE_URL=postgresql://speechnexus@127.0.0.1:55432/speechnexus_test \
SPEECHNEXUS_TEST_MYSQL_URL=mysql://root:speechnexus_test@127.0.0.1:55434/ \
  make coordination-test
```

This target fails immediately unless **both** DSNs are set, and
`make integration-test` requires the same pair. The shared transaction suite
also reports a failure, not a skip, when exactly one engine is configured:
one engine alone is never dual-database evidence. A normal pytest run with
neither DSN skips the integration cases; a skip is not qualification evidence.
Run the historical PG suite alone with
`uv run pytest -m integration tests/integration/test_cluster_coordination_schema.py -q`
when no MySQL instance is available. CI provisions disposable PG 16.15 and
MySQL 8.4.8 services and supplies both DSNs for its integration step.

An optional isolated MySQL test instance can be started as follows. The password
is a synthetic local-test credential. No host data directory is mounted.

```bash
docker run --rm --detach --name speechnexus-coordination-mysql \
  --publish 127.0.0.1:55434:3306 \
  --env MYSQL_ROOT_PASSWORD=speechnexus_test --env MYSQL_ROOT_HOST=% \
  mysql:8.4.8 --default-storage-engine=InnoDB \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_0900_bin
docker exec speechnexus-coordination-mysql \
  mysqladmin ping --host=127.0.0.1 --user=root --password=speechnexus_test
# Run tests after initialization reports ready, then remove the temporary instance:
docker stop speechnexus-coordination-mysql
```

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
