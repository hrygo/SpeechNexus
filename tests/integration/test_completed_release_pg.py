"""#852: the bounded operator exit returns a completed execution's slot.

The shape this module reproduces is the one no automatic rule moves: the Job is
``cancelling``, the execution already reached ``succeeded`` with the gateway's own
``engine_terminal_response`` recorded, and the admission lease is still
``reserved`` -- so ``engine_instances.held_slots`` stays at one forever. Isolation
refuses it (a completed execution is not an unknown one) and the quarantine
release refuses it (the lease is not quarantined).

Everything here runs against real PostgreSQL: the real two-level release
transaction, the real ledger, the real ``LeaseService.release`` arithmetic and the
real ``UnconfirmedCancellationSettlement`` that closes the Job afterwards. The
drain response is a recorded gateway document, not engine evidence: this proves
the release rules, not that an instance stopped.
"""

from __future__ import annotations

import asyncio
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

from speechnexus_backend import completed_release_cli as cli
from speechnexus_backend.job_cancellation_runtime import UnconfirmedCancellationSettlement
from speechnexus_backend.speech.application.completed_releases import (
    CompletedReleaseService,
)
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.completed_release import (
    completed_evidence_digest,
    prepared_digest,
)
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.domain.public_jobs import cleanup_state
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseEvidence,
    prepare_evidence,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
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
from speechnexus_backend.speech.infrastructure.quarantine_release_postgres import (
    PostgresQuarantineReleases,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    TABLE as RELEASE_LEDGER,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    quarantine_release_grants,
)
from speechnexus_backend.speech.infrastructure.readiness import CancellationDebtProbe

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_completed_release_test"
DIGEST = "sha256:" + "a" * 64
WORKSPACE = "workspace-test"
INSTANCE = "instance-test"
GENERATION = 1
LEASE = "lease-test-stage-main"
JOB = "job-completed-release"
#: The execution's own end, and the operator observation that follows it.
TERMINATED_AT_US = 5_000_000
OBSERVED_AT = "1970-01-01T00:00:07Z"
OBSERVED_AT_US = 7_000_000
REASON = "ticket #852: drained instance-test generation 1"
DRAIN: dict[str, Any] = {
    "instance_id": INSTANCE,
    "instance_generation": GENERATION,
    "drain_state": "drained",
    "active_intent_count": 0,
    "submitting_intent_count": 0,
    "quarantined_intent_count": 0,
    "safe_to_stop_instance": True,
    "updated_at": "1970-01-01T00:00:06Z",
}


@dataclass
class ReleaseSandbox:
    schema: str
    role: str
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
        return {
            "leases": self.query(
                "SELECT lease_id, lease_state, cleanup_state, budget_released_at_us, version "
                "FROM admission_leases ORDER BY lease_id"
            ),
            "instances": self.query(
                "SELECT instance_id, held_slots, quarantined_slots FROM engine_instances"
            ),
            "quotas": self.query(
                "SELECT held_attempts, quarantined_attempts FROM workspace_quotas"
            ),
            "attempts": self.query("SELECT quota_state FROM admission_attempts"),
            "intents": self.query(
                "SELECT submit_intent_id, execution_state, cleanup_state, error_code, "
                "terminated_at_us, version FROM submit_intents ORDER BY submit_intent_id"
            ),
            "stops": self.query(
                "SELECT submit_intent_id, evidence_kind, execution_stopped, resources_released, "
                "observed_at_us, reason FROM execution_stop_evidence "
                "ORDER BY submit_intent_id"
            ),
            "ledger": self.query(
                "SELECT lease_id, operation_id, instance_id, instance_generation, "
                "evidence_kind, evidence_observed_at_us, reason, released_submit_intent_ids "
                f"FROM {RELEASE_LEDGER} ORDER BY lease_id"
            ),
            "jobs": self.query("SELECT job_id, state, error_code, status_version FROM speech_jobs"),
        }

    def jobs(self) -> PublicJobs:
        adapter = PostgresPublicJobs(self.pool, timeout_seconds=3)
        return PublicJobs(adapter, adapter)

    def capacity(self) -> PostgresLeaseSettler:
        return PostgresLeaseSettler(self.pool, timeout_seconds=3)

    def adapter(self) -> PostgresQuarantineReleases:
        return PostgresQuarantineReleases(self.pool, timeout_seconds=5)


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


