"""Idle reclaim on the real PostgreSQL ledger; no engine model is started.

The unit suite pins the judgement table against in-memory facts. Here the gateway's own
restricted login reads the recorded generation row, its leases and its intents, so what
is covered is what a deployed gateway actually reads: the readiness instant, the newest
recorded activity, the reserved capacity and the drain buckets. Nothing is written and no
instance is stopped: the reclaim answer authorizes the Scheduler, it does not act.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_barrier import REQUEST, takeover
from tests.integration.test_gateway_stop_evidence import new_pool
from tests.integration.test_tts_durable_pg import database as database

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.instance_reclaim import InstanceReclaimService
from speechnexus_gateway.domain.instance_drain import InstanceIdentity
from speechnexus_gateway.domain.instance_reclaim import (
    ReclaimRefusal,
    ReclaimRequest,
    ReclaimState,
)
from speechnexus_gateway.domain.submission import StaleFencingTokenError
from speechnexus_gateway.domain.voice_design_release import (
    ReleaseEvidenceKind,
    VoiceDesignReleaseEvidence,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = [pytest.mark.integration]

INSTANCE = "instance-test"
GENERATION = 1
NODE = "host-test"
GATEWAY = "gateway-test"
POOL = "tts-realtime"
COOLDOWN_MS = 60_000
IDENTITY = InstanceIdentity(INSTANCE, GENERATION, NODE, POOL, GATEWAY)


def micros(at: datetime) -> int:
    delta = at - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass
class Live:
    ledger: PostgresSubmissionLedger
    reclaim: InstanceReclaimService
    ingress: GatewayIngress


class Releases:
    def __init__(self, value: VoiceDesignReleaseEvidence | None = None) -> None:
        self.value = value

    def release_evidence(self, **_: Any) -> VoiceDesignReleaseEvidence | None:
        return self.value


@dataclass
class Harness:
    live: Live
    releases: Releases

    def request(self, **changes: Any) -> ReclaimRequest:
        body: dict[str, Any] = {
            "instance_id": INSTANCE,
            "host_node_id": NODE,
            "pool_id": POOL,
            "instance_generation": GENERATION,
            "fencing_token": 1,
            "cooldown_ms": COOLDOWN_MS,
        }
        body.update(changes)
        return ReclaimRequest(**body)

    def evaluate(self, **changes: Any) -> Any:
        return self.live.reclaim.evaluate(self.request(**changes))


@pytest.fixture
def live(foundation: Foundation, tmp_path: Path) -> Harness:
    pool: GatewayPool = new_pool(foundation)
    owner = ProcessIngressOwnership(tmp_path, node_id=NODE, gateway_id=GATEWAY)
    ingress = GatewayIngress(owner, timeout_seconds=0.1)
    ledger = PostgresSubmissionLedger(pool)
    releases = Releases()
    reclaim = InstanceReclaimService(ingress=ingress, facts=ledger, releases=releases)
    try:
        yield Harness(Live(ledger, reclaim, ingress), releases)
    finally:
        owner.close()
        pool.close()


def settle_every_intent(foundation: Foundation, *, at: datetime) -> None:
    """Close every execution of the generation and release every lease it holds."""
    foundation.database.query(
        "UPDATE submit_intents s SET execution_state = 'succeeded', "
        "cleanup_state = 'complete', dispatched_at_us = %s, terminated_at_us = %s "
        "FROM admission_leases l WHERE l.lease_id = s.lease_id "
        "AND l.instance_id = %s AND l.instance_generation = %s",
        (micros(at - timedelta(minutes=30)), micros(at), INSTANCE, GENERATION),
    )
    release_every_lease(foundation, at=at)


def release_every_lease(foundation: Foundation, *, at: datetime) -> None:
    foundation.database.query(
        "UPDATE admission_leases SET lease_state = 'released', cleanup_state = 'complete', "
        "budget_released_at_us = %s WHERE instance_id = %s AND instance_generation = %s",
        (micros(at), INSTANCE, GENERATION),
    )


def set_ready_at(foundation: Foundation, *, at: datetime) -> None:
    foundation.database.query(
        "UPDATE engine_instances SET ready_at_us = %s "
        "WHERE instance_id = %s AND instance_generation = %s",
        (micros(at), INSTANCE, GENERATION),
    )


def test_the_recorded_facts_come_from_the_ledger_with_database_time(
    foundation: Foundation, live: Harness
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=10)
    set_ready_at(foundation, at=at)
    settle_every_intent(foundation, at=at)
    before = time.time()

    record = live.live.ledger.read_reclaim_record(IDENTITY)

    after = time.time()
    assert record is not None
    assert record.loading_policy == "on_demand"
    assert record.binding_revision_id == "binding-test"
    assert record.holds_capacity is False
    assert record.inventory.submitting_intent_ids == ()
    assert record.idle_anchor == at
    # The observation instant is database time, not this process's clock.
    assert before - 5 <= record.observed_at.timestamp() <= after + 5


def test_a_generation_this_gateway_never_recorded_has_no_facts(live: Harness) -> None:
    other = InstanceIdentity(INSTANCE, 99, NODE, POOL, GATEWAY)
    assert live.live.ledger.read_reclaim_record(other) is None


def test_an_idle_generation_past_its_cooldown_is_reclaimable(
    foundation: Foundation, live: Harness
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=10)
    set_ready_at(foundation, at=at)
    settle_every_intent(foundation, at=at)

    snapshot = live.evaluate()

    assert snapshot.decision.state is ReclaimState.RECLAIM_DUE
    assert snapshot.decision.refusal is None
    assert snapshot.value()["loading_policy"] == "on_demand"
    assert snapshot.value()["idle_reclaim_cooldown_ms"] == COOLDOWN_MS


def test_the_foundations_own_unfinished_intent_holds_the_generation(live: Harness) -> None:
    """A recorded-but-never-dispatched intent still holds capacity."""
    snapshot = live.evaluate()

    assert snapshot.decision.state is ReclaimState.HELD
    assert snapshot.decision.refusal is ReclaimRefusal.UNFINISHED_WORK


def test_a_generation_with_neither_readiness_nor_activity_is_not_idle(
    foundation: Foundation, live: Harness
) -> None:
    """The foundation records no readiness instant and no finished execution."""
    foundation.database.query(
        "DELETE FROM submit_intents s USING admission_leases l WHERE l.lease_id = s.lease_id "
        "AND l.instance_id = %s AND l.instance_generation = %s",
        (INSTANCE, GENERATION),
    )
    release_every_lease(foundation, at=datetime.now(UTC) - timedelta(minutes=10))

    snapshot = live.evaluate()

    assert snapshot.decision.state is ReclaimState.HELD
    assert snapshot.decision.refusal is ReclaimRefusal.ACTIVITY_UNKNOWN


def test_a_still_reserved_lease_holds_a_generation_with_no_unfinished_intent(
    foundation: Foundation, live: Harness
) -> None:
    """The completed-without-release shape: the execution ended, the slot did not."""
    at = datetime.now(UTC) - timedelta(minutes=10)
    set_ready_at(foundation, at=at)
    settle_every_intent(foundation, at=at)
    foundation.database.query(
        "UPDATE admission_leases SET lease_state = 'reserved', budget_released_at_us = NULL "
        "WHERE instance_id = %s AND instance_generation = %s",
        (INSTANCE, GENERATION),
    )

    snapshot = live.evaluate()

    assert snapshot.decision.refusal is ReclaimRefusal.CAPACITY_HELD


def test_an_unconfirmed_stop_of_the_generation_outranks_idleness(
    foundation: Foundation, live: Harness
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=10)
    set_ready_at(foundation, at=at)
    settle_every_intent(foundation, at=at)
    foundation.database.query(
        "UPDATE submit_intents SET execution_state = 'unknown_quarantined', "
        "cleanup_state = 'quarantined' WHERE submit_intent_id = 'submit-test'"
    )

    snapshot = live.evaluate()

    assert snapshot.decision.refusal is ReclaimRefusal.UNCONFIRMED_STOP


def test_verified_release_evidence_reports_the_generation_reclaimed(
    foundation: Foundation, live: Harness
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=10)
    set_ready_at(foundation, at=at)
    settle_every_intent(foundation, at=at)
    live.releases.value = VoiceDesignReleaseEvidence(
        kind=ReleaseEvidenceKind.CONTAINER_STOP,
        observed_at=datetime.now(UTC) - timedelta(seconds=5),
        exited=True,
        release_confirmed=True,
    )

    snapshot = live.evaluate()

    assert snapshot.decision.state is ReclaimState.RECLAIMED
    assert snapshot.decision.reclaimed is True


def test_a_sleep_acknowledgment_leaves_the_generation_held(
    foundation: Foundation, live: Harness
) -> None:
    at = datetime.now(UTC) - timedelta(minutes=10)
    set_ready_at(foundation, at=at)
    settle_every_intent(foundation, at=at)
    live.releases.value = VoiceDesignReleaseEvidence.engine_sleep_ack(
        observed_at=datetime.now(UTC) - timedelta(seconds=5), status="SUCCESS", freed_bytes=0
    )

    snapshot = live.evaluate()

    assert snapshot.decision.state is ReclaimState.HELD
    assert snapshot.decision.refusal is ReclaimRefusal.RELEASE_UNCONFIRMED


def test_a_sealed_barrier_makes_an_older_reclaim_token_stale(
    foundation: Foundation, live: Harness
) -> None:
    takeover(foundation)
    assert live.evaluate(fencing_token=REQUEST.min_accepted_fencing_token).decision.refusal is not (
        ReclaimRefusal.RESIDENT_POLICY
    )
    foundation.database.query(
        "UPDATE engine_instances SET last_fencing_token = %s WHERE instance_id = %s",
        (REQUEST.min_accepted_fencing_token, INSTANCE),
    )

    with pytest.raises(StaleFencingTokenError):
        live.evaluate(fencing_token=REQUEST.min_accepted_fencing_token - 1)


def test_the_reclaim_read_writes_nothing(foundation: Foundation, live: Harness) -> None:
    before = foundation.database.query(
        "SELECT (SELECT count(*) FROM submit_intents) AS intents, "
        "(SELECT count(*) FROM admission_leases) AS leases, "
        "(SELECT count(*) FROM execution_stop_evidence) AS evidence"
    )

    live.evaluate()

    after = foundation.database.query(
        "SELECT (SELECT count(*) FROM submit_intents) AS intents, "
        "(SELECT count(*) FROM admission_leases) AS leases, "
        "(SELECT count(*) FROM execution_stop_evidence) AS evidence"
    )
    assert after == before
