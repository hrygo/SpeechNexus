"""Serialize gateway submission intent creation with an exact binding disable."""

from typing import Any, Protocol

from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionRejectedError,
    SubmissionStoreError,
)


class BindingCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...
    def fetchall(self) -> list[dict[str, Any]]: ...


class BindingDisabledError(ExecutionRejectedError):
    error_code = ExecutionErrorCode.POLICY_OR_RESOURCE_DISABLED


def lock_binding(cursor: BindingCursor, revision_id: str) -> tuple[str, str]:
    cursor.execute(
        "SELECT binding_id, revision FROM speech_binding_revisions WHERE revision_id=%s",
        (revision_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SubmissionStoreError("binding revision authority is unavailable")
    binding_id, revision = rows[0]["binding_id"], rows[0]["revision"]
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:catalog:' || current_schema() || ':binding:' || %s, 0))",
        (binding_id,),
    )
    return binding_id, revision


def check_binding(cursor: BindingCursor, binding_id: str, revision: str, revision_id: str) -> None:
    cursor.execute(
        "SELECT mode FROM speech_publication_retirements "
        "WHERE kind='binding' AND object_id=%s AND revision=%s AND revision_id=%s",
        (binding_id, revision, revision_id),
    )
    retirements = cursor.fetchall()
    if len(retirements) > 1:
        raise SubmissionStoreError("binding retirement authority is inconsistent")
    if retirements and retirements[0]["mode"] == "disable":
        raise BindingDisabledError("The binding revision is disabled")
