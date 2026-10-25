"""Generate fixed-source *candidates*, before durable MinutesJob publication.

The future workbench task owner must persist intent/attempt/cancellation and
publish only under its final source/deletion transaction barrier. A returned
in-memory candidate does not claim durable publication or deletion revocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol

from speechnexus_backend.workbench.application.llm import (
    DisabledTextLlm,
    LlmCompletion,
    LlmError,
    LlmRequest,
    LlmUsage,
    TextLlm,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    Transcript,
    TranscriptRevision,
    validate_revision_number,
)


class MinutesSource(Protocol):
    async def get_transcript(self, transcript_id: str) -> Transcript:
        """Read a currently visible head, releasing its transaction before returning."""
        ...

    async def get_revision(self, transcript_id: str, revision: int) -> TranscriptRevision:
        """Read one immutable visible source, releasing its transaction before returning."""
        ...


@dataclass(frozen=True, slots=True)
class MinutesCandidate:
    """Private plain text candidate; not a wire MinutesJob or published artifact."""

    transcript_id: str
    source_revision: int
    source_revision_id: str
    source_sha256: str
    model: str
    template_revision: int
    template_sha256: str
    max_output_tokens: int
    temperature: int
    text: str = field(repr=False)
    usage: LlmUsage | None


class MinutesGeneration:
    def __init__(self, source: MinutesSource, llm: TextLlm | None = None) -> None:
        self._source = source
        self._llm: TextLlm = llm if llm is not None else DisabledTextLlm()

    async def _read(self, transcript_id: str, revision: int) -> TranscriptRevision:
        try:
            current = await self._source.get_transcript(transcript_id)
            snapshot = await self._source.get_revision(transcript_id, revision)
        except Exception:
            raise LlmError("source_unavailable") from None
        if (
            current.id != transcript_id
            or current.head != snapshot
            or snapshot.transcript_id != transcript_id
            or snapshot.number != revision
        ):
            raise LlmError("source_changed")
        if snapshot.status != "sealed":
            raise LlmError("source_not_sealed")
        return snapshot

    async def generate(
        self,
        *,
        transcript_id: str,
        source_revision: int,
        template_revision: int,
        llm_model: str | None = None,
    ) -> MinutesCandidate:
        profile = self._llm.profile
        if profile is None:
            raise LlmError("disabled")
        try:
            validate_revision_number(source_revision)
            validate_revision_number(template_revision)
        except InvalidVersionChangeError:
            raise LlmError("invalid_input") from None
        if template_revision != profile.template_revision or (
            llm_model is not None and llm_model != profile.model
        ):
            raise LlmError("profile_mismatch")
        snapshot = await self._read(transcript_id, source_revision)
        request = LlmRequest(profile, snapshot.text)
        try:
            result = await self._llm.generate(request)
        except LlmError:
            raise
        except Exception:
            raise LlmError("unavailable") from None
        if not isinstance(result, LlmCompletion) or result.profile != profile:
            raise LlmError("profile_mismatch")
        # Cancellation propagates at every await; never schedule background work.
        visible = await self._read(transcript_id, source_revision)
        if visible != snapshot:
            raise LlmError("source_changed")
        return MinutesCandidate(
            transcript_id=transcript_id,
            source_revision=source_revision,
            source_revision_id=snapshot.id,
            source_sha256="sha256:" + sha256(snapshot.text.encode("utf-8")).hexdigest(),
            model=profile.model,
            template_revision=profile.template_revision,
            template_sha256="sha256:" + sha256(profile.system_prompt.encode("utf-8")).hexdigest(),
            max_output_tokens=profile.max_output_tokens,
            temperature=0,
            text=result.text,
            usage=result.usage,
        )
