"""The generated collection surface declares atomic publication and retryable shapes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.pronunciation_sets_http import (
    contract,
    validate_request,
    validate_response,
)

from tests.support.contracts import document
from tests.support.pronunciation_examples import pronunciation_request

ROOT = Path(__file__).resolve().parents[2]
PATH = "/v1/nexus/pronunciation-sets"
LIST = "listPronunciationSets"
CREATE = "createPronunciationRevision"
SOURCE = cast(dict[str, Any], document(ROOT / "contracts/speech-openapi.yaml"))
BUNDLE = contract()


def operation(method: str) -> dict[str, Any]:
    return cast(dict[str, Any], BUNDLE["paths"][PATH][method])


def response(method: str, status: str) -> dict[str, Any]:
    node = cast(dict[str, Any], operation(method)["responses"][status])
    while set(node) == {"$ref"}:
        target: Any = BUNDLE
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = cast(dict[str, Any], target)
    return node


def codes(schema: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    node = schema["properties"]["error"]["properties"]["code"]
    if "const" in node:
        found.add(node["const"])
    found.update(node.get("enum", ()))
    return found


def test_collection_surface_owns_exactly_the_two_operations() -> None:
    assert set(BUNDLE["paths"]) == {PATH}
    assert set(BUNDLE["paths"][PATH]) == {"get", "post"}
    assert operation("get")["operationId"] == LIST
    assert operation("post")["operationId"] == CREATE
    assert set(operation("get")["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "405",
        "500",
        "503",
    }
    assert set(operation("post")["responses"]) == {
        "200",
        "201",
        "400",
        "401",
        "403",
        "405",
        "409",
        "422",
        "429",
        "500",
        "503",
    }


def test_neither_operation_keeps_the_central_placeholder() -> None:
    for method in ("get", "post"):
        for status in operation(method)["responses"]:
            assert response(method, status).get("$ref") != (
                "#/components/responses/OperationNotImplemented"
            )


@pytest.mark.parametrize("method", ["get", "post"])
def test_unavailable_is_an_inline_retryable_outage(method: str) -> None:
    unavailable = response(method, "503")
    assert "$ref" not in unavailable
    headers = unavailable["headers"]
    assert headers["Cache-Control"]["required"] is True
    assert headers["Cache-Control"]["schema"]["const"] == "no-store"
    assert headers["X-Should-Retry"]["required"] is True
    assert headers["X-Should-Retry"]["schema"]["const"] == "true"
    variants = unavailable["content"]["application/json"]["schema"]["allOf"]
    assert variants[0] == {"$ref": "#/components/schemas/ErrorResponse"}
    assert codes(variants[1]) == {"pronunciation_unavailable"}
    assert variants[1]["properties"]["error"]["properties"]["retryable"]["const"] is True


def test_error_codes_are_distinct_and_never_hide_the_request_error() -> None:
    for method in ("get", "post"):
        invalid = response(method, "400")["content"]["application/json"]["schema"]
        assert codes(invalid["allOf"][1]) == {"pronunciation_invalid_request"}
        forbidden = response(method, "403")["content"]["application/json"]["schema"]
        assert codes(forbidden["allOf"][1]) == {
            "pronunciation_host_rejected",
            "pronunciation_origin_rejected",
        }
    conflict = response("post", "409")["content"]["application/json"]["schema"]
    assert codes(conflict["allOf"][1]) == {
        "pronunciation_idempotency_conflict",
        "pronunciation_configuration_changed",
        "pronunciation_outcome_unknown",
    }
    unsupported = response("post", "422")["content"]["application/json"]["schema"]
    assert codes(unsupported["allOf"][1]) == {"pronunciation_publication_not_supported"}


def test_publish_is_declared_atomic_by_default() -> None:
    request = cast(dict[str, Any], BUNDLE["components"]["schemas"]["PronunciationCreateRequest"])
    publish = request["properties"]["publish"]
    assert publish["default"] is True
    description = publish["description"].lower()
    assert "atomic" in description and "422" in description


def test_generated_validators_default_to_publication_and_accept_drafts() -> None:
    body = pronunciation_request()
    validate_request(CREATE, {"body": body})
    assert body["publish"] is False
    defaulted = {key: value for key, value in body.items() if key != "publish"}
    validate_request(CREATE, {"body": defaulted})
    with pytest.raises(ContractValidationError):
        validate_request(CREATE, {"body": {**body, "publish": "yes"}})
    summary: dict[str, Any] = {
        "id": "pron-aaa",
        "head_revision": "1",
        "encoding": "pinyin",
        "entry_count": 2,
        "status": "draft",
    }
    page: dict[str, Any] = {
        "object": "nexus.list",
        "data": [summary],
        "has_more": False,
        "next_cursor": None,
    }
    validate_response(LIST, 200, {"body": page})
    response_body = {
        "encoding": body["encoding"],
        "entries": body["entries"],
        "resource_type": "pronunciation_revision",
        "id": "pron-aaa",
        "revision": "1",
        "object": "nexus.pronunciation_revision",
        "status": "draft",
        "created_at": "2026-01-01T00:00:00.000000Z",
    }
    validate_response(CREATE, 201, {"body": response_body})
    validate_response(CREATE, 200, {"body": {**response_body, "status": "published"}})
    with pytest.raises(ContractValidationError):
        validate_response(CREATE, 201, {"body": {**response_body, "id": "Not An Id"}})
    for broken in ({**summary, "entry_count": 0}, {**summary, "encoding": "kana"}):
        with pytest.raises(ContractValidationError):
            validate_response(LIST, 200, {"body": {**page, "data": [broken]}})


def test_source_contract_declares_the_same_statuses() -> None:
    published = SOURCE["paths"][PATH]
    assert set(published["get"]["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "405",
        "500",
        "503",
    }
    assert set(published["post"]["responses"]) == {
        "200",
        "201",
        "400",
        "401",
        "403",
        "405",
        "409",
        "422",
        "429",
        "500",
        "503",
    }
