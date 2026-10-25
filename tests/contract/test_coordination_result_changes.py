"""Result guarantees need their own direction; raw request defaults stay fixed."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# Both surfaces use the same actual-source temporary Git fixture, not wire copies.
from tests.contract.test_coordination_request_changes import (
    BINDINGS,
    ROOT,
    _change,
    _git,
)
from tests.contract.test_coordination_request_changes import (
    repository as repository,
)
from tests.support.contracts import validator


def test_scope_id_guarantee_loss_accepts_previously_invalid_result_and_is_rejected(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = repository
    original = validator("internal/coordination-transactions.schema.json", "TransactionResult")
    assert isinstance(original.schema, dict)
    weakened = copy.deepcopy(original.schema)
    weakened["required"].remove("scope_id")
    changed_validator = original.evolve(schema=weakened)
    examples = json.loads(
        (ROOT / "tests/fixtures/coordination/transaction-examples.json").read_text()
    )
    example = copy.deepcopy(examples["valid_results"][0])
    assert original.is_valid(example)
    del example["scope_id"]
    assert not original.is_valid(example) and changed_validator.is_valid(example)
    _change(root, lambda d: d["$defs"]["TransactionResult"]["required"].remove("scope_id"))
    assert module.inspect_source(base)["findings"] == [], "Request scope remains unchanged"
    findings = module.inspect_source(base, surface="result")["findings"]
    assert any("scope_id" in x and "guarantees removed" in x for x in findings)


def test_same_source_result_selection_reports_its_scope_and_default_requests_stay_equal(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, base = repository
    assert module.inspect_source(base) == module.inspect_source(base, surface="requests")
    monkeypatch.setattr(
        sys, "argv", ["check", "--base", base, "--surface", "result", "--format", "json"]
    )
    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["baseline"] == base and report["findings"] == []
    assert report["scope"] == "known-coordination-result-entrypoint-structure"
    assert report["entrypoints"] == ["TransactionResult"]
    assert report["direction"] == "response"
    assert report["versions"] == {
        "base": {"schema": "1.0.0", "binding": "1.0.0"},
        "current": {"schema": "1.0.0", "binding": "1.0.0"},
    }
    assert any("request" in x for x in report["unverified"])
    assert any("envelope" in x for x in report["unverified"])


@pytest.mark.parametrize(
    "change", ["annotation", "stronger-required", "narrower-output", "request-only", "mixed-root"]
)
def test_positive_or_unselected_changes_do_not_claim_full_compatibility(
    repository: tuple[ModuleType, Path, str],
    change: str,
) -> None:
    module, root, base = repository

    def update(d: dict[str, Any]) -> None:
        result = d["$defs"]["TransactionResult"]
        if change == "annotation":
            result["description"] = "Synthetic annotation."
        elif change == "stronger-required":
            result["required"].append("version")
        elif change == "narrower-output":
            result["properties"]["outcome"]["enum"].remove("unknown")
        elif change == "request-only":
            d["$defs"]["create_job"]["required"].append("synthetic_required")
        else:
            d["oneOf"][0]["required"].append("synthetic_envelope_field")

    _change(root, update)
    report = module.inspect_source(base, surface="result")
    assert report["findings"] == []
    assert report["unverified"], "This is only a structural result check, not policy qualification"


@pytest.mark.parametrize(
    "change",
    ["required", "enum", "type", "additional-properties", "conditional", "exclusive-variant"],
)
def test_known_response_guarantee_changes_are_detected(
    repository: tuple[ModuleType, Path, str],
    change: str,
) -> None:
    module, root, base = repository

    def update(d: dict[str, Any]) -> None:
        result = d["$defs"]["TransactionResult"]
        if change == "required":
            result["required"].remove("recovery")
        elif change == "enum":
            result["properties"]["outcome"]["enum"].append("synthetic_new_outcome")
        elif change == "type":
            result["type"] = ["object", "null"]
        elif change == "additional-properties":
            result["additionalProperties"] = True
        elif change == "conditional":
            result["allOf"][0]["then"]["required"] = []
        else:
            result["properties"]["value"]["oneOf"].append({"type": "null"})

    _change(root, update)
    assert module.inspect_source(base, surface="result")["findings"]


def test_widened_external_identifier_is_detected_across_a_version_bump(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = repository
    _change(
        root,
        lambda d: d["$defs"]["Identifier"].update(minLength=0),
        "contracts/shared/identifiers.schema.json",
    )
    _change(root, lambda d: d.update({"x-speechnexus-schema-version": "1.1.0"}))
    _change(root, lambda d: d["coordination"].update(version="1.1.0"), BINDINGS)
    report = module.inspect_source(base, surface="result")
    assert report["versions"]["current"] == {"schema": "1.1.0", "binding": "1.1.0"}
    assert any("scope_id" in x and "minLength" in x for x in report["findings"])


@pytest.mark.parametrize(
    "fault", ["remote", "pointer", "escape", "recursive", "invalid-transitive", "remapped"]
)
def test_unprovable_result_inputs_never_become_an_unchanged_equality_pass(
    repository: tuple[ModuleType, Path, str],
    fault: str,
) -> None:
    module, root, _ = repository
    if fault == "remapped":
        _change(
            root,
            lambda d: d["coordination"].update(
                schema_refs={"TransactionResult": "#/$defs/create_job"}
            ),
            BINDINGS,
        )
    elif fault == "invalid-transitive":
        _change(
            root,
            lambda d: d["$defs"]["Identifier"].update(type=123),
            "contracts/shared/identifiers.schema.json",
        )
    else:
        ref = {
            "remote": "https://outside.invalid/schema",
            "pointer": "#/$defs/Absent",
            "escape": "../../outside.json",
            "recursive": "#/$defs/TransactionResult",
        }[fault]
        _change(
            root,
            lambda d: d["$defs"]["TransactionResult"]["properties"].update(scope_id={"$ref": ref}),
        )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic unsupported result baseline")
    assert module.inspect_source(_git(root, "rev-parse", "HEAD"), surface="result")["findings"]


def test_moving_git_ref_does_not_hide_a_response_guarantee_loss(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, root, base = repository
    load: Callable[[], ModuleType] = module._kernel

    def move() -> ModuleType:
        _change(
            root,
            lambda d: d["$defs"]["Identifier"].update(minLength=0),
            "contracts/shared/identifiers.schema.json",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "Synthetic moving result baseline")
        return load()

    monkeypatch.setattr(module, "_kernel", move)
    report = module.inspect_source("HEAD", surface="result")
    assert report["baseline"] == base != _git(root, "rev-parse", "HEAD")
    assert report["findings"]


def test_boolean_reference_sibling_guarantee_cannot_disappear(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = repository

    def setup(d: dict[str, Any]) -> None:
        d["$defs"]["Any"] = True
        d["$defs"]["TransactionResult"]["properties"]["scope_id"] = {
            "$ref": "#/$defs/Any",
            "type": "string",
        }

    _change(root, setup)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic boolean result baseline")
    base = _git(root, "rev-parse", "HEAD")
    _change(root, lambda d: d["$defs"]["TransactionResult"]["properties"]["scope_id"].pop("type"))
    assert any("scope_id" in x for x in module.inspect_source(base, surface="result")["findings"])


def test_result_text_cli_rejects_lost_guarantee_and_unknown_surface_is_not_a_pass(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, root, base = repository
    _change(root, lambda d: d["$defs"]["TransactionResult"]["required"].remove("scope_id"))
    monkeypatch.setattr(sys, "argv", ["check", "--base", base, "--surface", "result"])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "Coordination result baseline" in output and "rejected" in output
    monkeypatch.setattr(sys, "argv", ["check", "--base", base, "--surface", "unknown"])
    with pytest.raises(SystemExit, match="2"):
        module.main()


def test_result_make_target_requires_baseline_and_ci_keeps_an_explicit_surface() -> None:
    import yaml

    result = subprocess.run(
        ["make", "contracts-coordination-results"],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "CONTRACT_BASE_REF is required" in result.stderr
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    selected = [
        x
        for x in jobs["python_contracts"]["steps"]
        if x.get("run") == "make contracts-coordination-results"
    ]
    assert len(selected) == 1 and selected[0]["if"] == "github.event_name == 'pull_request'"
    assert selected[0]["env"] == {"CONTRACT_BASE_REF": "origin/${{ github.base_ref }}"}
