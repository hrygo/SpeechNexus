"""Startup replays every owed deletion before the runtime opens content."""

from __future__ import annotations

import pytest

from speechnexus_backend.workbench.application.recovery import (
    RecoveryIncompleteError,
    WorkbenchRecovery,
)


class Ledger:
    def __init__(self, pages: list[tuple[str, ...]]) -> None:
        self.pages = list(pages)
        self.limits: list[int] = []

    async def pending(self, *, limit: int) -> tuple[str, ...]:
        self.limits.append(limit)
        return self.pages.pop(0) if self.pages else ()


class Eraser:
    def __init__(self, *, refuse: str | None = None) -> None:
        self.ran: list[str] = []
        self.refuse = refuse

    async def run(self, deletion_id: str) -> object | None:
        if deletion_id == self.refuse:
            raise ValueError("store refused the erasure")
        self.ran.append(deletion_id)
        return object()


async def test_every_pending_deletion_is_replayed_in_ledger_order() -> None:
    ledger, eraser = Ledger([("a", "b"), ("c",)]), Eraser()

    assert await WorkbenchRecovery(ledger, eraser, limit=2).run() == ("a", "b", "c")
    assert eraser.ran == ["a", "b", "c"]
    assert ledger.limits == [2, 2, 2]


async def test_an_empty_ledger_replays_nothing() -> None:
    ledger, eraser = Ledger([()]), Eraser()

    assert await WorkbenchRecovery(ledger, eraser).run() == ()
    assert eraser.ran == []


async def test_a_refused_erasure_aborts_recovery_and_leaves_the_rest_owed() -> None:
    ledger, eraser = Ledger([("a", "b", "c")]), Eraser(refuse="b")

    with pytest.raises(ValueError):
        await WorkbenchRecovery(ledger, eraser).run()
    assert eraser.ran == ["a"]


async def test_a_ledger_beyond_the_bound_refuses_instead_of_looping() -> None:
    ledger, eraser = Ledger([("a",), ("b",), ("c",)]), Eraser()

    with pytest.raises(RecoveryIncompleteError):
        await WorkbenchRecovery(ledger, eraser, limit=1, max_deletions=2).run()
    assert eraser.ran == ["a", "b"]


def test_impossible_bounds_are_rejected() -> None:
    with pytest.raises(ValueError):
        WorkbenchRecovery(Ledger([]), Eraser(), limit=0)
    with pytest.raises(ValueError):
        WorkbenchRecovery(Ledger([]), Eraser(), limit=4, max_deletions=2)
