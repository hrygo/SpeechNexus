"""The retirement write surface carries one command and the shared error envelope."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from openapi_spec_validator import validate
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.voice_retirement_http import validate_request, validate_response

from tests.contract.test_binding_entrypoints import generator

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "contracts/bindings.json").read_text())["http_surfaces"][
    "voice_retirement"
]
PATH = "/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements"
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/voice-catalogue-http.json").read_text())[
    "examples"
]


def _example(example_id: str) -> dict[str, Any]:
    return copy.deepcopy(
        next(example["value"] for example in EXAMPLES if example["id"] == example_id)
    )


def test_only_the_retirement_command_is_selected() -> None:
    from speechnexus_contracts.voice_retirement_http import contract

    assert set(CONFIG["operations"]) == {"retireVoiceRevision"}
    assert CONFIG["paths"] == [PATH]
    assert set(contract()["paths"]) == {PATH}


def test_generated_surface_matches_the_manifest() -> None:
    from speechnexus_contracts.voice_retirement_http import contract

    bundled = generator().bundle_http_surface(CONFIG)
    assert contract() == bundled
    validate(bundled)
    assert set(bundled["paths"]) == {PATH}
    assert set(bundled["paths"][PATH]) == {"post"}
    assert bundled["paths"][PATH]["post"]["operationId"] == "retireVoiceRevision"


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "retire"},
        {"mode": "retire", "reason": "sunset"},
        {"mode": "disable", "reason": "emergency"},
    ],
)
def test_request_body_accepts_the_declared_modes(body: dict[str, Any]) -> None:
    envelope = {
        "path": {"voice_id": "voice-demo-1", "revision": "2"},
        "headers": {"x-request-id": "req-retirement", "idempotency-key": "key-00000001"},
        "body": body,
    }
    assert validate_request("retireVoiceRevision", envelope) is envelope


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "retirex"},
        {"mode": "disable"},
        {"mode": "retire", "reason": "x" * 2001},
        {"mode": "retire", "unknown": True},
    ],
)
def test_request_body_rejects_undeclared_shapes(body: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        validate_request(
            "retireVoiceRevision",
            {"path": {"voice_id": "voice-demo-1", "revision": "2"}, "body": body},
        )


@pytest.mark.parametrize("example_id", ["published", "retired", "disabled-reason-2000"])
def test_success_response_is_a_voice_revision(example_id: str) -> None:
    value = _example(example_id)
    assert validate_response("retireVoiceRevision", 200, value) is value


def test_disable_rejection_is_the_shared_error_envelope() -> None:
    body = _example("getVoiceRevision-invalid-request")["body"]
    body["error"]["code"] = "voice_disable_unsupported"
    body["error"]["message"] = (
        "This deployment cannot cancel in-flight use, so disable is unsupported."
    )
    body["error"]["retryable"] = False
    envelope = {"body": body}
    assert validate_response("retireVoiceRevision", 422, envelope) is envelope


def test_unavailable_is_retryable_and_no_longer_pins_the_placeholder() -> None:
    body = _example("getVoiceRevision-invalid-request")["body"]
    body["error"]["code"] = "voice_retirement_unavailable"
    body["error"]["type"] = "service_unavailable_error"
    body["error"]["retryable"] = True
    envelope = {
        "body": body,
        "headers": {"Cache-Control": "no-store", "X-Should-Retry": "true"},
    }
    # The placeholder component pinned retryable=false; the implemented
    # operation now declares the generic retryable ServiceUnavailable body.
    assert validate_response("retireVoiceRevision", 503, envelope) is envelope


def test_retirement_is_not_part_of_the_read_only_catalogue_surface() -> None:
    from speechnexus_contracts.voice_catalogue_http import validate_request as read_request

    with pytest.raises(ValueError, match="Unknown HTTP operation"):
        read_request("retireVoiceRevision", {})
