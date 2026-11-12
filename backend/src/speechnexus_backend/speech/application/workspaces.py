"""Authoritative get, keyset reads and the two accepted workspace writes.

The service allocates the create identity, validates the frozen request fields
and derives the intent digest that the ledger stores. It owns no transaction:
the adapter resolves a replay before any mutation, so a retried key never applies
a second change. ``policy_id`` is refused because this deployment has no policy
registry that could resolve it; storing a fabricated revision would invent
authority the contract does not have.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Protocol

from speechnexus_backend.speech.domain.assets import counter, hash_bytes, identifier
from speechnexus_backend.speech.domain.workspaces import (
    MAX_IDEMPOTENCY_KEY,
    MIN_IDEMPOTENCY_KEY,
    Workspace,
    WorkspaceCreateCommand,
    WorkspaceNotFoundError,
    WorkspacePage,
    WorkspacePolicyUnsupportedError,
    WorkspaceQuery,
    WorkspaceStatus,
    WorkspaceUnavailableError,
    WorkspaceUpdateCommand,
    WorkspaceWriteReceipt,
    check_display_name,
    check_request_id,
)


class WorkspaceReads(Protocol):
    def get(self, workspace_id: str) -> Workspace | None: ...

    def scan(self, query: WorkspaceQuery) -> tuple[Workspace, ...]:
        """Return ordered rows after the key, bounded to limit + 1."""
        ...


class WorkspaceWrites(Protocol):
    def create(
        self, command: WorkspaceCreateCommand, *, workspace_id: str
    ) -> WorkspaceWriteReceipt: ...

    def update(self, command: WorkspaceUpdateCommand) -> WorkspaceWriteReceipt: ...


def new_workspace_id() -> str:
    """Allocate a server-owned identity; the contract accepts none from clients."""
    return "workspace-" + uuid.uuid4().hex


def _intent_digest(intent: dict[str, object]) -> str:
    # Named strings and integers only: no float, vendor DTO or wire envelope.
    return hash_bytes(
        json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )


def _key_digest(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None
    if not isinstance(idempotency_key, str) or not (
        MIN_IDEMPOTENCY_KEY <= len(idempotency_key) <= MAX_IDEMPOTENCY_KEY
    ):
        raise ValueError("Invalid Idempotency-Key")
    return hash_bytes(idempotency_key.encode("utf-8"))


class WorkspaceService:
    def __init__(
        self,
        reads: WorkspaceReads,
        writes: WorkspaceWrites | None = None,
        *,
        allocate: Callable[[], str] = new_workspace_id,
    ) -> None:
        self._reads = reads
        self._writes = writes
        self._allocate = allocate

    def with_writes(self, writes: WorkspaceWrites) -> WorkspaceService:
        """Compose the same authority with a separately granted writer."""
        return WorkspaceService(self._reads, writes, allocate=self._allocate)

    def get(self, workspace_id: str) -> Workspace:
        identifier(workspace_id)
        record = self._reads.get(workspace_id)
        if record is None:
            raise WorkspaceNotFoundError
        if not isinstance(record, Workspace) or record.workspace_id != workspace_id:
            raise WorkspaceUnavailableError
        return record

    def list(self, query: WorkspaceQuery) -> WorkspacePage:
        rows = self._reads.scan(query)
        if len(rows) > query.limit + 1:
            raise WorkspaceUnavailableError
        previous = query.after_id or ""
        for record in rows:
            if not isinstance(record, Workspace) or record.workspace_id <= previous:
                raise WorkspaceUnavailableError
            previous = record.workspace_id
        items = rows[: query.limit]
        return WorkspacePage(items, items[-1].workspace_id if len(rows) > query.limit else None)

    def create(
        self,
        *,
        display_name: str,
        policy_id: str | None,
        request_id: str,
        idempotency_key: str | None,
    ) -> WorkspaceWriteReceipt:
        if self._writes is None:
            raise WorkspaceUnavailableError
        check_display_name(display_name)
        check_request_id(request_id)
        if policy_id is not None:
            # The contract already constrains the identifier shape; this
            # deployment simply has no registry that could resolve it.
            raise WorkspacePolicyUnsupportedError
        command = WorkspaceCreateCommand(
            display_name=display_name,
            request_id=request_id,
            key_digest=_key_digest(idempotency_key),
            intent_digest=_intent_digest({"operation": "create", "display_name": display_name}),
        )
        receipt = self._writes.create(command, workspace_id=self._allocate())
        if not isinstance(receipt, WorkspaceWriteReceipt):
            raise WorkspaceUnavailableError
        return receipt

    def update(
        self,
        *,
        workspace_id: str,
        expected_metadata_revision: int,
        display_name: str | None,
        status: WorkspaceStatus | None,
        request_id: str,
        idempotency_key: str | None,
    ) -> WorkspaceWriteReceipt:
        if self._writes is None:
            raise WorkspaceUnavailableError
        identifier(workspace_id)
        revision = counter(expected_metadata_revision, minimum=1)
        check_request_id(request_id)
        intent: dict[str, object] = {
            "operation": "update",
            "workspace_id": workspace_id,
            # A JSON Schema integer also admits an integral JSON number; the
            # digest must not depend on that representation.
            "expected_metadata_revision": revision,
        }
        if display_name is not None:
            intent["display_name"] = check_display_name(display_name)
        if status is not None:
            intent["status"] = status
        command = WorkspaceUpdateCommand(
            workspace_id=workspace_id,
            expected_metadata_revision=revision,
            request_id=request_id,
            key_digest=_key_digest(idempotency_key),
            intent_digest=_intent_digest(intent),
            display_name=display_name,
            status=status,
        )
        receipt = self._writes.update(command)
        if not isinstance(receipt, WorkspaceWriteReceipt) or (
            receipt.workspace.workspace_id != workspace_id
        ):
            raise WorkspaceUnavailableError
        return receipt


__all__ = [
    "WorkspaceReads",
    "WorkspaceService",
    "WorkspaceWrites",
    "new_workspace_id",
]
