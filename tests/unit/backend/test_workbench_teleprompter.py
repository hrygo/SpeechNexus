"""Teleprompter position rules; no HTTP, database, model or matching engine."""

from datetime import UTC, datetime

import pytest

from speechnexus_backend.workbench.domain.teleprompter import (
    MAX_VISIBLE_POSITION,
    TeleprompterState,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 24, 12, 1, tzinfo=UTC)


def state(**changes: object) -> TeleprompterState:
    values: dict[str, object] = {
        "project_id": "project-demo",
        "script_revision": 3,
        "visible_position": 120,
        "manual_hold": False,
        "updated_at": NOW,
    }
    values.update(changes)
    return TeleprompterState(**values)  # type: ignore[arg-type]


def test_initial_state_accepts_exactly_the_declared_bounds() -> None:
    assert state(visible_position=0).visible_position == 0
    assert state(visible_position=MAX_VISIBLE_POSITION).visible_position == MAX_VISIBLE_POSITION
    assert state(script_revision=1).script_revision == 1
    assert state(script_revision=10**19 - 1).script_revision == 10**19 - 1
    assert state(manual_hold=True).manual_hold is True


@pytest.mark.parametrize(
    "changes",
    [
        {"visible_position": -1},
        {"visible_position": MAX_VISIBLE_POSITION + 1},
        {"visible_position": True},
        {"visible_position": 1.5},
        {"manual_hold": 0},
        {"script_revision": 0},
        {"script_revision": 10**19},
        {"script_revision": "3"},
        {"project_id": ""},
        {"project_id": 3},
        {"updated_at": datetime(2026, 10, 24, 12, 0)},
        {"updated_at": "2026-10-24T12:00:00Z"},
    ],
)
def test_invalid_state_never_reaches_the_store(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidVersionChangeError):
        state(**changes)


def test_equal_script_revision_moves_position_and_takeover() -> None:
    current = state(visible_position=900, manual_hold=False)
    # A manual seek may move backwards; only the script revision is ordered.
    moved = current.advance(
        script_revision=3, visible_position=10, manual_hold=True, updated_at=LATER
    )
    assert (moved.script_revision, moved.visible_position, moved.manual_hold) == (3, 10, True)
    assert moved.updated_at == LATER and current.visible_position == 900


def test_newer_script_revision_is_adopted() -> None:
    moved = state().advance(
        script_revision=7, visible_position=0, manual_hold=False, updated_at=LATER
    )
    assert moved.script_revision == 7 and moved.visible_position == 0


def test_older_script_revision_cannot_drag_the_script_back() -> None:
    current = state(script_revision=5, visible_position=400)
    with pytest.raises(RevisionConflictError) as raised:
        current.advance(script_revision=4, visible_position=0, manual_hold=False, updated_at=LATER)
    assert (raised.value.expected, raised.value.actual) == (4, 5)


def test_advance_rejects_an_unrepresentable_revision_before_comparison() -> None:
    with pytest.raises(InvalidVersionChangeError):
        state().advance(script_revision=0, visible_position=0, manual_hold=False, updated_at=LATER)
