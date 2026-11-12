#!/usr/bin/env python3
"""Test a disposable Unix-socket-only PG cluster, stop it, and remove its data.

Uses existing PostgreSQL binaries. Never discovers or contacts a running service,
installs software, modifies shared databases, or creates a persistent service.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]

# The probe's inner pytest ceiling is derived from the job budget that contains
# it. GitHub kills the entire job at its timeout-minutes and runs no later step,
# so a fixed inner ceiling that is much smaller than the job leaves no failing
# case and no log, and one that is too close to the job never gets to report.
# The integration partitions run under 30 minutes and the Python coverage job
# under 10 minutes in .github/workflows/ci.yml; keep these pairs in sync.
INTEGRATION_JOB_TIMEOUT_SECONDS = 30 * 60
COVERAGE_JOB_TIMEOUT_SECONDS = 10 * 60
# Setup before the probe starts (container init, checkout, dependency install,
# MinIO, and on Web lanes pnpm plus Chromium) and the probe's own stop-and-report
# tail share the same job budget. Reserve five minutes so the inner deadline
# always fires first: the 2026-11-06 Integration 2 attempt 1 (42251268385) spent
# 98 s in setup before the probe step began; the old two-minute reserve left only
# about 9 s from the inner deadline to job completion (19 s to the runner's hard
# cap).
INTEGRATION_JOB_RESERVE_SECONDS = 300
INTEGRATION_SHARD_PYTEST_TIMEOUT_SECONDS = (
    INTEGRATION_JOB_TIMEOUT_SECONDS - INTEGRATION_JOB_RESERVE_SECONDS
)
# Pre-existing: the full-coverage probe shares the whole Python job budget, so it
# has no reserve and the runner can cancel before the probe reports. #502 leaves
# this mode unchanged; the timeout output calls out the missing reserve.
FULL_COVERAGE_PYTEST_TIMEOUT_SECONDS = COVERAGE_JOB_TIMEOUT_SECONDS
DEFAULT_PYTEST_TIMEOUT_SECONDS = 180
PYTEST_LOG_TAIL_LINES = 50
# pytest -q prints one progress line per terminal row: markers followed by the
# completion percentage. These parse a timed-out log when no live progress file
# exists; the shard runner's progress file remains the exact source.
PYTEST_PROGRESS_PATTERN = re.compile(r"\[\s*(\d+)%\]")
PYTEST_PROGRESS_MARKERS = frozenset(".sxFEX")
PYTEST_TEST_ID_PATTERN = re.compile(r"^(tests/\S+::\S+)", re.MULTILINE)


NON_INTEGRATION_TEST_PATHS = (
    "tests/unit",
    "tests/contract",
    "tests/ci",
    # This module has no integration marker and ran in the original Python gate.
    "tests/integration/test_probe_inventory.py",
)

WORKBENCH_PG_TESTS = (
    "tests/integration/test_workbench_versions_postgres.py",
    "tests/integration/test_workbench_http_postgres.py",
    "tests/integration/test_workbench_http_management.py",
    "tests/integration/test_workbench_http_meetings.py",
    "tests/integration/test_workbench_sealing_postgres.py",
    "tests/integration/test_workbench_listings_postgres.py",
    "tests/integration/test_workbench_transcript_import_postgres.py",
    "tests/integration/test_workbench_transcript_exports_postgres.py",
    "tests/integration/test_workbench_minutes_postgres.py",
    "tests/integration/test_workbench_minutes_tasks_postgres.py",
    "tests/integration/test_workbench_meetings_postgres.py",
    "tests/integration/test_workbench_deletions_postgres.py",
    "tests/integration/test_workbench_deletion_cleanup.py",
    "tests/integration/test_workbench_content_writes.py",
    "tests/integration/test_workbench_owned_deletions.py",
    "tests/integration/test_workbench_runtime_export_dispatch.py",
    "tests/integration/test_workbench_books_postgres.py",
    "tests/integration/test_workbench_render_units_postgres.py",
    "tests/integration/test_workbench_render_units_http.py",
    "tests/integration/test_workbench_assistant_postgres.py",
    "tests/integration/test_workbench_assistant_http.py",
    "tests/integration/test_workbench_exports_postgres.py",
    "tests/integration/test_workbench_exports_http.py",
    "tests/integration/test_workbench_export_worker_postgres.py",
    "tests/integration/test_workbench_recovery_postgres.py",
    "tests/integration/test_workbench_saved_assets_cleanup.py",
    "tests/integration/test_workbench_saved_assets_postgres.py",
)

DEFAULT_EXTRA_TESTS = (
    "tests/unit/backend/test_workbench_postgres_cleanup.py",
    "tests/unit/backend/test_workbench_public_transcript_source.py",
    "tests/unit/backend/test_workbench_llm.py",
    "tests/unit/backend/test_workbench_minutes_tasks.py",
)

WORKBENCH_RUNTIME_TESTS = (
    "tests/integration/test_workbench_runtime.py",
    "tests/integration/test_workbench_runtime_export_dispatch.py",
    "tests/integration/test_workbench_runtime_capabilities.py",
    "tests/integration/test_workbench_runtime_saved_assets_http.py",
    "tests/integration/test_workbench_runtime_voice_catalogue.py",
)

SPEECH_PG_COVERAGE_TESTS = (
    "tests/integration/test_binding_publication_evidence.py",
    "tests/integration/test_binding_lifecycle_pg.py",
    "tests/integration/test_binding_disable_intent_pg.py",
    # Voice draft revisions are production Job-writer paths; their PG tests
    # must contribute to the full Python coverage gate, not only to the
    # separately scheduled integration shards.
    "tests/integration/test_voice_drafts_pg.py",
    "tests/integration/test_public_jobs_http.py",
    "tests/integration/test_voice_draft_revisions_migration_pg.py",
)


def build_pytest_command(
    *,
    coverage: bool = False,
    full_coverage: bool = False,
    workbench_runtime: bool = False,
    coverage_output: Path | None = None,
    coverage_index: int | None = None,
    coverage_count: int | None = None,
    coverage_report: Path | None = None,
) -> list[str]:
    """Keep the canonical full roots; optional fragments qualify only after combination."""
    if coverage and full_coverage:
        raise ValueError("coverage modes are mutually exclusive")
    paths = list(WORKBENCH_PG_TESTS)
    if full_coverage:
        paths = list(NON_INTEGRATION_TEST_PATHS) + paths + list(SPEECH_PG_COVERAGE_TESTS)
    else:
        paths.extend(DEFAULT_EXTRA_TESTS)
    if workbench_runtime or full_coverage:
        paths.extend(WORKBENCH_RUNTIME_TESTS)
    command = [sys.executable, "-m", "pytest", *dict.fromkeys(paths), "-q"]
    if coverage:
        # Retain the existing opt-in C storage qualification without weakening its gate.
        command.extend(
            [
                "--cov=speechnexus_backend.workbench.infrastructure",
                "--cov=speechnexus_backend.workbench.application.minutes_tasks",
                "--cov=speechnexus_backend.workbench.domain.minutes_tasks",
                "--cov-report=term-missing",
                "--cov-fail-under=90",
            ]
        )
    elif full_coverage:
        # No -m filter: the explicitly named PostgreSQL modules carry integration markers.
        # Bare --cov keeps every configured source and the default global fail_under=80.
        command.extend(["--cov", "--cov-report=term-missing", "--durations=20"])
    if (coverage or full_coverage) and coverage_output is not None:
        command.append(f"--cov-report=json:{coverage_output}")
    if coverage_index is not None:
        if (
            not full_coverage
            or type(coverage_count) is not int
            or coverage_count not in (1, 2)
            or type(coverage_index) is not int
            or not 0 <= coverage_index < coverage_count
            or coverage_report is None
        ):
            raise ValueError(
                "coverage partitions require full coverage and an exact identity/report"
            )
        return [
            sys.executable,
            str(ROOT / "scripts/dev-env/python-coverage.py"),
            "worker",
            "--index",
            str(coverage_index),
            "--count",
            str(coverage_count),
            "--report",
            str(coverage_report),
            "--",
            *command[3:],
            "--cov-fail-under=0",
        ]
    if coverage_count is not None or coverage_report is not None:
        raise ValueError("coverage partition metadata requires an index")
    return command


def build_integration_shard_command(
    *,
    index: int,
    count: int,
    report: Path,
    coverage_data: Path,
    progress: Path | None = None,
) -> list[str]:
    """Delegate inventory, partitioning and same-head evidence to the canonical runner."""
    if type(count) is not int or not 1 <= count <= 16:
        raise ValueError("shard count must be within 1..16")
    if type(index) is not int or not 0 <= index < count:
        raise ValueError("shard index is outside the partition")
    if coverage_data.name != f"coverage-{index}.data":
        raise ValueError("shard coverage data must be named coverage-<index>.data")
    command = [
        sys.executable,
        "scripts/dev-env/integration-shards.py",
        "run",
        "--index",
        str(index),
        "--count",
        str(count),
        "--report",
        str(report),
        "--coverage-data",
        str(coverage_data),
    ]
    if progress is not None:
        command.extend(["--progress", str(progress)])
    return command


def shard_progress_path(report: Path) -> Path:
    """Keep the live position beside the report without matching its evidence glob."""
    return report.with_name(f"progress-{report.stem}.json")


def private_pg_environment(
    inherited: Mapping[str, str], *, sockets: Path, full_coverage: bool = False
) -> dict[str, str]:
    """Pin coverage PG to this worker; retain dual-DB inputs for integration shards."""
    # libpq also reads PGHOSTADDR/PGSERVICE/PGOPTIONS. Inherited connection defaults
    # must not redirect the disposable socket cluster, even if other tests use DSNs.
    env = {key: value for key, value in inherited.items() if not key.startswith("PG")}
    env.update(PGHOST=str(sockets), PGPORT="55439", PGUSER="workbench_test_owner")
    env["SPEECHNEXUS_WORKBENCH_TEST_DSN"] = make_conninfo(
        host=str(sockets),
        port=55439,
        dbname="speechnexus_workbench_test",
        user="workbench_test_owner",
    )
    if full_coverage:
        # Both psycopg and SQLAlchemy consume this URI; quoting the socket path
        # prevents spaces and reserved characters from changing its destination.
        env["SPEECHNEXUS_TEST_DATABASE_URL"] = (
            "postgresql://workbench_test_owner@/speechnexus_workbench_test"
            f"?host={quote(str(sockets), safe='')}&port=55439"
        )
    return env


def require_object_storage(env: Mapping[str, str]) -> None:
    """The caller provides a project-isolated MinIO; never discover a shared service."""
    if not all(
        env.get(name)
        for name in (
            "SPEECHNEXUS_TEST_MINIO_ENDPOINT",
            "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY",
            "SPEECHNEXUS_TEST_MINIO_SECRET_KEY",
        )
    ):
        raise ValueError("probe requires explicit isolated MinIO endpoint and test keys")


def run(
    command: list[str], *, env: dict[str, str] | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=ROOT, env=env, check=True, capture_output=True, text=True, timeout=timeout
    )


def stop_cluster(pg_ctl: str, data: Path) -> bool:
    """Only an explicit no-server status and absent PID file confirm shutdown."""
    try:
        status = subprocess.run(
            [pg_ctl, "-D", str(data), "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if status.returncode == 0:
            stop = subprocess.run(
                [pg_ctl, "-D", str(data), "-m", "fast", "-w", "-t", "20", "stop"],
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            if stop.returncode != 0:
                return False
        elif status.returncode != 3:
            return False
        status = subprocess.run(
            [pg_ctl, "-D", str(data), "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return status.returncode == 3 and not (data / "postmaster.pid").exists()
    except OSError, subprocess.TimeoutExpired:
        return False


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--coverage", action="store_true", help="qualify scoped C storage at 90%")
    modes.add_argument(
        "--full-coverage",
        action="store_true",
        help="run the complete Python gate and Workbench PG/runtime tests with global coverage",
    )
    modes.add_argument(
        "--integration-shard",
        type=int,
        metavar="INDEX",
        help="run the canonical complete integration partition with private Workbench PG",
    )
    parser.add_argument("--shard-count", type=int, metavar="COUNT")
    parser.add_argument("--shard-report", type=Path, metavar="PATH")
    parser.add_argument(
        "--shard-coverage",
        type=Path,
        metavar="PATH",
        help="coverage database the integration partition exports; named coverage-<index>.data",
    )
    parser.add_argument("--python-shard", type=int)
    parser.add_argument("--python-shard-count", type=int)
    parser.add_argument("--python-shard-report", type=Path)
    parser.add_argument(
        "--workbench-runtime",
        action="store_true",
        help="also qualify independent workbench bootstrap/lifespan on private PG",
    )
    parser.add_argument(
        "--admin-workspaces",
        action="store_true",
        help="also require production Admin + actual A Workspace HTTP/SQL reads",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="also require production-bundle Chromium qualification",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="retain this run's stopped database for debugging; logs are always retained",
    )
    parser.add_argument("--output", type=Path, help="independent PG lifecycle evidence JSON")
    return parser


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argument_parser()
    args = parser.parse_args(argv)
    if args.python_shard is not None:
        if args.workbench_runtime or args.browser or args.admin_workspaces:
            parser.error("Python coverage partitions cannot add optional probe selections")
        try:
            build_pytest_command(
                full_coverage=args.full_coverage,
                coverage_index=args.python_shard,
                coverage_count=args.python_shard_count,
                coverage_report=args.python_shard_report,
            )
        except ValueError as error:
            parser.error(str(error))
        if args.output is None or args.output.resolve() == args.python_shard_report.resolve():
            parser.error("Python coverage partitions require an independent lifecycle --output")
    elif args.python_shard_count is not None or args.python_shard_report is not None:
        parser.error("Python coverage partition metadata requires --python-shard")
    shard = args.integration_shard is not None
    if shard:
        if args.shard_count is None or args.shard_report is None:
            parser.error("--integration-shard requires --shard-count and --shard-report")
        if args.shard_coverage is None:
            # An integration partition that exports no coverage cannot contribute
            # to the merged global gate, so this is not optional evidence.
            parser.error("--integration-shard requires --shard-coverage")
        if args.workbench_runtime or args.browser or args.admin_workspaces:
            parser.error("integration shard mode cannot use runtime or browser probe selection")
        try:
            build_integration_shard_command(
                index=args.integration_shard,
                count=args.shard_count,
                report=args.shard_report,
                coverage_data=args.shard_coverage,
            )
        except ValueError as error:
            parser.error(str(error))
        if args.output is None:
            args.output = (
                args.shard_report.parent / f"workbench-pg-shard-{args.integration_shard}.json"
            )
        if args.output.resolve() == args.shard_report.resolve():
            parser.error("PG lifecycle --output must differ from --shard-report")
    elif args.shard_count is not None or args.shard_report is not None:
        parser.error("--shard-count and --shard-report require --integration-shard")
    elif args.shard_coverage is not None:
        parser.error("--shard-coverage requires --integration-shard")
    elif args.output is None:
        args.output = ROOT / ".runtime/team-c/c00-pg-probe.json"
    return args


def pytest_timeout_seconds(*, full_coverage: bool, integration_shard: int | None) -> int:
    """Derive the inner ceiling from the outer job budget it runs inside."""
    if integration_shard is not None:
        return INTEGRATION_SHARD_PYTEST_TIMEOUT_SECONDS
    if full_coverage:
        return FULL_COVERAGE_PYTEST_TIMEOUT_SECONDS
    return DEFAULT_PYTEST_TIMEOUT_SECONDS


def pytest_job_timeout_seconds(*, full_coverage: bool, integration_shard: int | None) -> int | None:
    """Return the outer job budget when this mode runs inside a bounded CI job."""
    if integration_shard is not None:
        return INTEGRATION_JOB_TIMEOUT_SECONDS
    if full_coverage:
        return COVERAGE_JOB_TIMEOUT_SECONDS
    return None


def captured_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def display_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def print_log_tail(log_path: Path, *, limit: int = PYTEST_LOG_TAIL_LINES) -> None:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        print(
            f"pytest log unavailable ({type(error).__name__}): {display_path(log_path)}",
            file=sys.stderr,
        )
        return
    tail = lines[-limit:]
    print(f"--- pytest.log tail ({len(tail)} of {len(lines)} lines) ---", file=sys.stderr)
    for line in tail:
        print(line, file=sys.stderr)
    print("--- end of pytest.log tail ---", file=sys.stderr)


class ShardProgress(NamedTuple):
    selected: int
    completed: int
    passed: int
    failed: int
    skipped: int
    last_started: str | None
    last_completed: str | None
    elapsed_seconds: float | None


class PytestProgress(NamedTuple):
    percent: int | None
    markers: int
    failures: int


def optional_count(data: Mapping[str, object], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def read_shard_progress(path: Path | None) -> ShardProgress | None:
    """Read the shard runner's live position; an absent or partial file is not an error."""
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(data, dict):
        return None
    selected = optional_count(data, "selected")
    completed = optional_count(data, "completed")
    if selected is None or completed is None:
        return None
    completed = min(completed, selected)
    failed = optional_count(data, "failed") or 0
    skipped = optional_count(data, "skipped") or 0
    passed = optional_count(data, "passed")
    if passed is None:
        # Older progress files have no outcome split; do not invent failures.
        passed = max(completed - failed - skipped, 0)
    elapsed = data.get("elapsed_seconds")
    last_started = data.get("last_started")
    last_completed = data.get("last_completed")
    return ShardProgress(
        selected=selected,
        completed=completed,
        passed=min(passed, completed),
        failed=min(failed, completed),
        skipped=min(skipped, completed),
        last_started=last_started if isinstance(last_started, str) else None,
        last_completed=last_completed if isinstance(last_completed, str) else None,
        elapsed_seconds=(
            float(elapsed)
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
            else None
        ),
    )


