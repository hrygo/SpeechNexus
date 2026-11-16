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
_GATE = _WORKFLOW["jobs"]["required_checks"]
_STATES = ("success", "failure", "skipped", "cancelled", "")
_DELIVERY = {"MATRIX_RESULT": "success"}
# `guards` (routing boundaries, design tokens, generated-contract consumers) has
# no scope filter, so every route owes it a success rather than a skip.
_GUARDS = {"GUARDS_RESULT": "success"}
# `docs_refs` (the `文件:行号` reference gate) has no scope filter: every PR runs
# it, so every scope owes it a success rather than a skip (#859).
_REFERENCE = {"DOCS_REFS_RESULT": "success"}
# `docs_format` is the only lane a prose-only change owes; every heavier scope owes
# the same file set through `python_static`' `uv run ruff format --check .`.
# `INTEGRATION_RESULT` is the route's own integration evidence: a `web` change owes
# the two Web-capable partitions (#757), while `integration_gate` — the eight-partition
# evidence and merged-coverage gate — keeps requiring `full` and stays skipped
# everywhere else. A skipped integration job on `web` therefore fails the gate.
_EXPECTED = {
    "docs": {
        **_DELIVERY,
        **_GUARDS,
        **_REFERENCE,
        "DOCS_FORMAT_RESULT": "success",
        "PYTHON_RESULT": "skipped",
        "INTEGRATION_GATE_RESULT": "skipped",
        "INTEGRATION_RESULT": "skipped",
        "WEB_RESULT": "skipped",
        "COMPOSE_RESULT": "skipped",
        "ACCEPTANCE_RESULT": "skipped",
    },
    "web": {
        **_DELIVERY,
        **_GUARDS,
        **_REFERENCE,
        "DOCS_FORMAT_RESULT": "skipped",
        "PYTHON_RESULT": "success",
        "INTEGRATION_GATE_RESULT": "skipped",
        "INTEGRATION_RESULT": "success",
        "WEB_RESULT": "success",
        "COMPOSE_RESULT": "skipped",
        "ACCEPTANCE_RESULT": "skipped",
    },
    "full": {
        **_DELIVERY,
        **_GUARDS,
        **_REFERENCE,
        "DOCS_FORMAT_RESULT": "skipped",
        "PYTHON_RESULT": "success",
        "INTEGRATION_GATE_RESULT": "success",
        "INTEGRATION_RESULT": "success",
        "WEB_RESULT": "success",
        "COMPOSE_RESULT": "success",
        "ACCEPTANCE_RESULT": "success",
    },
    "no-db": {
        **_DELIVERY,
        **_GUARDS,
        **_REFERENCE,
        "DOCS_FORMAT_RESULT": "skipped",
        "PYTHON_RESULT": "success",
        "INTEGRATION_GATE_RESULT": "skipped",
        "INTEGRATION_RESULT": "skipped",
        "WEB_RESULT": "success",
        "COMPOSE_RESULT": "success",
        "ACCEPTANCE_RESULT": "success",
    },
}


def test_design_token_guard_runs_in_the_dependency_free_guards_lane() -> None:
    # The token, routing-boundary and contract-consumer guards used to share the
    # routing job, which made every dependency-installing lane wait for them. They
    # now own a separate unfiltered lane, still install no project dependency,
    # still fail closed, and stay required by the merge gate.
    steps = _WORKFLOW["jobs"]["guards"]["steps"]
    guard = next(
        s for s in steps if "node --test tests/ci/design-tokens.test.mjs" in s.get("run", "")
    )
    assert "node scripts/dev-env/validate-design-tokens.mjs" in guard["run"]
    assert "if" not in guard and "continue-on-error" not in guard
    assert any(s.get("with", {}).get("node-version-file") == ".nvmrc" for s in steps)
    assert not any("install" in s.get("run", "") for s in steps)
    assert "if" not in _WORKFLOW["jobs"]["guards"]
    assert "guards" in _WORKFLOW["jobs"]["required_checks"]["needs"]
    routing_runs = "\n".join(s.get("run", "") for s in _WORKFLOW["jobs"]["routing"]["steps"])
    assert "design-tokens" not in routing_runs and "tests/ci" not in routing_runs


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
    assert not any("playwright" in s.get("run", "") for s in _WORKFLOW["jobs"]["routing"]["steps"])


