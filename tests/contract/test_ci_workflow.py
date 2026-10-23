"""The protected Python status must require both parallel validation jobs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW: dict[str, Any] = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
_GATE = _WORKFLOW["jobs"]["python"]


@pytest.mark.parametrize("checks", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("coordination", ["success", "failure", "skipped", "cancelled"])
def test_protected_status_never_passes_missing_evidence(checks: str, coordination: str) -> None:
    assert _GATE["name"] == "Python quality and tests"
    assert set(_GATE["needs"]) == {"python_checks", "coordination"}
    assert _GATE["if"] == "${{ always() }}"
    step = _GATE["steps"][0]
    assert step["env"] == {
        "CHECKS_RESULT": "${{ needs.python_checks.result }}",
        "COORDINATION_RESULT": "${{ needs.coordination.result }}",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={"CHECKS_RESULT": checks, "COORDINATION_RESULT": coordination},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (checks == coordination == "success")