def parse_pytest_progress(text: str) -> PytestProgress:
    """Return the last completion percentage, markers and failure/error markers.

    A killed process usually leaves its final progress line without a newline or
    percentage, so a line made only of markers still counts.
    """
    percent: int | None = None
    markers = 0
    failures = 0
    for line in text.splitlines():
        match = PYTEST_PROGRESS_PATTERN.search(line)
        if match is not None:
            prefix = line[: match.start()]
            percent = int(match.group(1))
        elif line and set(line) <= PYTEST_PROGRESS_MARKERS:
            prefix = line
        else:
            continue
        markers += sum(1 for character in prefix if character in PYTEST_PROGRESS_MARKERS)
        failures += sum(1 for character in prefix if character in "FE")
    return PytestProgress(percent, markers, failures)


def collect_timeout_diagnostics(
    *,
    log_path: Path,
    progress_path: Path | None,
    elapsed: float,
    limit: int,
    job_limit: int | None,
) -> dict[str, object]:
    """Describe how far the suite got before the ceiling fired, without guessing."""
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    parsed = parse_pytest_progress(log_text)
    progress = read_shard_progress(progress_path)
    matches = PYTEST_TEST_ID_PATTERN.findall(log_text)
    last_test = matches[-1] if matches else None
    facts: dict[str, object] = {
        "elapsed_seconds": round(elapsed, 1),
        "limit_seconds": limit,
        "job_limit_seconds": job_limit,
        "reserve_seconds": None if job_limit is None else job_limit - limit,
        "log_percent": parsed.percent,
        "log_completed_markers": parsed.markers,
        "failure_markers": parsed.failures,
        "phase": "execution" if progress is not None or parsed.markers else "collection",
    }
    if progress is not None:
        facts.update(
            {
                "selected_cases": progress.selected,
                "completed_cases": progress.completed,
                "passed_cases": progress.passed,
                "failed_cases": progress.failed,
                "skipped_cases": progress.skipped,
                "remaining_cases": progress.selected - progress.completed,
                "last_started_test": progress.last_started or last_test,
                "last_completed_test": progress.last_completed,
                "progress_elapsed_seconds": progress.elapsed_seconds,
            }
        )
        if progress.selected:
            facts["percent"] = round(progress.completed * 100 / progress.selected)
    elif parsed.markers:
        facts.update(
            {
                "completed_cases": parsed.markers,
                "last_started_test": last_test,
                "last_completed_test": None,
            }
        )
        if parsed.percent:
            estimated_total = round(parsed.markers * 100 / parsed.percent)
            facts.update(
                {
                    "percent": parsed.percent,
                    "estimated_total_cases": estimated_total,
                    "remaining_cases": max(estimated_total - parsed.markers, 0),
                }
            )
    else:
        facts["last_started_test"] = last_test
        facts["last_completed_test"] = None
    facts["failure_evidence"] = bool(facts.get("failed_cases") or parsed.failures)
    completed = progress.completed if progress is not None else 0
    facts["progress_advanced"] = bool(completed > 0 or parsed.markers > 0)
    return facts


