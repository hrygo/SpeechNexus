"""#852: a cancel that cannot confirm its stop must not keep an instance slot.

The observed reference-deployment window ended with

    speech_jobs.state           cancelling
    admission_leases            reserved / pending
    submit_intents              unknown_quarantined / quarantined
    engine_instances.held_slots 1

and nothing advanced it afterwards: the accepted-cancellation reconciliation
worker recorded ``exhausted`` in its private ledger, stopped claiming the row,
and no other component owns ``cancelling``. The slot stayed reserved forever
while the public surface still answered ``202 cancelling``.

This module drives the production participants against real PostgreSQL: the real
reconciliation ledger, the real gateway document translation, the real
``PublicJobs`` terminal transition and the real ``LeaseService`` quarantine. The
gateway HTTP client and the stop facts are fakes; nothing here is engine or GPU
evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import AnyHttpUrl, SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.app import create_app
from speechnexus_backend.job_cancellation_runtime import (
    UnconfirmedCancellationSettlement,
    _PublicJobsSettlement,
)
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.job_cancellation import CancellationCapacity
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    JobCancellationReconciler,
)
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.domain.public_jobs import cleanup_state, retryable
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_cancellation_confirmation import (
    GatewayCancellationConfirmer,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence import (
    write_cancellation_fence,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    RECONCILIATION_TABLE,
    cancellation_debt_reader_grants,
    cancellation_reconciliation_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    PostgresCancellationReconciliationStore,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresLeaseSettler,
)
from speechnexus_backend.speech.infrastructure.job_schema import job_runtime_grants
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    PostgresPublicJobs,
)
from speechnexus_backend.speech.infrastructure.qualified_speech_facts_postgres import (
    speech_synthesis_reader_grants,
)
from speechnexus_backend.speech.infrastructure.readiness import CancellationDebtProbe

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_cancel_failure_test"
DIGEST = "sha256:" + "a" * 64
WORKSPACE = "workspace-test"
INSTANCE = "instance-test"
LEASE = "lease-test-stage-main"
LEASE_SECOND = "lease-test-stage-render"
# A fixed acceptance instant so the confirmation window in these cases is
# deterministic: 10 s of wall clock, then the acceptance is judged.
ACCEPTED_AT_US = 10_000_000
WINDOW_US = 10_000_000
# One accepted cancellation with no provable stop: the reference window's facts.
QUARANTINED_DOCUMENT_STATE = ("unknown_quarantined", "quarantined")
ACTIVE_DOCUMENT_STATE = ("executing", "pending")


@dataclass
class CancelSandbox:
    schema: str
    role: str
    job_id: str
    lease_ids: tuple[str, ...]
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)
    pool: Any = field(repr=False)

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return list(cursor.fetchall()) if cursor.description else []

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)

    def rows(self) -> dict[str, Any]:
        """The three tables the issue names, plus the private cancellation ledger."""
        jobs = self.query("SELECT job_id, state, error_code, status_version FROM speech_jobs")
        leases = self.query(
            "SELECT lease_id, operation_id, lease_state, cleanup_state, version "
            "FROM admission_leases ORDER BY lease_id"
        )
        instances = self.query(
            "SELECT instance_id, held_slots, quarantined_slots FROM engine_instances"
        )
        quotas = self.query("SELECT held_attempts, quarantined_attempts FROM workspace_quotas")
        intents = self.query(
            "SELECT execution_state, cleanup_state, error_code FROM submit_intents "
            "ORDER BY submit_intent_id"
        )
        ledger = self.query(
            f"SELECT job_id, attempts, last_outcome, settled_at_us FROM {RECONCILIATION_TABLE}"
        )
        return {
            "speech_jobs": jobs,
            "admission_leases": leases,
            "engine_instances": instances,
            "workspace_quotas": quotas,
            "submit_intents": intents,
            "ledger": ledger,
        }

    def jobs(self) -> PublicJobs:
        adapter = PostgresPublicJobs(self.pool, timeout_seconds=3)
        return PublicJobs(adapter, adapter)

    def capacity(self) -> PostgresLeaseSettler:
        return PostgresLeaseSettler(self.pool, timeout_seconds=3)


def _migrate(url: str, schema: str) -> None:
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def _seed_foundation(
    admin: psycopg.Connection[dict[str, Any]], job_id: str, lease_ids: tuple[str, ...]
) -> None:
    """Reserved leases holding instance slots, exactly as admission leaves them."""
    slots = len(lease_ids)
    admin.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('tts-realtime', 'realtime', 'active')"
    )
    admin.execute(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, held_attempts, policy_revision) "
        "VALUES (%s, 8, 1, 'policy-test')",
        (WORKSPACE,),
    )
    admin.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest, "
        "allocatable_unified_physical_memory_bytes, held_unified_physical_memory_bytes) "
        "VALUES ('host-test', 'unified_memory', 'active', %s, 100, %s)",
        (DIGEST, slots),
    )
    admin.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots, held_slots) VALUES "
        "(%s, 1, 'host-test', 'tts-realtime', 'gateway-test', 'binding-test', 'manifest-test', "
        "'template-test', 'managed', 'on_demand', %s, 'ready', %s, %s)",
        (INSTANCE, DIGEST, slots, slots),
    )
    admin.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) VALUES (%s, 'attempt-test', %s, 'binding-test', "
        "'manifest-test', %s, 'policy-test', 'realtime', %s, %s)",
        (job_id, WORKSPACE, DIGEST, DIGEST, json.dumps(list(lease_ids))),
    )
    for lease_id in lease_ids:
        admin.execute(
            "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
            "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
            "reserved_slots, deadline_at_us, reserved_unified_physical_memory_bytes) VALUES "
            "(%s, %s, 'attempt-test', %s, 'host-test', %s, 1, 'tts-realtime', "
            "'gateway-test', 1, 1, 1, 1)",
            (lease_id, job_id, lease_id, INSTANCE),
        )


def _seed_execution(
    admin: psycopg.Connection[dict[str, Any]],
    job_id: str,
    lease_id: str,
    facts: tuple[str, str],
    *,
    identity: str,
) -> None:
    """The gateway's own durable fact for one lease's submit intent.

    ``identity`` is the intent's stable id: the first lease is seeded under the
    Job-level identity because the confirmation port addresses one submit intent
    per operation, while isolation reads every lease's own row.
    """
    admin.execute(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
        "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
        "updated_at_us, output_stream_id, sample_rate_hz, execution_state, cleanup_state, "
        "error_code) VALUES (%s, %s, %s, 'attempt-test', %s, %s, '{}', 1, 1, 1, "
        "'tts-main', 24000, %s, %s, 'execution_cleanup_pending')",
        (identity, lease_id, job_id, DIGEST, DIGEST, facts[0], facts[1]),
    )


@contextmanager
def _sandbox(
    *leases: tuple[str, tuple[str, str] | None],
    coordination: bool = True,
    api_identity: bool = False,
    debt_reader: bool = False,
) -> Iterator[CancelSandbox]:
    """One Job whose leases carry the given gateway execution facts.

    ``None`` means that lease never reached submission, so the gateway recorded
    no execution fact for it at all -- the review's "one lease without the fact"
    case. ``coordination=False`` provisions the role the way a minimal-privilege
    deployment does: the Job, cancellation and ledger tables are granted, the
    lease and execution tables are not. ``api_identity=True`` reproduces the
    reference deployment's ``sn_api`` grant set exactly, and ``debt_reader=True``
    the optional read-only identity the debt detail documents.
    """
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not evidence")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_cancel_" + suffix, "sn_cancel_role_" + suffix
    job_id = "job-" + uuid.uuid4().hex
    lease_ids = tuple(lease_id for lease_id, _ in leases)
    _migrate(url, schema)
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    pool = None
    try:
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        if api_identity:
            # Exactly what deploy/README.md gives `sn_api`, nothing else.
            for grant in speech_synthesis_reader_grants(schema, role):
                admin.execute(grant)
        elif debt_reader:
            for grant in cancellation_debt_reader_grants(schema, role):
                admin.execute(grant)
        else:
            for grant in (
                *job_runtime_grants(schema, role),
                *public_job_grants(schema, role),
                *cancellation_reconciliation_grants(schema, role),
            ):
                admin.execute(grant)
        if coordination:
            for grant in coordination_runtime_grants(
                schema, speech_role=role, gateway_role=role + "_unused"
            ):
                if f'"{role}"' in grant:
                    admin.execute(grant)
        _seed_foundation(admin, job_id, lease_ids)
        for index, (lease_id, facts) in enumerate(leases):
            if facts is not None:
                _seed_execution(
                    admin,
                    job_id,
                    lease_id,
                    facts,
                    identity=submit_intent_identity(job_id if index == 0 else lease_id),
                )
        admin.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "state, status_version, created_at_us, updated_at_us) VALUES (%s, %s, 'tts', "
            "'tts-quality', %s, 'cancelling', 2, 1, 1)",
            (job_id, WORKSPACE, DIGEST),
        )
        admin.execute(
            "INSERT INTO speech_job_submissions (job_id, request_id, client_reference, "
            "effective_configuration_json, execution_revision, progress_unit, "
            "progress_total, submitted_at_us) VALUES (%s, 'request-852', NULL, '{}', "
            "'1', 'text_codepoints', 1, 1)",
            (job_id,),
        )
        pool = speech_pool(
            make_conninfo(url, user=role, password=PASSWORD),
            schema=schema,
            max_size=2,
            name="cancel-failure-" + suffix,
        )
        pool.open()
        # Seeded as the schema owner: the acceptance row is fixture state, and the
        # identity under test may legitimately be unable to write or read it.
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        with admin.cursor() as cursor:
            write_cancellation_fence(
                cursor,
                job_id=job_id,
                request_id="request-852",
                accepted_at_us=ACCEPTED_AT_US,
            )
        yield CancelSandbox(schema, role, job_id, lease_ids, admin, pool)
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


@pytest.fixture
def sandbox() -> Iterator[CancelSandbox]:
    """One Job, one lease, and the reference window's quarantined execution."""
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        yield box


