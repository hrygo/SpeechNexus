"""Coverage selection must include real storage paths without weakening either gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "workbench_pg_probe_driver", ROOT / "scripts/dev-env/workbench-pg-probe.py"
)
assert SPEC is not None and SPEC.loader is not None
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def test_coverage_modes_are_explicit_and_mutually_exclusive() -> None:
    parser = DRIVER.argument_parser()
    plain = parser.parse_args(["--pg-bin", "/synthetic/pg/bin"])
    assert not plain.coverage and not plain.full_coverage
    full = parser.parse_args(["--pg-bin", "/synthetic/pg/bin", "--full-coverage"])
    assert full.full_coverage and not full.coverage
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["--pg-bin", "/synthetic/pg/bin", "--coverage", "--full-coverage"])
    assert raised.value.code == 2


def test_scoped_coverage_retains_its_sources_and_ninety_percent_gate(tmp_path: Path) -> None:
    command = DRIVER.build_pytest_command(coverage=True, coverage_output=tmp_path / "cov.json")
    assert "--cov=speechnexus_backend.workbench.infrastructure" in command
    assert "--cov=speechnexus_backend.workbench.application.minutes_tasks" in command
    assert "--cov=speechnexus_backend.workbench.domain.minutes_tasks" in command
    assert "--cov-fail-under=90" in command
    assert "--cov" not in command
    assert "tests/unit" not in command
    assert "--cov-report=term-missing" in command


def test_full_coverage_combines_original_python_gate_and_all_real_pg_tests(tmp_path: Path) -> None:
    command = DRIVER.build_pytest_command(full_coverage=True, coverage_output=tmp_path / "cov.json")
    assert command[:3] == [DRIVER.sys.executable, "-m", "pytest"]
    for path in (
        "tests/unit",
        "tests/contract",
        "tests/ci",
        "tests/integration/test_probe_inventory.py",
    ):
        assert command.count(path) == 1
    for path in DRIVER.WORKBENCH_PG_TESTS + DRIVER.WORKBENCH_RUNTIME_TESTS:
        assert command.count(path) == 1
    assert "--cov" in command and "--cov-report=term-missing" in command
    assert f"--cov-report=json:{tmp_path / 'cov.json'}" in command
    assert not any(
        argument.startswith(("--cov=", "--cov-fail-under", "--cov-omit")) for argument in command
    )
    assert "-m" not in command[3:] and "-k" not in command[3:]
    # Explicit unit files would run twice beside their containing root.
    assert not any(path.startswith("tests/unit/") for path in command)


def test_plain_probe_and_optional_runtime_still_run_without_coverage() -> None:
    plain = DRIVER.build_pytest_command()
    runtime = DRIVER.build_pytest_command(workbench_runtime=True)
    assert "tests/integration/test_workbench_versions_postgres.py" in plain
    assert "tests/unit/backend/test_workbench_postgres_cleanup.py" in plain
    assert "tests/integration/test_workbench_runtime.py" not in plain
    assert "tests/integration/test_workbench_runtime.py" in runtime
    assert runtime.count("tests/integration/test_workbench_runtime_export_dispatch.py") == 1
    assert not any(argument.startswith("--cov") for argument in runtime)


def test_command_api_rejects_conflicting_modes() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        DRIVER.build_pytest_command(coverage=True, full_coverage=True)


def test_full_coverage_requires_complete_explicit_object_storage() -> None:
    configured = {
        "SPEECHNEXUS_TEST_MINIO_ENDPOINT": "http://127.0.0.1:59000",
        "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY": "synthetic-access",
        "SPEECHNEXUS_TEST_MINIO_SECRET_KEY": "synthetic-secret",
    }
    DRIVER.require_object_storage(configured)
    for missing in configured:
        incomplete = configured.copy()
        incomplete.pop(missing)
        with pytest.raises(ValueError, match="isolated MinIO") as raised:
            DRIVER.require_object_storage(incomplete)
        assert "synthetic-secret" not in str(raised.value)


def test_python_test_files_outside_integration_are_in_the_full_roots() -> None:
    covered_roots = {ROOT / path for path in DRIVER.NON_INTEGRATION_TEST_PATHS}
    for path in (ROOT / "tests").rglob("test*.py"):
        if "integration" not in path.relative_to(ROOT / "tests").parts:
            assert any(path == root or path.is_relative_to(root) for root in covered_roots), path
