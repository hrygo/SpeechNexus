"""Source-valid immutable intent, lossless private JSON and original-key boundaries."""

import copy
import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse as parse_admin
from speechnexus_contracts.model_bindings import parse as parse_binding

from speechnexus_backend.speech.application.binding_documents import (
    check_command,
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingDraft,
    BindingDraftResult,
)
from speechnexus_backend.speech.interfaces.binding_drafts import (
    draft_to_wire,
    prepare_binding_draft,
)
from tests.support.binding_examples import binding_request


def test_original_complete_intent_and_key_are_stable_without_defaults_or_allocation() -> None:
    request = binding_request()
    request["execution_recipe"].pop("requires_complete_reservation", None)
    original = copy.deepcopy(request)
    first = prepare_binding_draft(request, "same-original-key")
    other_order = {key: request[key] for key in reversed(request)}
    again = prepare_binding_draft(other_order, "same-original-key")
    assert first == again and first.request_digest == again.request_digest
    assert request == original
    assert (
        "requires_complete_reservation" not in decode_document(first.definition)["execution_recipe"]
    )
    assert "revision_id" not in decode_document(first.intent)
    check_command(first)
    changed = copy.deepcopy(request)
    changed["execution_recipe"]["requires_complete_reservation"] = True
    assert (
        prepare_binding_draft(changed, "same-original-key").request_digest != first.request_digest
    )
    with pytest.raises(FrozenInstanceError):
        first.binding_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "size",
    [9007199254740992, 9007199254740993, 10**100, 10**5000],
    ids=["above-safe", "inexact-if-float", "hundred-digits", "thousands-digits"],
)
def test_source_allowed_large_integers_round_trip_without_float_loss(size: int) -> None:
    request = binding_request()
    request["artifact_set"][0]["size_bytes"] = size
    parse_admin("ModelBindingCreateRequest", request)
    command = prepare_binding_draft(request, "large-original-key")
    assert json.loads(command.intent, parse_int=Decimal)["artifact_set"][0]["size_bytes"] == size
    assert decode_document(command.definition)["artifact_set"][0]["size_bytes"] == size
    wire = draft_to_wire(
        BindingDraft(command.binding_id, "1", "binding-revision-original", command.definition, 0)
    )
    assert wire["artifact_set"][0]["size_bytes"] == size
    parse_binding("ModelBindingRevision", wire)


def test_private_canonical_json_preserves_types_unicode_and_array_order() -> None:
    assert encode_document({"n": 1.0, "b": True}) == b'{"b":true,"n":1}'
    assert encode_document({"n": True}) != encode_document({"n": 1})
    assert encode_document({"n": False}) != encode_document({"n": 0})
    assert encode_document({"n": -0.0}) == encode_document({"n": 0})
    assert encode_document({"s": "e\u0301"}) != encode_document({"s": "\u00e9"})
    assert encode_document({"items": [1, 2]}) != encode_document({"items": [2, 1]})
    assert encode_document({"n": 10000000000.0}) == encode_document({"n": 10000000000})


@pytest.mark.parametrize(
    "raw", [b'{"a":1.0}', b'{"a":1,"a":1}', b' {"a":1}', b'{"a":NaN}', b"[]", b"{}" * 140000]
)
def test_noncanonical_corrupt_or_oversized_stored_documents_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode_document(raw)


@pytest.mark.parametrize(
    "value",
    [{"n": float("nan")}, {"n": float("inf")}, {"s": "\ud800"}, {1: "not-json"}, {"items": (1, 2)}],
)
def test_non_json_values_are_rejected_without_normalizing_them(value: Any) -> None:
    with pytest.raises(ValueError):
        encode_document(value)


def test_bounded_depth_fails_safely() -> None:
    value: Any = 0
    for _ in range(70):
        value = [value]
    with pytest.raises(ValueError):
        encode_document({"value": value})


def test_full_source_conditionals_are_enforced_beyond_the_request_dto() -> None:
    request = binding_request()
    request["tasks"], request["engine"]["kind"] = ["voice_design"], "vllm"
    parse_admin("ModelBindingCreateRequest", request)
    with pytest.raises(ContractValidationError, match="Invalid binding draft intent"):
        prepare_binding_draft(request, "original-key")


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_latest_revision": None},
        {"revision_id": "client-id"},
        {"publication": {"status": "draft"}},
        {"supersedes_revision": None},
    ],
)
def test_no_client_cas_identity_or_publication_fields(changes: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        prepare_binding_draft({**binding_request(), **changes}, "original-key")


def test_supersedes_is_retained_without_becoming_a_cas_guard() -> None:
    request = {**binding_request(), "supersedes_revision": "9999999999999999999"}
    command = prepare_binding_draft(request, "original-key")
    assert decode_document(command.intent)["supersedes_revision"] == request["supersedes_revision"]
    assert (
        decode_document(command.definition)["supersedes_revision"] == request["supersedes_revision"]
    )


def test_key_identity_spans_families_and_keyless_preparation_is_new_identity() -> None:
    request = binding_request()
    first = prepare_binding_draft(request, "original-key")
    changed = prepare_binding_draft({**request, "binding_id": "another-family"}, "original-key")
    assert changed.key_digest == first.key_digest and changed.request_digest != first.request_digest
    assert prepare_binding_draft(request).key_digest != prepare_binding_draft(request).key_digest
    with pytest.raises(ValueError):
        check_command(replace(first, binding_id="different"))


def test_wire_revision_global_identity_and_time_come_from_the_stored_record() -> None:
    command = prepare_binding_draft(binding_request(), "original-key")
    record = BindingDraft(
        command.binding_id,
        "9999999999999999999",
        "binding-original-revision-id",
        command.definition,
        1700000000123456,
    )
    wire = draft_to_wire(record)
    assert wire["revision"] == record.revision and wire["revision_id"] == record.revision_id
    assert wire["created_at"].endswith(".123456Z")
    assert wire["publication"]["status"] == "draft"
    assert wire["publication"]["qualification_report_id"] is None
    parse_binding("ModelBindingRevision", wire)
    assert BindingDraftResult("unknown").recovery == "query_original"
    assert BindingDraftResult("created", record).recovery == "none"


@pytest.mark.parametrize("key", ["short", "x" * 129, 123])
def test_invalid_original_keys_are_rejected(key: Any) -> None:
    with pytest.raises(ContractValidationError):
        prepare_binding_draft(binding_request(), key)