def test_superseded_pull_request_runs_are_cancelled_but_main_pushes_are_not() -> None:
    """A `main` push is the only evidence for the squashed commit actually on main.

    Cancelling it when the next merge lands leaves that commit unverified; 3 of 20
    `main` pushes ending 2026-11-15 were cancelled exactly this way. Stale PR runs
    are still cancelled (only the newest commit is a merge candidate), which is
    where the runner-minute saving comes from. `true` here would silently restore
    the post-merge evidence gap.
    """
    concurrency = _WORKFLOW["concurrency"]
    assert concurrency["group"] == "ci-${{ github.workflow }}-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] == ("${{ github.event_name == 'pull_request' }}")


def test_browser_preparation_keeps_partition_selection_and_original_budgets() -> None:
    jobs = _WORKFLOW["jobs"]
    shards = jobs["integration_shards"]
    install = next(s for s in shards["steps"] if "prepare-ci-browser.py" in s.get("run", ""))
    assert install["if"] == (
        "${{ needs.integration_plan.outputs[format('browser_{0}', matrix.shard)] == 'true' }}"
    )
    assert install["run"] == "python3 scripts/dev-env/prepare-ci-browser.py"
    assert "continue-on-error" not in install
    # #452 and #502: only the shard budget grew; the Web lane keeps its own budget.
    assert shards["timeout-minutes"] == 30
    assert jobs["web"]["timeout-minutes"] == 10
    assert shards["strategy"]["matrix"]["shard"] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_a_web_route_runs_the_partitions_that_select_the_browser_consumers() -> None:
    """#745 merged green and broke `main`; #756 needed a manual run for evidence (#757)."""
    shards = _WORKFLOW["jobs"]["integration_shards"]
    # `web` is a scope that must reach the browser chain, so this job can no longer
    # be `full`-only; `docs`/`no-db` keep skipping it through this same gate.
    assert shards["if"] == (
        "${{ needs.routing.outputs.mode == 'full' || needs.routing.outputs.mode == 'web' }}"
    )
    matrix = shards["strategy"]["matrix"]
    # The twelve-partition universe stays declared in the workflow, so `full` cannot
    # silently shrink; the route only removes lanes it cannot exercise.
    assert matrix["shard"] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    assert matrix["exclude"] == "${{ fromJSON(needs.routing.outputs.excluded_shards) }}"
    assert _WORKFLOW["jobs"]["routing"]["outputs"]["excluded_shards"] == (
        "${{ steps.route.outputs.excluded_shards }}"
    )
    # A Web route buys evidence only if its lanes really select a Web consumer;
    # the guard must be tied to `web`, must read both selection kinds the step name
    # promises, and must fail closed on anything but `true`. A lane may legitimately
    # select only one kind, so the two are combined with `&&` on the "not true" side.
    guard = next(
        s
        for s in shards["steps"]
        if s.get("name") == "Require the Web lanes to select actual Web consumers"
    )
    assert guard["if"] == "${{ needs.routing.outputs.mode == 'web' }}"
    assert guard["env"] == {
        "BROWSER": "${{ needs.integration_plan.outputs[format('browser_{0}', matrix.shard)] }}",
        "NODE_CONTRACTS": (
            "${{ needs.integration_plan.outputs[format('node_contracts_{0}', matrix.shard)] }}"
        ),
        "SHARD": "${{ matrix.shard }}",
    }
    assert (
        'if [[ "${BROWSER:-}" != "true" && "${NODE_CONTRACTS:-}" != "true" ]]; then' in guard["run"]
    )
    assert "exit 1" in guard["run"] and "continue-on-error" not in guard
    # The plan is published once for every partition; each shard consumes its own
    # requirements and fails closed on a missing or malformed value before any
    # install step can silently skip Chromium or the Node runtime.
    assert shards["needs"] == ["routing", "integration_plan"]
    plan = _WORKFLOW["jobs"]["integration_plan"]
    assert plan["if"] == shards["if"]
    collect = next(s for s in plan["steps"] if s.get("id") == "plan")
    assert "integration-shards.py plan" in collect["run"]
    validate = next(
        s for s in shards["steps"] if s.get("name") == "Validate the partition tool plan"
    )
    assert "Partition plan published no boolean" in validate["run"]
    assert shards["steps"].index(validate) < shards["steps"].index(guard)


