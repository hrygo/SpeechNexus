"""Transactional persistence boundary owned entirely by the workbench.

Production adapters must commit content, head and replay receipt atomically.
Locking reads serialize edits/deletes of a resource. claim_receipt must also
serialize an absent (scope, key), rather than SELECT then INSERT without a guard.
No HTTP, object writes, or inference belongs inside this transaction.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from speechnexus_backend.workbench.application.listings import ListingPosition
from speechnexus_backend.workbench.domain.listings import TranscriptSummary
from speechnexus_backend.workbench.domain.versions import Project, Transcript, TranscriptRevision

type MutationScope = tuple[str, str]
type VersionSnapshot = Project | Transcript


class VersionStoreError(Exception):
    """Sanitized adapter failure; commit uncertainty is preserved across boundaries.

    sqlstate is optional internal diagnostic metadata, never a public response field.
    Consumers must not infer rollback from it or retry a mutation automatically.
    """

    def __init__(self, *, sqlstate: str | None, commit_unknown: bool) -> None:
        super().__init__(
            "workbench store commit uncertain"
            if commit_unknown
            else "workbench store transaction failed"
        )
        self.sqlstate = sqlstate
        self.commit_unknown = commit_unknown


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Replay snapshot subject to the workbench's own deletion/retention policy."""

    intent_digest: str
    snapshot: VersionSnapshot = field(repr=False)


@dataclass(frozen=True, slots=True)
class ErasedMutationReceipt:
    """Keep only intent identity; content cannot be resurrected by a retry."""

    intent_digest: str


class VersionSession(Protocol):
    """Short workbench transaction; its clock is supplied by the authoritative store."""

    async def now(self) -> datetime: ...

    async def list_projects(
        self, workspace_id: str, *, limit: int, after: ListingPosition | None
    ) -> tuple[Project, ...]:
        """At most limit rows (up to 101), current heads, descending creation time/ID."""
        ...

    async def list_transcripts(
        self, project_id: str, *, limit: int, after: ListingPosition | None
    ) -> tuple[TranscriptSummary, ...] | None:
        """None for an absent/deleted parent; parent and rows share one statement view."""
        ...

    async def get_project(self, project_id: str, *, for_update: bool = False) -> Project | None: ...

    async def save_project(self, project: Project) -> None: ...

    async def get_transcript(
        self,
        transcript_id: str,
        *,
        for_update: bool = False,
    ) -> Transcript | None: ...

    async def save_transcript(self, transcript: Transcript) -> None:
        """Append its immutable head revision and atomically advance the head pointer."""
        ...

    async def get_revision(
        self, transcript_id: str, revision: int
    ) -> TranscriptRevision | None: ...

    async def get_transcript_view(
        self, transcript_id: str, revision: int | None
    ) -> tuple[Transcript, TranscriptRevision] | None:
        """Current head and selected immutable revision in one visible store snapshot.

        None if parent/transcript is absent/deleted or the requested revision is absent.
        A DB adapter must use one statement view rather than separate READ COMMITTED reads.
        """
        ...

    async def claim_receipt(
        self, scope: MutationScope, key: str
    ) -> MutationReceipt | ErasedMutationReceipt | None:
        """Lock the scoped key until commit/rollback, including when not yet present."""
        ...

    async def save_receipt(
        self, scope: MutationScope, key: str, receipt: MutationReceipt
    ) -> None: ...


class VersionUnitOfWork(Protocol):
    """A failed or cancelled transaction publishes neither content nor receipts."""

    def transaction(self) -> AbstractAsyncContextManager[VersionSession]: ...
