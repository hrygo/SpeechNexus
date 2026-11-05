"""Public projection from committed publication facts, without private command metadata."""

from datetime import UTC, datetime, timedelta
from typing import Any

from speechnexus_contracts.scenario_profiles import parse

from speechnexus_backend.speech.application.profile_documents import decode_document
from speechnexus_backend.speech.domain.profile_publications import PublishedProfile
from speechnexus_backend.speech.interfaces.profile_drafts import draft_to_wire


def publication_to_wire(publication: PublishedProfile) -> dict[str, Any]:
    p = publication

    def timestamp(value: int | None) -> str | None:
        return (
            None
            if value is None
            else (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value))
            .isoformat()
            .replace("+00:00", "Z")
        )

    wire = {
        **draft_to_wire(p.draft),
        "validation": decode_document(p.validation),
        "publication": {
            "status": p.status,
            "published_at": timestamp(p.published_at_us),
            "retired_at": timestamp(p.retired_at_us),
            "disabled_reason": p.disabled_reason,
        },
    }
    parse("ScenarioProfileRevision", wire)
    return wire