def test_integration_shard_budget_matches_the_workflow_job_timeout() -> None:
    shards = _WORKFLOW["jobs"]["integration_shards"]
    assert shards["timeout-minutes"] * 60 == _PROBE.INTEGRATION_JOB_TIMEOUT_SECONDS
    assert (
        _PROBE.INTEGRATION_SHARD_PYTEST_TIMEOUT_SECONDS
        == _PROBE.INTEGRATION_JOB_TIMEOUT_SECONDS - _PROBE.INTEGRATION_JOB_RESERVE_SECONDS
    )
    assert _PROBE.INTEGRATION_JOB_RESERVE_SECONDS < _PROBE.INTEGRATION_JOB_TIMEOUT_SECONDS


def test_python_coverage_budget_matches_the_workflow_job_timeout() -> None:
    """The full-coverage probe inherits the Python job ceiling; they must not drift.

    `python-coverage.py run --workers 4` invokes the probe with `--full-coverage`,
    so a stale 10-minute constant would kill pytest at 600 s even though the job
    now allows 900 s -- the extra headroom would never reach a slow runner. The
    no-reserve relationship (inner ceiling == job ceiling) is pre-existing and
    stays intact; only the value moves with the workflow budget.
    """
    python_tests = _WORKFLOW["jobs"]["python_tests"]
    assert python_tests["timeout-minutes"] * 60 == _PROBE.COVERAGE_JOB_TIMEOUT_SECONDS
    assert _PROBE.FULL_COVERAGE_PYTEST_TIMEOUT_SECONDS == _PROBE.COVERAGE_JOB_TIMEOUT_SECONDS
    assert _PROBE.pytest_timeout_seconds(full_coverage=True, integration_shard=None) == (
        _PROBE.COVERAGE_JOB_TIMEOUT_SECONDS
    )
    assert _PROBE.pytest_job_timeout_seconds(full_coverage=True, integration_shard=None) == (
        _PROBE.COVERAGE_JOB_TIMEOUT_SECONDS
    )


def test_integration_evidence_uses_bounded_same_run_cli_with_read_only_access() -> None:
    job = _WORKFLOW["jobs"]["integration_gate"]
    assert _WORKFLOW["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    # Setup plus a locked install precede the merge; the judgment itself is seconds.
    assert job["timeout-minutes"] == 10
    assert set(job["needs"]) == {"routing", "integration_shards", "python_tests"}
    step = next(s for s in job["steps"] if "download-integration-evidence.py" in s.get("run", ""))
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "CI_REPOSITORY": "${{ github.repository }}",
        "CI_RUN_ID": "${{ github.run_id }}",
    }
    assert '--repository "$CI_REPOSITORY" --run-id "$CI_RUN_ID"' in step["run"]
    assert (
        '--count 12 --coverage-workers 4 --output "$RUNNER_TEMP/integration-shards"' in step["run"]
    )
    assert '--coverage-output "$RUNNER_TEMP/coverage-evidence"' in step["run"]
    assert not any("actions/download-artifact@" in s.get("uses", "") for s in job["steps"])
    assert "if" not in step and "continue-on-error" not in step
    check = job["steps"][job["steps"].index(step) + 1]
    assert "scripts/dev-env/integration-shards.py check" in check["run"]
    assert '--count 12 --head "$(git rev-parse HEAD)"' in check["run"]
    assert "if" not in check and "continue-on-error" not in check
    merge = job["steps"][job["steps"].index(check) + 1]
    assert "uv run python scripts/dev-env/python-coverage.py combine" in merge["run"]
    for part in ("a", "b"):
        base = f'"$RUNNER_TEMP/coverage-evidence/workbench-pg-full-coverage-{part}'
        assert f'--job-summary {base}/workbench-pg-coverage.json"' in merge["run"]
        assert f'--job-parts {base}/workbench-pg-coverage-parts"' in merge["run"]
    assert '--integration-dir "$RUNNER_TEMP/integration-shards"' in merge["run"]
    assert '--count 12 --output "$RUNNER_TEMP/coverage-merge"' in merge["run"]
    # The gate must never be redirected to a different configuration or threshold.
    assert "--probe-rcfile" not in merge["run"] and "--fail-under" not in merge["run"]
    assert "if" not in merge and "continue-on-error" not in merge


