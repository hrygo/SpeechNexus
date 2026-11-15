"""Only exact, reviewed response-vocabulary widenings pass the HTTP gate.

A response that grows a code, or whose retry rule becomes per-code instead of a
constant, is a real structural change: a client written against the frozen baseline
may meet a value the old declaration excluded. The gate therefore rejects every such
finding unless one entry in the approval table names that exact line (#828 review M3
and M5 decided the voice-draft 503 widening).

This file proves three things, in the same spirit as the other approval guards:

* every entry corresponds to a finding the real comparison against the frozen
  baseline produces, so an approval cannot be granted to a change that does not
  exist;
* an unregistered widening still rejects the comparison;
* a registered line is filtered only in its own source and only for the version pair
  it was approved for, so a later change cannot ride along on it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import cast

from tests.contract.test_http_contract_changes import repository as repository

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/speech-openapi.yaml"


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "http_change_cli_vocabulary", ROOT / "scripts/dev-env/check-http-contract-changes.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_the_table_is_not_empty_and_every_entry_is_fully_annotated() -> None:
    table = module()._APPROVED_RESPONSE_VOCABULARY_WIDENINGS
    assert table, "no reviewed widening is registered"
    for finding, metadata in table.items():
        assert finding.startswith(SOURCE + ":paths/"), finding
        assert metadata["issue"].startswith("#"), finding
        assert len(metadata["reason"]) > 40, finding
        assert metadata["from_version"] and metadata["to_version"], finding


def test_every_entry_matches_a_finding_of_the_real_comparison() -> None:
    # The guard only filters an exact line, so an entry that the real comparison does
    # not produce is dead. Comparing the tree against the merge base is the only way
    # to prove the entries are live; `make contracts-speech-changes` runs the same
    # baseline in CI.
    loaded = module()
    report = loaded.inspect_sources("origin/main", surface="speech")
    approved = report["approved_response_vocabulary_changes"]
    assert len(approved) == len(loaded._APPROVED_RESPONSE_VOCABULARY_WIDENINGS)
    assert report["findings"] == []


def test_an_unregistered_widening_is_still_rejected() -> None:
    loaded = module()
    synthetic = [
        f"{SOURCE}:paths//v1/nexus/voice-designs/post/responses/503/"
        "application/json/allOf/1/properties/error/properties/code: "
        "enum values added: ['voice_draft_something_new']"
    ]
    remaining, approved = loaded._apply_approved_response_vocabulary_widenings(
        SOURCE, "0.1.0.dev18", "0.1.0.dev18", synthetic
    )
    assert approved == []
    assert remaining == synthetic


def test_a_registered_line_filters_only_its_own_source_and_version_pair() -> None:
    loaded = module()
    table = loaded._APPROVED_RESPONSE_VOCABULARY_WIDENINGS
    finding = next(iter(table))

    other_source, approved = loaded._apply_approved_response_vocabulary_widenings(
        "contracts/other-openapi.yaml", "0.1.0.dev18", "0.1.0.dev18", [finding]
    )
    assert approved == [] and other_source == [finding]

    other_version, approved = loaded._apply_approved_response_vocabulary_widenings(
        SOURCE, "0.1.0.dev18", "0.1.0.dev19", [finding]
    )
    assert approved == [] and other_version == [finding]

    same, approved = loaded._apply_approved_response_vocabulary_widenings(
        SOURCE, "0.1.0.dev18", "0.1.0.dev18", [finding]
    )
    assert same == [] and len(approved) == 1


def _table(loaded: ModuleType) -> dict[str, dict[str, str]]:
    table = getattr(loaded, "_APPROVED_RESPONSE_VOCABULARY_WIDENINGS")  # noqa: B009
    return cast("dict[str, dict[str, str]]", table)


def test_an_entry_without_its_reason_is_reported_instead_of_filtered() -> None:
    loaded = module()
    finding = next(iter(_table(loaded)))
    broken = dict(_table(loaded))
    entry = dict(broken[finding])
    entry["reason"] = ""
    broken[finding] = entry
    original = _table(loaded)
    setattr(loaded, "_APPROVED_RESPONSE_VOCABULARY_WIDENINGS", broken)  # noqa: B010
    try:
        remaining, approved = loaded._apply_approved_response_vocabulary_widenings(
            SOURCE, "0.1.0.dev18", "0.1.0.dev18", [finding]
        )
    finally:
        setattr(loaded, "_APPROVED_RESPONSE_VOCABULARY_WIDENINGS", original)  # noqa: B010
    assert approved == []
    assert any(item.endswith("has no issue or reason") for item in remaining)
