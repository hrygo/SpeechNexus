"""Runtime peak/drain decisions and complete immutable original identities."""

from dataclasses import replace
from typing import Any

import pytest

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.domain.runtime_intents import (
    RuntimeDeadline,
    RuntimeHead,
    RuntimeInstance,
    RuntimeMemory,
    RuntimeRecord,
    RuntimeRejectedError,
    RuntimeResult,
    prepare_resources,
    require_stop,
    unknown,
)
from tests.fake.runtime_intents import COMMAND, EMPTY, HOST, NOW, OWNER, PLAN, REQUEST


def _prepare(**changes: Any) -> PhysicalResources:
    args: dict[str, Any] = {
        "command": COMMAND,
        "ownership": OWNER,
        "pool_state": "active",
        "service_class": "realtime",
        "host": HOST,
        "head": None,
        "previous": None,
        "prerequisites": EMPTY,
        "now_us": NOW,
    }
    args.update(changes)
    return prepare_resources(**args)


def _instance(**changes: Any) -> RuntimeInstance:
    return replace(
        RuntimeInstance("instance-new", 1, PLAN, "stopped", "released", 0, 0, 6, 2, NOW - 1),
        **changes,
    )


def test_complete_peak_is_reserved_without_touching_quarantine_or_inputs() -> None:
    updated = _prepare()
    assert updated == HOST.held.add(PLAN.peak)
    assert HOST.quarantined.unified_physical_memory_bytes == 7
    with pytest.raises(RuntimeRejectedError, match="capacity_exhausted"):
        _prepare(host=replace(HOST, capacity=PhysicalResources(30, 0, 0, 2000, 100, 1000)))


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"command": replace(COMMAND, approved_plan=None)}, "unapproved_runtime_template"),
        ({"pool_state": "disabled"}, "policy_or_resource_disabled"),
        ({"service_class": "interactive"}, "policy_or_resource_disabled"),
        ({"host": replace(HOST, node_state="quarantined")}, "policy_or_resource_disabled"),
        ({"host": replace(HOST, topology_digest="sha256:" + "d" * 64)}, "recipe_mismatch"),
        ({"ownership": replace(OWNER, fencing_token=8)}, "stale_fencing_token"),
        ({"command": replace(COMMAND, owner_id="scheduler-other")}, "stale_fencing_token"),
        ({"ownership": replace(OWNER, expires_at_us=NOW)}, "stale_fencing_token"),
        (
            {"ownership": replace(OWNER, owner_state="reconciling_takeover")},
            "reconciliation_required",
        ),
        ({"now_us": -1}, "invalid_counter"),
        ({"host": replace(HOST, version=MAX_VERSION)}, "invalid_counter"),
        ({"command": replace(COMMAND, expected_head_version=1)}, "version_conflict"),
    ],
)
def test_preparation_needs_approved_exact_facts_and_live_current_authority(
    changes: dict[str, Any], code: str
) -> None:
    with pytest.raises(RuntimeRejectedError, match=code):
        _prepare(**changes)


def test_a_schema_valid_smaller_memory_reservation_cannot_undercharge_the_plan() -> None:
    smaller = RuntimeMemory(
        "unified_memory", PhysicalResources(19, 0, 0, 0, 0, 0), PLAN.memory.steady
    )
    with pytest.raises(RuntimeRejectedError, match="invalid_runtime_request"):
        _prepare(command=replace(COMMAND, request=replace(REQUEST, reserved_memory=smaller)))
    with pytest.raises(RuntimeRejectedError, match="invalid_runtime_request"):
        _prepare(
            command=replace(
                COMMAND,
                approved_plan=replace(
                    PLAN, binding=replace(PLAN.binding, approved_template_id="template-other")
                ),
            )
        )


def test_expired_deadline_is_not_renewed_or_treated_as_a_new_generation() -> None:
    request = replace(REQUEST, deadline=RuntimeDeadline("2020-01-01T00:00:00Z"))
    with pytest.raises(RuntimeRejectedError, match="lease_not_active"):
        _prepare(command=replace(COMMAND, request=request))


def test_replacement_needs_drained_old_generation_and_monotonic_high_water() -> None:
    head = RuntimeHead("instance-new", 1, 4, "runtime-old", "stop-old", 3)
    command = replace(
        COMMAND, request=replace(REQUEST, instance_generation=5), expected_head_version=3
    )
    facts = replace(EMPTY, verified_stopped=True)
    assert _prepare(
        command=command, head=head, previous=_instance(), prerequisites=facts
    ) == HOST.held.add(PLAN.peak)
    for key in (
        "all_leases_released",
        "all_submits_terminal",
        "all_runtime_intents_terminal",
        "verified_stopped",
    ):
        with pytest.raises(RuntimeRejectedError, match="gateway_drain_incomplete"):
            _prepare(
                command=command,
                head=head,
                previous=_instance(),
                prerequisites=replace(facts, **{key: False}),
            )
    with pytest.raises(RuntimeRejectedError, match="stale_instance_generation"):
        _prepare(
            command=replace(command, request=replace(command.request, instance_generation=4)),
            head=head,
            previous=_instance(),
            prerequisites=facts,
        )
    with pytest.raises(RuntimeRejectedError, match="version_conflict"):
        _prepare(
            command=replace(command, expected_head_version=2),
            head=head,
            previous=_instance(),
            prerequisites=facts,
        )
    with pytest.raises(RuntimeRejectedError, match="reconciliation_required"):
        _prepare(command=command, head=head, prerequisites=facts)


