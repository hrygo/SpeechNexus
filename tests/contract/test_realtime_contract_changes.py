"""Actual-source public RTE constraints and generated runtime selection stay visible."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry

from tests.contract.test_coordination_request_changes import _change, _git
from tests.support.contracts import retrieve, validator

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/realtime-events.schema.json"
BINDINGS = "contracts/bindings.json"
SCRIPT = "scripts/dev-env/check-realtime-contract-changes.py"


def _module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("rte_change_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path, str]:
    module = _module(ROOT / SCRIPT)
    monkeypatch.setattr(module, "_ROOT", tmp_path)
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "Synthetic realtime baseline")
    return module, tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _inspect(repository: tuple[ModuleType, Path, str]) -> dict[str, Any]:
    module, _, base = repository
    result: dict[str, Any] = module.inspect_source(base)
    return result


@pytest.mark.parametrize("change", ["nexus-seq", "root-closure"])
def test_real_source_counterexamples_missed_by_standard_policy_are_rejected(
    repository: tuple[ModuleType, Path, str],
    change: str,
) -> None:
    module, root, _ = repository
    original = validator("realtime-events.schema.json")
    assert isinstance(original.schema, dict)
    changed = copy.deepcopy(original.schema)
    frame = copy.deepcopy(changed["examples"][-1])
    assert original.is_valid(frame)

    def update(d: dict[str, Any]) -> None:
        if change == "nexus-seq":
            d["$defs"]["BaseNexusServerEvent"]["required"].remove("seq")
        else:
            d["unevaluatedProperties"] = True

    update(changed)
    if change == "nexus-seq":
        frame.pop("seq")
    else:
        frame["synthetic_extra"] = 1
    independent = Draft202012Validator(changed, registry=cast(Any, Registry)(retrieve=retrieve))
    assert not original.is_valid(frame) and independent.is_valid(frame)
    _change(root, update, SOURCE)
    old = json.loads((ROOT / SOURCE).read_text())
    new = json.loads((root / SOURCE).read_text())
    assert module._kernel()._compare_realtime_events_schema(old, new, SOURCE) == []
    assert _inspect(repository)["findings"]


def test_runtime_projection_removal_changes_actual_acceptance_and_is_not_hidden_by_source_identity(
    repository: tuple[ModuleType, Path, str],
) -> None:
    _, root, _ = repository
    schema = validator("realtime-events.schema.json").schema
    assert isinstance(schema, dict)
    original = copy.deepcopy(schema)
    binding = json.loads((root / BINDINGS).read_text())["profiles"]["realtime_events"]
    members = binding["schema_unions"]["TranscriptionEvent"]["members"]
    original["oneOf"] = [{"$ref": member} for member in members]
    changed = {**original, "oneOf": original["oneOf"][1:]}
    sample = original["examples"][0]
    assert Draft202012Validator(original, registry=cast(Any, Registry)(retrieve=retrieve)).is_valid(
        sample
    )
    assert not Draft202012Validator(
        changed, registry=cast(Any, Registry)(retrieve=retrieve)
    ).is_valid(sample)
    _change(
        root,
        lambda d: d["profiles"]["realtime_events"]["schema_unions"]["TranscriptionEvent"][
            "members"
        ].pop(0),
        BINDINGS,
    )
    assert any("projection" in x for x in _inspect(repository)["findings"])


def test_same_source_inventory_includes_directions_pcm_and_all_conditional_branches(
    repository: tuple[ModuleType, Path, str],
) -> None:
    _, _, base = repository
    report = _inspect(repository)
    assert report["baseline"] == base and report["findings"] == []
    assert report["entrypoints"] == ["RealtimeEvent", "TranscriptionEvent", "OpenAIPcmFormat"]
    inventory = report["inventory"]["base"]
    assert inventory == report["inventory"]["current"]
    assert inventory["binding_version"] == "0.1.0"
    assert len(inventory["events"]) == 33 and len(inventory["transcription_members"]) == 14
    assert sum(e["direction"] == "request" for e in inventory["events"].values()) == 11
    assert sum(e["direction"] == "response" for e in inventory["events"].values()) == 22
    assert inventory["events"]["NexusOperationDone"]["type"] == "nexus.operation.done"
    assert len(json.loads((ROOT / SOURCE).read_text())["$defs"]["NexusOperationDone"]["allOf"]) == 5
    assert any("ordering" in x for x in report["unverified"])
    assert any("interoperability" in x for x in report["unverified"])


@pytest.mark.parametrize(
    "change",
    [
        "client-required",
        "server-required",
        "root-required",
        "root-closure",
        "nexus-field",
        "pcm-rate",
        "pcm-default",
        "conditional",
        "event-type",
        "event-direction",
        "union-removal",
        "union-addition",
        "root-condition",
        "root-type",
        "default-presence",
    ],
)
def test_constraint_changes_are_fenced_without_claiming_directional_inclusion(
    repository: tuple[ModuleType, Path, str],
    change: str,
) -> None:
    _, root, _ = repository

    def update(d: dict[str, Any]) -> None:
        defs = d["$defs"]
        if change == "client-required":
            defs["NexusTtsCancel"]["allOf"][1]["required"].append("synthetic")
        elif change == "server-required":
            defs["BaseNexusServerEvent"]["required"].remove("seq")
        elif change == "root-required":
            d["required"] = ["synthetic"]
        elif change == "root-closure":
            d["unevaluatedProperties"] = True
        elif change == "nexus-field":
            defs["SessionNexusConfig"]["properties"]["synthetic"] = {"type": "string"}
        elif change in {"pcm-rate", "pcm-default"}:
            defs["OpenAIPcmFormat"]["properties"]["rate"][
                "const" if change == "pcm-rate" else "default"
            ] = 16000
        elif change == "conditional":
            defs["NexusOperationDone"]["allOf"][3]["then"]["required"] = []
        elif change == "event-type":
            defs["NexusTtsCancel"]["allOf"][1]["properties"]["type"]["const"] = "nexus.synthetic"
        elif change == "event-direction":
            defs["NexusTtsCancel"]["allOf"][0] = {"$ref": "#/$defs/BaseNexusServerEvent"}
        elif change == "union-removal":
            d["oneOf"].pop()
        elif change == "union-addition":
            defs["SyntheticEvent"] = copy.deepcopy(defs["NexusTtsCancel"])
            defs["SyntheticEvent"]["allOf"][1]["properties"]["type"]["const"] = "nexus.synthetic"
            d["oneOf"].append({"$ref": "#/$defs/SyntheticEvent"})
        elif change == "root-condition":
            d["not"] = {"required": ["synthetic"]}
        elif change == "root-type":
            d["type"] = ["object", "null"]
        else:
            del defs["OpenAIPcmFormat"]["properties"]["rate"]["default"]

    _change(root, update, SOURCE)
    assert _inspect(repository)["findings"]


@pytest.mark.parametrize(
    "change",
    ["annotations", "union-order", "projection-order", "version-only", "unused-definition"],
)
def test_limited_positive_controls_do_not_advertise_general_compatibility(
    repository: tuple[ModuleType, Path, str],
    change: str,
) -> None:
    _, root, _ = repository
    if change in {"projection-order", "version-only"}:

        def update_binding(d: dict[str, Any]) -> None:
            profile = d["profiles"]["realtime_events"]
            if change == "projection-order":
                profile["schema_unions"]["TranscriptionEvent"]["members"].reverse()
            else:
                profile["version"] = "0.2.0"

        _change(root, update_binding, BINDINGS)
    else:

        def update_source(d: dict[str, Any]) -> None:
            if change == "annotations":
                d["description"] = "Synthetic root annotation"
                d["$defs"]["BaseNexusServerEvent"]["properties"]["seq"]["description"] = (
                    "Synthetic description"
                )
                d["$defs"]["NexusOperationDone"]["allOf"][3]["then"]["description"] = (
                    "Synthetic condition description"
                )
            elif change == "union-order":
                d["oneOf"].reverse()
            else:
                d["$defs"]["UnusedSynthetic"] = {"type": "boolean"}

        _change(root, update_source, SOURCE)
    report = _inspect(repository)
    assert report["findings"] == [] and report["unverified"]


def test_version_bump_does_not_waive_transitive_repository_constraint_changes(
    repository: tuple[ModuleType, Path, str],
) -> None:
    _, root, _ = repository
    _change(
        root,
        lambda d: d["$defs"]["TtsOverrides"]["properties"].update(synthetic={"type": "integer"}),
        "contracts/shared/speech-overrides.schema.json",
    )
    _change(root, lambda d: d["profiles"]["realtime_events"].update(version="0.2.0"), BINDINGS)
    report = _inspect(repository)
    assert report["inventory"]["current"]["binding_version"] == "0.2.0"
    assert report["findings"]


@pytest.mark.parametrize("literal", ["const", "enum", "default"])
@pytest.mark.parametrize(
    "value", [True, {"nested": [False, {"$ref": "literal", "description": "data"}]}]
)
def test_literal_json_is_opaque_and_preserves_boolean_number_distinctions(
    repository: tuple[ModuleType, Path, str],
    literal: str,
    value: Any,
) -> None:
    module, root, _ = repository

    def set_value(d: dict[str, Any], val: Any) -> None:
        d["$defs"]["NexusTtsCancel"]["allOf"][1]["properties"]["synthetic"] = {
            literal: [val] if literal == "enum" else val
        }

    _change(root, lambda d: set_value(d, value), SOURCE)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic literal baseline")
    base = _git(root, "rev-parse", "HEAD")
    assert module.inspect_source(base)["findings"] == [], "Literal $ref is not a schema reference"
    changed: Any = (
        1 if value is True else {"nested": [0, {"$ref": "literal", "description": "data"}]}
    )
    _change(root, lambda d: set_value(d, changed), SOURCE)
    assert module.inspect_source(base)["findings"]


@pytest.mark.parametrize(
    "fault",
    [
        "remote",
        "missing-ref",
        "escape-ref",
        "recursive",
        "invalid-transitive",
        "resource-id",
        "nested-id",
        "dynamic-ref",
        "missing-profile",
        "remapped-root",
        "remapped-pcm",
        "duplicate-projection",
        "invalid-projection",
        "duplicate-union",
        "duplicate-type",
        "invalid-direction",
        "missing-source",
        "duplicate-json",
        "non-json-number",
    ],
)
def test_invalid_or_unprovable_inputs_fail_closed_even_when_baseline_matches(
    repository: tuple[ModuleType, Path, str],
    fault: str,
) -> None:
    module, root, _ = repository
    if fault in {
        "missing-profile",
        "remapped-root",
        "remapped-pcm",
        "duplicate-projection",
        "invalid-projection",
    }:

        def update_binding(d: dict[str, Any]) -> None:
            p = d["profiles"]["realtime_events"]
            if fault == "missing-profile":
                del d["profiles"]["realtime_events"]
            elif fault == "remapped-root":
                p["schema_refs"]["RealtimeEvent"] = "#/$defs/SessionUpdate"
            elif fault == "remapped-pcm":
                p["schema_refs"]["OpenAIPcmFormat"] = "#/$defs/SessionUpdate"
            else:
                p["schema_unions"]["TranscriptionEvent"]["members"].append(
                    "#/$defs/SessionUpdate" if fault == "duplicate-projection" else "#/$defs/Absent"
                )

        _change(root, update_binding, BINDINGS)
    elif fault in {"invalid-transitive", "resource-id"}:
        _change(
            root,
            lambda d: (
                d["$defs"]["TtsOverrides"].update(type=123)
                if fault == "invalid-transitive"
                else d.update({"$id": "https://outside.invalid/schema"})
            ),
            "contracts/shared/speech-overrides.schema.json",
        )
    elif fault == "missing-source":
        (root / SOURCE).unlink()
    elif fault in {"duplicate-json", "non-json-number"}:
        (root / SOURCE).write_text(
            '{"type":"object","type":"string"}'
            if fault == "duplicate-json"
            else '{"synthetic":NaN}'
        )
    else:

        def update_source(d: dict[str, Any]) -> None:
            props = d["$defs"]["NexusTtsCancel"]["allOf"][1]["properties"]
            refs = {
                "remote": "https://outside.invalid/schema",
                "missing-ref": "#/$defs/Absent",
                "escape-ref": "../../outside.json",
                "recursive": "#/$defs/NexusTtsCancel",
            }
            if fault in refs:
                props["synthetic"] = {"$ref": refs[fault]}
            elif fault == "nested-id":
                props["synthetic"] = {"$id": "https://outside.invalid/schema", "type": "string"}
            elif fault == "dynamic-ref":
                props["synthetic"] = {"$dynamicRef": "#foo"}
            elif fault == "duplicate-union":
                d["oneOf"].append(d["oneOf"][0])
            elif fault == "duplicate-type":
                props["type"]["const"] = "session.update"
            else:
                d["$defs"]["NexusTtsCancel"]["allOf"][0] = {"$ref": "#/$defs/Absent"}

        _change(root, update_source, SOURCE)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic invalid baseline")
    assert module.inspect_source(_git(root, "rev-parse", "HEAD"))["findings"]


def test_moving_baseline_is_resolved_once_before_any_document_read(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, root, base = repository
    load: Callable[[], ModuleType] = module._kernel

    def move() -> ModuleType:
        _change(
            root, lambda d: d["$defs"]["BaseNexusServerEvent"]["required"].remove("seq"), SOURCE
        )
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "Synthetic moved baseline")
        return load()

    monkeypatch.setattr(module, "_kernel", move)
    report = module.inspect_source("HEAD")
    assert report["baseline"] == base and report["findings"]


def test_real_cli_json_exit_codes_and_exact_baseline(
    repository: tuple[ModuleType, Path, str],
) -> None:
    _, root, base = repository
    directory = root / "scripts/dev-env"
    directory.mkdir(parents=True)
    for name in (Path(SCRIPT).name, "check-openai-contract-compat.py"):
        shutil.copyfile(ROOT / "scripts/dev-env" / name, directory / name)

    def run(ref: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(root / SCRIPT), "--base", ref, "--format", "json"],
            text=True,
            capture_output=True,
            check=False,
        )

    ok = run(base)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["baseline"] == base
    _change(root, lambda d: d.update(unevaluatedProperties=True), SOURCE)
    bad = run(base)
    assert bad.returncode == 1 and json.loads(bad.stdout)["findings"]
    invalid = run("refs/heads/absent")
    assert invalid.returncode == 2 and "baseline or kernel unavailable" in invalid.stderr


def test_decimal_precision_is_not_lost_before_constraint_comparison(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = repository
    _change(
        root,
        lambda d: d["$defs"]["NexusTtsCancel"]["allOf"][1]["properties"].update(
            synthetic={"const": "synthetic_decimal_marker"}
        ),
        SOURCE,
    )
    path = root / SOURCE
    path.write_text(path.read_text().replace('"synthetic_decimal_marker"', "0.1"))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic exact decimal baseline")
    base = _git(root, "rev-parse", "HEAD")
    assert module.inspect_source(base)["findings"] == []
    path.write_text(path.read_text().replace('"const": 0.1', '"const": 0.10000000000000001'))
    assert module.inspect_source(base)["findings"]


def test_literal_description_changes_are_data_changes_not_ignored_annotations(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = repository

    def update(d: dict[str, Any], value: str) -> None:
        d["$defs"]["NexusTtsCancel"]["allOf"][1]["properties"].update(
            synthetic={"const": {"description": value, "$ref": "literal"}}
        )

    _change(root, lambda d: update(d, "before"), SOURCE)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "Synthetic opaque literal baseline")
    base = _git(root, "rev-parse", "HEAD")
    _change(root, lambda d: update(d, "after"), SOURCE)
    assert module.inspect_source(base)["findings"]
