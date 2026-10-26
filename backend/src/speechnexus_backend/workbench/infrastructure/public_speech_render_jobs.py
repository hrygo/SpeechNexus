"""One bounded public-speech Job create per pinned render attempt.

The adapter never retries and never invents a second key: a transport failure or
an unreadable receipt is an unknown outcome the caller must recover by replaying
the original request, not by creating a new intent.
"""

from __future__ import annotations

import re

import httpx
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.parsers import parse

from speechnexus_backend.workbench.application.render_units import (
    RenderSubmissionConflictError,
    RenderSubmissionRejectedError,
    RenderSubmissionUnknownError,
)
from speechnexus_backend.workbench.domain.render_units import RenderUnit

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


class PublicSpeechRenderJobs:
    """Submit a TTS Job for a unit's fixed text, voice and model."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        base = client.base_url
        if (
            base.scheme not in {"http", "https"}
            or not base.host
            or base.userinfo
            or base.query
            or base.fragment
        ):
            raise ValueError("render dispatch requires one fixed public speech origin")
        self._client = client

    async def submit(self, *, unit: RenderUnit, key: str | None, reference: str) -> str:
        dependencies = unit.dependencies
        body: dict[str, object] = {
            "kind": "tts",
            "model": dependencies.model,
            "input": {"text": dependencies.text},
            "voice": {
                "id": dependencies.voice.id,
                "revision": str(dependencies.voice.revision),
            },
            "output": {"audio_format": "wav", "save_artifacts": True},
            "client_reference": reference,
        }
        if dependencies.profile is not None:
            body["profile"] = {
                "id": dependencies.profile.id,
                "revision": str(dependencies.profile.revision),
            }
        headers = {} if key is None else {"Idempotency-Key": key}
        try:
            command = parse("JobCreateRequest", body)
        except ContractValidationError, TypeError, ValueError:
            raise RenderSubmissionRejectedError from None
        try:
            response = await self._client.post("/v1/nexus/jobs", json=command, headers=headers)
        except httpx.HTTPError, TimeoutError:
            raise RenderSubmissionUnknownError from None
        if response.status_code == 202:
            try:
                payload = response.json()
            except ValueError:
                raise RenderSubmissionUnknownError from None
            job_id = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(job_id, str) or not _IDENTIFIER.fullmatch(job_id):
                raise RenderSubmissionUnknownError from None
            return job_id
        if response.status_code == 409:
            raise RenderSubmissionConflictError from None
        if response.status_code in {400, 401, 403, 415, 422}:
            raise RenderSubmissionRejectedError from None
        raise RenderSubmissionUnknownError from None
