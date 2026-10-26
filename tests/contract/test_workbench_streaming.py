"""Canonical Workbench streaming projection and bounded event consumption."""

import copy
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench_http import (
    iter_events,
    validate_event,
    validate_request,
    validate_response,
)

from tests.contract.test_binding_entrypoints import generator
from tests.contract.test_workbench_http_bindings import valid_schema

CORPUS = json.loads(
    (Path(__file__).parents[1] / "fixtures/contracts/workbench-sse.json").read_text()
)


@pytest.mark.parametrize("event", CORPUS["events"], ids=lambda e: e["type"])
def test_export_event_matches_independent_source_and_is_not_mutated(event: Any) -> None:
    before = copy.deepcopy(event)
    assert valid_schema({"$ref": "#/components/schemas/ExportEvent"}, event)
    assert validate_event("streamExportEvents", event) is event
    assert event == before


@pytest.mark.parametrize("example", CORPUS["streams"], ids=lambda e: e["id"])
@pytest.mark.parametrize("chunk_size", [1, 7, 4096])
def test_shared_bounded_sse_sequences(example: Any, chunk_size: int) -> None:
    wire = example["wire"].encode() if example["wire_kind"] == "utf8" else bytes(example["wire"])
    closed = []

    def chunks() -> Any:
        try:
            for start in range(0, len(wire), chunk_size):
                yield wire[start : start + chunk_size]
        finally:
            closed.append(True)

    events = iter_events(
        "streamExportEvents",
        chunks(),
        example["request"],
        max_event_bytes=example["max_event_bytes"],
    )
    actual: list[Any] = []
    if example["valid"]:
        actual.extend(events)
    else:
        with pytest.raises(ContractValidationError, match=r"^Invalid SSE stream$"):
            actual.extend(events)
    assert actual == example["expected"]
    assert closed == [True]


def test_sse_metadata_validation_does_not_consume_the_caller_stream() -> None:
    def unread() -> Generator[bytes]:
        yield from ()
        raise AssertionError("Metadata validation must not read payload")

    chunks = unread()
    envelope = {"body": chunks, "headers": {"Cache-Control": "no-cache"}}
    assert validate_response("streamExportEvents", 200, envelope) is envelope
    chunks.close()


def test_book_export_download_uses_the_existing_binary_contract() -> None:
    request = {"path": {"export_id": "export-demo"}, "headers": {"Range": "bytes=0-3"}}
    assert validate_request("downloadBookExport", request) is request
    response = {"body": b"\x00\xff"}
    assert validate_response("downloadBookExport", 206, response) is response


def test_generator_projects_the_canonical_sse_event_reference() -> None:
    module = generator()
    document = module.bundle_http_surface(
        {
            "source": "workbench-openapi.yaml",
            "paths": ["/v1/workbench/exports/{export_id}/events"],
        }
    )
    bundle = module.bundle_http_validation(document)
    event = bundle["x-nexus-http-operations"]["streamExportEvents"]["responses"]["200"]["event"]
    assert bundle["$defs"][event] == {"$ref": "#/$defs/ExportEvent"}


@pytest.mark.parametrize("constraint", ["body", "event"])
def test_sse_projection_rejects_constraints_it_cannot_enforce(constraint: str) -> None:
    module = generator()
    document = module.bundle_http_surface(
        {
            "source": "workbench-openapi.yaml",
            "paths": ["/v1/workbench/exports/{export_id}/events"],
        }
    )
    media = document["paths"]["/v1/workbench/exports/{export_id}/events"]["get"]["responses"][
        "200"
    ]["content"]["text/event-stream"]
    if constraint == "body":
        media["schema"]["maxLength"] = 1
    else:
        media["x-sse-event-schema"]["additionalProperties"] = False
    with pytest.raises(ValueError, match="Unsupported validated HTTP body representation"):
        module.bundle_http_validation(document)
