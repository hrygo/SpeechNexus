"""Public event grammar and trace inputs; not a C4 reducer or A05 authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.realtime_events import parse
from speechnexus_contracts.validation import validate_profile

from tests.contract.test_binding_entrypoints import generator
from tests.contract.test_contract_compat import _load_compat_module
from tests.support.contract_examples import example_value

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((ROOT / "contracts/bindings.json").read_text())
TRACES = json.loads((ROOT / "tests/fixtures/contracts/transcription-sequences.json").read_text())[
    "examples"
]


def test_transcription_member_selection_matches_declared_compatibility_scope() -> None:
    config = MANIFEST["profiles"]["realtime_events"]
    names = {
        ref.rsplit("/", 1)[-1] for ref in config["schema_unions"]["TranscriptionEvent"]["members"]
    }
    assert names == _load_compat_module()._OPENAI_REALTIME_DEFS
    assert len(names) == 14


@pytest.mark.parametrize("change", ["duplicate", "outside_union"])
def test_generated_event_selection_cannot_duplicate_or_expand_source_members(change: str) -> None:
    config = copy.deepcopy(MANIFEST["profiles"]["realtime_events"])
    members = config["schema_unions"]["TranscriptionEvent"]["members"]
    members.append(members[0] if change == "duplicate" else "#/$defs/PcmDescriptor")
    with pytest.raises(ValueError, match="unique members"):
        cast(Any, generator().bundle_contracts)(config)


def test_whole_source_import_retains_closedness_without_copying_example_audio() -> None:
    config = MANIFEST["profiles"]["realtime_events"]
    bundle = cast(Any, generator().bundle_contracts)(config)
    root = bundle["$defs"]["Root"]
    selected = bundle["$defs"]["TranscriptionEvent"]
    assert root["unevaluatedProperties"] is False
    assert selected["unevaluatedProperties"] is False
    assert "examples" not in root and "$defs" not in root
    assert "examples" not in selected
    with pytest.raises(ContractValidationError):
        parse("TranscriptionEvent", {"type": "input_audio_buffer.clear", "private": True})


@pytest.mark.parametrize("trace", TRACES, ids=lambda trace: "shapes-" + trace["id"])
def test_shared_trace_frames_validate_independently_of_semantic_expectations(
    trace: dict[str, Any],
) -> None:
    # Negative traces deliberately contain schema-valid events. Actual consumers
    # must assert semantic_valid with their own state machines after integration.
    assert type(trace["semantic_valid"]) is bool
    assert trace["rule"]
    for step in trace["steps"]:
        if "value" not in step:
            assert step["transport"] == "connection_lost"
            continue
        value = example_value(step)
        same_identity = parse("TranscriptionEvent", value) is value
        assert same_identity
        same_identity = parse("RealtimeEvent", value) is value
        assert same_identity


def test_public_and_internal_runtime_namespaces_do_not_cross() -> None:
    for profile, name in [
        ("runtime", "RealtimeSession"),
        ("realtime", "RuntimeInstanceIntentResponse"),
        ("realtime_events", "RuntimeInstanceIntentRequest"),
    ]:
        with pytest.raises(ValueError, match="Unknown profile contract"):
            validate_profile(profile, name, {})
