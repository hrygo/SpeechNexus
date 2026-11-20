"""The reviewed 422 code additions explain their own findings and nothing else.

`#1080` refuses `voice_design`, `voice_clone` and `voice_preview` at creation, because
their kinds declare no stored command kind: admission writes no attempt or lease and
no queue's claim can match them, so the old `202` promised an execution that never
started. The refusal is a machine-visible 422 code, and the speech HTTP gate reports
every response enum that grows one. It therefore approves the change through a *proof
over the whole contract diff* (`reviewed_vocabulary_addition`): the approval fires only
when `contracts/` differs in exactly the reviewed file, at exactly the reviewed enums,
with exactly the reviewed values, and with nothing else changed semantically.

This file proves the proof cannot leak:

* the registry names exactly the three enums, both codes and the response split;
* the baseline this file builds really lacks the codes, so the comparison is real;
* the real comparison approves its own findings and leaves no finding behind;
* an annotation edit beside the additions is still approved (structure is unchanged);
* an unreviewed code, a removed value, a permuted vocabulary, a sibling constraint,
  a second contract file, a tampered split response and a re-pointed second operation
  each keep the gate or the proof rejected;
* a contract that already carries the additions needs no approval.
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
SPEECH_SOURCE = "contracts/speech-openapi.yaml"
ERRORS_SOURCE = "contracts/shared/errors.schema.json"
JOB_CODE = "job_kind_not_dispatchable"
VOICE_CODE = "voice_draft_job_not_dispatchable"
# The reviewed pointers, written out here rather than read from the registry: the
# positive case must fail if the registry moves, so the test cannot inherit the
# registry's own mistake.
_INLINE_422_CODE = (
    "content",
    "application/json",
    "schema",
    "allOf",
    1,
    "properties",
    "error",
    "properties",
    "code",
)
JOBS_ENUM = ("paths", "/v1/nexus/jobs", "post", "responses", "422", *_INLINE_422_CODE, "enum")
SUBMIT_ENUM = (
    "components",
    "responses",
    "VoiceDraftSubmitUnprocessableEntity",
    *_INLINE_422_CODE,
    "enum",
)
CANDIDATE_ENUM = (
    "components",
    "responses",
    "VoiceDraftCandidateUnprocessableEntity",
    *_INLINE_422_CODE,
    "enum",
)
PREVIEW_ENUM = (
    "components",
    "responses",
    "VoiceDraftPreviewUnprocessableEntity",
    *_INLINE_422_CODE,
    "enum",
)
PREVIEW_COMPONENT = ("components", "responses", "VoiceDraftPreviewUnprocessableEntity")
PREVIEW_422 = ("paths", "/v1/nexus/voice-drafts/{draft_id}/previews", "post", "responses", "422")
VALIDATE_422 = (
    "paths",
    "/v1/nexus/voice-drafts/{draft_id}/validations",
    "post",
    "responses",
    "422",
)


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _kernel() -> ModuleType:
    return _load("vocabulary_kernel", "scripts/dev-env/check-openai-contract-compat.py")


def _http_gate() -> ModuleType:
    return _load("vocabulary_http_gate", "scripts/dev-env/check-http-contract-changes.py")


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True).strip()


def _read(root: Path) -> dict[str, Any]:
    path = root / SPEECH_SOURCE
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def _write(root: Path, document: dict[str, Any]) -> None:
    (root / SPEECH_SOURCE).write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _at(document: object, pointer: tuple[str | int, ...]) -> Any:
    current: Any = document
    for part in pointer:
        current = current[part]
    return current


def _revert_reviewed_change(root: Path) -> None:
    """Write the pre-#1080 contract: no codes, and no preview-specific response."""

    document = _read(root)
    for pointer, code in ((JOBS_ENUM, JOB_CODE), (SUBMIT_ENUM, VOICE_CODE)):
        enum = cast(list[str], _at(document, pointer))
        assert code in enum, (pointer, code)
        enum.remove(code)
    responses = cast(dict[str, Any], _at(document, PREVIEW_COMPONENT[:-1]))
    del responses["VoiceDraftPreviewUnprocessableEntity"]
    cast(dict[str, Any], _at(document, PREVIEW_422))["$ref"] = (
        "#/components/responses/VoiceDraftCandidateUnprocessableEntity"
    )
    _write(root, document)


