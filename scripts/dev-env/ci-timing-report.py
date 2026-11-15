"""Report measured CI wall clock, critical path and fixed overhead.

Read-only: queries the GitHub Actions API through ``gh`` and prints wall-clock
statistics. It never writes to the repository or the remote, and never decides
whether a run passed. Its purpose is to make CI efficiency changes falsifiable:
shortening a job that is not on the critical path cannot shorten the run.

Usage:
    python3 scripts/dev-env/ci-timing-report.py --limit 40
    python3 scripts/dev-env/ci-timing-report.py --run 43670911505
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any, cast

REPO = "hrygo/SpeechNexus"
FIXED_PREFIXES = (
    "Set up job",
    "Initialize containers",
    "Check out repository",
    "Set up uv",
    "Set up Node.js",
    "Activate pnpm",
    "Install locked",
    "Install Chromium",
    "Build actual Web bundles",
    "Select external tools",
    "Start isolated versioned object storage",
    "Post ",
    "Stop containers",
    "Complete job",
    "Download ",
    "Upload ",
    "Publish ",
    "Preserve ",
    "Record partition wall-clock start",
)


def parse_timestamp(value: str) -> float:
    """Parse a GitHub Actions ISO-8601 timestamp into epoch seconds."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def is_fixed_step(name: str) -> bool:
    """Classify setup/evidence steps that do not execute product tests."""
    return any(name.startswith(prefix) for prefix in FIXED_PREFIXES)


@dataclass(frozen=True)
class JobTiming:
    name: str
    started: float
    completed: float
    work_seconds: float = 0.0
    fixed_seconds: float = 0.0
    end_offset: float = 0.0

    @property
    def duration(self) -> float:
        return self.completed - self.started


@dataclass(frozen=True)
class RunTiming:
    run_id: int
    jobs: tuple[JobTiming, ...]

    @property
    def started(self) -> float:
        return min(job.started for job in self.jobs)

    @property
    def wall_clock(self) -> float:
        return max(job.completed for job in self.jobs) - self.started

    @property
    def runner_seconds(self) -> float:
        return sum(job.duration for job in self.jobs)

    @property
    def critical_path(self) -> tuple[str, float]:
        """Return the job that ends last and its end offset from run start."""
        last = max(self.jobs, key=lambda job: job.completed)
        return last.name, last.completed - self.started


def parse_job(raw: dict[str, Any]) -> JobTiming | None:
    """Convert one API job into timings; skip jobs without both timestamps."""
    started_at = raw.get("started_at")
    completed_at = raw.get("completed_at")
    if not started_at or not completed_at:
        return None
    work_seconds = 0.0
    fixed_seconds = 0.0
    for step in raw.get("steps") or []:
        step_start = step.get("started_at")
        step_end = step.get("completed_at")
        if not step_start or not step_end:
            continue
        seconds = parse_timestamp(step_end) - parse_timestamp(step_start)
        name = step.get("name", "")
        if is_fixed_step(name):
            fixed_seconds += seconds
        else:
            work_seconds += seconds
    return JobTiming(
        name=raw["name"],
        started=parse_timestamp(started_at),
        completed=parse_timestamp(completed_at),
        work_seconds=work_seconds,
        fixed_seconds=fixed_seconds,
    )


def parse_run(run_id: int, raw_jobs: list[dict[str, Any]]) -> RunTiming | None:
    """Convert API jobs into one run; return None when no job is measurable."""
    parsed = [job for raw in raw_jobs if (job := parse_job(raw)) is not None]
    if not parsed:
        return None
    started = min(job.started for job in parsed)
    jobs = tuple(
        JobTiming(
            name=job.name,
            started=job.started,
            completed=job.completed,
            work_seconds=job.work_seconds,
            fixed_seconds=job.fixed_seconds,
            end_offset=job.completed - started,
        )
        for job in parsed
    )
    return RunTiming(run_id=run_id, jobs=jobs)