def _seed_foundation(admin: psycopg.Connection[dict[str, Any]], job_id: str) -> None:
    """One reserved lease holding one instance slot, as admission leaves it."""
    admin.execute(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES ('tts-realtime', 'realtime', 'active')"
    )
    admin.execute(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, held_attempts, "
        "policy_revision) VALUES (%s, 8, 1, 'policy-test')",
        (WORKSPACE,),
    )
    admin.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest, "
        "allocatable_unified_physical_memory_bytes, held_unified_physical_memory_bytes) "
        "VALUES ('host-test', 'unified_memory', 'active', %s, 100, 1)",
        (DIGEST,),
    )
    admin.execute(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots, held_slots) VALUES "
        "(%s, 1, 'host-test', 'tts-realtime', 'gateway-test', 'binding-test', 'manifest-test', "
        "'template-test', 'managed', 'on_demand', %s, 'ready', 1, 1)",
        (INSTANCE, DIGEST),
    )
    admin.execute(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) VALUES (%s, 'attempt-test', %s, 'binding-test', "
        "'manifest-test', %s, 'policy-test', 'realtime', %s, %s)",
        (job_id, WORKSPACE, DIGEST, DIGEST, '["' + LEASE + '"]'),
    )
    admin.execute(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
        "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
        "reserved_slots, deadline_at_us, reserved_unified_physical_memory_bytes) VALUES "
        "(%s, %s, 'attempt-test', %s, 'host-test', %s, 1, 'tts-realtime', 'gateway-test', 1, 1, "
        "1, 1)",
        (LEASE, job_id, LEASE, INSTANCE),
    )


def _seed_execution(
    admin: psycopg.Connection[dict[str, Any]],
    job_id: str,
    *,
    execution_state: str = "succeeded",
    cleanup_state: str = "complete",
    terminal_evidence: str | None = "engine_terminal_response",
) -> None:
    """The gateway's own durable fact for the lease's submit intent."""
    admin.execute(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
        "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
        "updated_at_us, dispatched_at_us, terminated_at_us, output_stream_id, sample_rate_hz, "
        "execution_state, cleanup_state, error_code) VALUES (%s, %s, %s, 'attempt-test', %s, "
        "%s, '{}', 1, 1, 1, 2000000, %s, 'tts-main', 24000, %s, %s, NULL)",
        (
            submit_intent_identity(job_id),
            LEASE,
            job_id,
            DIGEST,
            DIGEST,
            TERMINATED_AT_US,
            execution_state,
            cleanup_state,
        ),
    )
    if terminal_evidence is not None:
        admin.execute(
            "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind, "
            "evidence_digest, observed_at_us, execution_stopped, resources_released, reason) "
            "VALUES ('evidence-terminal', %s, %s, %s, %s, 1, 1, NULL)",
            (submit_intent_identity(job_id), terminal_evidence, DIGEST, TERMINATED_AT_US),
        )


