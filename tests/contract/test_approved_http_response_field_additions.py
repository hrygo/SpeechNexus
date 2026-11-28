"""A required response field is never added to a frozen contract without review.

An added required property is a change a strict client feels: against a component with
``additionalProperties: false`` the old declaration says the new response is invalid. The
HTTP gate therefore rejects it unless one whole-diff proof shows that everything which
changed in ``contracts/`` is exactly the registered fields (#1282 gained
``stopped_generations`` and ``voided_generations`` on the node controller's lifecycle
head, the testimony an audited ledger repair cites).

This file proves three things, in the same spirit as the other approval guards:

* the registered fields are exactly what the real contract diff adds, so the approval is
  not granted to a change that does not exist;
* a second semantic edit beside them still rejects the comparison -- the approval cannot
  carry an unrelated narrowing, a second file, or an extra field;
* a field that is *not required*, or that the baseline already declared, is not covered:
  the proof normalizes the current document and compares it to the baseline, so either
  case leaves a difference behind.
"""

from __future__ import annotations

import copy
import importlib.util
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/internal/runtime-control.openapi.yaml"
COMPONENT = "RuntimeInstanceHeadResponse"
FIELDS = ("stopped_generations", "voided_generations")


def _kernel() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "contract_kernel", ROOT / "scripts/dev-env/check-openai-contract-compat.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[tuple[ModuleType, Path, str]]:
    """A frozen baseline that is today's contract without the reviewed fields."""
    kernel = _kernel()
    current = yaml.safe_load((ROOT / SOURCE).read_text(encoding="utf-8"))
    baseline = copy.deepcopy(current)
    component = baseline["components"]["schemas"][COMPONENT]
    component["properties"] = {
        name: value for name, value in component["properties"].items() if name not in FIELDS
    }
    component["required"] = [name for name in component["required"] if name not in FIELDS]
    target = tmp_path / SOURCE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
    for command in (
        ("init",),
        ("config", "user.name", "Synthetic contract test"),
        ("config", "user.email", "synthetic@example.invalid"),
        ("add", "."),
        ("commit", "-m", "Synthetic baseline without the reviewed fields"),
    ):
        subprocess.run(["git", "-C", str(tmp_path), *command], check=True, capture_output=True)
    base = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    target.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    yield kernel, tmp_path, base


def _current(tmp_path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((tmp_path / SOURCE).read_text(encoding="utf-8")))


def _write(tmp_path: Path, document: dict[str, Any]) -> None:
    (tmp_path / SOURCE).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_the_registered_fields_are_exactly_the_contract_diff(
    repository: tuple[ModuleType, Path, str],
) -> None:
    kernel, root, base = repository

    assert kernel.reviewed_response_field_addition(root, base) is None
    # The approval line names the fields and the issue that decided them, so a reviewer
    # reading the gate output sees what was allowed and why.
    line = kernel.reviewed_response_field_addition_line(2)
    for fact in (*FIELDS, SOURCE, "#1282"):
        assert fact in line


def test_a_second_semantic_edit_beside_the_fields_still_rejects(
    repository: tuple[ModuleType, Path, str],
) -> None:
    kernel, root, base = repository
    document = _current(root)
    component = document["components"]["schemas"][COMPONENT]
    component["description"] = "an annotation: must still pass"
    _write(root, document)
    assert kernel.reviewed_response_field_addition(root, base) is None

    document = _current(root)
    head = document["paths"]["/internal/v1/nodes/{node_id}/instances/{instance_id}/head"]["get"]
    head["operationId"] = "getInstanceHeadV2"  # semantic: must reject
    _write(root, document)
    problem = kernel.reviewed_response_field_addition(root, base)
    assert problem is not None
    assert "another semantic change" in problem


def test_an_extra_field_is_not_covered_by_the_registration(
    repository: tuple[ModuleType, Path, str],
) -> None:
    kernel, root, base = repository
    document = _current(root)
    component = document["components"]["schemas"][COMPONENT]
    component["properties"]["extra_generations"] = {"type": "array"}
    component["required"].append("extra_generations")
    _write(root, document)

    problem = kernel.reviewed_response_field_addition(root, base)
    assert problem is not None
    assert "another semantic change" in problem


def test_a_field_that_is_not_required_is_not_covered(
    repository: tuple[ModuleType, Path, str],
) -> None:
    kernel, root, base = repository
    document = _current(root)
    component = document["components"]["schemas"][COMPONENT]
    component["required"] = [name for name in component["required"] if name != FIELDS[0]]
    _write(root, document)

    problem = kernel.reviewed_response_field_addition(root, base)
    assert problem is not None
    assert "not required" in problem


def test_a_field_the_baseline_already_declares_is_not_covered(
    repository: tuple[ModuleType, Path, str],
) -> None:
    kernel, root, base = repository
    document = _current(root)
    component = document["components"]["schemas"][COMPONENT]
    component["required"].append("instance_id")  # already declared in the baseline
    _write(root, document)

    # ``instance_id`` is already required in the baseline, so removing the registered
    # fields leaves that extra requirement behind and the whole-diff comparison rejects.
    problem = kernel.reviewed_response_field_addition(root, base)
    assert problem is not None
    assert "another semantic change" in problem


def test_another_contract_file_is_outside_the_approval(
    repository: tuple[ModuleType, Path, str],
) -> None:
    kernel, root, base = repository
    other = root / "contracts/shared/unrelated.schema.json"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text('{"type": "object"}', encoding="utf-8")

    problem = kernel.reviewed_response_field_addition(root, base)
    assert problem is not None
    assert "differs in" in problem