@pytest.fixture
def two_leases() -> Iterator[CancelSandbox]:
    """One Job holding two instance slots, both executions quarantined."""
    with _sandbox(
        (LEASE, QUARANTINED_DOCUMENT_STATE), (LEASE_SECOND, QUARANTINED_DOCUMENT_STATE)
    ) as box:
        yield box


class _DocumentClient:
    """Serves the gateway document for the Job-level submit intent."""

    def __init__(self, job_id: str, facts: tuple[str, str] = QUARANTINED_DOCUMENT_STATE) -> None:
        self._job_id, self._facts = job_id, facts
        self.calls: list[str] = []

    async def __aenter__(self) -> _DocumentClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def query_document(self, submit_intent_id: str) -> dict[str, Any]:
        self.calls.append(submit_intent_id)
        return {
            "submit_intent_id": submit_intent_identity(self._job_id),
            "operation_id": self._job_id,
            "attempt_id": "attempt-test",
            "execution_state": self._facts[0],
            "cleanup_state": self._facts[1],
            "error_code": "execution_cleanup_pending",
            "stop_evidence_kind": "unconfirmed_timeout",
            "dispatched_at": "2026-11-16T00:00:00Z",
            "engine_request_id": "engine-request-test",
        }


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    job_id: str,
    facts: tuple[str, str] = QUARANTINED_DOCUMENT_STATE,
) -> _DocumentClient:
    client = _DocumentClient(job_id, facts)
    monkeypatch.setattr(
        GatewayExecutionClient,
        "from_settings",
        classmethod(lambda cls, settings: client),
    )
    return client