MIN_FULL_ROUTE_SHARDS = 6


def shard_count(run: RunTiming) -> int:
    """Number of integration partitions a run actually expanded.

    The plan job's display name also starts with "Integration " but expands to
    one runner, not a matrix partition; counting it would overstate the topology.
    """
    return sum(
        job.name.startswith("Integration ") and not job.name.startswith("Integration plan")
        for job in run.jobs
    )


def is_full_route(run: RunTiming) -> bool:
    """Accept both the six- and eight-partition topologies.

    A partition-count change must not break before/after comparisons, so the
    report keeps both topologies and states the shard-count distribution.
    """
    return shard_count(run) >= MIN_FULL_ROUTE_SHARDS


def fetch_runs(limit: int) -> list[int]:
    """Return successful run ids, newest first."""
    payload = subprocess.check_output(
        [
            "gh",
            "run",
            "list",
            "--workflow=ci.yml",
            "--limit",
            str(limit),
            "--json",
            "databaseId,conclusion",
        ],
        text=True,
    )
    return [
        item["databaseId"] for item in json.loads(payload) if item.get("conclusion") == "success"
    ]


def fetch_jobs(run_id: int, repo: str = REPO) -> list[dict[str, Any]]:
    payload = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs/{run_id}/jobs",
            "--jq",
            "[.jobs[] | {name, started_at, completed_at,"
            " steps: [.steps[] | {name, started_at, completed_at}]}]",
        ],
        text=True,
    )
    return cast(list[dict[str, Any]], json.loads(payload))


def format_report(runs: list[RunTiming]) -> str:
    """Render the measured baseline, critical path and per-job fixed overhead."""
    if not runs:
        return "No full-route successful runs in the requested window."
    walls = [run.wall_clock for run in runs]
    runner = median(run.runner_seconds for run in runs)
    counts: dict[int, int] = {}
    for run in runs:
        count = shard_count(run)
        counts[count] = counts.get(count, 0) + 1
    lines = [
        f"full-route success samples: {len(runs)}",
        f"shard counts: {dict(sorted(counts.items()))}",
        f"wall clock: min={min(walls):.0f}s median={median(walls):.0f}s max={max(walls):.0f}s",
        f"runner seconds (median): {runner:.0f}s",
        f"parallelism factor: {runner / median(walls):.2f}x",
        "",
        f"{'job':54} {'duration':>10} {'end':>10} {'fixed':>9} {'net work':>10}",
    ]
    by_name: dict[str, list[JobTiming]] = {}
    for run in runs:
        for job in run.jobs:
            by_name.setdefault(job.name, []).append(job)
    ordered = sorted(
        by_name.items(),
        key=lambda item: -median(job.end_offset for job in item[1]),
    )
    for name, jobs in ordered:
        lines.append(
            f"{name[:54]:54} {median(j.duration for j in jobs):9.0f}s "
            f"{median(j.end_offset for j in jobs):9.0f}s "
            f"{median(j.fixed_seconds for j in jobs):8.0f}s "
            f"{median(j.work_seconds for j in jobs):9.0f}s"
        )
    lines.append("")
    lines.append("Critical path ends at:")
    for run in runs:
        name, end = run.critical_path
        lines.append(f"  run {run.run_id}: {name} ends at {end:.0f}s")
    lines.append("")
    lines.append(
        "Only jobs on the critical path can shorten wall clock; "
        "shorter non-critical jobs save runner seconds, not time to merge."
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40, help="runs to inspect")
    parser.add_argument("--run", type=int, action="append", help="specific run id")
    parser.add_argument("--repo", default=REPO, help="owner/repo")
    args = parser.parse_args()
    ids = args.run or fetch_runs(args.limit)
    runs = [
        run
        for run_id in ids
        if (run := parse_run(run_id, fetch_jobs(run_id, args.repo))) and is_full_route(run)
    ]
    print(format_report(runs))


if __name__ == "__main__":
    main()
