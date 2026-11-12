"""Measure the complete Python gate in one job, then merge every lane's coverage.

Each worker is hosted by the existing disposable-PG probe. Neither a passing
fragment nor a collect-only report can replace complete execution. This job only
measures and exports its data; the integration summary job merges the six
partitions' data with it and enforces the unchanged global gate, so a partition
that was skipped or exported nothing fails the gate instead of shrinking it.
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
COVERAGE_DATA_LIMIT = 32 * 1024 * 1024
SUMMARY_FIELDS = frozenset(
    {
        "kind",
        "source_head",
        "config_digest",
        "workers",
        "tests_passed",
        "stopped",
        "collected",
        "passed",
        "coverage_parts",
        "coverage_percent",
    }
)
SHARD_REPORT_FIELDS = frozenset(
    {
        "version",
        "source_head",
        "shard_index",
        "shard_count",
        "collection_digest",
        "collection",
        "selected",
        "outcomes",
        "exit_code",
        "executed",
        "coverage_file",
        "coverage_digest",
        "elapsed_seconds",
    }
)
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


def combined_config() -> Path:
    return ROOT / "pyproject.toml"


def require_branch_data(path: Path) -> str:
    """One data file is only evidence if it carries branch arcs for real files."""
    from coverage import CoverageData

    if not path.is_file():
        raise ValueError(f"Coverage data file is missing: {path.name}")
    if not 0 < path.stat().st_size <= COVERAGE_DATA_LIMIT:
        raise ValueError(f"Coverage data file is empty or oversized: {path.name}")
    from coverage.exceptions import DataError

    try:
        data = CoverageData(basename=str(path))
        data.read()
        valid = data.has_arcs() and data.measured_files()
    except DataError:
        raise ValueError(f"Coverage data file is unreadable: {path.name}") from None
    if not valid:
        raise ValueError(f"Coverage data file has no branches or files: {path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def combine_data(files: Sequence[Path], directory: Path, *, config_file: Path) -> float:
    """Combine explicit data files; the unchanged gate is reported by the caller."""
    from coverage import Coverage

    combined = directory / "coverage.data"
    cov = Coverage(
        data_file=str(combined),
        config_file=str(config_file),
    )
    cov.combine(data_paths=[str(path) for path in files], strict=True, keep=True)
    cov.save()
    return cov.json_report(outfile=str(directory / "coverage.json"))


def gate_report(directory: Path, *, rcfile: Path) -> int:
    """Report through the project configuration, so fail_under stays authoritative."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "report",
            "--rcfile",
            str(rcfile),
            "--data-file",
            str(directory / "coverage.data"),
        ],
        cwd=ROOT,
        check=False,
    ).returncode


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
    config_digest = digest(combined_config())
    summary: dict[str, Any] = {
        "kind": "parallel-full-python-coverage",
        "source_head": head,
        "config_digest": config_digest,
        "workers": args.workers,
        "tests_passed": False,
        "stopped": False,
        "collected": None,
        "passed": None,
        "coverage_parts": [],
        "coverage_percent": None,
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
        if digest(combined_config()) != config_digest:
            raise ValueError("Coverage configuration changed while the Python gate was executing")
        summary.update(tests_passed=True, stopped=True, collected=count, passed=count)
        parts = []
        for index, path in enumerate(files):
            copy = directory / f"coverage-{index}.data"
            shutil.copyfile(path, copy)
            parts.append({"index": index, "file": copy.name, "digest": require_branch_data(copy)})
        # Measurement only: the merged judgment runs where all eight datasets exist.
        # The combined probe stays beside (not inside) the exported parts directory,
        # so the uploaded artifact holds only data files and execution logs.
        combined = directory.with_name(directory.name + "-combined")
        combined.mkdir()
        percent = combine_data(
            [directory / part["file"] for part in parts],
            combined,
            config_file=combined_config(),
        )
        summary.update(coverage_parts=parts, coverage_percent=percent)
        return 0
    finally:
        args.output.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n")


def verify_job_evidence(summary_path: Path, parts_dir: Path, *, head: str) -> list[Path]:
    """The measured dataset must come from this source, configuration and full run."""
    if summary_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Python coverage summary is oversized")
    data = json.loads(summary_path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    if not isinstance(data, dict) or set(data) != SUMMARY_FIELDS:
        raise ValueError("Python coverage summary has an unsupported shape")
    if (
        data["kind"] != "parallel-full-python-coverage"
        or data["source_head"] != head
        or data["config_digest"] != digest(combined_config())
        or type(data["workers"]) is not int
        or data["workers"] not in (1, 2)
    ):
        raise ValueError("Python coverage summary came from another source or configuration")
    if (
        data["tests_passed"] is not True
        or data["stopped"] is not True
        or type(data["collected"]) is not int
        or type(data["passed"]) is not int
        or data["collected"] < 1
        or data["passed"] != data["collected"]
    ):
        raise ValueError("Python coverage summary lacks complete, stopped execution evidence")
    parts = data["coverage_parts"]
    if not isinstance(parts, list) or len(parts) != data["workers"]:
        raise ValueError("Python coverage summary does not list every worker dataset")
    files: list[Path] = []
    indices: set[int] = set()
    for part in parts:
        if not isinstance(part, dict) or set(part) != {"index", "file", "digest"}:
            raise ValueError("Python coverage summary has an unsupported worker entry")
        index = part["index"]
        if type(index) is not int or index in indices or not 0 <= index < data["workers"]:
            raise ValueError("Python coverage summary has a duplicate or invalid worker")
        indices.add(index)
        if part["file"] != f"coverage-{index}.data":
            raise ValueError("Python worker coverage data has an unexpected name")
        path = parts_dir / part["file"]
        try:
            part_digest = require_branch_data(path)
        except ValueError:
            raise ValueError(f"Python worker {index} coverage data is missing or invalid") from None
        if part_digest != part["digest"]:
            raise ValueError(f"Python worker {index} coverage data is changed")
        files.append(path)
    if indices != set(range(data["workers"])):
        raise ValueError("Python coverage summary omitted a worker dataset")
    return sorted(files, key=lambda path: path.name)


def verify_shard_evidence(directory: Path, count: int, *, head: str) -> list[Path]:
    """Every partition must contribute its own dataset; none may be silently absent."""
    if type(count) is not int or not 1 <= count <= 16:
        raise ValueError("Invalid integration partition count")
    expected = {f"coverage-{index}.data" for index in range(count)}
    if {path.name for path in directory.glob("coverage-*.data")} - expected:
        raise ValueError("Unexpected integration coverage data is present")
    files: list[Path] = []
    for index in range(count):
        report = directory / f"integration-shard-{index}.json"
        if not report.is_file():
            raise ValueError(f"Missing integration coverage evidence for shard {index}")
        data = json.loads(report.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
        if not isinstance(data, dict) or set(data) != SHARD_REPORT_FIELDS:
            raise ValueError(f"Integration shard {index} report has an unsupported shape")
        if (
            type(data["version"]) is not int
            or data["version"] != 2
            or data["source_head"] != head
            or type(data["shard_index"]) is not int
            or data["shard_index"] != index
            or type(data["shard_count"]) is not int
            or data["shard_count"] != count
        ):
            raise ValueError(f"Integration shard {index} report has a different identity")
        if (
            data["executed"] is not True
            or type(data["exit_code"]) is not int
            or data["exit_code"] != 0
        ):
            raise ValueError(f"Integration shard {index} did not execute successfully")
        name = f"coverage-{index}.data"
        if data["coverage_file"] != name:
            raise ValueError(f"Integration shard {index} reported unexpected coverage data")
        path = directory / name
        try:
            shard_digest = require_branch_data(path)
        except ValueError:
            raise ValueError(
                f"Integration shard {index} coverage data is missing or invalid"
            ) from None
        if shard_digest != data["coverage_digest"]:
            raise ValueError(f"Integration shard {index} coverage data is changed")
        files.append(path)
    return files


def run_combine(args: argparse.Namespace) -> int:
    """Merge the measured dataset with all integration partitions, then judge once."""
    head = source_head()
    rcfile = (args.probe_rcfile or combined_config()).resolve()
    output = args.output.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("Merged coverage output must be new")
    output.mkdir(parents=True)
    summary: dict[str, Any] = {
        "kind": "merged-python-coverage-gate",
        "source_head": head,
        "config_digest": digest(combined_config()),
        "job_parts": 0,
        "shards": 0,
        "coverage_percent": None,
        "coverage_passed": False,
    }
    try:
        measured = verify_job_evidence(args.job_summary, args.job_parts, head=head)
        shards = verify_shard_evidence(args.integration_dir, args.count, head=head)
        percent = combine_data([*measured, *shards], output, config_file=rcfile)
        code = gate_report(output, rcfile=rcfile)
        summary.update(
            job_parts=len(measured),
            shards=len(shards),
            coverage_percent=percent,
            coverage_passed=code == 0,
        )
        print(
            f"Merged {len(measured)} measured dataset(s) and {len(shards)} integration "
            f"partition(s): {percent:.4f}% covered.",
            flush=True,
        )
        return code
    finally:
        (output / "merged.json").write_text(
            json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )


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
    combine = commands.add_parser(
        "combine", help="merge the measured and integration datasets and enforce the gate"
    )
    combine.add_argument("--job-summary", type=Path, required=True)
    combine.add_argument("--job-parts", type=Path, required=True)
    combine.add_argument("--integration-dir", type=Path, required=True)
    combine.add_argument("--count", type=int, required=True)
    combine.add_argument("--output", type=Path, required=True)
    combine.add_argument(
        "--probe-rcfile",
        type=Path,
        help="alternate coverage configuration for local reproduction only; CI never passes it",
    )
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run_parallel(args)
        if args.command == "worker":
            return run_worker(args)
        return run_combine(args)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Python coverage rejected: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
