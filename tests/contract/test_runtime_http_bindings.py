"""The existing controller source owns both language HTTP validators."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from openapi_spec_validator import validate
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.runtime import parse
from speechnexus_contracts.runtime_http import contract, validate_request, validate_response

from tests.contract.test_binding_entrypoints import generator

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/runtime-http.json").read_text())["examples"]


def test_runtime_http_surface_is_generated_from_the_original_two_routes() -> None:
    manifest = json.loads((ROOT / "contracts/bindings.json").read_text())
    source = generator().bundle_http_surface(manifest["http_surfaces"]["runtime"])
    assert contract() == source
    assert len(source["paths"]) == 2
    validate(source)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example["id"])
def test_runtime_http_parsers_accept_the_same_unchanged_examples(example: dict[str, Any]) -> None:
    value = copy.deepcopy(example["value"])
    before = copy.deepcopy(value)

    def check() -> Any:
        if example["kind"] == "request":
            return validate_request(example["operation"], value)
        return validate_response(example["operation"], example["status"], value)

    if example["valid"]:
        assert check() is value
        if example.get("status", 200) != 200:
            assert parse("RuntimeControlErrorResponse", value["body"]) is value["body"]
    else:
        with pytest.raises(ContractValidationError):
            check()
    assert value == before
