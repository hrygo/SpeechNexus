"""Only an explicit successful route can make application checks inapplicable."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW: dict[str, Any] = yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text())
_PROBE_SPEC = importlib.util.spec_from_file_location(
    "workbench_pg_probe_budget", _ROOT / "scripts/dev-env/workbench-pg-probe.py"
)
assert _PROBE_SPEC is not None and _PROBE_SPEC.loader is not None
_PROBE = importlib.util.module_from_spec(_PROBE_SPEC)
_PROBE_SPEC.loader.exec_module(_PROBE)
_GATE = _WORKFLOW["jobs"]["python"]
_STATES = ("success", "failure", "skipped", "cancelled", "")
_DELIVERY = {"DELIVERY_RESULT": "success"}
_EXPECTED = {
    "docs": {
        **_DELIVERY,
        "CHECKS_RESULT": "skipped",
        "COORDINATION_RESULT": "skipped",
        "WEB_RESULT": "skipped",
        "DEPLOYMENT_RESULT": "skipped",
    },
    "web": {
        **_DELIVERY,
        "CHECKS_RESULT": "success",
        "COORDINATION_RESULT": "skipped",
        "WEB_RESULT": "success",
        "DEPLOYMENT_RESULT": "skipped",
    },
    "full": {
        **_DELIVERY,
        "CHECKS_RESULT": "success",
        "COORDINATION_RESULT": "success",
        "WEB_RESULT": "success",
        "DEPLOYMENT_RESULT": "success",
    },
    "no-db": {
        **_DELIVERY,
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
    # #452 and #502: only the shard budget grew; the Web lane keeps its own budget.
    assert shards["timeout-minutes"] == 30
    assert jobs["web"]["timeout-minutes"] == 10
    assert shards["strategy"]["matrix"]["shard"] == [0, 1, 2, 3, 4, 5]


def test_integration_shard_budget_matches_the_workflow_job_timeout() -> None:
    shards = _WORKFLOW["jobs"]["integration_shards"]
    assert shards["timeout-minutes"] * 60 == _PROBE.INTEGRATION_JOB_TIMEOUT_SECONDS
    assert (
        _PROBE.INTEGRATION_SHARD_PYTEST_TIMEOUT_SECONDS
        == _PROBE.INTEGRATION_JOB_TIMEOUT_SECONDS - _PROBE.INTEGRATION_JOB_RESERVE_SECONDS
    )
    assert _PROBE.INTEGRATION_JOB_RESERVE_SECONDS < _PROBE.INTEGRATION_JOB_TIMEOUT_SECONDS


def test_integration_evidence_uses_bounded_same_run_cli_with_read_only_access() -> None:
    job = _WORKFLOW["jobs"]["coordination"]
    assert _WORKFLOW["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    # Setup plus a locked install precede the merge; the judgment itself is seconds.
    assert job["timeout-minutes"] == 10
    assert set(job["needs"]) == {"changes", "integration_shards", "python_checks"}
    step = next(s for s in job["steps"] if "download-integration-evidence.py" in s.get("run", ""))
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "CI_REPOSITORY": "${{ github.repository }}",
        "CI_RUN_ID": "${{ github.run_id }}",
    }
    assert '--repository "$CI_REPOSITORY" --run-id "$CI_RUN_ID"' in step["run"]
    assert '--count 6 --output "$RUNNER_TEMP/integration-shards"' in step["run"]
    assert '--coverage-output "$RUNNER_TEMP/coverage-evidence"' in step["run"]
    assert not any("actions/download-artifact@" in s.get("uses", "") for s in job["steps"])
    assert "if" not in step and "continue-on-error" not in step
    check = job["steps"][job["steps"].index(step) + 1]
    assert "scripts/dev-env/integration-shards.py check" in check["run"]
    assert '--count 6 --head "$(git rev-parse HEAD)"' in check["run"]
    assert "if" not in check and "continue-on-error" not in check
    merge = job["steps"][job["steps"].index(check) + 1]
    assert "uv run python scripts/dev-env/python-coverage.py combine" in merge["run"]
    assert (
        '--job-summary "$RUNNER_TEMP/coverage-evidence/workbench-pg-coverage.json"' in merge["run"]
    )
    assert (
        '--job-parts "$RUNNER_TEMP/coverage-evidence/workbench-pg-coverage-parts"' in merge["run"]
    )
    assert '--integration-dir "$RUNNER_TEMP/integration-shards"' in merge["run"]
    assert '--count 6 --output "$RUNNER_TEMP/coverage-merge"' in merge["run"]
    # The gate must never be redirected to a different configuration or threshold.
    assert "--probe-rcfile" not in merge["run"] and "--fail-under" not in merge["run"]
    assert "if" not in merge and "continue-on-error" not in merge


def test_merging_needs_both_evidence_sets_and_fails_closed_without_them() -> None:
    job = _WORKFLOW["jobs"]["coordination"]
    steps = job["steps"]
    shards = next(step for step in steps if step.get("name") == "Require every integration shard")
    assert shards["env"] == {"SHARDS_RESULT": "${{ needs.integration_shards.result }}"}
    measurement = next(
        step for step in steps if step.get("name") == "Require the Python coverage measurement"
    )
    assert measurement["env"] == {"MEASUREMENT_RESULT": "${{ needs.python_checks.result }}"}
    for state in _STATES:
        passed = subprocess.run(
            ["bash", "-e", "-c", measurement["run"]],
            env={**os.environ, "MEASUREMENT_RESULT": state},
            capture_output=True,
            text=True,
            check=False,
        )
        assert (passed.returncode == 0) == (state == "success")
    # Coverage judgment is composed here, so the final gate already requires it.
    python_gate = _WORKFLOW["jobs"]["python"]
    assert "coordination" in python_gate["needs"]
    assert _EXPECTED["full"]["COORDINATION_RESULT"] == "success"


def test_measured_coverage_is_exported_and_each_shard_uploads_its_dataset() -> None:
    execution = _WORKFLOW["jobs"]["python_checks"]
    steps = execution["steps"]
    coverage = next(step for step in steps if step.get("id") == "coverage")
    assert "uv run python scripts/dev-env/python-coverage.py run" in coverage["run"]
    artifact = next(
        step for step in steps if step.get("with", {}).get("name") == "workbench-pg-full-coverage"
    )
    assert artifact["with"]["if-no-files-found"] == "error"
    shards = _WORKFLOW["jobs"]["integration_shards"]
    run = next(step for step in shards["steps"] if "--integration-shard" in step.get("run", ""))
    assert '--shard-coverage "$RUNNER_TEMP/coverage-${{ matrix.shard }}.data"' in run["run"]
    evidence = next(
        step
        for step in shards["steps"]
        if step.get("name") == "Preserve integration partition evidence"
    )
    assert evidence["with"]["if-no-files-found"] == "error"
    assert "${{ runner.temp }}/coverage-${{ matrix.shard }}.data" in evidence["with"]["path"]


def _run_gate(
    env: dict[str, str], *, delivery: str | None = "success"
) -> subprocess.CompletedProcess[str]:
    defaults = {"CONTROLLER_RESULT": "success", "CONTROLLER_REQUIRED": "false"}
    if delivery is not None:
        defaults["DELIVERY_RESULT"] = delivery
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _GATE["steps"][0]["run"]],
        env={**defaults, **env},
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
    assert _GATE["name"] == "Gate: required checks for this change"
    assert set(_GATE["needs"]) == {
        "changes",
        "python_checks_gate",
        "coordination",
        "web",
        "deployment",
        "delivery_tracking",
        "node_controller_image",
    }
    assert _GATE["if"] == "${{ always() }}"
    step = _GATE["steps"][0]
    assert step["env"] == {
        "MODE": "${{ needs.changes.outputs.mode }}",
        "ROUTING_RESULT": "${{ needs.changes.result }}",
        "DELIVERY_RESULT": "${{ needs.delivery_tracking.result }}",
        "CHECKS_RESULT": "${{ needs.python_checks_gate.result }}",
        "COORDINATION_RESULT": "${{ needs.coordination.result }}",
        "WEB_RESULT": "${{ needs.web.result }}",
        "DEPLOYMENT_RESULT": "${{ needs.deployment.result }}",
        "CONTROLLER_RESULT": "${{ needs.node_controller_image.result }}",
        "CONTROLLER_REQUIRED": "${{ needs.node_controller_image.outputs.required }}",
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


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("state", _STATES)
def test_final_gate_requires_delivery_matrix_evidence(mode: str, state: str) -> None:
    states = {**_EXPECTED[mode], "DELIVERY_RESULT": state}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert (result.returncode == 0) is (state == "success")


@pytest.mark.parametrize("mode", list(_EXPECTED))
def test_final_gate_rejects_missing_delivery_matrix_result(mode: str) -> None:
    states = {key: value for key, value in _EXPECTED[mode].items() if key != "DELIVERY_RESULT"}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states}, delivery=None)
    assert result.returncode != 0


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
    assert gate["name"] == "Gate: Python execution and static checks"
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


def test_route_conformance_gate_runs_before_python_static_checks() -> None:
    steps = _WORKFLOW["jobs"]["python_contracts"]["steps"]
    gate = next(step for step in steps if step.get("run") == "make contracts-route-conformance")
    assert "if" not in gate and "continue-on-error" not in gate
    static_checks = {"uv run ruff check .", "uv run mypy", "uv run lint-imports"}
    static_indices = [index for index, step in enumerate(steps) if step.get("run") in static_checks]
    assert static_checks <= {step.get("run") for step in steps}
    assert steps.index(gate) < min(static_indices)


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
        "make contracts-route-conformance",
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
    assert "uv run python scripts/dev-env/python-coverage.py run" in coverage["run"]
    assert "--pg-bin /usr/lib/postgresql/16/bin --workers 2" in coverage["run"]
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
    assert artifact["with"]["path"].splitlines() == [
        "${{ runner.temp }}/workbench-pg-coverage.json",
        "${{ runner.temp }}/workbench-pg-coverage-parts/",
    ]
    assert artifact["with"]["if-no-files-found"] == "error"
    # Both workers use the full roots; the combined gate retains the original policy.
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
