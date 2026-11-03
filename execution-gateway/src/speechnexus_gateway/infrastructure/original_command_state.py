"""Verify the stored original command or its legal, content-free deletion tombstone."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from speechnexus_gateway.domain.command_redaction import CommandDeletionTombstone
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    CleanupState,
    InvalidExecutionRequestError,
    SubmissionIntent,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.original_command_sql import read_original_request
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor, from_unix_micros


def _redacted_at(value: object) -> datetime:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ValueError("invalid persisted instant")
    return from_unix_micros(value)


def read_original_document_state(
    cursor: SubmitCursor, intent: SubmissionIntent
) -> Literal["verified", "redacted"]:
    """Read a verified bounded document or legal tombstone, never dispatch authority."""
    try:
        cursor.execute(
            "SELECT l.lease_state, s.request_document IS NOT NULL AS document_present, "
            "s.command_redacted_at_us, s.command_deletion_job_id, "
            "s.command_deletion_evidence_digest "
            "FROM submit_intents AS s JOIN admission_leases AS l ON l.lease_id=s.lease_id "
            "WHERE s.submit_intent_id=%s",
            (intent.identity.submit_intent_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise ValueError("missing command authority")
        row = rows[0]
        tombstone = (
            row["command_redacted_at_us"],
            row["command_deletion_job_id"],
            row["command_deletion_evidence_digest"],
        )
        if row["document_present"] is True:
            if any(value is not None for value in tombstone):
                raise ValueError("command and deletion tombstone coexist")
            read_original_request(cursor, intent, max_document_bytes=MAX_ORIGINAL_COMMAND_BYTES)
            return "verified"
        if row["document_present"] is not False or any(value is None for value in tombstone):
            raise ValueError("command and deletion tombstone disagree")
        redacted_at = _redacted_at(tombstone[0])
        CommandDeletionTombstone(tombstone[1], tombstone[2], redacted_at)
        if (
            row["lease_state"] != "released"
            or not intent.execution_state.is_terminal
            or intent.cleanup_state not in {CleanupState.COMPLETE, CleanupState.NOT_REQUIRED}
            or not intent.recorded_at <= redacted_at <= intent.updated_at
        ):
            raise ValueError("invalid command deletion facts")
        # The retained digest is validated by SubmissionIntent; deleted bytes cannot
        # be reconstructed or independently verified against that digest.
        return "redacted"
    except InvalidExecutionRequestError, ValueError, TypeError, KeyError, OverflowError:
        raise SubmissionStoreError("original execution command state cannot be verified") from None
