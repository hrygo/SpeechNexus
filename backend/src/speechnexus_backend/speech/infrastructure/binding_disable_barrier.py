"""Shared PostgreSQL binding lock and immutable-revision disable check."""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import RETIREMENT_TABLE


class BindingLockExecutor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...


class BindingBarrierCursor(BindingLockExecutor, Protocol):
    def fetchall(self) -> list[dict[str, Any]]: ...


def lock_binding(executor: BindingLockExecutor, binding_id: str) -> None:
    """Hold until transaction end, before resource, operation or dispatch locks."""
    identifier(binding_id)
    executor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:catalog:' || current_schema() || ':binding:' || %s, 0))",
        (binding_id,),
    )


def revision_is_disabled(cursor: BindingBarrierCursor, binding_id: str, revision_id: str) -> bool:
    """Call under lock_binding; a retired but not disabled revision remains usable."""
    identifier(binding_id)
    identifier(revision_id)
    cursor.execute(
        f"SELECT 1 AS disabled FROM {RETIREMENT_TABLE} "
        "WHERE kind='binding' AND object_id=%s AND revision_id=%s AND mode='disable'",
        (binding_id, revision_id),
    )
    return bool(cursor.fetchall())