@contextmanager
def _sandbox(
    *,
    execution_state: str = "succeeded",
    cleanup_state: str = "complete",
    terminal_evidence: str | None = "engine_terminal_response",
    cancelled: bool = True,
) -> Iterator[ReleaseSandbox]:
    """One cancelling Job whose execution completed while its lease stayed reserved."""
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            f"Configure an isolated PostgreSQL DSN; skip is not release evidence ({VARIABLE})"
        )
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_completed_" + suffix, "sn_completed_role_" + suffix
    job_id = JOB + "-" + uuid.uuid4().hex[:8]
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
            *quarantine_release_grants(schema, role),
            *(
                grant
                for grant in coordination_runtime_grants(
                    schema, speech_role=role, gateway_role=role + "_unused"
                )
                if f'"{role}"' in grant
            ),
        ):
            admin.execute(grant)
        _seed_foundation(admin, job_id)
        _seed_execution(
            admin,
            job_id,
            execution_state=execution_state,
            cleanup_state=cleanup_state,
            terminal_evidence=terminal_evidence,
        )
        admin.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "state, status_version, created_at_us, updated_at_us) VALUES (%s, %s, 'tts', "
            "'tts-quality', %s, 'cancelling', 2, 1, 1)",
            (job_id, WORKSPACE, DIGEST),
        )
        if cancelled:
            # An accepted cancellation is the shape this exit closes: the fence is
            # written by the accept path and the reconciliation row is the worker's
            # claim ledger. Neither is optional for the operator exit.
            admin.execute(
                "INSERT INTO speech_job_cancellation_fences (job_id, accepted_at_us, request_id) "
                "VALUES (%s, 3000000, 'request-852-cancel')",
                (job_id,),
            )
        admin.execute(
            "INSERT INTO speech_job_submissions (job_id, request_id, client_reference, "
            "effective_configuration_json, execution_revision, progress_unit, progress_total, "
            "submitted_at_us) VALUES (%s, 'request-852', NULL, '{}', '1', "
            "'text_codepoints', 1, 1)",
            (job_id,),
        )
        pool = speech_pool(
            make_conninfo(url, user=role, password=PASSWORD),
            schema=schema,
            max_size=2,
            name="completed-release-" + suffix,
        )
        pool.open()
        yield ReleaseSandbox(schema, role, admin, pool)
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


@pytest.fixture
def sandbox() -> Iterator[ReleaseSandbox]:
    """The reference shape: a completed execution holding its reservation."""
    with _sandbox() as box:
        yield box


def _release(
    box: ReleaseSandbox,
    *,
    lease_id: str = LEASE,
    drain: dict[str, Any] | None = None,
    observed_at: str = OBSERVED_AT,
    reason: str = REASON,
) -> tuple[int, str]:
    out = __import__("io").StringIO()
    code = cli.execute(
        adapter=box.adapter(),
        service=CompletedReleaseService(box.adapter()),
        lease_id=lease_id,
        drain=cli.parse_drain_response(drain or DRAIN),
        observed_at_text=observed_at,
        reason=reason,
        idempotency_key=None,
        dry_run=False,
        out=out,
    )
    return code, out.getvalue()


def _evidence(
    box: ReleaseSandbox,
    *,
    drain: Any = None,
    observed_at: str = OBSERVED_AT,
    reason: str = REASON,
    digest_reason: str | None = None,
    digest: str | None = None,
) -> ReleaseEvidence:
    record = cli.parse_drain_response(DRAIN) if drain is None else drain
    minted = (
        prepared_digest(
            lease_id=LEASE,
            submit_intent_id=submit_intent_identity(box_job_id(box)),
            instance_id=INSTANCE,
            instance_generation=GENERATION,
            drain=record,
            observed_at=observed_at,
            reason=digest_reason or reason,
        )
        if digest is None
        else digest
    )
    return prepare_evidence(
        lease_id=LEASE,
        evidence_kind="operator_verified_instance_terminated",
        evidence_digest=minted,
        observed_at=observed_at,
        reason=reason,
        idempotency_key=None,
    )


def box_job_id(box: ReleaseSandbox) -> str:
    row = box.query("SELECT operation_id FROM admission_leases WHERE lease_id = %s", (LEASE,))[0]
    return str(row["operation_id"])


