"""Every restricted identity reads its own production face once, on the real database.

Three shipped slices failed the same way in different places: a read face started using
a column (``#1038`` added ``attempts`` to the cancellation ledger read) or a process
opened with the wrong identity (``#1041`` polled as the writer instead of the admission
role), the deployment kept the old ``GRANT``, and the whole statement failed with
``42501`` so the face degraded to ``unavailable``. Comparing grant functions with the
column names in a statement cannot catch that: the two are prose. The only answer is the
one ``#1041`` already gave for the poller -- **connect as the identity and run the
statement**.

This module does that for every face whose production code executes a statement under a
restricted role. Each case provisions the identity through the reference provisioning
(``scripts/dev-env/provision-job-roles.py`` and the ``*_grants`` functions), seeds the
rows the face reads, and then runs the face's *own* production query, service or adapter
over a connection authenticated as that identity. A face is covered only when its
statement returns the shape the code expects; nothing here falls back to the owner, and
no case may pass on a superuser connection.

Coverage -- face, identity, grant function to re-run:

* ``cancellation_debt.drilldown``: the three per-lease reads (``_DEBT``,
  ``_LEFT_CANCELLING_DEBT``, ``_PERMANENT_DEBT``) -- cancellation-debt reader /
  ``cancellation_debt_reader_grants``;
* ``cancellation_debt.summary``: the ``/readyz`` sentence plus the Job-level summary
  read (``_JOB_LEVEL_DEBT``) it degrades to -- cancellation-debt reader /
  ``cancellation_debt_reader_grants``;
* ``stop_evidence_debt``: the dispatch gate's stuck-Job read and its probe --
  cancellation-debt reader / ``cancellation_debt_reader_grants``;
* ``admission.readiness``: the admission-readiness report's row reads -- admission /
  ``public_job_admission_grants``;
* ``admission.watch_required_tables``: the poller's ``REQUIRED_TABLES`` self-check
  (``#1041``) -- admission / ``public_job_admission_grants``;
* ``dispatch.claim``: ``PostgresDispatchQueue.claim_due`` -- dispatch /
  ``job_runtime_grants``, ``job_dispatch_grants``, ``public_job_grants``,
  ``coordination_runtime_grants``;
* ``writer.claim_and_outbox``: ``create_in_transaction`` on an existing Job (the
  replay branch reads the coordination row and the outbox row) plus
  ``claim_in_transaction`` -- Job writer / ``job_runtime_grants`` plus the writer
  baseline in ``provision-job-roles.py``;
* ``binding_disable.coordinator``: the coordinator's intent, target and lease reads --
  binding-disable coordinator / ``disable_coordinator_grants``;
* ``asset.inspection``: ``PostgresAssetMetadata.get`` and ``.object`` -- asset runtime /
  ``asset_runtime_grants``.

Every surface declares the relations and columns its own statement names. The
declaration is what turns ``permission denied for table ...`` into an actionable
sentence: PostgreSQL names the table but never the missing column of a column-level
``GRANT``, and ``#1038`` was exactly a missing column. ``test_the_declaration_is_enough
_to_run_each_face`` revokes every privilege the identity holds and grants back only the
declaration, so a face that reads something its declaration omits fails there instead of
passing on the deployment's wider grant.

Boundaries: PostgreSQL at the shipped migration head, with synthetic rows only. It is
not a browser, engine, gateway or MinIO run: the asset face stops at the metadata
statement the inspection path reads, not at object storage. MySQL/InnoDB has no role
model here. It proves the face the production code runs today reads successfully as the
identity the deployment provisions today; it does not prove that a future edit to a
statement was also reflected in the declaration -- the declaration case is what reddens
when it was not.

The declaration is a **lower bound** on the read set, not an equality with the
deployment's grant: it proves the face needs no more than this, so it catches a
production grant narrower than the statement (``#1038``, ``#1041``) and cannot catch a
grant wider than the declaration. That asymmetry is deliberate -- one identity serves a
whole creation path, so asserting equality would be brittle and wrong -- and the
``asset``/``admission`` rows deliberately declare less than their identities hold.

Two things this module cannot answer, both of which stay with deployment: whether a live
role ever re-ran its grant function (the roles here are built and provisioned in the
test), and whether some relation is granted to ``PUBLIC`` (a per-role ``REVOKE`` cannot
remove that, so ``test_the_schema_grants_nothing_to_public`` asserts the schema has no
``PUBLIC`` ACL entry at all).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from tests.integration.test_cancel_failure_slot_pg import (
    DIGEST,
    WORKSPACE,
    _seed_execution,
    _seed_foundation,
)
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.admission_watch_runtime import (
    AdmissionWatchRuntime,
    AdmissionWatchSettings,
)
from speechnexus_backend.speech.domain.assets import digest
from speechnexus_backend.speech.domain.claims import ClaimJob
from speechnexus_backend.speech.domain.jobs import CreateJob, JobFact
from speechnexus_backend.speech.infrastructure.asset_schema import asset_runtime_grants
from speechnexus_backend.speech.infrastructure.assets_postgres import PostgresAssetMetadata
from speechnexus_backend.speech.infrastructure.binding_disable_coordinator_postgres import (
    PostgresDisableCoordinatorStore,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants,
)
from speechnexus_backend.speech.infrastructure.claims_sql import (
    claim_in_transaction,
    read_claim,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    RECONCILIATION_TABLE,
    cancellation_debt_reader_grants,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresDispatchQueue
from speechnexus_backend.speech.infrastructure.jobs_sql import create_in_transaction
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_admission import (
    load_admission_readiness,
)
from speechnexus_backend.speech.infrastructure.readiness import (
    _DEBT,
    _JOB_LEVEL_DEBT,
    _LEFT_CANCELLING_DEBT,
    _PERMANENT_DEBT,
    _STOP_EVIDENCE_DEBT,
    CancellationDebtProbe,
    StopEvidenceDebtProbe,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

ROOT = Path(__file__).resolve().parents[2]
PROVISION = ROOT / "scripts/dev-env/provision-job-roles.py"
PASSWORD = "synthetic_grant_coverage"
JOB_ID = "job-grant-coverage"
LEASE = "lease-grant-coverage"
COORDINATION_JOB = "job-coordination-grant"
COORDINATION_EVENT = "event-coordination-grant"
DISABLE_EVENT = "event-grant-coverage"
ASSET_NAMESPACE = "sha256:" + "e" * 64
ASSET_ID = "asset-grant-coverage"
#: Wait to the maximum the ledger's CHECK allows, so the seeded acceptance is
#: ``retrying on schedule``: the sentence then prints ``attempts`` and the drill-down
#: keeps ``[retrying: attempts=N, next in Ts]``, which is the fact ``#1038`` added.
FUTURE_US = 253402300799999999


def _provisioning() -> Any:
    """Load the reference provisioning script by path; it is not an importable module."""
    spec = importlib.util.spec_from_file_location("provision_job_roles", PROVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PROVISIONING = _provisioning()


@dataclass(frozen=True)
class Role:
    """One provisioned identity and the statements that produced its grants."""

    name: str
    grants: tuple[str, ...]


@dataclass(frozen=True)
class Read:
    """One privilege a face's statement needs; ``columns is None`` means the relation.

    Column-level declarations are the actionable half: PostgreSQL reports a missing
    column grant as ``permission denied for table ...``, so the column has to come
    from the face's own read set.
    """

    table: str
    columns: tuple[str, ...] | None = None
    privilege: str = "SELECT"


@dataclass(frozen=True)
class Surface:
    """One production read face: which identity runs it and what that identity reads."""

    name: str
    role: str
    grants: tuple[str, ...]
    reads: tuple[Read, ...]
    run: Callable[[Coverage], str]


@dataclass
class Coverage:
    """The provisioned identities, their grants, and the seeded rows."""

    db: JobDatabase
    roles: dict[str, Role]

    def role(self, key: str) -> str:
        return self.roles[key].name

    def dsn(self, key: str) -> str:
        return make_conninfo(self.db.url, user=self.roles[key].name, password=PASSWORD)


def _connect(coverage: Coverage, key: str) -> psycopg.Connection[dict[str, Any]]:
    """Open a connection authenticated as one provisioned identity."""
    connection = portable_pg_connect(
        coverage.dsn(key), row_factory=dict_row, autocommit=False, connect_timeout=5
    )
    connection.execute(f'SET search_path TO "{coverage.db.schema}", pg_catalog')
    connection.commit()
    return connection


def _now_us(connection: psycopg.Connection[dict[str, Any]]) -> int:
    row = connection.execute(
        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
    ).fetchone()
    assert row is not None
    return int(row["now_us"])


def _columns(db: JobDatabase, table: str) -> tuple[str, ...]:
    rows = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (db.schema, table),
    )
    return tuple(str(row["column_name"]) for row in rows)


def _revoke_relation(db: JobDatabase, table: str, role: str | None = None) -> None:
    """Remove every privilege one grantee holds on a relation, table and column level.

    ``role is None`` means ``PUBLIC``, which PostgreSQL only accepts unquoted; the
    keyword form is what lets the per-face cases stay closed when a migration granted
    the relation to everyone.
    """
    grantee = "PUBLIC" if role is None else f'"{role}"'
    db.admin.execute(f'REVOKE ALL ON "{db.schema}"."{table}" FROM {grantee}')
    for column in _columns(db, table):
        db.admin.execute(f'REVOKE SELECT ("{column}") ON "{db.schema}"."{table}" FROM {grantee}')


def _missing_privileges(coverage: Coverage, surface: Surface) -> list[str]:
    """The declared reads this identity cannot satisfy, named as ``relation.column``."""
    role = coverage.role(surface.role)
    missing: list[str] = []
    for read in surface.reads:
        qualified = f"{coverage.db.schema}.{read.table}"
        if read.columns is None:
            granted = coverage.db.query(
                "SELECT has_table_privilege(%s, %s, %s) AS ok", (role, qualified, read.privilege)
            )[0]["ok"]
            if not granted:
                missing.append(f"{read.table} ({read.privilege} on the relation)")
            continue
        for column in read.columns:
            granted = coverage.db.query(
                "SELECT has_column_privilege(%s, %s, %s, %s) AS ok",
                (role, qualified, column, read.privilege),
            )[0]["ok"]
            if not granted:
                missing.append(f"{read.table}.{column} ({read.privilege})")
    return missing


#: The one grant group whose identity arguments are keyword-only, so a naive
#: ``function(schema, role)`` suggestion cannot be pasted without a ``TypeError``.
KEYWORD_GRANTS = frozenset({"coordination_runtime_grants"})


def _repair(surface: Surface, coverage: Coverage) -> str:
    """The exact provisioning step to replay for this face, copy-pasteable."""
    role = coverage.role(surface.role)
    steps: list[str] = []
    for name in surface.grants:
        if name in KEYWORD_GRANTS:
            steps.append(
                f'{name}("{coverage.db.schema}", speech_role="{role}", '
                f'gateway_role="{coverage.role("gateway")}")'
            )
        elif name.endswith("_grants"):
            steps.append(f'{name}("{coverage.db.schema}", "{role}")')
        else:
            steps.append(f"{name} (with this identity's DSN)")
    return "re-run as the migration owner: " + ", ".join(steps)


def _failure(coverage: Coverage, surface: Surface, missing: list[str], error: object) -> str:
    """One sentence that names the face, the missing read, and the grant to re-run."""
    lines = [
        f"face {surface.name} runs as {coverage.role(surface.role)} and cannot read "
        "what its own statement reads",
    ]
    if missing:
        lines.append("missing privilege(s): " + ", ".join(missing))
    if error is not None:
        lines.append(f"the face's own read failed: {type(error).__name__}: {error}")
    lines.append(_repair(surface, coverage))
    return "\n  ".join(lines)


def _measure(coverage: Coverage, surface: Surface) -> tuple[bool, str]:
    """Run one face as its own identity; return success and either the shape or the reason."""
    missing = _missing_privileges(coverage, surface)
    error: object | None = None
    observed = ""
    try:
        observed = surface.run(coverage)
    except Exception as failure:
        error = failure
    if missing or error is not None:
        return False, _failure(coverage, surface, missing, error)
    return True, observed


# --------------------------------------------------------------------------------------
# Provisioning: the reference script for the three Job identities, the declared grant
# functions for the optional readers, and the rows the faces read.
# --------------------------------------------------------------------------------------


def _create_roles(db: JobDatabase, names: dict[str, str]) -> None:
    for name in names.values():
        db.admin.execute(
            f'CREATE ROLE "{name}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT'
        )
        db.admin.execute(f"ALTER ROLE \"{name}\" PASSWORD '{PASSWORD}'")


def _apply_grants(
    db: JobDatabase, statements: list[str], names: dict[str, str]
) -> dict[str, tuple[str, ...]]:
    """Apply every statement, grouped by the identity it names so it can be replayed."""
    grouped: dict[str, list[str]] = {key: [] for key in names}
    for statement in statements:
        owner = next((key for key, name in names.items() if f'"{name}"' in statement), None)
        if owner is None:
            raise AssertionError(f"grant statement names no provisioned role: {statement}")
        db.admin.execute(statement)
        grouped[owner].append(statement)
    return {key: tuple(value) for key, value in grouped.items()}


@contextmanager
def _provisioned(db: JobDatabase) -> Iterator[dict[str, Role]]:
    suffix = uuid.uuid4().hex[:8]
    names = {
        key: f"sn_gcov_{key}_{suffix}"
        for key in ("writer", "admission", "dispatch", "gateway", "debt", "disable", "asset")
    }
    _create_roles(db, names)
    try:
        statements = list(
            _PROVISIONING.statements(
                db.schema,
                names["writer"],
                names["admission"],
                names["dispatch"],
                names["gateway"],
                ("asr", "tts", "voice_validation"),
            )
        )
        # The optional readers are not in the reference script: each is its own
        # declared function, and a deployment points one DSN at it.
        statements += list(cancellation_debt_reader_grants(db.schema, names["debt"]))
        statements += list(disable_coordinator_grants(db.schema, names["disable"]))
        statements += list(asset_runtime_grants(db.schema, names["asset"]))
        grants = _apply_grants(db, statements, names)
        yield {key: Role(names[key], grants[key]) for key in names}
    finally:
        for name in names.values():
            db.admin.execute(f'DROP OWNED BY "{name}"')
            db.admin.execute(f'DROP ROLE IF EXISTS "{name}"')


def _restore(coverage: Coverage, role: str) -> None:
    """Replay one identity's own grant statements after a negative case revoked them."""
    for statement in coverage.roles[role].grants:
        coverage.db.admin.execute(statement)


