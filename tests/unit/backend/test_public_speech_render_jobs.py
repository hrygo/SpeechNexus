"""Render dispatch keeps one bounded Job create, the upstream refusal code and an honest receipt."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.workbench.application.render_reconcile import RenderJobReadError
from speechnexus_backend.workbench.application.render_units import (
    RenderRejection,
    RenderRejectionCause,
    RenderSubmissionRejectedError,
    RenderSubmissionUnknownError,
)
from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderUnit,
    RevisionPin,
)
from speechnexus_backend.workbench.infrastructure.public_speech_render_jobs import (
    PublicSpeechRenderJobs,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def unit(**changes: Any) -> RenderUnit:
    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text="合成旁白单元",
    )
    return RenderUnit.create(
        unit_id="unit", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    )


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://speech.test", transport=httpx.MockTransport(handler))


async def test_submit_sends_the_fixed_intent_and_returns_the_job_identity() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("idempotency-key")
        return httpx.Response(202, json={"id": "job_1"})

    job_id = await PublicSpeechRenderJobs(client(handler)).submit(
        unit=unit(), key="key-12345678", reference="gen:abc"
    )

    assert job_id == "job_1"
    assert seen["key"] == "key-12345678"
    assert seen["body"] == {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "合成旁白单元"},
        "voice": {"id": "voice", "revision": "3"},
        "output": {"audio_format": "wav", "save_artifacts": True},
        "client_reference": "gen:abc",
    }


async def test_keyless_submit_omits_the_header_and_keeps_the_reference() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("idempotency-key")
        seen["reference"] = json.loads(request.content)["client_reference"]
        return httpx.Response(202, json={"id": "job_2"})

    assert (
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key=None, reference="gen:keyless"
        )
        == "job_2"
    )
    assert seen["key"] is None
    assert seen["reference"] == "gen:keyless"


@pytest.mark.parametrize(
    "status",
    [400, 401, 403, 409, 413, 415, 422, 429],
)
async def test_refusals_are_not_reported_as_unknown(status: int) -> None:
    """Every declined status is a refusal that created no Job, 429 included.

    A 429 used to fall through to the unknown outcome, which told the caller to
    replay an original key for a submission the server never admitted. A 409 used to
    become a conflict error that dropped the upstream code, folding three different
    next steps into one. Both are refusals of *this* request: they state no Job exists.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    with pytest.raises(RenderSubmissionRejectedError):
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key="key-12345678", reference="gen:abc"
        )


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503, json={}),
        lambda request: httpx.Response(202, json={"no": "id"}),
        lambda request: httpx.Response(202, json={"id": "bad id"}),
        lambda request: httpx.Response(202, content=b"{"),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
async def test_unknown_and_unreadable_receipts_never_claim_absence(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    with pytest.raises(RenderSubmissionUnknownError):
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key="key-12345678", reference="gen:abc"
        )


async def test_an_unrepresentable_unit_never_opens_a_connection() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json={"id": "job_1"})

    with pytest.raises(RenderSubmissionRejectedError):
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key=None, reference="gen:" + "x" * 300
        )
    assert calls == 0


def upstream_error(
    code: str | None,
    *,
    param: str | None = None,
    kind: str = "invalid_request_error",
    retryable: bool = False,
) -> dict[str, Any]:
    """A refusal body in the declared public shape; ``code`` is optional to omit it."""
    error: dict[str, Any] = {
        "type": kind,
        "message": "The voice revision is not published.",
        "param": param,
        "request_id": "req_upstream_1",
        "retryable": retryable,
    }
    if code is not None:
        error["code"] = code
    return {"error": error}


async def refusal(
    handler: Callable[[httpx.Request], httpx.Response],
) -> RenderSubmissionRejectedError:
    with pytest.raises(RenderSubmissionRejectedError) as caught:
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit(), key="key-12345678", reference="gen:abc"
        )
    return caught.value


