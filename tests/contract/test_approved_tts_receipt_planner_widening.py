"""The reviewed planner widening explains its own findings and nothing else.

`#1073` raises the planner budget's ceiling from 400 to 4096 so a synchronous call
and a Job can accept the 4096 characters the contract has always declared. A receipt
and an `effective_configuration` restate that budget, so the widening reappears on the
response side -- but only for a caller that itself asked for more than 400. The two
gates therefore approve the movement through a *proof over the whole contract diff*
(`reviewed_bound_movement`): the approval fires only when `contracts/` differs in
exactly the reviewed files, at exactly the reviewed pointer and values, with nothing
else changed semantically.

This file proves the proof cannot leak:

* the registry names only the planner `max_chars` maximum, with both values pinned;
* the real comparison approves its own findings and leaves no finding behind;
* an unrelated response bound widening beside the movement still rejects;
* an unrelated `error.code` enum addition in another contract file still rejects;
* the compat gate's receipt filter approves exactly those three pointers and values;
* a contract that already carries the movement (post-merge `main`) needs no approval.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEECH_SOURCE = "contracts/speech-openapi.yaml"
OVERRIDES_SOURCE = "contracts/shared/speech-overrides.schema.json"
ERRORS_SOURCE = "contracts/shared/errors.schema.json"
REVIEWED = ("400", "4096")


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _http_gate() -> ModuleType:
    return _load(
        "http_change_cli_receipt_planner", "scripts/dev-env/check-http-contract-changes.py"
    )


def _compat_gate() -> ModuleType:
    return _load(
        "openai_compat_cli_receipt_planner", "scripts/dev-env/check-openai-contract-compat.py"
    )


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def _narrow_planner(root: Path) -> None:
    """Write the pre-#1073 planner ceiling (400) into both declaring files."""

    for relative, old, new in (
        (SPEECH_SOURCE, "maximum: 4096", "maximum: 400"),
        (OVERRIDES_SOURCE, '"maximum": 4096', '"maximum": 400'),
    ):
        path = root / relative
        text = path.read_text(encoding="utf-8")
        assert text.count(old) == 1, f"{relative} no longer declares one 4096 bound"
        path.write_text(text.replace(old, new), encoding="utf-8")


def _widen_unrelated_response_bound(root: Path) -> None:
    path = root / SPEECH_SOURCE
    text = path.read_text(encoding="utf-8")
    # `JobError.message.maxLength` is a response-only bound with no relation to the
    # planner budget, so the reviewed movement cannot explain its widening.
    old = (
        "        message:\n"
        "          type: string\n"
        "          minLength: 1\n"
        "          maxLength: 2000"
    )
    assert text.count(old) == 1
    path.write_text(text.replace(old, old.replace("2000", "3000")), encoding="utf-8")


def _add_an_error_code(root: Path) -> None:
    """A response vocabulary widening in another contract file.

    The movement cannot explain it, and `errors.schema.json` is not one of the files
    the movement is allowed to touch, so the proof must fail on the file set alone.
    """

    path = root / ERRORS_SOURCE
    text = path.read_text(encoding="utf-8")
    old = '        "service_unavailable_error"\n      ]'
    assert text.count(old) == 1
    path.write_text(
        text.replace(
            old, '        "service_unavailable_error",\n        "synthetic_reviewed_error"\n      ]'
        ),
        encoding="utf-8",
    )