def test_the_operator_exit_returns_the_held_slot_and_never_rewrites_the_execution(
    sandbox: ReleaseSandbox,
) -> None:
    """#852: the measurement that decides this issue is ``held_slots`` before/after."""
    before = sandbox.rows()
    assert before["instances"] == [
        {"instance_id": INSTANCE, "held_slots": 1, "quarantined_slots": 0}
    ]
    assert before["leases"][0]["lease_state"] == "reserved"

    code, text = _release(sandbox)
    after = sandbox.rows()

    assert code == cli.EXIT_RELEASED, text
    assert "instance_held_slots: 1" in text
    assert "instance_held_slots: 0" in text
    # The capacity the issue measured is back, and it is not merely "not held":
    assert after["instances"] == [
        {"instance_id": INSTANCE, "held_slots": 0, "quarantined_slots": 0}
    ]
    assert after["leases"] == [
        {
            "lease_id": LEASE,
            "lease_state": "released",
            "cleanup_state": "complete",
            "budget_released_at_us": after["leases"][0]["budget_released_at_us"],
            "version": 2,
        }
    ]
    assert after["leases"][0]["budget_released_at_us"] is not None
    assert after["quotas"] == [{"held_attempts": 0, "quarantined_attempts": 0}]
    assert after["attempts"] == [{"quota_state": "released"}]
    # The execution fact is untouched: this exit returns a reservation, it does
    # not rewrite the gateway's own conclusion about the execution.
    assert after["intents"] == before["intents"]
    assert after["stops"] == before["stops"]
    # The testimony is durable and pinned to the generation that was drained.
    assert after["ledger"] == [
        {
            "lease_id": LEASE,
            "operation_id": box_job_id(sandbox),
            "instance_id": INSTANCE,
            "instance_generation": GENERATION,
            "evidence_kind": "operator_verified_instance_terminated",
            "evidence_observed_at_us": OBSERVED_AT_US,
            "reason": REASON,
            "released_submit_intent_ids": '["' + submit_intent_identity(box_job_id(sandbox)) + '"]',
        }
    ]


def test_the_accepted_cancellation_then_closes_as_an_unconfirmed_failure(
    sandbox: ReleaseSandbox,
) -> None:
    """The released lease lets the existing judgment close the Job, unchanged."""
    job_id = box_job_id(sandbox)
    code, _ = _release(sandbox)
    assert code == cli.EXIT_RELEASED

    # Measured: between the release and the next judgment the Job is still
    # ``cancelling``, so the read model reports ``pending`` -- the pre-release value.
    # ``quarantined`` only appears once the judgment lands the terminal.
    pending = sandbox.jobs().get(job_id)
    assert pending is not None
    assert pending.state == "cancelling"
    assert cleanup_state(pending) == "pending"

    judgment = UnconfirmedCancellationSettlement(sandbox.jobs(), sandbox.capacity()).settle(job_id)

    assert judgment.settled is True
    assert judgment.outcome == "exhausted"
    row = sandbox.rows()["jobs"][0]
    assert (row["state"], row["error_code"]) == ("failed", "cancellation_unconfirmed")
    job = sandbox.jobs().get(job_id)
    assert job is not None
    # The terminal keeps its documented read-model value: it names *why* the
    # cancellation ended, not a live sum of the lease bucket a release already
    # moved. The direction is safe -- it never reports capacity as available.
    assert cleanup_state(job) == "quarantined"
    assert sandbox.rows()["instances"] == [
        {"instance_id": INSTANCE, "held_slots": 0, "quarantined_slots": 0}
    ]


def test_a_replay_answers_from_the_ledger_and_moves_nothing(sandbox: ReleaseSandbox) -> None:
    first, _ = _release(sandbox)
    assert first == cli.EXIT_RELEASED
    released = sandbox.rows()

    code, text = _release(sandbox)

    assert code == cli.EXIT_RELEASED
    assert "outcome: replayed" in text
    assert sandbox.rows()["instances"] == released["instances"]
    assert sandbox.rows()["leases"] == released["leases"]


def test_a_drain_of_another_generation_is_refused_by_the_real_transaction(
    sandbox: ReleaseSandbox,
) -> None:
    """Removing the generation pin is the red case: storage must refuse it too."""
    code, text = _release(sandbox, drain=DRAIN | {"instance_generation": GENERATION + 1})

    assert code == cli.EXIT_REFUSED
    assert "completed_release_drain_mismatch" in text
    assert sandbox.rows()["instances"][0]["held_slots"] == 1
    assert sandbox.rows()["leases"][0]["lease_state"] == "reserved"
    assert sandbox.rows()["ledger"] == []


def test_an_undrained_generation_is_refused_by_the_real_transaction(
    sandbox: ReleaseSandbox,
) -> None:
    """Allowing a release without a drain is the other red case."""
    undrained = DRAIN | {"pending": True, "active_intent_count": 1, "safe_to_stop_instance": False}

    code, text = _release(sandbox, drain=undrained)

    assert code == cli.EXIT_REFUSED
    assert "completed_release_not_drained" in text
    assert sandbox.rows()["instances"][0]["held_slots"] == 1
    assert sandbox.rows()["ledger"] == []