def _seed(db: JobDatabase) -> None:
    """The rows the faces read: one accepted cancellation holding a lease, and friends."""
    _seed_foundation(db.admin, JOB_ID, (LEASE,))
    _seed_execution(db.admin, JOB_ID, LEASE, ("failed", "complete"), identity="submit-" + JOB_ID)
    db.admin.execute(
        "INSERT INTO speech_workspaces (workspace_id, display_name, workspace_state, "
        "created_at_us) VALUES (%s, 'grant coverage', 'active', 1)",
        (WORKSPACE,),
    )
    db.admin.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) "
        "VALUES (%s, %s, 'tts', 'tts-quality', %s, 'cancelling', 2, 1, 1)",
        (JOB_ID, WORKSPACE, DIGEST),
    )
    db.admin.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id) "
        "VALUES (%s, 1, 'request-grant-coverage')",
        (JOB_ID,),
    )
    db.admin.execute(
        f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
        "last_outcome, settled_at_us, updated_at_us) "
        "VALUES (%s, 7, %s, 'stop_unconfirmed', NULL, 1)",
        (JOB_ID, FUTURE_US),
    )
    db.admin.execute(
        "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, initial_event_id, "
        "initial_event_digest, request_digest) VALUES (%s, %s, 'attempt-coordination', %s, "
        "%s, %s)",
        (COORDINATION_JOB, COORDINATION_JOB, COORDINATION_EVENT, DIGEST, DIGEST),
    )
    db.admin.execute(
        "INSERT INTO coordination_outbox (event_id, job_id, event_digest) VALUES (%s, %s, %s)",
        (COORDINATION_EVENT, COORDINATION_JOB, DIGEST),
    )
    db.admin.execute(
        "INSERT INTO speech_asset_storage_authority (binding_id, namespace_digest, "
        "recorded_at_us) VALUES (1, %s, 1)",
        (digest(ASSET_NAMESPACE),),
    )


