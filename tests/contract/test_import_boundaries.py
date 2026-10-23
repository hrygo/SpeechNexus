"""Exercise configured architecture guards against synthetic illegal imports."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import grimp
import pytest
from importlinter.contracts.independence import IndependenceContract
from importlinter.contracts.layers import LayersContract

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["importlinter"]


def _check(config: dict[str, Any], importer: str, imported: str) -> bool:
    graph = grimp.ImportGraph()
    modules = {*_CONFIG["root_packages"], importer, imported}
    for contract in _CONFIG["contracts"]:
        modules.update(contract.get("modules", []))
        for layer in contract.get("layers", []):
            modules.update(item.strip() for item in layer.split("|"))
    for module in list(modules):
        parts = module.split(".")
        modules.update(".".join(parts[:index]) for index in range(1, len(parts)))
    for module in modules:
        graph.add_module(module)
    graph.add_import(importer=importer, imported=imported, line_number=1, line_contents="synthetic")
    cls = LayersContract if config["type"] == "layers" else IndependenceContract
    contract = cls(config["name"], _CONFIG, config)
    return contract.check(graph, verbose=False).kept


@pytest.mark.parametrize("config", _CONFIG["contracts"], ids=lambda config: config["name"])
def test_each_architecture_guard_rejects_real_graph_violation(config: dict[str, Any]) -> None:
    if config["type"] == "layers":
        top = config["layers"][0].split("|")[0].strip()
        bottom = config["layers"][-1]
        assert not _check(config, bottom, top)
        assert _check(config, top, bottom)
        sibling = config["layers"][0].split("|")[1].strip()
        assert not _check(config, top, sibling)
        assert not _check(config, sibling, top)
    else:
        first, second = config["modules"]
        assert not _check(config, first, second)
        assert not _check(config, second, first)
        # The application composition root is outside the bounded contexts.
        assert _check(config, first + ".app", first + ".domain")


def test_public_client_import_stays_outside_speech_implementation() -> None:
    config = next(item for item in _CONFIG["contracts"] if "public protocols" in item["name"])
    assert _check(config, "speechnexus_backend.workbench.infrastructure", "openai")
