"""One-command entry to the known integration-flake conditions.

This harness is never enabled by default: it does nothing unless it is invoked
with an explicit `--condition`, and no conftest hook imports it. It starts the
requested condition against the isolated test databases, announces the measured
proof that the condition is in effect, runs pytest under that condition, then
restores the server state and stops the background load.

Usage (both DSNs are read from the environment, as for any integration run)::

    SPEECHNEXUS_TEST_DATABASE_URL=postgresql://... \\
    SPEECHNEXUS_TEST_MYSQL_URL=mysql://... \\
      uv run python -m tests.integration.flake_harness --condition latency-proxy

Conditions:

* `latency-proxy` -- loopback TCP proxy adding `--chunk-delay-ms` (default 10)
  per forwarded chunk, applied to *both* DSNs. Slows every round trip.
* `commit-delay` -- PostgreSQL `commit_delay`+`commit_siblings` with busy
  committers, and InnoDB `binlog_group_commit_sync_delay`. Slows the commit
  itself and no statement round trip.
* `both` -- both conditions at the same time.

Extra arguments after `--` are passed to pytest; without them the harness runs
the two delivery tests it was built for: the `#795` retry-window case and the
adjacent lease-window case. Both are expected green under every condition. The
lease case is what reproduced `Failed: ACK did not encounter the real outbox
row lock` under `latency-proxy` while its lease was still the 1 s product floor;
restoring a 1 s lease there makes this harness red again.

Exit status is pytest's; the harness itself exits 2 when a prerequisite is
missing or a condition silently fails to take effect. Cleanup runs in every
path: `ALTER SYSTEM RESET commit_delay/commit_siblings` + `pg_reload_conf`,
`SET GLOBAL binlog_group_commit_sync_delay` restored to its previous value, the
proxy's listeners closed, and all synthetic committers stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import ExitStack

from tests.support.flake_conditions import (
    DEFAULT_CHUNK_DELAY_MS,
    MYSQL_COMMIT_DELAY_US,
    POSTGRES_COMMIT_DELAY_US,
    MySQLSlowCommits,
    PostgresSlowCommits,
    TcpLatencyProxy,
)

POSTGRES_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
MYSQL_VARIABLE = "SPEECHNEXUS_TEST_MYSQL_URL"
DEFAULT_TARGETS = (
    "tests/integration/test_runtime_delivery.py"
    "::test_mismatched_receipt_and_retry_never_reopen_submission",
    "tests/integration/test_runtime_delivery.py"
    "::test_ack_rechecks_expiration_after_the_actual_outbox_row_lock",
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run integration tests under one explicitly requested slow condition."
    )
    parser.add_argument(
        "--condition",
        required=True,
        choices=("latency-proxy", "commit-delay", "both"),
        help="which slow condition to reproduce; there is no default",
    )
    parser.add_argument(
        "--chunk-delay-ms",
        type=float,
        default=DEFAULT_CHUNK_DELAY_MS,
        help="milliseconds added per forwarded TCP chunk (latency-proxy/both)",
    )
    parser.add_argument(
        "--commit-delay-us",
        type=int,
        default=None,
        help=(
            "commit delay in microseconds; defaults to 100000 for PostgreSQL "
            "(its maximum) and 200000 for InnoDB"
        ),
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="pytest arguments; defaults to the two delivery tests behind this issue",
    )
    arguments = parser.parse_args(argv)

    postgres_dsn = os.environ.get(POSTGRES_VARIABLE)
    mysql_dsn = os.environ.get(MYSQL_VARIABLE)
    configured = ((POSTGRES_VARIABLE, postgres_dsn), (MYSQL_VARIABLE, mysql_dsn))
    missing = [name for name, value in configured if not value]
    if missing or postgres_dsn is None or mysql_dsn is None:
        print(
            "This harness needs both isolated DSNs, as the integration suite does; missing "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    postgres_delay = arguments.commit_delay_us or POSTGRES_COMMIT_DELAY_US
    mysql_delay = arguments.commit_delay_us or MYSQL_COMMIT_DELAY_US
    targets = arguments.pytest_args or list(DEFAULT_TARGETS)
    environment = dict(os.environ)
    conditions: list[dict[str, object]] = []

    with ExitStack() as stack:
        if arguments.condition in ("latency-proxy", "both"):
            proxy = stack.enter_context(TcpLatencyProxy(chunk_delay_ms=arguments.chunk_delay_ms))
            environment[POSTGRES_VARIABLE] = proxy.rewrite(postgres_dsn)
            environment[MYSQL_VARIABLE] = proxy.rewrite(mysql_dsn)
            conditions.append(
                {
                    "condition": "tcp-latency-proxy",
                    "chunk_delay_ms": arguments.chunk_delay_ms,
                    "postgres_via": environment[POSTGRES_VARIABLE],
                    "mysql_via": environment[MYSQL_VARIABLE],
                }
            )
        if arguments.condition in ("commit-delay", "both"):
            conditions.append(
                stack.enter_context(
                    PostgresSlowCommits(postgres_dsn, commit_delay_us=postgres_delay)
                ).evidence
            )
            mysql_commits = stack.enter_context(
                MySQLSlowCommits(mysql_dsn, commit_delay_us=mysql_delay)
            )
            conditions.append(mysql_commits.evidence)

        print("flake harness conditions (measured, not assumed):")
        print(json.dumps(conditions, ensure_ascii=False, indent=2, default=str))
        print("pytest target:", " ".join(targets))
        sys.stdout.flush()
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", *targets],
            env=environment,
            check=False,
        )

    print("flake harness cleanup: conditions restored, background load stopped")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
