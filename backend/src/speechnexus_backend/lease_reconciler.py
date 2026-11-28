"""Bounded automatic sweep of reserved leases no dispatch can reach (#1248).

A Job that terminated before any dispatcher claimed it left its admission lease in
``reserved`` and the four capacity projections held: the return path
(``_settle_not_submitted``) is only reachable *after* a claim, so an accepted
cancellation of a still-``pending`` Job leaked the slots for good and the next
admission answered ``capacity_exhausted``. This process is the coordinator-side
sweep that settles exactly that shape.

One invocation is one bounded pass: read at most ``--limit`` eligible leases (the
predicate lives in ``speech/infrastructure/lease_reconciliation_sql.py`` and nowhere
else) and release each through the one release implementation
(``LeaseService.release`` -> ``lease_release_sql.release_in_transaction``). It writes
no testimony and no ledger row: the released lease row itself is the durable fact,
as it is for the dispatcher's own release.

Scheduling is a CronJob in ``deploy/cluster/app-plane/workers.yaml`` (one pass per
run, ``concurrencyPolicy: Forbid``). Idempotency comes from the release itself: a
second run -- racing or after a crash -- reads an already released lease and reports
``replayed`` instead of rolling any counter back twice.

Output is one JSON line per examined lease plus a pass summary, so a backlog can be
audited against the rows afterwards::

    {"attempt_id":"...","decision":"released","operation_id":"...","reserved_slots":1,...}
    {"event":"lease_reconciliation_pass","candidates":1,"limit":8,"released":1,...}

Exit codes: ``0`` the pass completed (including "nothing to do"), ``2`` at least one
lease was refused or its outcome is unknown -- the failed Job is the alarm, ``3``
the release identity or the database is unavailable, ``4`` usage.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from typing import Any, Final, TextIO

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.quarantine_release_runtime import (
    verify_quarantine_release_role,
)
from speechnexus_backend.speech.application.lease_reconciliation import (
    DEFAULT_SWEEP,
    MAX_SWEEP,
    LeaseReconciliation,
    LeaseReconciliationReport,
    LeaseSweep,
)
from speechnexus_backend.speech.infrastructure.lease_reconciliation_postgres import (
    PostgresLeaseReconciliation,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import (
    PostgresLeaseTransactions,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool

#: The release identity. It is the same identity the operator release CLI uses, and
#: it deliberately is not the API runtime role: only this role holds the column
#: UPDATE set the release performs.
DSN_VARIABLE: Final = "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL"
DEFAULT_SCHEMA: Final = "speech"

EXIT_CLEAN: Final = 0
EXIT_ALARM: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_USAGE: Final = 4


class LeaseReconcilerSettings(BaseSettings):
    """The two operating values; a missing DSN prevents startup instead of guessing."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        frozen=True,
    )

    database_url: SecretStr = Field(validation_alias=DSN_VARIABLE, min_length=1)
    database_schema: str = Field(
        default=DEFAULT_SCHEMA,
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA",
        pattern=r"^[a-z][a-z0-9_]{2,62}$",
    )


def sweep_line(sweep: LeaseSweep) -> dict[str, Any]:
    """One reviewable line: which lease, which operation, what it became.

    The signed numbers are the reservation that was returned, read from the row the
    selection statement returned rather than recomputed here, so the line and the
    database can be compared after the fact.
    """
    candidate = sweep.candidate
    return {
        "event": "lease_reconciliation",
        "lease_id": candidate.lease_id,
        "operation_id": candidate.operation_id,
        "attempt_id": candidate.attempt_id,
        "stage_id": candidate.stage_id,
        "decision": sweep.decision,
        "reserved_slots": candidate.reserved_slots,
        "attempt_quota_state": candidate.attempt_quota_state,
        "error_code": sweep.error_code,
    }


def pass_line(report: LeaseReconciliationReport) -> dict[str, Any]:
    """The pass summary: how much was read, how much was returned, and the bound."""
    return {
        "event": "lease_reconciliation_pass",
        "limit": report.limit,
        "candidates": len(report.sweeps),
        "planned": report.planned,
        "released": report.released,
        "replayed": report.replayed,
        "refused": report.refused,
        "unknown": report.unknown,
        "returned_slots": report.returned_slots,
    }


def _emit(out: TextIO, payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=out, flush=True)


def run(
    *,
    release: LeaseReconciliation,
    limit: int,
    dry_run: bool,
    out: TextIO,
) -> int:
    """The whole pass: bounded read, one release per candidate, one line each."""
    report = release.run_once(limit=limit, dry_run=dry_run)
    for sweep in report.sweeps:
        _emit(out, sweep_line(sweep))
    _emit(out, pass_line(report))
    if report.refused or report.unknown:
        # A conservative selection that still hit a refusal means the ledger and the
        # lease disagree; that is an operator alarm, not a quiet line in a log.
        return EXIT_ALARM
    return EXIT_CLEAN


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speechnexus-lease-reconciler",
        description=(
            "Return the capacity of reserved admission leases whose Job is already "
            "terminal and whose execution was never submitted."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SWEEP,
        help=f"leases one pass may examine, 1..{MAX_SWEEP} (default {DEFAULT_SWEEP})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the bounded set and write nothing",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    stream = sys.stdout if out is None else out
    args = _parser().parse_args(argv)
    if not 1 <= args.limit <= MAX_SWEEP:
        print(f"usage: --limit must be within 1..{MAX_SWEEP}", file=stream)
        return EXIT_USAGE
    try:
        settings = LeaseReconcilerSettings()
    except Exception:
        # A pydantic validation error renders its input, and the input is a DSN.
        print(f"usage: set {DSN_VARIABLE} and a valid database schema", file=stream)
        return EXIT_USAGE
    name = "speech-lease-reconciler-" + uuid.uuid4().hex[:8]
    # The pool's own diagnostic logger can print the host and principal; the sweep
    # replaces that with its own stable line below instead of leaking either.
    pool_logger = logging.getLogger("psycopg.pool")
    previous_disabled = pool_logger.disabled
    pool_logger.disabled = True
    pool = None
    try:
        try:
            pool = speech_pool(
                settings.database_url.get_secret_value(),
                schema=settings.database_schema,
                max_size=1,
                name=name,
            )
            pool.open()
            # The exact startup boundary the operator release holds: a role with a
            # wider or narrower surface is refused by name before the first lease is
            # read.
            verify_quarantine_release_role(pool, settings.database_schema)
        except ValueError as error:
            # Only our own boundary checks carry a safe message (relation/column names).
            print(f"unavailable: lease reconciliation startup rejected ({error})", file=stream)
            return EXIT_UNAVAILABLE
        except Exception:
            print("unavailable: the release identity could not open a pool", file=stream)
            return EXIT_UNAVAILABLE
        try:
            reconciliation = LeaseReconciliation(
                PostgresLeaseReconciliation(pool, PostgresLeaseTransactions(pool))
            )
            return run(release=reconciliation, limit=args.limit, dry_run=args.dry_run, out=stream)
        except Exception:
            # Never echo the driver message: a conninfo can carry the DSN.
            print("unavailable: the sweep did not complete; rerun it", file=stream)
            return EXIT_UNAVAILABLE
    finally:
        pool_logger.disabled = previous_disabled
        if pool is not None:
            pool.close()


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
