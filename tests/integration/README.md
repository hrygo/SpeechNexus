# Integration tests

Integration tests use the isolated PostgreSQL and object-storage instances
created by `make dev-bootstrap`. They must not connect to the shared services on
this host. Mark these tests with `@pytest.mark.integration`; normal test runs
skip them unless an explicit test database is configured.

```bash
SPEECHNEXUS_TEST_DATABASE_URL=postgresql://speechnexus@127.0.0.1:55432/speechnexus_test \
  make integration-test
```

CI provisions a disposable PostgreSQL 16 service and runs the same target with
its own DSN. Each test schema applies `migrations/0001_cluster_coordination_draft.sql`
and is dropped afterwards, so the checks exercise PostgreSQL behavior rather
than matching DDL text. The migration draft lives outside the Python package so
application wheels do not ship DDL resources.