@pytest.fixture
def coverage(database: JobDatabase) -> Iterator[Coverage]:
    with _provisioned(database) as roles:
        _seed(database)
        yield Coverage(database, roles)


# --------------------------------------------------------------------------------------
# The faces. Each ``run`` calls the production code with a connection authenticated as
# the identity the deployment provisions for that face.
# --------------------------------------------------------------------------------------

DEBT_READS: tuple[Read, ...] = (
    Read("speech_job_cancellation_fences", ("job_id", "accepted_at_us")),
    Read(
        RECONCILIATION_TABLE,
        ("job_id", "settled_at_us", "last_outcome", "next_check_at_us", "attempts"),
    ),
    Read("speech_jobs", ("job_id", "state", "updated_at_us")),
    Read("admission_leases", ("lease_id", "operation_id", "lease_state")),
    Read(
        "submit_intents",
        (
            "submit_intent_id",
            "lease_id",
            "execution_state",
            "cleanup_state",
            "dispatched_at_us",
            "engine_request_id",
        ),
    ),
    Read(
        "execution_stop_evidence",
        ("submit_intent_id", "evidence_kind", "execution_stopped", "resources_released"),
    ),
)
#: The dispatch gate's sibling read names no ledger, fence or execution-detail column:
#: it starts from the running Job and its held lease, so a ledger grant regression must
#: leave it green (that is what makes the ``#1038`` counterexample column-specific).
STOP_EVIDENCE_READS: tuple[Read, ...] = (
    Read("speech_jobs", ("job_id", "state", "updated_at_us")),
    Read("admission_leases", ("lease_id", "operation_id", "lease_state")),
    Read(
        "submit_intents",
        (
            "submit_intent_id",
            "lease_id",
            "execution_state",
            "cleanup_state",
            "dispatched_at_us",
            "engine_request_id",
        ),
    ),
    Read(
        "execution_stop_evidence",
        ("submit_intent_id", "evidence_kind", "execution_stopped", "resources_released"),
    ),
)
#: The column list every debt read returns, in order: the drill-down the operator reads.
DRILLDOWN_COLUMNS = (
    "job_id",
    "lease_id",
    "lease_state",
    "execution_state",
    "evidence_kind",
    "execution_stopped",
    "resources_released",
    "schedule_state",
    "debt_class",
    "attempts",
    "retry_in_seconds",
)
ADMISSION_READS: tuple[Read, ...] = (
    Read("speech_workspaces"),
    Read("workspace_quotas"),
    Read("speech_publications"),
    Read("speech_publication_heads"),
    Read("speech_binding_revisions"),
    Read("speech_publication_retirements"),
    Read("engine_instances"),
    Read("runtime_instance_heads"),
    Read("runtime_instance_intents"),
    Read("resource_pools"),
    Read("host_nodes"),
    Read("admission_leases"),
    Read("admission_attempts"),
)


