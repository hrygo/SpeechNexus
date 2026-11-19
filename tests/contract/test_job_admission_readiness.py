"""The admission-readiness verdict is one value, and its enum is the create path's.

#845: with an elapsed engine readiness receipt every ``POST /v1/nexus/jobs`` answers
``503 engine_not_ready`` while ``/readyz``, the container healthcheck and ``docker ps``
stay green. ``GET /v1/nexus/jobs/admission-readiness`` exists so the refusal is
observable without attempting one, and #845's four review rounds kept re-introducing
the same drift: the report was a *second* rendering of the create path's rules, so it
could name a refusal class a create never answers.

This surface is therefore declared as one value. ``cause`` is a single nullable enum
member -- the refusal the create path would reach *first* -- rather than a list, and
``status`` is ``admitting`` if and only if ``cause`` is null, and if and only if a
create sent at that instant would be admitted. A list could only describe a precedence
the create path does not have (which observed refusal it reached first) and let the
report name a class no create returns.

The assertions below read the declaration rather than a copy of it. The cause enum and
the create vocabulary are extracted from ``contracts/speech-openapi.yaml``; the two
Python declarations (``AdmissionReadinessCause`` and ``DEPLOYMENT_STATE_CODES``) are
compared against it in both directions. A code added on one side alone is red here,
which is the failure that matters: a class missing from the report lets it answer
``admitting`` while every create is refused, and a class naming no declared code
points an operator at an answer the API does not produce.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import ValidationError
from speechnexus_contracts import parse

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.domain.job_admission_readiness import (
    CAUSE_CODES,
    AdmissionReadinessCause,
    AdmissionReadinessStatus,
)
from speechnexus_backend.speech.interfaces.http.job_errors import (
    DEPLOYMENT_STATE_CODES,
    job_error,
)
from speechnexus_backend.speech.interfaces.http.unimplemented import (
    UNIMPLEMENTED_OPERATIONS,
    assert_placeholder_ownership,
    create_unimplemented_router,
    iter_routes,
)
from tests.contract.test_speech_http_statuses import response_contract
from tests.support.contracts import validator
from tests.unit.backend.test_health import _settings

OPERATION = "getJobAdmissionReadiness"
CREATE_OPERATION = "createJob"
PATH = "/v1/nexus/jobs/admission-readiness"
REQUEST_ID = "contract-admission-readiness"
WORKSPACE = "workspace_default"
CHECKED_AT = "2026-11-16T00:00:00+00:00"
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)
# The statuses whose create answer is enumerated. A shared ``ErrorResponse`` reference
# accepts any code, so only an enumerated status is a declaration that can be compared
# -- the capacity answer used to be a shared ``TooManyRequests`` and made half of the
# original guard vacuous (#845 review, section 4.3).
DECLARED_CREATE_STATUSES = (422, 429, 500, 503)

# The create codes that are *not* deployment state, one reason each. Together with
# ``DEPLOYMENT_STATE_CODES`` this partitions every enumerated create code, so a code
# added to ``createJob``'s declaration alone fails
# ``test_the_declared_partition_covers_every_create_code`` instead of silently having
# no cause (#845 second review, R4).
REQUEST_SHAPED = "depends on the request body, so no deployment state predicts it"
SINGLE_REQUEST = "a consequence of one request's identity, cause stays uncaused"
DEFECT = "a deployment defect answered as 500, not a refusal"

NON_DEPLOYMENT_STATE_CODES: dict[str, str] = {
    "unsupported_parameter": REQUEST_SHAPED,
    "job_kind_not_supported": REQUEST_SHAPED,
    "model_not_published": REQUEST_SHAPED,
    "model_binding_not_published": REQUEST_SHAPED,
    "model_binding_retired": REQUEST_SHAPED,
    "model_binding_not_current": REQUEST_SHAPED,
    "model_binding_mismatch": REQUEST_SHAPED,
    "model_binding_task_not_supported": REQUEST_SHAPED,
    "voice_not_published": REQUEST_SHAPED,
    "profile_not_published": REQUEST_SHAPED,
    "pronunciation_not_found": REQUEST_SHAPED,
    "asset_not_ready": REQUEST_SHAPED,
    "voice_draft_not_candidate": REQUEST_SHAPED,
    "job_binding_incomplete": REQUEST_SHAPED,
    "job_text_asset_unsupported": REQUEST_SHAPED,
    "job_request_expired": SINGLE_REQUEST,
    "job_outcome_unknown": SINGLE_REQUEST,
    "job_admission_contended": SINGLE_REQUEST,
    "recipe_not_admitted": REQUEST_SHAPED,
    "admission_state_invalid": DEFECT,
}

REPORT_PROPERTIES = {
    "status",
    "cause",
    "workspace_id",
    "checked_at",
    "detail",
    "instances",
    "capacity",
}


def _operation(operation_id: str) -> dict[str, Any]:
    for item in cast(dict[str, Any], DOCUMENT["paths"]).values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == operation_id:
                return specification
    raise AssertionError(f"the speech contract declares no {operation_id} operation")


def _schema(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], DOCUMENT["components"]["schemas"][name])


def _report_schema() -> dict[str, Any]:
    return _schema("AdmissionReadiness")


def _cause_enum() -> list[str]:
    cause = _report_schema()["properties"]["cause"]
    members: list[str] = []
    for branch in cast(list[dict[str, Any]], cause["oneOf"]):
        if branch.get("type") == "string":
            members.extend(cast(list[str], branch["enum"]))
        else:
            # The nullable branch is what makes `admitting` expressible.
            assert branch == {"type": "null"}
    return members


def _create_codes(status: int) -> set[str]:
    """The codes ``createJob`` declares for one status, from the contract itself."""
    contract = response_contract(CREATE_OPERATION, status)
    schema = cast(dict[str, Any], contract["content"]["application/json"]["schema"])
    narrowed = schema["allOf"][1]["properties"]["error"]["properties"]["code"]
    return set(cast(list[str], narrowed["enum"]))


def _create_job_accepts(status: int, code: str) -> bool:
    """Whether ``createJob`` declares this code for this status.

    Asking the authoritative validator instead of reading an ``enum`` by hand keeps
    external ``$ref`` files (``shared/errors.schema.json``) in play: a narrowed
    response rejects the code, a shared one accepts it, and both are declarations.
    """
    contract = response_contract(CREATE_OPERATION, status)
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


def _report_body(*, status: str, cause: str | None) -> dict[str, Any]:
    detail = None
    if cause is not None:
        detail = f"A create would answer {CAUSE_CODES[AdmissionReadinessCause(cause)]}."
    return {
        "status": status,
        "cause": cause,
        "workspace_id": WORKSPACE,
        "checked_at": CHECKED_AT,
        "detail": detail,
        "instances": [],
        "capacity": None,
    }


def test_the_report_is_declared_as_one_value_on_a_200_body() -> None:
    operation = _operation(OPERATION)
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AdmissionReadiness"
    }
    schema = _report_schema()
    assert set(schema["properties"]) == REPORT_PROPERTIES
    # The multi-valued field is gone, not merely optional: a consumer cannot read a
    # list, and the enum below is the only place a refusal class can appear.
    assert "causes" not in schema["properties"]
    assert set(schema["required"]) == REPORT_PROPERTIES
    assert _report_schema()["properties"]["status"]["enum"] == ["admitting", "degraded"]


def test_the_declaration_states_the_iff_between_status_cause_and_admission() -> None:
    # The rule has to survive in the declaration, not only in an implementation
    # comment: a poller reads `status`, an operator reads `cause`, and the two must be
    # the same answer.
    status = cast(str, _report_schema()["properties"]["status"]["description"])
    assert "if and only if `cause` is null" in status
    assert "if and only if a create" in status and "would be admitted" in status

    cause = cast(str, _report_schema()["properties"]["cause"]["description"])
    assert "One value, never a set" in cause

    description = cast(str, _operation(OPERATION)["description"])
    assert "if and only if `cause` is null" in description
    assert "would be admitted" in description
    # The reason this is not a `/readyz` check has to survive too: failing a
    # process-health probe would evict the whole API from a load balancer for a
    # condition that refuses creation only (#828 review B8).
    assert "/readyz" in description
    assert "load balancer" in description
    # Every cause is documented with the code a create answers for it, and no name
    # the enum does not own appears in the prose.
    for member in _cause_enum():
        assert f"`{member}`" in description, member
    assert "workspace_not_provisioned" not in description
    assert "`causes`" not in description


def test_the_schema_forbids_a_status_that_contradicts_the_cause() -> None:
    # The generated consumer enforces the iff, so a body cannot carry a refusal while
    # admitting or be degraded without one. This is the declaration half; the
    # projection half is the create path's own predicate.
    for status, cause in (("admitting", None), ("degraded", "engine_receipt_expired")):
        assert parse("AdmissionReadiness", _report_body(status=status, cause=cause)) == (
            _report_body(status=status, cause=cause)
        )
    for status, cause in (("admitting", "engine_receipt_expired"), ("degraded", None)):
        with pytest.raises(ValueError):
            parse("AdmissionReadiness", _report_body(status=status, cause=cause))
    # `detail` follows the same rule: an admitting report explains nothing, a
    # degraded one always carries the sentence.
    no_explanation = _report_body(status="degraded", cause="capacity_exhausted")
    no_explanation["detail"] = None
    with pytest.raises(ValueError):
        parse("AdmissionReadiness", no_explanation)
    stray_explanation = _report_body(status="admitting", cause=None)
    stray_explanation["detail"] = "Everything is fine."
    with pytest.raises(ValueError):
        parse("AdmissionReadiness", stray_explanation)


def test_the_report_document_rejects_an_undeclared_cause() -> None:
    # The enum is the contract: a class the schema does not declare must not travel,
    # and the single-value shape must not accept a list sneaking back in.
    for bad in ("engine_not_ready", "workspace_not_provisioned"):
        with pytest.raises(ValueError):
            parse("AdmissionReadiness", _report_body(status="degraded", cause=bad))
    body = _report_body(status="degraded", cause="capacity_exhausted")
    body["cause"] = ["capacity_exhausted"]
    with pytest.raises(ValueError):
        parse("AdmissionReadiness", body)


def test_the_contract_cause_enum_is_the_frozen_cause_vocabulary() -> None:
    assert set(_cause_enum()) == {cause.value for cause in AdmissionReadinessCause}
    assert set(_report_schema()["properties"]["status"]["enum"]) == {
        status.value for status in AdmissionReadinessStatus
    }


def test_every_cause_names_one_declared_create_code() -> None:
    # Injective: one code per class. Two classes sharing an answer would mean the
    # report cannot name the operator action the code implies.
    assert len(set(CAUSE_CODES.values())) == len(CAUSE_CODES) == len(AdmissionReadinessCause)


@pytest.mark.parametrize("cause", list(AdmissionReadinessCause), ids=lambda cause: cause.value)
def test_every_cause_names_a_create_code_the_create_operation_declares(
    cause: AdmissionReadinessCause,
) -> None:
    # The report cannot point an operator at an answer the API does not produce.
    code = CAUSE_CODES[cause]
    status = _status_of(code)
    assert status in DECLARED_CREATE_STATUSES, (
        f"{cause.value} names {code} on {status}, whose create answer is not enumerated"
    )
    assert _create_job_accepts(status, code), (
        f"{cause.value} names {code}, which createJob does not declare for {status}"
    )


def test_the_declaration_rejects_a_code_arriving_on_the_wrong_status() -> None:
    # The control: the check above can fail. `job_unavailable` is declared for 503, so
    # it must not be accepted as the 422 answer, and an invented code must not be
    # accepted anywhere.
    assert not _create_job_accepts(422, "job_unavailable")
    assert not _create_job_accepts(503, "engine_not_a_real_code")
    assert _create_job_accepts(422, "workspace_not_configured")


def test_the_causes_are_exactly_the_declared_deployment_state_answers() -> None:
    # Bidirectional over the two Python declarations: every class the report can name
    # is a deployment-state answer and every deployment-state answer has a class.
    assert set(CAUSE_CODES.values()) == DEPLOYMENT_STATE_CODES


def test_the_declared_partition_covers_every_create_code() -> None:
    # The closure the Python-to-Python comparison cannot give: the partition is read
    # against createJob's own declaration, so a code added to the contract alone --
    # or a cause mapping to a code the create operation stopped declaring -- fails.
    declared: set[str] = set()
    for status in DECLARED_CREATE_STATUSES:
        declared |= _create_codes(status)
    assert DEPLOYMENT_STATE_CODES | set(NON_DEPLOYMENT_STATE_CODES) == declared
    assert DEPLOYMENT_STATE_CODES.isdisjoint(NON_DEPLOYMENT_STATE_CODES)
    assert all(reason for reason in NON_DEPLOYMENT_STATE_CODES.values())
    # The contract's own enum, mapped through the declared table, has to cover the
    # same set: a class dropped from the declaration is a create answer the report
    # can no longer name, which is the drift this surface exists to remove.
    assert {CAUSE_CODES[AdmissionReadinessCause(member)] for member in _cause_enum()} == (
        DEPLOYMENT_STATE_CODES
    )
    # And every classified code is a declared answer, on the status the error table
    # renders it with.
    for code in DEPLOYMENT_STATE_CODES:
        assert _create_job_accepts(_status_of(code), code)


def test_the_capacity_answer_enumerates_its_code() -> None:
    # The 429 half used to be vacuous: `TooManyRequests` is a shared `ErrorResponse`,
    # so any code validated against it. `createJob` now enumerates the one capacity
    # code, so the check above can fail and a foreign code is rejected.
    schema = response_contract(CREATE_OPERATION, 429)["content"]["application/json"]["schema"]
    assert schema != {"$ref": "#/components/responses/TooManyRequests"}
    assert _create_codes(429) == {"job_capacity_exhausted"}
    assert not _create_job_accepts(429, "engine_not_ready")
    assert not _create_job_accepts(429, "engine_state_mismatch")
    assert not _create_job_accepts(503, "job_capacity_exhausted")


def test_the_report_path_is_a_literal_sibling_of_the_identifier_route() -> None:
    # The report path is literal and shares the `/v1/nexus/jobs/` prefix with
    # `getJob`. The identifier is opaque (`Identifier` allows `-`, and a real job id is
    # `job-<hex>`), so the two paths cannot be told apart by shape -- only by which
    # route Starlette reaches first. That ordering is what the two tests below pin; a
    # segment-count or character-set assertion here would prove nothing.
    paths = cast(dict[str, Any], DOCUMENT["paths"])
    assert PATH in paths
    assert "/v1/nexus/jobs/{job_id}" in paths
    # The operation ships its own handler now, so it must not be left in the
    # placeholder table: a placeholder that owned this template would answer 503 for
    # an operation the assembly really serves.
    assert "getJobAdmissionReadiness" not in UNIMPLEMENTED_OPERATIONS


async def test_the_report_path_is_answered_by_its_own_route_not_by_get_job() -> None:
    # Measured on the assembled application rather than argued: the literal report
    # route is registered before the identifier route, and the handler that answers
    # this path is the report's own. A real Job id still reaches `getJob`, which
    # answers `job_unavailable` because no job service is injected here.
    app = create_app(settings=_settings())
    flat = list(iter_routes(app.routes))
    routes = [getattr(route, "path", None) for route in flat]
    report_index = routes.index(PATH)
    job_index = routes.index("/v1/nexus/jobs/{job_id}")
    assert report_index < job_index
    handler = flat[report_index]
    assert getattr(handler.endpoint, "__name__", "") == "get_admission_readiness"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        report = await http.get(PATH, headers={"X-Request-ID": REQUEST_ID})
        job = await http.get("/v1/nexus/jobs/job-0123456789ab")
    # Without the admission role the report cannot be produced, and it says so with
    # the same declared storage answer a create uses -- never `admitting` by default.
    assert (report.status_code, report.json()["error"]["code"]) == (503, "job_unavailable")
    assert (job.status_code, job.json()["error"]["code"]) == (503, "job_unavailable")


def test_the_identifier_route_registered_first_swallows_the_report_path() -> None:
    # The control, and the reason the ordering matters: a parameter matches any single
    # segment, so with `{job_id}` registered first the report path is read as a Job id.
    # Both consequences are asserted: the request is answered by `getJob`, and the
    # assembly guard rejects that order. The real handler is registered before the
    # identifier route for exactly this reason.
    app = FastAPI()

    @app.get("/v1/nexus/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, str]:
        return {"job_id": job_id}

    @app.get(PATH)
    async def get_admission_readiness() -> dict[str, str]:
        return {"status": "admitting"}

    app.include_router(create_unimplemented_router())
    response = asyncio.run(_request(app, "GET", PATH))
    assert response.status_code == 200
    assert response.json() == {"job_id": "admission-readiness"}
    with pytest.raises(RuntimeError, match="answers /v1/nexus/jobs/admission-readiness"):
        assert_placeholder_ownership(app.routes)


async def _request(app: Any, method: str, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        return await http.request(method, path)
