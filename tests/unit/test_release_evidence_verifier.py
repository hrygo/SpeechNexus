"""The release-evidence verifier must judge the definition it is given, not only the snapshot."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "deploy" / "engines" / "file-asr-audio" / "evidence" / "gb10-20261027"
CANDIDATE = EVIDENCE / "binding-definition.candidate.json"
SNAPSHOT = ROOT / "tests" / "fixtures" / "release-evidence" / "gb10-20261027-db-snapshot.json"
CAPABILITY_SCHEMA = ROOT / "contracts" / "shared" / "capability-domains.schema.json"
CAPACITY_ID = "capm-file-asr-qwen3-1p7b-gb10-20261027"


def _load_verifier() -> ModuleType:
    path = ROOT / "scripts" / "dev-env" / "verify-release-evidence.py"
    spec = importlib.util.spec_from_file_location("verify_release_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERIFIER = _load_verifier()
MAIN = cast(Callable[[list[str] | None], int], VERIFIER.main)


def _run(
    capsys: pytest.CaptureFixture[str], definition: Path, snapshot: Path = SNAPSHOT
) -> tuple[int, str]:
    argv = [
        "--manifest",
        str(EVIDENCE / "capacity-manifest.json"),
        "--report",
        str(EVIDENCE / "qualification-report.json"),
        "--evidence",
        str(EVIDENCE / "evidence.json"),
        "--expect-manifest-id",
        CAPACITY_ID,
        "--expect-report-id",
        "qrep-file-asr-qwen3-1p7b-gb10-20261027",
        "--artifact-pin",
        str(ROOT / "deploy" / "engines" / "file-asr-audio" / "artifact-pin.json"),
        "--binding-definition",
        str(definition),
        "--database-snapshot",
        str(snapshot),
        "--require-published",
        "--require-digests",
    ]
    code = MAIN(argv)
    return code, capsys.readouterr().out


def _definition() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CANDIDATE.read_text(encoding="utf-8")))


def _write_json(tmp_path: Path, name: str, value: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def test_release_tiers_match_the_frozen_contract() -> None:
    schema = cast(dict[str, Any], json.loads(CAPABILITY_SCHEMA.read_text(encoding="utf-8")))
    definitions = cast(dict[str, Any], schema["$defs"])
    release_tier = cast(dict[str, Any], definitions["ReleaseTier"])

    assert set(cast(list[str], release_tier["enum"])) == cast(
        frozenset[str], VERIFIER.RELEASE_TIERS
    )


def test_candidate_definition_passes_semantic_checks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, output = _run(capsys, CANDIDATE)

    assert code == 0
    assert f"PASS binding_definition.capacity_manifest_id: {CAPACITY_ID}" in output
    assert f"PASS definition.capacity_manifest_id: {CAPACITY_ID}" in output
    assert "PASS binding_definition.release_tier: experimental" in output
    assert "PASS binding_definition.stage_roles: asr_batch" in output
    assert "\nRESULT PASS" in output


def test_missing_capacity_manifest_id_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = _definition()
    del cast(dict[str, Any], definition["execution_recipe"])["capacity_manifest_id"]

    code, output = _run(capsys, _write_json(tmp_path, "missing.json", definition))

    assert code == 1
    assert "FAIL binding_definition.capacity_manifest_id: missing" in output
    assert "RESULT FAIL" in output


def test_capacity_manifest_id_mismatch_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = _definition()
    cast(dict[str, Any], definition["execution_recipe"])["capacity_manifest_id"] = (
        "capm-file-asr-qwen3-1p7b-other"
    )

    code, output = _run(capsys, _write_json(tmp_path, "mismatch.json", definition))

    assert code == 1
    assert (
        "FAIL binding_definition.capacity_manifest_id: declares "
        f"capm-file-asr-qwen3-1p7b-other, expected {CAPACITY_ID}"
    ) in output
    assert "RESULT FAIL" in output


def test_tampered_definition_digest_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = _definition()
    parameter_defaults = cast(dict[str, Any], definition["parameter_defaults"])
    cast(dict[str, Any], parameter_defaults["asr"])["preview_interval_ms"] = 501

    code, output = _run(capsys, _write_json(tmp_path, "tampered.json", definition))

    assert code == 1
    assert "PASS binding_definition.max_segment: 30000" in output
    assert "FAIL digest.config_bundle: expected" in output
    assert "RESULT FAIL" in output


def test_tampered_instance_config_digest_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = cast(dict[str, Any], json.loads(SNAPSHOT.read_text(encoding="utf-8")))
    instances = cast(list[dict[str, Any]], snapshot["engine_instances"])
    instances[0]["config_digest"] = "sha256:" + "0" * 64

    code, output = _run(capsys, CANDIDATE, _write_json(tmp_path, "snapshot.json", snapshot))

    assert code == 1
    assert "FAIL capacity.instance[ei-file-asr-qwen3-1p7b-gb10-01].config_digest:" in output
    assert "RESULT FAIL" in output


def test_missing_execution_recipe_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    definition = _definition()
    del definition["execution_recipe"]

    code, output = _run(capsys, _write_json(tmp_path, "no-recipe.json", definition))

    assert code == 1
    assert "FAIL binding_definition.execution_recipe: missing execution_recipe" in output
    assert "RESULT FAIL" in output


def test_other_definition_semantics_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = _definition()
    recipe = cast(dict[str, Any], definition["execution_recipe"])
    recipe["capacity_manifest_id"] = "local-test-capacity-1"
    recipe["stage_roles"] = ["asr_preview"]
    definition["release_tier"] = "best_effort"
    cast(dict[str, Any], cast(dict[str, Any], definition["parameter_domains"])["asr"])[
        "max_segment_ms"
    ] = 29000

    code, output = _run(capsys, _write_json(tmp_path, "semantics.json", definition))

    assert code == 1
    assert "FAIL binding_definition.capacity_manifest_id: declares local-test-capacity-1" in output
    assert "FAIL binding_definition.placeholder:" in output
    assert (
        "FAIL binding_definition.release_tier: definition=best_effort, manifest=experimental"
        in output
    )
    assert "FAIL binding_definition.stage_roles: definition=['asr_preview']" in output
    assert "PASS binding_definition.tasks: asr_file" in output
    assert "FAIL binding_definition.max_segment: definition=29000, manifest=30000" in output
    assert "RESULT FAIL" in output


def test_missing_definition_release_tier_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = _definition()
    del definition["release_tier"]

    code, output = _run(capsys, _write_json(tmp_path, "no-tier.json", definition))

    assert code == 1
    assert "WARN binding_definition.release_tier: definition has no release_tier" in output
    assert "FAIL digest.config_bundle: expected" in output
    assert "RESULT FAIL" in output


def test_unrecognized_definition_release_tier_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    definition = _definition()
    definition["release_tier"] = "preview"

    code, output = _run(capsys, _write_json(tmp_path, "tier.json", definition))

    assert code == 1
    assert (
        "FAIL binding_definition.release_tier: unrecognized definition release_tier=preview"
        in output
    )
    assert "RESULT FAIL" in output
