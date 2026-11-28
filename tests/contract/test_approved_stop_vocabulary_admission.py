"""The reviewed stop-evidence admission explains its own findings and nothing else.

`#1301` fixes an execution that completed without ever reaching the engine: the
audio held no speech, the stage issued zero engine requests, so no terminal answer
can exist and the completion path quarantined a finished execution while it kept its
lease. The third proof is `not_dispatched`, and the terminal-status contract has to
say that a `succeeded` execution may carry it.

That is a structural change the speech HTTP gate refuses to accept implicitly: the
`succeeded` branch pinned `stop_evidence_kind` to a single `const`, so the change
shows up both as a grown vocabulary and as a rewritten conditional branch. It is
approved through a proof over the whole contract diff
(`reviewed_stop_vocabulary_admission`): the approval fires only when `contracts/`
differs by exactly the registered vocabulary value, in exactly the registered branch,
with that branch still carrying its original value, and with nothing else changed
semantically.

This file proves the proof cannot leak:

* the registry names exactly one file, one vocabulary pointer, one branch and one
  value, and the issue that decided it;
* the baseline this file builds really lacks the value and pins the branch, so the
  comparison is real;
* the real comparison approves its own findings and leaves no finding behind;
* an annotation beside the change is still approved (structure is unchanged);
* a branch that drops its original value, admits a third value, stops admitting the
  value at all, or changes something else beside its vocabulary each keep the gate or
  the proof rejected;
* a sibling constraint edit and a second contract file each keep the gate rejected;
* a contract that already carries the admission needs no approval.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/internal/execution-control.openapi.yaml"
OTHER_SOURCE = "contracts/internal/runtime-control.openapi.yaml"
CODE = "not_dispatched"
ORIGINAL = "engine_terminal_response"
# The two pointers are written out here rather than read from the registry: the
# positive case must fail if the registry moves, so the test cannot inherit the
# registry's own mistake.
VOCABULARY = ("components", "schemas", "StopEvidenceKind", "enum")
BRANCH = (
    "components",
    "schemas",
    "ExecutionTerminalEvidenceInvariants",
    "allOf",
    1,
    "then",
    "properties",
    "stop_evidence_kind",
)


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _kernel() -> ModuleType:
    return _load("stop_kernel", "scripts/dev-env/check-openai-contract-compat.py")


def _http_gate() -> ModuleType:
    return _load("stop_http_gate", "scripts/dev-env/check-http-contract-changes.py")


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def _read(root: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((root / SOURCE).read_text(encoding="utf-8")))


def _write(root: Path, document: dict[str, Any]) -> None:
    (root / SOURCE).write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _at(document: object, pointer: tuple[str | int, ...]) -> Any:
    current: Any = document
    for part in pointer:
        current = current[part]
    return current


def _revert_reviewed_change(root: Path) -> None:
    """Write the pre-#1301 contract: no value, no branch widening, no description."""

    document = _read(root)
    enum = cast(list[str], _at(document, VOCABULARY))
    assert CODE in enum
    enum.remove(CODE)
    cast(dict[str, Any], _at(document, (*BRANCH[:-1],)))[BRANCH[-1]] = {"const": ORIGINAL}
    kind = cast(dict[str, Any], _at(document, ("components", "schemas", "StopEvidenceKind")))
    kind.pop("description", None)
    _write(root, document)


def _repository(
    tmp_path: Path, mutate: Callable[[Path], None] | None = None
) -> tuple[ModuleType, str]:
    """A throwaway repo whose baseline is the current tree without the reviewed change."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _revert_reviewed_change(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Stop evidence baseline")
    _git(tmp_path, "config", "user.email", "baseline@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Pre-#1301 stop evidence")
    baseline = _git(tmp_path, "rev-parse", "HEAD")
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts", dirs_exist_ok=True)
    if mutate is not None:
        mutate(tmp_path)
    module = _http_gate()
    cast(Any, module)._ROOT = tmp_path
    return module, baseline


def _edit(edit: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    def mutate(root: Path) -> None:
        document = _read(root)
        edit(document)
        _write(root, document)

    return mutate


def _admit_a_third_value(document: dict[str, Any]) -> None:
    cast(dict[str, Any], _at(document, BRANCH))["enum"].append(
        "operator_verified_instance_terminated"
    )


def _drop_the_original_value(document: dict[str, Any]) -> None:
    branch = cast(dict[str, Any], _at(document, BRANCH))
    branch["enum"] = [CODE]


def _keep_the_branch_pinned(document: dict[str, Any]) -> None:
    cast(dict[str, Any], _at(document, (*BRANCH[:-1],)))[BRANCH[-1]] = {"const": ORIGINAL}


def _change_something_else_in_the_branch(document: dict[str, Any]) -> None:
    branch = cast(dict[str, Any], _at(document, BRANCH))
    branch["minItems"] = 1


def _add_a_sibling_semantic_change(document: dict[str, Any]) -> None:
    error_code = cast(
        dict[str, Any],
        _at(
            document,
            ("components", "schemas", "ExecutionTerminalEvidenceInvariants", "allOf", 1, "then"),
        ),
    )
    assert "properties" in error_code
    error_code["properties"]["error_code"] = {"const": "invalid_execution_request"}


def _add_a_description(document: dict[str, Any]) -> None:
    document["info"]["description"] = "Synthetic annotation added beside the admission."


def _touch_another_contract_file(root: Path) -> None:
    path = root / OTHER_SOURCE
    document = cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))
    document.setdefault("paths", {})["/synthetic-semantic-change"] = {"get": {}}
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


# --- the registry and the baseline the comparison runs against ---------------------


def test_the_registry_names_exactly_the_reviewed_admission() -> None:
    entries = _kernel()._REVIEWED_STOP_VOCABULARY_ADMISSIONS

    assert len(entries) == 1
    entry = entries[0]
    assert entry.path == SOURCE
    assert entry.vocabulary == VOCABULARY
    assert entry.branch == BRANCH
    assert entry.code == CODE
    assert entry.issue == "#1301"
    assert len(entry.reason) > 80


def test_the_synthesized_baseline_really_lacks_the_admission(tmp_path: Path) -> None:
    """The positive case is only meaningful if the baseline is the pre-change one."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _revert_reviewed_change(tmp_path)

    document = _read(tmp_path)
    assert CODE not in cast(list[str], _at(document, VOCABULARY))
    assert ORIGINAL in cast(list[str], _at(document, VOCABULARY))
    assert _at(document, BRANCH) == {"const": ORIGINAL}
    assert "description" not in cast(
        dict[str, Any], _at(document, ("components", "schemas", "StopEvidenceKind"))
    )


