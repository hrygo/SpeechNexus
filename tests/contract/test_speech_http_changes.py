"""Explicit full speech selection must retain the independent existing policies."""

from __future__ import annotations

import copy
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from tests.contract.test_http_contract_changes import ROOT, _git, _write, document, kernel
from tests.contract.test_http_contract_changes import repository as repository

SOURCE = "contracts/speech-openapi.yaml"
NEXUS = "/v1/nexus/assets"
STANDARD = "/v1/audio/speech"


@pytest.fixture
def speech_repository(repository: tuple[ModuleType, Path, str]) -> tuple[ModuleType, Path, str]:
    module, root, _ = repository
    request = {"type": "object", "properties": {"nexus": {"type": "string", "enum": ["a", "b"]}}}
    response = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
    speech = document(NEXUS, copy.deepcopy(request), copy.deepcopy(response))
    speech["paths"].update(
        document(STANDARD, copy.deepcopy(request), copy.deepcopy(response))["paths"]
    )
    _write(root / SOURCE, speech)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Synthetic speech baseline")
    return module, root, _git(root, "rev-parse", "HEAD")


def _change(root: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    value = yaml.safe_load((root / SOURCE).read_text())
    mutate(value)
    _write(root / SOURCE, value)


def test_actual_asset_path_removal_is_detected_without_claiming_the_whole_old_ci_passes(
    speech_repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = speech_repository
    shutil.copytree(ROOT / "contracts", root / "contracts", dirs_exist_ok=True)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Actual frozen contract source baseline")
    base = _git(root, "rev-parse", "HEAD")
    original = yaml.safe_load((root / SOURCE).read_text())
    report = module.inspect_sources(base, surface="speech")
    assert report["findings"] == [] and len(report["sources"]) == 1
    assert report["sources"][0]["paths"] == len(original["paths"])
    assert report["sources"][0]["base_version"] == original["info"]["version"]
    changed = copy.deepcopy(original)
    assert NEXUS in changed["paths"]
    del changed["paths"][NEXUS]
    _write(root / SOURCE, changed)
    legacy = kernel()
    vars(legacy)["_ROOT"] = root
    assert legacy._compare_openapi(original, changed, SOURCE, base_git_ref=base) == []
    assert module.inspect_sources(base)["findings"] == []
    assert any(
        NEXUS in x and "path removed" in x
        for x in module.inspect_sources(base, surface="speech")["findings"]
    )


def test_nexus_path_removal_requires_explicit_speech_selection(
    speech_repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = speech_repository
    _change(root, lambda d: d["paths"].pop(NEXUS))
    assert module.inspect_sources(base)["findings"] == []
    assert any(
        "path removed" in x for x in module.inspect_sources(base, surface="speech")["findings"]
    )


def test_speech_json_names_all_paths_and_limits_and_default_remains_four_sources(
    speech_repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, base = speech_repository
    assert module.inspect_sources(base) == module.inspect_sources(base, surface="nonstandard")
    assert len(module.inspect_sources(base)["sources"]) == 4
    monkeypatch.setattr(
        sys, "argv", ["check", "--base", base, "--surface", "speech", "--format", "json"]
    )
    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "known-speech-http-structure"
    assert report["baseline"] == base and report["findings"] == []
    assert report["sources"] == [
        {
            "path": SOURCE,
            "base_version": "0.1.0.dev0",
            "current_version": "0.1.0.dev0",
            "paths": 2,
            "findings": 0,
        }
    ]
    assert any("serialization" in x for x in report["unverified"])
    assert any("qualification" in x for x in report["unverified"])


def test_full_speech_checks_nexus_on_standard_paths_without_changing_the_standard_policy(
    speech_repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = speech_repository
    original = yaml.safe_load((root / SOURCE).read_text())
    _change(
        root,
        lambda d: d["paths"][STANDARD]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]["properties"]["nexus"].update(enum=["a"]),
    )
    changed = yaml.safe_load((root / SOURCE).read_text())
    assert kernel()._compare_openapi(original, changed, SOURCE) == []
    assert any(
        STANDARD in x and "nexus" in x
        for x in module.inspect_sources(base, surface="speech")["findings"]
    )


@pytest.mark.parametrize("direction", ["request", "response"])
def test_selected_speech_checks_both_request_narrowing_and_lost_response_guarantees(
    speech_repository: tuple[ModuleType, Path, str],
    direction: str,
) -> None:
    module, root, base = speech_repository

    def update(d: dict[str, Any]) -> None:
        operation = d["paths"][NEXUS]["post"]
        if direction == "request":
            operation["requestBody"]["content"]["application/json"]["schema"]["required"] = [
                "nexus"
            ]
        else:
            operation["responses"]["201"]["content"]["application/json"]["schema"]["required"] = []

    _change(root, update)
    assert module.inspect_sources(base, surface="speech")["findings"]


@pytest.mark.parametrize("change", ["annotation", "new-path"])
def test_positive_changes_in_selected_speech_do_not_create_false_breaks(
    speech_repository: tuple[ModuleType, Path, str],
    change: str,
) -> None:
    module, root, base = speech_repository

    def update(d: dict[str, Any]) -> None:
        if change == "annotation":
            d["paths"][NEXUS]["post"]["description"] = "Synthetic documentation."
        else:
            d["paths"]["/v1/nexus/synthetic"] = copy.deepcopy(d["paths"][NEXUS])

    _change(root, update)
    assert module.inspect_sources(base, surface="speech")["findings"] == []


def test_transitive_field_changes_are_pinned_and_a_version_bump_is_not_a_waiver(
    speech_repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = speech_repository
    _change(
        root,
        lambda d: d["paths"][NEXUS]["post"]["requestBody"]["content"]["application/json"].update(
            schema={"$ref": "shared/synthetic.schema.json#/$defs/Input"}
        ),
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Synthetic referenced speech baseline")
    base = _git(root, "rev-parse", "HEAD")
    external = root / "contracts/shared/synthetic.schema.json"
    value = json.loads(external.read_text())
    value["$defs"]["Input"]["properties"]["nexus"]["enum"] = ["a"]
    _write(external, value)
    _change(root, lambda d: d["info"].update(version="0.2.0.dev0"))
    report = module.inspect_sources(base, surface="speech")
    assert report["sources"][0]["current_version"] == "0.2.0.dev0"
    assert any("nexus" in x and "enum" in x for x in report["findings"])


@pytest.mark.parametrize(
    "fault", ["current-missing", "baseline-missing", "invalid-yaml", "missing-version"]
)
def test_speech_source_failures_cannot_fall_back_to_four_other_sources(
    speech_repository: tuple[ModuleType, Path, str],
    fault: str,
) -> None:
    module, root, base = speech_repository
    source = root / SOURCE
    if fault == "current-missing":
        source.unlink()
    elif fault == "baseline-missing":
        text = source.read_text()
        source.unlink()
        _git(root, "add", ".")
        _git(root, "commit", "-m", "Synthetic absent source baseline")
        base = _git(root, "rev-parse", "HEAD")
        source.write_text(text)
    elif fault == "invalid-yaml":
        source.write_text("[")
    else:
        _change(root, lambda d: d["info"].pop("version"))
    assert module.inspect_sources(base, surface="speech")["findings"]


@pytest.mark.parametrize(
    "reference",
    [
        "https://outside.invalid/schema",
        "#/components/schemas/Absent",
        "../outside.json",
        "#/components/schemas/Recursive",
    ],
)
def test_unprovable_speech_reference_is_rejected_even_when_unchanged(
    speech_repository: tuple[ModuleType, Path, str],
    reference: str,
) -> None:
    module, root, _ = speech_repository

    def update(d: dict[str, Any]) -> None:
        d["paths"][NEXUS]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
            "$ref": reference
        }
        d["components"] = {"schemas": {"Recursive": {"$ref": reference}}}

    _change(root, update)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Synthetic unsupported speech reference")
    assert module.inspect_sources(_git(root, "rev-parse", "HEAD"), surface="speech")["findings"]


def test_moving_baseline_cannot_hide_removed_nexus_path(
    speech_repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, root, base = speech_repository
    load: Callable[[], ModuleType] = module._kernel

    def move() -> ModuleType:
        _change(root, lambda d: d["paths"].pop(NEXUS))
        _git(root, "add", ".")
        _git(root, "commit", "-m", "Synthetic moving speech ref")
        return load()

    monkeypatch.setattr(module, "_kernel", move)
    report = module.inspect_sources("HEAD", surface="speech")
    assert report["baseline"] == base != _git(root, "rev-parse", "HEAD")
    assert any("path removed" in x for x in report["findings"])


def test_speech_text_cli_rejects_removed_path_and_unknown_surface(
    speech_repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, root, base = speech_repository
    _change(root, lambda d: d["paths"].pop(NEXUS))
    monkeypatch.setattr(sys, "argv", ["check", "--base", base, "--surface", "speech"])
    assert module.main() == 1
    assert "rejected" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["check", "--base", base, "--surface", "unknown"])
    with pytest.raises(SystemExit, match="2"):
        module.main()


def test_speech_make_target_and_existing_ci_job_require_an_explicit_baseline() -> None:
    import subprocess

    result = subprocess.run(
        ["make", "contracts-speech-changes"],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "CONTRACT_BASE_REF is required" in result.stderr
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"][
        "python_contracts"
    ]["steps"]
    selected = [s for s in steps if s.get("run") == "make contracts-speech-changes"]
    assert len(selected) == 1 and selected[0]["if"] == "github.event_name == 'pull_request'"
    assert selected[0]["env"] == {"CONTRACT_BASE_REF": "origin/${{ github.base_ref }}"}
