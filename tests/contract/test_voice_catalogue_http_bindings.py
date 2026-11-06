"""The selected catalogue reads preserve the independently frozen public source."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.voice_catalogue_http import contract, validate_request, validate_response
from speechnexus_contracts.workbench_http import validate_response as validate_workbench_response

from tests.contract.test_binding_entrypoints import generator
from tests.support.contracts import document, retrieve

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/voice-catalogue-http.json").read_text())[
    "examples"
]
CONFIG = json.loads((ROOT / "contracts/bindings.json").read_text())["http_surfaces"][
    "voice_catalogue"
]
SOURCE = document(ROOT / "contracts/speech-openapi.yaml")


def test_only_two_read_operations_are_selected_without_publication_authority() -> None:
    actual = contract()
    assert actual == generator().bundle_http_surface(CONFIG)
    validate(actual)
    assert set(actual["paths"]) == set(CONFIG["paths"])
    assert {item["get"]["operationId"] for item in actual["paths"].values()} == {
        "listVoices",
        "getVoiceRevision",
    }
    assert all(set(item) == {"get"} for item in actual["paths"].values())
    actual["paths"].clear()
    assert len(contract()["paths"]) == 2


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example["id"])
def test_shared_examples_match_source_and_generated_validator(example: dict[str, Any]) -> None:
    operation = next(
        item["get"]
        for item in SOURCE["paths"].values()
        if item.get("get", {}).get("operationId") == example["operation"]
    )
    response = operation["responses"][str(example["status"])]
    while "$ref" in response:
        response = SOURCE["components"]["responses"][response["$ref"].rsplit("/", 1)[1]]
    source_validator = Draft202012Validator(
        {"components": SOURCE["components"], **response["content"]["application/json"]["schema"]},
        registry=cast(Any, Registry)(retrieve=retrieve),
        format_checker=FormatChecker(),
    )
    source_valid = source_validator.is_valid(example["value"]["body"])
    headers = {name.lower(): value for name, value in example["value"].get("headers", {}).items()}
    for name, header in response.get("headers", {}).items():
        if header.get("required") and name.lower() not in headers:
            source_valid = False
        if name.lower() in headers and "schema" in header:
            source_valid = source_valid and source_validator.evolve(
                schema=header["schema"]
            ).is_valid(headers[name.lower()])
    assert source_valid is example["valid"]
    value = copy.deepcopy(example["value"])
    if example["valid"]:
        assert validate_response(example["operation"], example["status"], value) is value
    else:
        with pytest.raises(ContractValidationError):
            validate_response(example["operation"], example["status"], value)
    assert value == example["value"]


def test_read_requests_keep_defaults_omitted_and_reject_unknown_fields() -> None:
    value = {"query": {"workspace_id": "workspace-synthetic"}}
    assert validate_request("listVoices", value) is value
    assert value == {"query": {"workspace_id": "workspace-synthetic"}}
    assert validate_request(
        "getVoiceRevision", {"path": {"voice_id": "voice-demo-1", "revision": "2"}}
    )
    with pytest.raises(ContractValidationError):
        validate_request("listVoices", {"query": {"private_endpoint": "synthetic"}})


def test_workbench_v2_preserves_long_reasons_while_v1_stays_frozen() -> None:
    public = next(
        item["value"]["body"] for item in EXAMPLES if item["id"] == "disabled-reason-2000"
    )
    projection = {
        key: value
        for key, value in public.items()
        if key
        in {
            "voice_id",
            "revision",
            "display_name",
            "status",
            "model_binding",
            "published_at",
            "retired_at",
            "disabled_reason",
        }
    } | {key: public["capabilities"][key] for key in ("languages", "audio_formats")}
    envelope = {"body": projection}
    assert validate_workbench_response("getVoiceRevisionV2", 200, envelope) is envelope
    with pytest.raises(ContractValidationError):
        validate_workbench_response("getVoiceRevision", 200, envelope)
    projection["disabled_reason"] = "停" * 128
    assert validate_workbench_response("getVoiceRevision", 200, envelope) is envelope
    projection["disabled_reason"] = "停" * 2001
    with pytest.raises(ContractValidationError):
        validate_workbench_response("getVoiceRevisionV2", 200, envelope)


@pytest.mark.parametrize(
    "operation", ["retireVoiceRevision", "publishVoiceDraft", "createVoiceDraft"]
)
def test_writes_are_outside_the_read_surface(operation: str) -> None:
    with pytest.raises(ValueError, match="Unknown HTTP operation"):
        validate_request(operation, {})


@pytest.mark.parametrize("operation", ["listVoices", "getVoiceRevision"])
@pytest.mark.parametrize("header", ["Cache-Control", "X-Should-Retry"])
def test_invalid_request_requires_non_retry_headers(operation: str, header: str) -> None:
    value = copy.deepcopy(
        next(item["value"] for item in EXAMPLES if item["id"] == operation + "-invalid-request")
    )
    del value["headers"][header]
    with pytest.raises(ContractValidationError):
        validate_response(operation, 400, value)
