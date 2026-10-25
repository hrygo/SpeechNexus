"""Drive the persisted deletion ledger to completion before content is served.

An accepted deletion is a promise that confirmed content is erased. A restart must not
reopen content whose erasure is still owed, so startup replays the ledger first and
refuses to serve while anything remains (see ADR 0005). The replay is idempotent: the
adapter records completion in the same transaction that erases rows, and object erasure
is re-driven from the persisted project prefix.
"""

from __future__ import annotations

from typing import Protocol


class DeletionLedger(Protocol):
    """Accepted deletions whose erasure has not been recorded yet."""

    async def pending(self, *, limit: int) -> tuple[str, ...]: ...


class DeletionEraser(Protocol):
    """Erase one accepted deletion, idempotently, and publish its evidence."""

    async def run(self, deletion_id: str) -> object | None: ...


class RecoveryIncompleteError(RuntimeError):
    """The ledger still owes erasure after a bounded replay."""


class WorkbenchRecovery:
    """Replay every pending deletion, bounded, before the runtime opens content."""

    def __init__(
        self,
        ledger: DeletionLedger,
        eraser: DeletionEraser,
        *,
        limit: int = 64,
        max_deletions: int = 1024,
    ) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("recovery page size must be a positive integer")
        if type(max_deletions) is not int or max_deletions < limit:
            raise ValueError("recovery bound must cover at least one page")
        self._ledger = ledger
        self._eraser = eraser
        self._limit = limit
        self._max_deletions = max_deletions

    async def run(self) -> tuple[str, ...]:
        """Return the ids this pass replayed; raise if erasure cannot be finished."""
        replayed: list[str] = []
        while True:
            pending = await self._ledger.pending(limit=self._limit)
            if not pending:
                return tuple(replayed)
            if len(replayed) + len(pending) > self._max_deletions:
                # Bounded by construction: a ledger this large needs an operator, and
                # serving now would reopen content whose erasure is still owed.
                raise RecoveryIncompleteError(
                    "deletion ledger still owes erasure beyond the recovery bound"
                )
            for deletion_id in pending:
                # A refusal or store failure propagates: startup must fail closed rather
                # than serve content that an accepted deletion already covers.
                await self._eraser.run(deletion_id)
                replayed.append(deletion_id)
