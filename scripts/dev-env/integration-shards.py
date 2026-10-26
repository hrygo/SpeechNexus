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
from typing import Any

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


def partition(nodeids: Sequence[str], index: int, count: int) -> list[str]:
    check_count(count)
    if type(index) is not int or not 0 <= index < count:
        raise ValueError("Shard index is outside the partition")
    return sorted(
        nodeid
        for nodeid in nodeids
        if int.from_bytes(hashlib.sha256(nodeid.encode()).digest(), "big") % count == index
    )


def tool_requirements(nodeids: Sequence[str], index: int, count: int) -> dict[str, bool]:
    selected = partition(nodeids, index, count)
    return {
        "node_contracts": bool(set(selected).intersection(NODE_CONTRACT_TESTS)),
        "browser": any(
            nodeid.split("::", 1)[0].endswith("_browser.py")
            or nodeid.split("::", 1)[0] in BROWSER_PROBE_MODULES
            for nodeid in selected
        ),
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


@dataclass(frozen=True)
class Evidence:
    source_head: str
    collected: int
    passed: int
    skipped: tuple[str, ...]
    longest_seconds: float


def verify_reports(paths: Sequence[Path], *, source_head: str, count: int) -> Evidence:
    check_count(count)
    if re.fullmatch(r"[0-9a-f]{40}", source_head) is None or len(paths) != count:
        raise ValueError("Expected an exact source head and one report per shard")
    indices: set[int] = set()
    collection: list[str] | None = None
    observed: set[str] = set()
    skipped: list[str] = []
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
    return Evidence(source_head, len(collection), passed, tuple(sorted(skipped)), longest)


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

        def pytest_collection_finish(self, session: pytest.Session) -> None:
            if sorted(item.nodeid for item in session.items) != self.selected:
                raise pytest.UsageError("A later plugin changed the selected integration partition")

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

        @pytest.hookimpl(trylast=True)
        def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
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
                "elapsed_seconds": round(time.monotonic() - started, 6),
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")

    options = ["-m", "integration", "tests/integration", "-q"]
    if args.collect_only:
        options.append("--collect-only")
    return int(pytest.main(options, plugins=[ShardPlugin()]))


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
            sorted(args.reports_dir.glob("integration-shard-*.json")),
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
    if destination := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(destination).open("a", encoding="utf-8") as output:
            output.write(summary + "\n\n")
            for nodeid in evidence.skipped:
                output.write(f"- Skipped, not qualified: `{nodeid}`\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
