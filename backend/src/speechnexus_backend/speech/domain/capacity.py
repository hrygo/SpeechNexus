"""Physical resource projections shared by runtime preparation and admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MAX_RESOURCE_COUNTER = 2**53 - 1
type MemoryArchitecture = Literal["unified_memory", "discrete_device_memory"]


def _counter(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_RESOURCE_COUNTER
    ):
        raise ValueError("Invalid physical resource counter")


@dataclass(frozen=True, slots=True)
class PhysicalResources:
    unified_physical_memory_bytes: int
    device_memory_bytes: int
    host_memory_bytes: int
    cpu_decode_millicores: int
    host_pcm_buffer_bytes: int
    scratch_disk_bytes: int

    def __post_init__(self) -> None:
        for value in self.values:
            _counter(value)

    @property
    def values(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.unified_physical_memory_bytes,
            self.device_memory_bytes,
            self.host_memory_bytes,
            self.cpu_decode_millicores,
            self.host_pcm_buffer_bytes,
            self.scratch_disk_bytes,
        )

    @classmethod
    def zero(cls) -> PhysicalResources:
        return cls(0, 0, 0, 0, 0, 0)

    def add(self, other: PhysicalResources) -> PhysicalResources:
        return PhysicalResources(
            *(left + right for left, right in zip(self.values, other.values, strict=True))
        )

    def subtract(self, other: PhysicalResources) -> PhysicalResources:
        return PhysicalResources(
            *(left - right for left, right in zip(self.values, other.values, strict=True))
        )

    def scaled(self, slots: int) -> PhysicalResources:
        _counter(slots)
        return PhysicalResources(*(value * slots for value in self.values))

    def fits(self, capacity: PhysicalResources) -> bool:
        return all(
            value <= limit for value, limit in zip(self.values, capacity.values, strict=True)
        )

    def require_architecture(self, architecture: MemoryArchitecture) -> None:
        if architecture == "unified_memory":
            if self.device_memory_bytes or self.host_memory_bytes:
                raise ValueError(
                    "Unified physical memory cannot be charged again as host/device RAM"
                )
        elif architecture == "discrete_device_memory":
            if self.unified_physical_memory_bytes:
                raise ValueError("Discrete memory cannot use a unified physical counter")
        else:
            raise ValueError("Unknown physical memory architecture")


class CapacityExceededError(Exception):
    """The complete requested projection cannot fit; no partial reservation."""


def reserve_resources(
    *,
    capacity: PhysicalResources,
    held: PhysicalResources,
    quarantined: PhysicalResources,
    requested: PhysicalResources,
) -> PhysicalResources:
    # Compute in Python before constructing bounded projections. Overflow of a
    # proposed charge is capacity exhaustion; an already invalid ledger must
    # instead be reconciled. Quarantine continues to consume every dimension.
    if any(
        left + right > limit
        for left, right, limit in zip(held.values, quarantined.values, capacity.values, strict=True)
    ):
        raise ValueError("Invalid existing physical resource projections")
    if any(
        left + right + extra > limit
        for left, right, extra, limit in zip(
            held.values, quarantined.values, requested.values, capacity.values, strict=True
        )
    ):
        raise CapacityExceededError("capacity_exhausted")
    return held.add(requested)
