"""Executable acceptance examples for a proposed contract, never a production validator."""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from openapi_spec_validator import validate

from speechnexus_backend.workbench.application.transcript_exports import TranscriptTextExports
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from tests.contract.test_p0_public_contracts import (
    _component_validator,
    _resolve_local_refs,
    _resolved_yaml,
    _validator,
)
from tests.fake.workbench_versions import MemoryVersionUnitOfWork

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "contracts/workbench-openapi.yaml"
CANDIDATE = ROOT / "docs/plans/2026-10-24-team-c-c02-download-contract-candidate.yaml"
PATH = "/v1/workbench/transcripts/{transcript_id}/revisions/{revision}/content"


def proposed() -> dict[str, Any]:
    candidate = yaml.safe_load(CANDIDATE.read_text())
    assert candidate["status"] == "proposed" and candidate["review_required"] == ["A"]
    assert candidate["canonical_contract"] == "contracts/workbench-openapi.yaml"
    document = _resolved_yaml(SOURCE)
    patch = copy.deepcopy(candidate["patch"])
    _resolve_local_refs(patch, SOURCE)
    for path, value in patch["paths"].items():
        assert path not in document["paths"]
        document["paths"][path] = value
    for kind, definitions in patch["components"].items():
        assert not set(definitions) & document["components"][kind].keys()
        document["components"][kind].update(definitions)
    return document


def success(text: str) -> tuple[dict[str, Any], bytes]:
    body = text.encode()
    return {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Length": len(body),
        "Content-Disposition": 'attachment; filename="speech-transcript.txt"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Content-SHA256": "sha256:" + hashlib.sha256(body).hexdigest(),
        "X-Request-ID": "synthetic-request",
    }, body


def accepted(document: dict[str, Any], headers: dict[str, Any], body: bytes) -> None:
    # Header values here are normalized according to OpenAPI types, not raw HTTP serialization.
    response = document["paths"][PATH]["get"]["responses"]["200"]
    declared = response["headers"]
    schema = {
        "type": "object",
        "required": [name for name, value in declared.items() if value.get("required")],
        "properties": {name: value["schema"] for name, value in declared.items()},
        "components": document["components"],
    }
    _validator(schema).validate(headers)
    assert "Content-Type" in headers and "Content-Encoding" not in headers
    media = Message()
    media["Content-Type"] = headers["Content-Type"]
    assert media.get_content_type() == "text/plain"
    assert media.get_content_charset() == "utf-8"
    assert sum(name == "charset" for name, _ in (media.get_params() or [])) == 1
    assert len(body) == headers["Content-Length"]
    assert "sha256:" + hashlib.sha256(body).hexdigest() == headers["X-Content-SHA256"]
    content = next(iter(response["content"].values()))["schema"]
    _validator({**content, "components": document["components"]}).validate(body.decode("utf-8"))


def test_candidate_merges_additively_and_validates_as_openapi_without_changing_canonical() -> None:
    before = SOURCE.read_bytes()
    original = _resolved_yaml(SOURCE)
    document = proposed()
    validate(document)
    assert set(document["paths"]) - set(original["paths"]) == {PATH}
    for path, value in original["paths"].items():
        assert document["paths"][path] == value
    assert SOURCE.read_bytes() == before


@pytest.mark.parametrize("text", ["", "\ufeff合成\r\n😀e\u0301\n", "😀" * 1_000_000])
async def test_existing_fixed_revision_core_satisfies_candidate_examples(text: str) -> None:
    store = MemoryVersionUnitOfWork(now=datetime(2026, 10, 24, tzinfo=UTC))
    app = WorkbenchVersions(store)
    project = await app.create_project(workspace_id="workspace-one", name="合成")
    transcript = await app.create_manual_transcript(
        project_id=project.id, display_name="合成", text="原文"
    )
    await app.revise_transcript(transcript_id=transcript.id, expected_revision=1, text=text)
    result = await TranscriptTextExports(app).export_revision(transcript.id, 2)
    headers, body = success(text)
    assert result.content == body and result.sha256 == headers["X-Content-SHA256"]
    accepted(proposed(), headers, body)


@pytest.mark.parametrize(
    "name",
    [
        "Content-Length",
        "Content-Disposition",
        "Cache-Control",
        "X-Content-Type-Options",
        "X-Content-SHA256",
        "X-Request-ID",
    ],
)
def test_success_requires_each_declared_header(name: str) -> None:
    headers, body = success("合成")
    del headers[name]
    with pytest.raises(jsonschema.ValidationError):
        accepted(proposed(), headers, body)


@pytest.mark.parametrize(
    "name,value",
    [
        ("Content-Length", -1),
        ("Content-Length", 4_000_001),
        ("Content-Length", True),
        ("Content-Disposition", 'attachment; filename="../private.txt"'),
        ("Cache-Control", "public"),
        ("X-Content-Type-Options", ""),
        ("X-Content-SHA256", "bad"),
        ("X-Request-ID", ""),
        ("X-Request-ID", "x" * 129),
    ],
)
def test_invalid_headers_are_rejected(name: str, value: Any) -> None:
    headers, body = success("合成")
    headers[name] = value
    with pytest.raises(jsonschema.ValidationError):
        accepted(proposed(), headers, body)


