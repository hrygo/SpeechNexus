"""One bounded public-speech Job create per pinned render attempt.

The adapter never retries and never invents a second key: a transport failure or
an unreadable receipt is an unknown outcome the caller must recover by replaying
the original request, not by creating a new intent.

A refusal is the opposite kind of answer: the request was read and declined, so it
resolves nothing by retrying. Those statuses carry a stable upstream ``code`` whose
operator step differs per code, and the adapter keeps it instead of collapsing
every refusal into one anonymous error. A refusal whose envelope cannot be read
stays a refusal that states no cause -- it is never promoted to a "reason" and
never demoted to an unknown outcome.
"""

from __future__ import annotations

import logging
import re

import httpx
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.parsers import parse

from speechnexus_backend.workbench.application.render_reconcile import (
    RenderJobOutcome,
    RenderJobReadError,
)
from speechnexus_backend.workbench.application.render_units import (
    RenderRejection,
    RenderSubmissionConflictError,
    RenderSubmissionRejectedError,
    RenderSubmissionUnknownError,
)
from speechnexus_backend.workbench.domain.render_units import RenderUnit

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")

# Declined requests: the service read the request and refused it deterministically,
# so no Job row exists and replaying the same intent cannot succeed. 413 belongs
# here with the 4xx set: the body exceeded the deployment receive limit, whose code
# is ``job_request_too_large`` -- the caller shrinks the request, it does not retry.
_REFUSAL_STATUSES = frozenset({400, 401, 403, 413, 415, 422})

_log = logging.getLogger(__name__)

# Field pointer of the one Job field that carries the unit text. The adapter names
# it only after proving the text is what the local contract check refused.
_INLINE_TEXT_POINTER = "input.text"
_SHORT_TEXT = "synthetic"


def _local_rejection(body: dict[str, object], *, text: str) -> RenderRejection:
    """Name the locally proven offending field without restating any contract bound.

    The contract is the only authority for the bound: replacing the text with a short
    valid one and re-validating the same request proves that *only* the text is
    refused. A unit's text is a non-empty string (the render domain enforces
    ``1 <= len(text) <= 200000``), so "only the text is refused" means the inline
    text bound, never a length-independent failure. If the probe still fails, some
    other pinned field is at fault and no field is named.
    """
    try:
        parse("JobCreateRequest", {**body, "input": {"text": _SHORT_TEXT}})
    except ContractValidationError, TypeError, ValueError:
        return RenderRejection.local_contract()
    if not text:
        return RenderRejection.local_contract()
    return RenderRejection.local_contract(param=_INLINE_TEXT_POINTER)


def _refusal(response: httpx.Response) -> RenderRejection:
    """Read the upstream envelope; claim no cause when it cannot be read.

    The public Job contract declares this envelope, so a body that fails it is not
    a refusal *with* a reason: it stays a refusal with an unreadable reason, which
    the caller is told to handle as an unnamed refusal.
    """
    try:
        envelope = parse("ErrorResponse", response.json())
    except ContractValidationError, TypeError, ValueError:
        _log.warning(
            "render dispatch refused without a readable envelope: status=%s",
            response.status_code,
        )
        return RenderRejection.unreadable(status_code=response.status_code)
    error = envelope["error"]
    _log.info(
        "render dispatch refused upstream: status=%s code=%s param=%s",
        response.status_code,
        error["code"],
        error["param"],
    )
    return RenderRejection.upstream(
        status_code=response.status_code, code=error["code"], param=error["param"]
    )


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
        except (ContractValidationError, TypeError, ValueError) as error:
            # No request is sent, so the cause is the local contract check itself;
            # the validation error stays chained instead of being discarded.
            raise RenderSubmissionRejectedError(
                _local_rejection(body, text=dependencies.text)
            ) from error
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
        if response.status_code in _REFUSAL_STATUSES:
            raise RenderSubmissionRejectedError(_refusal(response))
        raise RenderSubmissionUnknownError from None

    async def read_job(self, *, job_id: str) -> RenderJobOutcome:
        if not _IDENTIFIER.fullmatch(job_id):
            raise RenderJobReadError
        try:
            response = await self._client.get(f"/v1/nexus/jobs/{job_id}")
        except httpx.HTTPError, TimeoutError:
            raise RenderJobReadError from None
        if response.status_code != 200:
            raise RenderJobReadError
        try:
            job = parse("Job", response.json())
        except ContractValidationError, TypeError, ValueError:
            raise RenderJobReadError from None
        status = job["status"]
        if status == "succeeded":
            result = job["result"]
            if result is None or result["kind"] != "tts":
                raise RenderJobReadError
            return RenderJobOutcome("succeeded", result["audio_artifact_id"], result["receipt_id"])
        if status in {"failed", "expired", "cancelled"}:
            return RenderJobOutcome(status)
        return RenderJobOutcome("pending")