async def test_a_coded_refusal_keeps_the_upstream_code_and_param() -> None:
    """The 422 names what was refused; keeping it is what names the operator's next step."""
    error = await refusal(
        lambda _: httpx.Response(
            422, json=upstream_error("voice_not_published", param="voice.revision")
        )
    )
    assert error.rejection is not None
    assert error.rejection.cause is RenderRejectionCause.UPSTREAM_CODE
    assert error.rejection.status_code == 422
    assert error.rejection.upstream_code == "voice_not_published"
    assert error.rejection.param == "voice.revision"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (422, "unsupported_parameter"),
        (422, "workspace_not_configured"),
        (422, "workspace_not_found"),
        (422, "voice_not_published"),
        (422, "asset_not_ready"),
        (413, "job_request_too_large"),
    ],
)
async def test_every_refusal_code_reaches_the_caller_unfolded(status: int, code: str) -> None:
    """Six different operator steps share one status class; none may absorb another."""
    error = await refusal(lambda _: httpx.Response(status, json=upstream_error(code)))
    assert error.rejection is not None
    assert (error.rejection.status_code, error.rejection.upstream_code) == (status, code)


@pytest.mark.parametrize(
    "code",
    [
        "idempotency_conflict",
        "execution_cleanup_pending",
        "job_retry_not_allowed",
        "job_retry_unavailable",
        "voice_draft_stale_revision",
    ],
)
async def test_a_409_keeps_the_upstream_code_that_names_its_step(code: str) -> None:
    """One status, five codes, three different next steps: the code is the cause (#755)."""
    error = await refusal(lambda _: httpx.Response(409, json=upstream_error(code)))
    assert error.rejection is not None
    assert error.rejection.cause is RenderRejectionCause.UPSTREAM_CODE
    assert (error.rejection.status_code, error.rejection.upstream_code) == (409, code)


@pytest.mark.parametrize("status", [401, 403, 415])
async def test_an_authentication_media_or_permission_refusal_keeps_its_own_status(
    status: int,
) -> None:
    """These are not 422s: the refusing status itself names a different operator step."""
    error = await refusal(
        lambda _: httpx.Response(
            status,
            json=upstream_error(
                None,
                kind="authentication_error" if status == 401 else "invalid_request_error",
            ),
        )
    )
    assert error.rejection is not None
    assert error.rejection.status_code == status


async def test_a_429_keeps_the_delay_it_states() -> None:
    """A capacity refusal names when retrying is worth it; dropping it leaves a guess."""
    error = await refusal(
        lambda _: httpx.Response(
            429,
            json=upstream_error("job_capacity_exhausted", kind="rate_limit_error", retryable=True),
            headers={"Retry-After": "7"},
        )
    )
    assert error.rejection is not None
    assert error.rejection.cause is RenderRejectionCause.UPSTREAM_CODE
    assert (error.rejection.status_code, error.rejection.upstream_code) == (
        429,
        "job_capacity_exhausted",
    )
    assert error.rejection.retry_after_seconds == 7


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7),
        ("0", 0),
        # HTTP-date and fractional delays are not the declared form; none is invented
        # from a clock reading, and the refusal still stands with no stated delay.
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("1.5", None),
        ("-1", None),
        ("", None),
    ],
)
async def test_only_a_declared_delta_seconds_delay_is_read(
    header: str, expected: int | None
) -> None:
    error = await refusal(
        lambda _: httpx.Response(
            429,
            json=upstream_error("job_capacity_exhausted", kind="rate_limit_error", retryable=True),
            headers={"Retry-After": header},
        )
    )
    assert error.rejection is not None
    assert error.rejection.retry_after_seconds == expected


async def test_a_delay_on_a_non_429_refusal_is_not_believed() -> None:
    """Only the capacity 429 declares a delay in this contract; a stray header is dropped."""
    error = await refusal(
        lambda _: httpx.Response(
            422, json=upstream_error("voice_not_published"), headers={"Retry-After": "9"}
        )
    )
    assert error.rejection is not None
    assert error.rejection.retry_after_seconds is None


