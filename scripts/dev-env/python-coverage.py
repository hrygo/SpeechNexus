"""Parallelize the complete Python gate inside one runner, then enforce global coverage.

Each worker is hosted by the existing disposable-PG probe. Neither a passing
fragment nor a collect-only report can replace complete execution and the 80%
combined gate. No dependency, runner job, or shared database is added.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import runpy
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "scripts/dev-env/workbench-pg-probe.py"
FIELDS = {
    "version",
    "source_head",
    "config_digest",
    "index",
    "count",
    "collection",
    "selected",
    "outcomes",
    "exit_code",
    "executed",
    "coverage_file",
    "coverage_digest",
    "elapsed_seconds",
}


def check_partition(index: int, count: int) -> None:
    if type(count) is not int or count not in (1, 2):
        raise ValueError("Python coverage supports one or two isolated workers")
    if type(index) is not int or not 0 <= index < count:
        raise ValueError("Invalid Python coverage worker identity")


def partition(nodeids: Sequence[str], index: int, count: int) -> list[str]:
    """Balance collection counts without splitting a file's fixtures across workers."""
    check_partition(index, count)
    files: dict[str, list[str]] = defaultdict(list)
    for nodeid in nodeids:
        files[nodeid.split("::", 1)[0]].append(nodeid)
    lanes: list[list[str]] = [[] for _ in range(count)]
    for path in sorted(files, key=lambda path: (-len(files[path]), path)):
        lane = min(range(count), key=lambda item: (len(lanes[item]), item))
        lanes[lane].extend(files[path])
    return sorted(lanes[index])


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def canonical_options() -> list[str]:
    # The probe remains the single authority for the full gate's roots and options.
    factory = runpy.run_path(str(DRIVER))["build_pytest_command"]
    command: list[str] = factory(full_coverage=True)
    return command[3:]


def run_worker(args: argparse.Namespace) -> int:
    import pytest

    check_partition(args.index, args.count)
    if os.environ.get("PYTEST_ADDOPTS"):
        raise ValueError("PYTEST_ADDOPTS cannot narrow the complete Python gate")
    options: list[str] = args.pytest_options
    if options[:1] == ["--"]:
        options = options[1:]
    comparable = [arg for arg in options if not arg.startswith("--cov-report=json:")]
    if comparable != [*canonical_options(), "--cov-fail-under=0"]:
        raise ValueError("Coverage workers require the canonical full gate without selection")
    data_file = Path(os.environ["COVERAGE_FILE"]).resolve()
    data_relative = data_file.relative_to(ROOT / ".runtime/team-c")
    if len(data_relative.parts) != 2 or data_relative.name != ".coverage":
        raise ValueError("Coverage data must belong to the disposable probe")
    head, config_digest = source_head(), digest(ROOT / "pyproject.toml")
    started = time.monotonic()

    class Worker:
        def __init__(self) -> None:
            self.full: list[str] = []
            self.selected: list[str] = []
            self.outcomes: dict[str, str] = {}

        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(
            self, config: pytest.Config, items: list[pytest.Item]
        ) -> None:
            self.full = sorted(item.nodeid for item in items)
            if len(self.full) != len(set(self.full)):
                raise pytest.UsageError("Python coverage nodeids must be unique")
            self.selected = partition(self.full, args.index, args.count)
            allowed = set(self.selected)
            removed = [item for item in items if item.nodeid not in allowed]
            items[:] = [item for item in items if item.nodeid in allowed]
            config.hook.pytest_deselected(items=removed)

        def pytest_collection_finish(self, session: pytest.Session) -> None:
            if sorted(item.nodeid for item in session.items) != self.selected:
                raise pytest.UsageError("A later plugin changed the Python coverage partition")

        def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
            if report.failed or report.skipped or hasattr(report, "wasxfail"):
                self.outcomes[report.nodeid] = "failed"
            elif report.when == "call" and self.outcomes.get(report.nodeid) != "failed":
                self.outcomes[report.nodeid] = "passed"

    worker = Worker()
    os.chdir(ROOT)
    code = int(pytest.main(options, plugins=[worker]))
    if not data_file.is_file():
        raise ValueError("Python worker produced no coverage data")
    result = {
        "version": 1,
        "source_head": head,
        "config_digest": config_digest,
        "index": args.index,
        "count": args.count,
        "collection": worker.full,
        "selected": worker.selected,
        "outcomes": worker.outcomes,
        "exit_code": code,
        "executed": True,
        "coverage_file": str(data_file.relative_to(ROOT)),
        "coverage_digest": digest(data_file),
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }
    args.report.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    return code


def unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate Python evidence field")
        result[key] = value
    return result


def nodeids(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 100000
        or any(
            not isinstance(item, str)
            or not item.startswith("tests/")
            or "::" not in item
            or len(item) > 16384
            for item in value
        )
        or value != sorted(set(value))
    ):
        raise ValueError("Python evidence needs a sorted, complete, unique test collection")
    return cast(list[str], value)


def verify_reports(paths: Sequence[Path], *, head: str, count: int) -> tuple[list[Path], int]:
    from coverage import CoverageData

    check_partition(0, count)
    if len(paths) != count:
        raise ValueError("Every Python coverage worker is required")
    full: list[str] | None = None
    observed: set[str] = set()
    indices: set[int] = set()
    files: list[Path] = []
    config_digest = digest(ROOT / "pyproject.toml")
    for path in paths:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Python evidence is oversized")
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
        if not isinstance(data, dict) or set(data) != FIELDS:
            raise ValueError("Unsupported Python coverage evidence")
        if (
            type(data["version"]) is not int
            or data["version"] != 1
            or data["source_head"] != head
            or data["config_digest"] != config_digest
            or type(data["count"]) is not int
            or data["count"] != count
        ):
            raise ValueError("Python evidence source/configuration/count differs")
        index = data["index"]
        check_partition(index, count)
        if index in indices:
            raise ValueError("Duplicate Python worker identity")
        indices.add(index)
        collection, selected = nodeids(data["collection"]), nodeids(data["selected"])
        if full is not None and full != collection:
            raise ValueError("Python workers collected different full suites")
        full = collection
        if selected != partition(collection, index, count) or observed.intersection(selected):
            raise ValueError("Python worker omitted or duplicated its assigned partition")
        observed.update(selected)
        if (
            data["executed"] is not True
            or type(data["exit_code"]) is not int
            or data["exit_code"] != 0
            or not isinstance(data["outcomes"], dict)
            or set(data["outcomes"]) != set(selected)
            or any(result != "passed" for result in data["outcomes"].values())
        ):
            raise ValueError("Python evidence contains failed, skipped or unexecuted tests")
        seconds = data["elapsed_seconds"]
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 <= seconds <= 1200
        ):
            raise ValueError("Python worker duration must be finite and bounded")
        coverage_file = (ROOT / data["coverage_file"]).resolve()
        relative = coverage_file.relative_to(ROOT / ".runtime/team-c")
        if (
            len(relative.parts) != 2
            or relative.name != ".coverage"
            or coverage_file in files
            or coverage_file.stat().st_size > 32 * 1024 * 1024
            or digest(coverage_file) != data["coverage_digest"]
        ):
            raise ValueError("Python coverage data is missing, repeated or changed")
        coverage_data = CoverageData(basename=str(coverage_file))
        coverage_data.read()
        if not coverage_data.has_arcs() or not coverage_data.measured_files():
            raise ValueError("Python worker has no branch coverage data")
        files.append(coverage_file)
    if full is None or observed != set(full) or indices != set(range(count)):
        raise ValueError("Python execution did not cover the complete collection exactly once")
    return files, len(full)