def _run_debt_drilldown(coverage: Coverage) -> str:
    """The three bounded/unbounded per-lease reads behind the detail, as the reader."""
    connection = _connect(coverage, "debt")
    try:
        now_us = _now_us(connection)
        shapes: list[str] = []
        named: list[str] = []
        with connection.cursor() as cursor:
            reads = (
                ("full", _DEBT, {"grace_us": 1, "limit": 8}),
                (
                    "left-cancelling",
                    _LEFT_CANCELLING_DEBT,
                    {
                        "now_us": now_us,
                        "window_us": 86_400_000_000,
                        "grace_us": 1,
                        "limit": 8,
                    },
                ),
                ("permanent", _PERMANENT_DEBT, {"now_us": now_us, "grace_us": 1, "limit": 8}),
            )
            for label, statement, parameters in reads:
                cursor.execute(statement, parameters)
                rows = cursor.fetchall()
                for row in rows:
                    assert tuple(row) == DRILLDOWN_COLUMNS, (label, tuple(row))
                shapes.append(f"{label}={len(rows)}x{len(DRILLDOWN_COLUMNS)}")
                if rows:
                    named.append(label)
        # The seeded acceptance is `cancelling` and holds a lease, so both reads that
        # can name it must; the fourth read is disjoint from it by construction.
        assert {"full", "permanent"} <= set(named), (named, shapes)
        return ", ".join(shapes)
    finally:
        connection.close()


