"""Source-validated private entry; public routes and publication remain separate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from speechnexus_contracts import ContractValidationError, parse

from speechnexus_backend.speech.application.pronunciation_documents import (
    decode_document,
    draft_parts,
    encode_document,
)
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationDraft,
    document_digest,
)


def draft_to_wire(draft: PronunciationDraft) -> dict[str, Any]:
    timestamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=draft.created_at_us)
    wire = {
        **decode_document(draft.definition),
        "resource_type": "pronunciation_revision",
        "id": draft.pronunciation_id,
        "revision": draft.revision,
        "object": "nexus.pronunciation_revision",
        "status": "draft",
        "created_at": timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    parse("PronunciationRevision", wire)
    return wire


def prepare_pronunciation_draft(
    request: Any, idempotency_key: str | None = None
) -> CreatePronunciationDraft:
    try:
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128
        ):
            raise ValueError
        intent = encode_document(request)
        pronunciation_id, expected, definition = draft_parts(decode_document(intent))
        # Automatic family IDs are allocated only after winning the persistent
        # command key, never included as freshly generated replay intent.
        draft_to_wire(PronunciationDraft(pronunciation_id or "pron-synthetic", "1", definition, 0))
        key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
        return CreatePronunciationDraft(
            pronunciation_id,
            expected,
            document_digest(b"pronunciation-create\0" + key.encode("utf-8")),
            intent,
            definition,
        )
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        raise ContractValidationError("Invalid pronunciation draft intent") from None