def _repository(
    tmp_path: Path, mutate: Callable[[Path], None] | None = None
) -> tuple[ModuleType, str]:
    """A throwaway repo whose baseline is the current tree without the reviewed change."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _revert_reviewed_change(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Vocabulary baseline")
    _git(tmp_path, "config", "user.email", "baseline@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Pre-#1080 code vocabulary")
    baseline = _git(tmp_path, "rev-parse", "HEAD")
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts", dirs_exist_ok=True)
    if mutate is not None:
        mutate(tmp_path)
    module = _http_gate()
    cast(Any, module)._ROOT = tmp_path
    return module, baseline


def _edit(edit: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    """Build a mutation that edits the reviewed source through one callback."""

    def mutate(root: Path) -> None:
        document = _read(root)
        edit(document)
        _write(root, document)

    return mutate


def _append_an_unreviewed_code(document: dict[str, Any]) -> None:
    cast(list[str], _at(document, JOBS_ENUM)).append("synthetic_reviewed_error")


def _remove_a_declared_value(document: dict[str, Any]) -> None:
    cast(list[str], _at(document, JOBS_ENUM)).remove("job_text_asset_unsupported")


def _permute_the_declared_values(document: dict[str, Any]) -> None:
    enum = cast(list[str], _at(document, JOBS_ENUM))
    enum.insert(0, enum.pop())


def _narrow_a_sibling_constraint(document: dict[str, Any]) -> None:
    retryable = cast(dict[str, Any], _at(document, (*SUBMIT_ENUM[:-2], "retryable")))
    assert retryable["enum"] == [True, False]
    retryable["enum"] = [True]


def _tamper_with_the_split_response(document: dict[str, Any]) -> None:
    kind = cast(dict[str, Any], _at(document, (*PREVIEW_ENUM[:-2], "type")))
    assert kind["const"] == "invalid_request_error"
    kind["const"] = "permission_error"


def _repoint_the_validation_response(document: dict[str, Any]) -> None:
    cast(dict[str, Any], _at(document, VALIDATE_422))["$ref"] = (
        "#/components/responses/VoiceDraftPreviewUnprocessableEntity"
    )


def _add_a_description(document: dict[str, Any]) -> None:
    document["info"]["description"] = "Synthetic annotation added beside the reviewed codes."


def _touch_another_contract_file(root: Path) -> None:
    path = root / ERRORS_SOURCE
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")


# --- the registry and the baseline the comparison runs against ---------------------


def test_the_registry_names_exactly_the_reviewed_additions() -> None:
    kernel = _kernel()
    additions = kernel._REVIEWED_CODE_ADDITIONS
    splits = kernel._REVIEWED_RESPONSE_SPLITS

    assert {entry.path for entry in additions} == {SPEECH_SOURCE}
    assert {entry.code for entry in additions} == {JOB_CODE, VOICE_CODE}
    assert [entry.pointer for entry in additions] == [JOBS_ENUM, SUBMIT_ENUM, PREVIEW_ENUM]
    # The preview response is new in this diff, so its baseline vocabulary is the
    # shared candidate response's, not a list the baseline document could hold.
    assert additions[0].mirror is None
    assert additions[1].mirror is None
    assert additions[2].mirror == CANDIDATE_ENUM
    assert len(splits) == 1
    split = splits[0]
    assert (split.path, split.pointer) == (SPEECH_SOURCE, PREVIEW_422)
    assert split.shared_component == "VoiceDraftCandidateUnprocessableEntity"
    assert split.reviewed_component == "VoiceDraftPreviewUnprocessableEntity"
    assert kernel._REVIEWED_CODE_ISSUE == "#1080"
    assert len(kernel._REVIEWED_CODE_REASON) > 80


def test_the_synthesized_baseline_really_lacks_the_reviewed_codes(tmp_path: Path) -> None:
    """The positive case is only meaningful if the baseline is the pre-change one."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _revert_reviewed_change(tmp_path)

    document = _read(tmp_path)
    assert JOBS_ENUM[-2:] == ("code", "enum")
    assert JOB_CODE not in cast(list[str], _at(document, JOBS_ENUM))
    assert VOICE_CODE not in cast(list[str], _at(document, SUBMIT_ENUM))
    assert "VoiceDraftPreviewUnprocessableEntity" not in cast(
        dict[str, Any], _at(document, PREVIEW_COMPONENT[:-1])
    )
    assert cast(dict[str, Any], _at(document, PREVIEW_422)) == {
        "$ref": "#/components/responses/VoiceDraftCandidateUnprocessableEntity"
    }
    # The shared candidate response is untouched: validation commits a dispatched
    # `voice_validation` Job and must keep the vocabulary it can really answer.
    assert VOICE_CODE not in cast(list[str], _at(document, CANDIDATE_ENUM))


# --- the approval ---------------------------------------------------------------


