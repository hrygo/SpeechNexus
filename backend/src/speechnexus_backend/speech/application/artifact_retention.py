"""Bounded retention sweep for committed Artifacts.

The sweep has exactly two decisions, and both are guarded by the recorded
retention deadline rather than by a caller's claim:

1. **expire** -- a committed ``ready`` row whose ``expires_at_us`` is at or
   before the database clock stops being served. The state change is the durable
   evidence that retention elapsed; the deadline and creation timestamps are
   left untouched, so the decision can be re-checked afterwards.
2. **remove** -- the content bytes of a row that is already ``expired`` are
   deleted from object storage, and only a confirmed removal marks the row
   ``deleted``. A removal that cannot be confirmed leaves the row ``expired``:
   the public answer stays ``artifact_expired`` and the next pass retries.
   A confirmed removal writes ``deleted_at_us`` in the same transition, so the
   durable row -- not just this process's log -- records that and when the bytes
   went away. When the transition is refused, the sweep reads the row back and
   reports what actually happened instead of calling it a removal.

No statement here selects, expires or deletes a row whose retention has not
elapsed, including a row some other path already marked ``expired``. Object I/O
never runs inside a database transaction, and the sweep never touches a row
whose Artifact row is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord

MAX_RETENTION_BATCH = 100
RetentionDecision = Literal["expired", "deleted", "blocked"]


class ArtifactRetentionRows(Protocol):
    def expired(self, limit: int) -> tuple[int, tuple[ArtifactRecord, ...]]: ...

    def mark_expired(self, artifact_id: str) -> ArtifactRecord | None: ...

    def pending_removal(self, limit: int) -> tuple[int, tuple[ArtifactRecord, ...]]: ...

    def mark_deleted(self, artifact_id: str) -> ArtifactRecord | None: ...

    def current(self, artifact_id: str) -> ArtifactRecord | None: ...


class ArtifactContentRemoval(Protocol):
    def delete_version(self, artifact: ArtifactRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """One reviewable decision: which Artifact, which deadline, which verdict."""

    artifact_id: str
    decision: RetentionDecision
    expires_at_us: int
    observed_at_us: int
    reason: str = ""
    deleted_at_us: int | None = None


class ArtifactRetention:
    """Expire and remove elapsed Artifact content in bounded, auditable passes."""

    def __init__(
        self,
        rows: ArtifactRetentionRows,
        content: ArtifactContentRemoval,
        *,
        batch_size: int = 10,
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_RETENTION_BATCH:
            raise ValueError("Artifact retention must be bounded")
        self._rows, self._content, self._batch = rows, content, batch_size

    @property
    def batch_size(self) -> int:
        return self._batch

    def sweep(self) -> tuple[RetentionOutcome, ...]:
        """Run one bounded expire pass plus one bounded removal pass."""
        return (*self._expire_pass(), *self._removal_pass())

    def _expire_pass(self) -> tuple[RetentionOutcome, ...]:
        now_us, rows = self._rows.expired(self._batch)
        outcomes: list[RetentionOutcome] = []
        for row in rows:
            if not row.retention_elapsed(now_us):
                # The repository already filters; re-check the decision itself so
                # a widened query can never expire content inside its window.
                outcomes.append(_blocked(row, now_us, "retention_not_elapsed"))
                continue
            marked = self._rows.mark_expired(row.artifact_id)
            if marked is None:
                # A concurrent sweep decided this row; it is not our decision.
                continue
            outcomes.append(RetentionOutcome(row.artifact_id, "expired", _deadline(row), now_us))
        return tuple(outcomes)

    def _removal_pass(self) -> tuple[RetentionOutcome, ...]:
        now_us, rows = self._rows.pending_removal(self._batch)
        outcomes: list[RetentionOutcome] = []
        for row in rows:
            # The deadline comes from the row and both the selection and the
            # `deleted` transition re-check it against the database clock, so a
            # row inside its retention window cannot lose content even if another
            # path marked it expired.
            if not row.retention_elapsed(now_us):
                outcomes.append(_blocked(row, now_us, "retention_not_elapsed"))
                continue
            try:
                self._content.delete_version(row)
            except ArtifactError:
                # Content removal is unconfirmed: keep the durable `expired` row
                # so the next pass retries instead of claiming a cleanup.
                outcomes.append(_blocked(row, now_us, "content_remove_unconfirmed"))
                continue
            recorded = self._rows.mark_deleted(row.artifact_id)
            if recorded is not None:
                outcomes.append(
                    RetentionOutcome(
                        row.artifact_id,
                        "deleted",
                        _deadline(row),
                        now_us,
                        deleted_at_us=recorded.deleted_at_us,
                    )
                )
                continue
            outcomes.append(self._explain_refusal(row, now_us))
        return tuple(outcomes)

    def _explain_refusal(self, row: ArtifactRecord, now_us: int) -> RetentionOutcome:
        """Name why the `deleted` transition did not apply.

        A refused transition is not evidence that content was already removed: the
        deadline may have been re-checked, or another pass may have moved the row
        first. The durable row decides which explanation is true.
        """
        current = self._rows.current(row.artifact_id)
        if current is None:
            return _blocked(row, now_us, "row_missing")
        if current.deleted_at_us is not None:
            # The ledger says the removal is recorded, so this pass did not need
            # to perform it; the state is still `deleted`.
            return RetentionOutcome(
                row.artifact_id,
                "deleted",
                _deadline(row),
                now_us,
                "recorded_by_another_pass",
                current.deleted_at_us,
            )
        if not current.retention_elapsed(now_us):
            return _blocked(row, now_us, "retention_not_elapsed")
        return _blocked(row, now_us, "row_state_changed")


def _blocked(row: ArtifactRecord, now_us: int, reason: str) -> RetentionOutcome:
    return RetentionOutcome(row.artifact_id, "blocked", _deadline(row), now_us, reason)


def _deadline(row: ArtifactRecord) -> int:
    return -1 if row.expires_at_us is None else row.expires_at_us


__all__ = [
    "MAX_RETENTION_BATCH",
    "ArtifactContentRemoval",
    "ArtifactRetention",
    "ArtifactRetentionRows",
    "RetentionDecision",
    "RetentionOutcome",
]
