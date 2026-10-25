"""Strict public projection of a render unit; private bodies stay internal."""

from typing import Any, Literal

from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.render_units import RenderUnit, RevisionPin
from speechnexus_backend.workbench.interfaces.version_views import _time


def _pin(value: RevisionPin) -> dict[str, str]:
    return {"id": value.id, "revision": str(value.revision)}


def render_unit_payload(value: RenderUnit, *, version: Literal[1, 2] = 2) -> Any:
    if type(value) is not RenderUnit:
        raise ValueError("invalid workbench render unit snapshot")
    dependencies = value.dependencies
    generation = value.generation
    return parse(
        "RenderUnitV2" if version == 2 else "RenderUnit",
        {
            "object": "nexus.render_unit",
            "id": value.id,
            "chapter_id": dependencies.chapter_id,
            "ordinal": value.ordinal,
            "revision": str(value.revision),
            "text": dependencies.text,
            "voice": _pin(dependencies.voice),
            "model": dependencies.model,
            **({"model_binding": _pin(dependencies.model_binding)} if version == 2 else {}),
            "pronunciation": None
            if dependencies.pronunciation is None
            else _pin(dependencies.pronunciation),
            "profile": None if dependencies.profile is None else _pin(dependencies.profile),
            "status": value.status,
            "speech_job_id": None if generation is None else generation.job_id,
            # The retained copy belongs to the produced result, so it is reported only
            # while that result is still pinned and a copy of it exists.
            "saved_asset_id": None if value.source is None else value.source.saved_asset_id,
            "source_artifact_id": None if value.source is None else value.source.artifact_id,
            "source_receipt_id": None if value.source is None else value.source.receipt_id,
            "updated_at": _time(value.updated_at),
        },
    )
