"""The create contract declares its default workspace and its configuration refusal.

#702: the contract did not say which workspace an omitted ``workspace_id`` selects,
while the server substituted ``workspace-default``. The workspace row exists from
the asset migration, but its quota row is written only by engine registration, so
a schema-valid create in a default deployment answered 503 ``job_unavailable``
("Job storage is unavailable") -- a retry-shaped outage answer for a condition no
retry can fix.

The cases below pin the two steps of the fix:

* the contract states the default workspace and the refusal: the field description
  carries ``default: workspace-default``, and ``createJob`` names 422
  ``workspace_not_configured`` and keeps it separate from both storage answers;
* the storage answers keep their own retry rules: a rejected or unreconcilable read
  is 503 ``job_unavailable`` with ``retryable: false``, while a dead connection or
  timed-out attempt is 503 ``job_outcome_unknown`` with ``retryable: true``. The
  declaration must not claim one rule for every storage failure (#702 review M2);
* the answers are real HTTP: each refusal reaches the caller as the declared
  envelope, validated against the declared response schema, so a code that is not
  the declared shape cannot pass by status alone.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import (
    JobAdmissionRejectedError,
    JobOutcomeUnknownError,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from tests.contract.test_speech_http_statuses import assert_error_contract, response_contract
from tests.support.contracts import validator
from tests.unit.backend.test_health import _settings

PREFIX = "/v1/nexus/jobs"
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
DEFAULT_WORKSPACE = "workspace-default"
NOT_CONFIGURED = "workspace_not_configured"
REQUEST_ID = "contract-workspace-default"


def _create_job() -> dict[str, Any]:
    for item in cast(dict[str, Any], DOCUMENT["paths"]).values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == "createJob":
                return specification
    raise AssertionError("the speech contract declares no createJob operation")


def _workspace_field() -> dict[str, Any]:
    schema = cast(dict[str, Any], DOCUMENT["components"]["schemas"]["JobCreateBase"])
    field = cast(dict[str, Any], schema["properties"]["workspace_id"])
    assert field.get("$ref") == "#/components/schemas/Identifier"
    return field


def _body() -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic public job"},
        "voice": {"id": "voice-vivian", "revision": "4"},
        "client_reference": "synthetic-workspace-default",
    }


def test_the_request_field_declares_the_default_workspace_and_its_refusals() -> None:
    field = _workspace_field()
    description = cast(str, field.get("description", ""))
    # The default is declared in the field's own description rather than through a
    # JSON Schema ``default`` keyword: adding that keyword alters every resolved
    # ``JobCreateRequest`` variant and the HTTP structural gate rejects an
    # exclusive-union change. The prose still states the exact default value the
    # server substitutes.
    assert f"default: {DEFAULT_WORKSPACE}" in description, (
        "an omitted workspace_id selects workspace-default, so the declaration must "
        "state it instead of leaving the behaviour to the implementation"
    )
    for code in ("workspace_not_found", "workspace_disabled", NOT_CONFIGURED):
        assert code in description, f"the field must name the deterministic answer {code}"


def test_create_job_declares_the_configuration_refusal_separately_from_the_outage() -> None:
    operation = _create_job()
    description = cast(str, operation["description"])
    assert DEFAULT_WORKSPACE in description
    assert NOT_CONFIGURED in description
    # The refusal must be described as deterministic and non-retryable, and the
    # contract must keep the storage-outage 503 as the answer for a failed read.
    assert "never retryable" in description
    assert "retrying" in description.lower()
    assert "503 `job_unavailable`" in description
    # The declaration must not claim every storage failure is non-retryable: the
    # rejected-query answer is ``job_unavailable`` (retryable: false), while a dead
    # connection or timed-out attempt is ``job_outcome_unknown`` (retryable: true).
    assert "connection-level failure" in description
    assert "`job_outcome_unknown`" in description
    assert "`retryable: true`" in description
    # The status the code answers with is declared on the operation.
    assert "422" in operation["responses"]
    declared = response_contract("createJob", 422)
    assert declared["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


def test_the_two_storage_answers_keep_their_own_retry_rules() -> None:
    configured = job_error(NOT_CONFIGURED, REQUEST_ID)
    assert configured.status_code == 422
    body = _json(configured)
    assert body["error"]["code"] == NOT_CONFIGURED
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["retryable"] is False, (
        "a missing quota row cannot be fixed by retrying, so the 4xx must not be marked retryable"
    )
    assert body["error"]["message"] and "quota" in body["error"]["message"]

    outage = job_error("job_unavailable", REQUEST_ID)
    assert outage.status_code == 503
    outage_body = _json(outage)
    assert outage_body["error"]["type"] == "service_unavailable_error"
    # The rejected-query code keeps its own answer; only the deterministic
    # configuration refusal moves to 422.
    assert outage_body["error"]["code"] == "job_unavailable"
    assert outage_body["error"]["retryable"] is False, (
        "a rejected or unreconcilable read cannot be fixed by retrying"
    )

    # A dead connection is the *other* storage failure and must stay retryable, so
    # neither code can absorb the other without failing one of these two assertions.
    unknown = job_error("job_outcome_unknown", REQUEST_ID)
    assert unknown.status_code == 503
    unknown_body = _json(unknown)
    assert unknown_body["error"]["type"] == "service_unavailable_error"
    assert unknown_body["error"]["code"] == "job_outcome_unknown"
    assert unknown_body["error"]["retryable"] is True, (
        "a connection-level failure may be retried; the contract must not claim every "
        "storage failure is non-retryable"
    )


class _RefusingJobs:
    """Refuses one create with the exception the admission store reports."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def submit(self, command: Any, *, request_id: str, idempotency_key: str | None) -> Any:
        raise self.error

    def artifacts(self, job: Any) -> tuple[()]:
        return ()


def _json(response: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(bytes(response.body)))


async def _post(error: Exception) -> Any:
    app = create_app(settings=_settings(), jobs=cast(Any, _RefusingJobs(error)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-workspace-default"}
        )


@pytest.mark.parametrize(
    ("error", "status", "code", "retryable"),
    [
        (JobAdmissionRejectedError(NOT_CONFIGURED), 422, NOT_CONFIGURED, False),
        # A real storage outage keeps the 503 answer: moving this onto the new 422
        # must fail here instead of passing as "a better error".
        (PublicJobUnavailableError(), 503, "job_unavailable", False),
        # The connection-level failure is a *different* 503: retryable, so collapsing
        # both storage failures onto one code (or one retry rule) fails here.
        (JobOutcomeUnknownError(), 503, "job_outcome_unknown", True),
    ],
)
async def test_create_answers_the_declared_envelope(
    error: Exception, status: int, code: str, retryable: bool
) -> None:
    response = await _post(error)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable
    assert_error_contract("createJob", response)
