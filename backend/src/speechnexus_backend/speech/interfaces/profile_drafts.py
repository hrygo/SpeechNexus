"""Source-validated process-local boundary, with no HTTP or publication surface."""

from __future__ import annotations

import uuid
from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse as parse_admin
from speechnexus_contracts.scenario_profiles import parse as parse_scenario

from speechnexus_backend.speech.application.profile_documents import (
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.domain.profile_drafts import (
    CreateProfileDraft,
    ProfileDraft,
    document_digest,
)


def draft_to_wire(draft: ProfileDraft) -> dict[str, Any]:
    wire = {
        **decode_document(draft.definition),
        "schema_version": "1.0.0",
        "profile_id": draft.profile_id,
        "revision": draft.revision,
        "publication": {
            "status": "draft",
            "published_at": None,
            "retired_at": None,
            "disabled_reason": None,
        },
    }
    parse_scenario("ScenarioProfileRevision", wire)
    return wire


def prepare_profile_draft(request: Any, idempotency_key: str | None = None) -> CreateProfileDraft:
    """Retain this private command for original-key recovery, including keyless calls."""
    try:
        parse_admin("ProfileRevisionCreateRequest", request)
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128
        ):
            raise ValueError
        intent = encode_document(request)
        definition = decode_document(intent)
        profile_id = definition.pop("profile_id")
        expected = definition.pop("expected_latest_revision")
        # Source schema conditionals cover the complete profile, not just the
        # reusable definition in the create request. Never add profile defaults.
        content = encode_document(definition)
        draft_to_wire(ProfileDraft(profile_id, "1", content, 0))
        _producer_rules(definition)
        return CreateProfileDraft(
            profile_id,
            expected,
            document_digest((idempotency_key or uuid.uuid4().hex).encode()),
            intent,
            content,
        )
    except ValueError, TypeError, KeyError, RecursionError, OverflowError:
        raise ContractValidationError("Invalid profile draft intent") from None


def _producer_rules(definition: dict[str, Any]) -> None:
    # Source descriptions require these comparisons across names and values.
    allowlist = set(definition["override_allowlist"])
    if set(definition["override_ranges"]) - allowlist:
        raise ValueError
    capabilities = definition["required_capabilities"]
    if len({item["capability"] for item in capabilities}) != len(capabilities):
        raise ValueError
    for bounds in [
        *definition["override_ranges"].values(),
        *(item.get("constraint", {}) for item in capabilities),
    ]:
        if "minimum" in bounds and "maximum" in bounds and bounds["minimum"] > bounds["maximum"]:
            raise ValueError
    asr = definition["parameters"].get("asr")
    if asr is not None and asr["max_segment_ms"] < asr["preview_interval_ms"]:
        raise ValueError
