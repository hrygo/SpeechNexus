"""record_stop on ordinary row locks and DML; the adapter owns commit and database time."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.submission import (
    RecordStop,
    StopEvidenceConflictError,
    StopEvidenceKind,
    SubmissionIntent,
    VerifiedStopEvidence,
)
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    lock_submit_intent,
    to_unix_micros,
)


def record_stop_in_transaction(
    cursor: SubmitCursor, request: RecordStop, *, clock: Callable[[], datetime]
) -> SubmissionIntent:
    """Append evidence and close this execution, without touching Scheduler budgets."""
    evidence = request.evidence
    intent = lock_submit_intent(cursor, evidence.submit_intent_id)
    cursor.execute(
        "SELECT evidence_id, submit_intent_id, evidence_kind, evidence_digest, observed_at_us, "
        "execution_stopped, resources_released, reason FROM execution_stop_evidence "
        "WHERE evidence_id = %s OR submit_intent_id = %s",
        (evidence.evidence_id, evidence.submit_intent_id),
    )
    rows = cursor.fetchall()
    previous: VerifiedStopEvidence | None = None
    if rows:
        if len(rows) != 1 or rows[0]["submit_intent_id"] != evidence.submit_intent_id:
            raise StopEvidenceConflictError("evidence identity belongs to another execution")
        row = rows[0]
        previous = VerifiedStopEvidence(
            evidence_id=row["evidence_id"],
            submit_intent_id=row["submit_intent_id"],
            evidence_kind=StopEvidenceKind(row["evidence_kind"]),
            evidence_digest=row["evidence_digest"],
            observed_at_us=row["observed_at_us"],
            execution_stopped=row["execution_stopped"] == 1,
            resources_released=row["resources_released"] == 1,
            reason=row["reason"],
        )
    updated = request.apply(intent, previous_evidence=previous, at=clock())
    if previous is not None:
        return updated  # An exact replay does not rewrite time, version or immutable evidence.
    cursor.execute(
        "INSERT INTO execution_stop_evidence "
        "(evidence_id, submit_intent_id, evidence_kind, evidence_digest, observed_at_us, "
        "execution_stopped, resources_released, reason) VALUES (%s, %s, %s, %s, %s, 1, 1, %s)",
        (
            evidence.evidence_id,
            evidence.submit_intent_id,
            evidence.evidence_kind.value,
            evidence.evidence_digest,
            evidence.observed_at_us,
            evidence.reason,
        ),
    )
    cursor.execute(
        "UPDATE submit_intents SET execution_state = %s, cleanup_state = %s, error_code = %s, "
        "terminated_at_us = %s, updated_at_us = %s, version = version + 1 "
        "WHERE submit_intent_id = %s AND version = %s",
        (
            updated.execution_state.value,
            updated.cleanup_state.value,
            None if updated.error_code is None else updated.error_code.value,
            evidence.observed_at_us,
            to_unix_micros(updated.updated_at),
            evidence.submit_intent_id,
            intent.version,
        ),
    )
    if cursor.rowcount != 1:
        # The enclosing transaction rolls the evidence INSERT back too.
        raise StopEvidenceConflictError("execution changed while recording its stop")
    return updated
