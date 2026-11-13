"""Production quarantine release on real PostgreSQL: one transaction, real locks.

Each case migrates an isolated schema through the real Alembic chain, installs
the declared release grants on a dedicated non-owner role, seeds the synthetic
quarantine state directly, and then drives the production adapter. A skipped
database is a failure, not evidence.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from psycopg.rows import dict_row
from pydantic import AnyHttpUrl, SecretStr
from speechnexus_contracts.admin_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.readiness import StaticProbe
from tests.support.migrations import HEAD

from speechnexus_backend.app import create_app
from speechnexus_backend.quarantine_release_runtime import (
    QuarantineReleaseRuntime,
    verify_quarantine_release_role,
)
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineReleaseService,
    QuarantineSurfaceError,
    QuarantineUnknownError,
)
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import LeaseResult
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseEvidence,
    ReleaseLedger,
    ReleaseOutcome,
    prepare_evidence,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.execution_release_schema import (
    release_guard_grants,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import (
    PostgresLeaseTransactions,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.quarantine_release_postgres import (
    PostgresQuarantineReleases,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    TABLE,
    TRIGGER,
    quarantine_release_grants,
)
from speechnexus_backend.speech.interfaces.http.quarantine_release_errors import (
    OPERATION as RELEASE_OPERATION,
)
from speechnexus_backend.speech.interfaces.http.quarantine_releases import PATH

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_release_test"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
LEASE = "lease-stage-main"
INTENT = "submit-stage-main"
OPERATION = "operation-test"
ATTEMPT = "attempt-test"
KIND = "operator_verified_instance_terminated"
REASON = "container record verified by the on-call operator"
POOL = "tts-realtime"
WORKSPACE = "workspace-test"
HOST = "host-test"
INSTANCE = "instance-test"
GATEWAY = "gateway-test"
TERMINAL_INTENTS = ("succeeded", "failed", "aborted")


def _stamp(value_us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value_us)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _connection_url(url: str, role: str) -> str:
    parts = urlsplit(url)
    netloc = f"{role}:{quote(PASSWORD, safe='')}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@dataclass
class Sandbox:
    url: str
    schema: str
    admin: psycopg.Connection[dict[str, Any]]
    release_role: str
    speech_role: str
    gateway_role: str
    release_pool: Any
    speech_pool: Any
    consumer: PostgresLeaseTransactions
    config: Config

    # -- reading ---------------------------------------------------------
    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = self.admin.execute(statement, parameters or None)
        return list(cursor.fetchall()) if cursor.description else []

    def scalar(self, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
        rows = self.query(statement, parameters)
        assert len(rows) == 1
        return next(iter(rows[0].values()))

    def count(self, table: str) -> int:
        return int(self.scalar(f"SELECT count(*) AS n FROM {table}"))

    def now(self) -> int:
        return int(
            self.scalar(
                "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
            )
        )

    def ledger_rows(self) -> list[dict[str, Any]]:
        return self.query(f"SELECT * FROM {TABLE} ORDER BY lease_id")

    def projections(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "leases": self.query(
                "SELECT lease_id, lease_state, cleanup_state, budget_released_at_us, version "
                "FROM admission_leases ORDER BY lease_id"
            ),
            "intents": self.query(
                "SELECT submit_intent_id, execution_state, cleanup_state, terminated_at_us, "
                "version FROM submit_intents ORDER BY submit_intent_id"
            ),
            "quotas": self.query(
                "SELECT workspace_id, held_attempts, quarantined_attempts, version "
                "FROM workspace_quotas"
            ),
            "hosts": self.query(
                "SELECT host_node_id, held_unified_physical_memory_bytes, "
                "quarantined_unified_physical_memory_bytes, version FROM host_nodes"
            ),
            "instances": self.query(
                "SELECT instance_id, held_slots, quarantined_slots, version FROM engine_instances"
            ),
            "attempts": self.query(
                "SELECT operation_id, attempt_id, quota_state, version FROM admission_attempts"
            ),
            "evidence": self.query(
                "SELECT evidence_id, submit_intent_id, evidence_kind, observed_at_us "
                "FROM execution_stop_evidence ORDER BY evidence_id"
            ),
        }

    # -- fixtures --------------------------------------------------------
    def reset(self) -> None:
        self.admin.execute(
            "TRUNCATE TABLE admission_quarantine_releases, execution_stop_evidence, "
            "submit_intents, admission_leases, admission_attempts, engine_instances, "
            "host_nodes, workspace_quotas, resource_pools, runtime_instance_heads, "
            "runtime_instance_intents, gateway_fencing_barriers, output_stream_heads, "
            "output_sample_intervals RESTART IDENTITY CASCADE"
        )

    def seed(self, *, stages: tuple[str, ...] = ("stage-main",)) -> None:
        self.reset()
        self.admin.execute(
            "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
            "VALUES (%s, 'realtime', 'active')",
            (POOL,),
        )
        self.admin.execute(
            "INSERT INTO workspace_quotas "
            "(workspace_id, max_attempts, held_attempts, quarantined_attempts, policy_revision) "
            "VALUES (%s, 2, 0, 1, 'policy-test')",
            (WORKSPACE,),
        )
        self.admin.execute(
            "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, "
            "topology_digest, allocatable_unified_physical_memory_bytes, "
            "quarantined_unified_physical_memory_bytes) "
            "VALUES (%s, 'unified_memory', 'active', %s, 8, %s)",
            (HOST, DIGEST, len(stages)),
        )
        self.admin.execute(
            "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
            "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
            "management_mode, loading_policy, config_digest, instance_state, max_slots, "
            "held_slots, quarantined_slots) VALUES (%s, 1, %s, %s, %s, 'binding-test', "
            "'manifest-test', 'template-test', 'managed', 'on_demand', %s, 'ready', 8, 0, %s)",
            (INSTANCE, HOST, POOL, GATEWAY, DIGEST, len(stages)),
        )
        self.admin.execute(
            "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
            "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, "
            "service_class, request_digest, required_stage_ids, quota_state) VALUES "
            "(%s, %s, %s, 'binding-test', 'manifest-test', %s, 'policy-test', "
            "'realtime', %s, %s, 'quarantined')",
            (OPERATION, ATTEMPT, WORKSPACE, DIGEST, DIGEST, json.dumps(list(stages))),
        )
        for index, stage in enumerate(stages):
            lease_id = LEASE if index == 0 else f"lease-{stage}"
            intent_id = INTENT if index == 0 else f"submit-{stage}"
            self.admin.execute(
                "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
                "host_node_id, instance_id, instance_generation, pool_id, gateway_id, "
                "fencing_token, reserved_slots, deadline_at_us, lease_state, cleanup_state, "
                "quarantine_observed_at_us, quarantine_reason_code, "
                "reserved_unified_physical_memory_bytes) VALUES "
                "(%s, %s, %s, %s, %s, %s, 1, %s, %s, 1, 1, %s, 'quarantined', 'quarantined', "
                "%s, 'engine_transport_unknown', 1)",
                (
                    lease_id,
                    OPERATION,
                    ATTEMPT,
                    stage,
                    HOST,
                    INSTANCE,
                    POOL,
                    GATEWAY,
                    self.now() + 60_000_000,
                    self.now() - 2_000_000,
                ),
            )
            self.admin.execute(
                "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, "
                "attempt_id, command_digest, request_digest, request_document, deadline_at_us, "
                "recorded_at_us, updated_at_us, dispatched_at_us, execution_state, cleanup_state) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'unknown_quarantined', "
                "'quarantined')",
                (
                    intent_id,
                    lease_id,
                    OPERATION,
                    ATTEMPT,
                    DIGEST,
                    DIGEST,
                    '{"synthetic_execution_body":true}',
                    self.now() + 60_000_000,
                    self.now() - 5_000_000,
                    self.now() - 5_000_000,
                    self.now() - 4_000_000,
                ),
            )

    # -- driving ---------------------------------------------------------
    def evidence(self, **overrides: Any) -> ReleaseEvidence:
        values: dict[str, Any] = {
            "lease_id": LEASE,
            "evidence_kind": KIND,
            "evidence_digest": DIGEST,
            "observed_at": _stamp(self.now() - 1_000),
            "reason": REASON,
            "idempotency_key": "operator-poll-0001",
        }
        values.update(overrides)
        return prepare_evidence(**values)

    def service(self, *, timeout_seconds: float = 3) -> QuarantineReleaseService:
        return QuarantineReleaseService(
            PostgresQuarantineReleases(self.release_pool, timeout_seconds=timeout_seconds)
        )

    def release(self, evidence: ReleaseEvidence | None = None) -> ReleaseOutcome:
        return self.service().release(evidence or self.evidence())

    def rejection(self, evidence: ReleaseEvidence) -> str:
        with pytest.raises(QuarantineReleaseRejectedError) as raised:
            self.release(evidence)
        return raised.value.code

    def lease_version(self, lease_id: str = LEASE) -> int:
        return int(
            self.scalar("SELECT version FROM admission_leases WHERE lease_id = %s", (lease_id,))
        )

    def add_stop_evidence(
        self, *, evidence_id: str, intent_id: str = INTENT, kind: str, reason: str | None
    ) -> None:
        self.admin.execute(
            "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, "
            "evidence_kind, evidence_digest, observed_at_us, reason, execution_stopped, "
            "resources_released) VALUES (%s, %s, %s, %s, %s, %s, 1, 1)",
            (evidence_id, intent_id, kind, DIGEST, self.now() - 500, reason),
        )

    def dsn(self, role: str) -> str:
        return _connection_url(self.url, role)

    def consumer_release(self, lease_id: str = LEASE) -> LeaseResult:
        return self.consumer.release_lease(ReleaseLease(lease_id, self.lease_version(lease_id)))


def _migration_config(schema: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    return config


@pytest.fixture(scope="module")
def sandbox() -> Iterator[Sandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(f"{VARIABLE} is required for the production quarantine release")
    suffix = uuid.uuid4().hex[:8]
    schema = f"qr_{suffix}"
    roles = (f"qr_rel_{suffix}", f"qr_sp_{suffix}", f"qr_gw_{suffix}")
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = _migration_config(schema)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, HEAD)
    finally:
        engine.dispose()
    admin.execute(f'SET search_path TO "{schema}",pg_catalog')
    for role in roles:
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    for statement in coordination_runtime_grants(
        schema, speech_role=roles[1], gateway_role=roles[2]
    ):
        admin.execute(statement)
    for statement in release_guard_grants(schema, roles[1]):
        admin.execute(statement)
    for statement in quarantine_release_grants(schema, roles[0]):
        admin.execute(statement)
    release_pool = speech_pool(
        _connection_url(url, roles[0]), schema=schema, max_size=4, name="qr-release"
    )
    speech = speech_pool(
        _connection_url(url, roles[1]), schema=schema, max_size=4, name="qr-speech"
    )
    release_pool.open()
    speech.open()
    box = Sandbox(
        url,
        schema,
        admin,
        roles[0],
        roles[1],
        roles[2],
        release_pool,
        speech,
        PostgresLeaseTransactions(speech),
        config,
    )
    try:
        yield box
    finally:
        release_pool.close()
        speech.close()
        admin.execute("SET search_path TO public")
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        for role in roles:
            admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


@pytest.fixture
def db(sandbox: Sandbox) -> Sandbox:
    sandbox.seed()
    return sandbox


def _drop_role(admin: psycopg.Connection[dict[str, Any]], role: str) -> None:
    admin.execute(f'DROP OWNED BY "{role}"')
    admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def _terminal(db: Sandbox, *, intent_id: str = INTENT, lease_id: str = LEASE) -> None:
    db.admin.execute(
        "UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete', "
        "terminated_at_us = %s WHERE submit_intent_id = %s",
        (db.now() - 400, intent_id),
    )


# ---------------------------------------------------------------------------
# 1. the committed transaction
# ---------------------------------------------------------------------------


def test_release_settles_every_projection_once(db: Sandbox) -> None:
    evidence = db.evidence()

    outcome = db.release(evidence)

    assert outcome.idempotent_replay is False
    ledger: ReleaseLedger = outcome.ledger
    assert ledger.released_submit_intent_ids == (INTENT,)
    assert ledger.idempotency_key_digest is not None
    assert ledger.evidence_observed_at_us <= ledger.budget_released_at_us

    lease = db.query("SELECT * FROM admission_leases WHERE lease_id = %s", (LEASE,))[0]
    assert lease["lease_state"] == "released"
    assert lease["cleanup_state"] == "complete"
    assert lease["budget_released_at_us"] == ledger.budget_released_at_us
    assert lease["quarantine_observed_at_us"] is not None
    assert lease["quarantine_reason_code"] == "engine_transport_unknown"
    assert lease["reserved_unified_physical_memory_bytes"] == 1

    intent = db.query("SELECT * FROM submit_intents WHERE submit_intent_id = %s", (INTENT,))[0]
    assert (intent["execution_state"], intent["cleanup_state"]) == ("aborted", "complete")
    assert intent["terminated_at_us"] == evidence.observed_at_us
    assert intent["version"] == 2

    quota = db.query("SELECT * FROM workspace_quotas")[0]
    assert (quota["held_attempts"], quota["quarantined_attempts"]) == (0, 0)
    assert quota["version"] == 2

    attempt = db.query("SELECT * FROM admission_attempts")[0]
    assert attempt["quota_state"] == "released"
    assert attempt["version"] == 2

    host = db.query("SELECT * FROM host_nodes")[0]
    assert host["quarantined_unified_physical_memory_bytes"] == 0
    assert host["allocatable_unified_physical_memory_bytes"] == 8
    assert host["version"] == 2

    instance = db.query("SELECT * FROM engine_instances")[0]
    assert (instance["held_slots"], instance["quarantined_slots"]) == (0, 0)
    assert instance["max_slots"] == 8
    assert instance["version"] == 2

    stops = db.query("SELECT * FROM execution_stop_evidence")
    assert len(stops) == 1
    assert stops[0]["evidence_kind"] == KIND
    assert stops[0]["reason"] == REASON
    assert db.count(TABLE) == 1
    assert db.count("execution_stop_evidence") == 1


def test_release_records_operator_evidence_that_reads_back_as_contract(db: Sandbox) -> None:
    outcome = db.release()

    row = db.ledger_rows()[0]
    assert row["lease_id"] == LEASE
    assert row["operation_id"] == OPERATION
    assert row["attempt_id"] == ATTEMPT
    assert row["stage_id"] == "stage-main"
    assert row["host_node_id"] == HOST
    assert row["instance_id"] == INSTANCE
    assert row["instance_generation"] == 1
    assert row["fencing_token"] == 1
    assert row["budget_released_at_us"] == outcome.ledger.budget_released_at_us
    assert row["released_submit_intent_ids"] == f'["{INTENT}"]'


# ---------------------------------------------------------------------------
# 2-5. idempotency table against the immutable ledger row
# ---------------------------------------------------------------------------


def test_same_key_same_evidence_replays_without_a_write(db: Sandbox) -> None:
    evidence = db.evidence()
    first = db.release(evidence)
    snapshot = db.projections()

    replay = db.release(evidence)

    assert replay.idempotent_replay is True
    assert replay.ledger == first.ledger
    assert db.projections() == snapshot
    assert db.count(TABLE) == 1
    assert db.count("execution_stop_evidence") == 1


@pytest.mark.parametrize("variant", ["different_key", "no_key", "same_key_new_reason"])
def test_other_keys_and_evidence_are_not_a_second_release(db: Sandbox, variant: str) -> None:
    base = db.evidence()
    first = db.release(base)
    snapshot = db.projections()
    observed = _stamp(base.observed_at_us)
    if variant == "same_key_new_reason":
        evidence = db.evidence(observed_at=observed, reason=REASON + " (repeat)")
        code = db.rejection(evidence)
        assert code == "idempotency_conflict"
    else:
        evidence = db.evidence(
            observed_at=observed,
            idempotency_key=None if variant == "no_key" else "operator-poll-0002",
        )
        replay = db.release(evidence)
        assert replay.idempotent_replay is True
        assert replay.ledger.budget_released_at_us == first.ledger.budget_released_at_us

    assert db.projections() == snapshot
    assert db.count(TABLE) == 1


def test_different_key_with_different_evidence_is_a_conflict(db: Sandbox) -> None:
    db.release()
    snapshot = db.projections()

    code = db.rejection(
        db.evidence(idempotency_key="operator-poll-0002", evidence_digest=OTHER_DIGEST)
    )

    assert code == "quarantine_release_conflict"
    assert db.projections() == snapshot
    assert db.count(TABLE) == 1


# ---------------------------------------------------------------------------
# 6-7. competition with the background release consumer
# ---------------------------------------------------------------------------


def test_consumer_first_makes_the_operator_evidence_inapplicable(db: Sandbox) -> None:
    now = db.now()
    db.add_stop_evidence(
        evidence_id="stop-machine",
        kind="instance_terminated_by_controller",
        reason=None,
    )
    db.admin.execute(
        "UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete', "
        "terminated_at_us = %s WHERE submit_intent_id = %s",
        (now - 400, INTENT),
    )
    result = db.consumer_release()
    assert result.fact is not None
    assert result.fact.lease_state == "released"
    consumer_budget = result.fact.budget_released_at_us
    snapshot = db.projections()

    code = db.rejection(db.evidence())

    assert code == "quarantine_release_conflict"
    assert db.projections() == snapshot
    assert db.count(TABLE) == 0
    stops = db.query("SELECT * FROM execution_stop_evidence")
    assert len(stops) == 1
    assert stops[0]["evidence_kind"] == "instance_terminated_by_controller"
    lease = db.query("SELECT * FROM admission_leases WHERE lease_id = %s", (LEASE,))[0]
    assert lease["budget_released_at_us"] == consumer_budget


def test_operator_first_makes_the_consumer_replay(db: Sandbox) -> None:
    outcome = db.release()
    snapshot = db.projections()

    result = db.consumer_release()

    assert result.outcome == "replayed", "the consumer must take the replay branch"
    assert result.fact is not None
    assert result.fact.budget_released_at_us == outcome.ledger.budget_released_at_us
    assert db.projections() == snapshot
    assert db.count(TABLE) == 1


def test_evidence_split_release_still_replays_read_only(db: Sandbox) -> None:
    """Branch F match path: evidence committed separately from the release."""
    now = db.now()
    db.add_stop_evidence(evidence_id="stop-operator-stored", kind=KIND, reason=REASON)
    db.admin.execute(
        "UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete', "
        "terminated_at_us = %s WHERE submit_intent_id = %s",
        (now - 400, INTENT),
    )
    db.consumer_release()
    snapshot = db.projections()
    stored = db.query("SELECT observed_at_us FROM execution_stop_evidence")[0]
    stored_budget = db.query(
        "SELECT budget_released_at_us FROM admission_leases WHERE lease_id = %s", (LEASE,)
    )[0]["budget_released_at_us"]
    evidence = db.evidence(observed_at=_stamp(int(stored["observed_at_us"])))

    replay = db.release(evidence)

    assert replay.idempotent_replay is True
    # Branch F's whole value is returning the stored release fact, so the values
    # are asserted, not just "no write happened".
    assert replay.ledger.budget_released_at_us == stored_budget
    assert replay.ledger.released_submit_intent_ids == (INTENT,)
    assert replay.ledger.evidence_kind == KIND
    assert replay.ledger.evidence_digest == DIGEST
    assert replay.ledger.evidence_observed_at_us == stored["observed_at_us"]
    assert replay.ledger.reason == REASON
    assert db.projections() == snapshot
    assert db.count(TABLE) == 0


# ---------------------------------------------------------------------------
# 8-11. preconditions on the locked facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement,parameters",
    [
        (
            "UPDATE submit_intents SET execution_state = 'executing', cleanup_state = 'pending' "
            "WHERE submit_intent_id = %s",
            (INTENT,),
        ),
        (
            "UPDATE admission_leases SET lease_state = 'reserved', cleanup_state = 'pending' "
            "WHERE lease_id = %s",
            (LEASE,),
        ),
        (
            "UPDATE admission_leases SET cleanup_state = 'pending' WHERE lease_id = %s",
            (LEASE,),
        ),
        (
            "UPDATE admission_leases SET quarantine_observed_at_us = NULL WHERE lease_id = %s",
            (LEASE,),
        ),
        (
            "UPDATE admission_leases SET lease_state = 'stopping' WHERE lease_id = %s",
            (LEASE,),
        ),
    ],
)
def test_lease_or_intent_outside_quarantine_conflicts(
    db: Sandbox, statement: str, parameters: tuple[Any, ...]
) -> None:
    db.admin.execute(statement, parameters)
    snapshot = db.projections()

    assert db.rejection(db.evidence()) == "quarantine_release_conflict"
    assert db.projections() == snapshot
    assert db.count(TABLE) == 0


def test_lease_without_a_submit_intent_conflicts(db: Sandbox) -> None:
    db.admin.execute("DELETE FROM submit_intents")
    snapshot = db.projections()

    assert db.rejection(db.evidence()) == "quarantine_release_conflict"
    assert db.projections() == snapshot
    assert db.count(TABLE) == 0


def test_missing_lease_is_not_found(db: Sandbox) -> None:
    assert db.rejection(db.evidence(lease_id="lease-absent")) == "not_found"


# ---------------------------------------------------------------------------
# 12. invalid evidence and cast arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "observed_at,expectation",
    [
        ("recorded_minus_one", "invalid"),
        ("dispatched_minus_one", "invalid"),
        ("future", "invalid"),
    ],
)
def test_observed_at_bounds_are_server_decided(
    db: Sandbox, observed_at: str, expectation: str
) -> None:
    now = db.now()
    intent = db.query("SELECT * FROM submit_intents WHERE submit_intent_id = %s", (INTENT,))[0]
    if observed_at == "future":
        value = now + 5_000_000
    elif observed_at == "recorded_minus_one":
        value = int(intent["recorded_at_us"]) - 1
    else:
        assert intent["dispatched_at_us"] is not None
        value = int(intent["dispatched_at_us"]) - 1
    snapshot = db.projections()

    assert db.rejection(db.evidence(observed_at=_stamp(value))) == expectation
    assert db.projections() == snapshot
    assert db.count("execution_stop_evidence") == 0


def test_version_exhaustion_rolls_back_every_write(db: Sandbox) -> None:
    db.admin.execute(
        "UPDATE admission_leases SET version = %s WHERE lease_id = %s", (MAX_VERSION, LEASE)
    )
    snapshot = db.projections()

    assert db.rejection(db.evidence()) == "quarantine_release_conflict"
    assert db.projections() == snapshot
    assert db.count("execution_stop_evidence") == 0
    assert db.count(TABLE) == 0


# ---------------------------------------------------------------------------
# 13/20. sibling stages settle the attempt quota once
# ---------------------------------------------------------------------------


def test_sibling_stage_release_returns_the_attempt_quota_once(db: Sandbox) -> None:
    db.seed(stages=("stage-main", "stage-extra"))
    other = db.evidence(
        lease_id="lease-stage-extra",
        idempotency_key="operator-poll-0002",
    )

    first = db.release()
    assert first.ledger.released_submit_intent_ids == (INTENT,)
    attempt = db.query("SELECT * FROM admission_attempts")[0]
    assert attempt["quota_state"] == "quarantined"
    assert attempt["version"] == 1

    second = db.release(other)

    assert second.idempotent_replay is False
    attempt = db.query("SELECT * FROM admission_attempts")[0]
    assert attempt["quota_state"] == "released"
    assert attempt["version"] == 2
    quota = db.query("SELECT * FROM workspace_quotas")[0]
    assert (quota["held_attempts"], quota["quarantined_attempts"]) == (0, 0)
    assert quota["version"] == 2
    assert db.count(TABLE) == 2
    assert db.count("execution_stop_evidence") == 2


def test_concurrent_sibling_stages_do_not_double_return_quota(db: Sandbox) -> None:
    db.seed(stages=("stage-main", "stage-extra"))
    evidences = [
        db.evidence(),
        db.evidence(lease_id="lease-stage-extra", idempotency_key="operator-poll-0002"),
    ]
    barrier = threading.Barrier(2)

    def run(evidence: ReleaseEvidence) -> ReleaseOutcome:
        barrier.wait(timeout=10)
        return db.service().release(evidence)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, evidence) for evidence in evidences]
        outcomes = [future.result(timeout=30) for future in futures]

    assert all(outcome.idempotent_replay is False for outcome in outcomes)
    attempt = db.query("SELECT * FROM admission_attempts")[0]
    assert (attempt["quota_state"], attempt["version"]) == ("released", 2)
    quota = db.query("SELECT * FROM workspace_quotas")[0]
    assert (quota["held_attempts"], quota["quarantined_attempts"], quota["version"]) == (0, 0, 2)
    assert db.count(TABLE) == 2


def test_operator_and_consumer_racing_the_same_lease_never_double_charge(db: Sandbox) -> None:
    now = db.now()
    db.add_stop_evidence(
        evidence_id="stop-machine", kind="instance_terminated_by_controller", reason=None
    )
    db.admin.execute(
        "UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete', "
        "terminated_at_us = %s WHERE submit_intent_id = %s",
        (now - 400, INTENT),
    )
    evidence = db.evidence()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def operator() -> None:
        barrier.wait(timeout=10)
        try:
            outcome = db.service().release(evidence)
            results.append("replay" if outcome.idempotent_replay else "committed")
        except QuarantineReleaseRejectedError as error:
            results.append(error.code)

    def consumer() -> None:
        barrier.wait(timeout=10)
        try:
            result = db.consumer_release()
            results.append("consumer_replay" if result.outcome == "replayed" else "consumer")
        except Exception as error:
            results.append(type(error).__name__)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(operator), pool.submit(consumer)]
        for future in futures:
            future.result(timeout=30)

    assert sorted(results) in (
        ["committed", "consumer_replay"],
        ["consumer", "quarantine_release_conflict"],
    )
    host = db.query("SELECT * FROM host_nodes")[0]
    assert (host["quarantined_unified_physical_memory_bytes"], host["version"]) == (0, 2)
    quota = db.query("SELECT * FROM workspace_quotas")[0]
    assert (quota["held_attempts"], quota["quarantined_attempts"], quota["version"]) == (0, 0, 2)
    assert db.count(TABLE) <= 1
    assert db.count("execution_stop_evidence") == 1


# ---------------------------------------------------------------------------
# 14. the release identity is exactly the declared column surface
# ---------------------------------------------------------------------------


def test_release_role_matches_the_declared_surface(sandbox: Sandbox) -> None:
    verify_quarantine_release_role(sandbox.release_pool, sandbox.schema)


def test_one_extra_column_grant_fails_startup(sandbox: Sandbox) -> None:
    extra = f"qr_extra_{uuid.uuid4().hex[:8]}"
    sandbox.admin.execute(f"CREATE ROLE \"{extra}\" LOGIN PASSWORD '{PASSWORD}'")
    try:
        for statement in quarantine_release_grants(sandbox.schema, extra):
            sandbox.admin.execute(statement)
        sandbox.admin.execute(
            f'GRANT UPDATE (quarantine_observed_at_us) ON "{sandbox.schema}".admission_leases '
            f'TO "{extra}"'
        )
        pool = speech_pool(
            _connection_url(sandbox.url, extra), schema=sandbox.schema, max_size=1, name="qr-extra"
        )
        pool.open()
        try:
            with pytest.raises(
                ValueError, match=r"declared surface at admission_leases\.quarantine_observed_at_us"
            ):
                verify_quarantine_release_role(pool, sandbox.schema)
        finally:
            pool.close()
    finally:
        _drop_role(sandbox.admin, extra)


def test_one_missing_column_grant_fails_startup(sandbox: Sandbox) -> None:
    deficit = f"qr_deficit_{uuid.uuid4().hex[:8]}"
    sandbox.admin.execute(f"CREATE ROLE \"{deficit}\" LOGIN PASSWORD '{PASSWORD}'")
    try:
        for statement in quarantine_release_grants(sandbox.schema, deficit):
            sandbox.admin.execute(statement)
        sandbox.admin.execute(
            f'REVOKE UPDATE (held_attempts) ON "{sandbox.schema}".workspace_quotas FROM "{deficit}"'
        )
        pool = speech_pool(
            _connection_url(sandbox.url, deficit),
            schema=sandbox.schema,
            max_size=1,
            name="qr-deficit",
        )
        pool.open()
        try:
            with pytest.raises(
                ValueError, match=r"declared surface at workspace_quotas\.held_attempts"
            ):
                verify_quarantine_release_role(pool, sandbox.schema)
        finally:
            pool.close()
    finally:
        _drop_role(sandbox.admin, deficit)


def test_disabled_append_only_trigger_fails_startup(db: Sandbox) -> None:
    db.admin.execute(f'ALTER TABLE "{db.schema}".{TABLE} DISABLE TRIGGER {TRIGGER}')
    try:
        with pytest.raises(ValueError, match="trigger"):
            verify_quarantine_release_role(db.release_pool, db.schema)
    finally:
        db.admin.execute(f'ALTER TABLE "{db.schema}".{TABLE} ENABLE TRIGGER {TRIGGER}')


# ---------------------------------------------------------------------------
# 15/21/23. safety net, pre-existing evidence, append-only behaviour
# ---------------------------------------------------------------------------


def test_every_ledger_row_matches_a_released_lease(db: Sandbox) -> None:
    db.release()

    mismatched = db.query(
        f"SELECT l.lease_id FROM {TABLE} l JOIN admission_leases a USING (lease_id) "
        "WHERE a.lease_state <> 'released' OR a.cleanup_state <> 'complete' "
        "OR a.budget_released_at_us IS DISTINCT FROM l.budget_released_at_us "
        "OR l.evidence_observed_at_us > l.budget_released_at_us"
    )
    assert mismatched == []


def test_pre_existing_machine_evidence_rolls_back_the_operator_write(db: Sandbox) -> None:
    db.add_stop_evidence(
        evidence_id="stop-machine", kind="instance_terminated_by_controller", reason=None
    )
    snapshot = db.projections()

    assert db.rejection(db.evidence()) == "quarantine_release_conflict"
    assert db.projections() == snapshot
    stops = db.query("SELECT evidence_id FROM execution_stop_evidence")
    assert [stop["evidence_id"] for stop in stops] == ["stop-machine"]
    assert db.count(TABLE) == 0


@pytest.mark.parametrize(
    "observed,reason,kind",
    [
        (2, REASON, KIND),
        (1, "   ", KIND),
        (1, REASON, "instance_terminated_by_controller"),
    ],
)
def test_ledger_refuses_incoherent_evidence_rows(
    db: Sandbox, observed: int, reason: str, kind: str
) -> None:
    db.seed(stages=("stage-main", "stage-extra"))
    db.release()
    snapshot = db.projections()

    with pytest.raises(psycopg.errors.CheckViolation):
        db.admin.execute(
            f"INSERT INTO {TABLE} (lease_id, operation_id, attempt_id, stage_id, host_node_id, "
            "instance_id, instance_generation, fencing_token, request_digest, evidence_kind, "
            "evidence_digest, evidence_observed_at_us, reason, budget_released_at_us, "
            "released_submit_intent_ids) VALUES "
            "('lease-stage-extra', %s, %s, 'stage-extra', %s, %s, 1, 1, %s, %s, %s, %s, %s, "
            "1, '[]')",
            (OPERATION, ATTEMPT, HOST, INSTANCE, DIGEST, kind, DIGEST, observed, reason),
        )
    db.admin.rollback()
    assert db.projections() == snapshot
    assert db.count(TABLE) == 1


def test_appended_ledger_row_cannot_be_rewritten(db: Sandbox) -> None:
    db.release()

    for statement in (
        f"UPDATE {TABLE} SET reason = 'rewritten'",
        f"DELETE FROM {TABLE}",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            db.admin.execute(statement)
        db.admin.rollback()
    assert db.count(TABLE) == 1


# ---------------------------------------------------------------------------
# 22. a replay never takes a business lock
# ---------------------------------------------------------------------------


def test_replay_is_not_blocked_by_a_lease_lock_holder(db: Sandbox) -> None:
    evidence = db.evidence()
    db.release(evidence)
    holder = psycopg.connect(db.url, autocommit=False)
    try:
        holder.execute(f'SET search_path TO "{db.schema}",pg_catalog')
        holder.execute("SELECT 1 FROM admission_leases WHERE lease_id = %s FOR UPDATE", (LEASE,))
        started = time.monotonic()
        replay = db.service(timeout_seconds=1).release(evidence)
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    assert replay.idempotent_replay is True
    assert elapsed < 0.9, "a replay must not queue behind the lease row lock"


# ---------------------------------------------------------------------------
# 16-17. fault injection
# ---------------------------------------------------------------------------


def test_terminated_backend_leaves_no_partial_release(db: Sandbox) -> None:
    evidence = db.evidence()
    # A test-only AFTER UPDATE trigger parks the transaction after it has already
    # written B's evidence, closed B's intent and mutated A's projections, so the
    # termination happens mid-transaction rather than before the first write.
    advisory = 987_654_321
    db.admin.execute(
        f'CREATE FUNCTION "{db.schema}".qr_park() RETURNS trigger LANGUAGE plpgsql AS '
        f"$$ BEGIN PERFORM pg_catalog.pg_advisory_xact_lock({advisory}); RETURN NULL; END $$"
    )
    db.admin.execute(
        f'CREATE TRIGGER qr_park AFTER UPDATE ON "{db.schema}".workspace_quotas '
        f'FOR EACH ROW EXECUTE FUNCTION "{db.schema}".qr_park()'
    )
    blocker = psycopg.connect(db.url, autocommit=False)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            db.service().release(db.evidence())
        except BaseException as error:
            outcome.append(error)

    try:
        blocker.execute("SELECT pg_advisory_xact_lock(%s)", (advisory,))
        thread = threading.Thread(target=run)
        thread.start()
        pid = _wait_for_lock_wait(db, "UPDATE workspace_quotas")
        assert db.admin.execute("SELECT pg_terminate_backend(%s) AS stopped", (pid,))
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        blocker.rollback()
        blocker.close()
        db.admin.execute(f'DROP TRIGGER IF EXISTS qr_park ON "{db.schema}".workspace_quotas')
        db.admin.execute(f'DROP FUNCTION IF EXISTS "{db.schema}".qr_park()')

    assert len(outcome) == 1
    assert isinstance(outcome[0], QuarantineUnknownError)
    assert db.count(TABLE) == 0
    assert db.count("execution_stop_evidence") == 0
    lease = db.query("SELECT * FROM admission_leases WHERE lease_id = %s", (LEASE,))[0]
    assert (lease["lease_state"], lease["budget_released_at_us"]) == ("quarantined", None)
    intent = db.query("SELECT * FROM submit_intents WHERE submit_intent_id = %s", (INTENT,))[0]
    assert (intent["execution_state"], intent["version"]) == ("unknown_quarantined", 1)
    quota = db.query("SELECT * FROM workspace_quotas")[0]
    assert (quota["held_attempts"], quota["quarantined_attempts"], quota["version"]) == (0, 1, 1)
    host = db.query("SELECT * FROM host_nodes")[0]
    assert host["quarantined_unified_physical_memory_bytes"] == 1
    assert host["version"] == 1
    retry = db.service().release(evidence)
    assert retry.idempotent_replay is False
    assert db.count(TABLE) == 1


def test_lost_commit_acknowledgement_is_unknown_until_queried(db: Sandbox) -> None:
    """A **service-level simulation** of a lost COMMIT acknowledgement.

    The wrapper commits through the real adapter and only then raises
    ``QuarantineUnknownError``, so this case proves the business conclusion —
    "after an unknown result, querying by the original key returns the replay" —
    and nothing about the transport. It is **not** evidence that a real
    "COMMIT sent, acknowledgement lost" outage is classified as unknown: that
    branch is the adapter's ``except OperationalError``, exercised by
    ``test_terminated_backend_leaves_no_partial_release`` with a terminated
    backend. Design §9.2 #17 records the same limitation.
    """
    evidence = db.evidence()

    class _LostAck:
        def __init__(self, inner: QuarantineReleaseService) -> None:
            self._inner = inner

        def lookup(self, evidence: ReleaseEvidence) -> ReleaseOutcome | None:
            return None

        def commit(self, evidence: ReleaseEvidence) -> ReleaseOutcome:
            self._inner.release(evidence)
            raise QuarantineUnknownError

    with pytest.raises(QuarantineUnknownError):
        QuarantineReleaseService(_LostAck(db.service())).release(evidence)

    committed = db.ledger_rows()
    assert len(committed) == 1
    replay = db.service().release(evidence)
    assert replay.idempotent_replay is True
    assert replay.ledger.budget_released_at_us == committed[0]["budget_released_at_us"]
    assert db.count(TABLE) == 1
    host = db.query("SELECT * FROM host_nodes")[0]
    assert (host["quarantined_unified_physical_memory_bytes"], host["version"]) == (0, 2)


def test_concurrent_same_key_and_evidence_releases_once(db: Sandbox) -> None:
    barrier = threading.Barrier(2)
    evidence = db.evidence()

    def run() -> ReleaseOutcome:
        barrier.wait(timeout=10)
        return db.service().release(evidence)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result(timeout=30) for future in (pool.submit(run), pool.submit(run))]

    assert sorted(outcome.idempotent_replay for outcome in outcomes) == [False, True]
    assert db.count(TABLE) == 1
    assert db.count("execution_stop_evidence") == 1
    host = db.query("SELECT * FROM host_nodes")[0]
    assert host["quarantined_unified_physical_memory_bytes"] == 0
    assert host["version"] == 2


def test_two_requests_that_both_miss_the_ledger_still_release_once(db: Sandbox) -> None:
    evidence = db.evidence()
    blocker = psycopg.connect(db.url, autocommit=False)
    barrier = threading.Barrier(2)
    try:
        blocker.execute(f'SET search_path TO "{db.schema}",pg_catalog')
        blocker.execute("SELECT 1 FROM admission_leases WHERE lease_id = %s FOR UPDATE", (LEASE,))

        def run() -> ReleaseOutcome:
            barrier.wait(timeout=10)
            return db.service().release(evidence)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            _wait_for_waiters(db, 2)
            blocker.rollback()
            outcomes = [future.result(timeout=30) for future in futures]
    finally:
        blocker.close()

    assert sorted(outcome.idempotent_replay for outcome in outcomes) == [False, True]
    assert db.count(TABLE) == 1
    assert db.count("execution_stop_evidence") == 1
    host = db.query("SELECT * FROM host_nodes")[0]
    assert (host["quarantined_unified_physical_memory_bytes"], host["version"]) == (0, 2)


def _wait_for_waiters(db: Sandbox, expected: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        rows = db.admin.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = %s "
            "AND state = 'active' AND wait_event_type = 'Lock'",
            (db.release_role,),
        ).fetchone()
        assert rows is not None
        if int(rows["n"]) >= expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"no {expected} release sessions waited on a business lock")


def test_concurrent_same_key_and_different_evidence_has_one_winner(db: Sandbox) -> None:
    barrier = threading.Barrier(2)
    evidences = [db.evidence(), db.evidence(reason=REASON + " (different)")]
    results: list[str] = []

    def run(evidence: ReleaseEvidence) -> None:
        barrier.wait(timeout=10)
        try:
            db.service().release(evidence)
            results.append("committed")
        except QuarantineReleaseRejectedError as error:
            results.append(error.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, evidence) for evidence in evidences]
        for future in futures:
            future.result(timeout=30)

    assert sorted(results) == ["committed", "idempotency_conflict"]
    assert db.count(TABLE) == 1
    assert db.count("execution_stop_evidence") == 1


def _wait_for_lock_wait(sandbox: Sandbox, needle: str) -> int:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        rows = sandbox.admin.execute(
            "SELECT pid, query FROM pg_stat_activity "
            "WHERE usename = %s AND state = 'active' AND wait_event_type = 'Lock'",
            (sandbox.release_role,),
        ).fetchall()
        for row in rows:
            if needle in (row["query"] or ""):
                return int(row["pid"])
        time.sleep(0.02)
    raise AssertionError(f"no release session waited on {needle}")


def test_runtime_rejects_a_privileged_identity(db: Sandbox) -> None:
    settings = _app_settings(db, role="speechnexus")
    runtime = QuarantineReleaseRuntime(settings)
    with pytest.raises(ValueError, match="startup rejected"):
        runtime.open()
    assert runtime.service is None


def _app_settings(db: Sandbox, *, role: str | None = None) -> Settings:
    return Settings(
        service_name="speechnexus-api-test",
        environment="test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
        database_schema=db.schema,
        quarantine_release_database_url=SecretStr(db.dsn(role or db.release_role)),
        quarantine_release_concurrency=2,
        quarantine_release_timeout_seconds=3,
    )


async def test_assembled_app_releases_through_its_own_runtime(db: Sandbox) -> None:
    app = create_app(
        settings=_app_settings(db),
        readiness=ReadinessService(
            [StaticProbe("postgresql"), StaticProbe("object_storage")], timeout_seconds=1
        ),
    )
    evidence = db.evidence()
    body = {
        "evidence_kind": evidence.evidence_kind,
        "evidence_digest": evidence.evidence_digest,
        "observed_at": _stamp(evidence.observed_at_us),
        "reason": evidence.reason,
    }
    headers = {
        "X-Request-ID": "request-quarantine-release-1",
        "Idempotency-Key": "operator-poll-0001",
    }
    url = PATH.format(lease_id=LEASE)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        first = await client.post(url, json=body, headers=headers)
        replay = await client.post(url, json=body, headers=headers)
        wrong = await client.post(
            url,
            json={**body, "reason": "different operator material"},
            headers=headers,
        )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert wrong.status_code == 409, wrong.text
    released = first.json()
    assert released["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["budget_released_at"] == released["budget_released_at"]
    validate_response(RELEASE_OPERATION, 200, {"body": released})
    assert wrong.json()["error"]["code"] == "idempotency_conflict"
    assert db.count(TABLE) == 1
    assert db.count("execution_stop_evidence") == 1


def test_a_release_waits_for_the_pool_lock_before_any_submit_intent_lock(db: Sandbox) -> None:
    """The release joins the global lock order; it never takes a later lock first.

    The gateway's own order is ``resource_pools → admission_leases →
    submit_intents``. If the release closed intents before taking the pool row
    lock, a gateway session holding the pool and asking for the intent would close
    a cycle. This case observes the order directly: while the release blocks on the
    pool row, it must hold **no** lock on ``submit_intents``.
    """
    evidence = db.evidence()
    holder = psycopg.connect(db.url, autocommit=False)
    completed: list[ReleaseOutcome] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            completed.append(db.service().release(evidence))
        except BaseException as error:
            failures.append(error)

    holder.execute(f'SET search_path TO "{db.schema}",pg_catalog')
    holder.execute("SELECT pool_id FROM resource_pools WHERE pool_id = %s FOR UPDATE", (POOL,))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        pid = _wait_for_lock_wait(db, "FROM resource_pools")
        held = db.query(
            "SELECT c.relname AS name, l.granted AS granted, l.mode AS mode "
            "FROM pg_catalog.pg_locks l "
            "JOIN pg_catalog.pg_class c ON c.oid = l.relation "
            "WHERE l.pid = %s AND c.relname IN ('resource_pools', 'submit_intents')",
            (pid,),
        )
        blockers = db.query("SELECT pg_catalog.pg_blocking_pids(%s) AS pids", (pid,))[0]["pids"]
        holder_pid = holder.info.backend_pid
    finally:
        holder.rollback()
        holder.close()
        thread.join(timeout=30)

    assert not thread.is_alive()
    assert failures == []
    # It is queued behind the holder at the pool row...
    assert holder_pid in blockers, (blockers, holder_pid)
    assert "resource_pools" in {row["name"] for row in held}, held
    # ...and has not touched a later lock in the order.
    assert "submit_intents" not in {row["name"] for row in held}, held
    assert len(completed) == 1
    assert completed[0].idempotent_replay is False
    assert db.count(TABLE) == 1


def test_a_revoked_select_grant_is_named_instead_of_reported_as_a_conflict(db: Sandbox) -> None:
    """Runtime revocation is a deployment defect: 42501, not 409 evidence conflict."""
    db.admin.execute(f'REVOKE SELECT ON "{db.schema}".admission_leases FROM "{db.release_role}"')
    snapshot = db.projections()
    try:
        with pytest.raises(QuarantineSurfaceError) as raised:
            db.service().release(db.evidence())
    finally:
        db.admin.execute(f'GRANT SELECT ON "{db.schema}".admission_leases TO "{db.release_role}"')

    assert raised.value.code == "quarantine_release_privilege_missing"
    assert not isinstance(raised.value, QuarantineReleaseRejectedError)
    assert db.projections() == snapshot
    assert db.count(TABLE) == 0


def test_a_mid_transaction_privilege_loss_rolls_back_and_names_the_defect(db: Sandbox) -> None:
    """A revoked UPDATE column fails after the evidence and CAS writes, and rolls back."""
    db.admin.execute(
        f'REVOKE UPDATE (held_attempts) ON "{db.schema}".workspace_quotas FROM "{db.release_role}"'
    )
    snapshot = db.projections()
    try:
        with pytest.raises(QuarantineSurfaceError) as raised:
            db.service().release(db.evidence())
    finally:
        db.admin.execute(
            f'GRANT UPDATE (held_attempts) ON "{db.schema}".workspace_quotas TO "{db.release_role}"'
        )

    assert raised.value.code == "quarantine_release_privilege_missing"
    assert db.projections() == snapshot
    assert db.count("execution_stop_evidence") == 0
    assert db.count(TABLE) == 0


@pytest.mark.parametrize("table", [TABLE, "execution_stop_evidence"])
def test_a_missing_relation_is_reported_as_an_unmigrated_schema(db: Sandbox, table: str) -> None:
    """A missing table is "migrate", not "change the evidence".

    Two positions are covered: the level-one ledger read (``admission_quarantine_
    releases``) and a write deeper in the transaction (the stop evidence), so both
    read and write refusals name the same deployment defect.
    """
    snapshot = db.projections()
    hidden = f"{table}_hidden"
    db.admin.execute(f'ALTER TABLE "{db.schema}".{table} RENAME TO {hidden}')
    try:
        with pytest.raises(QuarantineSurfaceError) as raised:
            db.service().release(db.evidence())
    finally:
        db.admin.execute(f'ALTER TABLE "{db.schema}".{hidden} RENAME TO {table}')

    assert raised.value.code == "quarantine_release_schema_not_migrated"
    assert not isinstance(raised.value, QuarantineReleaseRejectedError)
    assert db.projections() == snapshot
    assert db.count(TABLE) == 0


@pytest.mark.parametrize("privilege", ["USAGE", "SELECT", "USAGE, SELECT"])
def test_an_extra_sequence_grant_fails_startup(sandbox: Sandbox, privilege: str) -> None:
    """Sequences are invisible to the table enumeration, so they are checked alone.

    The release transaction needs no sequence at all: any ``USAGE``/``SELECT`` on a
    sequence in the configured schema widens the surface without showing up in the
    column-by-column equality.
    """
    extra = f"qr_seq_{uuid.uuid4().hex[:8]}"
    sequence = f"qr_sequence_{uuid.uuid4().hex[:8]}"
    sandbox.admin.execute(f'CREATE SEQUENCE "{sandbox.schema}"."{sequence}"')
    sandbox.admin.execute(f"CREATE ROLE \"{extra}\" LOGIN PASSWORD '{PASSWORD}'")
    try:
        for statement in quarantine_release_grants(sandbox.schema, extra):
            sandbox.admin.execute(statement)
        pool = speech_pool(
            _connection_url(sandbox.url, extra),
            schema=sandbox.schema,
            max_size=1,
            name="qr-sequence",
        )
        pool.open()
        try:
            verify_quarantine_release_role(pool, sandbox.schema)
            sandbox.admin.execute(
                f'GRANT {privilege} ON SEQUENCE "{sandbox.schema}"."{sequence}" TO "{extra}"'
            )
            with pytest.raises(ValueError, match="sequence"):
                verify_quarantine_release_role(pool, sandbox.schema)
        finally:
            pool.close()
    finally:
        _drop_role(sandbox.admin, extra)
        sandbox.admin.execute(f'DROP SEQUENCE IF EXISTS "{sandbox.schema}"."{sequence}"')


def test_the_runtime_refuses_to_start_with_an_extra_sequence_grant(sandbox: Sandbox) -> None:
    """The process boundary fails closed, not just the internal check."""
    extra = f"qr_seq_runtime_{uuid.uuid4().hex[:8]}"
    sequence = f"qr_sequence_runtime_{uuid.uuid4().hex[:8]}"
    sandbox.admin.execute(f'CREATE SEQUENCE "{sandbox.schema}"."{sequence}"')
    sandbox.admin.execute(f"CREATE ROLE \"{extra}\" LOGIN PASSWORD '{PASSWORD}'")
    try:
        for statement in quarantine_release_grants(sandbox.schema, extra):
            sandbox.admin.execute(statement)
        sandbox.admin.execute(
            f'GRANT USAGE ON SEQUENCE "{sandbox.schema}"."{sequence}" TO "{extra}"'
        )
        runtime = QuarantineReleaseRuntime(_app_settings(sandbox, role=extra))
        # The process boundary must name the stray sequence, not just refuse:
        # otherwise the operator only learns "startup rejected".
        with pytest.raises(
            ValueError, match=f"startup rejected.*sequence privileges on {sequence}"
        ):
            runtime.open()
        assert runtime.service is None
    finally:
        _drop_role(sandbox.admin, extra)
        sandbox.admin.execute(f'DROP SEQUENCE IF EXISTS "{sandbox.schema}"."{sequence}"')
