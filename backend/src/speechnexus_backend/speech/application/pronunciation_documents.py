"""Canonical source-validated intents with the declared publication default."""

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


def normalize_publish(value: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical intent with the contract's publication default applied.

    The source declares ``publish`` default ``true``. The canonical intent always
    carries an explicit boolean so a keyed replay and an omitted field name the
    same command instead of producing two different digests.
    """
    normalized = dict(value)
    normalized["publish"] = value.get("publish", True)
    return normalized


def draft_parts(value: Any) -> tuple[str | None, str | None, bool, bytes]:
    parse("PronunciationCreateRequest", value)
    publish = value.get("publish", True)
    if type(publish) is not bool:
        raise ValueError("Invalid pronunciation publication intent")
    return (
        value.get("id"),
        value.get("expected_revision"),
        publish,
        encode_document({"encoding": value["encoding"], "entries": value["entries"]}),
    )


def check_command(command: CreatePronunciationDraft) -> None:
    selected = draft_parts(decode_document(command.intent))
    if selected != (
        command.pronunciation_id,
        command.expected_revision,
        command.publish,
        command.definition,
    ):
        raise ValueError("Inconsistent pronunciation draft command")