def test_the_transaction_recomputes_the_testimony_digest(sandbox: ReleaseSandbox) -> None:
    """The digest is not trusted: it must cover the facts the release decided on."""
    adapter = sandbox.adapter()
    service = CompletedReleaseService(adapter)
    drain = cli.parse_drain_response(DRAIN)
    honest = _evidence(sandbox)
    # The digest covers the reason, so a testimony carrying one reason under a
    # digest minted for another did not come from the run it claims to describe.
    forged = _evidence(sandbox, digest_reason=REASON, reason=REASON + " (edited)")

    assert forged.evidence_digest == honest.evidence_digest
    assert forged.reason != honest.reason
    with pytest.raises(QuarantineReleaseRejectedError) as refused:
        service.release(forged, drain)
    assert refused.value.code == "invalid"
    assert sandbox.rows()["instances"][0]["held_slots"] == 1


def test_a_digest_for_another_generation_cannot_be_replayed_onto_this_lease(
    sandbox: ReleaseSandbox,
) -> None:
    """The digest names the generation, so a friendly-looking number is not enough."""
    service = CompletedReleaseService(sandbox.adapter())
    drain = cli.parse_drain_response(DRAIN)
    elsewhere = prepared_digest(
        lease_id=LEASE,
        submit_intent_id=submit_intent_identity(box_job_id(sandbox)),
        instance_id=INSTANCE,
        instance_generation=GENERATION + 1,
        drain=drain,
        observed_at=OBSERVED_AT,
        reason=REASON,
    )

    with pytest.raises(QuarantineReleaseRejectedError) as refused:
        service.release(_evidence(sandbox, digest=elsewhere), drain)
    assert refused.value.code == "invalid"
    assert sandbox.rows()["instances"][0]["held_slots"] == 1


def test_a_successful_execution_without_terminal_evidence_has_no_exit_here() -> None:
    """The state #338 owns is named, not papered over with a testimony."""
    with _sandbox(terminal_evidence=None) as box:
        code, text = _release(box)

        assert code == cli.EXIT_REFUSED
        assert "completed_release_requires_terminal_evidence" in text
        assert box.rows()["instances"][0]["held_slots"] == 1
        assert box.rows()["ledger"] == []


def test_the_unknown_execution_shape_is_not_accepted_by_this_exit() -> None:
    """The two debts keep two testimonies: this one is not the quarantine release."""
    with _sandbox(execution_state="unknown_quarantined", cleanup_state="quarantined") as box:
        code, text = _release(box)

        assert code == cli.EXIT_REFUSED
        assert "quarantine_release_conflict" in text
        assert box.rows()["instances"][0]["held_slots"] == 1


def test_the_quarantine_release_still_refuses_the_completed_shape(
    sandbox: ReleaseSandbox,
) -> None:
    """Widening the unknown-execution rule to accept this shape is what we did not do."""
    from speechnexus_backend.speech.infrastructure.quarantine_release_sql import (
        quarantine_release_in_transaction,
    )

    drain = cli.parse_drain_response(DRAIN)
    evidence = _evidence(sandbox)
    assert drain.drained

    with (
        pytest.raises(QuarantineReleaseRejectedError) as refused,
        sandbox.pool.connection() as connection,
        connection.cursor() as raw,
    ):
        quarantine_release_in_transaction(
            raw,
            evidence,
            clock=lambda: OBSERVED_AT_US,
        )

    assert refused.value.code == "quarantine_release_conflict"
    assert sandbox.rows()["instances"][0]["held_slots"] == 1
    assert sandbox.rows()["ledger"] == []


