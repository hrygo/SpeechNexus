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
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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