def _reconciler(
    box: CancelSandbox, *, confirm_timeout_seconds: float = 300, max_attempts: int = 3
) -> JobCancellationReconciler:
    jobs, capacity = box.jobs(), box.capacity()
    return JobCancellationReconciler(
        store=PostgresCancellationReconciliationStore(box.pool),
        confirmer=GatewayCancellationConfirmer(
            object(),  # type: ignore[arg-type]
            _PublicJobsSettlement(jobs, CancellationCapacity(capacity.release_sync)),
        ),
        unconfirmed=UnconfirmedCancellationSettlement(jobs, capacity),
        max_attempts=max_attempts,
        batch_size=8,
        min_age_seconds=0,
        confirm_timeout_seconds=confirm_timeout_seconds,
        base_backoff_seconds=1,
        max_backoff_seconds=4,
        budget_seconds=10,
    )


def test_unconfirmed_cancel_is_judged_and_the_slot_leaves_the_held_bucket(
    sandbox: CancelSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the window the worker asks; past it the acceptance is terminal.

    Before #852 the retry sequence ended with `exhausted`, `state=cancelling`,
    `lease_state=reserved` and `held_slots=1` forever.
    """
    before = sandbox.rows()
    assert before["speech_jobs"] == [
        {
            "job_id": sandbox.job_id,
            "state": "cancelling",
            "error_code": None,
            "status_version": 2,
        }
    ]
    assert before["admission_leases"] == [
        {
            "lease_id": LEASE,
            "operation_id": sandbox.job_id,
            "lease_state": "reserved",
            "cleanup_state": "pending",
            "version": 1,
        }
    ]
    assert before["engine_instances"] == [
        {
            "instance_id": INSTANCE,
            "held_slots": 1,
            "quarantined_slots": 0,
        }
    ]
    client = _install_client(monkeypatch, sandbox.job_id)
    reconciler = _reconciler(sandbox, confirm_timeout_seconds=WINDOW_US / 1_000_000)

    inside = asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + 1_000_000))
    assert (inside.claimed, inside.settled, inside.exhausted) == (1, 0, 0)
    assert client.calls == [submit_intent_identity(sandbox.job_id)]
    assert sandbox.rows()["speech_jobs"][0]["state"] == "cancelling"

    judged = asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
    after = sandbox.rows()

    # No further stop attempt is spent once the window has elapsed.
    assert (judged.claimed, judged.settled, judged.exhausted) == (1, 0, 1)
    assert len(client.calls) == 1
    assert after["speech_jobs"] == [
        {
            "job_id": sandbox.job_id,
            "state": "failed",
            "error_code": "cancellation_unconfirmed",
            "status_version": 3,
        }
    ]
    # The slot is no longer reported as held by a live execution; it is isolated
    # and remains charged to the instance until a verified stop releases it.
    assert after["engine_instances"] == [
        {
            "instance_id": INSTANCE,
            "held_slots": 0,
            "quarantined_slots": 1,
        }
    ]
    assert after["workspace_quotas"] == [{"held_attempts": 0, "quarantined_attempts": 1}]
    assert after["admission_leases"] == [
        {
            "lease_id": LEASE,
            "operation_id": sandbox.job_id,
            "lease_state": "quarantined",
            "cleanup_state": "quarantined",
            "version": 2,
        }
    ]
    assert after["ledger"] == [
        {
            "job_id": sandbox.job_id,
            "attempts": 2,
            "last_outcome": "exhausted",
            "settled_at_us": None,
        }
    ]
    # The public reading now states the truth: a terminal failure whose cleanup
    # is quarantined, and a retry that is refused instead of silently accepted.
    job = sandbox.jobs().get(sandbox.job_id)
    assert job is not None
    assert cleanup_state(job) == "quarantined"
    assert retryable(job) is False
    # The judgment did not rewrite the gateway's own execution fact.
    assert after["submit_intents"] == [
        {
            "execution_state": "unknown_quarantined",
            "cleanup_state": "quarantined",
            "error_code": "execution_cleanup_pending",
        }
    ]


def test_every_lease_of_the_job_is_isolated(
    two_leases: CancelSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#852 review P1: isolation must not stop at the first lease it can move.

    The previous adapter failed fast on any refused lease, so a second lease that
    *was* isolatable stayed reserved while its Job was closed as if nothing were
    held.
    """
    _install_client(monkeypatch, two_leases.job_id)
    reconciler = _reconciler(two_leases, confirm_timeout_seconds=WINDOW_US / 1_000_000)
    asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + 1_000_000))

    judged = asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
    after = two_leases.rows()

    assert (judged.exhausted, judged.blocked) == (1, ())
    assert [(lease["lease_id"], lease["lease_state"]) for lease in after["admission_leases"]] == [
        (LEASE, "quarantined"),
        (LEASE_SECOND, "quarantined"),
    ]
    assert after["engine_instances"] == [
        {"instance_id": INSTANCE, "held_slots": 0, "quarantined_slots": 2}
    ]
    # The attempt quota moves once per admission attempt, not once per lease.
    assert after["workspace_quotas"] == [{"held_attempts": 0, "quarantined_attempts": 1}]
    assert after["speech_jobs"][0]["state"] == "failed"


