"""Preserve contract semantics, safe construction and fresh reads across loaders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support import contracts
from tests.support.safe_yaml import load_yaml

ROOT = Path(__file__).resolve().parents[2] / "contracts"
SOURCES = sorted(path for path in ROOT.rglob("*") if path.suffix in {".json", ".yaml"})


def _same_values(actual: Any, expected: Any) -> None:
    assert type(actual) is type(expected)
    if isinstance(actual, dict):
        assert actual.keys() == expected.keys()
        for key, value in actual.items():
            _same_values(value, expected[key])
    elif isinstance(actual, list):
        assert len(actual) == len(expected)
        for value, original in zip(actual, expected, strict=True):
            _same_values(value, original)
    else:
        assert actual == expected


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: str(path.relative_to(ROOT)))
def test_repository_contracts_keep_the_same_types_and_values(path: Path) -> None:
    source = path.read_text()
    _same_values(load_yaml(source), yaml.safe_load(source))


@pytest.mark.parametrize("fallback", [False, True])
def test_both_backends_refuse_python_object_construction(
    fallback: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if fallback:
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    with pytest.raises(yaml.constructor.ConstructorError):
        load_yaml("!!python/name:builtins.str ''")
    _same_values(load_yaml("enabled: false\nlimit: 2\n"), {"enabled": False, "limit": 2})


def test_contract_documents_stay_fresh_and_mutations_do_not_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(contracts, "ROOT", tmp_path)
    path = tmp_path / "synthetic.yaml"
    path.write_text("type: object\nproperties: {count: {type: integer}}\n")
    first = contracts.document(path)
    first["properties"]["count"]["type"] = "boolean"
    assert contracts.document(path)["properties"]["count"]["type"] == "integer"
    path.write_text("type: object\nproperties: {count: {type: string}}\n")
    assert contracts.document(path)["properties"]["count"]["type"] == "string"
