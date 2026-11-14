"""The Job create body byte ceiling is declared and answers the declared 413.

#703: ``POST /v1/nexus/jobs`` refused a body above the deployment receive bound
with 400 ``invalid_job_request`` -- the same code an unparsable document gets --
and the operation declared no 413 at all, although the contract already shipped
the shared ``PayloadTooLarge`` component for the other upload surfaces. The bound
is deployment configuration (``job_receive_max_bytes``, default 256 KiB), so the
contract pins neither a literal byte count nor a fixed promise: it declares that
the bound exists, that deployment configuration determines it, and that an
over-bound body answers 413 instead of the 400/422 refusals that mean "your
document is wrong".

The cases below pin the two independent ceilings to their own answers:

* over the receive bound -> 413 ``job_request_too_large`` (this file);
* over the declared per-field ``text`` ceiling -> 422 ``unsupported_parameter``
  (kept here as the sibling assertion so widening one ceiling cannot silently
  move the other);
* a malformed ``Content-Length`` -> 400 ``invalid_job_request``: a negative
  length is a broken request, not an oversized one.

Two coverage points exist because the answer is easy to get subtly wrong:

* the bound is inclusive: a body of exactly ``job_receive_max_bytes`` is read,
  and only ``max_bytes + 1`` is refused. An implementation that compares ``>=``
  refuses a body the deployment still accepts, and no other case here notices;
* a body sent without ``Content-Length`` (chunked transfer) is bounded while it
  streams. The declared-length check alone would be dispatched by simply
  omitting the header, so the streaming accumulator is the only guard there.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import JobAdmissionRejectedError
from tests.contract.test_speech_http_statuses import assert_error_contract, response_contract
from tests.support.contracts import validator
from tests.unit.backend.test_health import _settings

PREFIX = "/v1/nexus/jobs"
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
# ``TtsJobInput.text.maxLength`` in contracts/speech-openapi.yaml: the declared
# per-synthesis ceiling (#690), not the transport bound this module is about.
TEXT_CEILING = 4_096
JOB_REQUEST_TOO_LARGE = "job_request_too_large"


class _RecordingJobs:
    """Records an admitted command; refuses at the declared admission boundary."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, command: Any, *, request_id: str, idempotency_key: str | None) -> Any:
        self.submitted.append(cast(dict[str, Any], command))
        raise JobAdmissionRejectedError("job_unavailable")

    def artifacts(self, job: Any) -> tuple[()]:
        return ()


def _create_job() -> dict[str, Any]:
    for item in cast(dict[str, Any], DOCUMENT["paths"]).values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == "createJob":
                return specification
    raise AssertionError("the speech contract declares no createJob operation")


def _body(*, text: str = "synthetic public job", **extra: Any) -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": text},
        "voice": {"id": "voice-vivian", "revision": "4"},
        "client_reference": "synthetic-body-bound",
        **extra,
    }


def _wire_bytes(body: dict[str, Any]) -> int:
    """Bytes ``httpx`` puts on the wire for ``json=body``.

    ``httpx`` serializes with ``ensure_ascii=False`` and the default separators, so
    a compact encoding here would understate the real body by the space after
    every separator and make an exact-boundary assertion silently loose.
    """
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _exact_payload(size: int) -> bytes:
    """Serialized Job body of exactly ``size`` bytes, same encoding ``httpx`` uses.

    The known members stay contract-legal; only the unknown ``padding`` member
    grows the body, so the receive layer -- not the schema -- is what decides.
    """
    empty = _body(padding="")
    overhead = _wire_bytes(empty)
    assert size > overhead
    payload = json.dumps(_body(padding="a" * (size - overhead)), ensure_ascii=False).encode("utf-8")
    assert len(payload) == size
    return payload


class _Captured:
    """Status and JSON body of one raw ASGI call."""

    def __init__(self) -> None:
        self.status = 0
        self.body = b""

    def payload(self) -> dict[str, Any]:
        assert self.body, "the app returned an empty body"
        return cast(dict[str, Any], json.loads(self.body))


async def _post_without_content_length(
    app: Any, payload: bytes, *, chunks: int, key: str
) -> _Captured:
    """POST ``payload`` as a chunked request: no ``Content-Length`` reaches the app.

    The real server omits the header for ``Transfer-Encoding: chunked`` bodies, so
    this drives the same ``request.stream()`` path: several ``http.request``
    messages with ``more_body`` before the final empty one.
    """
    size = max(1, -(-len(payload) // chunks))
    pending = [payload[index : index + size] for index in range(0, len(payload), size)]
    path = PREFIX.encode()
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": PREFIX,
        "raw_path": path,
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"transfer-encoding", b"chunked"),
            (b"idempotency-key", key.encode()),
        ],
        "client": ("127.0.0.1", 51000),
        "server": ("test", 80),
        "state": {},
    }
    captured = _Captured()

    async def receive() -> dict[str, Any]:
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            captured.status = cast(int, message["status"])
        elif message["type"] == "http.response.body":
            captured.body += cast(bytes, message.get("body", b""))

    assert not any(name == b"content-length" for name, _ in scope["headers"])
    await app(scope, receive, send)
    return captured


