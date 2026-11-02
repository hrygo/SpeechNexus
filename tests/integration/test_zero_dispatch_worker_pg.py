"""Real worker authority and settlement wiring; no physical stop qualification."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_binding_lifecycle_pg import sandbox as sandbox
from tests.integration.test_disable_composition_root_pg import seed_binding
from tests.integration.test_production_leases import LeaseDatabase
from tests.integration.test_zero_dispatch_release_pg import lease as lease
from tests.integration.test_zero_dispatch_release_pg import no_dispatch_failure

from speechnexus_backend import binding_disable_worker
from speechnexus_backend.binding_disable_worker import (
    BindingDisableWorkerSettings,
    _verify_settlement_principal,
    run,
)
from speechnexus_backend.speech.application.binding_disable_coordinator import (
    BindingDisableCoordinator,
    DisablePassResult,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants,
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.execution_release_schema import release_guard_grants
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool

pytestmark = pytest.mark.integration


@contextmanager
def settlement_role(
    lease: LeaseDatabase, *, inherit_only: bool = False
) -> Iterator[tuple[str, SpeechPool]]:
    db = lease.foundation.database
    role = db.schema + "_settlement"
    group = db.schema + "_inherited"
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    pool = speech_pool(
        make_conninfo(db.url, user=role, password="synthetic_settlement_test"), schema=db.schema
    )
    try:
        if inherit_only:
            db.query(f'CREATE ROLE "{group}" NOLOGIN')
            db.query(f'GRANT "{group}" TO "{role}" WITH INHERIT TRUE, SET FALSE')
        for statement in disable_settlement_grants(db.schema, group if inherit_only else role):
            db.query(statement)
        pool.open()
        yield role, pool
    finally:
        pool.close()
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')
        if inherit_only:
            db.query(f'DROP OWNED BY "{group}"')
            db.query(f'DROP ROLE "{group}"')


@pytest.mark.parametrize(
    "alteration",
    [
        "DROP VIEW execution_release_guards",
        'REVOKE SELECT (submit_intent_id) ON execution_release_guards FROM "{role}"',
        'REVOKE SELECT (no_dispatch_metadata_absent) ON execution_release_guards FROM "{role}"',
        'GRANT SELECT ON execution_release_guards TO "{role}"',
        'GRANT UPDATE (submit_intent_id) ON execution_release_guards TO "{role}"',
        'GRANT INSERT (submit_intent_id) ON execution_release_guards TO "{role}"',
        'GRANT REFERENCES (submit_intent_id) ON execution_release_guards TO "{role}"',
        'GRANT DELETE ON execution_release_guards TO "{role}"',
        'GRANT TRIGGER ON execution_release_guards TO "{role}"',
        'GRANT SELECT (engine_request_id) ON submit_intents TO "{role}"',
        'GRANT SELECT (request_document) ON submit_intents TO "{role}"',
        'GRANT SELECT (staging_output_object_key) ON submit_intents TO "{role}"',
        'GRANT SELECT (staging_result_document) ON submit_intents TO "{role}"',
        'GRANT INSERT ON execution_stop_evidence TO "{role}"',
        "GRANT SELECT ON execution_release_guards TO PUBLIC",
    ],
)
def test_worker_rejects_missing_and_excess_effective_permissions(
    lease: LeaseDatabase, alteration: str
) -> None:
    with settlement_role(lease) as (role, pool):
        lease.foundation.database.query(alteration.format(role=role))
        with pytest.raises(ValueError, match="Settlement worker"):
            _verify_settlement_principal(pool, lease.foundation.database.schema)


def test_worker_projection_has_only_two_column_reads_and_no_private_access(
    lease: LeaseDatabase,
) -> None:
    db = lease.foundation.database
    with settlement_role(lease) as (_, pool):
        _verify_settlement_principal(pool, db.schema)
        with pool.connection() as connection:
            assert connection.execute("SELECT * FROM execution_release_guards").fetchall() == [
                {"submit_intent_id": "submit-test", "no_dispatch_metadata_absent": 1}
            ]
            assert connection.execute(
                "SELECT has_table_privilege(current_user, 'execution_release_guards', 'SELECT') "
                "AS table_read"
            ).fetchone() == {"table_read": False}
        for query in (
            "SELECT engine_request_id, request_document FROM submit_intents",
            "SELECT staging_output_object_key, staging_result_document FROM submit_intents",
            "UPDATE execution_release_guards SET submit_intent_id='other'",
            "INSERT INTO execution_stop_evidence SELECT * FROM execution_stop_evidence",
            "DELETE FROM execution_stop_evidence",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as conn:
                conn.execute(query)
        # A future owner-added column never inherits the declared column grant.
        db.query(
            "CREATE OR REPLACE VIEW execution_release_guards AS SELECT submit_intent_id, "
            "0 AS no_dispatch_metadata_absent, request_document AS private_document "
            "FROM submit_intents"
        )
        _verify_settlement_principal(pool, db.schema)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as conn:
            conn.execute("SELECT private_document FROM execution_release_guards")


def settings(lease: LeaseDatabase, scanner: str, settlement: str) -> BindingDisableWorkerSettings:
    db = lease.foundation.database
    return BindingDisableWorkerSettings(
        SPEECHNEXUS_PROCESS_ROLE="binding-disable-worker",
        SPEECHNEXUS_DISABLE_DATABASE_URL=make_conninfo(
            db.url, user=scanner, password="synthetic_settlement_test"
        ),
        SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL=make_conninfo(
            db.url, user=settlement, password="synthetic_settlement_test"
        ),
        SPEECHNEXUS_DATABASE_SCHEMA=db.schema,
        SPEECHNEXUS_DISABLE_GATEWAY_ID="gateway-test",
        SPEECHNEXUS_DISABLE_NODE_ID="host-test",
        SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL="http://127.0.0.1:8010",
        SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL="synthetic-component-token",
        SPEECHNEXUS_DISABLE_GATEWAY_TIMEOUT_SECONDS=3,
        SPEECHNEXUS_DISABLE_STORAGE_TIMEOUT_SECONDS=3,
        SPEECHNEXUS_DISABLE_POLL_SECONDS=10,
        SPEECHNEXUS_DISABLE_BATCH_SIZE=10,
        SPEECHNEXUS_DISABLE_WAIT_TIMEOUT_MS=1000,
    )


@pytest.mark.parametrize(
    "alteration",
    [
        "DROP VIEW execution_release_guards",
        'REVOKE SELECT (submit_intent_id) ON execution_release_guards FROM "{role}"',
        'GRANT SELECT ON execution_release_guards TO "{role}"',
    ],
)
async def test_actual_worker_startup_rejects_before_gateway_or_loop(
    lease: LeaseDatabase, monkeypatch: pytest.MonkeyPatch, alteration: str
) -> None:
    db = lease.foundation.database
    scanner = db.schema + "_scan"
    db.query(f"CREATE ROLE \"{scanner}\" LOGIN PASSWORD 'synthetic_settlement_test'")

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Invalid settlement authority reached gateway construction")

    monkeypatch.setattr(binding_disable_worker, "GatewayExecutionClient", unexpected_gateway)
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        with settlement_role(lease) as (role, _):
            db.query(alteration.format(role=role))
            with pytest.raises(ValueError, match="Settlement worker"):
                await run(settings(lease, scanner, role))
    finally:
        db.query(f'DROP OWNED BY "{scanner}"')
        db.query(f'DROP ROLE "{scanner}"')


def test_owner_can_replace_previous_table_grant_with_exact_columns(lease: LeaseDatabase) -> None:
    db = lease.foundation.database
    with settlement_role(lease) as (role, pool):
        db.query(f'GRANT SELECT ON execution_release_guards TO "{role}"')
        with pytest.raises(ValueError, match="Settlement worker"):
            _verify_settlement_principal(pool, db.schema)
        db.query(f'REVOKE SELECT ON execution_release_guards FROM "{role}"')
        for statement in release_guard_grants(db.schema, role):
            db.query(statement)
        _verify_settlement_principal(pool, db.schema)


@pytest.mark.parametrize("indirect", [False, True], ids=["direct", "recursive"])
@pytest.mark.parametrize("extra", ["private-column", "view-table"])
async def test_set_only_role_cannot_bypass_actual_worker_startup(
    lease: LeaseDatabase, monkeypatch: pytest.MonkeyPatch, indirect: bool, extra: str
) -> None:
    db = lease.foundation.database
    scanner, group, bridge = (db.schema + suffix for suffix in ("_scan", "_group", "_bridge"))
    for role in (scanner, group, bridge):
        db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("SET-only authority reached gateway construction before rejection")

    monkeypatch.setattr(binding_disable_worker, "GatewayExecutionClient", unexpected_gateway)
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        db.query(f'GRANT USAGE ON SCHEMA "{db.schema}" TO "{group}"')
        if extra == "private-column":
            db.query(f'GRANT SELECT (request_document) ON submit_intents TO "{group}"')
            query = "SELECT request_document FROM submit_intents"
        else:
            db.query(f'GRANT SELECT ON execution_release_guards TO "{group}"')
            # Table-level SELECT also covers future private view columns.
            db.query(
                "CREATE OR REPLACE VIEW execution_release_guards AS SELECT submit_intent_id, "
                "0 AS no_dispatch_metadata_absent, request_document AS private_document "
                "FROM submit_intents"
            )
            query = "SELECT private_document FROM execution_release_guards"
        with settlement_role(lease) as (settlement, pool):
            parent = bridge if indirect else settlement
            db.query(f'GRANT "{group}" TO "{parent}" WITH INHERIT FALSE, SET TRUE')
            if indirect:
                db.query(f'GRANT "{bridge}" TO "{settlement}" WITH INHERIT FALSE, SET TRUE')
            with pool.connection() as connection:
                assert connection.execute(
                    "SELECT pg_has_role(current_user,%s,'USAGE') AS inherited, "
                    "pg_has_role(current_user,%s,'SET') AS switchable",
                    (group, group),
                ).fetchone() == {"inherited": False, "switchable": True}
            with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as conn:
                conn.execute(query)
            with pool.connection() as connection:
                connection.execute(f'SET LOCAL ROLE "{group}"')
                assert len(connection.execute(query).fetchall()) == 1
            with pytest.raises(ValueError, match="Settlement worker"):
                await run(settings(lease, scanner, settlement))
    finally:
        for role in (scanner, bridge, group):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')


@pytest.mark.parametrize(
    "authority",
    [
        "harmless-set",
        "inherited-private",
        "owner",
        "superuser",
        "role-admin",
        "createrole",
        "createdb",
        "replication",
        "bypassrls",
    ],
)
async def test_worker_refuses_switch_or_administrative_authority(
    lease: LeaseDatabase, monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    db = lease.foundation.database
    scanner, group = (db.schema + suffix for suffix in ("_scan", "_other"))
    db.query(f"CREATE ROLE \"{scanner}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    db.query(f'CREATE ROLE "{group}" NOLOGIN')

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Unsafe authority reached gateway construction")

    monkeypatch.setattr(binding_disable_worker, "GatewayExecutionClient", unexpected_gateway)
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        with settlement_role(lease) as (settlement, pool):
            if authority == "harmless-set":
                db.query(f'GRANT "{group}" TO "{settlement}" WITH INHERIT FALSE, SET TRUE')
            elif authority == "inherited-private":
                db.query(f'GRANT SELECT (request_document) ON submit_intents TO "{group}"')
                db.query(f'GRANT "{group}" TO "{settlement}" WITH INHERIT TRUE, SET FALSE')
            elif authority == "owner":
                owner = db.query("SELECT current_user AS owner")[0]["owner"]
                db.query(f'GRANT "{owner}" TO "{settlement}" WITH INHERIT FALSE, SET FALSE')
            elif authority == "role-admin":
                db.query(
                    f'GRANT "{group}" TO "{settlement}" WITH ADMIN TRUE, INHERIT FALSE, SET FALSE'
                )
                # ADMIN can turn a non-switchable edge into a SET path itself.
                with pool.connection() as connection:
                    connection.execute(f'GRANT "{group}" TO "{settlement}" WITH SET TRUE')
                    connection.execute(f'REVOKE "{group}" FROM "{settlement}"')
                    assert connection.execute(
                        "SELECT pg_has_role(current_user,%s,'SET') AS switchable", (group,)
                    ).fetchone() == {"switchable": False}
            else:
                db.query(f'ALTER ROLE "{settlement}" {authority.upper()}')
            with pytest.raises(ValueError, match="Settlement worker"):
                await run(settings(lease, scanner, settlement))
    finally:
        for role in (scanner, group):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')


def test_blocked_set_path_stays_unreachable_and_postgres_rejects_cycles(
    lease: LeaseDatabase,
) -> None:
    db = lease.foundation.database
    bridge, group = (db.schema + suffix for suffix in ("_bridge", "_group"))
    for role in (bridge, group):
        db.query(f'CREATE ROLE "{role}" NOLOGIN')
    try:
        db.query(f'GRANT SELECT (request_document) ON submit_intents TO "{group}"')
        db.query(f'GRANT "{group}" TO "{bridge}" WITH INHERIT FALSE, SET TRUE')
        with pytest.raises(psycopg.errors.InvalidGrantOperation):
            db.query(f'GRANT "{bridge}" TO "{group}"')
        with settlement_role(lease) as (settlement, pool):
            db.query(f'GRANT "{bridge}" TO "{settlement}" WITH INHERIT FALSE, SET FALSE')
            _verify_settlement_principal(pool, db.schema)
            with pytest.raises(psycopg.errors.InsufficientPrivilege), pool.connection() as conn:
                conn.execute(f'SET LOCAL ROLE "{group}"')
    finally:
        for role in (bridge, group):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')


@pytest.mark.parametrize("principal", ["scanner", "settlement"])
@pytest.mark.parametrize(
    "failure",
    [psycopg.OperationalError, psycopg.errors.QueryCanceled, psycopg.errors.InsufficientPrivilege],
)
async def test_actual_worker_catalog_failure_is_fatal_before_gateway(
    lease: LeaseDatabase,
    monkeypatch: pytest.MonkeyPatch,
    principal: str,
    failure: type[psycopg.Error],
) -> None:
    """Inject driver errors at the real catalog query; no server outage is claimed."""
    db = lease.foundation.database
    scanner = db.schema + "_scan"
    db.query(f"CREATE ROLE \"{scanner}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    original_execute = psycopg.Connection.execute
    failures = 0

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Unavailable role evidence reached gateway construction")

    monkeypatch.setattr(binding_disable_worker, "GatewayExecutionClient", unexpected_gateway)
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        with settlement_role(lease) as (settlement, _):
            fault_role = scanner if principal == "scanner" else settlement

            def unavailable(connection: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
                nonlocal failures
                if connection.info.user == fault_role and "AS can_switch" in str(query):
                    failures += 1
                    raise failure("synthetic catalog failure")
                return original_execute(connection, query, *args, **kwargs)

            monkeypatch.setattr(psycopg.Connection, "execute", unavailable)
            with pytest.raises(failure):
                await run(settings(lease, scanner, settlement))
            assert failures == 1
    finally:
        db.query(f'DROP OWNED BY "{scanner}"')
        db.query(f'DROP ROLE "{scanner}"')


async def test_actual_scanner_rejects_even_harmless_set_role(
    lease: LeaseDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = lease.foundation.database
    scanner, group = (db.schema + suffix for suffix in ("_scan", "_group"))
    for role in (scanner, group):
        db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")

    def unexpected_gateway(**kwargs: object) -> None:
        pytest.fail("Switchable scanner reached gateway construction")

    monkeypatch.setattr(binding_disable_worker, "GatewayExecutionClient", unexpected_gateway)
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        db.query(f'GRANT "{group}" TO "{scanner}" WITH INHERIT FALSE, SET TRUE')
        with (
            settlement_role(lease) as (settlement, _),
            pytest.raises(ValueError, match="Disable worker"),
        ):
            await run(settings(lease, scanner, settlement))
    finally:
        for role in (scanner, group):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')


@pytest.mark.parametrize("inherit_only", [False, True], ids=["direct", "inherit-without-set"])
@pytest.mark.parametrize("history_present", [False, True])
async def test_actual_worker_loop_settles_only_consistent_failure(
    lease: LeaseDatabase, monkeypatch: pytest.MonkeyPatch, history_present: bool, inherit_only: bool
) -> None:
    db = lease.foundation.database
    no_dispatch_failure(lease)
    if history_present:
        db.query("UPDATE submit_intents SET engine_request_id='synthetic-accepted'")
    db.query("UPDATE host_nodes SET held_unified_physical_memory_bytes=8")
    seed_binding(lease.foundation)
    # Synthetic lifecycle fixture; the original-abort test covers its real producer.
    db.query(
        "INSERT INTO speech_publication_lifecycle_events "
        "(event_id,action,binding_id,revision,revision_id,result_status,request_id,actor_type,"
        "occurred_at_us,policy_revision,intent_digest,change_summary_json) VALUES "
        "('event-synthetic','model_binding.disable','synthetic','1','binding-test','disabled',"
        "'request-synthetic','anonymous',1,1,%s,'{}')",
        ("sha256:" + "a" * 64,),
    )
    db.query("INSERT INTO speech_binding_disable_intents VALUES ('event-synthetic')")
    before = lease.snapshot()
    scanner = db.schema + "_scan"
    db.query(f"CREATE ROLE \"{scanner}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    results: asyncio.Queue[DisablePassResult] = asyncio.Queue()
    real_pass = BindingDisableCoordinator.run_pass

    async def observed_pass(coordinator: BindingDisableCoordinator) -> DisablePassResult:
        result = await real_pass(coordinator)
        results.put_nowait(result)
        return result

    monkeypatch.setattr(BindingDisableCoordinator, "run_pass", observed_pass)
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        with settlement_role(lease, inherit_only=inherit_only) as (settlement, _):
            task = asyncio.create_task(run(settings(lease, scanner, settlement)))
            try:
                result = await asyncio.wait_for(results.get(), timeout=10)
                assert not task.done()
                assert result.attempted == result.unavailable == 0
                assert result.pending == int(history_present)
                assert result.settled == int(not history_present)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        if history_present:
            assert lease.snapshot() == before
        else:
            assert db.query("SELECT held_attempts FROM workspace_quotas") == [{"held_attempts": 0}]
            assert db.query(
                "SELECT held_unified_physical_memory_bytes AS held FROM host_nodes"
            ) == [{"held": 7}]
            assert db.query("SELECT instance_state FROM engine_instances") == [
                {"instance_state": "ready"}
            ]
        assert db.query("SELECT * FROM submit_intents") == before["submit_intents"]
    finally:
        db.query(f'DROP OWNED BY "{scanner}"')
        db.query(f'DROP ROLE "{scanner}"')
