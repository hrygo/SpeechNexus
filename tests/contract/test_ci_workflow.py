"""Protected names retain complete evidence while contract checks run independently."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW: dict[str, Any] = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
_GATE = _WORKFLOW["jobs"]["python"]


@pytest.mark.parametrize("checks", ["success", "failure", "skipped", "cancelled", ""])
@pytest.mark.parametrize("coordination", ["success", "failure", "skipped", "cancelled", ""])
def test_protected_status_never_passes_missing_evidence(checks: str, coordination: str) -> None:
    assert _GATE["name"] == "Python quality and tests"
    assert set(_GATE["needs"]) == {"python_checks_gate", "coordination"}
    assert _GATE["if"] == "${{ always() }}"
    step = _GATE["steps"][0]
    assert step["env"] == {
        "CHECKS_RESULT": "${{ needs.python_checks_gate.result }}",
        "COORDINATION_RESULT": "${{ needs.coordination.result }}",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={"CHECKS_RESULT": checks, "COORDINATION_RESULT": coordination},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (checks == coordination == "success")


@pytest.mark.parametrize("checks", ["success", "failure", "skipped", "cancelled", ""])
@pytest.mark.parametrize("contracts", ["success", "failure", "skipped", "cancelled", ""])
def test_original_python_status_requires_both_complete_lanes(checks: str, contracts: str) -> None:
    gate = _WORKFLOW["jobs"]["python_checks_gate"]
    assert gate["name"] == "Python static checks and unit tests"
    assert set(gate["needs"]) == {"python_checks", "python_contracts"}
    assert gate["if"] == "${{ always() }}"
    step = gate["steps"][0]
    assert step["env"] == {
        "CHECKS_RESULT": "${{ needs.python_checks.result }}",
        "CONTRACTS_RESULT": "${{ needs.python_contracts.result }}",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={"CHECKS_RESULT": checks, "CONTRACTS_RESULT": contracts},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (checks == contracts == "success")


def test_full_coverage_and_every_contract_check_run_independently_on_the_same_source() -> None:
    jobs = _WORKFLOW["jobs"]
    execution, contracts = jobs["python_checks"], jobs["python_contracts"]
    checkouts = []
    for job in (execution, contracts):
        assert not job.get("needs")
        steps = job["steps"]
        assert any(
            step.get("run") == "uv sync --locked --all-packages --all-groups" for step in steps
        )
        assert any(step.get("with", {}).get("node-version-file") == ".nvmrc" for step in steps)
        assert any("pnpm install --frozen-lockfile" in step.get("run", "") for step in steps)
        checkout = next(step for step in steps if "actions/checkout@" in step.get("uses", ""))
        assert checkout["with"]["persist-credentials"] is False
        checkouts.append(checkout)
    assert checkouts[0]["uses"] == checkouts[1]["uses"]
    assert checkouts[0]["with"] == checkouts[1]["with"]
    assert "ref" not in checkouts[0]["with"]  # Both consume the event's exact checkout SHA.
    expected = {
        "make contracts-drift",
        'uv run python scripts/dev-env/check-openai-contract-compat.py --base "$CONTRACT_BASE_REF"',
        "make contracts-http-changes",
        "make contracts-speech-changes",
        "make contracts-coordination-requests",
        "make contracts-coordination-results",
        "uv run python scripts/dev-env/validate-contracts.py",
        "uv run ruff format --check .",
        "uv run ruff check .",
        "uv run mypy",
        "uv run lint-imports",
    }
    execution_commands = {step.get("run") for step in execution["steps"]}
    contract_commands = {step.get("run") for step in contracts["steps"]}
    assert expected <= contract_commands
    assert not expected.intersection(execution_commands)
    coverage = 'uv run pytest -m "not integration" --cov --cov-report=term-missing'
    assert coverage in execution_commands
    assert coverage not in contract_commands
    # The full single-suite coverage requirement remains in the original config.
    import tomllib

    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert config["tool"]["coverage"]["report"]["fail_under"] == 80
