"""Clear only the private command body and append its tombstone in one short transaction."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    CommandDeletionTombstone,
    CommandRedactionResult,
)
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    LeaseState,
    StaleSubmissionVersionError,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    from_unix_micros,
    lock_submit_intent,
    to_unix_micros,
)


def redact_execution_command_in_transaction(
    cursor: SubmitCursor, permit: AuthorizedCommandRedaction, *, clock: Callable[[], datetime]
) -> CommandRedactionResult:
    request = permit.request
    current = lock_submit_intent(cursor, request.submit_intent_id, lock_lease=True)
    cursor.execute(
        "SELECT l.lease_state, s.request_document IS NOT NULL AS document_present, "
        "s.command_redacted_at_us, s.command_deletion_job_id, s.command_deletion_evidence_digest "
        "FROM submit_intents AS s JOIN admission_leases AS l ON l.lease_id = s.lease_id "
        "WHERE s.submit_intent_id = %s",
        (request.submit_intent_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SubmissionStoreError("command deletion has no unique authority")
    row = rows[0]
    try:
        fields = (
            row["command_deletion_job_id"],
            row["command_deletion_evidence_digest"],
            row["command_redacted_at_us"],
        )
        if any(field is None for field in fields) and any(field is not None for field in fields):
            raise ValueError("partial command deletion tombstone")
        previous = (
            None
            if fields[0] is None
            else CommandDeletionTombstone(fields[0], fields[1], from_unix_micros(fields[2]))
        )
        lease_state = LeaseState(row["lease_state"])
    except (KeyError, TypeError, ValueError, InvalidExecutionRequestError) as error:
        raise SubmissionStoreError("invalid persisted command deletion facts") from error
    result = permit.apply(
        current,
        lease_state=lease_state,
        document_present=bool(row["document_present"]),
        previous=previous,
        at=clock(),
    )
    if result.replayed:
        return result
    redacted_at_us = to_unix_micros(result.tombstone.redacted_at)
    cursor.execute(
        "UPDATE submit_intents SET request_document = NULL, command_redacted_at_us = %s, "
        "command_deletion_job_id = %s, command_deletion_evidence_digest = %s, "
        "updated_at_us = %s, version = version + 1 "
        "WHERE submit_intent_id = %s AND version = %s AND request_document IS NOT NULL "
        "AND command_redacted_at_us IS NULL AND command_deletion_job_id IS NULL "
        "AND command_deletion_evidence_digest IS NULL",
        (
            redacted_at_us,
            request.deletion_job_id,
            request.deletion_evidence_digest,
            redacted_at_us,
            request.submit_intent_id,
            request.expected_version,
        ),
    )
    if cursor.rowcount != 1:
        raise StaleSubmissionVersionError("command deletion lost its original version")
    return result
