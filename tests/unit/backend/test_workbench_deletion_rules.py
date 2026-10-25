"""Pinned deletion plans must never authorize a stale or partial cascade."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_backend.workbench.domain.deletion import (
    ConfirmationMismatchError,
    DeletionDependency,
    DeletionDependencyConflictError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionPlanExpiredError,
    DeletionTarget,
    dependency_snapshot,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)
PROJECT = DeletionTarget("workbench", "project", "project-synthetic", 1)
TRANSCRIPT = DeletionTarget("workbench", "transcript", "transcript-synthetic", 2)
COPY = DeletionTarget("workbench", "saved_asset", "saved-synthetic", 1)


def plan(*, ordinary: bool = False) -> DeletionPlan:
    return DeletionPlan.create(
        plan_id="plan-synthetic",
        workspace_id="workspace-synthetic",
        target=PROJECT,
        mode="ordinary" if ordinary else "cascade",
        now=NOW,
        dependencies=(
            DeletionDependency(TRANSCRIPT, "owned_child", "cascade"),
            DeletionDependency(COPY, "independent_copy", "retain"),
        ),
    )


def test_plan_sorts_dependencies_and_pins_default_expiry() -> None:
    candidate = plan()
    assert candidate.expires_at == NOW + timedelta(seconds=300)
    assert candidate.revision == 1
    assert candidate.eligible
    assert candidate.dependencies[0].target == COPY
    assert candidate.confirmed_targets == (PROJECT, TRANSCRIPT)
    assert candidate.dependencies == dependency_snapshot(candidate.dependencies, mode="cascade")


def test_ordinary_plan_blocks_owned_children_and_independent_copy_is_retained() -> None:
    candidate = plan(ordinary=True)
    assert not candidate.eligible
    assert {dep.disposition for dep in candidate.dependencies} == {"retain", "blocks"}
    with pytest.raises(DeletionDependencyConflictError):
        candidate.confirm(
            expected_revision=1,
            current_target=PROJECT,
            current_dependencies=candidate.dependencies,
            targets=(PROJECT,),
            now=NOW,
        )


@pytest.mark.parametrize(
    "targets", [(PROJECT,), (PROJECT, TRANSCRIPT, COPY), (PROJECT, TRANSCRIPT, TRANSCRIPT)]
)
def test_confirm_requires_exact_unique_target_set(targets: tuple[DeletionTarget, ...]) -> None:
    candidate = plan()
    with pytest.raises(ConfirmationMismatchError):
        candidate.confirm(
            expected_revision=1,
            current_target=PROJECT,
            current_dependencies=candidate.dependencies,
            targets=targets,
            now=NOW,
        )


def test_confirmation_is_order_independent_and_does_not_include_source_speech_artifact() -> None:
    candidate = plan()
    candidate.confirm(
        expected_revision=1,
        current_target=PROJECT,
        current_dependencies=candidate.dependencies,
        targets=(TRANSCRIPT, PROJECT),
        now=NOW,
    )
    assert all(target.owner == "workbench" for target in candidate.confirmed_targets)


@pytest.mark.parametrize("mutation", ["target", "dependencies", "revision", "expiry"])
def test_changed_plan_or_dependencies_cannot_publish_tombstones(mutation: str) -> None:
    candidate = plan()
    with pytest.raises(
        DeletionPlanExpiredError if mutation == "expiry" else DeletionPlanChangedError
    ):
        candidate.confirm(
            expected_revision=2 if mutation == "revision" else 1,
            current_target=replace(PROJECT, revision=2) if mutation == "target" else PROJECT,
            current_dependencies=() if mutation == "dependencies" else candidate.dependencies,
            targets=(PROJECT, TRANSCRIPT),
            now=candidate.expires_at if mutation == "expiry" else NOW,
        )


def test_cross_owner_reference_blocks_even_cascade_and_copy_remains_independent() -> None:
    speech = DeletionTarget("speech", "artifact", "artifact-synthetic", 1)
    deps = dependency_snapshot(
        (
            DeletionDependency(speech, "persistent_reference", "cascade"),
            DeletionDependency(COPY, "independent_copy", "cascade"),
        ),
        mode="cascade",
    )
    assert next(dep for dep in deps if dep.target == speech).disposition == "blocks"
    assert next(dep for dep in deps if dep.target == COPY).disposition == "retain"


def test_duplicate_relation_is_rejected_even_when_disposition_differs() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        dependency_snapshot(
            (
                DeletionDependency(TRANSCRIPT, "owned_child", "cascade"),
                DeletionDependency(TRANSCRIPT, "owned_child", "blocks"),
            ),
            mode="cascade",
        )
