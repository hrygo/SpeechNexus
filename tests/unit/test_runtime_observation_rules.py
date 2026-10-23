"""Ready, quarantine and physical release require separate explicit facts."""

from dataclasses import replace
from typing import Any

import pytest

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import RuntimeRecord, RuntimeRejectedError
from speechnexus_backend.speech.domain.runtime_observations import (
    RuntimeObservationDecision,
    RuntimeObservedStop,
    decide_runtime_observation,
    require_observation_progress,
)
from tests.fake.runtime_intents import NOW, OWNER, PLAN
from tests.fake.runtime_observations import (
    HEAD,
    HOST,
    INSTANCE,
    OBSERVATION,
    OBSERVE,
    PREREQUISITES,
    READINESS,
    RECORD,
)


def _decide(**changes: Any) -> RuntimeObservationDecision:
    values: dict[str, Any] = {
        "command": OBSERVE,
        "record": RECORD,
        "head": HEAD,
        "instance": INSTANCE,
        "host": HOST,
        "ownership": OWNER,
        "prerequisites": PREREQUISITES,
        "now_us": NOW,
    }
    values.update(changes)
    return decide_runtime_observation(**values)


def test_qualified_ready_replaces_every_peak_dimension_with_steady_once() -> None:
    decision = _decide()
    assert decision.held == HOST.held.subtract(PLAN.peak).add(PLAN.steady)
    assert decision.quarantined == HOST.quarantined
    assert (decision.base_budget_kind, decision.instance_state, decision.ready_at_us) == (
        "steady",
        "ready",
        NOW,
    )
    assert decision.intent_state == "succeeded" and INSTANCE.base_budget_kind == "peak"


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        replace(READINESS, fencing_token=8),
        replace(READINESS, instance_generation=2),
        replace(READINESS, qualification_digest="sha256:" + "f" * 64),
        replace(READINESS, topology_digest="sha256:" + "f" * 64),
        replace(READINESS, expires_at_us=NOW),
        replace(READINESS, verified_at_us=NOW + 1),
    ],
)
def test_controller_ready_does_not_substitute_for_matching_live_gateway_qualification(
    receipt: Any,
) -> None:
    with pytest.raises(RuntimeRejectedError, match="reconciliation_required"):
        _decide(command=replace(OBSERVE, readiness=receipt))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"ownership": replace(OWNER, owner_id="other-owner")}, "stale_fencing_token"),
        ({"ownership": replace(OWNER, expires_at_us=NOW)}, "stale_fencing_token"),
        (
            {"ownership": replace(OWNER, owner_state="reconciling_takeover")},
            "reconciliation_required",
        ),
        ({"host": replace(HOST, topology_digest="sha256:" + "f" * 64)}, "recipe_mismatch"),
        (
            {"head": replace(HEAD, current_generation=2, generation_high_water=2)},
            "stale_instance_generation",
        ),
        ({"command": replace(OBSERVE, expected_instance_version=2)}, "version_conflict"),
        ({"command": replace(OBSERVE, expected_intent_version=2)}, "version_conflict"),
        ({"head": replace(HEAD, stop_intent_id="stop-new")}, "version_conflict"),
    ],
)
def test_authority_versions_and_current_generation_fence_budget_changes(
    change: dict[str, Any], code: str
) -> None:
    with pytest.raises(RuntimeRejectedError, match=code):
        _decide(**change)


@pytest.mark.parametrize("phase", ["preparing", "loading", "warming_up"])
def test_loading_keeps_complete_peak_without_warmup_receipt(phase: str) -> None:
    command = replace(
        OBSERVE,
        observation=replace(OBSERVATION, intent_state="pending", observed_instance_state=phase),
        readiness=None,
    )
    result = _decide(command=command)
    assert result.held == HOST.held and result.quarantined == HOST.quarantined
    assert (
        result.base_budget_kind == "peak"
        and result.ready_at_us is None
        and result.instance_state == phase
    )


def test_failed_does_not_prove_physical_release() -> None:
    command = replace(
        OBSERVE,
        observation=replace(
            OBSERVATION,
            intent_state="failed",
            observed_instance_state="failed",
            error_code="runtime_start_failed",
        ),
        readiness=None,
    )
    result = _decide(command=command)
    assert result.instance_state == "failed" and result.base_budget_kind == "peak"
    assert result.held == HOST.held and result.stopped_at_us is None


def test_superseded_epoch_records_actual_terminal_fact_but_quarantines_peak() -> None:
    result = _decide(
        ownership=replace(OWNER, fencing_token=8, owner_state="reconciling_takeover"),
        command=replace(OBSERVE, readiness=None),
    )
    assert result.held == HOST.held.subtract(PLAN.peak)
    assert result.quarantined == HOST.quarantined.add(PLAN.peak)
    assert result.intent_state == "succeeded" and result.instance_state == "quarantined"
    assert result.base_budget_kind == "q_peak" and result.ready_at_us is None
    assert result.host_state == "quarantined" and result.quarantine_reason


def test_over_peak_disables_host_even_below_configured_quarantine_threshold() -> None:
    command = replace(
        OBSERVE,
        observation=replace(OBSERVATION, observed_peak_memory=PhysicalResources(21, 0, 0, 0, 0, 0)),
        readiness=None,
    )
    result = _decide(command=command, host=replace(HOST, quarantine_threshold_ppm=1000000))
    assert result.instance_state == "quarantined" and result.base_budget_kind == "q_peak"
    assert result.host_state == "quarantined" and result.stopped_at_us is None