def test_one_lease_without_the_execution_fact_blocks_the_terminal_judgment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#852 review P1: a partially isolated Job must not be closed as isolated.

    The second lease never reached submission, so the gateway holds no execution
    fact for it and the domain refuses its quarantine. The Job must stay
    `cancelling`, the ledger must stay claimable, and the block must be named --
    and once the fact arrives the same claim completes the isolation.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), (LEASE_SECOND, None)) as box:
        _install_client(monkeypatch, box.job_id)
        reconciler = _reconciler(box, confirm_timeout_seconds=WINDOW_US / 1_000_000)
        asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + 1_000_000))

        blocked = asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
        after = box.rows()

        assert (blocked.exhausted, blocked.retried) == (0, 1)
        assert [(item.job_id, item.detail) for item in blocked.blocked] == [
            (
                box.job_id,
                f"1 lease(s) not isolated: {LEASE_SECOND} lease=reserved "
                "execution=not_submitted (execution_cleanup_pending)",
            )
        ]
        # The Job is not described as a clean terminal: it is still cancelling and
        # its public cleanup state still says so.
        assert after["speech_jobs"][0]["state"] == "cancelling"
        job = box.jobs().get(box.job_id)
        assert job is not None
        assert cleanup_state(job) == "pending"
        # One lease moved, one did not: the accounting says exactly that.
        assert after["admission_leases"] == [
            {
                "lease_id": LEASE,
                "operation_id": box.job_id,
                "lease_state": "quarantined",
                "cleanup_state": "quarantined",
                "version": 2,
            },
            {
                "lease_id": LEASE_SECOND,
                "operation_id": box.job_id,
                "lease_state": "reserved",
                "cleanup_state": "pending",
                "version": 1,
            },
        ]
        assert after["engine_instances"] == [
            {"instance_id": INSTANCE, "held_slots": 1, "quarantined_slots": 1}
        ]
        assert after["ledger"] == [
            {
                "job_id": box.job_id,
                "attempts": 2,
                "last_outcome": "stop_unconfirmed",
                "settled_at_us": None,
            }
        ]

        # A restart claims the row again: the block is an external condition, not
        # a final answer, so it survives process loss.
        restarted = _reconciler(box, confirm_timeout_seconds=WINDOW_US / 1_000_000)
        still = asyncio.run(restarted.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 5_000_000))
        assert (still.claimed, still.retried) == (1, 1)
        assert still.blocked

        # The gateway finally records the fact for the second lease; the next
        # claim isolates it and closes the Job. No manual SQL involved.
        _seed_execution(
            box.admin,
            box.job_id,
            LEASE_SECOND,
            QUARANTINED_DOCUMENT_STATE,
            identity=submit_intent_identity(LEASE_SECOND),
        )
        closed = asyncio.run(_reconciler(box).run_once(now_us=ACCEPTED_AT_US + 4_000_000_000))
        final = box.rows()

        assert (closed.exhausted, closed.blocked) == (1, ())
        assert final["speech_jobs"][0]["state"] == "failed"
        assert final["engine_instances"] == [
            {"instance_id": INSTANCE, "held_slots": 0, "quarantined_slots": 2}
        ]
        assert [lease["lease_state"] for lease in final["admission_leases"]] == [
            "quarantined",
            "quarantined",
        ]


