"""Complete physical charges include quarantine, architecture and safe counters."""

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.capacity import (
    MAX_RESOURCE_COUNTER,
    CapacityExceededError,
    MemoryArchitecture,
    PhysicalResources,
    reserve_resources,
)


def test_quarantine_and_shared_stage_costs_cannot_oversell_parent_memory() -> None:
    capacity = PhysicalResources(100, 0, 0, 2000, 50, 1000)
    held = PhysicalResources(50, 0, 0, 500, 10, 100)
    quarantined = PhysicalResources(20, 0, 0, 0, 0, 0)
    stage_cost = PhysicalResources(20, 0, 0, 250, 5, 20)
    with pytest.raises(CapacityExceededError):
        reserve_resources(
            capacity=capacity, held=held, quarantined=quarantined, requested=stage_cost.scaled(2)
        )
    updated = reserve_resources(
        capacity=capacity, held=held, quarantined=quarantined, requested=stage_cost
    )
    assert updated.unified_physical_memory_bytes == 70
    assert held.unified_physical_memory_bytes == 50
    assert quarantined.unified_physical_memory_bytes == 20


@pytest.mark.parametrize("dimension", range(6))
def test_no_other_dimension_can_be_ignored_when_memory_fits(dimension: int) -> None:
    capacity = PhysicalResources(*(100 for _ in range(6)))
    held = PhysicalResources(*(90 if index == dimension else 0 for index in range(6)))
    requested = PhysicalResources(*(11 if index == dimension else 0 for index in range(6)))
    with pytest.raises(CapacityExceededError):
        reserve_resources(
            capacity=capacity, held=held, quarantined=PhysicalResources.zero(), requested=requested
        )


def test_safe_integer_overflow_is_rejected_without_wrapping_or_changing_original() -> None:
    full = PhysicalResources(MAX_RESOURCE_COUNTER, 0, 0, 0, 0, 0)
    one = PhysicalResources(1, 0, 0, 0, 0, 0)
    with pytest.raises(CapacityExceededError):
        reserve_resources(
            capacity=full, held=full, quarantined=PhysicalResources.zero(), requested=one
        )
    with pytest.raises(ValueError):
        full.add(one)
    with pytest.raises(ValueError):
        full.scaled(2)
    with pytest.raises(ValueError):
        one.subtract(full)
    assert full.unified_physical_memory_bytes == MAX_RESOURCE_COUNTER


@pytest.mark.parametrize("value", [True, -1, 1.5, "1", MAX_RESOURCE_COUNTER + 1])
def test_domain_rejects_unsafe_or_coerced_resource_counters(value: Any) -> None:
    with pytest.raises(ValueError):
        PhysicalResources(value, 0, 0, 0, 0, 0)


def test_an_invalid_existing_ledger_does_not_look_like_an_ordinary_capacity_rejection() -> None:
    with pytest.raises(ValueError, match="existing physical"):
        reserve_resources(
            capacity=PhysicalResources.zero(),
            held=PhysicalResources(1, 0, 0, 0, 0, 0),
            quarantined=PhysicalResources.zero(),
            requested=PhysicalResources.zero(),
        )


def test_memory_architecture_prevents_double_physical_charges_and_inputs_stay_immutable() -> None:
    unified = PhysicalResources(10, 0, 0, 100, 2, 3)
    unified.require_architecture("unified_memory")
    with pytest.raises(ValueError):
        unified.require_architecture("discrete_device_memory")
    discrete = PhysicalResources(0, 10, 20, 100, 2, 3)
    discrete.require_architecture("discrete_device_memory")
    with pytest.raises(ValueError):
        discrete.require_architecture("unified_memory")
    with pytest.raises(ValueError):
        unified.require_architecture(cast(MemoryArchitecture, "other"))
    with pytest.raises(FrozenInstanceError):
        cast(Any, unified).host_memory_bytes = 1
    assert unified.scaled(0) == PhysicalResources.zero()
    assert unified.subtract(unified) == PhysicalResources.zero()
    assert PhysicalResources.zero().fits(unified)
