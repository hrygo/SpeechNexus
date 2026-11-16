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
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.job_cancellation_runtime import (
    UnconfirmedCancellationSettlement,
    _PublicJobsSettlement,
)
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    JobCancellationReconciler,
)
from speechnexus_backend.speech.application.public_jobs import PublicJobs
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
) -> Iterator[CancelSandbox]:
    """One Job whose leases carry the given gateway execution facts.

    ``None`` means that lease never reached submission, so the gateway recorded
    no execution fact for it at all -- the review's "one lease without the fact"
    case.
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
        for grant in (
            *job_runtime_grants(schema, role),
            *public_job_grants(schema, role),
            *cancellation_reconciliation_grants(schema, role),
        ):
            admin.execute(grant)
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
        with pool.connection() as connection, connection.cursor() as cursor:
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
            _PublicJobsSettlement(jobs, capacity.release_sync),
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
        assert f"{box.job_id} -> {LEASE_SECOND}(lease=reserved, execution=not_submitted)" in detail


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
