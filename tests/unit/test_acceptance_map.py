"""Acceptance bookkeeping must reject missing scope and inflated evidence."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "acceptance_map", ROOT / "scripts/dev-env/check-acceptance-map.py"
)
assert SPEC is not None and SPEC.loader is not None


def checker(*, fixture_scope: bool = True) -> Any:
    assert SPEC is not None and SPEC.loader is not None
    module: Any = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    if fixture_scope:
        module.FROZEN_SCOPE_SHA256 = digest(
            json.dumps(
                [{"C00/AC1": "结果可恢复。"}, {"C00/AC1/S01": "结果可恢复"}],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return module


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def mapping(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    (tmp_path / "source.md").write_text(
        "### C00/AC1\n\n> 结果可恢复。\n\n- `C00/AC1/S01`：结果可恢复。\n"
    )
    (tmp_path / "implementation.py").write_text("def recover():\n    return 'original'\n")
    (tmp_path / "result.json").write_text('{"passed":true}\n')
    reference = {
        "path": "implementation.py",
        "symbol": "def recover():",
        "sha256": digest((tmp_path / "implementation.py").read_text()),
    }
    manifest = json.dumps(
        {
            "source_head": "a" * 40,
            "files": {"implementation.py": reference["sha256"]},
        }
    )
    (tmp_path / "source.json").write_text(manifest)
    matrix = {
        "version": "0.3.0",
        "head_sha": "a" * 40,
        "source_document": "source.md",
        "entries": [
            {
                "id": "C00/AC1",
                "source_ac": "结果可恢复。",
                "source_sha256": digest("结果可恢复。"),
                "semantic_conditions": [
                    {
                        "id": "C00/AC1/S01",
                        "requirement": "结果可恢复",
                        "implementation_status": "implemented",
                        "acceptance_status": "not_run",
                        "owner": "C",
                        "implementation_refs": [reference],
                        "consumer_refs": [reference],
                        "test_refs": [reference],
                        "dependency_artifacts": [],
                        "evidence_refs": ["run"],
                        "required_dimensions": ["real_service", "persistence"],
                    }
                ],
            }
        ],
        "evidence_registry": {
            "run": {
                "path": "result.json",
                "sha256": digest('{"passed":true}\n'),
                "dimensions": ["real_service", "persistence"],
                "result_status": "passed",
                "source_head": "a" * 40,
                "scope": "Synthetic bytes, actual service and persistence; no real model.",
                "source_manifest": "source.json",
                "source_manifest_sha256": digest(manifest),
            }
        },
    }
    return matrix, tmp_path


def condition(matrix: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = matrix["entries"][0]["semantic_conditions"][0]
    return result


def codes(matrix: dict[str, Any], root: Path) -> set[str]:
    return {item["code"] for item in checker().validate(matrix, root, root)}


def test_valid_mapping_does_not_claim_acceptance(mapping: tuple[dict[str, Any], Path]) -> None:
    matrix, root = mapping
    assert checker().validate(matrix, root, root) == []
    report = checker().report(matrix, root, root)
    assert report["accepted"] is False
    assert report["acceptance_counts"] == {"not_run": 1}


def test_removed_condition_is_detected_even_when_source_remains(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    matrix["entries"][0]["semantic_conditions"] = []
    assert "condition_set" in codes(matrix, root)


def test_removed_or_duplicate_original_ac_is_detected(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    matrix["entries"].append(copy.deepcopy(matrix["entries"][0]))
    assert "duplicate_ac" in codes(matrix, root)
    matrix["entries"] = []
    assert "ac_set" in codes(matrix, root)


def test_changed_requirement_or_original_text_is_detected(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    condition(matrix)["requirement"] = "弱化的要求"
    assert "condition_text" in codes(matrix, root)
    matrix["entries"][0]["source_ac"] = "范围已改变。"
    assert {"source_text", "source_digest"} <= codes(matrix, root)


@pytest.mark.parametrize("key", ["implementation_refs", "consumer_refs", "test_refs"])
def test_missing_required_mapping_is_detected(
    mapping: tuple[dict[str, Any], Path], key: str
) -> None:
    matrix, root = mapping
    condition(matrix)[key] = []
    assert "mapping_missing" in codes(matrix, root)


def test_named_dependency_requires_owner_artifact_release_condition_and_impact(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    current = condition(matrix)
    for key in ("implementation_refs", "consumer_refs", "test_refs"):
        current[key] = []
    current["implementation_status"] = "dependency_pending"
    current["dependency_artifacts"] = [
        {
            "owner": "A04",
            "artifact": "Job API",
            "unblock_condition": "fixed release",
            "impact": "S01",
        }
    ]
    assert checker().validate(matrix, root, root) == []
    current["dependency_artifacts"][0].pop("unblock_condition")
    assert "dependency_incomplete" in codes(matrix, root)


@pytest.mark.parametrize("change", ["missing_file", "missing_symbol", "changed_bytes"])
def test_invalid_source_reference_is_detected(
    mapping: tuple[dict[str, Any], Path], change: str
) -> None:
    matrix, root = mapping
    if change == "missing_file":
        (root / "implementation.py").unlink()
    elif change == "missing_symbol":
        condition(matrix)["implementation_refs"][0]["symbol"] = "def absent():"
    else:
        (root / "implementation.py").write_text("def recover():\n    return 'new'\n")
    assert "reference_invalid" in codes(matrix, root)


@pytest.mark.parametrize("path", ["../escape.py", "/outside.py"])
def test_reference_cannot_escape_its_declared_root(
    mapping: tuple[dict[str, Any], Path], path: str
) -> None:
    matrix, root = mapping
    condition(matrix)["implementation_refs"][0]["path"] = path
    assert "reference_invalid" in codes(matrix, root)


def test_unknown_or_tampered_evidence_is_detected(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    condition(matrix)["evidence_refs"].append("absent")
    (root / "result.json").write_text('{"passed":false}\n')
    assert {"evidence_unknown", "evidence_invalid"} <= codes(matrix, root)


def pass_condition(matrix: dict[str, Any]) -> None:
    condition(matrix).update(
        acceptance_status="passed",
        acceptance_review={
            "reviewer": "C",
            "date": "2026-10-25",
            "reason": "S01 assertions reviewed",
        },
    )


def test_acceptance_requires_explicit_semantic_review(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    condition(matrix)["acceptance_status"] = "passed"
    assert "acceptance_review" in codes(matrix, root)
    pass_condition(matrix)
    assert checker().validate(matrix, root, root) == []


@pytest.mark.parametrize("result", ["failed", "skipped", "unknown", "not_run"])
def test_nonpassing_result_cannot_support_a_pass(
    mapping: tuple[dict[str, Any], Path], result: str
) -> None:
    matrix, root = mapping
    pass_condition(matrix)
    matrix["evidence_registry"]["run"]["result_status"] = result
    assert "evidence_insufficient" in codes(matrix, root)


def test_synthetic_browser_does_not_prove_model_or_physical_device(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    pass_condition(matrix)
    condition(matrix)["required_dimensions"] = ["real_model", "physical_device"]
    matrix["evidence_registry"]["run"]["dimensions"] = ["browser", "synthetic"]
    assert "evidence_insufficient" in codes(matrix, root)


def test_historical_head_cannot_settle_current_acceptance(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    pass_condition(matrix)
    matrix["evidence_registry"]["run"]["source_head"] = "b" * 40
    assert "evidence_insufficient" in codes(matrix, root)


def test_undefined_qualification_scope_cannot_pass(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    pass_condition(matrix)
    condition(matrix)["required_dimensions"] = []
    assert "qualification_missing" in codes(matrix, root)


def test_cli_fails_on_missing_mapping_and_does_not_rewrite_input(
    mapping: tuple[dict[str, Any], Path],
    tmp_path: Path,
) -> None:
    matrix, root = mapping
    condition(matrix)["consumer_refs"] = []
    source = tmp_path / "map.json"
    source.write_text(json.dumps(matrix, ensure_ascii=False))
    before = source.read_bytes()
    output = tmp_path / "audit.json"
    assert (
        checker().main(
            [
                "--matrix",
                str(source),
                "--root",
                str(root),
                "--evidence-root",
                str(root),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert source.read_bytes() == before
    assert json.loads(output.read_text())["accepted"] is False


def test_invalid_json_still_produces_a_failure_report(tmp_path: Path) -> None:
    source = tmp_path / "broken.json"
    source.write_text("{")
    output = tmp_path / "audit.json"
    assert (
        checker().main(
            [
                "--matrix",
                str(source),
                "--root",
                str(tmp_path),
                "--evidence-root",
                str(tmp_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads(output.read_text())["findings"][0]["code"] == "invalid_input"


@pytest.mark.parametrize("target", ["map.json", "source.md", "implementation.py", "result.json"])
def test_report_never_overwrites_any_audited_input(
    mapping: tuple[dict[str, Any], Path],
    target: str,
) -> None:
    matrix, root = mapping
    source = root / "map.json"
    source.write_text(json.dumps(matrix, ensure_ascii=False))
    destination = root / target
    before = destination.read_bytes()
    with pytest.raises(SystemExit) as error:
        checker().main(
            [
                "--matrix",
                str(source),
                "--root",
                str(root),
                "--evidence-root",
                str(root),
                "--output",
                str(destination),
            ]
        )
    assert error.value.code == 2
    assert destination.read_bytes() == before


def test_symlink_reference_cannot_escape_repository(
    mapping: tuple[dict[str, Any], Path],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    matrix, root = mapping
    outside = tmp_path_factory.mktemp("outside") / "outside.py"
    outside.write_text("def recover():\n    return 'original'\n")
    (root / "linked.py").symlink_to(outside)
    condition(matrix)["implementation_refs"][0]["path"] = "linked.py"
    assert "reference_invalid" in codes(matrix, root)


def test_pending_dependency_cannot_be_declared_accepted(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    pass_condition(matrix)
    condition(matrix)["implementation_status"] = "dependency_pending"
    assert "evidence_insufficient" in codes(matrix, root)


def test_tracked_inventory_preserves_the_original_41_acs_and_semantic_ids() -> None:
    module = checker(fixture_scope=False)
    matrix = json.loads(module.DEFAULT_MATRIX.read_text())
    originals, semantic = module.original_scope((ROOT / matrix["source_document"]).read_text())
    assert len(originals) == 41
    assert {identifier.split("/")[0] for identifier in originals} == {
        f"C{number:02d}" for number in range(9)
    }
    assert {entry["id"] for entry in matrix["entries"]} == set(originals)
    assert (
        digest(
            json.dumps(
                [originals, semantic],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        == module.FROZEN_SCOPE_SHA256
    )
    assert {
        item["id"] for entry in matrix["entries"] for item in entry["semantic_conditions"]
    } == set(semantic)
    for entry in matrix["entries"]:
        assert entry["source_ac"] == originals[entry["id"]]
        assert entry["source_sha256"] == digest(entry["source_ac"])


def test_run_manifest_cannot_be_rebound_to_modified_uncommitted_source(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    pass_condition(matrix)
    (root / "implementation.py").write_text("def recover():\n    return 'not-tested'\n")
    for key in ("implementation_refs", "consumer_refs", "test_refs"):
        condition(matrix)[key][0]["sha256"] = digest((root / "implementation.py").read_text())
    assert "evidence_insufficient" in codes(matrix, root)


@pytest.mark.parametrize("field", ["metadata", "source_manifest"])
def test_declared_proof_metadata_must_exist_and_match_its_pinned_bytes(
    mapping: tuple[dict[str, Any], Path],
    field: str,
) -> None:
    matrix, root = mapping
    proof = matrix["evidence_registry"]["run"]
    proof[field] = "absent.json"
    proof[field + "_sha256"] = digest("expected")
    assert "evidence_invalid" in codes(matrix, root)
    (root / "absent.json").write_text("changed")
    assert "evidence_invalid" in codes(matrix, root)


def test_removing_a_condition_from_both_editable_inputs_cannot_shrink_scope(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    module = checker()
    source = root / "source.md"
    original = source.read_text() + "- `C00/AC1/S02`：保留原身份。\n"
    module.FROZEN_SCOPE_SHA256 = digest(
        json.dumps(
            list(module.original_scope(original)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    # Both editable inputs retain only S01; the separately frozen scope still includes S02.
    assert "scope_digest" in {item["code"] for item in module.validate(matrix, root, root)}


def test_malformed_entries_do_not_allow_report_to_overwrite_evidence(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    matrix["entries"] = [{}]
    source = root / "map.json"
    source.write_text(json.dumps(matrix))
    output = root / "result.json"
    before = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        checker().main(
            [
                "--matrix",
                str(source),
                "--root",
                str(root),
                "--evidence-root",
                str(root),
                "--output",
                str(output),
            ]
        )
    assert error.value.code == 2
    assert output.read_bytes() == before


def test_hardlink_alias_cannot_overwrite_evidence(
    mapping: tuple[dict[str, Any], Path],
) -> None:
    matrix, root = mapping
    source = root / "map.json"
    source.write_text(json.dumps(matrix))
    output = root / "alias.json"
    output.hardlink_to(root / "result.json")
    before = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        checker().main(
            [
                "--matrix",
                str(source),
                "--root",
                str(root),
                "--evidence-root",
                str(root),
                "--output",
                str(output),
            ]
        )
    assert error.value.code == 2
    assert output.read_bytes() == before


@pytest.mark.parametrize("head", ["", "main", "1234567", None])
def test_floating_or_absent_version_identity_cannot_prove_acceptance(
    mapping: tuple[dict[str, Any], Path],
    head: Any,
) -> None:
    matrix, root = mapping
    pass_condition(matrix)
    matrix["head_sha"] = head
    matrix["evidence_registry"]["run"]["source_head"] = head
    assert "version_invalid" in codes(matrix, root)
