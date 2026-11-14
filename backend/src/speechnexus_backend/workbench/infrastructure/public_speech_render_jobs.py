"""One bounded public-speech Job create per pinned render attempt.

The adapter never retries and never invents a second key: a transport failure or
an unreadable receipt is an unknown outcome the caller must recover by replaying
the original request, not by creating a new intent.

A refusal is the opposite kind of answer: the request was read and declined, so it
resolves nothing by retrying the same bytes. Those statuses carry a stable upstream
``code`` whose operator step differs per code, and the adapter keeps it instead of
collapsing every refusal into one anonymous error. A refusal whose envelope cannot
be read stays a refusal that states no cause -- it is never promoted to a "reason"
and never demoted to an unknown outcome.

The status class alone decides the disposition for the 4xx families whose meaning
is not in the code: a 401 is bad credentials, a 403 a permission refusal, a 415 an
unsupported media type, a 409 an intent/state conflict, and a 429 an admission
refusal that created no row. Folding those into "the request is invalid" is a wrong
attribution: the next step for each is different and none of them is "edit the
unit". A 429 additionally states a ``Retry-After`` delay, which is forwarded as an
observed fact so the operator is not left to guess how long "later" is.
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
    RenderSubmissionRejectedError,
    RenderSubmissionUnknownError,
)
from speechnexus_backend.workbench.domain.render_units import RenderUnit

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# ``Retry-After`` is a non-negative integer delta-seconds here, as the public Job
# contract declares it. The HTTP-date form is not accepted: this contract never
# sends it, and inventing a delay from a clock reading would state a fact nobody
# observed.
_RETRY_AFTER = re.compile(r"(?:0|[1-9][0-9]{0,8})")

# Declined requests: the service read the request and refused it deterministically,
# so no Job row exists and replaying the same intent cannot succeed unchanged. 413
# belongs here with the 4xx set: the body exceeded the deployment receive limit,
# whose code is ``job_request_too_large`` -- the caller shrinks the request, it does
# not retry. 429 is refused *before* admission writes any row: it is a refusal whose
# operator step is "wait, then retry", not an unknown outcome. 409 is a refusal whose
# meaning is carried by its code (three different next steps), handled the same way.
_REFUSAL_STATUSES = frozenset({400, 401, 403, 409, 413, 415, 422, 429})

_log = logging.getLogger(__name__)

# Field pointer of the one Job field that carries the unit text. The adapter names
# it only after proving the text is what the local contract check refused.
_INLINE_TEXT_POINTER = "input.text"
_SHORT_TEXT = "synthetic"


def _local_rejection(body: dict[str, object]) -> RenderRejection:
    """Name the locally proven offending field without restating any contract bound.

    The contract is the only authority for the bound: replacing the text with a short
    valid one and re-validating the same request proves that *only* the text is
    refused. A render text is a non-empty string (the domain enforces
    ``1 <= len(text) <= 200000``), so "only the text is refused" can only be the
    inline text bound, never a length-independent failure. If the probe still fails,
    another pinned field is at fault and no field is named.
    """
    try:
        parse("JobCreateRequest", {**body, "input": {"text": _SHORT_TEXT}})
    except ContractValidationError, TypeError, ValueError:
        return RenderRejection.local_contract()
    return RenderRejection.local_contract(param=_INLINE_TEXT_POINTER)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    """The delay a 429 named, when it named one this contract can read.

    Only a 429 may state it (the public Job error table sets ``Retry-After`` for
    ``job_capacity_exhausted`` alone), and an unparseable value is dropped rather
    than guessed: the refusal is still a refusal, it just states no delay.
    """
    if response.status_code != 429:
        return None
    value = response.headers.get("Retry-After")
    if value is None or not _RETRY_AFTER.fullmatch(value):
        return None
    return int(value)


def _refusal(response: httpx.Response) -> RenderRejection:
    """Read the upstream envelope; claim no cause when it cannot be read.

    The public Job contract declares this envelope, so a body that fails it is not
    a refusal *with* a reason: it stays a refusal with an unreadable reason, which
    the caller is told to handle as an unnamed refusal. The status and the
    ``Retry-After`` header are still read, because they were observed.
    """
    retry_after_seconds = _retry_after_seconds(response)
    try:
        envelope = parse("ErrorResponse", response.json())
    except ContractValidationError, TypeError, ValueError:
        _log.warning(
            "render dispatch refused without a readable envelope: status=%s",
            response.status_code,
        )
        return RenderRejection.unreadable(
            status_code=response.status_code, retry_after_seconds=retry_after_seconds
        )
    error = envelope["error"]
    _log.info(
        "render dispatch refused upstream: status=%s code=%s param=%s",
        response.status_code,
        error["code"],
        error["param"],
    )
    return RenderRejection.upstream(
        status_code=response.status_code,
        code=error["code"],
        param=error["param"],
        retry_after_seconds=retry_after_seconds,
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
            raise RenderSubmissionRejectedError(_local_rejection(body)) from error
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