def test_create_job_declares_a_413_payload_too_large() -> None:
    responses = _create_job()["responses"]
    assert "413" in responses, (
        "createJob refuses bodies above the deployment receive bound, so the "
        "operation must declare 413 instead of leaving that answer undeclared"
    )
    # Reuse of the existing shared component, not a second semantic copy.
    assert responses["413"] == {"$ref": "#/components/responses/PayloadTooLarge"}
    assert response_contract("createJob", 413)["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


def test_the_declaration_names_a_deployment_bound_and_the_stable_code() -> None:
    description = cast(str, _create_job()["description"])
    lowered = description.lower()
    # The bound must be described as deployment-determined: pinning a literal byte
    # count here would drift from ``job_receive_max_bytes``.
    assert "deployment" in lowered
    assert "413" in description and JOB_REQUEST_TOO_LARGE in description
    assert "256 kib" in lowered, "the contract states the deployment default, not a promise"
    # The deployment default in the declaration must be the shipped one.
    assert f"{_settings().job_receive_max_bytes // 1024} KiB" in description


async def test_body_over_the_receive_bound_answers_the_declared_413() -> None:
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    max_bytes = _settings().job_receive_max_bytes
    # The text is at the declared plan ceiling, so only the transport bound can
    # explain the refusal; ``padding`` is not a contract member and exists only to
    # grow the wire body past that bound.
    body = _body(text="中" * TEXT_CEILING, padding="a" * (max_bytes + 1024))
    assert _wire_bytes(body) > max_bytes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-body-bound-too-large"}
        )

    assert response.status_code == 413, response.text
    error = response.json()["error"]
    assert error["code"] == JOB_REQUEST_TOO_LARGE
    assert error["type"] == "invalid_request_error"
    assert error["retryable"] is False
    assert response.headers["cache-control"] == "no-store"
    # The reply is the declared 413 envelope, not an arbitrary framework error.
    assert_error_contract("createJob", response)
    # Refused before parse and admission: a parsed body would be the unknown
    # member's 422, and an admitted one would reach the recording service.
    assert jobs.submitted == []


async def test_declared_content_length_over_the_bound_answers_the_declared_413() -> None:
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    max_bytes = _settings().job_receive_max_bytes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A declared length is refused before the body is read, so the sent bytes
        # stay far below the bound and only the header can explain the answer.
        response = await client.post(
            PREFIX,
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(max_bytes + 1),
                "Idempotency-Key": "synthetic-declared-length",
            },
        )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == JOB_REQUEST_TOO_LARGE
    assert_error_contract("createJob", response)
    assert jobs.submitted == []


@pytest.mark.parametrize(
    ("offset", "status", "code"),
    [
        (-1, 422, "unsupported_parameter"),
        (0, 422, "unsupported_parameter"),
        (1, 413, JOB_REQUEST_TOO_LARGE),
    ],
    ids=["one-byte-under-bound", "exactly-at-bound", "one-byte-over-bound"],
)
async def test_the_receive_bound_is_inclusive(offset: int, status: int, code: str) -> None:
    """``max_bytes`` itself is read; only ``max_bytes + 1`` is refused.

    The accepted answers are 422 ``unsupported_parameter``: that code is produced
    after the receive layer has read the whole body, so it proves the bound let the
    document through instead of merely producing a different error.
    """
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    max_bytes = _settings().job_receive_max_bytes
    payload = _exact_payload(max_bytes + offset)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PREFIX,
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": f"synthetic-bound{offset:+d}",
            },
        )

    assert len(payload) == max_bytes + offset
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert_error_contract("createJob", response)
    assert jobs.submitted == []


async def test_a_chunked_body_without_content_length_is_bounded_while_streaming() -> None:
    """The streaming accumulator is the only guard when no length is declared.

    The declared-length check is bypassed by omitting ``Content-Length`` (chunked
    transfer). If the accumulator were removed, the oversized body would reach
    schema validation and answer 422 instead of 413, which this case would catch.
    """
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    max_bytes = _settings().job_receive_max_bytes
    oversized = _exact_payload(max_bytes + 1024)

    # Control on the same transport: a small chunked body is routed and validated
    # normally, so a 413 below cannot come from the transport being unsupported.
    small = await _post_without_content_length(
        app, _exact_payload(2048), chunks=4, key="synthetic-chunked-small"
    )
    assert small.status == 422, small.body
    assert small.payload()["error"]["code"] == "unsupported_parameter"

    refused = await _post_without_content_length(
        app, oversized, chunks=5, key="synthetic-chunked-oversized"
    )
    assert refused.status == 413, refused.body
    error = refused.payload()["error"]
    assert error["code"] == JOB_REQUEST_TOO_LARGE
    assert error["type"] == "invalid_request_error"
    assert error["retryable"] is False
    # The refusal happened before parse and admission.
    assert jobs.submitted == []


async def test_negative_content_length_stays_a_400_not_a_413() -> None:
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PREFIX,
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "-1",
                "Idempotency-Key": "synthetic-negative-length",
            },
        )

    # A negative declared length is a malformed request; 413 would claim the body
    # was too large, which is a different cause and a different caller action.
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_job_request"
    assert jobs.submitted == []


@pytest.mark.parametrize(
    "text",
    ["啊" * (TEXT_CEILING + 1), "a" * (TEXT_CEILING + 1)],
    ids=["cjk-over-text-ceiling", "ascii-over-text-ceiling"],
)
async def test_text_above_the_declared_ceiling_keeps_its_own_422(text: str) -> None:
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    body = _body(text=text)
    # Far below the receive bound, so only the declared field ceiling can explain
    # this refusal. Widening the byte ceiling must not turn this case into a 413.
    assert _wire_bytes(body) < _settings().job_receive_max_bytes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            PREFIX, json=body, headers={"Idempotency-Key": "synthetic-over-text-ceiling"}
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unsupported_parameter"
    assert_error_contract("createJob", response)
    assert jobs.submitted == []
