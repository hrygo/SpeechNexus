"""Coverage selection must include real storage paths without weakening either gate."""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url

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
    for path in (
        "tests/integration/test_binding_publication_evidence.py",
        "tests/integration/test_binding_lifecycle_pg.py",
        "tests/integration/test_binding_disable_intent_pg.py",
    ):
        assert command.count(path) == 1
        assert path not in DRIVER.build_pytest_command()
        assert path not in DRIVER.build_pytest_command(coverage=True)
    assert "--cov" in command and "--cov-report=term-missing" in command
    assert "--durations=20" in command
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
    assert runtime.count("tests/integration/test_workbench_runtime_voice_catalogue.py") == 1
    assert not any(argument.startswith("--cov") for argument in runtime)


def test_command_api_rejects_conflicting_modes() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        DRIVER.build_pytest_command(coverage=True, full_coverage=True)


def test_python_coverage_fragment_keeps_canonical_roots_and_combines_before_gate(
    tmp_path: Path,
) -> None:
    report = tmp_path / "worker.json"
    command = DRIVER.build_pytest_command(
        full_coverage=True, coverage_index=1, coverage_count=2, coverage_report=report
    )
    canonical = DRIVER.build_pytest_command(full_coverage=True)
    assert command[:3] == [
        DRIVER.sys.executable,
        str(ROOT / "scripts/dev-env/python-coverage.py"),
        "worker",
    ]
    assert command[3:10] == ["--index", "1", "--count", "2", "--report", str(report), "--"]
    assert command[10:] == [*canonical[3:], "--cov-fail-under=0"]
    args = DRIVER.parse_arguments(
        [
            "--pg-bin",
            "/synthetic/pg/bin",
            "--full-coverage",
            "--python-shard",
            "1",
            "--python-shard-count",
            "2",
            "--python-shard-report",
            str(report),
            "--output",
            str(tmp_path / "postgres.json"),
        ]
    )
    assert args.python_shard == 1 and args.python_shard_count == 2


@pytest.mark.parametrize(
    "flags",
    [
        ["--python-shard", "0"],
        ["--python-shard-count", "2"],
        ["--python-shard-report", "worker.json"],
        [
            "--full-coverage",
            "--python-shard",
            "0",
            "--python-shard-count",
            "3",
            "--python-shard-report",
            "worker.json",
            "--output",
            "postgres.json",
        ],
        [
            "--full-coverage",
            "--python-shard",
            "0",
            "--python-shard-count",
            "2",
            "--python-shard-report",
            "worker.json",
        ],
        [
            "--full-coverage",
            "--python-shard",
            "0",
            "--python-shard-count",
            "2",
            "--python-shard-report",
            "worker.json",
            "--output",
            "worker.json",
        ],
        [
            "--full-coverage",
            "--python-shard",
            "0",
            "--python-shard-count",
            "2",
            "--python-shard-report",
            "worker.json",
            "--output",
            "postgres.json",
            "--browser",
        ],
    ],
)
def test_python_coverage_fragment_rejects_incomplete_or_conflicting_selection(
    flags: list[str],
) -> None:
    with pytest.raises(SystemExit):
        DRIVER.parse_arguments(["--pg-bin", "/synthetic/pg/bin", *flags])


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


def test_shard_command_delegates_the_complete_run_and_report_to_the_original_runner(
    tmp_path: Path,
) -> None:
    report = tmp_path / "shard report.json"
    assert DRIVER.build_integration_shard_command(index=2, count=6, report=report) == [
        DRIVER.sys.executable,
        "scripts/dev-env/integration-shards.py",
        "run",
        "--index",
        "2",
        "--count",
        "6",
        "--report",
        str(report),
    ]


