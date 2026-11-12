"""Recorded instance facts, keyset positions and the page acceptance rules.

Every field carries the fact it projects: a ready instance has a recorded
readiness time, a stopped instance has a recorded stop time, a quarantined
instance has a recorded reason, and the quarantine reason is reported only while
the instance is quarantined. A page that storage did not return in strictly
ascending key order is refused instead of being re-sorted here.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.model_instances import ModelInstanceService
from speechnexus_backend.speech.domain.model_instances import (
    InstancePosition,
    ModelInstance,
    ModelInstanceLookup,
    ModelInstancePage,
    ModelInstanceQuery,
    ModelInstanceUnavailableError,
)

DIGEST = "sha256:" + "a" * 64


def instance(**overrides: Any) -> ModelInstance:
    values: dict[str, Any] = {
        "instance_id": "inst-asr-1",
        "host_node_id": "node-a",
        "gateway_id": "gateway-1",
        "binding_id": "asr-quality",
        "binding_revision": "7",
        "binding_revision_id": "binding-revision-1",
        "pool_id": "asr-realtime",
        "generation": 1,
        "config_digest": DIGEST,
        "state": "ready",
        "ready_at_us": 1_700_000_000_000_001,
        "stopped_at_us": None,
        "quarantine_reason": None,
    }
    values.update(overrides)
    return ModelInstance(**values)


def test_a_ready_instance_accepts_work_and_reports_its_recorded_readiness() -> None:
    ready = instance()
    assert ready.accepting_new_work is True
    assert ready.position == InstancePosition("inst-asr-1", 1)
    assert ready.current_quarantine_reason is None


def test_accepting_new_work_follows_the_state_and_nothing_else() -> None:
    for state in ("preparing", "loading", "warming_up", "draining", "stopping", "failed"):
        assert instance(state=state, ready_at_us=None).accepting_new_work is False
    assert (
        instance(state="stopped", stopped_at_us=1_700_000_000_000_002).accepting_new_work is False
    )
    assert (
        instance(state="quarantined", quarantine_reason="repeated load failure").accepting_new_work
        is False
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"state": "ready", "ready_at_us": None}, "requires its recorded readiness time"),
        ({"state": "stopped", "stopped_at_us": None}, "requires its recorded stop time"),
        ({"state": "quarantined", "quarantine_reason": None}, "requires its recorded reason"),
        ({"state": "quarantined", "quarantine_reason": "  "}, "requires its recorded reason"),
        ({"state": "paused"}, "Invalid instance state"),
        ({"pool_id": "asr-elsewhere"}, "Invalid instance pool"),
        ({"config_digest": "sha256:short"}, "Invalid instance configuration digest"),
        ({"binding_revision": "0"}, "Invalid configuration revision"),
        ({"instance_id": "Inst-ASR"}, "Invalid asset identity"),
        ({"generation": 0}, "Invalid asset counter"),
        ({"ready_at_us": -1}, "Invalid asset counter"),
    ],
)
def test_a_fact_that_contradicts_its_source_is_refused(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        instance(**overrides)


def test_the_quarantine_reason_is_reported_only_while_quarantined() -> None:
    quarantined = instance(state="quarantined", quarantine_reason="repeated load failure")
    assert quarantined.current_quarantine_reason == "repeated load failure"
    # The store keeps the recorded reason after the instance leaves quarantine;
    # the current condition is no longer a quarantine, so the field is null.
    stopped = instance(
        state="stopped",
        stopped_at_us=1_700_000_000_000_002,
        quarantine_reason="repeated load failure",
    )
    assert stopped.current_quarantine_reason is None


def test_positions_order_by_instance_id_then_generation() -> None:
    assert InstancePosition("inst-a", 1) < InstancePosition("inst-a", 2)
    assert InstancePosition("inst-a", 9) < InstancePosition("inst-b", 1)
    assert sorted(
        [
            InstancePosition("inst-b", 1),
            InstancePosition("inst-a", 2),
            InstancePosition("inst-a", 1),
        ]
    ) == [
        InstancePosition("inst-a", 1),
        InstancePosition("inst-a", 2),
        InstancePosition("inst-b", 1),
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "Invalid asset counter"),
        ({"limit": 101}, "Invalid asset counter"),
        ({"limit": 1, "host_node_id": "Node-A"}, "Invalid asset identity"),
        ({"limit": 1, "state": "paused"}, "Invalid instance state filter"),
        ({"limit": 1, "after": ("inst-a", 1)}, "Invalid instance cursor position"),
    ],
)
def test_a_query_outside_the_declared_bounds_is_refused(
    arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ModelInstanceQuery(**arguments)


def test_a_query_matches_only_the_rows_its_filters_and_frontier_select() -> None:
    query = ModelInstanceQuery(10, InstancePosition("inst-a", 2), "node-a", "ready")
    assert query.matches(instance(instance_id="inst-a", generation=3)) is True
    assert query.matches(instance(instance_id="inst-a", generation=2)) is False
    assert query.matches(instance(instance_id="inst-a", generation=1)) is False
    assert (
        query.matches(instance(instance_id="inst-b", generation=1, host_node_id="node-b")) is False
    )
    assert (
        query.matches(
            instance(instance_id="inst-b", generation=1, state="draining", ready_at_us=None)
        )
        is False
    )


def test_a_page_must_be_strictly_ascending_and_frontiered_by_its_last_row() -> None:
    first, second = instance(), instance(instance_id="inst-asr-2")
    assert ModelInstancePage((first, second), second.position).next_position == second.position
    assert ModelInstancePage((), None).items == ()
    with pytest.raises(ValueError, match="strictly ascending"):
        ModelInstancePage((second, first), first.position)
    with pytest.raises(ValueError, match="frontier must be its last row"):
        ModelInstancePage((first, second), first.position)
    with pytest.raises(ValueError, match="may only carry recorded instances"):
        ModelInstancePage((cast(Any, object()),), None)


class Reads:
    def __init__(self, rows: Any = (), lookup: Any = None) -> None:
        self.rows, self.lookup = rows, lookup
        self.calls: list[Any] = []

    def scan(self, query: ModelInstanceQuery) -> Any:
        self.calls.append(("scan", query))
        if isinstance(self.rows, Exception):
            raise self.rows
        return self.rows

    def find(self, instance_id: str, generation: int) -> Any:
        self.calls.append(("find", instance_id, generation))
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return self.lookup


def test_the_service_derives_has_more_from_the_over_read_row_only() -> None:
    first, second = instance(), instance(instance_id="inst-asr-2")
    service = ModelInstanceService(cast(Any, Reads((first, second))))
    page = service.list(ModelInstanceQuery(1))
    assert page.items == (first,)
    assert page.next_position == first.position
    service = ModelInstanceService(cast(Any, Reads((first,))))
    page = service.list(ModelInstanceQuery(1))
    assert page.items == (first,)
    assert page.next_position is None


@pytest.mark.parametrize(
    "rows",
    [
        [instance()],  # not a tuple: the port contract is broken
        (
            instance(),
            instance(
                instance_id="inst-asr-1",
                generation=1,
                state="stopped",
                ready_at_us=None,
                stopped_at_us=1,
            ),
        ),  # duplicate position
        (instance(instance_id="inst-asr-2"), instance(instance_id="inst-asr-1")),  # descending
        (instance(host_node_id="node-b"),),  # filter violated
    ],
)
def test_a_page_that_contradicts_the_statement_is_refused(rows: Any) -> None:
    service = ModelInstanceService(cast(Any, Reads(rows)))
    with pytest.raises(ModelInstanceUnavailableError):
        service.list(ModelInstanceQuery(10, host_node_id="node-a"))


def test_an_over_read_beyond_one_row_is_refused() -> None:
    rows = (instance(), instance(instance_id="inst-asr-2"), instance(instance_id="inst-asr-3"))
    service = ModelInstanceService(cast(Any, Reads(rows)))
    with pytest.raises(ModelInstanceUnavailableError):
        service.list(ModelInstanceQuery(1))


def test_a_read_failure_becomes_the_single_unavailable_error() -> None:
    service = ModelInstanceService(cast(Any, Reads(ValueError("bad row"))))
    with pytest.raises(ModelInstanceUnavailableError):
        service.list(ModelInstanceQuery(1))
    service = ModelInstanceService(cast(Any, Reads(lookup=KeyError("missing column"))))
    with pytest.raises(ModelInstanceUnavailableError):
        service.get("inst-asr-1", 1)


def test_a_lookup_returns_the_requested_generation_and_refuses_a_substitute() -> None:
    recorded = instance()
    service = ModelInstanceService(cast(Any, Reads(lookup=ModelInstanceLookup(True, recorded))))
    assert service.get("inst-asr-1", 1).instance == recorded
    service = ModelInstanceService(
        cast(Any, Reads(lookup=ModelInstanceLookup(True, instance(generation=2))))
    )
    with pytest.raises(ModelInstanceUnavailableError):
        service.get("inst-asr-1", 1)
    service = ModelInstanceService(cast(Any, Reads(lookup=object())))
    with pytest.raises(ModelInstanceUnavailableError):
        service.get("inst-asr-1", 1)


def test_a_lookup_rejects_a_malformed_identity_before_storage_is_touched() -> None:
    reads = Reads(lookup=ModelInstanceLookup(False, None))
    service = ModelInstanceService(cast(Any, reads))
    with pytest.raises(ModelInstanceUnavailableError):
        service.get("Inst-ASR", 1)
    with pytest.raises(ModelInstanceUnavailableError):
        service.get("inst-asr-1", 0)
    assert reads.calls == []


def test_an_absent_instance_and_an_absent_generation_are_distinct_lookups() -> None:
    unknown = ModelInstanceLookup(False, None)
    assert (unknown.instance_exists, unknown.instance) == (False, None)
    generation_unknown = ModelInstanceLookup(True, None)
    assert (generation_unknown.instance_exists, generation_unknown.instance) == (True, None)
