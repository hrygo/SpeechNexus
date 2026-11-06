"""Public Job submit, read, retry and the durable event stream.

Every response is projected from committed public facts. The event stream replays
the persisted lifecycle and progress rows in sequence: it never synthesizes a
progress event and never reports a terminal state twice.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from speechnexus_contracts import ContractValidationError, parse
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse
from starlette.types import Message, Send

from speechnexus_backend.speech.application.job_cancellation import JobCancellationService
from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    CancellationView,
    DisabledWorkspaceError,
    IdempotencyConflictError,
    JobCleanupPendingError,
    JobOutcomeUnknownError,
    JobRejectedError,
    JobRetryNotAllowedError,
    JobRetryUnavailableError,
    ModelBindingError,
    ModelNotPublishedError,
    ProfileNotPublishedError,
    PronunciationNotFoundError,
    PublicJobs,
    UnknownWorkspaceError,
    UnsupportedJobKindError,
    VoiceNotPublishedError,
)
from speechnexus_backend.speech.domain.public_jobs import (
    TERMINAL_STATES,
    JobArtifactFact,
    JobEventFact,
    PublicJob,
    PublicJobNotFoundError,
    PublicJobUnavailableError,
    cleanup_state,
    identifier,
)
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from speechnexus_backend.speech.interfaces.http.middleware import REQUEST_ABORT_KEY

PREFIX = "/v1/nexus/jobs"
_STATUS = {
    "pending": "queued",
    "running": "running",
    "cancelling": "cancelling",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "expired": "expired",
}
# Persistent event sequences start at zero; the public EventSequence starts at
# one. The wire id is therefore the persistent sequence plus one, and resuming
# translates it back before the strictly-greater read.
_EVENT_ID = re.compile(r"[1-9][0-9]{0,18}\Z")
# The wire id is the persisted sequence plus one, so the largest resumable id is
# one past the largest sequence the store accepts.
_EVENT_CURSOR_MAX = 9_223_372_036_854_775_807
_EVENT_BATCH = 100
_EVENT_POLL_SECONDS = 0.5
# A comment line keeps idle proxies and clients from timing the stream out. The
# interval is bounded by configuration; it never consumes an event id.
_EVENT_HEARTBEAT_SECONDS = 15.0
_EVENT_SEND_TIMEOUT_SECONDS = 10.0
# Closing the source is server-side cleanup, not a client write, so it carries
# its own bound: a source whose close hangs must not postpone the reset and the
# warning. It is deliberately not derived from the operator-tuned send budget,
# which may be a fraction of a second. The bound relies on the source honouring
# cancellation: asyncio.timeout cancels once, so a close that swallows
# CancelledError and keeps waiting is not interruptible from here.
_EVENT_CLOSE_TIMEOUT_SECONDS = 5.0
_KEEPALIVE = b": keepalive\n\n"

_LOGGER = logging.getLogger(__name__)


class _TooLargeError(ValueError):
    pass


class _StreamStalledError(Exception):
    """The client stopped draining the response body."""


def _pause_seconds(now: float, beat: float, heartbeat: float, poll: float) -> float:
    """Seconds until the earlier of the next poll and the next due heartbeat."""
    return max(0.0, min(beat + heartbeat, now + poll) - now)


class _EventStreamResponse(StreamingResponse):
    """Event stream with a bounded per-frame write and a slow-client cutoff.

    Frames are pulled one page at a time: the next page is only read after every
    frame of the current page was accepted by the server transport, so a
    connection holds at most one page of projected frames (_EVENT_BATCH) plus the
    frame in flight. When a write cannot complete within the configured timeout
    the client is not draining, so the source is closed within its own bound
    and, when the middleware captured one, the server's abort hook resets the
    socket instead of blocking the request task forever. The disconnect is
    logged with counts and timings only; the client resumes from its last
    committed id.
    """

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        send_timeout_seconds: float,
        close_timeout_seconds: float = _EVENT_CLOSE_TIMEOUT_SECONDS,
        abort_connection: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        if not 0 < close_timeout_seconds <= 60:
            raise ValueError("Public Job event close timeout must be bounded")
        super().__init__(content, **kwargs)
        self._send_timeout_seconds = send_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._abort_connection = abort_connection

    async def stream_response(self, send: Send) -> None:
        async def guarded(message: Message) -> None:
            # Every send is bounded, including the terminal empty body: with a
            # peer that stopped reading, the last frame blocks on the same write
            # buffer as a data frame.
            try:
                async with asyncio.timeout(self._send_timeout_seconds):
                    await send(message)
            except TimeoutError:
                raise _StreamStalledError from None

        try:
            await super().stream_response(guarded)
        except _StreamStalledError:
            await self._stop_stalled_stream()

    async def _stop_stalled_stream(self) -> None:
        """Close the source, reset the connection and report what actually ran."""
        close_timed_out = False
        try:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                deadline = asyncio.timeout(self._close_timeout_seconds)
                try:
                    async with deadline:
                        await close()
                finally:
                    # Expiry is the fact, not the exception type: a source that
                    # fails, or returns, while its close is being cancelled still
                    # ends a close that this deadline cut. The wait is cut, not
                    # the obligation; the reset below still runs.
                    close_timed_out = deadline.expired()
        except Exception:
            # A failing source must not skip the reset or the warning below.
            pass
        finally:
            reset = False
            if self._abort_connection is not None:
                try:
                    self._abort_connection()
                except Exception:
                    # The socket stays open and uvicorn falls back to its own
                    # close, so the record must not claim a reset.
                    pass
                else:
                    reset = True
            _LOGGER.warning(
                "job_event_stream_backpressure",
                extra={
                    "event": "job_event_stream_backpressure",
                    "reason": "slow_client",
                    "send_timeout_seconds": self._send_timeout_seconds,
                    "close_timed_out": close_timed_out,
                    "connection_reset": reset,
                },
            )


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


def _artifact(artifact: JobArtifactFact) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "media_type": artifact.media_type,
        "bytes": artifact.bytes,
        "sha256": artifact.sha256,
        "receipt_id": artifact.receipt_id,
        "expires_at": None if artifact.expires_at_us is None else _stamp(artifact.expires_at_us),
    }


def public_job(job: PublicJob, artifacts: Sequence[JobArtifactFact] = ()) -> dict[str, Any]:
    configuration = json.loads(job.submission.effective_configuration_json)
    if not isinstance(configuration, dict):
        raise ContractValidationError("Invalid effective configuration")
    body: dict[str, Any] = {
        "id": job.job_id,
        "object": "nexus.job",
        "kind": job.kind,
        "status": _STATUS[job.state],
        "cleanup_state": cleanup_state(job),
        "request_id": job.submission.request_id,
        "workspace_id": job.workspace_id,
        "client_reference": job.submission.client_reference,
        "profile": configuration.get("profile"),
        "execution_revision": str(job.submission.execution_revision),
        "progress": {
            "unit": (job.submission.progress_unit if job.progress is None else job.progress.unit),
            "completed": 0 if job.progress is None else job.progress.completed,
            "total": (
                job.submission.progress_total if job.progress is None else job.progress.total
            ),
        },
        "artifacts": [_artifact(artifact) for artifact in artifacts],
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


def public_cancellation(view: CancellationView) -> dict[str, Any]:
    job, receipt = view.job, view.receipt
    body: dict[str, Any] = {
        "job_id": job.job_id,
        "accepted": receipt.accepted,
        "status": _STATUS[job.state],
        "cleanup_state": cleanup_state(job),
        "cancellation_requested_at": _stamp(receipt.requested_at_us),
        "operations": [],
    }
    return dict(parse("JobCancellation", body))


def public_event(
    event: JobEventFact,
    job: PublicJob,
    artifacts: Sequence[JobArtifactFact] = (),
) -> dict[str, Any]:
    """One committed event row projected as its declared event variant."""
    data: dict[str, Any]
    if event.kind == "progress":
        progress = event.progress
        if progress is None:
            raise PublicJobUnavailableError
        kind, data = (
            "job.progress",
            {
                "status": "running",
                "progress": {
                    "unit": progress.unit,
                    "completed": progress.completed,
                    "total": progress.total,
                },
            },
        )
    elif event.state == "pending":
        kind, data = "job.queued", {"status": "queued"}
    elif event.state == "running":
        kind, data = (
            "job.started",
            {
                "status": "running",
                "execution_revision": str(job.submission.execution_revision),
            },
        )
    elif event.state == "cancelling":
        kind, data = "job.cancelling", {"status": "cancelling"}
    elif event.state == "succeeded":
        if job.result_json is None or not artifacts:
            # A succeeded event must carry the committed Artifact and result;
            # without them the persisted facts cannot prove success.
            raise PublicJobUnavailableError
        kind, data = (
            "job.succeeded",
            {
                "status": "succeeded",
                "artifacts": [_artifact(artifact) for artifact in artifacts],
                "result": json.loads(job.result_json),
                "cleanup_state": cleanup_state(job),
            },
        )
    elif event.state == "failed":
        if job.error_code is None:
            raise PublicJobUnavailableError
        kind, data = (
            "job.failed",
            {
                "status": "failed",
                "error": {
                    "code": job.error_code,
                    "message": "The Job failed.",
                    "retryable": False,
                    "param": None,
                },
                "cleanup_state": cleanup_state(job),
            },
        )
    elif event.state == "cancelled":
        kind, data = (
            "job.cancelled",
            {
                "status": "cancelled",
                "cleanup_state": cleanup_state(job),
            },
        )
    else:
        kind, data = (
            "job.expired",
            {
                "status": "expired",
                "cleanup_state": cleanup_state(job),
            },
        )
    body: dict[str, Any] = {
        "id": str(event.event_seq + 1),
        "type": kind,
        "job_id": job.job_id,
        "occurred_at": _stamp(event.occurred_at_us),
        "data": data,
    }
    return dict(parse("JobEvent", body))


def _frame(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, sort_keys=False, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event['id']}\ndata: {payload}\n\n".encode()


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


def _last_event_id(request: Request) -> int:
    """Translate Last-Event-ID into the last persistent sequence already seen."""
    values = request.headers.getlist("last-event-id")
    if not values:
        return -1
    if len(values) != 1:
        raise ValueError("Duplicate Last-Event-ID")
    raw = values[0].strip()
    if not _EVENT_ID.fullmatch(raw):
        raise ValueError("Invalid Last-Event-ID")
    value = int(raw)
    if value > _EVENT_CURSOR_MAX:
        # A syntactically valid but unreachable cursor would otherwise open an
        # empty stream that closes before any future event can arrive.
        raise ValueError("Last-Event-ID out of range")
    return value - 1


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
        wire = public_job(job, service.artifacts(job))
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
    except ModelBindingError as error:
        return job_error(error.code, request_id)
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
        wire = public_job(job, service.artifacts(job))
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


def _cancellation_response(view: CancellationView) -> Response:
    wire = public_cancellation(view)
    terminal = view.job.state in TERMINAL_STATES
    headers = {"Cache-Control": "no-store"}
    if not terminal:
        headers["Retry-After"] = "1"
    return Response(
        json.dumps(wire, sort_keys=False, separators=(",", ":"), ensure_ascii=False),
        status_code=200 if terminal else 202,
        media_type="application/json",
        headers=headers,
    )


def _cancel(
    service: PublicJobs, job_id: str, request_id: str, idempotency_key: str | None
) -> CancellationView | Response:
    try:
        identifier(job_id)
        return service.cancel(job_id, request_id=request_id, idempotency_key=idempotency_key)
    except JobCleanupPendingError:
        return job_error("execution_cleanup_pending", request_id)
    except IdempotencyConflictError:
        return job_error("idempotency_conflict", request_id)
    except JobOutcomeUnknownError:
        return job_error("job_outcome_unknown", request_id)
    except PublicJobNotFoundError, ValueError:
        return job_error("job_not_found", request_id)
    except PublicJobUnavailableError, ContractValidationError:
        return job_error("job_unavailable", request_id)


def _retry(
    service: PublicJobs, job_id: str, request_id: str, idempotency_key: str | None
) -> Response:
    """Create the replacement Job; the parent keeps its terminal facts."""
    try:
        job = service.retry(job_id, request_id=request_id, idempotency_key=idempotency_key)
        wire = public_job(job, service.artifacts(job))
        return Response(
            json.dumps(wire, sort_keys=False, separators=(",", ":"), ensure_ascii=False),
            status_code=202,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    except JobCleanupPendingError:
        return job_error("execution_cleanup_pending", request_id)
    except JobRetryNotAllowedError:
        return job_error("job_retry_not_allowed", request_id)
    except JobRetryUnavailableError:
        return job_error("job_retry_unavailable", request_id)
    except IdempotencyConflictError:
        return job_error("idempotency_conflict", request_id)
    except JobOutcomeUnknownError:
        return job_error("job_outcome_unknown", request_id)
    except UnknownWorkspaceError:
        return job_error("workspace_not_found", request_id)
    except DisabledWorkspaceError:
        return job_error("workspace_disabled", request_id)
    except ModelNotPublishedError:
        return job_error("model_not_published", request_id)
    except ModelBindingError as error:
        return job_error(error.code, request_id)
    except VoiceNotPublishedError:
        return job_error("voice_not_published", request_id)
    except ProfileNotPublishedError:
        return job_error("profile_not_published", request_id)
    except PronunciationNotFoundError:
        return job_error("pronunciation_not_found", request_id)
    except AssetNotReadyError:
        return job_error("asset_not_ready", request_id)
    except JobRejectedError:
        return job_error("job_unavailable", request_id)
    except PublicJobNotFoundError, ValueError:
        return job_error("job_not_found", request_id)
    except PublicJobUnavailableError, ContractValidationError:
        return job_error("job_unavailable", request_id)


def _open_stream(
    service: PublicJobs,
    job_id: str,
    request_id: str,
    after_seq: int,
    *,
    heartbeat_seconds: float,
    send_timeout_seconds: float,
    abort_connection: Callable[[], None] | None = None,
) -> Response:
    """Validate existence before the response so 404 is a real status, not an empty stream."""
    try:
        identifier(job_id)
        service.get(job_id)
    except PublicJobNotFoundError, ValueError:
        return job_error("job_not_found", request_id)
    except PublicJobUnavailableError, ContractValidationError:
        return job_error("job_unavailable", request_id)
    return _EventStreamResponse(
        _event_stream(service, job_id, after_seq, heartbeat_seconds=heartbeat_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Request-ID": request_id},
        send_timeout_seconds=send_timeout_seconds,
        abort_connection=abort_connection,
    )


async def _read_page(
    service: PublicJobs, job_id: str, after_seq: int
) -> tuple[PublicJob, tuple[tuple[int, bytes], ...]]:
    """Read the persisted page first, then the snapshot that projects it.

    Reading events before the snapshot keeps the projection at least as new as
    the rows it must explain, and projecting inside the caller's guard means a
    committed terminal row can never abort a response that already started.
    """
    page = await run_in_threadpool(service.events, job_id, after_seq=after_seq, limit=_EVENT_BATCH)
    job = await run_in_threadpool(service.get, job_id)
    artifacts = await run_in_threadpool(service.artifacts, job)
    return job, tuple(
        (event.event_seq, _frame(public_event(event, job, artifacts))) for event in page
    )


async def _event_stream(
    service: PublicJobs,
    job_id: str,
    after_seq: int,
    *,
    heartbeat_seconds: float | None = None,
    poll_seconds: float | None = None,
) -> AsyncIterator[bytes]:
    heartbeat = _EVENT_HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    poll = _EVENT_POLL_SECONDS if poll_seconds is None else poll_seconds
    loop = asyncio.get_running_loop()
    cursor, beat = after_seq, loop.time()
    while True:
        try:
            job, frames = await _read_page(service, job_id, cursor)
        except PublicJobNotFoundError, PublicJobUnavailableError, ValueError:
            # A committed row can outrun the snapshot that must explain it, and a
            # vanished or unreadable Job has no provable frame. Re-read once, then
            # end the stream instead of emitting a partial or invented event.
            await asyncio.sleep(poll)
            try:
                job, frames = await _read_page(service, job_id, cursor)
            except PublicJobNotFoundError, PublicJobUnavailableError, ValueError:
                return
        for event_seq, frame in frames:
            yield frame
            cursor = event_seq
        if job.state in TERMINAL_STATES and not frames:
            return
        now = loop.time()
        if now - beat >= heartbeat:
            beat = now
            yield _KEEPALIVE
            now = loop.time()
        # Sleep to the nearer deadline measured after the work already done, so a
        # slow read cannot push the beat a whole poll interval past its cadence.
        await asyncio.sleep(_pause_seconds(now, beat, heartbeat, poll))


def create_job_router(
    get_service: Callable[[], PublicJobs | None],
    *,
    get_confirmation: Callable[[], JobCancellationService | None] | None = None,
    max_bytes: int = 262144,
    receive_timeout_seconds: float = 5,
    event_heartbeat_seconds: float | None = None,
    event_send_timeout_seconds: float | None = None,
) -> APIRouter:
    if not 1 <= max_bytes <= 1048576:
        raise ValueError("Public Job body limit must be bounded")
    if not 0 < receive_timeout_seconds <= 30:
        raise ValueError("Public Job receive timeout must be bounded")
    if event_heartbeat_seconds is not None and not 0 < event_heartbeat_seconds <= 60:
        raise ValueError("Public Job event heartbeat must be bounded")
    if event_send_timeout_seconds is not None and not 0 < event_send_timeout_seconds <= 60:
        raise ValueError("Public Job event send timeout must be bounded")
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

    @router.post(PREFIX + "/{job_id}/cancellations", operation_id="cancelJob")
    async def cancel_job(request: Request, job_id: str) -> Response:
        service = get_service()
        request_id = str(request.state.request_id)
        if service is None:
            return job_error("job_unavailable", request_id)
        try:
            key = _idempotency_key(request)
        except ValueError:
            return job_error("invalid_job_request", request_id)
        outcome = await run_in_threadpool(_cancel, service, job_id, request_id, key)
        if isinstance(outcome, Response):
            return outcome
        confirmation = get_confirmation() if get_confirmation is not None else None
        if confirmation is not None and outcome.job.state == "cancelling":
            try:
                confirmed = await confirmation.confirm(outcome.job, outcome.receipt)
            except (
                IdempotencyConflictError,
                JobOutcomeUnknownError,
                PublicJobUnavailableError,
                ContractValidationError,
            ):
                confirmed = outcome.job
            if confirmed.job_id == outcome.job.job_id:
                outcome = CancellationView(confirmed, outcome.receipt, outcome.accepted_now)
        return _cancellation_response(outcome)

    @router.post(PREFIX + "/{job_id}/retries", operation_id="retryJob")
    async def retry_job(request: Request, job_id: str) -> Response:
        service = get_service()
        request_id = str(request.state.request_id)
        if service is None:
            return job_error("job_unavailable", request_id)
        try:
            key = _idempotency_key(request)
        except ValueError:
            return job_error("invalid_job_request", request_id)
        return await run_in_threadpool(_retry, service, job_id, request_id, key)

    @router.get(PREFIX + "/{job_id}/events", operation_id="streamJobEvents")
    async def stream_job_events(request: Request, job_id: str) -> Response:
        service = get_service()
        request_id = str(request.state.request_id)
        if service is None:
            return job_error("job_unavailable", request_id)
        try:
            after_seq = _last_event_id(request)
        except ValueError:
            return job_error("invalid_job_request", request_id)
        # Unset overrides follow the module defaults so an operator or a test can
        # tune the cadence without rebuilding the router.
        heartbeat = (
            _EVENT_HEARTBEAT_SECONDS if event_heartbeat_seconds is None else event_heartbeat_seconds
        )
        send_timeout = (
            _EVENT_SEND_TIMEOUT_SECONDS
            if event_send_timeout_seconds is None
            else event_send_timeout_seconds
        )
        open_stream = functools.partial(
            _open_stream,
            service,
            job_id,
            request_id,
            after_seq,
            heartbeat_seconds=heartbeat,
            send_timeout_seconds=send_timeout,
            abort_connection=request.scope.get(REQUEST_ABORT_KEY),
        )
        return await run_in_threadpool(open_stream)

    return router