def _repository(tmp_path: Path, mutate: Any = None) -> tuple[ModuleType, str]:
    """A throwaway repo whose baseline is the current tree with the planner at 400."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _narrow_planner(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Planner baseline")
    _git(tmp_path, "config", "user.email", "baseline@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Pre-#1073 planner ceiling")
    baseline = _git(tmp_path, "rev-parse", "HEAD")
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts", dirs_exist_ok=True)
    if mutate is not None:
        mutate(tmp_path)
    module = _http_gate()
    cast(Any, module)._ROOT = tmp_path
    return module, baseline


def test_the_registry_names_only_the_reviewed_planner_bound() -> None:
    kernel = _compat_gate()
    movement = kernel._REVIEWED_BOUND_MOVEMENT
    assert {entry.path for entry in movement} == {SPEECH_SOURCE, OVERRIDES_SOURCE}
    for entry in movement:
        assert entry.pointer[-4:] == ("planner", "properties", "max_chars", "maximum"), entry.label
        assert entry.keyword == "maximum"
        assert (entry.before, entry.after) == (400, 4096), entry.label
    assert kernel._REVIEWED_BOUND_ISSUE == "#1073"
    assert len(kernel._REVIEWED_BOUND_REASON) > 80
    assert len(kernel._TTS_RECEIPT_PLANNER_POINTERS) == 3
    for pointer in kernel._TTS_RECEIPT_PLANNER_POINTERS:
        assert pointer.startswith(ERRORS_SOURCE + "/$defs/OperationRecovery/"), pointer
        assert pointer.endswith("/properties/planner/properties/max_chars"), pointer


def test_the_real_comparison_approves_its_own_findings(
    tmp_path: Path,
) -> None:
    module, baseline = _repository(tmp_path)

    report = module.inspect_sources(baseline, surface="speech")

    assert report["findings"] == [], "\n".join(report["findings"][:3])
    movements = report["approved_reviewed_bound_movements"]
    assert len(movements) == 1
    assert SPEECH_SOURCE + "/components/schemas/RestTtsOverrides" in movements[0]
    assert OVERRIDES_SOURCE + "/$defs/TtsOverrides" in movements[0]
    assert "#1073" in movements[0]


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (_widen_unrelated_response_bound, "another semantic change"),
        (_add_an_error_code, "contracts/ differs in"),
    ],
    ids=["unrelated-response-bound", "unrelated-error-code"],
)
def test_an_unrelated_change_beside_the_movement_still_rejects(
    tmp_path: Path, mutate: Any, reason: str
) -> None:
    module, baseline = _repository(tmp_path, mutate)

    report = module.inspect_sources(baseline, surface="speech")

    assert report["approved_reviewed_bound_movements"] == []
    assert report["findings"], "an unrelated change must keep the gate rejected"
    problem = report["reviewed_bound_movement_problem"]
    assert problem is not None and reason in problem, problem


def test_a_contract_that_already_carries_the_movement_needs_no_approval(tmp_path: Path) -> None:
    """Post-merge `main` compares against itself: no findings, no approval line."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Current tree")
    _git(tmp_path, "config", "user.email", "current@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Current planner ceiling")
    module = _http_gate()
    cast(Any, module)._ROOT = tmp_path

    report = module.inspect_sources("HEAD", surface="speech")

    assert report["findings"] == []
    assert report["approved_reviewed_bound_movements"] == []


def test_the_compat_filter_approves_exactly_the_three_receipt_pointers() -> None:
    kernel = _compat_gate()
    message = "maximum was tightened from 4096 to 400"
    reviewed = [f"{pointer}: {message}" for pointer in kernel._TTS_RECEIPT_PLANNER_POINTERS]
    unrelated = [
        # Same message, another pointer.
        f"{ERRORS_SOURCE}/$defs/OperationRecovery/properties/receipt/properties/requested/"
        f"properties/instructions_present: {message}",
        # Same pointer, another value pair.
        f"{kernel._TTS_RECEIPT_PLANNER_POINTERS[0]}: maximum was tightened from 8192 to 400",
        # Another document entirely.
        f"{SPEECH_SOURCE}:paths//v1/models/get/responses/200: {message}",
    ]

    remaining, approved = kernel._apply_reviewed_receipt_planner_widenings([*reviewed, *unrelated])

    assert approved == [f"{line} [#1073: {kernel._REVIEWED_BOUND_REASON}]" for line in reviewed]
    assert remaining == unrelated


def test_the_compat_proof_fails_when_another_file_differs(
    tmp_path: Path,
) -> None:
    """The compat proof runs over the whole diff, so one extra file disables it."""

    kernel = _compat_gate()
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _narrow_planner(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Compat baseline")
    _git(tmp_path, "config", "user.email", "baseline@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Pre-#1073 planner ceiling")
    baseline = _git(tmp_path, "rev-parse", "HEAD")
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts", dirs_exist_ok=True)
    _add_an_error_code(tmp_path)

    problem = kernel.reviewed_bound_movement(tmp_path, baseline)

    assert problem is not None and "contracts/ differs in" in problem, problem


def test_the_scope_anchor_cannot_match_a_prefix_or_a_partial_pointer() -> None:
    """A second widening at a *parent* of the reviewed bound is not approved."""

    kernel = _compat_gate()
    message = "maximum was tightened from 4096 to 400"
    broader = [
        f"{ERRORS_SOURCE}/$defs/OperationRecovery/properties/receipt/properties/requested/"
        f"properties/planner: {message}",
        f"{ERRORS_SOURCE}/$defs/OperationRecovery/properties/receipt/properties/requested/"
        f"properties/planner/properties/max_chars: maximum was tightened from 4096 to 300",
    ]

    remaining, approved = kernel._apply_reviewed_receipt_planner_widenings(list(broader))

    assert approved == []
    assert remaining == broader