def _run_debt_summary(coverage: Coverage) -> str:
    """The ``/readyz`` summary: its Job-level read plus the production probe's sentence."""
    # The Job-level read returns five untyped NULL placeholders, so a dict row factory
    # would collapse them into one key and hide the tuple shape the code indexes.
    connection = portable_pg_connect(coverage.dsn("debt"), autocommit=False, connect_timeout=5)
    try:
        connection.execute(f'SET search_path TO "{coverage.db.schema}", pg_catalog')
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            cursor.execute(_JOB_LEVEL_DEBT, {"grace_us": 1, "limit": 8})
            rows = cursor.fetchall()
        connection.rollback()
    finally:
        connection.close()
    assert [row[0] for row in rows] == [JOB_ID], rows
    for row in rows:
        # Eleven positions and the ledger retry facts at the end (#1038): the read is
        # consumed positionally, so the positions are the shape.
        assert len(row) == len(DRILLDOWN_COLUMNS), row
        assert row[7] == "scheduled", row
        assert row[9] == 7 and row[10] is not None, row
    probe = CancellationDebtProbe(
        coverage.dsn("debt"),
        schema=coverage.db.schema,
        timeout_seconds=8,
        grace_seconds=1,
    )
    try:
        detail = asyncio.run(probe.check())
    finally:
        asyncio.run(probe.aclose())
    assert detail is not None, "the probe reported no debt for a seeded accepted cancellation"
    assert "unavailable" not in detail, detail
    assert JOB_ID in detail, detail
    # The retry facts are the columns #1038 added: the sentence only prints them when
    # the ledger's `attempts` and `next_check_at_us` were readable.
    assert "retrying on schedule: 1" in detail, detail
    assert "attempts=7" in detail, detail
    return f"summary={len(rows)}x{len(DRILLDOWN_COLUMNS)} {detail}"


def _run_stop_evidence(coverage: Coverage) -> str:
    """The dispatch gate's sibling read and its probe, both as the same identity."""
    connection = _connect(coverage, "debt")
    try:
        now_us = _now_us(connection)
        with connection.cursor() as cursor:
            cursor.execute(_STOP_EVIDENCE_DEBT, {"grace_us": 1, "limit": 8, "now_us": now_us})
            rows = cursor.fetchall()
        assert all(
            tuple(row)
            == (
                "job_id",
                "submit_intent_id",
                "execution_state",
                "cleanup_state",
                "evidence_kind",
            )
            for row in rows
        ), rows
    finally:
        connection.close()
    probe = StopEvidenceDebtProbe(
        coverage.dsn("debt"),
        schema=coverage.db.schema,
        timeout_seconds=8,
        grace_seconds=1,
    )
    try:
        detail = asyncio.run(probe.check())
    finally:
        asyncio.run(probe.aclose())
    # The seeded Job is `cancelling`, so this probe must see no debt -- and must not
    # answer `unavailable (permission)`, which is what a missing grant produces.
    assert detail is None, detail
    return f"rows={len(rows)} probe=none"


def _run_admission_readiness(coverage: Coverage) -> str:
    """The readiness report's own row reads, through the production loader."""
    connection = _connect(coverage, "admission")
    try:
        connection.execute("SET TRANSACTION READ ONLY")
        now_us = _now_us(connection)
        with connection.cursor() as cursor:
            report = load_admission_readiness(cursor, WORKSPACE, now_us=now_us)
        connection.rollback()
    finally:
        connection.close()
    assert report.workspace_id == WORKSPACE, report
    assert report.status.value in {"admitting", "degraded"}, report
    return (
        f"status={report.status.value} cause="
        f"{report.cause.value if report.cause is not None else None} "
        f"instances={len(report.instances)}"
    )


def _run_admission_watch(coverage: Coverage) -> str:
    """The poller's open-time self-check (``#1041``): ``SELECT 1`` per required relation."""
    settings = AdmissionWatchSettings(
        enabled=True,
        database_url=coverage.dsn("admission"),
        database_schema=coverage.db.schema,
        interval_seconds=60,
        transaction_timeout_seconds=5,
    )
    runtime = AdmissionWatchRuntime(settings)
    try:
        asyncio.run(runtime.open())
    finally:
        asyncio.run(runtime.close())
    return "required tables readable"


def _run_dispatch_claim(coverage: Coverage) -> str:
    """The execution dispatcher's due-Job claim, as the dispatch identity."""
    pool = speech_pool(coverage.dsn("dispatch"), schema=coverage.db.schema, max_size=2)
    pool.open()
    try:
        queue = PostgresDispatchQueue(
            pool, kinds=("asr", "tts"), require_admission=True, timeout_seconds=5
        )
        claims = asyncio.run(queue.claim_due("worker-grant-coverage", 30_000, 4))
    finally:
        pool.close()
    assert claims == [], claims
    return "claim=0"