def test_an_execution_that_is_still_running_is_reported_not_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#852 review P1: no terminal while the execution may still be running.

    The lease cannot be quarantined (the execution never proved its stop), so the
    Job keeps `cancelling`, `cleanup_state` keeps saying `pending`, the slot stays
    in the held bucket, and the block is named. Writing the terminal here would
    report `cleanup_state=quarantined` for capacity that is still in use.
    """
    with _sandbox((LEASE, ACTIVE_DOCUMENT_STATE)) as box:
        _install_client(monkeypatch, box.job_id, ACTIVE_DOCUMENT_STATE)
        reconciler = _reconciler(box, confirm_timeout_seconds=WINDOW_US / 1_000_000)
        asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + 1_000_000))

        blocked = asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
        after = box.rows()

        assert (blocked.exhausted, blocked.retried) == (0, 1)
        assert "executing" in blocked.blocked[0].detail
        assert after["speech_jobs"][0]["state"] == "cancelling"
        assert after["engine_instances"] == [
            {"instance_id": INSTANCE, "held_slots": 1, "quarantined_slots": 0}
        ]
        assert after["admission_leases"][0]["lease_state"] == "reserved"
        assert after["ledger"][0]["last_outcome"] == "stop_unconfirmed"
        job = box.jobs().get(box.job_id)
        assert job is not None
        assert cleanup_state(job) == "pending"
        assert retryable(job) is False


def _debt_probe(box: CancelSandbox) -> CancellationDebtProbe:
    return CancellationDebtProbe(
        make_conninfo(os.environ[VARIABLE], user=box.role, password=PASSWORD),
        schema=box.schema,
        timeout_seconds=3,
        grace_seconds=0.001,
    )


def _add_acceptance(
    box: CancelSandbox,
    *,
    ledger: tuple[str, int, int] | None = None,
    job_id: str | None = None,
) -> str:
    """A second accepted cancellation: Job and fence, plus an optional ledger row.

    ``ledger`` is ``(last_outcome, attempts, next_check_at_us)``. The extra Job holds
    no lease, so the full read degrades it to ``no-held-lease``: the point is that the
    schedule scopes count Jobs, not leases, and several Jobs can share one schema.
    """
    job_id = job_id or "job-" + uuid.uuid4().hex
    box.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) VALUES (%s, %s, 'tts', "
        "'tts-quality', %s, 'cancelling', 2, 1, 1)",
        (job_id, WORKSPACE, DIGEST),
    )
    box.execute(
        "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id) "
        "VALUES (%s, %s, 'request-852')",
        (job_id, ACCEPTED_AT_US),
    )
    if ledger is not None:
        outcome, attempts, next_check_at_us = ledger
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) VALUES (%s, %s, %s, %s, NULL, 1)",
            (job_id, attempts, next_check_at_us, outcome),
        )
    return job_id


def test_the_readiness_detail_names_the_blocking_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#852 review P1: an acceptance that cannot return capacity stays readable.

    The detail is the non-SQL exit: it names the Job, the lease and both states,
    so an operator knows which gateway stop to request. It also proves the
    probe's predicate catches an unclaimed fence, not just a retried one.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), (LEASE_SECOND, None)) as box:
        # A deployment that never ran a worker -- a fence with no ledger row -- is
        # unclaimed debt, not a settled acceptance.
        before = asyncio.run(_debt_probe(box).check())
        assert before is not None
        assert before.startswith("cancellation_debt: 1 accepted cancellation(s)")

        _install_client(monkeypatch, box.job_id)
        asyncio.run(
            _reconciler(box, confirm_timeout_seconds=WINDOW_US / 1_000_000).run_once(
                now_us=ACCEPTED_AT_US + WINDOW_US + 1
            )
        )
        detail = asyncio.run(_debt_probe(box).check())

        assert detail is not None
        assert detail.startswith("cancellation_debt: 1 accepted cancellation(s)")
        assert (
            f"{box.job_id} -> {LEASE_SECOND}(lease=reserved, execution=not_submitted, "
            "subclass=execution-unknown)" in detail
        )


def test_the_detail_says_when_no_worker_has_ever_claimed_the_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#852: "still holds capacity" alone reads as progress; ``never claimed`` says it is not.

    This is the no-worker deployment shape -- a fence with no reconciliation-ledger
    row -- not the reference window: the reference window's own ledger row is
    ``exhausted`` (see this module's docstring and
    ``docs/reviews/2026-11-16-cancel-failure-slot-release.md``), which
    ``test_the_reference_window_shape_is_never_claimable_again`` covers. The mark is
    historical: the first claim removes it, and its absence never proves a worker is
    running.
    """
    with _sandbox((LEASE, ACTIVE_DOCUMENT_STATE)) as box:
        unclaimed = asyncio.run(_debt_probe(box).check())
        assert unclaimed is not None
        assert unclaimed.startswith("cancellation_debt: 1 accepted cancellation(s)")
        assert "(held leases: execution-unknown=1; never claimed: 1)" in unclaimed
        assert "never claimable" not in unclaimed
        assert "due now" not in unclaimed

        _install_client(monkeypatch, box.job_id, ACTIVE_DOCUMENT_STATE)
        reconciler = _reconciler(box, confirm_timeout_seconds=WINDOW_US / 1_000_000)
        asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + 1_000_000))
        asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))

        # A worker has now claimed the acceptance and recorded why it is blocked: the
        # historical mark is gone, and the blocked lease is still named in full.
        claimed = asyncio.run(_debt_probe(box).check())
        assert claimed is not None
        assert claimed.startswith("cancellation_debt: 1 accepted cancellation(s)")
        assert "never claimed" not in claimed
        assert "never claimable" not in claimed
        assert (
            f"{box.job_id} -> {LEASE}(lease=reserved, execution=executing, "
            "subclass=execution-unknown)" in claimed
        )
        assert box.rows()["ledger"][0]["last_outcome"] == "stop_unconfirmed"


