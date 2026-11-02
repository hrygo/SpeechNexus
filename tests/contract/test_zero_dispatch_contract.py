"""The candidate is a closed read/cancel protocol, never execution authority."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.submission import SubmissionIdentity, SubmitRequest
from tests.contract.test_binding_entrypoints import generator
from tests.support.contracts import document, validator

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "internal/zero-dispatch.schema.json"


def test_bundle_resolves_array_pointer_without_copying_original_constraints() -> None:
    bundle = generator().bundle_contracts(
        {
            "source": SOURCE,
            "version": "1.0.0",
            "format": "json-schema",
            "definitions_prefix": "/$defs/",
            "schemas": ["OriginalExecutionKey"],
        }
    )
    conditions = [
        value
        for name, value in bundle["$defs"].items()
        if name.endswith("execution_control_openapi_yaml_0")
    ]
    assert len(conditions) == 1
    assert conditions[0]["then"]["properties"]["sample_rate_hz"]["enum"] == [16000, 24000, 48000]


@pytest.mark.parametrize("index", ["-1", "01", "2", "-", "٠"])
def test_bundle_rejects_invalid_array_pointer(index: str) -> None:
    with pytest.raises(ValueError, match="Invalid array index"):
        generator().bundle_contracts(
            {
                "source": "internal/execution-control.openapi.yaml",
                "schemas": ["SyntheticCondition"],
                "schema_refs": {
                    "SyntheticCondition": "#/components/schemas/SubmitExecutionRequest/allOf/"
                    + index
                },
            }
        )


def test_identity_reuses_all_original_fields_and_constraints() -> None:
    source = document(ROOT / "contracts" / SOURCE)
    identity = source["$defs"]["SubmissionIdentity"]
    original = document(ROOT / "contracts/internal/execution-control.openapi.yaml")
    submit = original["components"]["schemas"]["SubmitExecutionRequest"]
    names = {field.name for field in fields(SubmissionIdentity)}
    assert len(names) == 19
    assert set(identity["properties"]) == names == set(submit["properties"]) - {"command"}
    assert set(identity["required"]) == set(submit["required"]) - {"command"}
    for name, definition in identity["properties"].items():
        assert definition["$ref"].endswith(
            "execution-control.openapi.yaml#/components/schemas/SubmitExecutionRequest/properties/"
            + name
        )
    assert identity["allOf"] == [
        {"$ref": original_ref}
        for original_ref in (
            (ROOT / "contracts/internal/execution-control.openapi.yaml").as_uri()
            + f"#/components/schemas/SubmitExecutionRequest/allOf/{index}"
            for index in range(len(submit["allOf"]))
        )
    ]


def test_no_positive_authority_in_candidate_profile() -> None:
    source = document(ROOT / "contracts" / SOURCE)
    rules = source["x-nexus-protocol"]
    assert rules["default_enabled"] is False
    assert rules["implemented_writers"] == []
    assert rules["replay_order"] == [
        "verify_original_identity_and_intent",
        "return_exact_original_receipt_if_present",
        "check_current_state_and_version_for_new_action",
    ]
    assert rules["gate_transitions"] == [
        {"cause": "receive_cancel", "from": "open", "to": "cancelled_closed"},
        {"cause": "receive_cancel", "from": "cancelled_closed", "to": "cancelled_closed"},
        {"cause": "receive_cancel", "from": "engine_possible", "to": "engine_possible"},
        {"cause": "receive_cancel", "from": "sealed_zero", "to": "sealed_zero"},
        {"cause": "first_committed_permit", "from": "open", "to": "engine_possible"},
    ]
    manifest = json.loads((ROOT / "contracts/bindings.json").read_text())
    config = manifest["profiles"]["zero_dispatch"]
    assert config["client"] is False
    assert not any("seal" in name.lower() or "permit" in name.lower() for name in config["schemas"])
    assert "paths" not in source


def test_example_digests_reuse_original_native_serializer() -> None:
    examples: list[dict[str, Any]] = json.loads(
        (ROOT / "tests/fixtures/contracts/zero-dispatch.json").read_text()
    )["examples"]
    key = next(e["value"] for e in examples if e["id"] == "zero-original-key")
    existing: list[dict[str, Any]] = json.loads(
        (ROOT / "tests/fixtures/contracts/consumer-profiles.json").read_text()
    )["examples"]
    request = next(
        e["value"] for e in existing if e["schema"] == "SubmitExecutionRequest" and e["valid"]
    )
    identity = dict(key["identity"])
    identity["deadline_at"] = datetime.fromisoformat(identity["deadline_at"])
    native = SubmitRequest(
        identity=SubmissionIdentity(**identity),
        command=request["command"],
        output_stream_id=key["output_stream_id"],
    )
    assert native.request_digest == key["request_digest"]
    assert command_digest(native.command) == native.identity.command_digest
    recorded = next(e["value"] for e in examples if e["id"] == "zero-cancel-recorded")
    replay = next(e["value"] for e in examples if e["id"] == "zero-cancel-replay")
    assert recorded["cancellation"] == replay["cancellation"]
    assert recorded["gate"] == replay["gate"]
    receipt = recorded["cancellation"]
    assert command_digest(receipt["intent"]) == receipt["intent_digest"]


def test_shared_corpus_covers_recovery_replay_conflict_and_untrusted_evidence() -> None:
    examples: list[dict[str, Any]] = json.loads(
        (ROOT / "tests/fixtures/contracts/zero-dispatch.json").read_text()
    )["examples"]
    by_id = {example["id"]: example for example in examples}
    for name in (
        "zero-query-open",
        "zero-missing-gate",
        "zero-incomplete-document",
        "zero-unknown-submit",
        "zero-unknown-object-version",
        "zero-late-cancel",
        "zero-cancel-replay",
        "zero-intent-conflict",
        "zero-version-conflict",
        "zero-reject-epoch-as-authority",
        "zero-reject-resource-closed-bool",
        "zero-reject-seal-write",
        "zero-reject-missing-as-open",
    ):
        assert name in by_id
    for example in examples:
        assert validator(SOURCE, example["schema"]).is_valid(example["value"]) is example["valid"]