def test_merging_needs_both_evidence_sets_and_fails_closed_without_them() -> None:
    job = _WORKFLOW["jobs"]["integration_gate"]
    steps = job["steps"]
    shards = next(step for step in steps if step.get("name") == "Require every integration shard")
    assert shards["env"] == {"SHARDS_RESULT": "${{ needs.integration_shards.result }}"}
    measurement = next(
        step for step in steps if step.get("name") == "Require the Python coverage measurement"
    )
    assert measurement["env"] == {"MEASUREMENT_RESULT": "${{ needs.python_tests.result }}"}
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
    final_gate = _WORKFLOW["jobs"]["required_checks"]
    assert "integration_gate" in final_gate["needs"]
    assert _EXPECTED["full"]["INTEGRATION_GATE_RESULT"] == "success"


def test_measured_coverage_is_exported_and_each_shard_uploads_its_dataset() -> None:
    execution = _WORKFLOW["jobs"]["python_tests"]
    steps = execution["steps"]
    coverage = next(step for step in steps if step.get("id") == "coverage")
    assert "uv run python scripts/dev-env/python-coverage.py run" in coverage["run"]
    artifact = next(
        step
        for step in steps
        if str(step.get("with", {}).get("name", "")).startswith("workbench-pg-full-coverage-")
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
    env: dict[str, str], *, matrix_result: str | None = "success"
) -> subprocess.CompletedProcess[str]:
    defaults = {
        "CONTROLLER_RESULT": "success",
        "CONTROLLER_REQUIRED": "false",
        "DOCS_REFS_RESULT": "success",
    }
    if matrix_result is not None:
        defaults["MATRIX_RESULT"] = matrix_result
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _GATE["steps"][0]["run"]],
        env={**defaults, **env},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("python_result", _STATES)
@pytest.mark.parametrize("integration_gate_result", _STATES)
def test_protected_status_never_passes_missing_evidence(
    mode: str, python_result: str, integration_gate_result: str
) -> None:
    assert _GATE["name"] == "Gate: required checks for this change"
    assert set(_GATE["needs"]) == {
        "routing",
        "guards",
        "docs_format",
        "python_gate",
        "integration_gate",
        "integration_shards",
        "web",
        "compose",
        "docs_matrix",
        "docs_refs",
        "reference_acceptance",
        "controller_image",
    }
    assert _GATE["if"] == "${{ always() }}"
    step = _GATE["steps"][0]
    assert step["env"] == {
        "MODE": "${{ needs.routing.outputs.mode }}",
        "ROUTING_RESULT": "${{ needs.routing.result }}",
        "GUARDS_RESULT": "${{ needs.guards.result }}",
        "MATRIX_RESULT": "${{ needs.docs_matrix.result }}",
        "DOCS_REFS_RESULT": "${{ needs.docs_refs.result }}",
        "DOCS_FORMAT_RESULT": "${{ needs.docs_format.result }}",
        "PYTHON_RESULT": "${{ needs.python_gate.result }}",
        "INTEGRATION_GATE_RESULT": "${{ needs.integration_gate.result }}",
        "INTEGRATION_RESULT": "${{ needs.integration_shards.result }}",
        "WEB_RESULT": "${{ needs.web.result }}",
        "COMPOSE_RESULT": "${{ needs.compose.result }}",
        "ACCEPTANCE_RESULT": "${{ needs.reference_acceptance.result }}",
        "CONTROLLER_RESULT": "${{ needs.controller_image.result }}",
        "CONTROLLER_REQUIRED": "${{ needs.controller_image.outputs.required }}",
    }
    states = {
        **_EXPECTED[mode],
        "PYTHON_RESULT": python_result,
        "INTEGRATION_GATE_RESULT": integration_gate_result,
    }
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert (result.returncode == 0) is (states == _EXPECTED[mode])


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("state", _STATES)
def test_final_gate_requires_the_route_own_integration_lanes(mode: str, state: str) -> None:
    states = {**_EXPECTED[mode], "INTEGRATION_RESULT": state}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    expected = "success" if mode in ("web", "full") else "skipped"
    assert (result.returncode == 0) is (state == expected)


