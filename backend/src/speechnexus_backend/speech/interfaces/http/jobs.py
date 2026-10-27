"""Public Job submit and read; one accepted intent becomes one durable row."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError, parse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    DisabledWorkspaceError,
    IdempotencyConflictError,
    JobOutcomeUnknownError,
    JobRejectedError,
    ModelNotPublishedError,
    ProfileNotPublishedError,
    PronunciationNotFoundError,
    PublicJobs,
    UnknownWorkspaceError,
    UnsupportedJobKindError,
    VoiceNotPublishedError,
)
from speechnexus_backend.speech.domain.public_jobs import (
    PublicJob,
    PublicJobNotFoundError,
    PublicJobUnavailableError,
    identifier,
)
from speechnexus_backend.speech.interfaces.http.job_errors import job_error

PREFIX = "/v1/nexus/jobs"
_STATUS = {
    "pending": "queued",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "expired": "expired",
}
_CLEANUP = {
    "pending": "not_required",
    "running": "not_required",
    "succeeded": "complete",
    "failed": "not_required",
    "cancelled": "complete",
    "expired": "not_required",
}


class _TooLargeError(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


def _stamp(value: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def public_job(job: PublicJob) -> dict[str, Any]:
    configuration = json.loads(job.submission.effective_configuration_json)
    if not isinstance(configuration, dict):
        raise ContractValidationError("Invalid effective configuration")
    body: dict[str, Any] = {
        "id": job.job_id,
        "object": "nexus.job",
        "kind": job.kind,
        "status": _STATUS[job.state],
        "cleanup_state": _CLEANUP[job.state],
        "request_id": job.submission.request_id,
        "workspace_id": job.workspace_id,
        "client_reference": job.submission.client_reference,
        "profile": configuration.get("profile"),
        "execution_revision": str(job.submission.execution_revision),
        "progress": {
            "unit": job.submission.progress_unit,
            "completed": 0,
            "total": job.submission.progress_total,
        },
        "artifacts": [],
        "result": None if job.result_json is None else json.loads(job.result_json),
        "operations": [],
        "effective_configuration": configuration,
        "created_at": _stamp(job.created_at_us),
        "updated_at": _stamp(job.updated_at_us),
    }
    if job.state == "failed":
        body["error"] = {
            "code": job.error_code,
            "message": "The Job failed.",
            "retryable": False,
            "param": None,
        }
    return dict(parse("Job", body))


async def _receive(request: Request, max_bytes: int, receive_timeout: float) -> bytes:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise ValueError("Duplicate Content-Length")
    if lengths:
        try:
            declared = int(lengths[0])
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if declared < 0 or declared > max_bytes:
            raise _TooLargeError
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("Job creation requires JSON")
    chunks: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(receive_timeout):
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise _TooLargeError
                chunks.append(chunk)
    except TimeoutError:
        raise ValueError("Job receive timeout") from None
    except ClientDisconnect:
        raise ValueError("Job client disconnected") from None
    return b"".join(chunks)


def _idempotency_key(request: Request) -> str | None:
    values = request.headers.getlist("idempotency-key")
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("Duplicate Idempotency-Key")
    key = values[0]
    if not 8 <= len(key) <= 128 or not key.isascii() or any(ord(c) < 32 for c in key):
        raise ValueError("Invalid Idempotency-Key")
    return key


async def _create(
    service: PublicJobs, request: Request, max_bytes: int, receive_timeout: float
) -> Response:
    request_id = str(request.state.request_id)
    try:
        key = _idempotency_key(request)
        raw = await _receive(request, max_bytes, receive_timeout)
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except _TooLargeError:
        return job_error("invalid_job_request", request_id)
    except ValueError, TypeError, RecursionError, UnicodeDecodeError:
        return job_error("invalid_job_request", request_id)
    try:
        command = parse("JobCreateRequest", payload)
    except ContractValidationError, TypeError, ValueError:
        return job_error("unsupported_parameter", request_id)
    try:
        job = await run_in_threadpool(
            service.submit, command, request_id=request_id, idempotency_key=key
        )
        wire = public_job(job)
        return Response(
            json.dumps(wire, sort_keys=False, separators=(",", ":"), ensure_ascii=False),
            status_code=202,
            media_type="application/json",
            headers={
                "Location": PREFIX + "/" + job.job_id,
                "Cache-Control": "no-store",
            },
        )
    except UnsupportedJobKindError:
        return job_error("job_kind_not_supported", request_id)
    except UnknownWorkspaceError:
        return job_error("workspace_not_found", request_id)
    except DisabledWorkspaceError:
        return job_error("workspace_disabled", request_id)
    except ModelNotPublishedError:
        return job_error("model_not_published", request_id)
    except VoiceNotPublishedError:
        return job_error("voice_not_published", request_id)
    except ProfileNotPublishedError:
        return job_error("profile_not_published", request_id)
    except PronunciationNotFoundError:
        return job_error("pronunciation_not_found", request_id)
    except AssetNotReadyError:
        return job_error("asset_not_ready", request_id)
    except IdempotencyConflictError:
        return job_error("idempotency_conflict", request_id)
    except JobOutcomeUnknownError:
        return job_error("job_outcome_unknown", request_id)
    except JobRejectedError, PublicJobUnavailableError:
        return job_error("job_unavailable", request_id)
    except ValueError, TypeError, ContractValidationError:
        return job_error("invalid_job_request", request_id)


def _read(service: PublicJobs, job_id: str, request_id: str) -> Response:
    try:
        identifier(job_id)
        job = service.get(job_id)
        wire = public_job(job)
        return Response(
            json.dumps(wire, sort_keys=False, separators=(",", ":"), ensure_ascii=False),
            status_code=200,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except PublicJobNotFoundError, ValueError:
        return job_error("job_not_found", request_id)
    except PublicJobUnavailableError, ContractValidationError:
        return job_error("job_unavailable", request_id)


def create_job_router(
    get_service: Callable[[], PublicJobs | None],
    *,
    max_bytes: int = 262144,
    receive_timeout_seconds: float = 5,
) -> APIRouter:
    if not 1 <= max_bytes <= 1048576:
        raise ValueError("Public Job body limit must be bounded")
    if not 0 < receive_timeout_seconds <= 30:
        raise ValueError("Public Job receive timeout must be bounded")
    router = APIRouter(tags=["jobs"])

    @router.post(PREFIX, operation_id="createJob")
    async def create_job(request: Request) -> Response:
        service = get_service()
        if service is None:
            return job_error("job_unavailable", str(request.state.request_id))
        return await _create(service, request, max_bytes, receive_timeout_seconds)

    @router.get(PREFIX + "/{job_id}", operation_id="getJob")
    async def get_job(request: Request, job_id: str) -> Response:
        service = get_service()
        request_id = str(request.state.request_id)
        if service is None:
            return job_error("job_unavailable", request_id)
        return await run_in_threadpool(_read, service, job_id, request_id)

    return router
