"""Canonical intent and producer semantics, without a database or qualification."""

import json
from dataclasses import replace
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.profile_documents import (
    check_command,
    decode_document,
)
from speechnexus_backend.speech.domain.profile_drafts import (
    ProfileDraft,
    ProfileDraftResult,
    ProfileUnavailableError,
    revision_number,
)
from speechnexus_backend.speech.infrastructure.profile_drafts_postgres import PostgresProfileDrafts
from speechnexus_backend.speech.interfaces.profile_drafts import (
    draft_to_wire,
    prepare_profile_draft,
)
from tests.support.profile_examples import profile_request


def test_canonical_numbers_property_order_unicode_and_input_immutability() -> None:
    request = profile_request()
    before = json.dumps(request)
    first = prepare_profile_draft(request, "original-key")
    reordered = dict(reversed(list(request.items())))
    reordered["parameters"]["asr"]["preview_interval_ms"] = 500.0
    second = prepare_profile_draft(reordered, "original-key")
    assert first == second
    assert decode_document(first.intent)["parameters"]["asr"]["preview_interval_ms"] == 500
    request["parameters"]["asr"]["preview_interval_ms"] = 500
    assert json.dumps(request) == before
    check_command(first)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_latest_revision", 1),
        ("expected_latest_revision", "0"),
        ("publication", {"status": "published"}),
        ("kind", "invalid"),
    ],
)
def test_source_contract_rejects_invalid_or_producer_owned_fields(field: str, value: Any) -> None:
    request = profile_request()
    request[field] = value
    with pytest.raises(ContractValidationError, match=r"^Invalid profile draft intent$"):
        prepare_profile_draft(request, "original-key")


@pytest.mark.parametrize("case", ["range", "unlisted", "capability", "segment", "nan"])
def test_source_described_cross_field_rules_reject_before_any_transaction(case: str) -> None:
    request = profile_request()
    if case == "range":
        request["override_ranges"]["asr.preview_interval_ms"] = {"minimum": 1000, "maximum": 300}
    elif case == "unlisted":
        request["override_allowlist"] = []
    elif case == "capability":
        cap = dict(request["required_capabilities"][0])
        cap["required"] = False
        request["required_capabilities"].append(cap)
    elif case == "segment":
        request["parameters"]["asr"]["max_segment_ms"] = 300
    else:
        request["override_ranges"]["asr.preview_interval_ms"]["minimum"] = float("nan")
    with pytest.raises(ContractValidationError, match=r"^Invalid profile draft intent$"):
        prepare_profile_draft(request)


def test_keyless_calls_are_distinct_and_retained_commands_are_recoverable() -> None:
    first, second = (prepare_profile_draft(profile_request()) for _ in range(2))
    assert first.intent == second.intent and first.key_digest != second.key_digest
    assert ProfileDraftResult("unknown").recovery == "query_original"


@pytest.mark.parametrize("key", ["", "short", "x" * 129, 10])
def test_idempotency_header_limits(key: Any) -> None:
    with pytest.raises(ContractValidationError):
        prepare_profile_draft(profile_request(), key)


def test_full_decimal_revision_draft_projection_and_private_command_consistency() -> None:
    command = prepare_profile_draft(profile_request(), "original-key")
    revision = "9999999999999999999"
    assert revision_number(revision) == 10**19 - 1
    wire = draft_to_wire(ProfileDraft(command.profile_id, revision, command.definition, 1))
    assert wire["revision"] == revision and wire["publication"] == {
        "status": "draft",
        "published_at": None,
        "retired_at": None,
        "disabled_reason": None,
    }
    assert "validation" not in wire
    with pytest.raises(ValueError):
        check_command(replace(command, profile_id="another-profile"))


@pytest.mark.parametrize("raw", [b'{"x": 1}', b'{"x":NaN}', b"[]", b"[" * 65535 + b"]" * 65535])
def test_corrupt_noncanonical_or_deep_records_fail_safely(raw: bytes) -> None:
    with pytest.raises(ValueError, match=r"^Invalid stored profile document$"):
        decode_document(raw)


def test_closed_pool_and_failed_get_never_imply_missing_or_allow_a_new_write() -> None:
    from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

    pool = speech_pool("postgresql://unused.invalid/unused")
    command = prepare_profile_draft(profile_request(), "original-key")
    port = PostgresProfileDrafts(pool)
    assert port.create(command).recovery == port.recover(command).recovery == "query_original"
    with pytest.raises(ProfileUnavailableError):
        port.get(command.profile_id, "1")