@pytest.mark.parametrize("mode", list(_EXPECTED))
def test_final_gate_rejects_missing_integration_result(mode: str) -> None:
    states = {key: value for key, value in _EXPECTED[mode].items() if key != "INTEGRATION_RESULT"}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert result.returncode != 0


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("lane", ["WEB_RESULT", "COMPOSE_RESULT"])
@pytest.mark.parametrize("state", _STATES)
def test_final_gate_requires_the_web_and_compose_results_for_its_scope(
    mode: str, lane: str, state: str
) -> None:
    states = {**_EXPECTED[mode], lane: state}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert (result.returncode == 0) is (states == _EXPECTED[mode])


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("state", _STATES)
def test_final_gate_requires_the_reference_acceptance_entry(mode: str, state: str) -> None:
    states = {**_EXPECTED[mode], "ACCEPTANCE_RESULT": state}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    expected = "skipped" if mode in ("docs", "web") else "success"
    assert (result.returncode == 0) is (state == expected)


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("state", _STATES)
def test_final_gate_requires_the_document_reference_lane(mode: str, state: str) -> None:
    # The reference gate is unfiltered, so no scope may treat a skip as applicable.
    states = {**_EXPECTED[mode], "DOCS_REFS_RESULT": state}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert (result.returncode == 0) is (state == "success")


@pytest.mark.parametrize("mode", list(_EXPECTED))
@pytest.mark.parametrize("state", _STATES)
def test_final_gate_requires_delivery_matrix_evidence(mode: str, state: str) -> None:
    states = {**_EXPECTED[mode], "MATRIX_RESULT": state}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states})
    assert (result.returncode == 0) is (state == "success")