def combine_coverage(files: Sequence[Path], directory: Path) -> tuple[float, int]:
    from coverage import Coverage

    combined = directory / "coverage.data"
    cov = Coverage(data_file=str(combined), config_file=str(ROOT / "pyproject.toml"))
    cov.combine(data_paths=[str(path) for path in files], strict=True, keep=True)
    cov.save()
    percent = cov.json_report(outfile=str(directory / "coverage.json"))
    gate = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--rcfile",
            str(ROOT / "pyproject.toml"),
            "--data-file",
            str(combined),
        ],
        cwd=ROOT,
        check=False,
    )
    return percent, gate.returncode


def prepare_shared_bucket() -> None:
    """Provision the fixtures' shared bucket before either process can create it."""
    import urllib3
    from minio import Minio

    runpy.run_path(str(DRIVER))["require_object_storage"](os.environ)
    endpoint = os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"]
    with urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=10), retries=False) as http:
        client = Minio(
            endpoint.removeprefix("http://").removeprefix("https://"),
            access_key=os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
            secret_key=os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
            secure=endpoint.startswith("https://"),
            http_client=http,
        )
        bucket = "speechnexus-workbench-saved"
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)


def run_parallel(args: argparse.Namespace) -> int:
    check_partition(0, args.workers)
    if os.environ.get("PYTEST_ADDOPTS"):
        raise ValueError("PYTEST_ADDOPTS cannot narrow the complete Python gate")
    args.output = args.output.resolve()
    args.pg_bin = args.pg_bin.resolve()
    head = source_head()
    directory = args.output.with_suffix("").with_name(args.output.stem + "-parts")
    directory.mkdir(parents=True)  # Never reuse unknown results from an earlier attempt.
    summary: dict[str, Any] = {
        "kind": "parallel-full-python-coverage",
        "source_head": head,
        "workers": args.workers,
        "tests_passed": False,
        "stopped": False,
        "coverage_passed": False,
    }
    try:
        prepare_shared_bucket()

        def run(index: int) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--pg-bin",
                    str(args.pg_bin),
                    "--full-coverage",
                    "--python-shard",
                    str(index),
                    "--python-shard-count",
                    str(args.workers),
                    "--python-shard-report",
                    str(directory / f"worker-{index}.json"),
                    "--output",
                    str(directory / f"postgres-{index}.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

        # Every probe finishes its own finally/stop/cleanup, including after a sibling fails.
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(run, range(args.workers)))
        for index, result in enumerate(results):
            print(f"Python coverage worker {index}: exit={result.returncode}", flush=True)
            log = result.stdout + result.stderr
            (directory / f"worker-{index}.log").write_text(log, encoding="utf-8")
            print(log, end="", flush=True)
        if any(result.returncode != 0 for result in results):
            raise ValueError("A Python probe failed; combined coverage cannot qualify it")
        for index in range(args.workers):
            lifecycle = json.loads((directory / f"postgres-{index}.json").read_text())
            if lifecycle.get("tests_passed") is not True or lifecycle.get("stopped") is not True:
                raise ValueError("A Python worker lacks execution or shutdown evidence")
        files, count = verify_reports(
            [directory / f"worker-{index}.json" for index in range(args.workers)],
            head=head,
            count=args.workers,
        )
        if source_head() != head:
            raise ValueError("Source changed while the Python gate was executing")
        summary.update(tests_passed=True, stopped=True, collected=count, passed=count)
        for index, path in enumerate(files):
            shutil.copyfile(path, directory / f"coverage-{index}.data")
        percent, code = combine_coverage(files, directory)
        summary.update(coverage_percent=percent, coverage_passed=code == 0)
        return code
    finally:
        args.output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--pg-bin", type=Path, required=True)
    run.add_argument("--workers", type=int, choices=(1, 2), default=2)
    run.add_argument("--output", type=Path, required=True)
    worker = commands.add_parser("worker")
    worker.add_argument("--index", type=int, required=True)
    worker.add_argument("--count", type=int, required=True)
    worker.add_argument("--report", type=Path, required=True)
    worker.add_argument("pytest_options", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return run_worker(args) if args.command == "worker" else run_parallel(args)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Python coverage rejected: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