def print_timeout_diagnostics(facts: Mapping[str, object]) -> None:
    """Print the timeout facts a reader needs to tell progress from a stuck suite."""
    percent = facts.get("percent")
    completed = facts.get("completed_cases")
    selected = facts.get("selected_cases")
    passed = facts.get("passed_cases")
    failed = facts.get("failed_cases")
    skipped = facts.get("skipped_cases")
    failures = facts.get("failure_markers")
    if isinstance(selected, int) and isinstance(completed, int):
        breakdown = ""
        if isinstance(passed, int) and isinstance(failed, int) and isinstance(skipped, int):
            breakdown = f" ({passed} passed, {failed} failed, {skipped} skipped)"
        print(
            f"pytest progress: {percent if isinstance(percent, int) else 'unknown'}% — "
            f"completed {completed} of {selected} selected cases{breakdown}; "
            f"{facts['remaining_cases']} not completed",
            file=sys.stderr,
        )
    elif isinstance(percent, int) and isinstance(completed, int):
        print(
            f"pytest progress: {percent}% — about {completed} cases completed "
            f"({failures if isinstance(failures, int) else 0} failure/error markers; "
            "estimated from the progress line, no exact total)",
            file=sys.stderr,
        )
    elif isinstance(completed, int) and completed:
        print(
            f"pytest progress: about {completed} cases completed "
            f"({failures if isinstance(failures, int) else 0} failure/error markers); "
            "no completion percentage was published",
            file=sys.stderr,
        )
    else:
        print(
            "pytest progress: no case finished and no progress line was written; "
            "collection was still running",
            file=sys.stderr,
        )
    phase = facts.get("phase")
    progress_elapsed = facts.get("progress_elapsed_seconds")
    if isinstance(progress_elapsed, (int, float)) and not isinstance(progress_elapsed, bool):
        print(
            f"phase: {phase}; progress file last updated {progress_elapsed:.1f} s after "
            "the shard runner started",
            file=sys.stderr,
        )
    else:
        print(f"phase: {phase}", file=sys.stderr)
    started = facts.get("last_started_test")
    finished = facts.get("last_completed_test")
    print(
        f"last started test: {started if isinstance(started, str) else 'unknown'}",
        file=sys.stderr,
    )
    print(
        f"last completed test: {finished if isinstance(finished, str) else 'unknown'}",
        file=sys.stderr,
    )
    job_limit = facts.get("job_limit_seconds")
    reserve = facts.get("reserve_seconds")
    limit = facts.get("limit_seconds")
    if isinstance(job_limit, int) and isinstance(reserve, int) and isinstance(limit, int):
        if reserve <= 0:
            print(
                f"timeout budget: probe limit {limit} s equals the job limit; no reserve, "
                "so the runner may cancel before the probe reports (pre-existing for "
                "--full-coverage)",
                file=sys.stderr,
            )
        else:
            share = reserve * 100 // job_limit if job_limit else 0
            print(
                f"timeout budget: probe limit {limit} s inside job limit {job_limit} s; "
                f"{reserve} s ({share}%) beyond the probe ceiling for setup, dependency "
                "install and the stop/report tail",
                file=sys.stderr,
            )
    else:
        print(
            f"timeout budget: probe limit {limit} s (no outer job budget for this mode)",
            file=sys.stderr,
        )
    if facts.get("failure_evidence"):
        print(
            "suggested action: failure/error markers were recorded before the timeout; "
            "inspect pytest.log and fix the failure instead of changing the budget",
            file=sys.stderr,
        )
    elif facts.get("progress_advanced"):
        print(
            "suggested action: the suite was still advancing and recorded no failure marker; "
            "confirm the shard legitimately grew and raise the budget, otherwise split the "
            "shard instead of raising it (docs/validation/ci-validation.md)",
            file=sys.stderr,
        )
    else:
        print(
            "suggested action: no test completed and no progress line was written; "
            "collection may still be running or the suite may be stuck; inspect pytest.log "
            "before changing the budget",
            file=sys.stderr,
        )