@pytest.mark.parametrize("change", ["bytes", "digest", "utf8", "nul", "characters"])
def test_content_corruption_and_bounds_are_not_success(change: str) -> None:
    headers, body = success("合成")
    if change == "bytes":
        headers["Content-Length"] += 1
    elif change == "digest":
        headers["X-Content-SHA256"] = "sha256:" + "0" * 64
    else:
        body = {"utf8": b"\xff", "nul": b"\x00", "characters": b"x" * 1_000_001}[change]
        headers["Content-Length"] = len(body)
        headers["X-Content-SHA256"] = "sha256:" + hashlib.sha256(body).hexdigest()
    with pytest.raises((AssertionError, UnicodeDecodeError, jsonschema.ValidationError)):
        accepted(proposed(), headers, body)


@pytest.mark.parametrize(
    "name,value,valid",
    [
        ("Identifier", "transcript-one", True),
        ("Identifier", "../private", False),
        ("Revision", "9007199254740993", True),
        ("Revision", "9999999999999999999", True),
        ("Revision", "01", False),
        ("Revision", "0", False),
        ("Revision", 1, False),
        ("Revision", True, False),
    ],
)
def test_path_reuses_shared_opaque_identifier_and_decimal_revision(
    name: str, value: Any, valid: bool
) -> None:
    assert _component_validator(proposed(), name).is_valid(value) is valid


def test_operation_declares_full_text_and_existing_error_envelopes_without_job_mutations() -> None:
    operation = proposed()["paths"][PATH]["get"]
    assert "requestBody" not in operation
    assert {"200", "400", "401", "404", "503"} == operation["responses"].keys()
    assert operation["operationId"] == "downloadTranscriptRevisionText"
    assert list(operation["responses"]["200"]["content"]) == ["text/plain; charset=utf-8"]
    assert "Accept-Ranges" not in operation["responses"]["200"]["headers"]
    assert [p["$ref"].split("/")[-1] for p in operation["parameters"]] == [
        "TranscriptIdPath",
        "RevisionPath",
        "XRequestId",
    ]
    for code, response in {
        "400": "BadRequest",
        "401": "Unauthorized",
        "404": "NotFound",
        "503": "ServiceUnavailable",
    }.items():
        assert operation["responses"][code]["$ref"] == "#/components/responses/" + response


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html; charset=utf-8",
        "text/plain",
        "text/plain; charset=ascii",
        "text/plain; charset=utf-8; charset=ascii",
    ],
)
def test_success_requires_one_explicit_utf8_plain_text_media_type(content_type: str) -> None:
    headers, body = success("合成")
    headers["Content-Type"] = content_type
    with pytest.raises(AssertionError):
        accepted(proposed(), headers, body)


def test_content_encoding_and_missing_media_type_cannot_bypass_raw_byte_semantics() -> None:
    headers, body = success("合成")
    headers["Content-Encoding"] = "gzip"
    with pytest.raises(AssertionError):
        accepted(proposed(), headers, body)
    del headers["Content-Encoding"]
    del headers["Content-Type"]
    with pytest.raises(AssertionError):
        accepted(proposed(), headers, body)


def test_media_type_names_and_charset_are_case_insensitive() -> None:
    headers, body = success("合成")
    headers["Content-Type"] = "TEXT/PLAIN; CHARSET=UTF-8"
    accepted(proposed(), headers, body)


@pytest.mark.parametrize("status", ["400", "401", "404", "503"])
def test_proposed_errors_use_shared_envelope_and_status_specific_semantics(status: str) -> None:
    semantics = proposed()["paths"][PATH]["get"]["x-nexus-error-semantics"][status]
    error = {
        **semantics,
        "message": "Synthetic safe error",
        "param": None,
        "request_id": "synthetic",
    }
    schema = {
        "allOf": [
            {"$ref": "#/components/schemas/ErrorResponse"},
            {
                "properties": {
                    "error": {
                        "properties": {name: {"const": value} for name, value in semantics.items()}
                    }
                }
            },
        ],
        "components": proposed()["components"],
    }
    validator = _validator(schema)
    validator.validate({"error": error})
    assert semantics["retryable"] is (status == "503")
    changes: list[dict[str, Any]] = [
        {"code": "other_code"},
        {"type": "api_error"},
        {"retryable": not semantics["retryable"]},
        {"internal_endpoint": "synthetic-private"},
    ]
    for change in changes:
        with pytest.raises(jsonschema.ValidationError):
            validator.validate({"error": {**error, **change}})


def test_error_semantics_survive_applying_only_the_openapi_patch() -> None:
    operation = proposed()["paths"][PATH]["get"]
    assert set(operation["x-nexus-error-semantics"]) == {"400", "401", "404", "503"}
