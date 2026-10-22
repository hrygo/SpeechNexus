# Integration tests

Integration tests use the isolated PostgreSQL and object-storage instances
created by `make dev-bootstrap`. They must not connect to the shared services on
this host. Mark these tests with `@pytest.mark.integration`; they are opt-in
until the P1 persistence adapters exist.