async def test_a_refusal_without_a_readable_envelope_keeps_the_delay_it_read() -> None:
    """The header is observed even when the body is not, so the wait is not lost."""
    error = await refusal(
        lambda _: httpx.Response(
            429, content=b"<html>429 Too Many Requests</html>", headers={"Retry-After": "3"}
        )
    )
    assert error.rejection is not None
    assert error.rejection.cause is RenderRejectionCause.UPSTREAM_UNREADABLE
    assert (error.rejection.status_code, error.rejection.retry_after_seconds) == (429, 3)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # A refusal generated before the JSON layer, e.g. a proxy's own 413 page.
        (413, b"<html><h1>413 Request Entity Too Large</h1></html>"),
        # JSON that is not the declared envelope.
        (422, upstream_error("voice_not_published")["error"]),
        # The declared envelope except the code: a refusal that named nothing.
        (422, upstream_error(None)),
        # The declared envelope with a value that cannot be a stable code.
        (422, upstream_error("Voice Not Published")),
    ],
)
async def test_an_unreadable_refusal_states_no_cause(status: int, body: Any) -> None:
    """Unreadable is not "coded": the refusal stands, but no reason may be claimed for it."""
    error = await refusal(
        lambda _: (
            httpx.Response(status, content=body)
            if isinstance(body, bytes)
            else httpx.Response(status, json=body)
        )
    )
    assert error.rejection is not None
    assert error.rejection.cause is RenderRejectionCause.UPSTREAM_UNREADABLE
    assert error.rejection.status_code == status
    assert error.rejection.upstream_code is None
    assert error.rejection.param is None


def test_a_rejection_cannot_name_a_cause_it_did_not_observe() -> None:
    """The value type refuses both lies: a code never read, and a code without one."""
    with pytest.raises(ValueError):
        RenderRejection(RenderRejectionCause.UPSTREAM_CODE, status_code=422)
    with pytest.raises(ValueError):
        RenderRejection(
            RenderRejectionCause.UPSTREAM_UNREADABLE, status_code=422, upstream_code="refused"
        )
    with pytest.raises(ValueError):
        RenderRejection(RenderRejectionCause.LOCAL_CONTRACT, upstream_code="voice_not_published")
    with pytest.raises(ValueError):
        RenderRejection(RenderRejectionCause.LOCAL_CONTRACT, status_code=422)
    # A delay is an observed upstream fact: no response was read locally, and a
    # negative or non-integer value is not a delay this contract can state.
    with pytest.raises(ValueError):
        RenderRejection(RenderRejectionCause.LOCAL_CONTRACT, retry_after_seconds=3)
    with pytest.raises(ValueError):
        RenderRejection(
            RenderRejectionCause.UPSTREAM_CODE,
            status_code=429,
            upstream_code="job_capacity_exhausted",
            retry_after_seconds=-1,
        )
    with pytest.raises(ValueError):
        RenderRejection(
            RenderRejectionCause.UPSTREAM_CODE,
            status_code=429,
            upstream_code="job_capacity_exhausted",
            retry_after_seconds=True,
        )


def unit_with_text(text: str) -> RenderUnit:
    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text=text,
    )
    return RenderUnit.create(
        unit_id="unit", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    )


async def test_text_above_the_job_bound_is_rejected_locally_before_any_connection() -> None:
    """One Job synthesizes once, so an over-bound unit is refused at the contract check.

    The public Job contract bounds inline text to 4096 characters. Exceeding it is
    rejected locally with the same error the remote 400/422 produces, and no
    connection is opened, so the refusal never becomes an unknown outcome.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json={"id": "job_1"})

    with pytest.raises(RenderSubmissionRejectedError):
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit_with_text("中" * 4097), key="key-12345678", reference="gen:abc"
        )
    assert calls == 0


async def test_text_at_the_job_bound_is_still_submitted() -> None:
    """The declared bound stays usable: exactly 4096 characters still reaches the API."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"id": "job_1"})

    job_id = await PublicSpeechRenderJobs(client(handler)).submit(
        unit=unit_with_text("中" * 4096), key="key-12345678", reference="gen:abc"
    )

    assert job_id == "job_1"
    assert len(seen["body"]["input"]["text"]) == 4096