def test_the_reference_window_shape_is_never_claimable_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#852 reference window: fence present, ledger row ``exhausted``, restart ``claimed=0``.

    ``docs/reviews/2026-11-16-cancel-failure-slot-release.md`` records the window's
    ledger as ``last_outcome=exhausted`` with ``settled_at_us=NULL`` and a restart as
    ``claimed=0``: the claim predicate excludes ``exhausted``, so nothing can advance
    it. The read face must say that instead of looking exactly like a blocked row a
    worker will retry -- and rerunning the worker must stay a no-op.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) "
            "VALUES (%s, 8, 0, 'exhausted', NULL, 1)",
            (box.job_id,),
        )

        detail = asyncio.run(_debt_probe(box).check())
        assert detail is not None
        assert "(held leases: execution-unknown=1; never claimable (exhausted): 1)" in detail
        assert "never claimed" not in detail
        assert "due now" not in detail

        _install_client(monkeypatch, box.job_id)
        batch = asyncio.run(_reconciler(box).run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
        assert (batch.claimed, batch.settled) == (0, 0)


def test_a_claimable_acceptance_is_reported_as_due_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger row that is claimable at this instant is named as such.

    ``due now`` is the post-first-claim no-worker shape: the row is scheduled, so the
    detail must not read as "a worker is advancing it". The same predicate that reports
    it is the one the worker claims with, so a healthy worker also lands here between
    polls -- only a mark that persists says nobody is advancing.
    """
    with _sandbox((LEASE, ACTIVE_DOCUMENT_STATE)) as box:
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) "
            "VALUES (%s, 1, 0, 'pending', NULL, 1)",
            (box.job_id,),
        )

        detail = asyncio.run(_debt_probe(box).check())
        assert detail is not None
        assert "(held leases: execution-unknown=1; due now: 1)" in detail
        assert "never claimed" not in detail
        assert "never claimable" not in detail

        _install_client(monkeypatch, box.job_id, ACTIVE_DOCUMENT_STATE)
        batch = asyncio.run(_reconciler(box).run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
        assert batch.claimed == 1


def test_a_future_check_is_not_due_now_and_not_claimable_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger row scheduled in the future is neither ``due now`` nor claimable.

    ``next_check_at_us`` is a schedule, not a claim stamp: until it arrives the row
    says nothing about who is advancing it, so the detail must not print ``due now``.
    This is the negative half of ``due now`` -- without it, a CASE that treats any
    non-NULL schedule as due would still pass every positive case, because those
    rows are all in the past. The row is also not ``never claimed`` (a ledger row
    exists) and not ``never claimable (exhausted)`` (the worker will select it).
    """
    with _sandbox((LEASE, ACTIVE_DOCUMENT_STATE)) as box:
        # One hour past the database clock, which is the clock both the probe's
        # ``due`` branch and its grace filter read.
        box.execute(
            f"INSERT INTO {RECONCILIATION_TABLE} (job_id, attempts, next_check_at_us, "
            "last_outcome, settled_at_us, updated_at_us) VALUES (%s, 1, "
            "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) + 3600000000, "
            "'pending', NULL, 1)",
            (box.job_id,),
        )

        scheduled = asyncio.run(_debt_probe(box).check())
        assert scheduled is not None
        assert scheduled.startswith("cancellation_debt: 1 accepted cancellation(s)")
        assert "due now" not in scheduled
        assert "never claimed" not in scheduled
        assert "never claimable" not in scheduled
        # No schedule scope at all: the drill-down follows the lease scope directly.
        assert "(held leases: execution-unknown=1): " in scheduled

        _install_client(monkeypatch, box.job_id, ACTIVE_DOCUMENT_STATE)
        reconciler = _reconciler(box)
        # The row is scheduled one hour past the database clock the probe reads; this
        # explicit claim clock stays far earlier than that instant, so both readers
        # agree it is not due until the UPDATE below brings the schedule forward.
        now_us = ACCEPTED_AT_US + WINDOW_US + 1
        before = asyncio.run(reconciler.run_once(now_us=now_us))
        assert before.claimed == 0

        # The schedule arrives: the same worker and clock now select the row.
        box.execute(
            f"UPDATE {RECONCILIATION_TABLE} SET next_check_at_us = 0 WHERE job_id = %s",
            (box.job_id,),
        )
        after = asyncio.run(reconciler.run_once(now_us=now_us))
        assert after.claimed == 1


def test_the_schedule_scopes_count_jobs_across_a_mixed_deployment() -> None:
    """Three ledger facts on one read: exact per-Job counts, independent of lease rows.

    The main Job holds two leases and has no ledger row, so it must count once under
    ``never claimed``; the extra Jobs add one more never-claimed, two exhausted and two
    due. A constant count cannot satisfy this, and counting ledger rows instead of Jobs
    cannot either.
    """
    with _sandbox((LEASE, ACTIVE_DOCUMENT_STATE), (LEASE_SECOND, None)) as box:
        _add_acceptance(box)  # never claimed
        _add_acceptance(box, ledger=("exhausted", 8, 0))
        _add_acceptance(box, ledger=("exhausted", 8, 0))
        _add_acceptance(box, ledger=("pending", 1, 0))
        _add_acceptance(box, ledger=("pending", 1, 0))

        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert detail.startswith("cancellation_debt: 6 accepted cancellation(s) still hold capacity")
    assert "over 2 held lease(s)" in detail
    assert (
        "(held leases: execution-unknown=2; jobs: no-held-lease=5; "
        "never claimed: 2; never claimable (exhausted): 2; due now: 2)" in detail
    )


def test_reading_the_debt_leaves_the_capacity_account_untouched() -> None:
    """The probe is read-only: capacity, leases and the ledger must not move."""
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        _add_acceptance(box, ledger=("exhausted", 8, 0))
        _add_acceptance(box, ledger=("pending", 1, 0))
        before = box.rows()
        detail = asyncio.run(_debt_probe(box).check())
        after = box.rows()

    assert detail is not None
    assert before["engine_instances"] == [
        {"instance_id": INSTANCE, "held_slots": 1, "quarantined_slots": 0}
    ]
    assert before == after


def test_the_schedule_counts_survive_detail_truncation() -> None:
    """Many long Job ids truncate the drill-down, never the counted scopes."""
    with _sandbox((LEASE, ACTIVE_DOCUMENT_STATE)) as box:
        for _ in range(39):
            _add_acceptance(box, job_id="job-" + uuid.uuid4().hex + "-" + "x" * 72)
        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert len(detail) <= 512
    assert detail.endswith("... (truncated)")
    assert "never claimed: 40" in detail


def test_a_job_already_left_cancelling_is_documented_as_a_blind_spot() -> None:
    """#852 residual, pinned: capacity held after the Job left ``cancelling``.

    Both debt reads are gated on ``state = 'cancelling'`` because that is the only
    shape the live-state index (migration 0056) can reach; a Job that closed as
    ``cancelled`` while its release was refused, or was failed by another owner
    while the lease stayed reserved, is therefore *not* in the detail. This case
    pins the current behaviour so the documented drill-down query stays the only
    read surface for it, and states what closing it needs (a held-lease access
    path, i.e. another migration).
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE)) as box:
        jobs = box.jobs()
        current = jobs.get(box.job_id)
        assert current is not None
        assert (
            jobs.settle_cancelled(box.job_id, expected_version=current.status_version).state
            == "cancelled"
        )

        after = box.rows()
        assert after["speech_jobs"][0]["state"] == "cancelled"
        # Capacity is still held: the release this worker would retry never ran.
        assert after["admission_leases"][0]["lease_state"] == "reserved"
        assert after["engine_instances"] == [
            {"instance_id": INSTANCE, "held_slots": 1, "quarantined_slots": 0}
        ]
        # ... and the polled read surface does not name it (the pinned limitation).
        assert asyncio.run(_debt_probe(box).check()) is None

        named = box.query(
            "SELECT j.job_id FROM speech_job_cancellation_fences AS f "
            "JOIN speech_jobs AS j ON j.job_id = f.job_id "
            "JOIN admission_leases AS l ON l.operation_id = f.job_id "
            "WHERE l.lease_state NOT IN ('released', 'quarantined')"
        )
        assert named == [{"job_id": box.job_id}]


def test_lease_attempts_saturate_at_64_and_stay_claimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#852 known limitation: the claim clamp must stay a schedule, not a counter.

    ``LEAST(attempts + 1, 64)`` exists because a blocked acceptance must stay
    claimable indefinitely; reverting it to ``attempts + 1`` would surface as a
    CHECK violation hours later, when the default backoff reaches the bound.
    Seeded at the bound so the regression is visible in one pass.
    """
    with _sandbox((LEASE, ACTIVE_DOCUMENT_STATE)) as box:
        _install_client(monkeypatch, box.job_id, ACTIVE_DOCUMENT_STATE)
        reconciler = _reconciler(box, confirm_timeout_seconds=WINDOW_US / 1_000_000)
        asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + 1_000_000))
        box.execute(
            f"UPDATE {RECONCILIATION_TABLE} SET attempts = 64, next_check_at_us = 0 "
            "WHERE job_id = %s",
            (box.job_id,),
        )

        first = asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
        # The claim lease plus the 300 s maximum backoff must elapse before the next pass
        # may claim the row again; a claim that never becomes due again is the regression.
        second = asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 301_000_000))

        assert (first.claimed, second.claimed) == (1, 1)
        assert box.rows()["ledger"][0]["attempts"] == 64


