"""Source-validated entry: canonical intent, immutable revision, publication state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from speechnexus_contracts import ContractValidationError, parse

from speechnexus_backend.speech.application.pronunciation_documents import (
    decode_document,
    draft_parts,
    encode_document,
    normalize_publish,
)
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationDraft,
    PronunciationSummary,
    document_digest,
)


def summary_to_wire(summary: PronunciationSummary) -> dict[str, Any]:
    """One public family head and the publication state of that exact revision.

    ``head_revision`` is the family's latest immutable revision. ``status`` is
    ``published`` only when that exact revision has a publication ledger row, so
    a draft appended above a published revision lists as a draft and never
    borrows the older revision's publication.
    """
    return {
        "id": summary.pronunciation_id,
        "head_revision": summary.head_revision,
        "encoding": summary.encoding,
        "entry_count": summary.entry_count,
        "status": "published" if summary.published else "draft",
    }


def draft_to_wire(draft: PronunciationDraft) -> dict[str, Any]:
    timestamp = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=draft.created_at_us)
    wire = {
        **decode_document(draft.definition),
        "resource_type": "pronunciation_revision",
        "id": draft.pronunciation_id,
        "revision": draft.revision,
        "object": "nexus.pronunciation_revision",
        "status": "published" if draft.published else "draft",
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
        # The canonical intent always carries the declared publication default so
        # omission and an explicit value name one command, never two digests.
        intent = encode_document(normalize_publish(request))
        pronunciation_id, expected, publish, definition = draft_parts(decode_document(intent))
        # Automatic family IDs are allocated only after winning the persistent
        # command key, never included as freshly generated replay intent.
        draft_to_wire(PronunciationDraft(pronunciation_id or "pron-synthetic", "1", definition, 0))
        key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
        return CreatePronunciationDraft(
            pronunciation_id,
            expected,
            publish,
            document_digest(b"pronunciation-create\0" + key.encode("utf-8")),
            intent,
            definition,
        )
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        raise ContractValidationError("Invalid pronunciation draft intent") from None