def _run_writer_claim(coverage: Coverage) -> str:
    """The writer's create replay (which reads the outbox) plus its coordination claim."""
    request = CreateJob(
        job_id=COORDINATION_JOB,
        operation_id=COORDINATION_JOB,
        attempt_id="attempt-coordination",
        event_id=COORDINATION_EVENT,
        event_digest=DIGEST,
        request_digest=DIGEST,
    )
    connection = _connect(coverage, "writer")
    try:
        now_us = _now_us(connection)
        with connection.cursor() as cursor:
            # ``create_in_transaction`` on an existing Job takes the replay branch:
            # it reads the coordination row and then the outbox row it published. No
            # statement here is copied from the adapter, so a change to the production
            # SELECT reddens this face instead of passing on a stale copy.
            created = create_in_transaction(cursor, request)
            current = read_claim(cursor, COORDINATION_JOB, lock=True)
            result = claim_in_transaction(
                cursor,
                ClaimJob(COORDINATION_JOB, "worker-grant-coverage", current.claim_version, 30_000),
                clock=lambda: now_us,
            )
        connection.rollback()
    finally:
        connection.close()
    assert created.outcome == "replayed", created
    assert created.fact == JobFact(COORDINATION_JOB, DIGEST), created
    assert result.outcome == "committed", result
    return f"create={created.outcome} claim={result.outcome} version={current.claim_version}"


def _run_binding_disable(coverage: Coverage) -> str:
    """The disable coordinator's intent, target and lease reads."""
    pool = speech_pool(coverage.dsn("disable"), schema=coverage.db.schema, max_size=2)
    pool.open()
    try:
        store = PostgresDisableCoordinatorStore(pool, timeout_seconds=5)
        events = store.event_ids(None, 8)
        targets = store.targets(
            DISABLE_EVENT, None, 8, "gateway-grant-coverage", "host-grant-coverage"
        )
        leases = store.leases(
            DISABLE_EVENT, "gateway-grant-coverage", "host-grant-coverage", None, 8
        )
    finally:
        pool.close()
    assert (events, targets, leases) == ([], [], []), (events, targets, leases)
    return "events=0 targets=0 leases=0"


def _run_asset_inspection(coverage: Coverage) -> str:
    """The asset inspection read: the metadata row and its verified object proof."""
    pool = speech_pool(coverage.dsn("asset"), schema=coverage.db.schema, max_size=2)
    pool.open()
    try:
        metadata = PostgresAssetMetadata(pool, namespace_digest=ASSET_NAMESPACE, timeout_seconds=5)
        record, now_us = metadata.get(ASSET_ID)
        proof = metadata.object(ASSET_ID)
    finally:
        pool.close()
    assert record is None and proof is None
    assert now_us > 0
    return f"asset=none clock={now_us > 0}"


SURFACES: tuple[Surface, ...] = (
    Surface(
        "cancellation_debt.drilldown",
        "debt",
        ("cancellation_debt_reader_grants",),
        DEBT_READS,
        _run_debt_drilldown,
    ),
    Surface(
        "cancellation_debt.summary",
        "debt",
        ("cancellation_debt_reader_grants",),
        DEBT_READS,
        _run_debt_summary,
    ),
    Surface(
        "stop_evidence_debt",
        "debt",
        ("cancellation_debt_reader_grants",),
        STOP_EVIDENCE_READS,
        _run_stop_evidence,
    ),
    Surface(
        "admission.readiness",
        "admission",
        ("public_job_admission_grants",),
        ADMISSION_READS,
        _run_admission_readiness,
    ),
    Surface(
        "admission.watch_required_tables",
        "admission",
        ("public_job_admission_grants",),
        ADMISSION_READS,
        _run_admission_watch,
    ),
    Surface(
        "dispatch.claim",
        "dispatch",
        (
            "job_runtime_grants",
            "job_dispatch_grants",
            "public_job_grants",
            "coordination_runtime_grants",
        ),
        (
            Read("coordination_jobs"),
            Read(
                "coordination_jobs",
                ("claim_owner_id", "claim_version", "claim_expires_at_us"),
                "UPDATE",
            ),
            Read("speech_jobs"),
            Read("admission_operations"),
            Read("admission_attempts"),
            Read("admission_leases"),
        ),
        _run_dispatch_claim,
    ),
    Surface(
        "writer.claim_and_outbox",
        "writer",
        ("job_runtime_grants", "scripts/dev-env/provision-job-roles.py"),
        (
            Read("coordination_jobs"),
            Read("coordination_outbox"),
            Read(
                "coordination_jobs",
                ("claim_owner_id", "claim_version", "claim_expires_at_us"),
                "UPDATE",
            ),
        ),
        _run_writer_claim,
    ),
    Surface(
        "binding_disable.coordinator",
        "disable",
        ("disable_coordinator_grants",),
        (
            Read("speech_binding_disable_intents", ("event_id",)),
            Read("speech_binding_disable_targets"),
            Read("speech_publication_lifecycle_events", ("event_id", "revision_id", "action")),
            Read(
                "admission_attempts",
                (
                    "operation_id",
                    "attempt_id",
                    "workspace_id",
                    "binding_revision_id",
                    "policy_revision_id",
                ),
            ),
            Read(
                "admission_leases",
                (
                    "operation_id",
                    "attempt_id",
                    "lease_id",
                    "stage_id",
                    "pool_id",
                    "gateway_id",
                    "host_node_id",
                    "instance_id",
                    "instance_generation",
                    "fencing_token",
                    "lease_state",
                ),
            ),
            Read(
                "submit_intents",
                (
                    "submit_intent_id",
                    "operation_id",
                    "attempt_id",
                    "lease_id",
                    "voice_revision_id",
                    "command_digest",
                    "execution_state",
                ),
            ),
        ),
        _run_binding_disable,
    ),
    Surface(
        "asset.inspection",
        "asset",
        ("asset_runtime_grants",),
        (
            Read("speech_assets"),
            Read("speech_asset_objects"),
            Read("speech_asset_storage_authority"),
        ),
        _run_asset_inspection,
    ),
)
SURFACE_BY_NAME = {surface.name: surface for surface in SURFACES}


