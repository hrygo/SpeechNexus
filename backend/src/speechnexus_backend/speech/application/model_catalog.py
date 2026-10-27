"""Public model catalog built only from published bindings and declared facts.

Readiness is never inferred from the existence of a binding: an absent fact stays
``False``/``unavailable`` so a listed model cannot look healthier than measured.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from speechnexus_backend.speech.domain.publications import Publication

TASKS = (
    "asr_realtime",
    "asr_full_segment_review",
    "asr_file",
    "tts_realtime",
    "tts_file",
    "voice_design",
    "voice_clone",
    "voice_validation",
    "forced_alignment",
    "speaker_diarization",
    "vad",
)
RELEASE_TIERS = ("experimental", "best_effort", "production")
ADMISSION_STATES = ("available", "queueable", "capacity_full", "unavailable")


@dataclass(frozen=True, slots=True)
class ReadinessFacts:
    """Measured readiness. Defaults mean "not established", never "probably fine"."""

    engine_ready: bool = False
    quality_qualified: bool = False
    admission_state: str = "unavailable"

    def __post_init__(self) -> None:
        if type(self.engine_ready) is not bool or type(self.quality_qualified) is not bool:
            raise ValueError("Readiness flags must be explicit booleans")
        if self.admission_state not in ADMISSION_STATES:
            raise ValueError("Unknown admission state")


def model_document(
    publication: Publication,
    definition: Mapping[str, Any],
    *,
    owned_by: str,
    readiness: ReadinessFacts | None = None,
) -> dict[str, Any]:
    """One public model entry, or a failure when the binding disagrees with itself."""
    if publication.kind != "binding":
        raise ValueError("Only published bindings become models")
    if definition.get("binding_id") != publication.object_id:
        raise ValueError("Published revision names another binding")
    logical_model = definition.get("logical_model")
    if not isinstance(logical_model, str) or not 1 <= len(logical_model) <= 128:
        raise ValueError("Binding declares no logical model")
    declaration = definition.get("tasks")
    if not isinstance(declaration, list) or not declaration:
        raise ValueError("Binding declares no tasks")
    tasks = sorted({task for task in declaration if isinstance(task, str)})
    if not tasks or any(task not in TASKS for task in tasks):
        raise ValueError("Binding declares an unknown task")
    release_tier = definition.get("release_tier")
    if release_tier not in RELEASE_TIERS:
        raise ValueError("Binding declares no release tier")
    if not isinstance(owned_by, str) or not 1 <= len(owned_by) <= 128:
        raise ValueError("Model catalog requires an owner")

    measured = readiness or ReadinessFacts()
    return {
        "id": publication.object_id,
        "object": "model",
        "created": publication.published_at_us // 1_000_000,
        "owned_by": owned_by,
        "nexus": {
            "logical_model": logical_model,
            "binding": publication.reference,
            "tasks": tasks,
            "release_tier": release_tier,
            "readiness": {
                "configured": True,
                "engine_ready": measured.engine_ready,
                "quality_qualified": measured.quality_qualified,
                "admission_state": measured.admission_state,
            },
        },
    }


def model_list(models: list[dict[str, Any]]) -> dict[str, Any]:
    """The OpenAI-style envelope; ordering is the caller's published ordering."""
    return {"object": "list", "data": list(models)}
