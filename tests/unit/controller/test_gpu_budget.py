"""The one place the engine's VRAM bound is decided (#1262)."""

from __future__ import annotations

import pytest

from speechnexus_node_controller.domain.gpu_budget import (
    GpuBudgetUndeclaredError,
    resolve_gpu_budget,
)


def test_a_template_without_a_gpu_needs_no_bound() -> None:
    """No GPU means no ratio, and an unstated budget stops being a refusal."""
    assert (
        resolve_gpu_budget(
            gpu_device_count=0,
            budget_mib=None,
            total_mib=None,
            utilization=None,
        )
        is None
    )


def test_a_stated_ratio_is_the_bound_and_wins_over_derivation() -> None:
    """The operator's number is authoritative; the budget stays the enforcer."""
    budget = resolve_gpu_budget(
        gpu_device_count=1,
        budget_mib=12316,
        total_mib=124577,
        utilization=0.0989,
    )
    assert budget is not None
    assert budget.utilization == 0.0989
    assert budget.source == "declared"
    assert budget.argument == ("--gpu-memory-utilization", "0.0989")


def test_two_declared_numbers_derive_a_ratio_that_never_exceeds_the_budget() -> None:
    """Derivation rounds down, so the engine is never allowed more than the budget."""
    budget = resolve_gpu_budget(
        gpu_device_count=1,
        budget_mib=12316,
        total_mib=124577,
        utilization=None,
    )
    assert budget is not None
    assert budget.source == "derived"
    # 12316 / 124577 = 0.098864..., floored to the four digits an operator writes.
    assert budget.utilization == 0.0988
    assert budget.utilization * 124577 <= 12316


def test_a_budget_at_or_above_the_card_derives_a_full_card_bound() -> None:
    """A dedicated card is a deployment that may state the whole device."""
    budget = resolve_gpu_budget(
        gpu_device_count=1,
        budget_mib=24576,
        total_mib=24576,
        utilization=None,
    )
    assert budget is not None
    assert budget.utilization == 1.0


@pytest.mark.parametrize(
    ("budget_mib", "total_mib"),
    [(None, None), (12316, None), (None, 124577)],
)
def test_a_gpu_template_this_deployment_cannot_bound_is_refused(
    budget_mib: int | None, total_mib: int | None
) -> None:
    """Half a derivation is still no bound, and no bound means the engine picks 92%.

    The engine's own default is the reason this is a refusal rather than a
    warning: "unset" is not documented in the intent, so a reader would take the
    resulting 92% for a bound the deployment declared.
    """
    with pytest.raises(GpuBudgetUndeclaredError) as raised:
        resolve_gpu_budget(
            gpu_device_count=1,
            budget_mib=budget_mib,
            total_mib=total_mib,
            utilization=None,
        )
    reason = raised.value.reason
    assert "SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_UTILIZATION" in reason
    assert "SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_MIB" in reason
    assert "SPEECHNEXUS_CONTROLLER_ENGINE_GPU_TOTAL_MIB" in reason
    assert "92%" in reason
