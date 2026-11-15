"""An elapsed engine readiness receipt is its own refusal, not a storage outage.

#828 (found by the #572 end-to-end acceptance): after a resume the first
``POST /v1/nexus/jobs`` answered

    503 {"error":{"code":"job_unavailable","type":"service_unavailable_error",
                  "message":"Job storage is unavailable.","retryable":false}}

while every storage fact was healthy. The engine instance's readiness receipt had
elapsed (``expires_at_us`` passed), admission refused it as
``reconciliation_required`` and ``rejection_code`` folded every non-capacity
admission code into ``job_unavailable``. Re-running the registration answered
``202`` for the same body, so the answer named a broken store for a condition a
re-registration fixes.

``retryable`` follows one rule (`contracts/shared/errors.schema.json`
``ErrorBody.retryable``): it is true only when the same request can succeed
without an out-of-band change. Re-signing the receipt is an operator step, so the
readiness answer is ``retryable: false`` and the message states the recovery
(#828 review B1).

The cases below pin the two halves of the fix in the declaration and over real
HTTP, in both directions:

* an elapsed receipt answers 503 ``engine_not_ready`` with ``retryable: false``
  and says the receipt must be re-signed before a new request;
* a store that really refused the read keeps 503 ``job_unavailable`` with
  ``retryable: false`` -- the readiness code must not absorb it, and it must not
  absorb the readiness code;
* the operation description states the refusal, its recovery and that it is
  neither the storage nor the capacity answer, and the 503 response declares the
  whole set of codes it can carry.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import JobAdmissionRejectedError
from speechnexus_backend.speech.infrastructure.public_job_admission import rejection_code
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from tests.contract.test_speech_http_statuses import assert_error_contract, response_contract
from tests.support.contracts import validator
from tests.unit.backend.test_health import _settings

PREFIX = "/v1/nexus/jobs"
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
CODE = "engine_not_ready"
REQUEST_ID = "contract-engine-readiness"


def _create_job() -> dict[str, Any]:
    for item in cast(dict[str, Any], DOCUMENT["paths"]).values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == "createJob":
                return specification
    raise AssertionError("the speech contract declares no createJob operation")


def _declared_codes(status: str) -> list[str]:
    schema = _create_job()["responses"][status]["content"]["application/json"]["schema"]
    return cast(
        list[str],
        schema["allOf"][1]["properties"]["error"]["properties"]["code"]["enum"],
    )


def _body() -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic public job"},
        "voice": {"id": "voice-vivian", "revision": "4"},
        "client_reference": "synthetic-engine-readiness",
    }


def test_the_operation_declares_the_readiness_refusal_with_its_recovery() -> None:
    description = cast(str, _create_job()["description"])
    assert f"`{CODE}`" in description
    assert "readiness receipt" in description
    # The declaration must state the retry rule and the out-of-band recovery, not
    # just name a new code.
    assert "`retryable: false`" in description
    assert "`retryable: true`" in description  # the contention class only
    assert "speechnexus-register-engine --renew-readiness" in description
    assert "writes no row" in description
    # The readiness answer must stay distinct from both neighbours it used to hide in.
    assert "`job_unavailable`" in description
    assert "`job_capacity_exhausted`" in description
    assert "receipt TTL" in description
    # The 503 surface declares every code it can carry, so the new one is
    # machine-discoverable instead of prose-only (#828 review B2).
    assert "503" in _create_job()["responses"]
    assert CODE in _declared_codes("503")
    declared = response_contract("createJob", 503)
    assert declared["content"]["application/json"]["schema"]["allOf"][0] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


def test_the_readiness_answer_is_not_retryable_and_names_the_receipt() -> None:
    response = job_error(CODE, REQUEST_ID)
    assert response.status_code == 503
    body = _json(response)
    assert body["error"]["code"] == CODE
    assert body["error"]["type"] == "service_unavailable_error"
    assert body["error"]["retryable"] is False, (
        "re-signing the receipt is an operator step, so the refusal must not invite a retry"
    )
    assert "readiness" in body["error"]["message"]
    assert "storage" not in body["error"]["message"].lower()
    # ``retryable: false`` must not hide the recovery: the message names the step.
    assert "speechnexus-register-engine --renew-readiness" in body["error"]["message"]


def test_a_store_that_really_refused_the_read_keeps_the_outage_answer() -> None:
    response = job_error("job_unavailable", REQUEST_ID)
    assert response.status_code == 503
    body = _json(response)
    assert body["error"]["code"] == "job_unavailable"
    assert body["error"]["type"] == "service_unavailable_error"
    assert body["error"]["retryable"] is False
    # The two 503s stay distinguishable by code and message in both directions;
    # neither may absorb the other, and only the store answer blames storage.
    readiness = _json(job_error(CODE, REQUEST_ID))
    assert body["error"]["message"] != readiness["error"]["message"]
    assert "storage" in body["error"]["message"].lower()
    assert "storage" not in readiness["error"]["message"].lower()


@pytest.mark.parametrize(
    ("admission_code", "public"),
    [
        # The elapsed receipt names its own recovery.
        ("readiness_expired", CODE),
        # The three engine-side neighbours that used to answer the storage outage
        # are now distinct causes with distinct recoveries (#828 review B3).
        ("reconciliation_required", "engine_state_mismatch"),
        ("policy_or_resource_disabled", "engine_resource_disabled"),
        ("recipe_mismatch", "recipe_not_admitted"),
        # Capacity keeps its own answer.
        ("capacity_exhausted", "job_capacity_exhausted"),
        ("quota_exceeded", "job_capacity_exhausted"),
        ("idempotency_conflict", "idempotency_conflict"),
    ],
)
def test_the_readiness_neighbours_keep_distinct_answers(admission_code: str, public: str) -> None:
    assert rejection_code(admission_code) == public


def _json(response: Any) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(bytes(response.body)))


class _RefusingJobs:
    """Refuses one create with the exception the admission store reports."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def submit(self, command: Any, *, request_id: str, idempotency_key: str | None) -> Any:
        raise self.error

    def artifacts(self, job: Any) -> tuple[()]:
        return ()


async def _post(error: Exception) -> Any:
    app = create_app(settings=_settings(), jobs=cast(Any, _RefusingJobs(error)))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-engine-readiness"}
        )


async def test_create_answers_the_declared_envelope_over_http() -> None:
    response = await _post(JobAdmissionRejectedError(CODE))
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == CODE
    assert response.json()["error"]["retryable"] is False
    assert_error_contract("createJob", response)


async def test_the_http_layer_does_not_fold_the_new_code_into_the_outage() -> None:
    # `_ADMISSION_REJECTION_CODES` decides whether the store's code reaches HTTP as
    # the mapped refusal or falls back to `job_unavailable`. Omitting the new code
    # here would silently restore the defect without changing any unit expectation.
    response = await _post(JobAdmissionRejectedError(CODE))
    outage = _json(job_error("job_unavailable", REQUEST_ID))
    assert response.json()["error"]["code"] != "job_unavailable"
    assert response.json()["error"]["message"] != outage["error"]["message"]
