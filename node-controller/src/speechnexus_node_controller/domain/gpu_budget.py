"""The VRAM bound one engine generation runs under, and what it takes to state it.

An approved template must not carry a GPU memory number: the same file is approved
for a production card and for a development box, so a constant there would be
correct for exactly one of them. That leaves the bound as deployment
configuration, and this module is its only definition point.

Omitting the bound is not the same as leaving the engine unbounded. vLLM's own
default is ``--gpu-memory-utilization 0.92``: a deployment that states nothing
gets 92 % of the whole card, which fails on a shared card at engine start-up and
is invisible on a dedicated one (#1262). A GPU template whose bound this
deployment cannot state is therefore refused before anything is created, and the
refusal names the two ways to state it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

BUDGET_MIB_VARIABLE: Final = "SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_MIB"
"""The VRAM one engine may use, in MiB. Also the cap libvgpu enforces."""

TOTAL_MIB_VARIABLE: Final = "SPEECHNEXUS_CONTROLLER_ENGINE_GPU_TOTAL_MIB"
"""How much VRAM the node's card has, in MiB. Only needed to derive a ratio."""

UTILIZATION_VARIABLE: Final = "SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_UTILIZATION"
"""The ratio the engine is told to take, when the deployment states it directly."""

_DERIVATION_SCALE: Final = 10_000


class GpuBudgetUndeclaredError(Exception):
    """A GPU template asked for VRAM this deployment never bounded."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @classmethod
    def undeclared(cls) -> GpuBudgetUndeclaredError:
        return cls(
            "the approved template requests a GPU but this deployment states no VRAM "
            f"bound for an engine: set {UTILIZATION_VARIABLE}, or set "
            f"{BUDGET_MIB_VARIABLE} together with {TOTAL_MIB_VARIABLE} so the ratio is "
            "derived from the budget. Starting the engine without a bound is not a "
            "neutral default: the engine then claims 92% of the whole card, which "
            "fails where the card is shared (#1262)"
        )


@dataclass(frozen=True)
class GpuBudget:
    """The ratio one engine is told to take, and how the deployment stated it."""

    utilization: float
    source: Literal["declared", "derived"]

    @property
    def argument(self) -> tuple[str, str]:
        """The engine argv pair this budget adds."""
        return ("--gpu-memory-utilization", f"{self.utilization:g}")


def resolve_gpu_budget(
    *,
    gpu_device_count: int,
    budget_mib: int | None,
    total_mib: int | None,
    utilization: float | None,
) -> GpuBudget | None:
    """The bound for this template, ``None`` when the template asks for no GPU.

    A stated ratio wins: it is the operator saying what this engine may take, and
    the budget stays the enforcement mechanism rather than the source of the flag.
    Otherwise the ratio comes from the two declared numbers. Both being absent is
    the state vLLM fills in with 92 %, so it refuses instead.
    """
    if not gpu_device_count:
        return None
    if utilization is not None:
        return GpuBudget(utilization=utilization, source="declared")
    if budget_mib is None or total_mib is None:
        raise GpuBudgetUndeclaredError.undeclared()
    # Round down: a derived ratio must never ask for more VRAM than the budget
    # allows, and a ratio with more digits than an operator would write reads as
    # a measurement rather than a bound.
    ratio = math.floor(min(1.0, budget_mib / total_mib) * _DERIVATION_SCALE)
    return GpuBudget(utilization=ratio / _DERIVATION_SCALE, source="derived")
