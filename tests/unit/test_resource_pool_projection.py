"""Aggregate invariants and the stored-state to declared-status mapping.

These are the rules the administrative projection may not bend: a pool is only
ready when a contributing instance serves, quarantined capacity is never free
capacity, and an unknown or unordered aggregate fails closed instead of being
projected.
"""

from __future__ import annotations

import pytest

from speechnexus_backend.speech.domain.resource_pools import (
    ResourcePool,
    ResourcePoolFacts,
    pool_status,
    project,
)


def facts(**overrides: object) -> ResourcePoolFacts:
    values: dict[str, object] = {
        "pool_id": "asr-realtime",
        "pool_state": "active",
        "service_class": "realtime",
        "node_ids": ("node-a", "node-b"),
        "ready_instances": 2,
        "max_active_slots": 6,
        "realtime_reserved_slots": 4,
        "active_slots": 1,
        "quarantine_slots": 1,
    }
    values.update(overrides)
    return ResourcePoolFacts(**values)  # type: ignore[arg-type]


def test_ready_requires_a_contributing_node_capacity_and_a_serving_instance() -> None:
    assert pool_status(facts()) == "ready"
    assert pool_status(facts(ready_instances=0)) == "degraded"
    assert pool_status(facts(node_ids=(), ready_instances=0, max_active_slots=0)) == "unavailable"
    assert pool_status(facts(max_active_slots=0)) == "unavailable"
    for state in ("draining", "disabled", "synthetic-unknown"):
        assert pool_status(facts(pool_state=state)) == "unavailable"


def test_projection_maps_every_declared_field_from_the_aggregate() -> None:
    pool = project(facts())
    assert pool == ResourcePool(
        pool_id="asr-realtime",
        node_ids=("node-a", "node-b"),
        service_class="realtime",
        max_active_slots=6,
        realtime_reserved_slots=4,
        active_slots=1,
        quarantine_slots=1,
        status="ready",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"realtime_reserved_slots": 7},
        {"active_slots": 3, "quarantine_slots": 4},
        {"node_ids": ("node-b", "node-a")},
        {"node_ids": ("node-a", "node-a")},
        {"pool_id": "synthetic-pool"},
        {"service_class": "synthetic-class"},
        {"ready_instances": -1},
    ],
)
def test_invalid_aggregates_are_refused_instead_of_projected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        project(facts(**overrides))


def test_an_instance_without_a_contributing_node_is_inconsistent() -> None:
    with pytest.raises(ValueError):
        facts(node_ids=(), ready_instances=1)