def test_a_role_without_the_coordination_grants_still_names_the_job() -> None:
    """The debt probe degrades instead of failing under a minimal-privilege role.

    The reference runtime role is granted the Job, cancellation and ledger tables
    it writes; it is *not* granted the speech owner's ``admission_leases`` or the
    gateway's ``submit_intents`` (they hold another owner's rows and the canonical
    private command document). Joining them raised ``permission denied``, which the
    readiness service turned into ``/readyz`` 503 -- the whole API unhealthy
    because a diagnostic could not read a table it must never be granted.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False) as box:
        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    assert detail.startswith("cancellation_debt: 1 accepted cancellation(s)")
    assert f"{box.job_id} -> lease-unread" in detail
    # The Job-level path carries the same ledger fact: a downgraded role must not lose
    # the one mark that says nothing has ever claimed this acceptance.
    assert "(jobs: lease-unread=1; never claimed: 1)" in detail
    assert "[lease detail unavailable: permission]" in detail


def test_the_debt_probe_never_fails_a_readiness_report() -> None:
    """No probe failure may make ``/readyz`` unready; the detail names the cause."""
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False) as box:
        unreadable = asyncio.run(
            ReadinessService(
                [
                    CancellationDebtProbe(
                        make_conninfo(os.environ[VARIABLE], user=box.role, password=PASSWORD),
                        schema=box.schema,
                        timeout_seconds=3,
                        grace_seconds=0.001,
                    )
                ],
                timeout_seconds=3,
            ).inspect()
        )
    assert unreadable.ready is True
    assert unreadable.checks[0].status.value == "ok"
    assert unreadable.checks[0].detail is not None
    assert unreadable.checks[0].detail.startswith("cancellation_debt:")

    # A DSN that cannot be opened at all is a probe fact, not an unready API.
    unreachable = asyncio.run(
        ReadinessService(
            [
                CancellationDebtProbe(
                    "postgresql://nobody:nobody@127.0.0.1:1/none",
                    schema="speech",
                    timeout_seconds=1,
                )
            ],
            timeout_seconds=2,
        ).inspect()
    )
    assert unreachable.ready is True
    assert unreachable.checks[0].detail == "cancellation_debt: unavailable (connection)"


def test_readyz_and_diagnostics_stay_reachable_when_the_probe_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP surface an operator uses must not collapse with the probe.

    ``/readyz`` is the deployment healthcheck and diagnostics is the read surface;
    a role that is not granted the lease tables must leave both serving, with the
    degraded detail saying which layer this slice reaches.
    """
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False) as box:
        probe = _debt_probe(box)
        app = create_app(
            settings=_http_settings(box),
            readiness=ReadinessService([probe], timeout_seconds=3),
        )

        async def fetch(path: str) -> tuple[int, dict[str, Any]]:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(path)
            return response.status_code, cast(dict[str, Any], response.json())

        status, ready = asyncio.run(fetch("/readyz"))
        assert status == 200, ready
        assert ready["status"] == "ready"
        detail = next(
            check["detail"] for check in ready["checks"] if check["name"] == "cancellation_debt"
        )
        assert detail.startswith("cancellation_debt: 1 accepted cancellation(s)")
        assert "[lease detail unavailable: permission]" in detail

        status, diagnostics = asyncio.run(fetch("/v1/nexus/admin/diagnostics"))
        assert status == 200, diagnostics


