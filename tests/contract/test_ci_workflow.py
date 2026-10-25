"""Only an explicit successful route can make application checks inapplicable."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW: dict[str, Any] = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
_GATE = _WORKFLOW["jobs"]["python"]
_STATES = ("success", "failure", "skipped", "cancelled", "")
_EXPECTED = {
    "docs": {
        "CHECKS_RESULT": "skipped",
        "COORDINATION_RESULT": "skipped",
        "WEB_RESULT": "skipped",
        "DEPLOYMENT_RESULT": "skipped",
    },
    "web": {
        "CHECKS_RESULT": "success",
        "COORDINATION_RESULT": "skipped",
        "WEB_RESULT": "success",
        "DEPLOYMENT_RESULT": "skipped",
    },
    "full": {
        "CHECKS_RESULT": "success",
        "COORDINATION_RESULT": "success",
        "WEB_RESULT": "success",
        "DEPLOYMENT_RESULT": "success",
    },
    "no-db": {
        "CHECKS_RESULT": "success",
        "COORDINATION_RESULT": "skipped",
        "WEB_RESULT": "success",
        "DEPLOYMENT_RESULT": "success",
    },
}


def test_design_token_guard_runs_before_routing_without_dependency_install() -> None:
    steps = _WORKFLOW["jobs"]["changes"]["steps"]
    guard = next(
        s for s in steps if "node --test tests/ci/design-tokens.test.mjs" in s.get("run", "")
    )
    assert "node scripts/dev-env/validate-design-tokens.mjs" in guard["run"]
    assert "if" not in guard and "continue-on-error" not in guard
    assert steps.index(guard) < next(i for i, s in enumerate(steps) if s.get("id") == "route")
    assert any(s.get("with", {}).get("node-version-file") == ".nvmrc" for s in steps)
    assert not any("install" in s.get("run", "") for s in steps)


def test_accessibility_checks_run_in_protected_web_lane_after_build() -> None:
    steps = _WORKFLOW["jobs"]["web"]["steps"]
    build = next(i for i, s in enumerate(steps) if s.get("run") == "pnpm build")
    install = next(i for i, s in enumerate(steps) if "prepare-ci-browser.py" in s.get("run", ""))
    check = next(i for i, s in enumerate(steps) if s.get("id") == "a11y")
    assert build < install < check
    assert steps[install]["run"] == "python3 scripts/dev-env/prepare-ci-browser.py"
    assert steps[check]["run"] == "pnpm test:a11y"
    assert "continue-on-error" not in steps[check] and "if" not in steps[check]
    artifact = next(
        s for s in steps if s.get("with", {}).get("name") == "web-accessibility-evidence"
    )
    assert "steps.a11y.outcome == 'success'" in artifact["if"]
    assert "steps.a11y.outcome == 'failure'" in artifact["if"]
    assert artifact["with"]["if-no-files-found"] == "error"
    assert "test-results/" in artifact["with"]["path"]
    assert not any("playwright" in s.get("run", "") for s in _WORKFLOW["jobs"]["changes"]["steps"])


def test_browser_preparation_keeps_partition_selection_and_original_budgets() -> None:
    jobs = _WORKFLOW["jobs"]
    shards = jobs["integration_shards"]
    install = next(s for s in shards["steps"] if "prepare-ci-browser.py" in s.get("run", ""))
    assert install["if"] == "steps.integration_tools.outputs.browser == 'true'"
    assert install["run"] == "python3 scripts/dev-env/prepare-ci-browser.py"
    assert "continue-on-error" not in install
    assert shards["timeout-minutes"] == jobs["web"]["timeout-minutes"] == 10
    assert shards["strategy"]["matrix"]["shard"] == [0, 1, 2, 3, 4, 5]


def _run_gate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _GATE["steps"][0]["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("checks", _STATES)
@pytest.mark.parametrize("coordination", _STATES)
def test_protected_status_never_passes_missing_evidence(
    mode: str, checks: str, coordination: str
) -> None:
    assert _GATE["name"] == "Python quality and tests"
    assert set(_GATE["needs"]) == {
        "changes",
        "python_checks_gate",
        "coordination",
        "web",
        "deployment",
    }
    assert _GATE["if"] == "${{ always() }}"
    step = _GATE["steps"][0]
    assert step["env"] == {
        "MODE": "${{ needs.changes.outputs.mode }}",
        "ROUTING_RESULT": "${{ needs.changes.result }}",
        "CHECKS_RESULT": "${{ needs.python_checks_gate.result }}",
        "COORDINATION_RESULT": "${{ needs.coordination.result }}",
        "WEB_RESULT": "${{ needs.web.result }}",
        "DEPLOYMENT_RESULT": "${{ needs.deployment.result }}",
    }
    states = {**_EXPECTED[mode], "CHECKS_RESULT": checks, "COORDINATION_RESULT": coordination}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert (result.returncode == 0) is (states == _EXPECTED[mode])


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("lane", ["WEB_RESULT", "DEPLOYMENT_RESULT"])
@pytest.mark.parametrize("state", _STATES)
def test_final_gate_requires_the_web_and_deployment_results_for_its_scope(
    mode: str, lane: str, state: str
) -> None:
    states = {**_EXPECTED[mode], lane: state}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert (result.returncode == 0) is (states == _EXPECTED[mode])


@pytest.mark.parametrize("mode", [*_EXPECTED, "", "unknown"])
@pytest.mark.parametrize("routing", _STATES)
def test_missing_or_invalid_routing_cannot_authorize_a_merge(mode: str, routing: str) -> None:
    states = _EXPECTED.get(mode, _EXPECTED["full"])
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": routing, **states})
    assert (result.returncode == 0) is (mode in _EXPECTED and routing == "success")


@pytest.mark.parametrize("checks", ["success", "failure", "skipped", "cancelled", ""])
@pytest.mark.parametrize("contracts", ["success", "failure", "skipped", "cancelled", ""])
def test_original_python_status_requires_both_complete_lanes(checks: str, contracts: str) -> None:
    gate = _WORKFLOW["jobs"]["python_checks_gate"]
    assert gate["name"] == "Python static checks and unit tests"
    assert set(gate["needs"]) == {"changes", "python_checks", "python_contracts"}
    assert "always()" in gate["if"] and "needs.changes.outputs.mode" in gate["if"]
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
        assert job["needs"] == ["changes"]
        assert (
            job["if"]
            == "${{ needs.changes.outputs.mode == 'full' || needs.changes.outputs.mode == 'web' "
            "|| needs.changes.outputs.mode == 'no-db' }}"
        )
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
        "make contracts-realtime-changes",
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
    coverage = next(step for step in execution["steps"] if step.get("id") == "coverage")
    assert "uv run python scripts/dev-env/workbench-pg-probe.py" in coverage["run"]
    assert "--pg-bin /usr/lib/postgresql/16/bin --full-coverage" in coverage["run"]
    assert '--output "$RUNNER_TEMP/workbench-pg-coverage.json"' in coverage["run"]
    assert coverage["run"] not in contract_commands
    assert "--cov-fail-under" not in coverage["run"]
    assert "continue-on-error" not in coverage and "if" not in coverage
    assert coverage["env"] == {
        "SPEECHNEXUS_TEST_MINIO_ENDPOINT": "http://127.0.0.1:59000",
        "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY": "speechnexus_test",
        "SPEECHNEXUS_TEST_MINIO_SECRET_KEY": "synthetic_asset_test_secret",
    }
    steps = execution["steps"]
    pg = next(step for step in steps if "private coverage cluster" in step["name"])
    storage = next(step for step in steps if "object storage for coverage" in step["name"])
    assert "test -x /usr/lib/postgresql/16/bin/initdb" in pg["run"]
    assert "--no-install-recommends --yes postgresql-16" in pg["run"]
    assert storage["run"] == "./scripts/dev-env/minio-test-service.sh"
    assert steps.index(pg) < steps.index(storage) < steps.index(coverage)
    artifact = next(
        step for step in steps if step.get("with", {}).get("name") == "workbench-pg-full-coverage"
    )
    assert "steps.coverage.outcome == 'success'" in artifact["if"]
    assert "steps.coverage.outcome == 'failure'" in artifact["if"]
    assert artifact["with"]["path"] == "${{ runner.temp }}/workbench-pg-coverage.json"
    assert artifact["with"]["if-no-files-found"] == "error"
    # The full single-suite coverage requirement remains in the original config.
    import tomllib

    config = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert config["tool"]["coverage"]["report"]["fail_under"] == 80


def test_required_workflow_is_never_filtered_out_and_classification_is_lightweight() -> None:
    workflow = yaml.load((_ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    events = workflow["on"]
    assert set(events) == {"push", "pull_request", "workflow_dispatch"}
    for event in ("push", "pull_request"):
        assert not (set(events[event] or {}) & {"paths", "paths-ignore"})
    jobs = _WORKFLOW["jobs"]
    routing = jobs["changes"]
    commands = "\n".join(step.get("run", "") for step in routing["steps"])
    assert "python3 -m unittest discover -s tests/ci" in commands
    assert "scripts/dev-env/ci-routing.py" in commands
    assert "github.event.pull_request.base.sha" in str(routing)
    assert "uv sync" not in commands and "pnpm install" not in commands
    assert "services" not in routing and "needs" not in routing
    assert jobs["integration_shards"]["if"] == "${{ needs.changes.outputs.mode == 'full' }}"
    assert (
        jobs["deployment"]["if"]
        == "${{ needs.changes.outputs.mode == 'full' || needs.changes.outputs.mode == 'no-db' }}"
    )
    assert jobs["web"]["if"] == jobs["python_checks"]["if"]