@dataclass(frozen=True)
class Isolation:
    """One negative case: revoke a relation from an identity and say who must go red."""

    role: str
    table: str


ISOLATION_CASES: tuple[Isolation, ...] = (
    # One surface only: nothing else in the deployment reads the lease row.
    Isolation("dispatch", "admission_leases"),
    # One surface only: the writer's outbox claim is its own read.
    Isolation("writer", "coordination_outbox"),
    # The whole debt family reads the ledger, and no other identity does.
    Isolation("debt", RECONCILIATION_TABLE),
    Isolation("admission", "workspace_quotas"),
    Isolation("disable", "speech_binding_disable_intents"),
    Isolation("asset", "speech_asset_objects"),
)


def _expected_red(case: Isolation) -> set[str]:
    """Every surface whose declared reads, under that identity, name the relation."""
    return {
        surface.name
        for surface in SURFACES
        if surface.role == case.role and any(read.table == case.table for read in surface.reads)
    }


def _measure_all(coverage: Coverage) -> dict[str, tuple[bool, str]]:
    return {surface.name: _measure(coverage, surface) for surface in SURFACES}


@pytest.mark.parametrize("surface", SURFACES, ids=[surface.name for surface in SURFACES])
def test_every_face_reads_as_its_own_identity(coverage: Coverage, surface: Surface) -> None:
    """One face, one identity: the statement must succeed and return its own shape.

    Parametrized per face on purpose. A grant that fell behind one deployment step
    has to redden *that* face's case and leave its neighbours green, so the report
    names the face an operator must repair instead of one opaque failure.
    """
    ok, observed = _measure(coverage, surface)
    print(f"FACE {surface.name:<34} {'ok  ' if ok else 'FAIL'} {observed}")
    assert ok, observed


@pytest.mark.parametrize(
    ("case",),
    [(case,) for case in ISOLATION_CASES],
    ids=[f"{case.role}-{case.table}" for case in ISOLATION_CASES],
)
def test_revoking_one_relation_reddens_only_the_faces_that_read_it(
    coverage: Coverage, case: Isolation
) -> None:
    """A grant is per identity and per relation: the blast radius is the declared faces."""
    expected = _expected_red(case)
    assert expected, f"{case.role} declares no face reading {case.table}"
    _revoke_relation(coverage.db, case.table, coverage.role(case.role))
    try:
        results = _measure_all(coverage)
    finally:
        _restore(coverage, case.role)
    red = {name for name, (ok, _) in results.items() if not ok}
    for name, (ok, observed) in results.items():
        print(f"CASE {case.role}.{case.table} {name:<34} {'RED ' if not ok else 'ok  '} {observed}")
    assert red == expected, (
        f"revoking {case.table} from {coverage.role(case.role)} reddened {sorted(red)}, "
        f"but the declared faces reading it are {sorted(expected)}"
    )
    # Every red surface names its own repair rather than a bare privilege error.
    for name in red:
        assert coverage.role(SURFACE_BY_NAME[name].role) in results[name][1]
    # And the same run is green again once the identity's grants are replayed.
    restored = {name: ok for name, (ok, _) in _measure_all(coverage).items()}
    assert all(restored.values()), [name for name, ok in restored.items() if not ok]


def test_a_four_column_ledger_grant_names_the_missing_attempts(coverage: Coverage) -> None:
    """The ``#1038`` counterexample: the old four-column ledger grant must go red by name.

    A deployment that never re-ran ``cancellation_debt_reader_grants`` keeps the
    ledger granted on ``job_id, settled_at_us, last_outcome, next_check_at_us``.
    Column-level ``SELECT`` then fails the whole statement, and the read face becomes
    ``unavailable``. The test has to say *which* column, not just that it failed.
    """
    coverage.db.admin.execute(
        f'REVOKE SELECT ("attempts") ON "{coverage.db.schema}"."{RECONCILIATION_TABLE}" '
        f'FROM "{coverage.role("debt")}"'
    )
    try:
        for name in ("cancellation_debt.drilldown", "cancellation_debt.summary"):
            ok, observed = _measure(coverage, SURFACE_BY_NAME[name])
            print(f"ROLLBACK {name:<34} {'ok  ' if ok else 'RED '} {observed}")
            assert not ok, f"{name} passed with the pre-#1038 four-column ledger grant"
            assert f"{RECONCILIATION_TABLE}.attempts (SELECT)" in observed, observed
            assert "cancellation_debt_reader_grants" in observed, observed
        # The sibling probe does not read the ledger's retry columns, so it stays green:
        # the counterexample is column-specific, not a blanket failure.
        ok, observed = _measure(coverage, SURFACE_BY_NAME["stop_evidence_debt"])
        assert ok, observed
    finally:
        _restore(coverage, "debt")