@pytest.mark.parametrize("index,count", [(-1, 6), (6, 6), (0, 0), (0, 17), (True, 6)])
def test_shard_builder_rejects_invalid_partition(index: int, count: int, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DRIVER.build_integration_shard_command(index=index, count=count, report=tmp_path / "r.json")


def test_shard_arguments_have_an_independent_default_driver_report(tmp_path: Path) -> None:
    report = tmp_path / "integration-shard-0.json"
    args = DRIVER.parse_arguments(
        [
            "--pg-bin",
            "/synthetic/pg/bin",
            "--integration-shard",
            "0",
            "--shard-count",
            "6",
            "--shard-report",
            str(report),
        ]
    )
    assert args.integration_shard == 0 and args.shard_count == 6
    assert args.shard_report == report
    assert args.output == tmp_path / "workbench-pg-shard-0.json"
    assert args.output != args.shard_report


@pytest.mark.parametrize(
    "extra",
    [
        ["--coverage"],
        ["--full-coverage"],
        ["--workbench-runtime"],
        ["--browser"],
        ["--admin-workspaces"],
    ],
)
def test_shard_mode_rejects_probe_selection_modes(extra: list[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        DRIVER.parse_arguments(
            [
                "--pg-bin",
                "/synthetic/pg/bin",
                "--integration-shard",
                "0",
                "--shard-count",
                "6",
                "--shard-report",
                str(tmp_path / "shard.json"),
                *extra,
            ]
        )
    assert raised.value.code == 2


@pytest.mark.parametrize(
    "flags",
    [
        ["--integration-shard", "0"],
        ["--shard-count", "6"],
        ["--shard-report", "r.json"],
    ],
)
def test_shard_identity_and_report_are_required_together(flags: list[str]) -> None:
    with pytest.raises(SystemExit):
        DRIVER.parse_arguments(["--pg-bin", "/synthetic/pg/bin", *flags])


def test_shard_report_cannot_be_overwritten_by_the_driver_report(tmp_path: Path) -> None:
    report = tmp_path / "same.json"
    with pytest.raises(SystemExit):
        DRIVER.parse_arguments(
            [
                "--pg-bin",
                "/synthetic/pg/bin",
                "--integration-shard",
                "0",
                "--shard-count",
                "6",
                "--shard-report",
                str(report),
                "--output",
                str(report),
            ]
        )


def test_private_pg_environment_keeps_external_databases_and_minio(tmp_path: Path) -> None:
    inherited = {
        "SPEECHNEXUS_TEST_DATABASE_URL": "postgresql://isolated-coordination/test",
        "SPEECHNEXUS_TEST_MYSQL_URL": "mysql://isolated-coordination/test",
        "SPEECHNEXUS_TEST_MINIO_ENDPOINT": "http://127.0.0.1:59000",
        "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY": "synthetic-access",
        "SPEECHNEXUS_TEST_MINIO_SECRET_KEY": "synthetic-secret",
        "SPEECHNEXUS_WORKBENCH_TEST_DSN": "postgresql://must-never-use/shared",
        "PGHOSTADDR": "must-not-use",
        "PGSERVICE": "must-not-use",
        "PGOPTIONS": "must-not-use",
    }
    env = DRIVER.private_pg_environment(inherited, sockets=tmp_path / "socket")
    for name, value in inherited.items():
        if name.startswith("SPEECHNEXUS_TEST_"):
            assert env[name] == value
    assert not {"PGHOSTADDR", "PGSERVICE", "PGOPTIONS"} & env.keys()
    private = conninfo_to_dict(env["SPEECHNEXUS_WORKBENCH_TEST_DSN"])
    assert private == {
        "host": str(tmp_path / "socket"),
        "port": "55439",
        "dbname": "speechnexus_workbench_test",
        "user": "workbench_test_owner",
    }


def test_full_coverage_speech_dsn_targets_only_its_private_socket(tmp_path: Path) -> None:
    socket = tmp_path / "socket with # space"
    env = DRIVER.private_pg_environment(
        {
            "SPEECHNEXUS_TEST_DATABASE_URL": "postgresql://external/shared",
            "SPEECHNEXUS_TEST_MYSQL_URL": "mysql://external/test",
            "PGHOSTADDR": "external",
        },
        sockets=socket,
        full_coverage=True,
    )
    dsn = env["SPEECHNEXUS_TEST_DATABASE_URL"]
    assert conninfo_to_dict(dsn) == {
        "host": str(socket),
        "port": "55439",
        "dbname": "speechnexus_workbench_test",
        "user": "workbench_test_owner",
    }
    sqlalchemy_url = make_url(dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    assert sqlalchemy_url.username == "workbench_test_owner"
    assert sqlalchemy_url.database == "speechnexus_workbench_test"
    assert sqlalchemy_url.host is None
    assert sqlalchemy_url.query == {"host": str(socket), "port": "55439"}
    assert env["SPEECHNEXUS_TEST_MYSQL_URL"] == "mysql://external/test"
    assert "PGHOSTADDR" not in env


def write_progress(path: Path, **fields: object) -> Path:
    data = {
        "version": 1,
        "selected": 32,
        "completed": 16,
        "passed": 14,
        "failed": 2,
        "skipped": 0,
        "last_started": "tests/integration/test_demo.py::test_demo_case[110]",
        "last_completed": "tests/integration/test_demo.py::test_demo_case[87]",
        "elapsed_seconds": 4.6,
        **fields,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_injected_timeout_reports_progress_names_limit_and_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "integration-shard-0.json"
    progress = write_progress(
        DRIVER.shard_progress_path(report),
        passed=16,
        failed=0,
        skipped=0,
        last_started="tests/integration/test_demo.py::test_demo_case[110]",
        last_completed="tests/integration/test_demo.py::test_demo_case[87]",
    )
    result: dict[str, object] = {}
    with pytest.raises(subprocess.TimeoutExpired):
        DRIVER.run_test_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            env=dict(os.environ),
            log_path=tmp_path / "pytest.log",
            timeout_seconds=1,
            progress_path=progress,
            job_timeout_seconds=1800,
            result=result,
        )
    err = capsys.readouterr().err
    assert "completed 16 of 32 selected cases" in err
    assert "16 passed, 0 failed, 0 skipped" in err
    assert "last started test: tests/integration/test_demo.py::test_demo_case[110]" in err
    assert "last completed test: tests/integration/test_demo.py::test_demo_case[87]" in err
    assert "probe limit 1 s inside job limit 1800 s" in err
    assert "suggested action: the suite was still advancing" in err
    facts = result["pytest_timeout"]
    assert isinstance(facts, dict)
    assert facts["completed_cases"] == 16
    assert facts["failure_evidence"] is False
    assert facts["phase"] == "execution"


def test_zero_percent_progress_line_does_not_divide_by_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "pytest.log"
    log.write_text(".... [ 0%]\n", encoding="utf-8")
    facts = DRIVER.collect_timeout_diagnostics(
        log_path=log,
        progress_path=None,
        elapsed=5.0,
        limit=5,
        job_limit=1800,
    )
    assert facts["completed_cases"] == 4
    assert "percent" not in facts
    DRIVER.print_timeout_diagnostics(facts)
    err = capsys.readouterr().err
    assert "about 4 cases completed" in err
    assert "suggested action:" in err


def test_failure_markers_do_not_claim_the_suite_was_healthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    progress = write_progress(
        tmp_path / "progress-integration-shard-0.json",
        completed=4,
        passed=2,
        failed=2,
        skipped=0,
        selected=10,
    )
    facts = DRIVER.collect_timeout_diagnostics(
        log_path=tmp_path / "missing-pytest.log",
        progress_path=progress,
        elapsed=5.0,
        limit=5,
        job_limit=1800,
    )
    assert facts["failed_cases"] == 2
    assert facts["failure_evidence"] is True
    DRIVER.print_timeout_diagnostics(facts)
    err = capsys.readouterr().err
    assert "2 passed, 2 failed, 0 skipped" in err
    assert "failure/error markers were recorded" in err
    assert "still advancing" not in err


def test_live_progress_name_is_not_a_shard_report() -> None:
    name = DRIVER.shard_progress_path(Path("integration-shard-0.json")).name
    assert name.startswith("progress-")
    assert not fnmatch.fnmatch(name, "integration-shard-*.json")


def test_full_coverage_timeout_calls_out_the_missing_reserve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    facts = DRIVER.collect_timeout_diagnostics(
        log_path=tmp_path / "missing-pytest.log",
        progress_path=None,
        elapsed=600.0,
        limit=600,
        job_limit=600,
    )
    DRIVER.print_timeout_diagnostics(facts)
    err = capsys.readouterr().err
    assert "no reserve" in err
    assert "0 s (0%)" not in err
