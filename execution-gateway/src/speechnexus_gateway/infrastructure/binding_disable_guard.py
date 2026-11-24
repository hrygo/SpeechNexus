"""Serialize gateway submission intent creation with an exact binding disable.

The mutex is the coordination keyed lock's binding row, the same one the backend's disable
barrier holds: the two sides are separate deployments, so both derive it through
``speechnexus_portable_sql.keyed_lock`` (``binding_lock_key`` + the shared table name)
rather than restating a key literal. The row lock is this transaction's first lock and is
held until commit, exactly where the old advisory lock sat:

    coordination_keyed_locks(binding key) -> submit authority rows -> admission lease rows

That ordering is what lets ``check_binding`` below see the retirement row of a disable that
already committed: the writer held the same binding row while it wrote it.
"""

from typing import Any, Protocol

from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionRejectedError,
    SubmissionStoreError,
)
from speechnexus_portable_sql.keyed_lock import (
    COORDINATION_KEYED_LOCK_TABLE,
    acquire_in,
    binding_lock_key,
)


class BindingCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...
    def fetchall(self) -> list[dict[str, Any]]: ...


class BindingDisabledError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.POLICY_OR_RESOURCE_DISABLED


def lock_binding(cursor: BindingCursor, revision_id: str) -> tuple[str, str]:
    cursor.execute(
        "SELECT binding_id, revision FROM speech_binding_revisions WHERE revision_id=?",
        (revision_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SubmissionStoreError("binding revision authority is unavailable")
    binding_id, revision = rows[0]["binding_id"], rows[0]["revision"]
    # Same key the backend barrier keys on, in the caller's transaction: the lock row is
    # created on first use, so this is also the side that proves the table is reachable.
    acquire_in(cursor, key=binding_lock_key(binding_id), table=COORDINATION_KEYED_LOCK_TABLE)
    return binding_id, revision


def check_binding(cursor: BindingCursor, binding_id: str, revision: str, revision_id: str) -> None:
    cursor.execute(
        "SELECT mode FROM speech_publication_retirements "
        "WHERE kind='binding' AND object_id=? AND revision=? AND revision_id=?",
        (binding_id, revision, revision_id),
    )
    retirements = cursor.fetchall()
    if len(retirements) > 1:
        raise SubmissionStoreError("binding retirement authority is inconsistent")
    if retirements and retirements[0]["mode"] == "disable":
        raise BindingDisabledError("The binding revision is disabled")