def test_the_reference_api_identity_keeps_readyz_ok_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reference deployment's ``sn_api`` grant set, not a union sandbox role.

    ``sn_api`` holds exactly ``speech_synthesis_reader_grants`` (catalogue,
    qualification, bindings, voices, settlement root, workspaces) -- no fence, no
    ledger, no Job, lease or execution reads. The probe must therefore report
    ``unavailable (permission)`` and must never make ``/readyz`` unready: in the
    reference deployment ``admin`` and ``workbench`` wait for
    ``service_healthy``, so a 503 here keeps the whole stack from starting.
    """
    with _sandbox(
        (LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False, api_identity=True
    ) as box:
        identity = make_conninfo(os.environ[VARIABLE], user=box.role, password=PASSWORD)
        probe = CancellationDebtProbe(
            identity, schema=box.schema, timeout_seconds=3, grace_seconds=0.001
        )
        assert asyncio.run(probe.check()) == "cancellation_debt: unavailable (permission)"

        app = create_app(
            settings=_http_settings(box), readiness=ReadinessService([probe], timeout_seconds=3)
        )

        async def fetch(path: str) -> tuple[int, dict[str, Any]]:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(path)
            return response.status_code, cast(dict[str, Any], response.json())

        status, ready = asyncio.run(fetch("/readyz"))
        assert status == 200, ready
        assert ready["status"] == "ready"
        check = next(item for item in ready["checks"] if item["name"] == "cancellation_debt")
        assert check["status"] == "ok"
        assert check["detail"] == "cancellation_debt: unavailable (permission)"

        status, _ = asyncio.run(fetch("/v1/nexus/admin/diagnostics"))
        assert status == 200


def test_the_optional_reader_identity_reports_the_full_detail() -> None:
    """Pointing the dedicated DSN at the optional reader role yields the full view."""
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False, debt_reader=True) as box:
        detail = asyncio.run(_debt_probe(box).check())

    assert detail is not None
    # The subclass tag is what routes the operator: this unknown execution is not
    # the completed one, so its exit is isolation plus a verified stop.
    assert (
        f"{box.job_id} -> {LEASE}(lease=reserved, execution=unknown_quarantined, "
        "subclass=execution-unknown)" in detail
    )
    assert "over 1 held lease(s)" in detail
    # The acceptance was never claimed (no ledger row): the same sentence must say
    # that the deployment has no driver, or "cancellation in progress" is a lie (#852).
    assert "(held leases: execution-unknown=1; never claimed: 1)" in detail
    assert "completed-without-release" not in detail
    assert "unavailable" not in detail


def test_the_probe_reuses_its_session_instead_of_connecting_per_poll() -> None:
    """``/readyz`` is polled every few seconds; it must not open a session each time."""
    with _sandbox((LEASE, QUARANTINED_DOCUMENT_STATE), coordination=False, debt_reader=True) as box:
        probe = _debt_probe(box)

        async def poll() -> list[str | None]:
            return [await probe.check() for _ in range(4)]

        details = asyncio.run(poll())
        sessions = box.query(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = %s", (box.role,)
        )[0]["n"]
        asyncio.run(probe.aclose())

    assert len(details) == 4
    assert all(detail is not None for detail in details)
    assert sessions == 1, sessions


def test_an_unconfigured_probe_says_so_instead_of_failing() -> None:
    """No dedicated DSN is the default deployment: name it, never fail readiness."""
    probe = CancellationDebtProbe(None, schema="speech", timeout_seconds=1)

    report = asyncio.run(ReadinessService([probe], timeout_seconds=1).inspect())

    assert report.ready is True
    assert report.checks[0].status.value == "ok"
    assert report.checks[0].detail == "cancellation_debt: unavailable (not configured)"


def _http_settings(box: CancelSandbox) -> Settings:
    """The composition root's settings, pointing at the sandbox role."""
    return Settings(
        environment="test",
        database_url=SecretStr(
            make_conninfo(os.environ[VARIABLE], user=box.role, password=PASSWORD)
        ),
        database_schema=box.schema,
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/minio/health/live"),
        object_storage_bucket="synthetic-bucket",
    )


def test_a_closed_acceptance_is_not_reported_as_debt(
    sandbox: CancelSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_client(monkeypatch, sandbox.job_id)
    reconciler = _reconciler(sandbox, confirm_timeout_seconds=WINDOW_US / 1_000_000)
    asyncio.run(reconciler.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1))
    assert sandbox.rows()["speech_jobs"][0]["state"] == "failed"

    assert asyncio.run(_debt_probe(sandbox).check()) is None


def test_a_verdict_once_written_is_never_claimed_again(
    sandbox: CancelSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_client(monkeypatch, sandbox.job_id)
    first = _reconciler(sandbox, confirm_timeout_seconds=WINDOW_US / 1_000_000)
    assert asyncio.run(first.run_once(now_us=ACCEPTED_AT_US + 1_000_000)).retried == 1
    assert asyncio.run(first.run_once(now_us=ACCEPTED_AT_US + WINDOW_US + 1)).exhausted == 1
    settled = sandbox.rows()

    restarted = _reconciler(sandbox, confirm_timeout_seconds=WINDOW_US / 1_000_000)
    second = asyncio.run(restarted.run_once(now_us=ACCEPTED_AT_US + 900_000_000))

    assert second.claimed == 0
    assert sandbox.rows() == settled
    assert settled["speech_jobs"][0]["state"] == "failed"


def test_judgment_does_not_rewrite_a_job_that_was_cancelled_first(
    sandbox: CancelSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified stop that lands first keeps the Job cancelled, not failed."""
    jobs = sandbox.jobs()
    current = jobs.get(sandbox.job_id)
    assert current is not None
    assert (
        jobs.settle_cancelled(sandbox.job_id, expected_version=current.status_version).state
        == "cancelled"
    )

    _install_client(monkeypatch, sandbox.job_id)
    result = asyncio.run(
        _reconciler(sandbox, confirm_timeout_seconds=WINDOW_US / 1_000_000).run_once(
            now_us=ACCEPTED_AT_US + WINDOW_US + 1
        )
    )

    assert (result.claimed, result.exhausted) == (1, 1)
    assert sandbox.rows()["speech_jobs"] == [
        {
            "job_id": sandbox.job_id,
            "state": "cancelled",
            "error_code": None,
            "status_version": 3,
        }
    ]
