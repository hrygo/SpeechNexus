"""Native binary envelopes share the original source, without publishing C routes."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench_http import validate_request, validate_response

from tests.contract.test_binding_entrypoints import generator
from tests.contract.test_workbench_http_bindings import SOURCE, resolve, valid_schema

EXAMPLES = json.loads(
    (Path(__file__).parents[1] / "fixtures/contracts/workbench-binary-http.json").read_text()
)["examples"]


def source_accepts(example: dict[str, Any], value: dict[str, Any]) -> bool:
    path = (
        "/v1/workbench/exports/{export_id}/content"
        if example["operation"] == "downloadBookExport"
        else "/v1/workbench/saved-assets/{asset_id}/content"
    )
    operation = SOURCE["paths"][path]["get"]
    raw = operation["responses"].get(str(example["status"]))
    if raw is None or "body" not in value:
        return False
    response = resolve(raw)
    actual_headers = {k.lower(): v for k, v in value["headers"].items()}
    for name, header in response.get("headers", {}).items():
        definition = resolve(header)
        if name.lower() in actual_headers:
            if not valid_schema(definition["schema"], actual_headers[name.lower()]):
                return False
        elif definition.get("required"):
            return False
    content = response["content"]
    if "application/octet-stream" in content:
        return isinstance(value["body"], bytes) and valid_schema(
            content["application/octet-stream"]["schema"], ""
        )
    return valid_schema(content["application/json"]["schema"], value["body"])


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda x: x["id"])
def test_native_binary_response_agrees_with_source_and_keeps_input(example: dict[str, Any]) -> None:
    value = copy.deepcopy(example["value"])
    if example["body_kind"] == "bytes":
        value["body"] = bytes(value["body"])
    before = copy.deepcopy(value)
    assert source_accepts(example, value) is example["valid"]
    if example["valid"]:
        assert validate_response(example["operation"], example["status"], value) is value
    else:
        with pytest.raises(ContractValidationError, match=r"^Invalid HTTP response$"):
            validate_response(example["operation"], example["status"], value)
    assert value == before


def test_saved_asset_binary_entry_accepts_original_range_and_native_bytes() -> None:
    request = {"path": {"asset_id": "asset-demo"}, "headers": {"Range": "bytes=0-3"}}
    assert validate_request("downloadSavedAsset", request) is request
    reply = {"headers": {"Content-Range": "bytes 0-3/8"}, "body": b"test"}
    assert validate_response("downloadSavedAsset", 206, reply) is reply


@pytest.mark.parametrize("change", ["extra_binary_constraint", "binary_ref_constraint", "sse"])
def test_generator_rejects_unproved_binary_constraints_and_other_media(change: str) -> None:
    from speechnexus_contracts.workbench_http import contract

    document = contract()
    response = document["paths"]["/v1/workbench/saved-assets/{asset_id}/content"]["get"][
        "responses"
    ]["200"]
    if change == "extra_binary_constraint":
        response["content"]["application/octet-stream"]["schema"]["maxLength"] = 1
    elif change == "binary_ref_constraint":
        document["components"]["schemas"]["SyntheticBinary"] = {
            "type": "string",
            "format": "binary",
        }
        response["content"]["application/octet-stream"]["schema"] = {
            "$ref": "#/components/schemas/SyntheticBinary",
            "maxLength": 1,
        }
    else:
        response["content"] = {"text/event-stream": {"schema": {"type": "string"}}}
    with pytest.raises(ValueError, match="Unsupported validated HTTP body representation"):
        generator().bundle_http_validation(document)