def run_test_command(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    log_path: Path,
    timeout_seconds: int,
    progress_path: Path | None = None,
    job_timeout_seconds: int | None = None,
    result: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run pytest-shaped work and keep its output on disk, including on timeout."""
    # pytest and the shard runner share this process tree. Unbuffered output means
    # a killed child still leaves the last started case on disk instead of an
    # unflushed block.
    child_env = {**env, "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        # subprocess.run raises before the log is written; persist the partial
        # output first so the tail below and the retained evidence both exist.
        output = captured_text(error.stdout) + captured_text(error.stderr)
        log_path.write_text(output, encoding="utf-8")
        elapsed = time.monotonic() - started
        facts = collect_timeout_diagnostics(
            log_path=log_path,
            progress_path=progress_path,
            elapsed=elapsed,
            limit=timeout_seconds,
            job_limit=job_timeout_seconds,
        )
        if result is not None:
            result["pytest_timeout"] = facts
        print(
            f"probe subprocess timed out after {elapsed:.1f} s "
            f"(limit {timeout_seconds} s); pytest log: {display_path(log_path)}",
            file=sys.stderr,
        )
        print_timeout_diagnostics(facts)
        if output:
            print_log_tail(log_path)
        else:
            print(
                "pytest.log is empty: no output was captured before the timeout",
                file=sys.stderr,
            )
        raise
    log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    print(completed.stdout + completed.stderr, end="")
    return completed


def main() -> int:
    args = parse_arguments()
    parser = argument_parser()
    if args.full_coverage or args.integration_shard is not None:
        try:
            require_object_storage(os.environ)
        except ValueError as error:
            parser.error(str(error))
    pg_bin: Path = args.pg_bin.resolve()
    output: Path = args.output.resolve()
    for binary in ("initdb", "pg_ctl", "createdb", "postgres"):
        if not (pg_bin / binary).is_file():
            parser.error(f"missing PostgreSQL binary: {binary}")
    runtime = ROOT / ".runtime/team-c"
    runtime.mkdir(parents=True, exist_ok=True)
    cluster = Path(tempfile.mkdtemp(prefix="pg-", dir=runtime))
    data, sockets = cluster / "data", cluster / "socket"
    sockets.mkdir(mode=0o700)
    if len(str(sockets).encode()) > 80:
        parser.error("workspace path is too long for a private PostgreSQL Unix socket")
    pg_ctl = str(pg_bin / "pg_ctl")
    start_attempted = False
    start_confirmed = False
    stopped = False
    result: dict[str, object] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "kind": "disposable-workbench-postgres",
        "cluster": str(cluster.relative_to(ROOT)),
        "tcp": False,
        "postgres": run([str(pg_bin / "postgres"), "--version"]).stdout.strip(),
        "tests_passed": False,
        "stopped": False,
        "shutdown_observed": None,
    }
    code = 1
    try:
        run(
            [
                str(pg_bin / "initdb"),
                "-D",
                str(data),
                "-U",
                "workbench_test_owner",
                "--auth-local=trust",
                "--auth-host=reject",
                "--no-locale",
                "--encoding=UTF8",
            ]
        )
        with (data / "postgresql.conf").open("a") as config:
            socket_literal = str(sockets).replace("'", "''")
            config.write(
                f"\nlisten_addresses = ''\nunix_socket_directories = '{socket_literal}'\n"
                "unix_socket_permissions = 0700\nport = 55439\nshared_buffers = '32MB'\n"
                "max_connections = 20\nspeechnexus.disposable_workbench_probe = 'on'\n"
            )
        # A failed start may still leave a process: always inspect and stop below.
        start_attempted = True
        run(
            [
                pg_ctl,
                "-D",
                str(data),
                "-l",
                str(cluster / "postgres.log"),
                "-w",
                "-t",
                "20",
                "start",
            ]
        )
        start_confirmed = True
        env = private_pg_environment(os.environ, sockets=sockets, full_coverage=args.full_coverage)
        run([str(pg_bin / "createdb"), "speechnexus_workbench_test"], env=env)
        progress_path: Path | None = None
        if args.integration_shard is not None:
            shard_report = args.shard_report.resolve()
            progress_path = shard_progress_path(shard_report)
            command = build_integration_shard_command(
                index=args.integration_shard,
                count=args.shard_count,
                report=shard_report,
                coverage_data=args.shard_coverage.resolve(),
                progress=progress_path,
            )
            result["integration_shard"] = args.integration_shard
            result["shard_count"] = args.shard_count
            result["shard_report"] = str(shard_report)
            result["shard_coverage"] = str(args.shard_coverage.resolve())
        else:
            command = build_pytest_command(
                coverage=args.coverage,
                full_coverage=args.full_coverage,
                workbench_runtime=args.workbench_runtime,
                coverage_output=cluster / "coverage.json",
                coverage_index=args.python_shard,
                coverage_count=args.python_shard_count,
                coverage_report=args.python_shard_report,
            )
        if args.browser:
            command.append("tests/integration/test_workbench_meeting_browser.py")
            command.append("tests/integration/test_workbench_deletion_browser.py")
            # The bundle against the deployment's own composition root, not a hand-built app.
            command.append("tests/integration/test_workbench_real_runtime_browser.py")
            # The authoring/proofreading page against the same real composition root.
            command.append("tests/integration/test_workbench_real_runtime_manuscripts_browser.py")
            # The assistant surface must stay closed unless the deployment itself says so.
            command.append("tests/integration/test_workbench_real_runtime_assistant_browser.py")
            # The AdminUI diagnostics page against the same real runtime.
            command.append("tests/integration/test_admin_diagnostics_browser.py")
            # Profile drafts use the real A-owned composition/SQL with transport fault injection.
            command.append("tests/integration/test_admin_profiles_browser.py")
            env["SPEECHNEXUS_ADMIN_PROFILES_OUTPUT"] = str(
                args.output.resolve().parent / (args.output.stem + "-admin-profiles")
            )
            # The deployment teleprompter state consumed by the production bundle.
            command.append(
                "tests/integration/test_workbench_real_runtime_prompter_state_browser.py"
            )
            # The audiobook flow: a chapter created in the production bundle, verified over HTTP.
            command.append(
                "tests/integration/test_workbench_real_runtime_chapter_create_browser.py"
            )
            # Public committed-content download through the production Web consumer.
            command.append("tests/integration/test_workbench_real_runtime_download_browser.py")
            # Native public voice catalogue -> actual C runtime -> production consumer.
            command.append(
                "tests/integration/test_workbench_real_runtime_voice_catalogue_browser.py"
            )
            # The realtime tab must not open the microphone before a session is connected.
            command.append(
                "tests/integration/test_workbench_real_runtime_realtime_consent_browser.py"
            )
            # Guards the probe inventory: no C-side probe may be wrapped by nothing.
            command.append("tests/integration/test_probe_inventory.py")
            # Two self-contained synthetic realtime producers (ASR sessions, TTS rendering).
            command.append("tests/integration/test_workbench_realtime_synthetic_probes.py")
            # Five self-contained probes that existed but ran in no gate: capture graph, media
            # worklet, voice creation, the Admin task centre and the file workflow demo.
            command.append("tests/integration/test_workbench_orphan_probes.py")
            env["SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT"] = str(
                args.output.resolve().parent / (args.output.stem + "-browser")
            )
            result["browser"] = "production-bundle-real-c-http-private-pg-fake-llm-and-real-runtime"
        if args.admin_workspaces:
            command.append("tests/integration/test_admin_workspaces_browser.py")
            env["SPEECHNEXUS_ADMIN_WORKSPACES_OUTPUT"] = str(
                args.output.resolve().parent / (args.output.stem + "-admin-workspaces")
            )
            result["admin_workspaces"] = "production-admin-real-a-http-private-pg-explicit-service"
        if args.workbench_runtime or args.full_coverage:
            result["workbench_runtime"] = "real-c-runtime-private-pg"
        if args.coverage or args.full_coverage:
            env["COVERAGE_FILE"] = str(cluster / ".coverage")
            result["coverage_mode"] = (
                "full-python-coverage-fragment"
                if args.python_shard is not None
                else "full-python-and-workbench-pg"
                if args.full_coverage
                else "scoped-c"
            )
            result["coverage_log"] = str((cluster / "coverage.json").relative_to(ROOT))
        pytest_log = cluster / "pytest.log"
        test_timeout = pytest_timeout_seconds(
            full_coverage=args.full_coverage, integration_shard=args.integration_shard
        )
        job_timeout = pytest_job_timeout_seconds(
            full_coverage=args.full_coverage, integration_shard=args.integration_shard
        )
        result["pytest_timeout_seconds"] = test_timeout
        result["pytest_job_timeout_seconds"] = job_timeout
        tests = run_test_command(
            command,
            env=env,
            log_path=pytest_log,
            timeout_seconds=test_timeout,
            progress_path=progress_path,
            job_timeout_seconds=job_timeout,
            result=result,
        )
        result["tests_passed"] = tests.returncode == 0
        result["pytest_log"] = str(pytest_log.relative_to(ROOT))
        code = tests.returncode
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        # All child inputs are this probe's own synthetic identities, never a shared DSN.
        if isinstance(error, subprocess.TimeoutExpired) and (cluster / "pytest.log").exists():
            # run_test_command already printed the elapsed time and tail; retain
            # the same facts in the lifecycle report. A PG command timeout never
            # creates this log, so its absence is not claimed as pytest evidence.
            result["pytest_log"] = str((cluster / "pytest.log").relative_to(ROOT))
        print(f"probe subprocess failed: {type(error).__name__}", file=sys.stderr)
        result["failure"] = type(error).__name__
    except (OSError, KeyboardInterrupt) as error:
        result["failure"] = type(error).__name__
        raise
    finally:
        if start_attempted:
            shutdown_observed = stop_cluster(pg_ctl, data)
            result["shutdown_observed"] = shutdown_observed
            # A failed or interrupted start can still complete later, even when
            # status currently reports no server. Do not claim it has stopped.
            stopped = start_confirmed and shutdown_observed
        result["stopped"] = stopped
        result["data_removed"] = False
        if start_attempted and not start_confirmed:
            result["data_retained_reason"] = "startup-unconfirmed"
            code = 1
        elif start_attempted and not stopped:
            result["data_retained_reason"] = "shutdown-unconfirmed"
            code = 1
        elif args.keep_data:
            result["data_retained_reason"] = "requested"
        else:
            try:
                # This path belongs solely to the cluster created above, including a
                # partial initdb that never started a server. Keep all sibling evidence.
                if data.exists():
                    shutil.rmtree(data)
                result["data_removed"] = True
            except OSError as error:
                result["data_retained_reason"] = "cleanup-failed"
                result["cleanup_failure"] = type(error).__name__
                code = 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"evidence: {output.relative_to(ROOT) if output.is_relative_to(ROOT) else output}")
        if result.get("data_retained_reason") == "startup-unconfirmed":
            print(
                f"probe startup unconfirmed; inspect retained cluster: {cluster}", file=sys.stderr
            )
        elif start_attempted and not stopped:
            print(
                f"probe shutdown unconfirmed; inspect retained cluster: {cluster}", file=sys.stderr
            )
        elif result.get("data_retained_reason") == "cleanup-failed":
            print(f"probe cleanup failed; remaining data may be partial: {data}", file=sys.stderr)
        elif not result["data_removed"]:
            print(
                f"probe data retained ({result['data_retained_reason']}): {data}", file=sys.stderr
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
