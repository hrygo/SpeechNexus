"""Admission observability is a declared surface, not a sentence in a refusal.

#845: with an elapsed engine readiness receipt, every ``POST /v1/nexus/jobs``
answers ``503 engine_not_ready`` while ``/readyz``, the container healthcheck and
``docker ps`` stay green. Nothing a monitor polls reflected whether a create would
be admitted, so the refusal was only observable by attempting one.

The cases below pin the two halves of the answer:

* the declaration adds one route whose 200 body carries the verdict, with a closed
  ``causes`` enum -- a machine consumer never parses a message to tell an elapsed
  receipt from exhausted capacity or an unreadable store;
* the runtime keeps the division of labour the declaration states: a degraded
  admission report is ``200`` with a cause while ``/readyz`` is still ``200``, so
  the operational surface keeps meaning process health and cannot be quietly
  repurposed into an admission gate;
* every cause names a public create code the create operation actually declares,
  so a report can never point an operator at an answer the API does not produce.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import ValidationError
from speechnexus_contracts import parse

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.job_admission_readiness import (
    CAUSE_CODES,
    AdmissionReadinessCause,
    AdmissionReadinessFacts,
    storage_unavailable_readiness,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.interfaces.http.job_errors import job_error
from tests.contract.test_speech_http_statuses import assert_error_contract, response_contract
from tests.fake.admission import RUNTIMES
from tests.fake.readiness import StaticProbe
from tests.support.contracts import validator
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_job_admission_readiness import NOW as NOW_US
from tests.unit.backend.test_job_admission_readiness import WORKSPACE as WORKSPACE_ID
from tests.unit.backend.test_job_admission_readiness import _facts, _receipt

PREFIX = "/v1/nexus/jobs"
PATH = f"{PREFIX}/admission-readiness"
OPERATION = "getJobAdmissionReadiness"
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
WORKSPACE = WORKSPACE_ID
REQUEST_ID = "contract-admission-report"
DAY_US = 86_400_000_000


def _operation() -> dict[str, Any]:
    for item in cast(dict[str, Any], DOCUMENT["paths"]).values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == OPERATION:
                return specification
    raise AssertionError(f"the speech contract declares no {OPERATION} operation")


def _schema(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], DOCUMENT["components"]["schemas"][name])


def _causes() -> list[str]:
    return cast(
        list[str],
        _schema("AdmissionReadiness")["properties"]["causes"]["items"]["enum"],
    )


class _FactsStore:
    def __init__(self, facts: AdmissionReadinessFacts | None) -> None:
        self._facts = facts

    def admission_readiness_facts(self, workspace_id: str) -> AdmissionReadinessFacts:
        if self._facts is None:
            raise PublicJobUnavailableError
        return self._facts

    def now_us(self) -> int:
        return NOW_US


def _client(store: _FactsStore) -> AsyncClient:
    # A healthy process-health surface: the point of the cases below is that
    # `/readyz` stays 200 while admission is degraded.
    readiness = ReadinessService(
        [StaticProbe("postgresql"), StaticProbe("object_storage")], timeout_seconds=1
    )
    jobs = PublicJobs(store, store, readiness=store)  # type: ignore[arg-type]
    app = create_app(settings=_settings(), readiness=readiness, jobs=jobs)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _read(store: _FactsStore) -> tuple[Any, Any]:
    async with _client(store) as client:
        report = await client.get(PATH, headers={"X-Request-ID": "contract-admission-report"})
        readyz = await client.get("/readyz")
    return report, readyz


def test_the_contract_declares_one_report_route_with_a_closed_cause_enum() -> None:
    operation = _operation()
    assert set(_causes()) == {cause.value for cause in AdmissionReadinessCause}
    # The three classes the issue names are members; `workspace_not_provisioned` is
    # the fourth so the surface never says `admitting` while a create would
    # deterministically answer 422.
    assert {"engine_receipt_expired", "capacity_exhausted", "storage_unavailable"} <= set(_causes())
    responses = cast(dict[str, Any], operation["responses"])
    assert "200" in responses, "the verdict travels in the body, so the report is a 200"
    schema = responses["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/AdmissionReadiness"}
    # `status` is the one field a poller asserts on and it is a closed two-valued
    # enum, not a free string.
    assert _schema("AdmissionReadiness")["properties"]["status"]["enum"] == [
        "admitting",
        "degraded",
    ]


def test_the_declaration_states_the_failure_domain_and_the_division_of_labour() -> None:
    description = cast(str, _operation()["description"])
    # The reason this is not a `/readyz` check has to survive in the declaration:
    # failing a process-health probe would evict the whole API from a load balancer
    # for a condition that refuses creation only (#828 review B8).
    assert "/readyz" in description
    assert "load balancer" in description
    # The two surfaces, named with their own meaning.
    assert "process and dependency health" in description
    assert "whether a create is admitted" in description
    # A degraded deployment must not answer a 5xx here, and the declaration says so.
    assert "200" in description
    assert "`job_unavailable`" in description


def _create_job_accepts(status: int, code: str) -> bool:
    """Whether ``createJob`` declares this code for this status.

    Asking the authoritative validator instead of reading an ``enum`` by hand keeps
    external ``$ref`` files (``shared/errors.schema.json``) in play: a narrowed
    response rejects the code, a shared one accepts it, and both are declarations.
    """
    contract = response_contract("createJob", status)
    schema = contract["content"]["application/json"]["schema"]
    check = SOURCE.evolve(schema={**DOCUMENT, "allOf": [schema]})
    try:
        check.validate(_error_body(code))
    except ValidationError:
        return False
    return True


def _error_body(code: str) -> dict[str, Any]:
    response = job_error(code, REQUEST_ID)
    assert response.status_code == _status_of(code), (
        f"{code} must keep its own status; the report states this mapping"
    )
    return cast(dict[str, Any], json.loads(bytes(response.body)))


def _status_of(code: str) -> int:
    return job_error(code, REQUEST_ID).status_code


@pytest.mark.parametrize(
    ("cause", "status"),
    [
        (AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED, 503),
        (AdmissionReadinessCause.CAPACITY_EXHAUSTED, 429),
        (AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED, 422),
        (AdmissionReadinessCause.STORAGE_UNAVAILABLE, 503),
    ],
)
def test_every_cause_names_a_create_code_the_create_operation_declares(
    cause: AdmissionReadinessCause, status: int
) -> None:
    # The report cannot point an operator at an answer the API does not produce.
    code = CAUSE_CODES[cause]
    assert job_error(code, REQUEST_ID).status_code == status
    assert _create_job_accepts(status, code), (
        f"{cause.value} names {code}, which createJob does not declare for {status}"
    )


def test_the_declaration_rejects_a_code_arriving_on_the_wrong_status() -> None:
    # The control: the check above can fail. `job_unavailable` is declared for 503,
    # so it must not be accepted as the 422 answer, and an invented code must not be
    # accepted anywhere.
    assert not _create_job_accepts(422, "job_unavailable")
    assert not _create_job_accepts(503, "engine_not_a_real_code")


async def test_a_degraded_report_is_a_200_with_a_cause_while_readyz_stays_200() -> None:
    report, readyz = await _read(_FactsStore(_facts(expired=True)))

    assert report.status_code == 200, (
        "a degraded verdict is the body, not the status: a 5xx would make 'the report "
        "is broken' and 'admission is degraded' indistinguishable"
    )
    assert readyz.status_code == 200, (
        "process health is untouched: an elapsed receipt refuses creation, it does not "
        "stop the API from serving"
    )
    body = report.json()
    assert body["status"] == "degraded"
    assert body["causes"] == ["engine_receipt_expired"]
    assert body["workspace_id"] == WORKSPACE
    # The product body is the declared document: validated here against the
    # generated schema, so an undeclared field or a missing one fails this test.
    assert parse("AdmissionReadiness", body) == body
    assert report.headers["cache-control"] == "no-store"


async def test_an_admitting_report_is_a_200_with_no_causes() -> None:
    # A receipt with the whole default window left, so the instance reports `valid`
    # rather than the `expiring` warning threshold.
    report, readyz = await _read(
        _FactsStore(
            _facts(
                runtimes=(
                    replace(
                        RUNTIMES[0],
                        readiness=replace(_receipt(), expires_at_us=NOW_US + 30 * DAY_US),
                    ),
                )
            )
        )
    )

    assert report.status_code == 200
    assert readyz.status_code == 200
    body = report.json()
    assert body["status"] == "admitting"
    assert body["causes"] == []
    # Nothing is degraded, so there is nothing to explain.
    assert body["detail"] is None
    assert body["instances"][0]["readiness_state"] == "valid"
    assert parse("AdmissionReadiness", body) == body


async def test_an_unreadable_store_is_reported_not_raised() -> None:
    report, _ = await _read(_FactsStore(None))

    # The report is the surface that explains a refusal; an outage of the report
    # itself must still answer with the class that says so instead of a 500.
    assert report.status_code == 200
    assert report.json()["causes"] == ["storage_unavailable"]
    assert parse("AdmissionReadiness", report.json()) == report.json()


async def test_a_deployment_without_job_admission_answers_the_declared_envelope() -> None:
    # Every other Job route answers 503 `job_unavailable` when the Job surface is
    # not assembled; this route keeps that answer instead of inventing a fifth one.
    app = create_app(settings=_settings(), jobs=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(PATH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "job_unavailable"
    assert_error_contract(OPERATION, response)


def _resolve(schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = cast(
            dict[str, Any],
            DOCUMENT["components"]["schemas"][cast(str, schema["$ref"]).rsplit("/", 1)[1]],
        )
    return schema


def _create_job_codes(status: int) -> list[str]:
    """The codes ``createJob`` declares for one status.

    A narrowed response lists its own subset under ``allOf[1]``; a shared response
    narrows through ``ErrorResponse``'s full ``code`` enum. Both are declarations,
    so the helper accepts either and returns exactly what the operation allows.
    """
    contract = response_contract("createJob", status)
    schema = cast(dict[str, Any], contract["content"]["application/json"]["schema"])
    if "allOf" in schema:
        return cast(
            list[str],
            schema["allOf"][1]["properties"]["error"]["properties"]["code"]["enum"],
        )
    resolved = _resolve(schema)
    return cast(list[str], resolved["properties"]["error"]["properties"]["code"]["enum"])


def test_the_report_document_rejects_an_undeclared_cause() -> None:
    # The enum is the contract: a cause the schema does not declare must not travel.
    body = json.loads(json.dumps(storage_report_body()))
    body["causes"] = ["engine_not_ready"]
    with pytest.raises(ValueError):
        parse("AdmissionReadiness", body)


def storage_report_body() -> dict[str, Any]:
    report = storage_unavailable_readiness(workspace_id=WORKSPACE, now_us=NOW_US)
    from speechnexus_backend.speech.interfaces.http.jobs import public_admission_readiness

    return public_admission_readiness(report)


def test_the_report_is_not_reachable_as_a_job_identifier() -> None:
    # The literal segment cannot be a Job id (`_IDENTIFIER` excludes `-`), so the
    # two routes cannot be confused; the declaration must not have made the
    # identifier route shadow the report either.
    paths = cast(dict[str, Any], DOCUMENT["paths"])
    assert PATH in paths
    assert f"{PREFIX}/{{job_id}}" in paths


def test_the_causes_are_exactly_the_declared_deployment_state_answers() -> None:
    # Bidirectional: every class the report can name is a declared answer, and every
    # declared "this deployment refuses" answer has a class. A code added to
    # `job_errors` without a cause fails here -- which is the failure that matters,
    # because the report would otherwise say `admitting` while a create refuses
    # (#845 review).
    from speechnexus_backend.speech.interfaces.http.job_errors import DEPLOYMENT_STATE_CODES

    assert set(CAUSE_CODES.values()) == DEPLOYMENT_STATE_CODES


def test_the_capacity_answer_enumerates_its_code() -> None:
    # The 429 half used to be vacuous: `TooManyRequests` is a shared `ErrorResponse`,
    # so any code validated against it. `createJob` now enumerates the one capacity
    # code, so the check above can fail and a foreign code is rejected.
    schema = response_contract("createJob", 429)["content"]["application/json"]["schema"]
    assert schema != {"$ref": "#/components/responses/TooManyRequests"}
    assert _create_job_accepts(429, "job_capacity_exhausted")
    assert not _create_job_accepts(429, "engine_not_ready")
    assert not _create_job_accepts(429, "engine_state_mismatch")


def test_the_declaration_states_that_the_missing_instance_arm_needs_an_operator() -> None:
    # A capacity refusal that only clears when an instance becomes ready is not the
    # "retry and it clears itself" shape `Retry-After` implies, so the declaration
    # says so instead of leaving a client looping forever (#845 review).
    description = cast(str, response_contract("createJob", 429)["description"])
    assert "Retry-After" in description
    assert "operator" in description