def test_the_real_comparison_approves_its_own_findings(tmp_path: Path) -> None:
    module, baseline = _repository(tmp_path)

    report = module.inspect_sources(baseline, surface="speech")

    assert report["findings"] == [], "\n".join(report["findings"][:3])
    approvals = report["approved_reviewed_code_additions"]
    assert len(approvals) == 1, approvals
    assert JOB_CODE in approvals[0] and VOICE_CODE in approvals[0]
    assert SPEECH_SOURCE in approvals[0] and "#1080" in approvals[0]
    assert report["reviewed_code_addition_problem"] is None
    # The planner proof did not cover this diff, and it must not be reported as if
    # the gate had expected it to: no finding is left for it to explain.
    assert report["findings"] == []


def test_an_annotation_beside_the_additions_is_still_approved(tmp_path: Path) -> None:
    """Annotations cannot change acceptance, so they cannot break the proof either."""

    module, baseline = _repository(tmp_path, _edit(_add_a_description))

    report = module.inspect_sources(baseline, surface="speech")

    assert report["findings"] == []
    assert len(report["approved_reviewed_code_additions"]) == 1


# --- rejected: the vocabulary itself -------------------------------------------


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (_edit(_append_an_unreviewed_code), "not exactly the reviewed"),
        (_edit(_remove_a_declared_value), "not exactly the reviewed"),
        (_edit(_permute_the_declared_values), "restates a value"),
    ],
    ids=["unreviewed-code", "removed-value", "permuted-vocabulary"],
)
def test_an_unreviewed_vocabulary_change_keeps_the_gate_rejected(
    tmp_path: Path, mutate: Callable[[Path], None], reason: str
) -> None:
    module, baseline = _repository(tmp_path, mutate)

    report = module.inspect_sources(baseline, surface="speech")

    assert report["approved_reviewed_code_additions"] == []
    assert report["findings"], "an unreviewed vocabulary change must stay rejected"
    problem = report["reviewed_code_addition_problem"]
    assert problem is not None and reason in problem, problem


@pytest.mark.parametrize(
    "mutate",
    [_edit(_narrow_a_sibling_constraint), _edit(_repoint_the_validation_response)],
    ids=["narrowed-sibling-constraint", "repointed-second-operation"],
)
def test_another_semantic_change_beside_the_addition_keeps_the_gate_rejected(
    tmp_path: Path, mutate: Callable[[Path], None]
) -> None:
    module, baseline = _repository(tmp_path, mutate)

    report = module.inspect_sources(baseline, surface="speech")

    assert report["approved_reviewed_code_additions"] == []
    assert report["findings"], "a second semantic change must stay rejected"
    problem = report["reviewed_code_addition_problem"]
    assert problem is not None and "another semantic change" in problem, problem


def test_the_split_response_must_be_the_shared_one_plus_the_code(tmp_path: Path) -> None:
    """The new response is deleted before the tree compare, so this needs its own check.

    Without it, re-pointing an operation at a component that declares something the
    shared response never did would pass as "the reviewed split".
    """

    _, baseline = _repository(tmp_path, _edit(_tamper_with_the_split_response))

    problem = _kernel().reviewed_vocabulary_addition(tmp_path, baseline)

    assert problem is not None and "plus the reviewed code" in problem, problem


def test_a_second_contract_file_keeps_the_proof_rejected(tmp_path: Path) -> None:
    """The proof is a whole-diff claim: one extra file disables it, whatever it holds."""

    _, baseline = _repository(tmp_path, _touch_another_contract_file)

    problem = _kernel().reviewed_vocabulary_addition(tmp_path, baseline)

    assert problem is not None and "contracts/ differs in" in problem, problem
    assert ERRORS_SOURCE in problem


def test_a_contract_that_already_carries_the_additions_needs_no_approval(tmp_path: Path) -> None:
    """Post-merge `main` compares against itself: no findings, no approval line."""

    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Current tree")
    _git(tmp_path, "config", "user.email", "current@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "Current code vocabulary")
    module = _http_gate()
    cast(Any, module)._ROOT = tmp_path

    report = module.inspect_sources("HEAD", surface="speech")

    assert report["findings"] == []
    assert report["approved_reviewed_code_additions"] == []


# --- the pointer helper the proof depends on -----------------------------------


def test_the_pointer_reader_reaches_a_list_index_and_keeps_keys_distinct() -> None:
    kernel = _kernel()
    document = {"allOf": [{"a": 1}, {"b": [{"code": {"enum": ["x"]}}]}], "1": "a mapping key"}

    assert kernel._pointer_value(document, ("allOf", 1, "b", 0, "code", "enum")) == ["x"]
    # A mapping key written as "1" is a name, not a position.
    assert kernel._pointer_value(document, ("1",)) == "a mapping key"
    assert kernel._pointer_value(document, ("allOf", 2)) is None
    assert kernel._pointer_value(document, ("allOf", 1, "missing")) is None