# --- the approval ---------------------------------------------------------------


def test_the_real_comparison_approves_its_own_findings(tmp_path: Path) -> None:
    module, baseline = _repository(tmp_path)

    report = module.inspect_sources(baseline)

    assert report["findings"] == [], "\n".join(report["findings"][:3])
    approvals = report["approved_reviewed_stop_admissions"]
    assert len(approvals) == 1, approvals
    assert CODE in approvals[0] and SOURCE in approvals[0] and "#1301" in approvals[0]
    assert report["reviewed_stop_admission_problem"] is None


def test_an_annotation_beside_the_admission_is_still_approved(tmp_path: Path) -> None:
    """Annotations cannot change acceptance, so they cannot break the proof either."""

    module, baseline = _repository(tmp_path, _edit(_add_a_description))

    report = module.inspect_sources(baseline)

    assert report["findings"] == []
    assert len(report["approved_reviewed_stop_admissions"]) == 1


# --- rejected: the admitted vocabulary and its branch ---------------------------


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (_edit(_admit_a_third_value), "the reviewed branch admits"),
        (_edit(_drop_the_original_value), "the reviewed branch admits"),
        (_edit(_keep_the_branch_pinned), "the reviewed branch admits"),
        (_edit(_change_something_else_in_the_branch), "changed more than its vocabulary"),
    ],
    ids=["third-value", "dropped-original", "branch-pinned", "extra-keyword"],
)
def test_an_unreviewed_branch_shape_keeps_the_gate_rejected(
    tmp_path: Path, mutate: Callable[[Path], None], reason: str
) -> None:
    module, baseline = _repository(tmp_path, mutate)

    report = module.inspect_sources(baseline)

    assert report["approved_reviewed_stop_admissions"] == []
    assert report["findings"], "an unreviewed branch shape must stay rejected"
    problem = report["reviewed_stop_admission_problem"]
    assert problem is not None and reason in problem, problem


# --- rejected: anything else that moved ----------------------------------------


def test_a_sibling_semantic_change_keeps_the_gate_rejected(tmp_path: Path) -> None:
    module, baseline = _repository(tmp_path, _edit(_add_a_sibling_semantic_change))

    report = module.inspect_sources(baseline)

    assert report["approved_reviewed_stop_admissions"] == []
    assert report["findings"], "a second semantic change must stay rejected"
    problem = report["reviewed_stop_admission_problem"]
    assert problem is not None and "another semantic change" in problem, problem


def test_a_second_contract_file_keeps_the_proof_rejected(tmp_path: Path) -> None:
    """The proof is a whole-diff claim: one extra file disables it, whatever it holds."""

    _, baseline = _repository(tmp_path, _touch_another_contract_file)

    problem = _kernel().reviewed_stop_vocabulary_admission(tmp_path, baseline)

    assert problem is not None and "may explain only its own file" in problem, problem
    assert OTHER_SOURCE in problem


def test_a_contract_that_already_carries_the_admission_needs_no_approval(tmp_path: Path) -> None:
    """Post-merge `main` compares against itself: no findings, no approval line."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Current tree")
    _git(tmp_path, "config", "user.email", "current@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Current stop evidence")
    module = _http_gate()
    cast(Any, module)._ROOT = tmp_path

    report = module.inspect_sources("HEAD")

    assert report["findings"] == []
    assert report["approved_reviewed_stop_admissions"] == []
