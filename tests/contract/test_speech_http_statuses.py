"""Previously undeclared statuses match real HTTP replies and the public schema."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from jsonschema import ValidationError

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.assets import AssetService
from tests.support.contracts import validator
from tests.unit.backend.test_artifact_http import ARTIFACT_ID, CONTENT, service
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import _service as job_service
from tests.unit.backend.test_transcription_http import chain, files

CASES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/contracts/speech-http-statuses.json"
    ).read_text()
)
SOURCE = validator("speech-openapi.yaml")
DOCUMENT = cast(dict[str, Any], SOURCE.schema)


def response_contract(operation: str, status: int) -> dict[str, Any]:
    for item in DOCUMENT["paths"].values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == operation:
                response: dict[str, Any] = specification["responses"][str(status)]
                while "$ref" in response:
                    response = DOCUMENT["components"]["responses"][
                        response["$ref"].rsplit("/", 1)[1]
                    ]
                return response
    raise AssertionError(operation)


def assert_error_contract(operation: str, response: httpx.Response) -> None:
    contract = response_contract(operation, response.status_code)
    schema = contract["content"]["application/json"]["schema"]
    check = SOURCE.evolve(schema={**DOCUMENT, "allOf": [schema]})
    check.validate(response.json())
    # A bare framework detail or a truncated envelope must not be accepted.
    with pytest.raises(ValidationError):
        check.validate({"detail": "Method Not Allowed"})
    invalid = copy.deepcopy(response.json())
    del invalid["error"]["request_id"]
    with pytest.raises(ValidationError):
        check.validate(invalid)


@pytest.mark.parametrize("case", CASES["method_not_allowed"], ids=lambda case: case["operation"])
async def test_wrong_method_uses_declared_error_schema(case: dict[str, str]) -> None:
    # Enable ingress without reading storage: unsupported methods never call it.
    assets = Mock(spec=AssetService)
    app = create_app(settings=_settings(), assets=assets)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        response = await http.put(case["path"], headers={"X-Request-ID": "status-example"})
    assert response.status_code == 405
    assert response.headers["allow"] == case["allow"]
    assert response.json()["error"]["code"] == case["code"]
    assert response.json()["error"]["request_id"] == "status-example"
    assert response.json()["error"]["retryable"] is False
    assert_error_contract(case["operation"], response)
    assert assets.mock_calls == []


@pytest.mark.parametrize("case", CASES["errors"], ids=lambda case: case["operation"])
async def test_transcription_not_found_and_invalid_cancellation(case: dict[str, Any]) -> None:
    jobs, _, _ = job_service()
    transcriptions, _, _ = chain()
    app = create_app(settings=_settings(), jobs=jobs, transcriptions=transcriptions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        if case["operation"] == "createTranscription":
            response = await http.post(case["path"], data={"model": "unpublished"}, files=files())
        else:
            response = await http.post(case["path"], headers={"Idempotency-Key": "short"})
    assert response.status_code == case["status"]
    assert response.json()["error"]["code"] == case["code"]
    assert_error_contract(case["operation"], response)


@pytest.mark.parametrize("case", CASES["artifacts"], ids=lambda case: case["operation"])
async def test_artifact_conditional_range_and_unavailable_replies(case: dict[str, str]) -> None:
    artifacts, _, _ = service()
    path = case["path"].replace("{artifact_id}", ARTIFACT_ID)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings=_settings(), artifacts=artifacts)),
        base_url="http://localhost",
    ) as http:
        initial = await http.get(path)
        cached = await http.get(path, headers={"If-None-Match": initial.headers["etag"]})
        if case["operation"] == "downloadArtifactContent":
            unsatisfiable = await http.get(path, headers={"Range": f"bytes={len(CONTENT)}-"})
            assert unsatisfiable.status_code == 416
            assert unsatisfiable.headers["content-range"] == f"bytes */{len(CONTENT)}"
            assert_error_contract(case["operation"], unsatisfiable)
    assert cached.status_code == 304 and cached.content == b""
    contract = response_contract(case["operation"], 304)
    assert not contract.get("content")
    assert "ETag" in contract["headers"]
    assert cached.headers["etag"] == initial.headers["etag"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings=_settings())),
        base_url="http://localhost",
    ) as http:
        unavailable = await http.get(path)
    assert unavailable.status_code == 503
    assert_error_contract(case["operation"], unavailable)


@pytest.mark.parametrize(
    "operation",
    [
        "createVoiceDesign",
        "createVoiceClone",
        "getVoiceDraft",
        "updateVoiceDraftMetadata",
        "generateVoiceDraftRevision",
        "createVoicePreview",
        "validateVoiceDraftRevision",
    ],
)
def test_voice_draft_500_covers_response_shape_and_storage_rejection(operation: str) -> None:
    """The declared 500 also describes deterministic storage refusals."""
    description = " ".join(response_contract(operation, 500)["description"].split())
    assert "response-shape violation" in description
    assert "CHECK or constraint refusal" in description
    assert "not retryable" in description


VOICE_DRAFT_SUBMIT_OPERATIONS = (
    "createVoiceDesign",
    "createVoiceClone",
    "generateVoiceDraftRevision",
    "createVoicePreview",
    "validateVoiceDraftRevision",
)

# The 422 refusal keys the voice-draft surface can answer, and the operations that can
# reach each. Derived from the handler's own branches: `not_candidate` answers only the
# two candidate-gated operations, `unsupported_parameter` belongs to the metadata patch
# and never to a submit.
VOICE_DRAFT_422_KEYS: dict[str, tuple[str, ...]] = {
    "input_unavailable": VOICE_DRAFT_SUBMIT_OPERATIONS,
    "request_expired": VOICE_DRAFT_SUBMIT_OPERATIONS,
    "not_candidate": ("createVoicePreview", "validateVoiceDraftRevision"),
}


def _declared_codes(operation: str, status: int) -> set[str]:
    """The code enum one operation's response declares, or an empty set if open."""
    contract = response_contract(operation, status)
    schema = contract["content"]["application/json"]["schema"]
    enum = (
        schema.get("allOf", [{}])[-1]
        .get("properties", {})
        .get("error", {})
        .get("properties", {})
        .get("code", {})
        .get("enum")
    )
    return set(enum or ())


