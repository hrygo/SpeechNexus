"""Source-validated private draft boundary; no HTTP or publication surface."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse as parse_admin
from speechnexus_contracts.model_bindings import parse as parse_binding

from speechnexus_backend.speech.application.binding_documents import (
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.application.binding_publication import PublishedBinding
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft, CreateBindingDraft
from speechnexus_backend.speech.domain.configuration import document_digest


def _timestamp(value: int) -> str:
    seconds, micros = divmod(value, 1000000)
    created = datetime.fromtimestamp(seconds, UTC).replace(microsecond=micros)
    return created.isoformat(timespec="microseconds").replace("+00:00", "Z")


def revision_to_wire(
    draft: BindingDraft, published: PublishedBinding | None = None
) -> dict[str, Any]:
    """Project stored facts; a publication is never downgraded to ``draft``."""
    if published is None:
        publication: dict[str, Any] = {
            "status": "draft",
            "published_at": None,
            "retired_at": None,
            "capacity_manifest_id": None,
            "qualification_report_id": None,
            "disabled_reason": None,
        }
    else:
        publication = {
            "status": "published",
            "published_at": _timestamp(published.published_at_us),
            "retired_at": None,
            "capacity_manifest_id": published.capacity_manifest_id,
            "qualification_report_id": published.qualification_report_id,
            "disabled_reason": None,
        }
    wire = {
        **decode_document(draft.definition),
        "schema_version": "1.0.0",
        "binding_id": draft.binding_id,
        "revision": draft.revision,
        "revision_id": draft.revision_id,
        "created_at": _timestamp(draft.created_at_us),
        "publication": publication,
    }
    parse_binding("ModelBindingRevision", wire)
    return wire


def draft_to_wire(draft: BindingDraft) -> dict[str, Any]:
    """The POST 201 shape only; GET must use stored publication facts."""
    return revision_to_wire(draft, None)


def prepare_binding_draft(request: Any, idempotency_key: str | None = None) -> CreateBindingDraft:
    """Retain the complete private command for recovery; keyless calls get a new identity."""
    try:
        parse_admin("ModelBindingCreateRequest", request)
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128
        ):
            raise ValueError
        intent = encode_document(request)
        definition = decode_document(intent)
        binding_id = definition.pop("binding_id")
        content = encode_document(definition)
        # Full-source conditionals (e.g. voice_design engine) are not all present
        # in the create DTO. This synthetic identity is validation-only, never stored.
        draft_to_wire(BindingDraft(binding_id, "1", "binding-validation-only", content, 0))
        return CreateBindingDraft(
            binding_id,
            document_digest((idempotency_key or uuid.uuid4().hex).encode()),
            intent,
            content,
        )
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        raise ContractValidationError("Invalid binding draft intent") from None