@pytest.mark.parametrize(
    "changes",
    [
        {"instance_state": "quarantined"},
        {"base_budget_kind": "peak"},
        {"quarantined_slots": 1},
        {"stopped_at_us": None},
        {"stopped_at_us": NOW + 1},
    ],
)
def test_unknown_budget_or_future_stop_time_cannot_authorize_replacement(
    changes: dict[str, Any],
) -> None:
    head = RuntimeHead("instance-new", 1, 1, "runtime-old", "stop-old", 3)
    command = replace(
        COMMAND, request=replace(REQUEST, instance_generation=2), expected_head_version=3
    )
    with pytest.raises(RuntimeRejectedError, match="gateway_drain_incomplete"):
        _prepare(
            command=command,
            head=head,
            previous=_instance(**changes),
            prerequisites=replace(EMPTY, verified_stopped=True),
        )


def test_cleanup_stop_can_be_requested_for_fenced_quarantine_without_releasing_it() -> None:
    request = replace(REQUEST, action="stop")
    command = replace(COMMAND, request=request, expected_head_version=3, approved_plan=None)
    head = RuntimeHead("instance-new", 1, 1, "runtime-old", None, 3)
    instance = _instance(
        instance_state="quarantined",
        base_budget_kind="q_steady",
        quarantined_slots=1,
        stopped_at_us=None,
    )
    facts = replace(EMPTY, all_leases_released=False, fenced_quarantine_cleanup=True)
    require_stop(
        command,
        ownership=replace(OWNER, owner_state="reconciling_takeover"),
        head=head,
        instance=instance,
        prerequisites=facts,
        now_us=NOW,
    )
    assert instance.quarantined_slots == 1 and instance.base_budget_kind == "q_steady"
    for broken in (
        replace(facts, fenced_quarantine_cleanup=False),
        replace(facts, all_submits_terminal=False),
        replace(facts, all_runtime_intents_terminal=False),
    ):
        with pytest.raises(RuntimeRejectedError, match="gateway_drain_incomplete"):
            require_stop(
                command,
                ownership=OWNER,
                head=head,
                instance=instance,
                prerequisites=broken,
                now_us=NOW,
            )
    with pytest.raises(RuntimeRejectedError, match="gateway_drain_incomplete"):
        require_stop(
            command,
            ownership=OWNER,
            head=head,
            instance=replace(instance, held_slots=1),
            prerequisites=facts,
            now_us=NOW,
        )


def test_deadline_preserves_precision_and_uses_exact_integer_time() -> None:
    first = RuntimeDeadline("2026-10-23T12:00:00.1234567Z")
    second = RuntimeDeadline("2026-10-23T12:00:00.1234568Z")
    assert first.at_us == second.at_us
    assert first.text != second.text
    assert RuntimeDeadline("2026-10-23t13:00:00.1234567+01:00").at_us == first.at_us
    with pytest.raises(ValueError):
        RuntimeDeadline("2026-10-23 12:00:00")
    with pytest.raises(ValueError):
        RuntimeDeadline("9999-12-31T23:59:59Z")


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1", 2**53])
def test_runtime_generation_is_a_strict_safe_counter(value: Any) -> None:
    with pytest.raises(ValueError):
        replace(REQUEST, instance_generation=value)


def test_unobserved_records_and_unknown_results_do_not_invent_success() -> None:
    record = RuntimeRecord(
        REQUEST, PLAN, "scheduler-test", "sha256:" + "d" * 64, "pending", NOW, NOW, 1
    )
    result = RuntimeResult("committed", REQUEST.identity, record)
    assert result.fact is not None and result.fact.observation_document is None
    with pytest.raises(ValueError):
        replace(record, intent_state="succeeded")
    with pytest.raises(ValueError):
        RuntimeResult("committed", REQUEST.identity)
    unresolved = unknown(REQUEST.identity)
    assert unresolved.fact is None and unresolved.recovery == "query_original"
    with pytest.raises(ValueError):
        replace(unresolved, recovery="retry_transaction")


def test_deadline_offset_cannot_overflow_datetime_range() -> None:
    with pytest.raises(ValueError):
        RuntimeDeadline("9999-12-31T23:59:59-23:59")
