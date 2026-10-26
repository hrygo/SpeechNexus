"""Private drafts preserve actual source intent without publication defaults."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError, parse

from speechnexus_backend.speech.application.pronunciation_documents import (
    check_command,
    decode_document,
    encode_document,
)
from speechnexus_backend.speech.domain.pronunciation_drafts import (
    MAX_PRONUNCIATION_DOCUMENT_BYTES,
    PronunciationDraft,
    PronunciationDraftResult,
    document_digest,
)
from speechnexus_backend.speech.interfaces.pronunciation_drafts import (
    draft_to_wire,
    prepare_pronunciation_draft,
)
from tests.support.pronunciation_examples import pronunciation_request


def test_prepared_command_pins_order_and_original_input_without_allocating_identity() -> None:
    request = pronunciation_request()
    original = copy.deepcopy(request)
    first = prepare_pronunciation_draft(request, "original-key")
    assert first.pronunciation_id is None and first.expected_revision is None
    assert decode_document(first.intent) == original
    assert request == original
    request["entries"].reverse()
    second = prepare_pronunciation_draft(request, "original-key")
    assert first.key_digest == second.key_digest and first.request_digest != second.request_digest
    assert decode_document(first.definition)["entries"] == original["entries"]
    reordered = {k: original[k] for k in reversed(original)}
    assert prepare_pronunciation_draft(reordered, "original-key") == first
    assert "key_digest" not in decode_document(first.intent)


def test_repeated_preparation_does_not_generate_a_new_auto_family_replay_intent() -> None:
    first = prepare_pronunciation_draft(pronunciation_request(), "original-key")
    assert first == prepare_pronunciation_draft(pronunciation_request(), "original-key")
    assert first.pronunciation_id is None
    keyless = prepare_pronunciation_draft(pronunciation_request())
    another = prepare_pronunciation_draft(pronunciation_request())
    assert keyless.intent == another.intent and keyless.key_digest != another.key_digest


def test_duplicate_graphemes_and_unicode_material_are_preserved_without_priority_rules() -> None:
    entries = [
        {"grapheme": "é", "phonemes": "first"},
        {"grapheme": "é", "phonemes": "second"},
        {"grapheme": "e\u0301", "phonemes": "third"},
    ]
    command = prepare_pronunciation_draft(pronunciation_request(entries=entries), "original-key")
    assert decode_document(command.definition)["entries"] == entries


def test_source_published_default_is_still_valid_but_not_selected_by_private_draft_port() -> None:
    for value in (None, True):
        request = pronunciation_request()
        if value is None:
            del request["publish"]
        else:
            request["publish"] = value
        before = copy.deepcopy(request)
        assert parse("PronunciationCreateRequest", request) is request
        with pytest.raises(ContractValidationError, match=r"^Invalid pronunciation draft intent$"):
            prepare_pronunciation_draft(request, "original-key")
        assert request == before


def test_append_uses_exact_opaque_revision_and_pinned_family() -> None:
    command = prepare_pronunciation_draft(
        pronunciation_request(id="pron-case-sensitive", expected_revision="9999999999999999998"),
        "original-key",
    )
    assert command.pronunciation_id == "pron-case-sensitive"
    assert command.expected_revision == "9999999999999999998"
    check_command(command)
    for changed in (
        replace(command, pronunciation_id="pron-other-sensitive"),
        replace(command, expected_revision="2"),
        replace(command, definition=encode_document({"encoding": "ipa", "entries": []})),
    ):
        with pytest.raises(ValueError):
            check_command(changed)


@pytest.mark.parametrize(
    "change",
    [
        {"publish": 0},
        {"publish": None},
        {"entries": []},
        {"encoding": "synthetic"},
        {"id": "pron-unpaired"},
        {"expected_revision": "1"},
        {"private_key": "synthetic"},
        {"entries": [{"grapheme": "sensitive-synthetic", "phonemes": 123}]},
    ],
)
def test_invalid_or_ambiguous_intents_fail_safely_before_storage(change: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError) as error:
        prepare_pronunciation_draft(pronunciation_request(**change), "original-key")
    assert "sensitive-synthetic" not in str(error.value)


@pytest.mark.parametrize("key", ["short", "x" * 129, 7, True])
def test_source_key_bounds_are_not_coerced(key: Any) -> None:
    with pytest.raises(ContractValidationError):
        prepare_pronunciation_draft(pronunciation_request(), key)


def test_projection_is_source_valid_draft_only_and_does_not_alias_stored_material() -> None:
    prepared = prepare_pronunciation_draft(pronunciation_request(), "original-key")
    record = PronunciationDraft("pron-real-allocation", "1", prepared.definition, 1761436800123456)
    wire = draft_to_wire(record)
    assert parse("PronunciationRevision", wire) is wire
    assert wire["status"] == "draft" and wire["created_at"].endswith(".123456Z")
    assert not {"intent", "request_digest", "key_digest"}.intersection(wire)
    wire["entries"][0]["phonemes"] = "changed client copy"
    assert draft_to_wire(record)["entries"][0]["phonemes"] != "changed client copy"
    assert PronunciationDraftResult("unknown").recovery == "query_original"
    assert PronunciationDraftResult("created", record).recovery == "none"


def test_full_declared_entry_count_is_not_narrowed_by_the_profile_codec_byte_limit() -> None:
    request = pronunciation_request(entries=[{"grapheme": "x", "phonemes": "x"}] * 10000)
    prepared = prepare_pronunciation_draft(request, "original-key")
    assert len(prepared.intent) > 262144
    assert len(decode_document(prepared.definition)["entries"]) == 10000
    assert len(prepared.intent) < MAX_PRONUNCIATION_DOCUMENT_BYTES


@pytest.mark.parametrize("raw", [b'{"a":1,"a":1}', b'{ "a": 1}', b'{"a":NaN}', b"[]"])
def test_stored_noncanonical_or_non_json_material_is_unavailable(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode_document(raw)


def test_private_record_and_command_validation_preserve_bounds() -> None:
    prepared = prepare_pronunciation_draft(pronunciation_request(), "original-key")
    for revision in ("0", "01", "10000000000000000000"):
        with pytest.raises(ValueError):
            PronunciationDraft("pron-synthetic", revision, prepared.definition, 0)
    with pytest.raises(ValueError):
        replace(prepared, pronunciation_id="pron-synthetic")
    with pytest.raises(ValueError):
        replace(prepared, key_digest="not-a-digest")
    for timestamp in (-1, True, 253402300800000000):
        with pytest.raises(ValueError):
            PronunciationDraft("pron-synthetic", "1", prepared.definition, timestamp)
    with pytest.raises(ValueError):
        document_digest(b"")
