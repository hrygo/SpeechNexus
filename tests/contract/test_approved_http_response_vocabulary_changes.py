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
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from tests.contract.test_http_contract_changes import repository as repository

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/speech-openapi.yaml"
# The contract as it stood before the `#828` approvals landed, committed as a fixture
# (`git show c7e43f9a^:contracts/speech-openapi.yaml`). A fixture rather than a commit
# reference on purpose: CI checks out with `fetch-depth: 1`, so the parent of the merge
# is not in the object database and a pinned SHA there reads as `bad object`. The
# fixture also keeps this test independent of how main moves afterwards.
_PRE_APPROVAL_FIXTURE = ROOT / "tests/contract/fixtures/speech-openapi.pre-828.yaml"

_KNOWN_WIDENED_CODE = "voice_draft_engine_unavailable"
_ADDED_CODES = re.compile(r"enum values added: \[([^\]]*)\]")


def _finding_class(finding: str) -> str:
    """The kind of change a finding names, e.g. `enum values added`."""
    tail = finding.rsplit(": ", 1)[-1]
    return tail.split(":", 1)[0]


def _added_codes(table: dict[str, dict[str, str]]) -> set[str]:
    codes: set[str] = set()
    for finding in table:
        match = _ADDED_CODES.search(finding)
        if match is not None:
            codes.update(value.strip().strip("'") for value in match.group(1).split(","))
    return {code for code in codes if code}


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


def _real_contract_against_the_frozen_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, str]:
    """Point the gate at a throwaway repository whose baseline is the pre-#828 contract.

    The repository holds the fixture as its only committed contract; the working tree
    then carries the real one, so the comparison is the real one and needs no history
    from the checkout CI actually runs in.
    """
    # The shared schemas are copied as they are: every approval names
    # `contracts/speech-openapi.yaml`, so the rest of the directory is identical on both
    # sides of the comparison.
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    shutil.copyfile(_PRE_APPROVAL_FIXTURE, tmp_path / SOURCE)
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "config", "user.name", "Vocabulary baseline"], check=True)
    subprocess.run([*git, "config", "user.email", "baseline@example.invalid"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "Pre-#828 contract"], check=True)
    baseline = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    shutil.copyfile(ROOT / SOURCE, tmp_path / SOURCE)
    spec = importlib.util.spec_from_file_location(
        "http_change_cli_vocabulary_live",
        ROOT / "scripts/dev-env/check-http-contract-changes.py",
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    monkeypatch.setattr(loaded, "_ROOT", tmp_path)
    return loaded, baseline


def test_every_entry_matches_a_widening_the_current_contract_still_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each approval must still describe a widening relative to the pre-#828 contract.

    The baseline is the committed fixture, not ``origin/main``: comparing the tree
    against ``origin/main`` compared it against itself once the approvals were merged,
    so that form could never pass on ``main`` again (it failed immediately after the
    merge). Only the vocabulary-widening class is compared; an unrelated later change
    to the same file is the PR-time gate's business, not this test's.
    """
    module, baseline = _real_contract_against_the_frozen_baseline(tmp_path, monkeypatch)
    report = module.inspect_sources(baseline, surface="speech")
    # The filter appends " [issue: reason]" to each finding it waves through, so the
    # identity of an approval is the finding text before that suffix.
    approved = {item.rsplit(" [", 1)[0] for item in report["approved_response_vocabulary_changes"]}
    table = set(_table(module))
    assert approved == table, {
        "widening_not_approved": sorted(approved - table),
        "approval_without_widening": sorted(table - approved),
    }
    # The other direction: a widening the table does not name must still be rejected.
    # Every class the table uses is asserted, derived from the entries themselves, so a
    # new class cannot pass unguarded (22 of the 27 approvals are `const` changes).
    classes = {_finding_class(finding) for finding in table}
    leftovers = [
        finding
        for finding in report["findings"]
        if finding.startswith(SOURCE) and _finding_class(finding) in classes
    ]
    assert leftovers == [], leftovers


def test_the_frozen_fixture_predates_the_widenings_it_is_compared_against() -> None:
    # A fixture captured after the approvals would already carry the widened
    # vocabulary, so the comparison above would report nothing and pass vacuously.
    committed = _PRE_APPROVAL_FIXTURE.read_text(encoding="utf-8")
    added = _added_codes(_table(module()))
    assert _KNOWN_WIDENED_CODE in added, added
    assert _KNOWN_WIDENED_CODE in (ROOT / SOURCE).read_text(encoding="utf-8")
    assert _KNOWN_WIDENED_CODE not in committed, (
        f"the fixture already contains {_KNOWN_WIDENED_CODE}, so it does not predate the approvals"
    )


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
