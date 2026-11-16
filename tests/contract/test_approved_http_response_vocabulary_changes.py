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
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

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
    """The kind of change a finding names, e.g. `enum values added`.

    The pointer comes first (`contracts/speech-openapi.yaml:paths/...`), so the class is
    what follows the first `": "` separator; for an enum finding that is
    `enum values added`, whose own `": "` introduces the codes. Splitting from the right
    instead would return the code list and silently drop the class from every set built
    from it.
    """
    return finding.split(": ", 1)[1].split(":", 1)[0]


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
    table = set(_table(module))
    # Every class the table uses is asserted, derived from the entries themselves, so a
    # new class cannot pass unguarded (22 of the 27 approvals are `const` changes).
    classes = {_finding_class(finding) for finding in table}
    # Compare against the raw findings instead of the filter's output, because the filter
    # only waves through entries whose recorded version pair matches the comparison:
    # `info.version` is bumped by every feature PR, so a version bump would wipe out all
    # 27 approvals and report a contract that is fine as broken. The versions are not
    # this test's subject; `test_a_registered_line_filters_only_its_own_source_and_version_pair`
    # covers the filter's scoping. The filter appends " [issue: reason]" to what it waves
    # through, so the identity of a finding is its text before that suffix.
    carried = {item.rsplit(" [", 1)[0] for item in report["approved_response_vocabulary_changes"]}
    carried |= {
        finding
        for finding in report["findings"]
        if finding.startswith(SOURCE) and _finding_class(finding) in classes
    }
    # One finding per line: a value-less or swapped approval is exactly what the
    # reader has to see here, and pytest truncates a long dict's values.
    assert carried == table, "\n".join(
        [
            "widening_not_approved:",
            *sorted(carried - table),
            "approval_without_widening:",
            *sorted(table - carried),
        ]
    )


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


_REVIEWED_CONST = re.compile(r"const value changed: (.+) -> (.+)$")
_HEADER_POINTER = (
    "contracts/speech-openapi.yaml:paths//v1/nexus/voice-drafts/{draft_id}/validations/"
    "post/responses/503/headers/x-should-retry"
)


def _inspect_a_mutated_working_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Compare the frozen baseline against the real contract after one mutation.

    The mutation lands in the throwaway repository's working tree only, so the
    comparison is the same one the gate runs at PR time: baseline help, no history.
    """
    loaded, baseline = _real_contract_against_the_frozen_baseline(tmp_path, monkeypatch)
    contract = yaml.safe_load((tmp_path / SOURCE).read_text(encoding="utf-8"))
    mutate(contract)
    (tmp_path / SOURCE).write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return cast("dict[str, Any]", loaded.inspect_sources(baseline, surface="speech"))


def _set_header_vocabulary(value: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def mutate(contract: dict[str, Any]) -> None:
        header = contract["paths"]["/v1/nexus/voice-drafts/{draft_id}/validations"]["post"][
            "responses"
        ]["503"]["headers"]["X-Should-Retry"]
        header["schema"] = {"type": "string", **value}

    return mutate


def _set_retryable_vocabulary(value: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    def mutate(contract: dict[str, Any]) -> None:
        error = contract["paths"]["/v1/nexus/voice-drafts/{draft_id}/validations"]["post"][
            "responses"
        ]["503"]["content"]["application/json"]["schema"]["allOf"][1]["properties"]["error"][
            "properties"
        ]
        error["retryable"] = value

    return mutate


def test_the_unmutated_contract_still_passes_so_every_mutation_below_is_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _inspect_a_mutated_working_tree(tmp_path, monkeypatch, lambda contract: None)
    assert report["findings"] == []
    assert len(report["approved_response_vocabulary_changes"]) == 27


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        # The reviewed change widened the header from `const: "true"` to the closed
        # enum `["true", "false"]`. A third vocabulary - a single other value, or an
        # enum carrying a code nobody reviewed - is not that change, so its finding
        # has to name the unreviewed value and reject.
        (
            _set_header_vocabulary({"const": "false"}),
            f"{_HEADER_POINTER}: const value changed: 'true' -> 'false'",
        ),
        (
            _set_header_vocabulary({"const": "maybe"}),
            f"{_HEADER_POINTER}: const value changed: 'true' -> 'maybe'",
        ),
        (
            _set_header_vocabulary({"enum": ["true", "maybe"]}),
            f"{_HEADER_POINTER}: const value changed: 'true' -> ['true', 'maybe']",
        ),
        # The body's `retryable` was reviewed as the per-code enum `[true, false]`;
        # collapsing it back to one constant swaps the reviewed vocabulary.
        (
            _set_retryable_vocabulary({"const": False}),
            "contracts/speech-openapi.yaml:paths//v1/nexus/voice-drafts/{draft_id}/"
            "validations/post/responses/503/application/json/allOf/1/properties/error/"
            "properties/retryable: const value changed: True -> False",
        ),
    ],
)
def test_a_value_swapped_away_from_the_reviewed_vocabulary_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    report = _inspect_a_mutated_working_tree(tmp_path, monkeypatch, mutate)
    assert report["findings"] == [expected]
    # The mutated pointer's own approval no longer matches; the other 26 still do.
    approved = {item.rsplit(" [", 1)[0] for item in report["approved_response_vocabulary_changes"]}
    assert expected not in approved
    assert len(approved) == 26


def test_the_reviewed_vocabulary_relisted_verbatim_stays_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The positive control: the value the entry reviews, restated with the same
    # meaning, is still approved. Binding values must not make a dormant entry
    # reject an unchanged declaration.
    report = _inspect_a_mutated_working_tree(
        tmp_path, monkeypatch, _set_header_vocabulary({"enum": ["true", "false"]})
    )
    assert report["findings"] == []
    assert any(_HEADER_POINTER in item for item in report["approved_response_vocabulary_changes"])


def test_every_reviewed_const_entry_names_both_values() -> None:
    # A value-less key is the defect for issue #858: it would cover any later value
    # at that pointer. The class prefix stays `const value changed` so the class
    # guard above keeps working; the values follow it, as the enum form already does.
    table = _table(module())
    const_entries = [key for key in table if ": const value changed" in key]
    assert len(const_entries) == 22
    for key in const_entries:
        match = _REVIEWED_CONST.search(key)
        assert match is not None, key
        old, new = match.group(1), match.group(2)
        assert old and new and old != new, key
        assert key.split(": const value changed")[1].startswith(": "), key


def test_the_const_finding_names_the_value_it_replaced() -> None:
    kernel = module()._kernel()
    issues: list[str] = []
    kernel._compare_schema(
        {"type": "string", "const": "false"},
        {"type": "string", "const": "true"},
        "probe",
        issues,
        direction="response",
    )
    assert issues == ["probe: const value changed: 'false' -> 'true'"]
