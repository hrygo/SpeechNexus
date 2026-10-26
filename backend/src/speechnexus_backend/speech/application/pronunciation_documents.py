"""Canonical source-validated draft intents, with no default injection."""

from __future__ import annotations

import json
from typing import Any

import rfc8785
from speechnexus_contracts import parse

from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    document_digest,
)


def encode_document(value: Any) -> bytes:
    try:
        raw = rfc8785.dumps(value)
        document_digest(raw)
        return raw
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid pronunciation document") from None


def decode_document(raw: bytes) -> dict[str, Any]:
    try:
        document_digest(raw)
        value = json.loads(raw)
        if not isinstance(value, dict) or encode_document(value) != raw:
            raise ValueError
        return value
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid stored pronunciation document") from None


def draft_parts(value: Any) -> tuple[str | None, str | None, bytes]:
    parse("PronunciationCreateRequest", value)
    # This private draft port selects only an explicit existing source branch.
    # Omission still means the source's published default; do not reinterpret it.
    if value.get("publish") is not False:
        raise ValueError("Explicit pronunciation draft intent required")
    return (
        value.get("id"),
        value.get("expected_revision"),
        encode_document({"encoding": value["encoding"], "entries": value["entries"]}),
    )


def check_command(command: CreatePronunciationDraft) -> None:
    selected = draft_parts(decode_document(command.intent))
    if selected != (command.pronunciation_id, command.expected_revision, command.definition):
        raise ValueError("Inconsistent pronunciation draft command")
