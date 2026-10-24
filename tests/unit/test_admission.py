"""Complete resource decisions and canonical context; no scheduler/GPU qualification."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.domain.admission import (
    AdmissionDecision,
    AdmissionFact,
    AdmissionIdentity,
    AdmissionRejectedError,
    AdmissionResult,
    AdmitRecipe,
    ApprovedStage,
    OriginalBusinessCommand,
    decide_admission,
    require_live_authority,
    unknown,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.infrastructure.admission_codec import (
    business_command_from_value,
    prepare_admission,
    request_from_wire,
    request_to_wire,
    validate_business_command,
)
from tests.fake.admission import (
    ADMISSION,
    APPROVED,
    BUSINESS,
    COMMAND,
    DOCUMENT,
    HOSTS,
    POOLS,
    QUOTA,
    RUNTIMES,
    STAGE,
)
from tests.fake.runtime_intents import NOW, PLAN


def _decide(command: AdmitRecipe = COMMAND, **changes: Any) -> AdmissionDecision:
    facts: dict[str, Any] = {
        "quota": QUOTA,
        "pools": POOLS,
        "hosts": HOSTS,
        "runtimes": RUNTIMES,
        "now_us": NOW,
    }
    facts.update(changes)
    return decide_admission(command, **facts)


def test_reserves_only_stage_increments_and_preserves_baseline_and_quarantine() -> None:
    decision = _decide()
    assert decision.reservations[0].physical == PLAN.per_slot
    assert decision.host_held == (("host-test", HOSTS[0].held.add(PLAN.per_slot)),)
    assert decision.instance_held_slots == ((STAGE.instance_key, 1),)
    assert HOSTS[0].held != PLAN.per_slot and HOSTS[0].quarantined.values[0] == 7


def test_all_stages_sharing_generation_accumulate_every_dimension_once() -> None:
    second = replace(
        STAGE, stage_id="stage-review", lease_id="lease-admit-review", reserved_slots=2
    )
    command = replace(
        COMMAND,
        request=replace(ADMISSION, stages=(second, STAGE)),
        approved_recipe=replace(
            APPROVED, stages=(*APPROVED.stages, ApprovedStage(second.stage_id, 2, PLAN))
        ),
    )
    decision = _decide(command)
    assert [r.stage.stage_id for r in decision.reservations] == ["stage-main", "stage-review"]
    assert decision.host_held == (("host-test", HOSTS[0].held.add(PLAN.per_slot.scaled(3))),)
    assert decision.instance_held_slots == ((STAGE.instance_key, 3),)


@pytest.mark.parametrize("change", ["missing", "extra", "digest", "binding", "policy", "class"])
def test_recipe_must_match_exact_approved_stage_set_and_revision(change: str) -> None:
    approved = {
        "missing": replace(
            APPROVED, stages=(*APPROVED.stages, ApprovedStage("stage-review", 1, PLAN))
        ),
        "extra": replace(APPROVED, stages=(ApprovedStage("stage-other", 1, PLAN),)),
        "digest": replace(APPROVED, recipe_digest="sha256:" + "a" * 64),
        "binding": replace(
            APPROVED,
            manifest_id="other-manifest",
            stages=(
                ApprovedStage(
                    "stage-main",
                    1,
                    replace(PLAN, binding=replace(PLAN.binding, manifest_id="other-manifest")),
                ),
            ),
        ),
        "policy": replace(APPROVED, policy_revision_id="other-policy"),
        "class": replace(APPROVED, service_class="batch"),
    }[change]
    with pytest.raises(AdmissionRejectedError, match="recipe_mismatch"):
        _decide(replace(COMMAND, approved_recipe=approved))


@pytest.mark.parametrize("dimension", ["generated_samples", "input_bytes", "audio_duration_us"])
def test_unknown_business_quota_dimension_is_never_treated_as_zero(dimension: str) -> None:
    command = replace(
        COMMAND,
        approved_recipe=replace(
            APPROVED, quota_dimensions=frozenset({"concurrent_attempts", dimension})
        ),
    )
    with pytest.raises(AdmissionRejectedError, match="policy_or_resource_disabled"):
        _decide(command)


@pytest.mark.parametrize("dimension", [0, 3, 4, 5])
def test_each_unified_host_dimension_includes_quarantined_occupancy(dimension: int) -> None:
    host = HOSTS[0]
    values = list(host.capacity.values)
    values[dimension] = (
        host.held.values[dimension]
        + host.quarantined.values[dimension]
        + PLAN.per_slot.values[dimension]
        - 1
    )
    host = replace(host, capacity=PhysicalResources(*values))
    with pytest.raises(AdmissionRejectedError, match="capacity_exhausted"):
        _decide(hosts=(host,))


@pytest.mark.parametrize("dimension", [1, 2, 3, 4, 5])
def test_discrete_memory_and_auxiliary_dimensions_are_independent(dimension: int) -> None:
    peak = PhysicalResources(0, 20, 10, 200, 10, 50)
    steady = PhysicalResources(0, 10, 5, 100, 5, 25)
    per_slot = PhysicalResources(0, 3, 2, 20, 2, 5)
    plan = replace(
        PLAN,
        memory_architecture="discrete_device_memory",
        peak=peak,
        steady=steady,
        per_slot=per_slot,
    )
    held, quarantined = steady, PhysicalResources(0, 7, 3, 20, 2, 3)
    limits = [0, 100, 100, 2000, 100, 1000]
    limits[dimension] = (
        held.values[dimension] + quarantined.values[dimension] + per_slot.values[dimension] - 1
    )
    host = replace(
        HOSTS[0],
        memory_architecture="discrete_device_memory",
        capacity=PhysicalResources(*limits),
        held=held,
        quarantined=quarantined,
    )
    runtime = RUNTIMES[0]
    runtime = replace(
        runtime,
        instance=replace(runtime.instance, plan=plan),
        observation=replace(
            runtime.observation, reserved_memory=plan.memory, observed_peak_memory=plan.memory.peak
        ),
    )
    command = replace(
        COMMAND, approved_recipe=replace(APPROVED, stages=(ApprovedStage("stage-main", 1, plan),))
    )
    with pytest.raises(AdmissionRejectedError, match="capacity_exhausted"):
        _decide(command, hosts=(host,), runtimes=(runtime,))


def test_batch_floor_is_not_charged_again_when_realtime_occupies_it() -> None:
    runtime = replace(RUNTIMES[0], instance=replace(RUNTIMES[0].instance, held_slots=1))
    command = replace(
        COMMAND,
        request=replace(
            ADMISSION, service_class="batch", stages=(replace(STAGE, reserved_slots=3),)
        ),
        approved_recipe=replace(APPROVED, service_class="batch"),
    )
    assert _decide(command, runtimes=(runtime,)).instance_held_slots[0][1] == 4
    too_much = replace(
        command, request=replace(command.request, stages=(replace(STAGE, reserved_slots=4),))
    )
    with pytest.raises(AdmissionRejectedError, match="capacity_exhausted"):
        _decide(too_much)


def test_realtime_can_use_all_slots_but_quarantined_batch_keeps_its_charge() -> None:
    full = replace(COMMAND, request=replace(ADMISSION, stages=(replace(STAGE, reserved_slots=4),)))
    assert _decide(full).instance_held_slots[0][1] == 4
    runtime = replace(
        RUNTIMES[0], instance=replace(RUNTIMES[0].instance, quarantined_slots=1), batch_slots=1
    )
    batch = replace(
        COMMAND,
        request=replace(
            ADMISSION, service_class="batch", stages=(replace(STAGE, reserved_slots=3),)
        ),
        approved_recipe=replace(APPROVED, service_class="batch"),
    )
    with pytest.raises(AdmissionRejectedError, match="capacity_exhausted"):
        _decide(batch, runtimes=(runtime,))


@pytest.mark.parametrize("part", ["owner", "approval", "readiness", "request"])
def test_authority_windows_are_rechecked_after_waits(part: str) -> None:
    command = replace(
        COMMAND,
        request=replace(ADMISSION, deadline_at_us=NOW + 120000000),
        approved_recipe=replace(APPROVED, admission_expires_at_us=NOW + 120000000),
    )
    pools = (
        replace(POOLS[0], ownership=replace(POOLS[0].ownership, expires_at_us=NOW + 120000000)),
    )
    runtime = replace(
        RUNTIMES[0], readiness=replace(RUNTIMES[0].readiness, expires_at_us=NOW + 120000000)
    )
    code = "reconciliation_required"
    if part == "owner":
        pools = POOLS
        code = "stale_fencing_token"
    elif part == "approval":
        command = replace(command, approved_recipe=APPROVED)
        code = "policy_or_resource_disabled"
    elif part == "request":
        command = replace(command, request=replace(ADMISSION, deadline_at_us=NOW + 60000000))
        code = "lease_not_active"
    else:
        runtime = RUNTIMES[0]
    with pytest.raises(AdmissionRejectedError, match=code):
        require_live_authority(command, pools, (runtime,), NOW + 60000000)


@pytest.mark.parametrize(
    "kind", ["generation", "version", "stop", "observation", "unready", "peak", "host", "template"]
)
def test_generation_state_and_server_plan_cannot_be_replaced_by_ready_string(kind: str) -> None:
    runtime = RUNTIMES[0]
    host = HOSTS[0]
    code = "policy_or_resource_disabled"
    if kind == "generation":
        runtime = replace(
            runtime, head=replace(runtime.head, current_generation=2, generation_high_water=2)
        )
        code = "recipe_mismatch"
    elif kind == "version":
        runtime = replace(runtime, instance=replace(runtime.instance, version=3))
        code = "version_conflict"
    elif kind == "stop":
        runtime = replace(runtime, head=replace(runtime.head, stop_intent_id="stop-pending"))
    elif kind == "observation":
        runtime = replace(runtime, head=replace(runtime.head, prepare_intent_id="other-prepare"))
        code = "recipe_mismatch"
    elif kind == "unready":
        runtime = replace(runtime, instance=replace(runtime.instance, instance_state="warming_up"))
    elif kind == "peak":
        runtime = replace(runtime, instance=replace(runtime.instance, base_budget_kind="peak"))
    elif kind == "host":
        host = replace(host, node_state="quarantined")
    else:
        runtime = replace(
            runtime,
            instance=replace(
                runtime.instance, plan=replace(PLAN, per_slot=PLAN.per_slot.scaled(2))
            ),
        )
        code = "recipe_mismatch"
    with pytest.raises(AdmissionRejectedError, match=code):
        _decide(runtimes=(runtime,), hosts=(host,))


@pytest.mark.parametrize("field", ["quota", "instance", "host"])
def test_exhausted_versions_do_not_wrap(field: str) -> None:
    changes: dict[str, Any] = {}
    command = COMMAND
    if field == "quota":
        changes["quota"] = replace(QUOTA, version=MAX_VERSION)
        command = replace(COMMAND, request=replace(ADMISSION, expected_quota_version=MAX_VERSION))
    elif field == "instance":
        changes["runtimes"] = (
            replace(RUNTIMES[0], instance=replace(RUNTIMES[0].instance, version=MAX_VERSION)),
        )
        command = replace(
            COMMAND,
            request=replace(
                ADMISSION, stages=(replace(STAGE, expected_instance_version=MAX_VERSION),)
            ),
        )
    else:
        changes["hosts"] = (replace(HOSTS[0], version=MAX_VERSION),)
    with pytest.raises(AdmissionRejectedError, match="invalid_counter"):
        _decide(command, **changes)


def test_quota_counts_held_and_quarantined_attempts() -> None:
    with pytest.raises(AdmissionRejectedError, match="quota_exceeded"):
        _decide(quota=replace(QUOTA, held_attempts=2, quarantined_attempts=2))
    with pytest.raises(AdmissionRejectedError, match="version_conflict"):
        _decide(quota=replace(QUOTA, version=2))


@pytest.mark.parametrize("change", ["stage", "lease", "authorities", "slots", "versions"])
def test_invalid_or_duplicated_request_identity_is_rejected_before_a_transaction(
    change: str,
) -> None:
    with pytest.raises(ValueError):
        if change == "authorities":
            replace(ADMISSION, authorities=(*ADMISSION.authorities, *ADMISSION.authorities))
        elif change == "slots":
            replace(STAGE, reserved_slots=0)
        elif change == "stage":
            replace(ADMISSION, stages=(STAGE, replace(STAGE, lease_id="other-lease")))
        elif change == "lease":
            replace(ADMISSION, stages=(STAGE, replace(STAGE, stage_id="other-stage")))
        else:
            replace(
                ADMISSION,
                stages=(
                    STAGE,
                    replace(
                        STAGE,
                        stage_id="other-stage",
                        lease_id="other-lease",
                        expected_instance_version=3,
                    ),
                ),
            )


def test_business_canonicalization_preserves_text_and_never_reinterprets_defaults() -> None:
    first = business_command_from_value({"z": 1, "text": "synthetic é"}, max_command_bytes=1024)
    second = business_command_from_value({"text": "synthetic é", "z": 1.0}, max_command_bytes=1024)
    assert first == second
    assert first.document == '{"text":"synthetic é","z":1}'
    changed = business_command_from_value(
        {"text": "synthetic e\u0301", "z": 1}, max_command_bytes=1024
    )
    assert first.request_digest != changed.request_digest
    assert "synthetic é" not in repr(first)
    validate_business_command(first, max_command_bytes=1024)


@pytest.mark.parametrize(
    "value",
    [{"number": float("nan")}, {"number": float("inf")}, {"number": 2**53}, {"text": "\ud800"}, []],
)
def test_invalid_private_json_is_rejected_without_echoing_content(value: Any) -> None:
    with pytest.raises(ValueError, match="Invalid original effective business command"):
        business_command_from_value(value, max_command_bytes=1024)


def test_private_context_must_be_canonical_bounded_and_digest_bound() -> None:
    noncanonical = '{ "synthetic_effective_command": true }'
    command = OriginalBusinessCommand(
        noncanonical, "sha256:" + hashlib.sha256(noncanonical.encode()).hexdigest()
    )
    with pytest.raises(ValueError):
        validate_business_command(command, max_command_bytes=1024)
    with pytest.raises(ValueError):
        validate_business_command(BUSINESS, max_command_bytes=1)
    with pytest.raises(ValueError):
        OriginalBusinessCommand(DOCUMENT, "sha256:" + "a" * 64)


def test_canonical_admission_and_business_digests_have_distinct_meanings() -> None:
    prepared = prepare_admission(COMMAND, max_command_bytes=65536)
    assert prepared.request_digest != BUSINESS.request_digest
    assert json.loads(prepared.document) == request_to_wire(ADMISSION)
    assert request_from_wire(json.loads(prepared.document), BUSINESS) == ADMISSION
    assert ADMISSION.job.request_digest == BUSINESS.request_digest
    assert DOCUMENT not in repr(prepared)


def test_same_source_admission_example_and_unknown_field_rejection() -> None:
    source = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/coordination/transaction-examples.json"
        ).read_text()
    )
    value = copy.deepcopy(
        next(c["request"] for c in source["valid_commands"] if c["operation"] == "admit_recipe")
    )
    assert request_to_wire(request_from_wire(value, BUSINESS)) == value
    value["business_command"] = {"synthetic": True}
    with pytest.raises(ContractValidationError):
        request_from_wire(value, BUSINESS)


def test_unknown_and_success_results_cannot_invent_retry_or_partial_success() -> None:
    assert unknown(ADMISSION.identity).recovery == "query_original"
    with pytest.raises(ValueError):
        AdmissionResult(
            "unknown",
            ADMISSION.identity,
            error_code="outcome_unknown",
            recovery="retry_transaction",
        )
    with pytest.raises(ValueError):
        AdmissionResult("committed", ADMISSION.identity)
    with pytest.raises(ValueError):
        AdmissionFact(AdmissionIdentity("operation-admit", "attempt-admit"), (), 1)
