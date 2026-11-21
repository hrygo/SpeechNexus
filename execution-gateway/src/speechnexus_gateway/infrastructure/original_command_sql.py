"""Read one bounded immutable original command; no dispatch or mutation permission."""

from __future__ import annotations

import json
from typing import Any, cast

from speechnexus_contracts.execution import parse

from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor, read_submit_intent

# The database must not ship an unbounded/corrupt private document to the process.
_READ_ORIGINAL = (
    "SELECT version, request_digest, command_digest, "
    "request_document IS NOT NULL AS document_present, "
    "OCTET_LENGTH(request_document) AS document_bytes, "
    "CASE WHEN OCTET_LENGTH(request_document) <= %s THEN request_document ELSE NULL END "
    "AS bounded_document, command_redacted_at_us, command_deletion_job_id, "
    "command_deletion_evidence_digest FROM submit_intents WHERE submit_intent_id = %s"
)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate command field")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError("nonfinite command number")


def read_original_request(
    cursor: SubmitCursor, intent: SubmissionIntent, *, max_document_bytes: int
) -> SubmitRequest:
    """Return the original effective request, never permission to dispatch it again."""
    if (
        type(max_document_bytes) is not int
        or not 1 <= max_document_bytes <= MAX_ORIGINAL_COMMAND_BYTES
    ):
        raise ValueError("original command reads require a bounded byte budget")
    current = read_submit_intent(cursor, intent.identity.submit_intent_id)
    if current is None:
        raise SubmissionStoreError("the original execution is unavailable")
    if (
        current.identity != intent.identity
        or current.request_digest != intent.request_digest
        or current.version != intent.version
    ):
        raise StaleSubmissionVersionError("the original execution changed before command read")
    cursor.execute(_READ_ORIGINAL, (max_document_bytes, intent.identity.submit_intent_id))
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise SubmissionStoreError("the original execution command is unavailable")
    row = rows[0]
    if row["version"] != intent.version:
        raise StaleSubmissionVersionError("the original execution changed during command read")
    try:
        if (
            not row["document_present"]
            or any(
                row[name] is not None
                for name in (
                    "command_redacted_at_us",
                    "command_deletion_job_id",
                    "command_deletion_evidence_digest",
                )
            )
            or type(row["document_bytes"]) is not int
            or not 0 < row["document_bytes"] <= max_document_bytes
            or not isinstance(row["bounded_document"], str)
        ):
            raise ValueError
        document = row["bounded_document"]
        if len(document.encode("utf-8")) != row["document_bytes"]:
            raise ValueError
        value = json.loads(document, object_pairs_hook=_unique, parse_constant=_nonfinite)
        if not isinstance(value, dict) or set(value) != {"execution_request", "output_stream_id"}:
            raise ValueError
        payload = parse("SubmitExecutionRequest", value["execution_request"])
        # Reuse the existing native serializer rather than maintain a second identity codec.
        request = SubmitRequest(
            identity=intent.identity,
            command=cast(JsonValue, payload["command"]),
            output_stream_id=value["output_stream_id"],
        )
        if (
            request.request_document != document
            or request.output_stream_id != intent.output_stream_id
            or request.request_digest != row["request_digest"]
            or request.request_digest != intent.request_digest
            or command_digest(request.command) != row["command_digest"]
            or row["command_digest"] != intent.identity.command_digest
        ):
            raise ValueError
    except KeyError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError:
        raise SubmissionStoreError("the original execution command cannot be verified") from None
    return request
