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
from speechnexus_portable_sql.contract import PortableSqlError

type MutationScope = tuple[str, str]
type VersionSnapshot = Project | Transcript


class VersionStoreError(Exception):
    """Sanitized adapter failure; commit uncertainty is preserved across boundaries.

    `failure` is the contract's classification of the driver error and the only
    diagnostic *and* branching fact on this boundary: a `ConflictError` means the
    server rejected a duplicate/conflicting write, any other `StorageError` means a
    storage-level refusal (`reason` says which). Adapters never hand the driver's
    SQLSTATE, error class or message across this boundary, so the same logical
    conflict classifies the same way on every engine. The attribute is internal
    metadata, never a public response field. It is None when the adapter raised
    this error for a business state instead of a driver failure.

    Consumers branch on the contract type only; they must not infer rollback from
    the classification itself or retry a mutation automatically.

    commit_unknown marks the outcome this request cannot decide. Adapters set it only
    in these cases:

    * the COMMIT was still in flight when the transaction's own budget ran out, so no
      server determination was ever read;
    * the driver reported transport loss or backend shutdown during the COMMIT, or the
      COMMIT failed with anything the contract does not classify as a constraint
      conflict. A unique or foreign-key conflict is the only refusal that proves the
      transaction rolled back; serialization failures, deadlocks, lock timeouts,
      statement-unknown completion and unclassified failures all stay unknown;
    * the content-write adapter conservatively reuses the flag for business states
      whose earlier write is undetermined: an export no longer running, an owner
      already writing or unknown, a lost CAS token. There this request certainly did
      not commit; the unresolved attempt is the prior one.

    It stays False when the server answered inside that budget: any failure raised
    before the COMMIT is attempted, and a COMMIT refusal that proves abort. Caller
    cancellation is outside the flag as well; it escapes as CancelledError with no
    classification.
    """

    def __init__(self, *, failure: PortableSqlError | None, commit_unknown: bool) -> None:
        super().__init__(
            "workbench store commit uncertain"
            if commit_unknown
            else "workbench store transaction failed"
        )
        self.failure = failure
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
