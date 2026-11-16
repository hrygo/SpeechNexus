"""Idle reclaim: one decision table, one release gate, and no silent failure.

The reclaim answer authorizes a stop, so these tests pin the conservative direction:
unfinished work, an unconfirmed stop, a still-reserved lease, an unknown idle anchor and
an active cooldown all hold the generation, and only a verified container/process
termination reports ``reclaimed``. An engine sleep acknowledgment is recorded as the
counterexample it is: it never becomes release evidence, and a store that cannot answer
raises instead of looking like a healthy "not idle" generation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_gateway.application.instance_reclaim import (
    InstanceReclaimService,
    ReclaimSnapshot,
)
from speechnexus_gateway.domain.instance_drain import DrainInventory, InstanceIdentity
from speechnexus_gateway.domain.instance_reclaim import (
    MAX_IDLE_RECLAIM_COOLDOWN_MS,
    MIN_IDLE_RECLAIM_COOLDOWN_MS,
    ReclaimDecision,
    ReclaimFacts,
    ReclaimRecord,
    ReclaimRefusal,
    ReclaimRequest,
    ReclaimState,
    decide,
)
from speechnexus_gateway.domain.submission import (
    InstanceGenerationMismatchError,
    InvalidExecutionRequestError,
    StaleFencingTokenError,
    SubmissionStoreUnavailableError,
)
from speechnexus_gateway.domain.voice_design_release import (
    ReleaseEvidenceKind,
    VoiceDesignReleaseEvidence,
)
from tests.fake.execution import fake_ingress

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
COOLDOWN_MS = 60_000
IDENTITY = InstanceIdentity("instance-voice-1", 4, "node-1", "voice-creation", "gateway-1")


def inventory(
    *,
    submitting: tuple[str, ...] = (),
    active: tuple[str, ...] = (),
    quarantined: tuple[str, ...] = (),
) -> DrainInventory:
    return DrainInventory(submitting, active, quarantined)


def facts(**changes: Any) -> ReclaimFacts:
    fields: dict[str, Any] = {
        "loading_policy": "on_demand",
        "inventory": inventory(),
        "holds_capacity": False,
        "cooldown_ms": COOLDOWN_MS,
        "observed_at": NOW,
        "idle_anchor": NOW - timedelta(minutes=10),
        "release": None,
    }
    fields.update(changes)
    return ReclaimFacts(**fields)


def stopped_evidence(*, exited: bool = True, released: bool = True) -> VoiceDesignReleaseEvidence:
    return VoiceDesignReleaseEvidence(
        kind=ReleaseEvidenceKind.CONTAINER_STOP,
        observed_at=NOW - timedelta(seconds=5),
        exited=exited,
        release_confirmed=released,
    )


# --- the judgement table -------------------------------------------------------------


def test_an_idle_on_demand_generation_past_its_cooldown_is_reclaimable() -> None:
    decision = decide(facts())

    assert decision.state is ReclaimState.RECLAIM_DUE
    assert decision.reclaim_due is True and decision.reclaimed is False
    assert decision.refusal is None
    assert decision.idle_ms == 10 * 60 * 1000
    assert decision.value() == {
        "reclaim_state": "reclaim_due",
        "refusal": None,
        "idle_ms": 600_000,
        "idle_reclaim_cooldown_ms": COOLDOWN_MS,
    }


def test_a_resident_loading_policy_is_never_reclaimed_by_idleness() -> None:
    decision = decide(facts(loading_policy="resident"))

    assert decision.state is ReclaimState.HELD
    assert decision.refusal is ReclaimRefusal.RESIDENT_POLICY


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        ({"inventory": inventory(submitting=("submit-a",))}, ReclaimRefusal.UNFINISHED_WORK),
        ({"inventory": inventory(active=("submit-b",))}, ReclaimRefusal.UNFINISHED_WORK),
        ({"inventory": inventory(quarantined=("submit-c",))}, ReclaimRefusal.UNCONFIRMED_STOP),
        ({"holds_capacity": True}, ReclaimRefusal.CAPACITY_HELD),
        ({"idle_anchor": None}, ReclaimRefusal.ACTIVITY_UNKNOWN),
        ({"idle_anchor": NOW - timedelta(seconds=59)}, ReclaimRefusal.COOLDOWN_ACTIVE),
    ],
)
def test_each_protection_condition_holds_the_generation(
    change: dict[str, Any], refusal: ReclaimRefusal
) -> None:
    decision = decide(facts(**change))

    assert decision.state is ReclaimState.HELD
    assert decision.refusal is refusal
    assert decision.reclaim_due is False and decision.reclaimed is False


def test_a_quarantined_generation_outranks_unfinished_work() -> None:
    """An unconfirmed stop of that generation is the fact it closes on first."""
    decision = decide(facts(inventory=inventory(active=("submit-a",), quarantined=("submit-c",))))

    assert decision.refusal is ReclaimRefusal.UNCONFIRMED_STOP


def test_the_cooldown_boundary_is_inclusive_at_the_manifest_value() -> None:
    due = decide(facts(idle_anchor=NOW - timedelta(milliseconds=COOLDOWN_MS)))
    early = decide(facts(idle_anchor=NOW - timedelta(milliseconds=COOLDOWN_MS - 1)))

    assert due.state is ReclaimState.RECLAIM_DUE and due.idle_ms == COOLDOWN_MS
    assert early.refusal is ReclaimRefusal.COOLDOWN_ACTIVE and early.idle_ms == COOLDOWN_MS - 1


# --- release evidence ----------------------------------------------------------------


def test_a_verified_container_stop_reports_the_generation_reclaimed() -> None:
    decision = decide(facts(release=stopped_evidence()))

    assert decision.state is ReclaimState.RECLAIMED
    assert decision.reclaimed is True and decision.reclaim_due is False
    assert decision.refusal is None


def test_an_engine_sleep_acknowledgment_never_reclaims_the_generation() -> None:
    """Counterexample: a SUCCESS answer with freed_bytes=0 is not a release."""
    sleep_ack = VoiceDesignReleaseEvidence.engine_sleep_ack(
        observed_at=NOW - timedelta(seconds=5), status="SUCCESS", freed_bytes=0
    )

    decision = decide(facts(release=sleep_ack))

    assert sleep_ack.is_release_evidence is False
    assert decision.state is ReclaimState.HELD
    assert decision.refusal is ReclaimRefusal.RELEASE_UNCONFIRMED
    assert decision.reclaimed is False


@pytest.mark.parametrize(
    "release",
    [
        VoiceDesignReleaseEvidence(
            kind=ReleaseEvidenceKind.CONTAINER_STOP, observed_at=NOW, exited=False
        ),
        VoiceDesignReleaseEvidence(
            kind=ReleaseEvidenceKind.PROCESS_EXIT, observed_at=NOW, exited=True
        ),
        VoiceDesignReleaseEvidence(
            kind=ReleaseEvidenceKind.PROCESS_EXIT,
            observed_at=NOW,
            exited=False,
            release_confirmed=True,
        ),
    ],
)
def test_a_partial_termination_report_is_held_not_swallowed(
    release: VoiceDesignReleaseEvidence,
) -> None:
    decision = decide(facts(release=release))

    assert decision.state is ReclaimState.HELD
    assert decision.refusal is ReclaimRefusal.RELEASE_UNCONFIRMED


def test_a_terminated_generation_still_holding_gateway_work_is_not_reported_reclaimed() -> None:
    """The recovery sweep closes the executions this gateway still holds."""
    decision = decide(facts(release=stopped_evidence(), inventory=inventory(quarantined=("x",))))

    assert decision.state is ReclaimState.HELD
    assert decision.refusal is ReclaimRefusal.UNCONFIRMED_STOP


# --- fact validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"loading_policy": "sometimes"},
        {"cooldown_ms": MIN_IDLE_RECLAIM_COOLDOWN_MS - 1},
        {"cooldown_ms": MAX_IDLE_RECLAIM_COOLDOWN_MS + 1},
        {"holds_capacity": 1},
        {"idle_anchor": datetime(2026, 11, 17, 12, 0)},
        {"idle_anchor": NOW + timedelta(seconds=1)},
        {"release": stopped_evidence(), "observed_at": NOW - timedelta(minutes=1)},
    ],
)
def test_facts_outside_the_recorded_rules_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        facts(**change)


def test_only_a_held_decision_carries_a_refusal() -> None:
    with pytest.raises(InvalidExecutionRequestError):
        ReclaimDecision(state=ReclaimState.RECLAIM_DUE, cooldown_ms=COOLDOWN_MS)
    with pytest.raises(InvalidExecutionRequestError):
        ReclaimDecision(
            state=ReclaimState.RECLAIMED,
            cooldown_ms=COOLDOWN_MS,
            refusal=ReclaimRefusal.CAPACITY_HELD,
        )


# --- the service reports, and never swallows a broken read ---------------------------


def record(**changes: Any) -> ReclaimRecord:
    fields: dict[str, Any] = {
        "loading_policy": "on_demand",
        "binding_revision_id": "binding-rev-1",
        "last_fencing_token": 7,
        "observed_at": NOW,
        "inventory": inventory(),
        "holds_capacity": False,
        "idle_anchor": NOW - timedelta(minutes=10),
    }
    fields.update(changes)
    return ReclaimRecord(**fields)


class Facts:
    def __init__(
        self,
        value: ReclaimRecord | None = None,
        *,
        error: Exception | None = None,
        missing: bool = False,
    ) -> None:
        self.value = None if missing else (record() if value is None else value)
        self.error = error
        self.reads = 0

    def read_reclaim_record(self, identity: InstanceIdentity) -> ReclaimRecord | None:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.value


class Releases:
    def __init__(self, value: VoiceDesignReleaseEvidence | None = None) -> None:
        self.value = value
        self.calls: list[dict[str, Any]] = []

    def release_evidence(self, **kwargs: Any) -> VoiceDesignReleaseEvidence | None:
        self.calls.append(kwargs)
        return self.value


def request(**changes: Any) -> ReclaimRequest:
    fields: dict[str, Any] = {
        "instance_id": IDENTITY.instance_id,
        "host_node_id": IDENTITY.host_node_id,
        "pool_id": IDENTITY.pool_id,
        "instance_generation": IDENTITY.instance_generation,
        "fencing_token": 7,
        "cooldown_ms": COOLDOWN_MS,
    }
    fields.update(changes)
    return ReclaimRequest(**fields)


def service(
    facts_reader: Facts | None = None, releases: Releases | None = None
) -> tuple[InstanceReclaimService, Facts, Releases]:
    resolved_facts = facts_reader or Facts()
    resolved_releases = releases or Releases()
    return (
        InstanceReclaimService(
            ingress=fake_ingress(),
            facts=resolved_facts,
            releases=resolved_releases,
            now=lambda: NOW,
        ),
        resolved_facts,
        resolved_releases,
    )


def test_the_service_reports_the_recorded_decision_and_reads_the_node_once() -> None:
    reclaim, facts_reader, releases = service(releases=Releases(stopped_evidence()))

    snapshot = reclaim.evaluate(request())

    assert isinstance(snapshot, ReclaimSnapshot)
    assert snapshot.decision.state is ReclaimState.RECLAIMED
    assert snapshot.value() == {
        "instance_id": IDENTITY.instance_id,
        "instance_generation": IDENTITY.instance_generation,
        "pool_id": IDENTITY.pool_id,
        "loading_policy": "on_demand",
        "reclaim_state": "reclaimed",
        "refusal": None,
        "idle_ms": None,
        "idle_reclaim_cooldown_ms": COOLDOWN_MS,
    }
    assert facts_reader.reads == 1
    assert releases.calls == [
        {
            "instance_id": IDENTITY.instance_id,
            "instance_generation": IDENTITY.instance_generation,
            "pool_id": IDENTITY.pool_id,
            "binding_revision_id": "binding-rev-1",
            "fencing_token": 7,
        }
    ]


def test_a_store_that_cannot_answer_is_a_failure_not_a_held_decision() -> None:
    """Counterexample: a swallowed read failure would report a healthy idle instance."""
    reclaim, _, _ = service(Facts(error=SubmissionStoreUnavailableError("ledger unavailable")))

    with pytest.raises(SubmissionStoreUnavailableError):
        reclaim.evaluate(request())


def test_an_unrecorded_generation_is_refused_instead_of_reported_idle() -> None:
    reclaim, facts_reader, _ = service(Facts(missing=True))

    with pytest.raises(InstanceGenerationMismatchError):
        reclaim.evaluate(request())

    assert facts_reader.reads == 1


def test_a_stale_fencing_token_cannot_reclaim_a_generation() -> None:
    reclaim, _, _ = service(Facts(record(last_fencing_token=9)))

    with pytest.raises(StaleFencingTokenError):
        reclaim.evaluate(request(fencing_token=8))


def test_another_nodes_generation_is_refused_before_any_read() -> None:
    reclaim, facts_reader, _ = service()

    with pytest.raises(InstanceGenerationMismatchError):
        reclaim.evaluate(request(host_node_id="node-2"))

    assert facts_reader.reads == 0


def test_a_manifest_cooldown_below_the_contract_floor_is_refused() -> None:
    with pytest.raises(InvalidExecutionRequestError):
        request(cooldown_ms=MIN_IDLE_RECLAIM_COOLDOWN_MS - 1)
