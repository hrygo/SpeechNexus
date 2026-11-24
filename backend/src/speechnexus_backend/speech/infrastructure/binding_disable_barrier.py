"""Shared catalog binding mutex and immutable-revision disable check.

The mutex is a row lock in ``coordination_keyed_locks``, not a PostgreSQL advisory lock.
The pair that must serialize is a backend transaction and an execution-gateway submit:
two independent deployments (``import-linter`` forbids either from importing the other),
so the primitive, the table name and the key text all come from
``speechnexus_portable_sql.keyed_lock`` -- one definition point both sides call, which is
what makes "same binding -> same row" a property of the code instead of two string
literals that happen to match.

Lock order. The binding row lock is the **first** lock of the transaction and is held
until commit:

    coordination_keyed_locks(binding key) -> coordination_keyed_locks(voice key) -> admission
    capacity/lease rows -> claim bucket -> canonical Job -> public Job/outbox/cancellation

Every caller below takes it before any other row lock; the gateway takes it before it
reads the retirement row that decides the disable. Reversing that order on one side only
would deadlock instead of the old "no wait at all", which is why the two sides move in
one commit.
"""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_portable_sql.keyed_lock import (
    COORDINATION_KEYED_LOCK_TABLE,
    acquire_in,
    binding_lock_key,
)


class BindingLockExecutor(Protocol):
    """Anything that can run a statement in the caller's transaction.

    Both shapes are passed in production: some callers hand over their cursor (so the
    lock shares the cursor's transaction), others the connection itself. ``acquire_in``
    resolves which connection owns the executor; this module never opens its own.
    """

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...


class BindingBarrierCursor(BindingLockExecutor, Protocol):
    def fetchall(self) -> list[dict[str, Any]]: ...


def lock_binding(executor: BindingLockExecutor, binding_id: str) -> None:
    """Hold until transaction end, before resource, operation or dispatch locks."""
    identifier(binding_id)
    acquire_in(executor, key=binding_lock_key(binding_id), table=COORDINATION_KEYED_LOCK_TABLE)


def revision_is_disabled(cursor: BindingBarrierCursor, binding_id: str, revision_id: str) -> bool:
    """Call under lock_binding; a retired but not disabled revision remains usable."""
    identifier(binding_id)
    identifier(revision_id)
    cursor.execute(
        "SELECT 1 AS disabled FROM speech_publication_retirements "
        "WHERE kind='binding' AND object_id=? AND revision_id=? AND mode='disable'",
        (binding_id, revision_id),
    )
    return bool(cursor.fetchall())
