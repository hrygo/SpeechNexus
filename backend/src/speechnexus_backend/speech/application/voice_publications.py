"""Publish one validated voice draft revision as an immutable voice revision.

The application layer only defines the transaction boundary and the immutable
documents it writes. Everything that needs a lock, a row read or the database
clock happens inside the store's single transaction; this layer never opens
object storage and never re-measures a capability.

Publication accepts ``cloned`` draft revisions only. A ``design`` draft remains
``preview_only`` because its validation does not prove a reusable voice exists,
so publishing it would promise a voice the execution side cannot deliver.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.voice_catalogue_http import validate_response as validate_voice
from speechnexus_contracts.voice_drafts_http import (
    validate_response as validate_publication,
)

from speechnexus_backend.speech.domain.public_jobs import (
    canonical_digest,
    idempotency_digest,
    identifier,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import (
    VoiceCapabilityEvidence,
    VoiceMaterial,
    require_revision,
    revision_id_for,
    voice_id_for_draft,
)

_REVISION = re.compile(r"[1-9][0-9]{0,18}\Z")


class VoicePublicationNotFoundError(LookupError):
    """The draft does not exist."""


class VoicePublicationConflictError(RuntimeError):
    """The target head, the draft lifecycle or the idempotency intent conflicts."""


class VoicePublicationNotEligibleError(ValueError):
    """The revision cannot be published with the recorded facts."""


class VoicePublicationUnavailableError(RuntimeError):
    """Publication storage or the committed report is unavailable."""


class VoicePublicationContractError(RuntimeError):
    """A document this deployment would write violates its own contract."""


class VoicePublicationInputError(ValueError):
    """The request cannot name a publication."""


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    expected_draft_revision: str
    validation_job_id: str
    manual_confirmation: bool
    display_name: str | None

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> PublicationRequest:
        unknown = set(payload) - {
            "expected_draft_revision",
            "validation_job_id",
            "manual_confirmation",
            "display_name",
        }
        if unknown:
            raise VoicePublicationInputError("unknown_publication_fields")
        expected = payload.get("expected_draft_revision")
        if not isinstance(expected, str) or _REVISION.fullmatch(expected) is None:
            raise VoicePublicationInputError("invalid_expected_draft_revision")
        job_id = payload.get("validation_job_id")
        if not isinstance(job_id, str):
            raise VoicePublicationInputError("invalid_validation_job_id")
        try:
            identifier(job_id)
        except ValueError:
            raise VoicePublicationInputError("invalid_validation_job_id") from None
        if payload.get("manual_confirmation") is not True:
            raise VoicePublicationInputError("manual_confirmation_required")
        display_name = payload.get("display_name")
        if display_name is not None and (
            not isinstance(display_name, str) or not 1 <= len(display_name) <= 128
        ):
            raise VoicePublicationInputError("invalid_display_name")
        return cls(
            expected_draft_revision=expected,
            validation_job_id=job_id,
            manual_confirmation=True,
            display_name=display_name,
        )

    def as_intent(self) -> dict[str, Any]:
        intent: dict[str, Any] = {
            "expected_draft_revision": self.expected_draft_revision,
            "validation_job_id": self.validation_job_id,
            "manual_confirmation": True,
        }
        if self.display_name is not None:
            intent["display_name"] = self.display_name
        return intent


class VoicePublicationStore(Protocol):
    def publish(
        self,
        draft_id: str,
        request: PublicationRequest,
        *,
        request_digest: str,
        key_digest: str | None,
    ) -> tuple[dict[str, Any], bool]: ...


def timestamp(us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us)
    return moment.isoformat().replace("+00:00", "Z")


def build_voice_revision_document(
    *,
    voice_id: str,
    revision: str,
    display_name: str,
    source_type: str,
    draft_id: str,
    draft_revision: str,
    material: VoiceMaterial | None,
    evidence: VoiceCapabilityEvidence,
    binding_id: str,
    binding_revision: str,
    checked_at_us: int,
    published_at_us: int,
) -> dict[str, Any]:
    """The immutable public revision projection stored with the publication."""
    if source_type not in ("designed", "cloned"):
        raise VoicePublicationNotEligibleError("unsupported_source_type")
    if source_type == "cloned" and material is None:
        raise VoicePublicationNotEligibleError("clone_material_missing")
    if source_type == "designed" and material is not None:
        raise VoicePublicationNotEligibleError("design_material_not_allowed")
    document = {
        "resource_type": "voice_revision",
        "voice_id": voice_id,
        "revision": revision,
        "object": "nexus.voice_revision",
        "status": "published",
        "display_name": display_name,
        "source": {
            "type": source_type,
            "draft_id": draft_id,
            "draft_revision": draft_revision,
            "source_asset_digest": None if material is None else material.sha256,
        },
        "model_binding": {"id": binding_id, "revision": binding_revision},
        "capabilities": evidence.capabilities,
        "validation": {
            "validation_id": evidence.validation_id,
            "status": "passed",
            "checked_at": timestamp(checked_at_us),
        },
        "published_at": timestamp(published_at_us),
        "retired_at": None,
        "disabled_reason": None,
    }
    try:
        validate_voice("getVoiceRevision", 200, {"body": document})
    except ContractValidationError:
        raise VoicePublicationContractError from None
    return document


def build_publication_view(
    *,
    publication_id: str,
    draft_id: str,
    draft_revision: str,
    voice_id: str,
    voice_revision: str,
    validation_job_id: str,
    validation_id: str,
    published_at_us: int,
) -> dict[str, Any]:
    body = {
        "resource_type": "voice_publication",
        "id": publication_id,
        "object": "nexus.voice_publication",
        "draft_id": draft_id,
        "draft_revision": draft_revision,
        "voice_id": voice_id,
        "voice_revision": voice_revision,
        "validation_job_id": validation_job_id,
        "validation_id": validation_id,
        "manual_confirmation": True,
        "published_at": timestamp(published_at_us),
    }
    try:
        validate_publication("publishVoiceDraftRevision", 201, {"body": body})
    except ContractValidationError:
        raise VoicePublicationContractError from None
    return body


def require_source_eligibility(kind: str) -> None:
    """Only a cloned draft has a proven reusable path in this slice."""
    if kind == "design":
        raise VoicePublicationNotEligibleError("design_preview_only")
    if kind != "clone":
        raise VoicePublicationNotEligibleError("unsupported_draft_kind")


class VoicePublications:
    def __init__(self, store: VoicePublicationStore) -> None:
        self._store = store

    def publish(
        self,
        draft_id: str,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        draft_id = identifier(draft_id)
        request = PublicationRequest.parse(payload)
        digest = canonical_digest({"draft_id": draft_id, **request.as_intent()})
        key = None if idempotency_key is None else idempotency_digest(idempotency_key)
        body, created = self._store.publish(
            draft_id, request, request_digest=digest, key_digest=key
        )
        # The body is revalidated after the write committed; a violation is an
        # implementation defect, not a client conflict or a retryable outage.
        try:
            validate_publication(
                "publishVoiceDraftRevision", 201 if created else 200, {"body": body}
            )
        except ContractValidationError:
            raise VoicePublicationContractError from None
        return body, created


def build_evidence_checked_at_us(validation: Mapping[str, Any]) -> int:
    """The public validation timestamp in microseconds, never a local clock."""
    created = validation.get("created_at")
    if not isinstance(created, str):
        raise VoicePublicationNotEligibleError("validation_created_at_missing")
    try:
        moment = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        raise VoicePublicationNotEligibleError("validation_created_at_invalid") from None
    if moment.tzinfo is None:
        raise VoicePublicationNotEligibleError("validation_created_at_invalid")
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    us = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    if not 0 <= us <= 253_402_300_799_999_999:
        raise VoicePublicationNotEligibleError("validation_created_at_invalid")
    return us


def require_revision_text(value: object) -> str:
    return require_revision(value, "voice revision")


def stable_revision_id(voice_id: str, revision: str) -> str:
    return revision_id_for(voice_id, revision)


def stable_voice_id(draft_id: str) -> str:
    return voice_id_for_draft(draft_id)


__all__ = [
    "PublicationRequest",
    "VoicePublicationConflictError",
    "VoicePublicationContractError",
    "VoicePublicationInputError",
    "VoicePublicationNotEligibleError",
    "VoicePublicationNotFoundError",
    "VoicePublicationStore",
    "VoicePublicationUnavailableError",
    "VoicePublications",
    "build_evidence_checked_at_us",
    "build_publication_view",
    "build_voice_revision_document",
    "require_revision_text",
    "require_source_eligibility",
    "stable_revision_id",
    "stable_voice_id",
    "timestamp",
]
