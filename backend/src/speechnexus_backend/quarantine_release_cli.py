"""Settle one quarantined admission lease from the command line, without the API.

``#1299`` recorded the deadlock: the API's startup preflight refuses to start while a
lease is held past its settlement window, and every operator exit for that lease --
``POST /v1/nexus/admin/admission-leases/<lease>/quarantine-releases`` among them --
was an API route. The process that must clear the blockage was therefore the process
the blockage prevented from starting.

This console script is that exit, moved out of the API's lifecycle. It reuses the
same service and the same ledger the route uses (``QuarantineReleaseService`` over
``PostgresQuarantineReleaseTransactions``), so there is one implementation of the
release rules and two ways to reach it:

    # what would it do? read-only, never writes
    SPEECHNEXUS_DATABASE_URL=... speechnexus-quarantine-release \
        --operation job-abc --evidence-digest sha256:... --reason "node terminated" --check

    # actually settle it (the write needs --confirm)
    ... --confirm

Two rules the script keeps on purpose:

* it resolves the lease from the **operation id**, the identity an operator can read
  out of the preflight's own refusal, and it refuses when that operation has zero or
  more than one unsettled lease rather than guessing;
* ``--check`` never writes, so the same command is safe to run while the API is
  crash-looping (which is exactly when it is needed).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineReleaseService,
    QuarantineSurfaceError,
    QuarantineUnavailableError,
    QuarantineUnknownError,
)
from speechnexus_backend.speech.domain.quarantine_release import (
    EVIDENCE_KIND,
    ReleaseEvidence,
    evidence_request_digest,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.quarantine_release_postgres import (
    PostgresQuarantineReleases,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_INVALID = 2

DEFAULT_SCHEMA = "speech"
_LEASE_QUERY = (
    "SELECT lease_id, operation_id, attempt_id, lease_state, cleanup_state "
    "FROM admission_leases WHERE operation_id = ? AND budget_released_at_us IS NULL"
)


def unsettled_lease(pool: object, operation_id: str) -> dict[str, object]:
    """The one unsettled lease for this operation; refusal when it is not exactly one."""
    with pool.connection(timeout=3) as connection:  # type: ignore[attr-defined]
        connection.execute("SET TRANSACTION READ ONLY")
        rows = connection.execute(_LEASE_QUERY, (operation_id,)).fetchall()
    if not rows:
        raise LookupError(f"no unsettled lease for operation {operation_id}")
    if len(rows) != 1:
        raise LookupError(
            f"{len(rows)} unsettled leases for operation {operation_id}; name one explicitly"
        )
    return dict(rows[0])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Release one quarantined admission lease without the speech API"
    )
    parser.add_argument("--operation", required=True, help="the operation id the refusal named")
    parser.add_argument(
        "--evidence-digest",
        default="sha256:" + "0" * 64,
        help="digest of the operator's stop evidence (default: an explicit all-zero digest)",
    )
    parser.add_argument(
        "--observed-at-us",
        type=int,
        default=0,
        help="when the stop was observed, epoch microseconds (0 = not recorded)",
    )
    parser.add_argument("--reason", default="operator_verified_instance_terminated")
    parser.add_argument(
        "--check", action="store_true", help="read only: say what would be released"
    )
    parser.add_argument("--confirm", action="store_true", help="perform the release")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--schema", default=None)
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)

    database_url = options.database_url or os.environ.get("SPEECHNEXUS_DATABASE_URL")
    if not database_url:
        print(
            "no coordination DSN: pass --database-url or SPEECHNEXUS_DATABASE_URL",
            file=sys.stderr,
        )
        return EXIT_INVALID
    schema = options.schema or os.environ.get("SPEECHNEXUS_DATABASE_SCHEMA") or DEFAULT_SCHEMA

    pool = speech_pool(database_url, schema=schema, max_size=1, name="quarantine-release")
    try:
        pool.open()
        try:
            lease = unsettled_lease(pool, options.operation)
        except LookupError as error:
            print(f"refused: {error}", file=sys.stderr)
            return EXIT_REFUSED
        lease_id = str(lease["lease_id"])
        evidence = ReleaseEvidence(
            lease_id=lease_id,
            evidence_kind=EVIDENCE_KIND,
            evidence_digest=options.evidence_digest,
            observed_at_us=options.observed_at_us,
            reason=options.reason,
            idempotency_key_digest=None,
            request_digest=evidence_request_digest(
                lease_id,
                EVIDENCE_KIND,
                options.evidence_digest,
                options.observed_at_us,
                options.reason,
            ),
        )
        if options.check or not options.confirm:
            print(
                f"would release lease_id={lease_id} operation_id={options.operation} "
                f"attempt_id={lease['attempt_id']} state={lease['lease_state']}/"
                f"{lease['cleanup_state']} (re-run with --confirm to write)",
                file=sys.stdout,
            )
            return EXIT_OK
        service = QuarantineReleaseService(PostgresQuarantineReleases(pool, timeout_seconds=3))
        outcome = service.release(evidence)
        print(
            f"released lease_id={lease_id} operation_id={options.operation} "
            f"replay={getattr(outcome, 'replayed', False)}",
            file=sys.stdout,
        )
        return EXIT_OK
    except QuarantineSurfaceError as error:
        print(
            f"refused: the storage surface cannot serve this release ({error.code})",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    except QuarantineUnavailableError:
        print("refused: the release transaction never started; retry is safe", file=sys.stderr)
        return EXIT_REFUSED
    except QuarantineUnknownError:
        print("unknown: the commit result is unknown; query before retrying", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as error:  # any driver failure is a refusal, never a silent success
        print(f"refused: {type(error).__name__}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