@pytest.mark.parametrize("operation", VOICE_DRAFT_SUBMIT_OPERATIONS)
def test_voice_draft_422_enumerates_the_codes_the_surface_can_answer(operation: str) -> None:
    """A 422 code only in a description is invisible to every generated consumer.

    #828 review F6: the new deterministic refusal code lived in prose on the 422 that
    every voice-draft operation shares as an unconstrained `UnprocessableEntity`, so
    `speech.generated.ts`, `voice_drafts.openapi.json` and the validators never mentioned
    it -- the same "declared in text, absent from the machine contract" defect this
    change fixed on the Job surface. The enum is now per operation and this test derives
    the expectation from the handler's branches, then checks both directions.
    """
    from speechnexus_backend.speech.interfaces.http.voice_drafts import _ERRORS

    expected = {
        f"voice_draft_{_ERRORS[key][3]}"
        for key, operations in VOICE_DRAFT_422_KEYS.items()
        if operation in operations
    }
    declared = _declared_codes(operation, 422)
    assert declared == expected, {
        "missing": sorted(expected - declared),
        "unreachable": sorted(declared - expected),
    }
    # The specific regression: the elapsed-deadline code must be machine-visible.
    assert "voice_draft_request_expired" in declared
    # Every code in this response really is a 422 the surface answers, and it is
    # non-retryable, so the narrowed headers stay true.
    assert all(_ERRORS[key][0] == 422 for key in VOICE_DRAFT_422_KEYS)
    assert all(_ERRORS[key][4] is False for key in VOICE_DRAFT_422_KEYS)


@pytest.mark.parametrize("operation", VOICE_DRAFT_SUBMIT_OPERATIONS)
def test_voice_draft_422_headers_match_the_deterministic_class(operation: str) -> None:
    contract = response_contract(operation, 422)
    assert contract["headers"]["X-Should-Retry"]["schema"] == {
        "type": "string",
        "const": "false",
    }
    assert contract["headers"]["Cache-Control"]["schema"]["const"] == "no-store"


def test_the_elapsed_deadline_code_reaches_the_generated_consumers() -> None:
    """The machine-visible claim holds for the shipped artifacts, not just the source.

    The contract enum is only half of F6: a consumer reads the generated TypeScript union
    or the bundled validator schema, so the guard has to look at those files too.
    """
    root = Path(__file__).resolve().parents[2]
    consumers = {
        "packages/contracts/src/speech.generated.ts": "voice_draft_request_expired",
        "packages/contracts/src/speechnexus_contracts/voice_drafts.openapi.json": (
            "voice_draft_request_expired"
        ),
        "packages/contracts/src/speechnexus_contracts/voice_drafts-http.schema.json": (
            "voice_draft_request_expired"
        ),
    }
    missing = [
        relative
        for relative, needle in consumers.items()
        if needle not in (root / relative).read_text(encoding="utf-8")
    ]
    assert missing == [], missing
