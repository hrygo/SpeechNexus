"""Bounded canonical documents shared by the boundary and storage codec."""

from __future__ import annotations

import json
from typing import Any

import rfc8785

from speechnexus_backend.speech.domain.profile_drafts import (
    CreateProfileDraft,
    document_digest,
)


def encode_document(value: Any) -> bytes:
    try:
        raw = rfc8785.dumps(value)
        document_digest(raw)
        return raw
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid profile document") from None


def decode_document(raw: bytes) -> dict[str, Any]:
    try:
        document_digest(raw)
        value = json.loads(raw)
        if not isinstance(value, dict) or encode_document(value) != raw:
            raise ValueError
        return value
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid stored profile document") from None


def check_command(command: CreateProfileDraft) -> None:
    intent = decode_document(command.intent)
    if (
        intent.pop("profile_id") != command.profile_id
        or intent.pop("expected_latest_revision") != command.expected_latest_revision
        or encode_document(intent) != command.definition
    ):
        raise ValueError("Inconsistent profile command")
