"""Strict public projection of the base's published voice catalogue.

Every value here is the speech base's own published value, reported verbatim: the workbench
does not renumber a revision, rewrite a status or expose a draft. The workbench's own naming
(`voice_id`, `next_cursor`) stays on the workbench side of the boundary.
"""

from __future__ import annotations

from typing import Any

from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.voices import (
    VoiceCataloguePage,
    VoiceRevision,
    VoiceSummary,
)
from speechnexus_backend.workbench.interfaces.version_views import _time


def voice_summary_payload(value: VoiceSummary) -> Any:
    if type(value) is not VoiceSummary:
        raise ValueError("invalid workbench voice summary")
    return parse(
        "VoiceSummary",
        {
            "voice_id": value.voice_id,
            "revision": value.revision,
            "display_name": value.display_name,
            "source_type": value.source_type,
            "status": value.status,
            "languages": list(value.languages),
            "audio_formats": list(value.audio_formats),
        },
    )


def voice_revision_payload(value: VoiceRevision) -> Any:
    if type(value) is not VoiceRevision:
        raise ValueError("invalid workbench voice revision")
    return parse(
        "VoiceRevision",
        {
            "voice_id": value.voice_id,
            "revision": value.revision,
            "display_name": value.display_name,
            "status": value.status,
            "languages": list(value.languages),
            "audio_formats": list(value.audio_formats),
            "model_binding": {
                "id": value.model_binding_id,
                "revision": value.model_binding_revision,
            },
            "published_at": _time(value.published_at),
            "retired_at": None if value.retired_at is None else _time(value.retired_at),
            "disabled_reason": value.disabled_reason,
        },
    )


def voice_catalogue_payload(page: VoiceCataloguePage) -> Any:
    if type(page) is not VoiceCataloguePage:
        raise ValueError("invalid workbench voice catalogue page")
    return {
        "object": "nexus.list",
        "data": [voice_summary_payload(item) for item in page.items],
        "has_more": page.has_more,
        "next_cursor": page.next_cursor,
    }
