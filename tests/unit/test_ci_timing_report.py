"""The CI timing report must measure, not guess: parsing and critical path."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev-env" / "ci-timing-report.py"
_M = runpy.run_path(str(_SCRIPT))
parse_timestamp = _M["parse_timestamp"]
is_fixed_step = _M["is_fixed_step"]
parse_job = _M["parse_job"]
parse_run = _M["parse_run"]
is_full_route = _M["is_full_route"]
shard_count = _M["shard_count"]
format_report = _M["format_report"]


def _stamp(second: int) -> str:
    """Deterministic ISO-8601 timestamp at ``second`` after a fixed epoch."""
    minutes, rest = divmod(second, 60)
    return f"2026-01-01T00:{minutes:02d}:{rest:02d}Z"


def _job(
    name: str, start: int, end: int, steps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "started_at": _stamp(start),
        "completed_at": _stamp(end),
        "steps": steps or [],
    }


def _step(name: str, start: int, end: int) -> dict[str, Any]:
    return {"name": name, "started_at": _stamp(start), "completed_at": _stamp(end)}


def test_parse_timestamp_is_utc_and_relative() -> None:
    assert parse_timestamp(_stamp(90)) - parse_timestamp(_stamp(30)) == 60


@pytest.mark.parametrize(
    ("name", "fixed"),
    [
        ("Set up uv", True),
        ("Initialize containers", True),
        ("Install locked dependencies", True),
        ("Check out repository", True),
        ("Run a complete integration partition with PG, InnoDB and MinIO", False),
        ("Measure Python unit, contract and CI tests", False),
    ],
)
def test_fixed_step_classification(name: str, fixed: bool) -> None:
    assert is_fixed_step(name) is fixed


def test_parse_run_computes_wall_clock_and_critical_path() -> None:
    raw = [
        _job("Route: scope", 0, 20),
        _job("Integration 0/6: PG/MySQL/MinIO", 20, 440),
        _job("Integration 4/6: Web + PG/MySQL/MinIO", 20, 586),
        _job("Gate: required checks for this change", 586, 589),
    ]
    run = parse_run(42, raw)
    assert run is not None
    assert run.wall_clock == 589
    assert run.runner_seconds == 20 + 420 + 566 + 3
    assert run.critical_path == ("Gate: required checks for this change", 589)
    assert [job.end_offset for job in run.jobs] == [20, 440, 586, 589]


def test_net_test_time_and_fixed_overhead_are_separated() -> None:
    raw = _job(
        "Integration 4/6: Web + PG/MySQL/MinIO",
        0,
        100,
        [
            _step("Initialize containers", 0, 10),
            _step("Install locked dependencies", 10, 20),
            _step("Run a complete integration partition with PG", 20, 90),
            _step("Publish integration partition timing", 90, 100),
        ],
    )
    job = parse_job(raw)
    assert job is not None
    assert job.work_seconds == 70
    assert job.fixed_seconds == 30
    assert job.duration == 100


def test_non_integration_jobs_report_their_own_work_time() -> None:
    raw = _job(
        "Python: tests, coverage and build",
        0,
        100,
        [
            _step("Set up uv", 0, 5),
            _step("Measure Python unit, contract and CI tests", 5, 95),
            _step("Post Set up uv", 95, 100),
        ],
    )
    job = parse_job(raw)
    assert job is not None
    assert job.work_seconds == 90
    assert job.fixed_seconds == 10


def test_jobs_without_timestamps_are_skipped() -> None:
    raw = [
        _job("skipped", 0, 10),
        {"name": "pending", "started_at": None, "completed_at": None, "steps": []},
    ]
    assert parse_job(raw[1]) is None
    run = parse_run(1, raw)
    assert run is not None and len(run.jobs) == 1
    assert parse_run(2, [raw[1]]) is None


def test_full_route_accepts_both_six_and_eight_partitions() -> None:
    raw = [_job(f"Integration {i}/8: PG/MySQL/MinIO", 0, 10 + i) for i in range(5)]
    assert parse_run(1, raw) is not None
    assert is_full_route(parse_run(1, raw)) is False
    assert shard_count(parse_run(1, raw)) == 5
    raw.append(_job("Integration 5/8: PG/MySQL/MinIO", 0, 15))
    assert is_full_route(parse_run(2, raw)) is True
    assert shard_count(parse_run(2, raw)) == 6
    raw.extend(_job(f"Integration {i}/8: Web + PG/MySQL/MinIO", 0, 16 + i) for i in (6, 7))
    assert is_full_route(parse_run(3, raw)) is True
    assert shard_count(parse_run(3, raw)) == 8
    # The plan job shares the display-name prefix but is one runner, not a partition.
    raw.append(_job("Integration plan: one collection selects every partition's tools", 0, 12))
    assert shard_count(parse_run(4, raw)) == 8


def test_format_report_states_the_critical_path_rule() -> None:
    raw = [
        _job("Integration 0/6: PG/MySQL/MinIO", 0, 400),
        _job("Integration 4/6: Web + PG/MySQL/MinIO", 0, 586),
        _job("Python: tests, coverage and build", 0, 480),
    ] + [_job(f"Integration {i}/6: PG/MySQL/MinIO", 0, 420) for i in (1, 2, 3, 5)]
    run = parse_run(7, raw)
    assert run is not None
    report = format_report([run])
    assert "full-route success samples: 1" in report
    assert "shard counts: {6: 1}" in report
    assert "wall clock: min=586s median=586s max=586s" in report
    assert "Integration 4/6: Web + PG/MySQL/MinIO ends at 586s" in report
    assert "Only jobs on the critical path can shorten wall clock" in report
    assert format_report([]).startswith("No full-route")
