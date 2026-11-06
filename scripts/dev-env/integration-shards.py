"""Run complete integration partitions; verify their union using only the stdlib.

The check subcommand also runs on the GitHub runner's system Python (3.12).
The requirements and run commands import pytest. Reports contain test identities, not DSNs,
credentials, request bodies, or captured test output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
FIELDS = frozenset(
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
        "elapsed_seconds",
    }
)
# The directory may also hold live progress or other sidecars; only the exact
# report names are evidence.
REPORT_NAME_PATTERN = re.compile(r"integration-shard-\d+\.json")

# One Web/Chromium or Node-contract case costs several ordinary DB cases: the
# browser cases drive the production bundles and the Node consumers build the
# client bundle. The value is the measured per-case ratio between the two
# resource classes in complete 2026-11-06 runs (3.4x-6.4x; class means
# 4.6x-5.5x); five is the centre.
# It only distributes ordinary cases between lanes and never changes a case's
# resource class.
WEB_CASE_WEIGHT = 5

# Imbalance is a warning, not a gate. The requirement after the 2026-11
# repartition is 1.25x inside a resource class; a shard above it needs a
# repartition, not a larger timeout.
IMBALANCE_WARNING_RATIO = 1.25
FIFTEEN_MINUTE_WARNING_SECONDS = 900


# Explicit external-tool prerequisites; these cases still belong to the complete
# collection and are never omitted when a tool is absent. Use the same partition
# function as execution, rather than baking a shard number into the workflow.
NODE_CONTRACT_TESTS = (
    "tests/integration/test_pronunciation_http.py::"
    "test_generated_consumers_read_actual_loopback_http",
    "tests/integration/test_binding_http.py::"
    "test_generated_typescript_client_consumes_actual_loopback_http[postgresql]",
    "tests/integration/test_profile_http.py::"
    "test_generated_typescript_client_consumes_actual_loopback_http[postgresql]",
    "tests/integration/test_binding_defaults_http.py::"
    "test_generated_typescript_client_consumes_actual_loopback_http[postgresql]",
    *(
        "tests/integration/test_pronunciation_drafts.py::"
        f"test_auto_id_fixed_read_append_replay_and_real_typescript_consumer[{dialect}]"
        for dialect in ("postgresql", "mysql")
    ),
    *(
        "tests/integration/test_binding_defaults_storage.py::"
        "test_atomic_parent_snapshot_original_replay_restart_and_fixed_projection"
        f"[{dialect}-parameters{case}]"
        for dialect in ("postgresql", "mysql")
        for case in range(2)
    ),
)

# Other browser consumers use the *_browser.py convention. These two modules
# additionally run self-contained Chromium probes against the production bundle.
BROWSER_PROBE_MODULES = frozenset(
    {
        "tests/integration/test_workbench_orphan_probes.py",
        "tests/integration/test_workbench_realtime_synthetic_probes.py",
    }
)


def check_count(count: int) -> None:
    if type(count) is not int or not 1 <= count <= 16:
        raise ValueError("Shard count must be within 1..16")


def requires_browser(nodeid: str) -> bool:
    module = nodeid.split("::", 1)[0]
    return module.endswith("_browser.py") or module in BROWSER_PROBE_MODULES


def _hash_bucket(nodeid: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(nodeid.encode()).digest(), "big") % count


def _lane_quotas(*, ordinary: int, web_cases: Sequence[int], count: int) -> list[int]:
    """Choose how many ordinary cases each lane receives.

    Web cases stay on the Web-capable lanes, but each one is already worth
    WEB_CASE_WEIGHT ordinary cases, so those lanes receive proportionally fewer
    ordinary cases. Quotas are whole nodeids and sum exactly to ordinary;
    largest remainder keeps totals exact and breaks ties by lane index.
    """
    web_lanes = len(web_cases)
    target = (ordinary + WEB_CASE_WEIGHT * sum(web_cases)) / count
    desired = [
        (
            target
            if lane < count - web_lanes
            else max(0.0, target - WEB_CASE_WEIGHT * web_cases[lane - (count - web_lanes)])
        )
        for lane in range(count)
    ]
    total = sum(desired)
    if total <= 0:
        # The Web cases alone exhaust every target; spread ordinary cases evenly.
        desired = [1.0] * count
        total = float(count)
    scaled = [value * ordinary / total for value in desired]
    quotas = [math.floor(value) for value in scaled]
    remainder = ordinary - sum(quotas)
    order = sorted(
        range(count), key=lambda lane: (scaled[lane] - quotas[lane], -lane), reverse=True
    )
    for lane in order[:remainder]:
        quotas[lane] += 1
    return quotas


def partition(nodeids: Sequence[str], index: int, count: int) -> list[str]:
    check_count(count)
    if type(index) is not int or not 0 <= index < count:
        raise ValueError("Shard index is outside the partition")
    web = {
        nodeid for nodeid in nodeids if requires_browser(nodeid) or nodeid in NODE_CONTRACT_TESTS
    }
    if count > 1 and web and len(web) < len(nodeids):
        # Keep four DB-only lanes and the two Web-capable lanes, but do not leave
        # the Web lanes with only the small Web class: they also execute ordinary
        # DB cases. Requirement, execution and evidence verification all derive
        # this assignment from the complete inventory, so every shard computes
        # the same one without shared state.
        web_lane_count = max(1, count // 3)
        ordinary = sorted(nodeid for nodeid in nodeids if nodeid not in web)
        web_lanes: list[list[str]] = [[] for _ in range(web_lane_count)]
        # Round-robin a hash-ordered list: Web cases are few and uneven in cost,
        # so exact counts per Web lane matter more than a hash-modulo split.
        web_order = sorted(
            web, key=lambda nodeid: (hashlib.sha256(nodeid.encode()).digest(), nodeid)
        )
        for position, nodeid in enumerate(web_order):
            web_lanes[position % web_lane_count].append(nodeid)
        quotas = _lane_quotas(
            ordinary=len(ordinary),
            web_cases=[len(lane) for lane in web_lanes],
            count=count,
        )
        ordinary_order = sorted(
            ordinary, key=lambda nodeid: (hashlib.sha256(nodeid.encode()).digest(), nodeid)
        )
        selected: list[str] = []
        cursor = 0
        for lane, quota in enumerate(quotas):
            lane_cases = ordinary_order[cursor : cursor + quota]
            cursor += quota
            if lane >= count - web_lane_count:
                lane_cases = lane_cases + web_lanes[lane - (count - web_lane_count)]
            if lane == index:
                selected = lane_cases
        return sorted(selected)
    return sorted(nodeid for nodeid in nodeids if _hash_bucket(nodeid, count) == index)


def tool_requirements(nodeids: Sequence[str], index: int, count: int) -> dict[str, bool]:
    selected = partition(nodeids, index, count)
    return {
        "node_contracts": bool(set(selected).intersection(NODE_CONTRACT_TESTS)),
        "browser": any(requires_browser(nodeid) for nodeid in selected),
    }


def collect_integration_nodeids() -> list[str]:
    """Resolve fixture/parameter IDs using the same collection as execution."""
    import pytest

    if os.environ.get("PYTEST_ADDOPTS"):
        raise ValueError("PYTEST_ADDOPTS cannot narrow integration tool requirements")

    class Collection:
        def __init__(self) -> None:
            self.nodeids: list[str] = []

        def pytest_collection_finish(self, session: pytest.Session) -> None:
            self.nodeids = sorted(item.nodeid for item in session.items)

    os.chdir(ROOT)
    plugin = Collection()
    # GITHUB_OUTPUT must contain only key=value records, never pytest's output.
    output = StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        result = pytest.main(
            ["-m", "integration", "tests/integration", "--collect-only", "-q"], plugins=[plugin]
        )
    if result != 0 or not plugin.nodeids:
        print(output.getvalue(), file=sys.stderr)
        raise ValueError("Complete integration collection failed while selecting tools")
    return _nodeids(plugin.nodeids)


def collection_digest(nodeids: list[str]) -> str:
    encoded = json.dumps(nodeids, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate report field")
        value[key] = item
    return value


def _nodeids(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 100000
        or any(not isinstance(item, str) or not 1 <= len(item) <= 4096 for item in value)
        or value != sorted(set(value))
        or any(not item.startswith("tests/integration/") or "::" not in item for item in value)
    ):
        raise ValueError("Report requires sorted, unique integration nodeids")
    return value


class ClassMean(NamedTuple):
    cases: float
    seconds: float


@dataclass(frozen=True)
class ShardEvidence:
    index: int
    cases: int
    elapsed_seconds: float
    web: bool


@dataclass(frozen=True)
class Evidence:
    source_head: str
    collected: int
    passed: int
    skipped: tuple[str, ...]
    longest_seconds: float
    shards: tuple[ShardEvidence, ...]


def verify_reports(paths: Sequence[Path], *, source_head: str, count: int) -> Evidence:
    check_count(count)
    if re.fullmatch(r"[0-9a-f]{40}", source_head) is None or len(paths) != count:
        raise ValueError("Expected an exact source head and one report per shard")
    indices: set[int] = set()
    collection: list[str] | None = None
    observed: set[str] = set()
    skipped: list[str] = []
    shards: list[ShardEvidence] = []
    passed = 0
    longest = 0.0
    for path in paths:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Integration report is oversized")
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
        if not isinstance(data, dict) or set(data) != FIELDS:
            raise ValueError("Integration report has an unsupported shape")
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("Integration report has an unsupported version")
        if (
            data["source_head"] != source_head
            or type(data["shard_count"]) is not int
            or data["shard_count"] != count
        ):
            raise ValueError("Shard source head/count does not match")
        index = data["shard_index"]
        if type(index) is not int or index in indices or not 0 <= index < count:
            raise ValueError("Duplicate or invalid shard identity")
        indices.add(index)
        full = _nodeids(data["collection"])
        selected = _nodeids(data["selected"])
        if not full or not selected or data["collection_digest"] != collection_digest(full):
            raise ValueError("Shard collection is empty or its digest does not match")
        if collection is not None and collection != full:
            raise ValueError("Shards collected different complete test sets")
        collection = full
        if selected != partition(full, index, count) or observed.intersection(selected):
            raise ValueError("Shard selected a different partition or duplicated execution")
        observed.update(selected)
        if (
            data["executed"] is not True
            or type(data["exit_code"]) is not int
            or data["exit_code"] != 0
        ):
            raise ValueError("Collect-only, failed, cancelled or incomplete shards cannot pass")
        seconds = data["elapsed_seconds"]
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or not 0 <= seconds <= 3600
        ):
            raise ValueError("Shard duration must be bounded")
        longest = max(longest, seconds)
        web = any(requires_browser(nodeid) or nodeid in NODE_CONTRACT_TESTS for nodeid in selected)
        shards.append(ShardEvidence(index, len(selected), float(seconds), web))
        outcomes = data["outcomes"]
        if not isinstance(outcomes, dict) or set(outcomes) != set(selected):
            raise ValueError("Shard did not record every selected test result")
        shard_passed = 0
        for nodeid, outcome in outcomes.items():
            if outcome == "passed":
                passed += 1
                shard_passed += 1
            elif (
                outcome == "skipped"
                and nodeid.startswith("tests/integration/test_gateway_submit_ledger.py::")
                and nodeid.endswith("[mysql]")
            ):
                # Existing PostgreSQL-only product adapter. Its InnoDB schema
                # parameter is inapplicable, never shared-transaction evidence.
                skipped.append(nodeid)
            else:
                raise ValueError("A shard failed or skipped a required integration case")
        if not shard_passed:
            raise ValueError("An entirely skipped or unexecuted shard cannot pass")
    if collection is None or observed != set(collection) or indices != set(range(count)):
        raise ValueError("Integration partition omitted part of the complete collection")
    return Evidence(
        source_head,
        len(collection),
        passed,
        tuple(sorted(skipped)),
        longest,
        tuple(sorted(shards, key=lambda shard: shard.index)),
    )


def write_progress(
    path: Path,
    *,
    source_head: str,
    index: int,
    count: int,
    selected: int,
    completed: int,
    passed: int,
    failed: int,
    skipped: int,
    last_started: str | None,
    last_completed: str | None,
    started: float,
) -> None:
    """Publish the live shard position for the probe's timeout diagnostics.

    This file only explains a timeout; an unwritable path must never change the
    gate, so every failure here is ignored.
    """
    data = {
        "version": 1,
        "source_head": source_head,
        "shard_index": index,
        "shard_count": count,
        "selected": selected,
        "completed": completed,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "last_started": last_started,
        "last_completed": last_completed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def run_shard(args: argparse.Namespace) -> int:
    import pytest

    check_count(args.count)
    partition([], args.index, args.count)
    if os.environ.get("PYTEST_ADDOPTS"):
        raise ValueError("PYTEST_ADDOPTS cannot narrow the complete integration run")
    if not all(
        os.environ.get(name)
        for name in ("SPEECHNEXUS_TEST_DATABASE_URL", "SPEECHNEXUS_TEST_MYSQL_URL")
    ):
        raise ValueError("Both isolated database URLs are required")
    os.chdir(ROOT)
    source_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    started = time.monotonic()

    class ShardPlugin:
        def __init__(self) -> None:
            self.full: list[str] = []
            self.selected: list[str] = []
            self.outcomes: dict[str, str] = {}
            self.completed = 0
            self.passed = 0
            self.failed = 0
            self.skipped = 0
            self.last_started: str | None = None
            self.last_completed: str | None = None

        def _publish(self) -> None:
            if args.progress is None:
                return
            write_progress(
                args.progress,
                source_head=source_head,
                index=args.index,
                count=args.count,
                selected=len(self.selected),
                completed=self.completed,
                passed=self.passed,
                failed=self.failed,
                skipped=self.skipped,
                last_started=self.last_started,
                last_completed=self.last_completed,
                started=started,
            )

        def pytest_configure(self, config: pytest.Config) -> None:
            if (
                config.getoption("keyword")
                or config.getoption("markexpr") != "integration"
                or config.getoption("deselect", None)
                or config.getoption("ignore", None)
                or config.getoption("ignore_glob", None)
                or config.getoption("lf", None)
            ):
                raise pytest.UsageError("Integration shards cannot use partial selection options")

        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(
            self, config: pytest.Config, items: list[pytest.Item]
        ) -> None:
            self.full = sorted(item.nodeid for item in items)
            if len(self.full) != len(set(self.full)):
                raise pytest.UsageError("Integration nodeids must be unique")
            self.selected = partition(self.full, args.index, args.count)
            allowed = set(self.selected)
            deselected = [item for item in items if item.nodeid not in allowed]
            items[:] = [item for item in items if item.nodeid in allowed]
            config.hook.pytest_deselected(items=deselected)
            self._publish()

        def pytest_collection_finish(self, session: pytest.Session) -> None:
            if sorted(item.nodeid for item in session.items) != self.selected:
                raise pytest.UsageError("A later plugin changed the selected integration partition")

        def pytest_runtest_logstart(
            self, nodeid: str, location: tuple[str, int | None, str]
        ) -> None:
            self.last_started = nodeid
            self._publish()

        def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
            nodeid = report.nodeid
            if (
                report.failed
                or hasattr(report, "wasxfail")
                or (report.skipped and report.when == "teardown")
            ):
                self.outcomes[nodeid] = "failed"
            elif report.skipped:
                self.outcomes[nodeid] = "skipped"
            elif report.when == "call" and self.outcomes.get(nodeid) != "failed":
                self.outcomes[nodeid] = "passed"
            if report.when == "teardown":
                self.completed += 1
                outcome = self.outcomes.get(nodeid)
                if outcome == "failed":
                    self.failed += 1
                elif outcome == "skipped":
                    self.skipped += 1
                else:
                    self.passed += 1
                self.last_completed = nodeid
                self._publish()

        @pytest.hookimpl(trylast=True)
        def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
            elapsed = time.monotonic() - started
            data = {
                "version": 1,
                "source_head": source_head,
                "shard_index": args.index,
                "shard_count": args.count,
                "collection_digest": collection_digest(self.full),
                "collection": self.full,
                "selected": self.selected,
                "outcomes": self.outcomes,
                "exit_code": int(exitstatus),
                "executed": not args.collect_only,
                "elapsed_seconds": round(elapsed, 6),
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
            self._publish()
            print(
                f"Integration shard {args.index}/{args.count}: "
                f"{len(self.selected)} selected cases, {elapsed:.1f} s"
            )

    options = ["-m", "integration", "tests/integration", "-q", "--durations=20"]
    if args.collect_only:
        options.append("--collect-only")
    return int(pytest.main(options, plugins=[ShardPlugin()]))


def resource_class_means(evidence: Evidence) -> dict[bool, ClassMean]:
    """Mean case count and duration inside each resource class."""
    groups: dict[bool, list[ShardEvidence]] = {}
    for shard in evidence.shards:
        groups.setdefault(shard.web, []).append(shard)
    return {
        web: ClassMean(
            sum(shard.cases for shard in group) / len(group),
            sum(shard.elapsed_seconds for shard in group) / len(group),
        )
        for web, group in groups.items()
    }


def shard_distribution(evidence: Evidence, count: int) -> tuple[list[str], list[str]]:
    """Report each shard's case count and duration; imbalance is a warning, not a gate.

    The Web lanes also execute ordinary DB cases, but each Web case still costs more,
    so they hold fewer cases than a DB-only lane. Compare inside the same capability
    class instead of against one global mean; a class with one lane has no imbalance
    to report.
    """
    del count  # The class mean is derived from the reports, not from a uniform split.
    means = resource_class_means(evidence)
    class_sizes = {
        web: sum(1 for candidate in evidence.shards if candidate.web == web) for web in means
    }
    rows: list[str] = []
    warnings: list[str] = []
    for shard in evidence.shards:
        mean = means[shard.web]
        case_ratio = shard.cases / mean.cases if mean.cases else 0.0
        time_ratio = shard.elapsed_seconds / mean.seconds if mean.seconds else 0.0
        label = "Web" if shard.web else "DB"
        rows.append(
            f"  shard {shard.index} ({label}): {shard.cases} cases, "
            f"{shard.elapsed_seconds:.1f} s ({case_ratio:.2f}x the {mean.cases:.1f}-case "
            f"{label} mean; {time_ratio:.2f}x the {mean.seconds:.1f}-s mean)"
        )
        if class_sizes[shard.web] > 1 and shard.cases > mean.cases * IMBALANCE_WARNING_RATIO:
            warnings.append(
                f"warning: integration shard {shard.index} ({label}) has {shard.cases} cases "
                f"({case_ratio:.2f}x the {mean.cases:.1f}-case {label} mean, above the "
                f"{IMBALANCE_WARNING_RATIO:.2f}x warning line); split or repartition it "
                "instead of raising the timeout (docs/validation/ci-validation.md)"
            )
        if shard.elapsed_seconds > FIFTEEN_MINUTE_WARNING_SECONDS:
            warnings.append(
                f"warning: integration shard {shard.index} ({label}) took "
                f"{shard.elapsed_seconds:.1f} s, above the 15-minute warning line; split or "
                "repartition it instead of raising the timeout "
                "(docs/validation/ci-validation.md)"
            )
        elif (
            class_sizes[shard.web] > 1
            and shard.elapsed_seconds > mean.seconds * IMBALANCE_WARNING_RATIO
        ):
            warnings.append(
                f"warning: integration shard {shard.index} ({label}) took "
                f"{shard.elapsed_seconds:.1f} s ({time_ratio:.2f}x the {mean.seconds:.1f}-s "
                f"{label} mean, above the {IMBALANCE_WARNING_RATIO:.2f}x warning line); "
                "inspect the slow shard and split or repartition it"
            )
    return rows, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tools = commands.add_parser("requirements", help="Select tools for this integration partition")
    tools.add_argument("--index", type=int, required=True)
    tools.add_argument("--count", type=int, required=True)
    run = commands.add_parser("run", help="Run one complete integration partition")
    run.add_argument("--index", type=int, required=True)
    run.add_argument("--count", type=int, required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument(
        "--progress",
        type=Path,
        help="live position for timeout diagnostics; never part of the gate",
    )
    run.add_argument("--collect-only", action="store_true")
    check = commands.add_parser("check", help="Require complete, disjoint, successful evidence")
    check.add_argument("--count", type=int, required=True)
    check.add_argument("--head", required=True)
    check.add_argument("--reports-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "requirements":
            check_count(args.count)
            partition([], args.index, args.count)
            for name, needed in tool_requirements(
                collect_integration_nodeids(), args.index, args.count
            ).items():
                print(name + "=" + str(needed).lower())
            return 0
        if args.command == "run":
            return run_shard(args)
        evidence = verify_reports(
            sorted(
                path
                for path in args.reports_dir.glob("integration-shard-*.json")
                if REPORT_NAME_PATTERN.fullmatch(path.name)
            ),
            source_head=args.head,
            count=args.count,
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Integration evidence rejected: {error}", file=sys.stderr)
        return 1
    summary = (
        f"Complete integration partition: {evidence.collected} collected, "
        f"{evidence.passed} passed, {len(evidence.skipped)} existing PG-only parameters skipped; "
        f"source {evidence.source_head}. Longest shard execution {evidence.longest_seconds:.2f}s."
    )
    print(summary)
    rows, warnings = shard_distribution(evidence, args.count)
    print("Per-shard execution:")
    for row in rows:
        print(row)
    for warning in warnings:
        print(warning, file=sys.stderr)
    if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(destination).open("a", encoding="utf-8") as output:
            output.write(summary + "\n\n")
            # The six per-shard job summaries already publish wall clock, pytest
            # execution and selected cases; do not duplicate that table here.
            if warnings:
                output.write("\n".join(warnings) + "\n\n")
            for nodeid in evidence.skipped:
                output.write(f"- Skipped, not qualified: `{nodeid}`\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