async def test_a_locally_refused_unit_names_only_the_field_it_proved() -> None:
    """The refused unit never reached the API; its contract error stays chained, not dropped.

    The adapter must not restate the inline bound: it replaces the text with a short
    valid one and re-validates, which proves the text is the *only* refused part and
    therefore names the text field. A refusal caused by another pinned field proves
    nothing about the text and must not name it.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(202, json={"id": "job_1"})

    with pytest.raises(RenderSubmissionRejectedError) as caught:
        await PublicSpeechRenderJobs(client(handler)).submit(
            unit=unit_with_text("中" * 4097), key="key-12345678", reference="gen:abc"
        )
    assert calls == 0
    assert caught.value.rejection is not None
    assert caught.value.rejection.cause is RenderRejectionCause.LOCAL_CONTRACT
    assert caught.value.rejection.status_code is None
    assert caught.value.rejection.upstream_code is None
    assert caught.value.rejection.param == "input.text"
    assert isinstance(caught.value.__cause__, ContractValidationError)


async def test_a_local_refusal_of_another_field_names_no_text_cause() -> None:
    """Only a proven text bound may be named; an over-long reference proves nothing."""
    with pytest.raises(RenderSubmissionRejectedError) as caught:
        await PublicSpeechRenderJobs(client(lambda _: httpx.Response(202, json={}))).submit(
            unit=unit_with_text("中"), key="key-12345678", reference="gen:" + "x" * 300
        )
    assert caught.value.rejection is not None
    assert caught.value.rejection.cause is RenderRejectionCause.LOCAL_CONTRACT
    assert caught.value.rejection.param is None


def job(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "job_1",
        "object": "nexus.job",
        "kind": "tts",
        "status": "succeeded",
        "cleanup_state": "not_required",
        "request_id": "req_1",
        "workspace_id": "workspace-demo",
        "execution_revision": "1",
        "progress": {"unit": "audio_ms", "completed": 1, "total": 1},
        "artifacts": [
            {
                "artifact_id": "artifact_1",
                "kind": "audio",
                "media_type": "audio/wav",
                "bytes": 1024,
                "sha256": "sha256:" + "a" * 64,
                "receipt_id": "receipt_1",
            }
        ],
        "result": {
            "kind": "tts",
            "audio_artifact_id": "artifact_1",
            "receipt_id": "receipt_1",
        },
        "operations": [],
        "effective_configuration": {
            "models": {"tts": {"id": "tts-quality", "revision": "1"}},
            "policy_revision": "1",
            "catalog_revision": "1",
        },
        "error": None,
        "created_at": "2026-10-24T00:00:00Z",
        "updated_at": "2026-10-24T00:00:01Z",
    }
    value.update(changes)
    return value


async def test_read_job_returns_the_tts_receipt_of_a_succeeded_job() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=job())

    async with client(handler) as http:
        outcome = await PublicSpeechRenderJobs(http).read_job(job_id="job_1")
    assert paths == ["/v1/nexus/jobs/job_1"]
    assert (outcome.state, outcome.artifact_id, outcome.receipt_id) == (
        "succeeded",
        "artifact_1",
        "receipt_1",
    )


async def test_read_job_reports_terminal_failures_and_pending_states() -> None:
    async with client(
        lambda _: httpx.Response(200, json=job(status="running", result=None))
    ) as http:
        assert (await PublicSpeechRenderJobs(http).read_job(job_id="job_1")).state == "pending"
    failed = job(
        status="failed",
        result=None,
        artifacts=[],
        cleanup_state="complete",
        error={"code": "engine_failure", "message": "synthetic", "retryable": False},
    )
    async with client(lambda _: httpx.Response(200, json=failed)) as http:
        assert (await PublicSpeechRenderJobs(http).read_job(job_id="job_1")).state == "failed"
    expired = job(status="expired", result=None, artifacts=[], cleanup_state="not_required")
    async with client(lambda _: httpx.Response(200, json=expired)) as http:
        assert (await PublicSpeechRenderJobs(http).read_job(job_id="job_1")).state == "expired"


async def test_read_job_refuses_an_unreadable_or_foreign_receipt() -> None:
    with pytest.raises(RenderJobReadError):
        async with client(lambda _: httpx.Response(200, json=job(kind="asr"))) as http:
            await PublicSpeechRenderJobs(http).read_job(job_id="job_1")
    with pytest.raises(RenderJobReadError):
        async with client(lambda _: httpx.Response(503, json={"error": {}})) as http:
            await PublicSpeechRenderJobs(http).read_job(job_id="job_1")

    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=job())

    with pytest.raises(RenderJobReadError):
        async with client(handler) as http:
            await PublicSpeechRenderJobs(http).read_job(job_id="job_1/../other")
    assert called is False
