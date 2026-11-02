"""Dedicated worker roles must not inherit server capabilities, even without SET."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.integration.test_production_leases import LeaseDatabase
from tests.integration.test_zero_dispatch_release_pg import lease as lease
from tests.integration.test_zero_dispatch_worker_pg import settings, settlement_role

from speechnexus_backend import binding_disable_worker as worker
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool

pytestmark = pytest.mark.integration
SERVER_ROLES = ("pg_read_server_files", "pg_write_server_files", "pg_execute_server_program")


@pytest.fixture
def principals(lease: LeaseDatabase) -> Iterator[tuple[str, SpeechPool, str, SpeechPool]]:
    db = lease.foundation.database
    scanner = db.schema + "_scan"
    group = db.schema + "_scan_acl"
    db.query(f"CREATE ROLE \"{scanner}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    db.query(f'CREATE ROLE "{group}" NOLOGIN')
    scan_pool = speech_pool(
        make_conninfo(db.url, user=scanner, password="synthetic_settlement_test"), schema=db.schema
    )
    try:
        db.query(f'GRANT "{group}" TO "{scanner}" WITH INHERIT TRUE, SET FALSE, ADMIN FALSE')
        for statement in disable_coordinator_grants(db.schema, group):
            db.query(statement)
        scan_pool.open()
        with settlement_role(lease, inherit_only=True) as (settlement, settlement_pool):
            yield scanner, scan_pool, settlement, settlement_pool
    finally:
        scan_pool.close()
        db.query(f'DROP OWNED BY "{scanner}"')
        db.query(f'DROP ROLE "{scanner}"')
        db.query(f'DROP OWNED BY "{group}"')
        db.query(f'DROP ROLE "{group}"')


@contextmanager
def capability_grant(
    lease: LeaseDatabase, role: str, capability: str, *, indirect: bool, inherit: bool = True
) -> Iterator[None]:
    db = lease.foundation.database
    bridge = db.schema + "_bridge"
    if indirect:
        db.query(f'CREATE ROLE "{bridge}" NOLOGIN')
    try:
        parent = bridge if indirect else role
        db.query(f'GRANT "{capability}" TO "{parent}" WITH INHERIT TRUE, SET FALSE, ADMIN FALSE')
        if indirect:
            db.query(
                f'GRANT "{bridge}" TO "{role}" '
                f"WITH INHERIT {'TRUE' if inherit else 'FALSE'}, SET FALSE, ADMIN FALSE"
            )
        yield
    finally:
        db.query(f'REVOKE "{capability}" FROM "{parent}"')
        if indirect:
            db.query(f'DROP ROLE "{bridge}"')


def native_authority(pool: SpeechPool, capability: str) -> dict[str, object]:
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT pg_catalog.pg_has_role(current_user,%s,'USAGE') AS inherited, "
            "pg_catalog.pg_has_role(current_user,%s,'SET') AS switchable, "
            "pg_catalog.pg_has_role(current_user,%s,'MEMBER WITH ADMIN OPTION') AS admin",
            (capability,) * 3,
        ).fetchone()
        assert row is not None
        return row


def server_operation(pool: SpeechPool, capability: str, path: str) -> None:
    with pool.connection() as connection:
        if capability == "pg_read_server_files":
            connection.execute("CREATE TEMP TABLE synthetic_marker (marker text) ON COMMIT DROP")
            connection.execute(f"COPY synthetic_marker FROM '{path}'")
            assert connection.execute("SELECT marker FROM synthetic_marker").fetchone() == {
                "marker": "synthetic_permission_marker"
            }
        elif capability == "pg_write_server_files":
            connection.execute(f"COPY (SELECT 'synthetic_permission_marker') TO '{path}.out'")
        else:
            # This command only consumes synthetic COPY output inside the isolated server.
            cursor = connection.execute(
                "COPY (SELECT 'synthetic_permission_marker') TO PROGRAM 'cat >/dev/null'"
            )
            assert cursor.statusmessage == "COPY 1"


@pytest.mark.parametrize("principal", ["scanner", "settlement"])
@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "recursive"])
@pytest.mark.parametrize("capability", SERVER_ROLES)
async def test_effective_server_capability_rejected_before_gateway(
    lease: LeaseDatabase,
    principals: tuple[str, SpeechPool, str, SpeechPool],
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
    indirect: bool,
    capability: str,
) -> None:
    db = lease.foundation.database
    scanner, scan_pool, settlement, settlement_pool = principals
    role, pool = (scanner, scan_pool) if principal == "scanner" else (settlement, settlement_pool)
    verify = (
        worker._verify_principal if principal == "scanner" else worker._verify_settlement_principal
    )
    label = "Disable worker" if principal == "scanner" else "Settlement worker"
    # Schema is a fixture-generated identifier; no business paths or content are used.
    path = f"/tmp/{db.schema}_del415_marker"
    db.query(f"COPY (SELECT 'synthetic_permission_marker') TO '{path}'")

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Inherited server capability reached gateway construction")

    monkeypatch.setattr(worker, "GatewayExecutionClient", unexpected_gateway)
    try:
        verify(pool, db.schema)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            server_operation(pool, capability, path)
        with capability_grant(lease, role, capability, indirect=indirect):
            assert native_authority(pool, capability) == {
                "inherited": True,
                "switchable": False,
                "admin": False,
            }
            server_operation(pool, capability, path)
            if capability == "pg_write_server_files":
                assert db.query(f"SELECT pg_read_file('{path}.out') AS marker") == [
                    {"marker": "synthetic_permission_marker\n"}
                ]
            # Check the full composition root first: an old candidate hits the sentinel.
            with pytest.raises(ValueError, match=label):
                await worker.run(settings(lease, scanner, settlement))
            with pytest.raises(ValueError, match=label):
                verify(pool, db.schema)
    finally:
        # Remove only the two generated synthetic files in this disposable test server.
        db.query(f"COPY (SELECT '') TO PROGRAM 'rm -f -- {path} {path}.out'")


@pytest.mark.parametrize("principal", ["scanner", "settlement"])
@pytest.mark.parametrize("capability", SERVER_ROLES)
async def test_blocked_server_inheritance_and_ordinary_acl_group_can_start(
    lease: LeaseDatabase,
    principals: tuple[str, SpeechPool, str, SpeechPool],
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
    capability: str,
) -> None:
    scanner, scan_pool, settlement, settlement_pool = principals
    role, pool = (scanner, scan_pool) if principal == "scanner" else (settlement, settlement_pool)

    class ReachedGatewayError(Exception):
        pass

    def reached_gateway(**kwargs: object) -> None:
        raise ReachedGatewayError

    monkeypatch.setattr(worker, "GatewayExecutionClient", reached_gateway)
    with capability_grant(lease, role, capability, indirect=True, inherit=False):
        assert native_authority(pool, capability) == {
            "inherited": False,
            "switchable": False,
            "admin": False,
        }
        with pytest.raises(ReachedGatewayError):
            await worker.run(settings(lease, scanner, settlement))


@pytest.mark.parametrize("principal", ["scanner", "settlement"])
async def test_all_available_predefined_capabilities_are_rejected(
    lease: LeaseDatabase,
    principals: tuple[str, SpeechPool, str, SpeechPool],
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
) -> None:
    """Also exercise monitoring, global data, signaling, checkpoint and reserved slots."""
    db = lease.foundation.database
    scanner, scan_pool, settlement, settlement_pool = principals
    role, pool = (scanner, scan_pool) if principal == "scanner" else (settlement, settlement_pool)
    label = "Disable worker" if principal == "scanner" else "Settlement worker"

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Predefined capability reached gateway construction")

    monkeypatch.setattr(worker, "GatewayExecutionClient", unexpected_gateway)
    capabilities = db.query(
        "SELECT rolname FROM pg_catalog.pg_roles WHERE left(rolname,3)='pg_' "
        "AND rolname <> 'pg_database_owner' ORDER BY rolname"
    )
    assert {row["rolname"] for row in capabilities} >= set(SERVER_ROLES)
    for row in capabilities:
        capability = str(row["rolname"])
        with capability_grant(lease, role, capability, indirect=True):
            assert native_authority(pool, capability) == {
                "inherited": True,
                "switchable": False,
                "admin": False,
            }
            with pytest.raises(ValueError, match=label + ".*predefined capabilities"):
                await worker.run(settings(lease, scanner, settlement))


@pytest.mark.parametrize("principal", ["scanner", "settlement"])
async def test_implicit_database_owner_capability_is_rejected(
    lease: LeaseDatabase,
    principals: tuple[str, SpeechPool, str, SpeechPool],
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
) -> None:
    db = lease.foundation.database
    scanner, scan_pool, settlement, settlement_pool = principals
    role, pool = (scanner, scan_pool) if principal == "scanner" else (settlement, settlement_pool)
    database = db.query(
        "SELECT datname, pg_catalog.pg_get_userbyid(datdba) AS owner "
        "FROM pg_catalog.pg_database WHERE datname=current_database()"
    )[0]
    name = psycopg.sql.Identifier(str(database["datname"])).as_string()
    owner = psycopg.sql.Identifier(str(database["owner"])).as_string()

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Implicit database owner reached gateway construction")

    monkeypatch.setattr(worker, "GatewayExecutionClient", unexpected_gateway)
    try:
        db.query(f'ALTER DATABASE {name} OWNER TO "{role}"')
        assert native_authority(pool, "pg_database_owner")["inherited"] is True
        with pytest.raises(ValueError, match="predefined capabilities"):
            await worker.run(settings(lease, scanner, settlement))
    finally:
        db.query(f"ALTER DATABASE {name} OWNER TO {owner}")


@pytest.mark.parametrize("principal", ["scanner", "settlement"])
@pytest.mark.parametrize(
    "failure",
    [psycopg.OperationalError, psycopg.errors.QueryCanceled, psycopg.errors.InsufficientPrivilege],
)
async def test_server_authority_query_failure_refuses_startup(
    lease: LeaseDatabase,
    principals: tuple[str, SpeechPool, str, SpeechPool],
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
    failure: type[psycopg.Error],
) -> None:
    scanner, _, settlement, _ = principals
    role = scanner if principal == "scanner" else settlement
    original_execute = psycopg.Connection.execute
    failures = 0

    def unavailable(connection: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failures
        if connection.info.user == role and "AS predefined" in str(query):
            failures += 1
            raise failure("synthetic predefined-role evidence failure")
        return original_execute(connection, query, *args, **kwargs)

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Unavailable predefined-role evidence reached gateway construction")

    monkeypatch.setattr(psycopg.Connection, "execute", unavailable)
    monkeypatch.setattr(worker, "GatewayExecutionClient", unexpected_gateway)
    with pytest.raises(failure):
        await worker.run(settings(lease, scanner, settlement))
    assert failures == 1
