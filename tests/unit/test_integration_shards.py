"""A missing test/result/shard cannot masquerade as a successful integration gate."""

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/integration-shards.py"
SPEC = importlib.util.spec_from_file_location("integration_shard_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)
HEAD = "a" * 40
NODEIDS = sorted(
    [f"tests/integration/test_demo.py::test_{i}" for i in range(24)]
    + [
        "tests/integration/test_gateway_submit_ledger.py::test_pg_only[mysql]",
    ]
)


def reports(directory: Path) -> list[Path]:
    directory.mkdir(exist_ok=True)
    result = []
    for index in range(2):
        selected = GATE.partition(NODEIDS, index, 2)
        data = {
            "version": 1,
            "source_head": HEAD,
            "shard_index": index,
            "shard_count": 2,
            "collection_digest": GATE.collection_digest(NODEIDS),
            "collection": NODEIDS,
            "selected": selected,
            "outcomes": {
                nodeid: "skipped" if nodeid.endswith("[mysql]") else "passed" for nodeid in selected
            },
            "exit_code": 0,
            "executed": True,
            "elapsed_seconds": 10,
        }
        path = directory / f"integration-shard-{index}.json"
        path.write_text(json.dumps(data))
        result.append(path)
    return result


def test_stable_assignment_does_not_depend_on_python_hash_randomization() -> None:
    ids = [f"tests/integration/test_demo.py::test_{i}" for i in range(4)]
    assert GATE.partition(ids, 0, 2) == [ids[0], ids[2], ids[3]]
    assert GATE.partition(list(reversed(ids)), 1, 2) == [ids[1]]


def test_complete_union_reports_skips_separately_from_passes(tmp_path: Path) -> None:
    result = GATE.verify_reports(reports(tmp_path), source_head=HEAD, count=2)
    assert result.collected == 25 and result.passed == 24
    assert result.skipped == (NODEIDS[-1],)


@pytest.mark.parametrize(
    "case",
    [
        "missing-shard",
        "duplicate-shard",
        "wrong-head",
        "different-collection",
        "wrong-digest",
        "missing-test",
        "duplicate-test",
        "missing-result",
        "extra-result",
        "failed",
        "unexpected-skip",
        "xfail",
        "collect-only",
        "failed-exit",
        "boolean-exit",
        "boolean-version",
        "boolean-index",
        "wrong-count",
        "bad-duration",
        "unknown-field",
        "unsorted-collection",
    ],
)
def test_incomplete_or_conflicting_evidence_is_rejected(tmp_path: Path, case: str) -> None:
    paths = reports(tmp_path)
    data = json.loads(paths[0].read_text())
    nodeid = data["selected"][0]
    if case == "missing-shard":
        paths.pop()
    elif case == "duplicate-shard":
        paths = [paths[0], paths[0]]
    elif case == "wrong-head":
        data["source_head"] = "b" * 40
    elif case == "different-collection":
        data["collection"] = sorted([*NODEIDS, "tests/integration/test_new.py::test_extra"])
        data["collection_digest"] = GATE.collection_digest(data["collection"])
        data["selected"] = GATE.partition(data["collection"], 0, 2)
        data["outcomes"] = dict.fromkeys(data["selected"], "passed")
    elif case == "wrong-digest":
        data["collection_digest"] = "sha256:" + "0" * 64
    elif case == "missing-test":
        data["selected"].pop()
    elif case == "duplicate-test":
        data["selected"].append(nodeid)
    elif case == "missing-result":
        data["outcomes"].pop(nodeid)
    elif case == "extra-result":
        data["outcomes"]["tests/integration/test_new.py::test_extra"] = "passed"
    elif case in ("failed", "unexpected-skip", "xfail"):
        data["outcomes"][nodeid] = {
            "failed": "failed",
            "unexpected-skip": "skipped",
            "xfail": "xfailed",
        }[case]
    elif case == "collect-only":
        data["executed"] = False
    elif case == "failed-exit":
        data["exit_code"] = 1
    elif case == "boolean-exit":
        data["exit_code"] = False
    elif case == "boolean-version":
        data["version"] = True
    elif case == "boolean-index":
        data["shard_index"] = False
    elif case == "wrong-count":
        data["shard_count"] = 3
    elif case == "bad-duration":
        data["elapsed_seconds"] = float("nan")
    elif case == "unknown-field":
        data["private_field"] = True
    else:
        data["collection"] = list(reversed(data["collection"]))
    paths[0].write_text(json.dumps(data))
    with pytest.raises(ValueError):
        GATE.verify_reports(paths, source_head=HEAD, count=2)


def test_duplicate_json_fields_and_oversized_reports_cannot_override_evidence(
    tmp_path: Path,
) -> None:
    paths = reports(tmp_path)
    original = paths[0].read_text()
    paths[0].write_text(original[:-1] + ',"exit_code":0}')
    with pytest.raises(ValueError, match="Duplicate"):
        GATE.verify_reports(paths, source_head=HEAD, count=2)
    paths[0].write_text(" " * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="oversized"):
        GATE.verify_reports(paths, source_head=HEAD, count=2)


def test_checker_needs_no_site_packages_and_parses_on_runner_python(tmp_path: Path) -> None:
    ast.parse(SCRIPT.read_text(), feature_version=(3, 12))
    reports(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(SCRIPT),
            "check",
            "--count",
            "2",
            "--head",
            HEAD,
            "--reports-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "25 collected, 24 passed, 1 existing PG-only" in result.stdout


def temp_project(directory: Path) -> tuple[Path, Path, dict[str, str]]:
    root = directory / "project"
    target = root / "scripts/dev-env/integration-shards.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, target)
    tests = root / "tests/integration"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers = ["integration: synthetic integration"]\n'
    )
    (tests / "test_primary.py").write_text("""import os
import pytest
pytestmark = pytest.mark.integration
@pytest.mark.parametrize("index", range(24))
def test_primary(index):
    assert not (os.environ.get("SYNTHETIC_FAIL_CASE") and index == 0)
""")
    (tests / "test_gateway_submit_ledger.py").write_text("""import pytest
pytestmark = pytest.mark.integration
@pytest.mark.parametrize("database", ["postgresql", "mysql"])
def test_pg_only(database):
    if database == "mysql":
        pytest.skip("synthetic PG-only adapter parameter")
""")
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Synthetic CI",
            "-c",
            "user.email=synthetic@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["SPEECHNEXUS_TEST_DATABASE_URL"] = "synthetic-isolated-postgres"
    env["SPEECHNEXUS_TEST_MYSQL_URL"] = "synthetic-isolated-mysql"
    return root, target, env


def test_real_runner_does_not_hide_failure_or_mistake_collection_for_execution(
    tmp_path: Path,
) -> None:
    root, target, env = temp_project(tmp_path)
    directory = tmp_path / "reports"
    paths = [directory / f"integration-shard-{index}.json" for index in range(2)]

    def run(
        index: int, *, collect: bool = False, fail: bool = False
    ) -> subprocess.CompletedProcess[str]:
        flags = ["--collect-only"] if collect else []
        actual = {**env, "SYNTHETIC_FAIL_CASE": "1"} if fail else env
        return subprocess.run(
            [
                sys.executable,
                str(target),
                "run",
                "--index",
                str(index),
                "--count",
                "2",
                "--report",
                str(paths[index]),
                *flags,
            ],
            cwd=root,
            env=actual,
            capture_output=True,
            text=True,
            check=False,
        )

    for index in range(2):
        result = run(index)
        assert result.returncode == 0, result.stdout + result.stderr
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    evidence = GATE.verify_reports(paths, source_head=head, count=2)
    assert evidence.collected == 26 and evidence.passed == 25 and len(evidence.skipped) == 1
    assert run(0, collect=True).returncode == 0
    with pytest.raises(ValueError, match="Collect-only"):
        GATE.verify_reports(paths, source_head=head, count=2)
    failed_id = "tests/integration/test_primary.py::test_primary[0]"
    failed_index = next(index for index in range(2) if GATE.partition([failed_id], index, 2))
    failed = run(failed_index, fail=True)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    failed_report = json.loads(paths[failed_index].read_text())
    assert failed_report["outcomes"][failed_id] == "failed"
    assert failed_report["exit_code"] == 1


@pytest.mark.parametrize("setting", ["PYTEST_ADDOPTS", "missing-database"])
def test_environment_cannot_silently_select_a_partial_or_unconfigured_suite(
    tmp_path: Path, setting: str
) -> None:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["SPEECHNEXUS_TEST_DATABASE_URL"] = "synthetic-isolated-postgres"
    env["SPEECHNEXUS_TEST_MYSQL_URL"] = "synthetic-isolated-mysql"
    if setting == "PYTEST_ADDOPTS":
        env["PYTEST_ADDOPTS"] = "-k nonexistent"
    else:
        env.pop("SPEECHNEXUS_TEST_MYSQL_URL")
    report = tmp_path / "integration-shard-0.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "run",
            "--index",
            "0",
            "--count",
            "2",
            "--report",
            str(report),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert not report.exists()


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
def test_protected_gate_keeps_original_names_and_fails_on_missing_execution(result: str) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    assert {
        "Python static checks and unit tests",
        "PostgreSQL and InnoDB integration",
        "Python quality and tests",
        "Web quality, tests, and build",
        "Deployment configuration",
    } <= {job["name"] for job in jobs.values()}
    shards = jobs["integration_shards"]
    assert shards["strategy"] == {
        "fail-fast": False,
        "max-parallel": 6,
        "matrix": {"shard": [0, 1, 2, 3, 4, 5]},
    }
    assert set(shards["services"]) == {"postgres", "mysql"}
    gate = jobs["coordination"]
    assert gate["needs"] == ["changes", "integration_shards"] and "always()" in gate["if"]
    assert "needs.changes.outputs.mode == 'full'" in gate["if"]
    condition = gate["steps"][0]["run"]
    executed = subprocess.run(
        ["bash", "-e", "-c", condition],
        env={**os.environ, "SHARDS_RESULT": result},
        capture_output=True,
        check=False,
    )
    assert (executed.returncode == 0) == (result == "success")


def test_actual_node_http_consumer_gets_locked_tools_only_in_its_partition() -> None:
    consumer = "test_generated_typescript_client_consumes_actual_loopback_http"
    paths = [
        ROOT / f"tests/integration/test_{resource}_http.py"
        for resource in ("binding", "profile", "binding_defaults")
    ]
    for path in paths:
        definitions = {
            node.name
            for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.FunctionDef)
        }
        assert consumer in definitions
    snapshot_consumer = "test_atomic_parent_snapshot_original_replay_restart_and_fixed_projection"
    snapshot_path = ROOT / "tests/integration/test_binding_defaults_storage.py"
    assert snapshot_consumer in {
        node.name
        for node in ast.parse(snapshot_path.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    expected = (
        *(f"{path.relative_to(ROOT)}::{consumer}[postgresql]" for path in paths),
        *(
            f"{snapshot_path.relative_to(ROOT)}::{snapshot_consumer}[{dialect}-parameters{case}]"
            for dialect in ("postgresql", "mysql")
            for case in range(2)
        ),
    )
    assert expected == GATE.NODE_CONTRACT_TESTS
    selected: list[int] = []
    for index in range(6):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "requirements", "--index", str(index), "--count", "6"],
            capture_output=True,
            text=True,
            check=True,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines())
        assert set(values) == {"node_contracts", "browser"}
        assert set(values.values()) <= {"true", "false"}
        if values["node_contracts"] == "true":
            selected.append(index)
    assert selected == sorted({index for index in range(6) if GATE.partition(expected, index, 6)})
    assert 3 in selected
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"][
        "integration_shards"
    ]["steps"]
    choose = next(step for step in steps if step.get("id") == "integration_tools")
    assert "requirements" in choose["run"] and '--count 6 >> "$GITHUB_OUTPUT"' in choose["run"]
    node = next(
        step
        for step in steps
        if step["name"] == "Set up Node.js for actual contract HTTP consumption"
    )
    deps = next(
        step for step in steps if step["name"] == "Install locked contract runtime dependencies"
    )
    activate = next(
        step for step in steps if step["name"] == "Activate pnpm for integration consumers"
    )
    for step in (node, activate):
        assert step["if"] == (
            "steps.integration_tools.outputs.node_contracts == 'true' || "
            "steps.integration_tools.outputs.browser == 'true'"
        )
    assert deps["if"] == (
        "steps.integration_tools.outputs.node_contracts == 'true' && "
        "steps.integration_tools.outputs.browser != 'true'"
    )
    assert "uv run python scripts/dev-env/integration-shards.py requirements" in choose["run"]
    assert node["with"]["node-version-file"] == ".nvmrc"
    assert "pnpm@${PNPM_VERSION}" in activate["run"]
    assert (
        "--filter @speechnexus/contracts --prod install --frozen-lockfile --ignore-scripts"
        in deps["run"]
    )
    assert steps.index(choose) < steps.index(node) < steps.index(activate) < steps.index(deps)
    assert steps.index(deps) < next(
        i for i, step in enumerate(steps) if "Run a complete integration partition" in step["name"]
    )