def _public_acl(db: JobDatabase) -> list[str]:
    """Every relation, column or schema ACL entry whose grantee is PUBLIC (oid 0)."""
    rows = db.query(
        "SELECT 'relation ' || c.relname AS entry FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace WHERE n.nspname = %s "
        "AND c.relacl IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM aclexplode(c.relacl) AS a WHERE a.grantee = 0) "
        "UNION ALL "
        "SELECT 'column ' || c.relname || '.' || attr.attname FROM pg_attribute AS attr "
        "JOIN pg_class AS c ON c.oid = attr.attrelid "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace WHERE n.nspname = %s "
        "AND attr.attnum > 0 AND NOT attr.attisdropped AND attr.attacl IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM aclexplode(attr.attacl) AS a WHERE a.grantee = 0) "
        "UNION ALL "
        "SELECT 'schema ' || n.nspname FROM pg_namespace AS n WHERE n.nspname = %s "
        "AND n.nspacl IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM aclexplode(n.nspacl) AS a WHERE a.grantee = 0)",
        (db.schema, db.schema, db.schema),
    )
    return [str(row["entry"]) for row in rows]


def _granted_relations(coverage: Coverage, role: str) -> tuple[str, ...]:
    """Every relation this identity can touch at table or column level, right now."""
    rows = coverage.db.query(
        "SELECT c.relname AS relname FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r','p','v') AND ("
        "has_table_privilege(%s, c.oid, 'SELECT') OR "
        "has_table_privilege(%s, c.oid, 'INSERT') OR "
        "has_table_privilege(%s, c.oid, 'UPDATE') OR "
        "has_table_privilege(%s, c.oid, 'DELETE') OR EXISTS ("
        "SELECT 1 FROM pg_attribute AS a WHERE a.attrelid = c.oid AND a.attnum > 0 "
        "AND NOT a.attisdropped AND ("
        "has_column_privilege(%s, c.oid, a.attnum, 'SELECT') OR "
        "has_column_privilege(%s, c.oid, a.attnum, 'INSERT') OR "
        "has_column_privilege(%s, c.oid, a.attnum, 'UPDATE')))) ORDER BY c.relname",
        (coverage.db.schema, role, role, role, role, role, role, role),
    )
    return tuple(str(row["relname"]) for row in rows)


def _grant_only_declared(coverage: Coverage, surface: Surface) -> None:
    """Replace one identity's grants with exactly what the face declares it reads.

    Everything the identity holds is revoked first, not just the declared relations:
    a face that reads an undeclared relation would otherwise pass on the deployment's
    wider grant and the declaration would silently stop describing the read set.
    """
    role = coverage.role(surface.role)
    for table in _granted_relations(coverage, role):
        _revoke_relation(coverage.db, table, role)
        # A PUBLIC grant survives every per-role REVOKE and would green the face.
        _revoke_relation(coverage.db, table)
    for read in surface.reads:
        if read.columns is None:
            statement = f'GRANT {read.privilege} ON "{coverage.db.schema}"."{read.table}"'
        else:
            columns = ", ".join(f'"{column}"' for column in read.columns)
            statement = (
                f'GRANT {read.privilege} ({columns}) ON "{coverage.db.schema}"."{read.table}"'
            )
        coverage.db.admin.execute(f'{statement} TO "{role}"')


def test_the_schema_grants_nothing_to_public(coverage: Coverage) -> None:
    """PUBLIC is not a role a face's DSN can be narrowed away from.

    ``REVOKE ... FROM <role>`` cannot take back a privilege the migration granted to
    ``PUBLIC``, so a future ``GRANT SELECT ... TO PUBLIC`` would let every negative
    case pass while the per-identity grants stayed narrow. Assert the schema has no
    such entry, so the coverage cases above are not greened by a shared grantee.
    """
    entries = _public_acl(coverage.db)
    assert not entries, (
        "schema grants privileges to PUBLIC: "
        + ", ".join(entries)
        + " -- a per-role REVOKE cannot remove it, so the per-face cases would pass on"
        " the PUBLIC grant; grant the relation to the face's own role instead"
    )


def test_the_declaration_is_enough_to_run_each_face(coverage: Coverage) -> None:
    """The declaration is the deployment contract: it alone must satisfy the statement.

    A declaration that grew stale would pass the positive case on the wider grant and
    still hide the column a future edit added. Granting exactly the declared reads --
    and nothing else the identity holds -- makes the contract itself the evidence.
    """
    for surface in SURFACES:
        revoked = _granted_relations(coverage, coverage.role(surface.role))
        _grant_only_declared(coverage, surface)
        try:
            ok, observed = _measure(coverage, surface)
        finally:
            _restore(coverage, surface.role)
        print(
            f"DECLARED {surface.name:<34} revoked={len(revoked):<3} "
            f"{'ok  ' if ok else 'FAIL'} {observed}"
        )
        assert ok, observed
