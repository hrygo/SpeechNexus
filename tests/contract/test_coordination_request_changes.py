"""Directional request entrypoints must not disappear behind the mixed root union."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.support.contracts import validator

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/internal/coordination-transactions.schema.json"
BINDINGS = "contracts/bindings.json"


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Synthetic contract test",
            "-c",
            "user.email=synthetic@example.invalid",
            *args,
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _change(root: Path, update: Callable[[dict[str, Any]], None], path: str = SOURCE) -> None:
    file = root / path
    value = json.loads(file.read_text())
    update(value)
    _write(file, value)


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path, str]:
    spec = importlib.util.spec_from_file_location(
        "coordination_request_cli", ROOT / "scripts/dev-env/check-coordination-request-changes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_ROOT", tmp_path)
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Synthetic baseline")
    return module, tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def test_existing_create_job_example_rejects_narrowed_digest_and_gate_reports_it(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = repository
    examples = json.loads(
        (ROOT / "tests/fixtures/coordination/transaction-examples.json").read_text()
    )
    request = next(
        c["request"] for c in examples["valid_commands"] if c["operation"] == "create_job"
    )
    contract = validator("internal/coordination-transactions.schema.json", "create_job")
    assert contract.is_valid(request)
    assert isinstance(contract.schema, dict)
    schema = dict(contract.schema)
    schema["properties"] = {**schema["properties"], "request_digest": {"maxLength": 1}}
    assert not contract.evolve(schema=schema).is_valid(request)
    _change(
        root, lambda d: d["$defs"]["create_job"]["properties"]["request_digest"].update(maxLength=1)
    )
    report = module.inspect_source(base)
    assert any("create_job" in x and "maxLength" in x for x in report["findings"])


def test_actual_same_source_reports_explicit_versions_18_requests_and_unverified_results(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, base = repository
    monkeypatch.setattr(sys, "argv", ["check", "--base", base, "--format", "json"])
    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["baseline"] == base and report["findings"] == []
    assert report["source"] == SOURCE and len(report["entrypoints"]) == 18
    assert "TransactionResult" not in report["entrypoints"]
    assert report["versions"] == {
        "base": {"schema": "1.0.0", "binding": "1.0.0"},
        "current": {"schema": "1.0.0", "binding": "1.0.0"},
    }
    assert any("TransactionResult" in x for x in report["unverified"])
    assert any("envelope" in x for x in report["unverified"])


@pytest.mark.parametrize(
    "change", ["annotation", "request-widening", "result-only", "root-envelope"]
)
def test_unchanged_or_out_of_scope_parts_do_not_acquire_a_false_compatibility_claim(
    repository: tuple[ModuleType, Path, str],
    change: str,
) -> None:
    module, root, base = repository

    def update(d: dict[str, Any]) -> None:
        if change == "annotation":
            d["$defs"]["create_job"]["description"] = "Synthetic description."
        elif change == "request-widening":
            d["$defs"]["create_job"]["required"].remove("request_digest")
        elif change == "result-only":
            d["$defs"]["TransactionResult"]["properties"]["synthetic_output"] = {"type": "string"}
        else:
            d["oneOf"][0]["required"].append("synthetic_envelope_field")

    _change(root, update)
    report = module.inspect_source(base)
    assert report["findings"] == []
    assert report["scope"] == "known-coordination-request-entrypoint-structure"
    assert report["unverified"], "These cases do not prove the mixed root or result is compatible"


def test_frozen_external_identifier_changes_are_checked_even_with_matching_version_bump(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = repository
    _change(
        root,
        lambda d: d["$defs"]["Sha256Digest"].update(maxLength=1),
        "contracts/shared/identifiers.schema.json",
    )
    _change(root, lambda d: d.update({"x-speechnexus-schema-version": "1.1.0"}))
    _change(root, lambda d: d["coordination"].update(version="1.1.0"), BINDINGS)
    report = module.inspect_source(base)
    assert any("request_digest" in x and "maxLength" in x for x in report["findings"])
    assert report["versions"]["current"] == {"schema": "1.1.0", "binding": "1.1.0"}


@pytest.mark.parametrize(
    "fault",
    [
        "source",
        "binding",
        "definition",
        "schema-version",
        "binding-version",
        "binding-source",
        "binding-entries",
        "invalid-schema",
        "duplicate-key",
        "identity",
    ],
)
def test_incomplete_or_invalid_metadata_and_sources_never_pass(
    repository: tuple[ModuleType, Path, str],
    fault: str,
) -> None:
    module, root, base = repository
    if fault in {"source", "binding"}:
        (root / (SOURCE if fault == "source" else BINDINGS)).unlink()
    elif fault == "definition":
        _change(root, lambda d: d["$defs"].pop("create_job"))
    elif fault == "schema-version":
        _change(root, lambda d: d.pop("x-speechnexus-schema-version"))
    elif fault == "binding-version":
        _change(root, lambda d: d["coordination"].update(version="wrong"), BINDINGS)
    elif fault == "binding-source":
        _change(root, lambda d: d["coordination"].update(source="another.schema.json"), BINDINGS)
    elif fault == "binding-entries":
        _change(root, lambda d: d["coordination"]["schemas"].remove("create_job"), BINDINGS)
    elif fault == "invalid-schema":
        _change(root, lambda d: d["$defs"]["create_job"].update(required="request_digest"))
    elif fault == "identity":
        _change(root, lambda d: d.update({"$id": "https://changed.invalid/coordination"}))
    else:
        (root / SOURCE).write_text('{"$defs":{},"$defs":{}}')
    assert module.inspect_source(base)["findings"]


@pytest.mark.parametrize("path", [SOURCE, BINDINGS])
def test_missing_frozen_baseline_file_is_not_a_new_source_waiver(
    repository: tuple[ModuleType, Path, str],
    path: str,
) -> None:
    module, root, _ = repository
    file = root / path
    text = file.read_text()
    file.unlink()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic incomplete baseline")
    base = _git(root, "rev-parse", "HEAD")
    file.write_text(text)
    assert module.inspect_source(base)["findings"]


@pytest.mark.parametrize("fault", ["remote", "escape", "missing-pointer", "recursive"])
def test_unchanged_unprovable_reference_is_rejected(
    repository: tuple[ModuleType, Path, str],
    fault: str,
) -> None:
    module, root, _ = repository
    refs = {
        "remote": "https://outside.invalid/schema",
        "escape": "../../outside.json",
        "missing-pointer": "#/$defs/Absent",
        "recursive": "#/$defs/create_job",
    }
    _change(
        root,
        lambda d: d["$defs"]["create_job"]["properties"].update(
            request_digest={"$ref": refs[fault]}
        ),
    )
    _write(root / "outside.json", {"type": "string"})
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic unsupported reference baseline")
    assert module.inspect_source(_git(root, "rev-parse", "HEAD"))["findings"]


def test_moving_baseline_does_not_hide_transitive_schema_change(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, root, base = repository
    load: Callable[[], ModuleType] = module._kernel

    def move_head() -> ModuleType:
        _change(
            root,
            lambda d: d["$defs"]["Sha256Digest"].update(maxLength=1),
            "contracts/shared/identifiers.schema.json",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "Synthetic moving ref")
        return load()

    monkeypatch.setattr(module, "_kernel", move_head)
    report = module.inspect_source("HEAD")
    assert report["baseline"] == base != _git(root, "rev-parse", "HEAD")
    assert report["findings"]


def test_bad_base_is_an_error_and_breaking_change_sets_text_cli_exit_status(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, root, base = repository
    monkeypatch.setattr(sys, "argv", ["check", "--base", "missing-synthetic-ref"])
    assert module.main() == 2
    assert "baseline or kernel unavailable" in capsys.readouterr().err
    _change(root, lambda d: d["$defs"]["create_job"]["required"].append("new_required"))
    monkeypatch.setattr(sys, "argv", ["check", "--base", base])
    assert module.main() == 1
    assert "rejected" in capsys.readouterr().out


@pytest.mark.parametrize(
    "operation",
    [
        value["operation"]
        for value in json.loads(
            (ROOT / "tests/fixtures/coordination/transaction-examples.json").read_text()
        )["valid_commands"]
    ],
)
def test_every_existing_valid_request_rejects_a_new_required_field(
    repository: tuple[ModuleType, Path, str],
    operation: str,
) -> None:
    module, root, base = repository
    _change(root, lambda d: d["$defs"][operation]["required"].append("synthetic_required"))
    assert any(
        operation in x and "new required fields" in x
        for x in module.inspect_source(base)["findings"]
    )


def test_nexus_is_checked_in_raw_coordination_requests(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = repository
    _change(
        root,
        lambda d: d["$defs"]["create_job"]["properties"].update(
            nexus={"type": "string", "enum": ["synthetic-a", "synthetic-b"]}
        ),
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic Nexus baseline")
    base = _git(root, "rev-parse", "HEAD")
    _change(
        root, lambda d: d["$defs"]["create_job"]["properties"]["nexus"].update(enum=["synthetic-a"])
    )
    assert any("nexus" in x and "enum" in x for x in module.inspect_source(base)["findings"])


def test_boolean_reference_sibling_narrowing_remains_visible(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = repository

    def baseline(d: dict[str, Any]) -> None:
        d["$defs"]["Any"] = True
        d["$defs"]["create_job"]["properties"]["request_digest"] = {"$ref": "#/$defs/Any"}

    _change(root, baseline)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic boolean baseline")
    base = _git(root, "rev-parse", "HEAD")
    _change(
        root,
        lambda d: d["$defs"]["create_job"]["properties"]["request_digest"].update(type="string"),
    )
    assert any("request_digest" in x for x in module.inspect_source(base)["findings"])


def test_new_operation_does_not_silently_expand_the_frozen_entrypoint_inventory(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = repository

    def update(d: dict[str, Any]) -> None:
        d["$defs"]["synthetic_new_port"] = {"type": "object"}
        d["oneOf"].append({"properties": {"operation": {"const": "synthetic_new_port"}}})

    _change(root, update)
    _change(root, lambda d: d["coordination"]["schemas"].append("synthetic_new_port"), BINDINGS)
    assert module.inspect_source(base)["findings"]


@pytest.mark.parametrize("key", ["schema_refs", "schema_unions"])
def test_runtime_entrypoint_remapping_cannot_leave_the_gate_comparing_unused_definitions(
    repository: tuple[ModuleType, Path, str],
    key: str,
) -> None:
    module, root, _ = repository
    override = (
        {"create_job": "#/$defs/claim_job"}
        if key == "schema_refs"
        else {"create_job": {"base": "#", "members": ["#/$defs/create_job"]}}
    )
    _change(root, lambda d: d["coordination"].update({key: override}), BINDINGS)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic remapped baseline")
    assert module.inspect_source(_git(root, "rev-parse", "HEAD"))["findings"]


@pytest.mark.parametrize("invalid", [{"type": 123}, {"maxLength": "invalid"}])
def test_invalid_transitive_request_schema_is_not_a_pass_even_when_unchanged(
    repository: tuple[ModuleType, Path, str],
    invalid: dict[str, Any],
) -> None:
    module, root, _ = repository
    _change(
        root,
        lambda d: d["$defs"]["Sha256Digest"].update(invalid),
        "contracts/shared/identifiers.schema.json",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic invalid referenced schema")
    assert module.inspect_source(_git(root, "rev-parse", "HEAD"))["findings"]


def test_make_requires_a_baseline_and_ci_uses_fetched_base_in_existing_job() -> None:
    import yaml

    result = subprocess.run(
        ["make", "contracts-coordination-requests"],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "CONTRACT_BASE_REF is required" in result.stderr
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    steps = jobs["python_checks"]["steps"]
    selected = [step for step in steps if step.get("run") == "make contracts-coordination-requests"]
    assert len(selected) == 1
    assert selected[0]["if"] == "github.event_name == 'pull_request'"
    assert selected[0]["env"] == {"CONTRACT_BASE_REF": "origin/${{ github.base_ref }}"}
    assert jobs["integration_shards"]["strategy"]["matrix"]["shard"] == [0, 1, 2, 3, 4, 5]
