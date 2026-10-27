"""Project published revisions into the frozen public configuration document.

The projection only restates facts that already exist as publications: it does
not choose defaults, invent readiness, or fall back to draft revisions. Callers
supply the policy and catalog counters owned by the catalog authority.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from speechnexus_backend.speech.domain.publications import Publication

_MODEL_KEYS = ("asr", "tts", "voice")
_DEGRADATIONS = ("none", "resource_tightened", "quality_reduced")
_MAX_REVISION = 9_999_999_999_999_999_999


def revision_text(revision: int) -> str:
    """Contract revisions are monotonic decimal text with no leading zeros."""
    if type(revision) is not int or not 1 <= revision <= _MAX_REVISION:
        raise ValueError("Invalid contract revision")
    return str(revision)


def effective_configuration(
    *,
    models: Mapping[str, Publication],
    policy_revision: int,
    catalog_revision: int,
    profile: Publication | None = None,
    voice: Publication | None = None,
    pronunciation: Publication | None = None,
    parameters: Mapping[str, Any] | None = None,
    degradation: str = "none",
) -> dict[str, Any]:
    """Build the configuration a request actually executes with, or fail."""
    if not models:
        raise ValueError("Effective configuration requires at least one model")
    unknown = sorted(set(models) - set(_MODEL_KEYS))
    if unknown:
        raise ValueError("Unknown model slot: " + ",".join(unknown))
    for slot, publication in models.items():
        if publication.kind != "binding":
            raise ValueError("Model slot " + slot + " must reference a binding")
    if voice is not None and voice.kind != "voice":
        raise ValueError("Voice reference must be a publication of a voice")
    if profile is not None and profile.kind != "profile":
        raise ValueError("Profile reference must be a publication of a profile")
    if pronunciation is not None and pronunciation.kind != "pronunciation":
        raise ValueError("Pronunciation reference must be a publication of a pronunciation set")
    if degradation not in _DEGRADATIONS:
        raise ValueError("Unknown degradation state")

    document: dict[str, Any] = {
        "models": {slot: models[slot].reference for slot in _MODEL_KEYS if slot in models},
        "policy_revision": revision_text(policy_revision),
        "catalog_revision": revision_text(catalog_revision),
        "degradation": degradation,
    }
    if profile is not None:
        document["profile"] = profile.reference
    if voice is not None:
        document["voice"] = voice.reference
    if pronunciation is not None:
        document["pronunciation"] = pronunciation.reference
    if parameters is not None:
        document["parameters"] = copy.deepcopy(dict(parameters))
    return document