def test_later_ready_never_automatically_unquarantines() -> None:
    instance = replace(
        INSTANCE,
        instance_state="quarantined",
        base_budget_kind="q_peak",
        quarantine_reason="controller could not identify the original runtime",
    )
    host = replace(
        HOST,
        held=HOST.held.subtract(PLAN.peak),
        quarantined=HOST.quarantined.add(PLAN.peak),
        node_state="quarantined",
    )
    result = _decide(instance=instance, host=host, command=replace(OBSERVE, readiness=None))
    assert result.held == host.held and result.quarantined == host.quarantined
    assert result.base_budget_kind == "q_peak" and result.ready_at_us is None
    assert result.intent_state == "succeeded" and result.host_state == "quarantined"


def _stop(**changes: Any) -> RuntimeObservationDecision:
    request = replace(RECORD.request, intent_id="runtime-stop", action="stop")
    observation = replace(
        OBSERVATION,
        intent_id=request.intent_id,
        action="stop",
        observed_instance_state="stopped",
        stop_evidence=RuntimeObservedStop(True, True, NOW - 100),
    )
    values: dict[str, Any] = {
        "command": replace(OBSERVE, observation=observation, readiness=None),
        "record": RuntimeRecord(
            request,
            PLAN,
            RECORD.creator_id,
            RECORD.request_digest,
            "pending",
            NOW - 1000,
            NOW - 1000,
            1,
        ),
        "head": replace(HEAD, stop_intent_id=request.intent_id),
        "instance": replace(INSTANCE, instance_state="draining"),
    }
    values.update(changes)
    return _decide(**values)


def test_physical_stop_and_all_released_leases_return_only_base() -> None:
    result = _stop()
    assert result.held == HOST.held.subtract(PLAN.peak) and result.quarantined == HOST.quarantined
    assert (
        result.base_budget_kind == "released"
        and result.instance_state == "stopped"
        and result.stopped_at_us == NOW
    )


@pytest.mark.parametrize(
    "fact", ["all_leases_released", "all_submits_terminal", "other_runtime_intents_terminal"]
)
def test_physical_stop_is_recorded_but_base_waits_for_cleanup(fact: str) -> None:
    result = _stop(prerequisites=replace(PREREQUISITES, **{fact: False}))
    assert result.held == HOST.held and result.quarantined == HOST.quarantined
    assert result.intent_state == "reconciling" and result.instance_state == "stopping"
    assert result.base_budget_kind == "peak" and result.stopped_at_us is None


def test_verified_stop_releases_quarantined_base_without_restoring_host() -> None:
    host = replace(
        HOST,
        held=HOST.held.subtract(PLAN.peak),
        quarantined=HOST.quarantined.add(PLAN.peak),
        node_state="quarantined",
    )
    instance = replace(
        INSTANCE,
        instance_state="quarantined",
        base_budget_kind="q_peak",
        quarantine_reason="controller lost the original container identity",
    )
    result = _stop(host=host, instance=instance)
    assert result.held == host.held and result.quarantined == HOST.quarantined
    assert result.base_budget_kind == "released" and result.host_state == "quarantined"


def test_observation_progress_preserves_creation_peak_and_terminal_facts() -> None:
    require_observation_progress(OBSERVATION, OBSERVATION)
    pending = replace(OBSERVATION, intent_state="pending", observed_instance_state="loading")
    for changed in (
        replace(pending, created_at_us=pending.created_at_us - 1),
        replace(pending, observed_peak_memory=None),
        replace(pending, observed_peak_memory=PLAN.memory.steady),
    ):
        with pytest.raises(RuntimeRejectedError, match="intent_conflict"):
            require_observation_progress(pending, changed)
    with pytest.raises(RuntimeRejectedError, match="version_conflict"):
        require_observation_progress(
            pending, replace(pending, updated_at_us=pending.updated_at_us - 1)
        )
    with pytest.raises(RuntimeRejectedError, match="intent_conflict"):
        require_observation_progress(
            OBSERVATION, replace(OBSERVATION, updated_at_us=OBSERVATION.updated_at_us + 1)
        )


def test_submicrosecond_future_observation_cannot_release_peak_early() -> None:
    observation = replace(OBSERVATION, updated_at_us=NOW, updated_submicrosecond="1")
    with pytest.raises(RuntimeRejectedError, match="reconciliation_required"):
        _decide(command=replace(OBSERVE, observation=observation))
    assert (
        _decide(command=replace(OBSERVE, observation=observation), now_us=NOW + 1).base_budget_kind
        == "steady"
    )


def test_creation_and_update_precision_are_part_of_observation_progress() -> None:
    pending = replace(
        OBSERVATION,
        intent_state="pending",
        observed_instance_state="loading",
        created_submicrosecond="1",
        updated_submicrosecond="2",
    )
    with pytest.raises(RuntimeRejectedError, match="intent_conflict"):
        require_observation_progress(pending, replace(pending, created_submicrosecond="2"))
    with pytest.raises(RuntimeRejectedError, match="version_conflict"):
        require_observation_progress(pending, replace(pending, updated_submicrosecond="1"))


def test_pending_stop_cannot_regress_from_stopping_to_draining() -> None:
    request = replace(RECORD.request, intent_id="runtime-stop", action="stop")
    observation = replace(
        OBSERVATION,
        intent_id=request.intent_id,
        action="stop",
        intent_state="pending",
        observed_instance_state="draining",
    )
    command = replace(OBSERVE, observation=observation, readiness=None)
    with pytest.raises(RuntimeRejectedError, match="reconciliation_required"):
        _stop(command=command, instance=replace(INSTANCE, instance_state="stopping"))