def test_the_digest_function_is_the_documented_canonical_one() -> None:
    """A minted digest is reproducible from the recorded testimony, byte for byte."""
    drain = cli.parse_drain_response(DRAIN)

    assert prepared_digest(
        lease_id=LEASE,
        submit_intent_id=JOB,
        instance_id=INSTANCE,
        instance_generation=GENERATION,
        drain=drain,
        observed_at=OBSERVED_AT,
        reason=REASON,
    ) == completed_evidence_digest(
        lease_id=LEASE,
        submit_intent_id=JOB,
        instance_id=INSTANCE,
        instance_generation=GENERATION,
        drain=drain,
        observed_at_us=OBSERVED_AT_US,
        reason=REASON,
    )


def test_a_job_without_an_accepted_cancellation_is_refused() -> None:
    """#852 review M2: measured -- a Job with no fence released successfully.

    The tool now proves the identification criteria itself, so an operator who
    runs it against the wrong Job gets a refusal and an unchanged slot instead of
    a release nobody can attribute to an accepted cancellation.
    """
    with _sandbox(cancelled=False) as box:
        code, text = _release(box)

        assert code == cli.EXIT_REFUSED
        assert "completed_release_not_accepted_cancellation" in text
        assert box.rows()["instances"][0]["held_slots"] == 1
        assert box.rows()["ledger"] == []


def test_a_drain_from_before_the_execution_ended_is_refused(sandbox: ReleaseSandbox) -> None:
    """#852 review M1 on real storage: a pre-execution drain still released.

    The drain is structurally perfect for the right generation; only the gateway's
    own read time (``updated_at``) predates the execution's end.
    """
    code, text = _release(sandbox, drain=DRAIN | {"updated_at": "1970-01-01T00:00:04Z"})

    assert code == cli.EXIT_REFUSED
    assert "completed_release_drain_stale" in text
    assert sandbox.rows()["instances"][0]["held_slots"] == 1
    assert sandbox.rows()["leases"][0]["lease_state"] == "reserved"
    assert sandbox.rows()["ledger"] == []


def test_a_settled_cancellation_is_refused(sandbox: ReleaseSandbox) -> None:
    """A concluded judgment already accounted for this capacity."""
    job_id = box_job_id(sandbox)
    sandbox.execute(
        "INSERT INTO speech_job_cancellation_reconciliations (job_id, attempts, "
        "next_check_at_us, last_outcome, settled_at_us, updated_at_us) "
        "VALUES (%s, 1, 8000000, 'settled', 8000000, 8000000)",
        (job_id,),
    )

    code, text = _release(sandbox)

    assert code == cli.EXIT_REFUSED
    assert "completed_release_already_settled" in text
    assert sandbox.rows()["instances"][0]["held_slots"] == 1
    assert sandbox.rows()["ledger"] == []


def _debt(box: ReleaseSandbox) -> str | None:
    """The readiness detail, read through this sandbox's own service identity."""
    probe = CancellationDebtProbe(
        make_conninfo(os.environ[VARIABLE], user=box.role, password=PASSWORD),
        schema=box.schema,
        timeout_seconds=3,
        grace_seconds=0.001,
    )
    return asyncio.run(probe.check())


def test_the_label_routes_this_shape_to_the_exit_that_accepts_it(
    sandbox: ReleaseSandbox,
) -> None:
    """#852 review M2: the label and the gate must agree, both ways."""
    detail = _debt(sandbox)

    assert detail is not None
    assert "subclass=completed-without-release" in detail
    assert "over 1 held lease(s)" in detail
    assert "(held leases: completed-without-release=1)" in detail
    assert _release(sandbox)[0] == cli.EXIT_RELEASED


def test_the_label_does_not_route_the_unproven_shape_to_that_exit() -> None:
    """A ``succeeded`` execution with no terminal fact is #338's shape, not this one.

    The label exists to route, so it may not point at a command that always
    refuses. This case proves the routing is honest end to end: the probe says
    ``execution-unknown`` and the tool refuses with the terminal-evidence code.
    """
    with _sandbox(terminal_evidence=None) as box:
        detail = _debt(box)

        assert detail is not None
        assert "subclass=execution-unknown" in detail
        assert "completed-without-release" not in detail

        code, text = _release(box)

        assert code == cli.EXIT_REFUSED
        assert "completed_release_requires_terminal_evidence" in text
        assert box.rows()["ledger"] == []
