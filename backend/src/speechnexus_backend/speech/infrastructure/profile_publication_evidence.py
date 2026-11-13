"""Deployment-owned reviewed publication evidence; read-only, no shipped authority.

The profile publication writer commits a publication only after a narrow
read-only authority port observed the exact revision. That port had no
production implementation, so every publish answered 503
``profile_result_unknown`` after the request had already entered the writer.

This module supplies the deployment-owned input the port consumes: one bounded
JSON document mounted read-only into the API process and named by
``SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE``. It records which revision
definition and which validation report a deployment actually reviewed. It
contains no defaults, never writes, and never invents eligibility: a revision
absent from the document is a stable ineligibility decision, and a document that
cannot be read or parsed stops API startup naming the variable instead of
degrading to the previous 503.

The evidence is a review record, not a measurement: this module cannot prove the
operator ran a validation, produced a real engine result or qualified capacity.
It only binds a publish request to the exact reviewed bytes so a caller-supplied
validation object cannot substitute for a review.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from speechnexus_backend.speech.application.profile_documents import (
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import document_digest, revision_number
from speechnexus_backend.speech.domain.profile_drafts import ProfileDraft
from speechnexus_backend.speech.domain.profile_lifecycle import (
    ProfileLifecycleCommand,
    ProfilePublicationIneligibleError,
    ProfilePublicationObservation,
)

EVIDENCE_FILE_VARIABLE = "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE"
EVIDENCE_FORMAT = "speechnexus.profile-publication-evidence/1"
MAX_EVIDENCE_BYTES = 1_048_576
MAX_REVIEWED_REVISIONS = 512
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ENTRY_KEYS = frozenset(
    {
        "profile_id",
        "revision",
        "definition_digest",
        "validation_digest",
        "reviewed_by",
        "reviewed_at",
    }
)


def _invalid(reason: str) -> ValueError:
    return ValueError(f"{EVIDENCE_FILE_VARIABLE} is invalid: {reason}")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _constant(_value: str) -> Any:
    raise ValueError("non-finite number")


@dataclass(frozen=True, slots=True)
class ReviewedPublication:
    """One revision definition plus the validation report a deployment reviewed."""

    profile_id: str
    revision: str
    definition_digest: str
    validation_digest: str
    reviewed_by: str
    reviewed_at: str

    def authority_document(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "revision": self.revision,
            "definition_digest": self.definition_digest,
            "validation_digest": self.validation_digest,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
        }

    @property
    def authority_digest(self) -> str:
        """Digest of the reviewed record stored with the committed publication."""
        return document_digest(encode_document(self.authority_document()))


@dataclass(frozen=True, slots=True)
class ReviewedPublications:
    entries: Mapping[tuple[str, str], ReviewedPublication]

    def get(self, profile_id: str, revision: str) -> ReviewedPublication | None:
        return self.entries.get((profile_id, revision))


def _reviewed_publication(value: Any) -> ReviewedPublication:
    if not isinstance(value, dict) or set(value) != _ENTRY_KEYS:
        raise _invalid("each entry needs exactly " + ", ".join(sorted(_ENTRY_KEYS)))
    try:
        identifier(value["profile_id"])
        revision_number(value["revision"])
    except ValueError:
        raise _invalid("each entry needs a valid profile_id and revision") from None
    for key in ("definition_digest", "validation_digest"):
        if not isinstance(value[key], str) or _DIGEST.fullmatch(value[key]) is None:
            raise _invalid(f"{key} must be a sha256 digest")
    reviewed_by = value["reviewed_by"]
    if (
        not isinstance(reviewed_by, str)
        or not 1 <= len(reviewed_by) <= 256
        or any(ord(character) < 32 for character in reviewed_by)
    ):
        raise _invalid("reviewed_by must be 1..256 printable characters")
    reviewed_at = value["reviewed_at"]
    if not isinstance(reviewed_at, str):
        raise _invalid("reviewed_at must be an ISO 8601 timestamp")
    try:
        parsed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError, AttributeError:
        raise _invalid("reviewed_at must be an ISO 8601 timestamp") from None
    if parsed_at.tzinfo is None:
        raise _invalid("reviewed_at must carry a timezone offset")
    return ReviewedPublication(
        value["profile_id"],
        value["revision"],
        value["definition_digest"],
        value["validation_digest"],
        reviewed_by,
        reviewed_at,
    )


def parse_reviewed_publications(raw: bytes) -> ReviewedPublications:
    """Parse one bounded evidence document; every rejection names the variable."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_EVIDENCE_BYTES:
        raise _invalid(f"the document must be 1..{MAX_EVIDENCE_BYTES} bytes")
    try:
        document = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except ValueError, RecursionError:
        raise _invalid("the document is not plain bounded JSON") from None
    if not isinstance(document, dict) or set(document) != {"format", "entries"}:
        raise _invalid("the document needs exactly format and entries")
    if document["format"] != EVIDENCE_FORMAT:
        raise _invalid(f"format must be {EVIDENCE_FORMAT}")
    entries = document["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_REVIEWED_REVISIONS:
        raise _invalid(f"entries must be a list of at most {MAX_REVIEWED_REVISIONS} revisions")
    reviewed: dict[tuple[str, str], ReviewedPublication] = {}
    for item in entries:
        entry = _reviewed_publication(item)
        key = (entry.profile_id, entry.revision)
        if key in reviewed:
            raise _invalid("a profile revision appears twice")
        reviewed[key] = entry
    return ReviewedPublications(reviewed)


def load_reviewed_publications(path: Path) -> ReviewedPublications:
    """Read the deployment's evidence document, naming the variable on any failure."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise ValueError(
            f"{EVIDENCE_FILE_VARIABLE} names {path}, which cannot be read: the profile writer "
            "requires the reviewed publication evidence document"
        ) from None
    return parse_reviewed_publications(raw)


class DeploymentFilePublicationVerifier:
    """Authority from the deployment's reviewed document; it publishes nothing."""

    def __init__(self, reviewed: ReviewedPublications) -> None:
        self._reviewed = reviewed

    def verify(
        self, command: ProfileLifecycleCommand, target: ProfileDraft
    ) -> ProfilePublicationObservation:
        definition_digest = document_digest(target.definition)
        entry = self._reviewed.get(command.profile_id, command.revision)
        if entry is None:
            raise ProfilePublicationIneligibleError("No reviewed publication evidence")
        if entry.definition_digest != definition_digest:
            raise ProfilePublicationIneligibleError("Reviewed evidence covers another definition")
        validation = decode_document(command.intent)["body"]["validation"]
        if document_digest(encode_document(validation)) != entry.validation_digest:
            raise ProfilePublicationIneligibleError("Reviewed evidence covers another validation")
        return ProfilePublicationObservation(
            command.profile_id,
            command.revision,
            definition_digest,
            command.request_digest,
            entry.authority_digest,
        )