@pytest.mark.parametrize("mode", list(_EXPECTED))
def test_final_gate_rejects_missing_delivery_matrix_result(mode: str) -> None:
    states = {key: value for key, value in _EXPECTED[mode].items() if key != "MATRIX_RESULT"}
    result = _run_gate({"MODE": mode, "ROUTING_RESULT": "success", **states}, matrix_result=None)
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
    gate = _WORKFLOW["jobs"]["python_gate"]
    assert gate["name"] == "Gate: Python tests and static checks"
    assert set(gate["needs"]) == {"routing", "python_tests", "python_static"}
    assert "always()" in gate["if"] and "needs.routing.outputs.mode" in gate["if"]
    step = gate["steps"][0]
    assert step["env"] == {
        "TESTS_RESULT": "${{ needs.python_tests.result }}",
        "STATIC_RESULT": "${{ needs.python_static.result }}",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={"TESTS_RESULT": checks, "STATIC_RESULT": contracts},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (checks == contracts == "success")


def test_route_conformance_gate_runs_before_python_static_checks() -> None:
    steps = _WORKFLOW["jobs"]["python_static"]["steps"]
    gate = next(step for step in steps if step.get("run") == "make contracts-route-conformance")
    assert "if" not in gate and "continue-on-error" not in gate
    static_checks = {"uv run ruff check .", "uv run mypy", "uv run lint-imports"}
    static_indices = [index for index, step in enumerate(steps) if step.get("run") in static_checks]
    assert static_checks <= {step.get("run") for step in steps}
    assert steps.index(gate) < min(static_indices)


def test_full_coverage_and_every_contract_check_run_independently_on_the_same_source() -> None:
    jobs = _WORKFLOW["jobs"]
    execution, contracts = jobs["python_tests"], jobs["python_static"]
    checkouts = []
    for job in (execution, contracts):
        assert job["needs"] == ["routing"]
        assert (
            job["if"]
            == "${{ needs.routing.outputs.mode == 'full' || needs.routing.outputs.mode == 'web' "
            "|| needs.routing.outputs.mode == 'no-db' }}"
        )
        steps = job["steps"]
        assert any(step.get("run") == "bash scripts/dev-env/uv-sync-retry.sh" for step in steps)
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
    assert "--pg-bin /usr/lib/postgresql/16/bin --workers 4" in coverage["run"]
    assert "--partitions 8 --index-base ${{ matrix.index_base }}" in coverage["run"]
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
        step
        for step in steps
        if str(step.get("with", {}).get("name", "")).startswith("workbench-pg-full-coverage-")
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
    routing = jobs["routing"]
    commands = "\n".join(step.get("run", "") for step in routing["steps"])
    # The routing-boundary suite owns its own unfiltered lane; the routing job keeps
    # only checkout and the scope decision, so heavy lanes start sooner.
    guards_commands = "\n".join(step.get("run", "") for step in jobs["guards"]["steps"])
    assert "python3 -m unittest discover -s tests/ci" in guards_commands
    assert "python3 -m unittest discover -s tests/ci" not in commands
    assert "if" not in jobs["guards"] and "needs" not in jobs["guards"]
    assert "scripts/dev-env/ci-routing.py" in commands
    assert "github.event.pull_request.base.sha" in str(routing)
    assert "uv sync" not in commands and "pnpm install" not in commands
    assert "services" not in routing and "needs" not in routing
    assert jobs["integration_shards"]["if"] == (
        "${{ needs.routing.outputs.mode == 'full' || needs.routing.outputs.mode == 'web' }}"
    )
    assert (
        jobs["compose"]["if"]
        == "${{ needs.routing.outputs.mode == 'full' || needs.routing.outputs.mode == 'no-db' }}"
    )
    assert jobs["web"]["if"] == jobs["python_tests"]["if"]


def test_prose_only_changes_owe_the_markdown_format_lane() -> None:
    job = _WORKFLOW["jobs"]["docs_format"]
    assert job["name"] == "Docs: markdown code block formatting"
    assert job["needs"] == ["routing"]
    # The other three scopes run the same command inside `python_static`; running
    # this lane there too would duplicate the check, skipping it would leave the hole.
    assert job["if"] == "${{ needs.routing.outputs.mode == 'docs' }}"
    assert isinstance(job["timeout-minutes"], int) and job["timeout-minutes"] <= 5
    runs = [step.get("run", "") for step in job["steps"]]
    assert not any("uv sync" in run for run in runs)
    assert not any("pnpm install" in run for run in runs)
    assert not any(step.get("if") for step in job["steps"])
    checkout = next(step for step in job["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False
    setup = next(step for step in job["steps"] if "astral-sh/setup-uv@" in step.get("uses", ""))
    assert setup["with"]["version"] == "${{ env.UV_VERSION }}"
    # Byte-for-byte the same Ruff invocation the full static lane uses: the checked
    # file set (`.`) is identical, so no Markdown surface is covered by one lane and
    # missed by the other. tests/ci pins the version against uv.lock.
    assert runs[-1] == 'uvx --from "ruff==${RUFF_VERSION}" ruff format --check .'
    static_format = next(
        step["run"]
        for step in _WORKFLOW["jobs"]["python_static"]["steps"]
        if step.get("run", "").endswith("ruff format --check .")
    )
    assert static_format == "uv run ruff format --check ."
    assert runs[-1].split("ruff format", 1)[1] == static_format.split("ruff format", 1)[1]


def test_the_gate_requires_the_markdown_format_result_only_for_prose_scope() -> None:
    assert "docs_format" in _GATE["needs"]
    assert _EXPECTED["docs"]["DOCS_FORMAT_RESULT"] == "success"
    for mode in ("web", "no-db", "full"):
        assert _EXPECTED[mode]["DOCS_FORMAT_RESULT"] == "skipped"
    lane = next(
        step
        for step in _GATE["steps"]
        if step.get("name") == "Require successful routing and every applicable CI result"
    )
    assert lane["env"]["DOCS_FORMAT_RESULT"] == "${{ needs.docs_format.result }}"
    # A docs-format failure is not absorbed by the routing or delivery evidence.
    result = _run_gate(
        {
            "MODE": "docs",
            "ROUTING_RESULT": "success",
            **_EXPECTED["docs"],
            "DOCS_FORMAT_RESULT": "failure",
        }
    )
    assert result.returncode != 0
    assert "docs_format=failure" in result.stdout