def test_browser_consumers_get_locked_web_build_and_chromium_before_execution() -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"][
        "integration_shards"
    ]["steps"]
    dependencies = next(
        step
        for step in steps
        if step["name"] == "Install locked Web dependencies for browser consumers"
    )
    build = next(
        step for step in steps if step["name"] == "Build actual Web bundles for browser consumers"
    )
    chromium = next(
        step for step in steps if step["name"] == "Install Chromium for browser consumers"
    )
    for step in (dependencies, build, chromium):
        assert step["if"] == "steps.integration_tools.outputs.browser == 'true'"
        assert "continue-on-error" not in step
    assert dependencies["run"] == "pnpm install --frozen-lockfile"
    assert build["run"] == "pnpm build"
    assert chromium["run"] == "pnpm exec playwright install --with-deps --only-shell chromium"
    run = next(
        i for i, step in enumerate(steps) if "Run a complete integration partition" in step["name"]
    )
    assert steps.index(dependencies) < steps.index(build) < steps.index(chromium) < run


def test_requirement_collection_routes_real_parameterized_browser_nodeids(tmp_path: Path) -> None:
    root, target, env = temp_project(tmp_path)
    (root / "tests/integration/test_example_browser.py").write_text(
        "import pytest\npytestmark = pytest.mark.integration\n"
        '@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])\n'
        "def test_browser(dialect): pass\n"
    )
    nodeids = [
        *(f"tests/integration/test_primary.py::test_primary[{i}]" for i in range(24)),
        *(
            f"tests/integration/test_gateway_submit_ledger.py::test_pg_only[{db}]"
            for db in ("postgresql", "mysql")
        ),
        *(
            f"tests/integration/test_example_browser.py::test_browser[{db}]"
            for db in ("postgresql", "mysql")
        ),
    ]
    for index in range(6):
        result = subprocess.run(
            [sys.executable, str(target), "requirements", "--index", str(index), "--count", "6"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        expected = GATE.tool_requirements(nodeids, index, 6)
        assert result.stdout == "".join(
            f"{name}={str(value).lower()}\n" for name, value in expected.items()
        )


@pytest.mark.parametrize("failure", ["collection-error", "partial-addopts"])
def test_requirement_collection_cannot_claim_missing_dependencies_are_unneeded(
    tmp_path: Path, failure: str
) -> None:
    root, target, env = temp_project(tmp_path)
    if failure == "collection-error":
        (root / "tests/integration/test_broken.py").write_text("def broken(\n")
    else:
        env["PYTEST_ADDOPTS"] = "-k nonexistent"
    result = subprocess.run(
        [sys.executable, str(target), "requirements", "--index", "0", "--count", "6"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""


def test_every_shard_provisions_private_workbench_pg_without_replacing_dual_database_env() -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"][
        "integration_shards"
    ]["steps"]
    run = next(step for step in steps if "Run a complete integration partition" in step["name"])
    assert "uv run python scripts/dev-env/workbench-pg-probe.py" in run["run"]
    assert "--pg-bin /usr/lib/postgresql/16/bin" in run["run"]
    assert '--integration-shard "${{ matrix.shard }}" --shard-count 6' in run["run"]
    assert '--shard-report "$RUNNER_TEMP/integration-shard-${{ matrix.shard }}.json"' in run["run"]
    assert '--output "$RUNNER_TEMP/workbench-pg-shard-${{ matrix.shard }}.json"' in run["run"]
    assert "--collect-only" not in run["run"]
    assert "if" not in run and "continue-on-error" not in run
    assert run["env"] == {
        "SPEECHNEXUS_TEST_DATABASE_URL": (
            "postgresql://speechnexus:speechnexus_test@127.0.0.1:5432/speechnexus_test"
        ),
        "SPEECHNEXUS_TEST_MYSQL_URL": "mysql://root:speechnexus_test@127.0.0.1:3306/",
        "SPEECHNEXUS_TEST_MINIO_ENDPOINT": "http://127.0.0.1:59000",
        "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY": "speechnexus_test",
        "SPEECHNEXUS_TEST_MINIO_SECRET_KEY": "synthetic_asset_test_secret",
    }
    pg = next(step for step in steps if "private integration cluster" in step["name"])
    assert "if" not in pg and "continue-on-error" not in pg
    assert "test -x /usr/lib/postgresql/16/bin/initdb" in pg["run"]
    assert "--no-install-recommends --yes postgresql-16" in pg["run"]
    assert steps.index(pg) < steps.index(run)
    evidence = next(
        step for step in steps if step["name"] == "Preserve integration partition evidence"
    )
    assert evidence["with"]["path"].splitlines() == [
        "${{ runner.temp }}/integration-shard-${{ matrix.shard }}.json",
        "${{ runner.temp }}/workbench-pg-shard-${{ matrix.shard }}.json",
    ]
    assert evidence["with"]["if-no-files-found"] == "error"
