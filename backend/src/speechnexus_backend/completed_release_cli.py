"""Bounded operator exit for an accepted cancellation whose execution completed.

An execution that reached ``succeeded`` while its admission lease stayed
``reserved`` cannot be moved by either automatic rule: isolation accepts only an
``unknown_quarantined`` execution, and the quarantine release needs the lease
``quarantined``. Two rules, each right, left the instance slot held with no owner
(#852). This tool is the bounded exit for exactly that shape, and it is the only
writer of the testimony it records.

The sequence is the one in ``docs/components/production-jobs.md``:

1. read the target lease and prove it is that shape;
2. take the gateway's drain response for the lease's own instance generation and
   refuse anything but ``drained`` with zero unfinished work;
3. record the testimony and release the reservation in one transaction;
4. print the instance's held and quarantined slots before and after.

The execution fact is never rewritten: the gateway's ``engine_terminal_response``
stays the proof that the execution ended, and this tool only returns the
reservation no other owner releases. The testimony is pinned to the lease's
``instance_id`` + ``instance_generation`` and covers the whole drain response, so
a testimony minted for another lease, another instance or another generation --
or one recorded before the generation was drained -- cannot authorize a release.

Run it under the dedicated release identity; the API runtime role deliberately
does not hold these grants::

    SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL=... \\
    SPEECHNEXUS_DATABASE_SCHEMA=speech \\
    python -m speechnexus_backend.completed_release_cli \\
        --lease-id lease-stage-main-... \\
        --drain-response drain.json \\
        --observed-at 2026-11-16T10:00:00Z \\
        --reason "ticket #852: drained dur-1 generation 3"

Exit codes: ``0`` released or replayed, ``2`` refused by a stable rule, ``3``
storage unavailable or an unknown outcome, ``4`` usage or input error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TextIO

from speechnexus_backend.speech.application.completed_releases import (
    CompletedLeaseReader,
    CompletedLeaseSnapshot,
    CompletedReleaseReleaser,
    CompletedReleaseService,
)
from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineSurfaceError,
    QuarantineUnavailableError,
    QuarantineUnknownError,
)
from speechnexus_backend.speech.domain.completed_release import (
    CompletedIntent,
    CompletedReleaseFacts,
    DrainRecord,
    completed_evidence_digest,
    require_completed_releasable,
    require_observed_at,
)
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseOutcome,
    prepare_evidence,
)
from speechnexus_backend.speech.domain.runtime_intents import runtime_timestamp_us
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.quarantine_release_postgres import (
    PostgresQuarantineReleases,
)

#: The dedicated release identity; the API's own DSN must not be used.
DSN_VARIABLE: Final = "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL"
SCHEMA_VARIABLE: Final = "SPEECHNEXUS_DATABASE_SCHEMA"
DEFAULT_SCHEMA: Final = "speech"
#: A drain response is three counters and five identity fields; nothing larger is one.
MAX_RESPONSE_BYTES: Final = 16384
MAX_REASON: Final = 2000

EXIT_RELEASED: Final = 0
EXIT_REFUSED: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_USAGE: Final = 4

_DRAIN_FIELDS: Final = frozenset(
    {
        "active_intent_count",
        "drain_state",
        "instance_generation",
        "instance_id",
        "quarantined_intent_count",
        "safe_to_stop_instance",
        "submitting_intent_count",
        "updated_at",
    }
)

#: One bounded operator instruction per refusal; the tool never guesses the cause.
_REFUSAL_HINTS: Final = {
    "completed_release_drain_mismatch": (
        "the drain response names another instance or generation than this lease "
        "reserved; drain that exact generation and record its response"
    ),
    "completed_release_not_drained": (
        "the drained generation still reports unfinished work or does not assert "
        "safe_to_stop_instance; wait for drained and re-drain"
    ),
    "completed_release_requires_terminal_evidence": (
        "the gateway recorded no engine_terminal_response for this execution; this "
        "shape is not releasable by testimony and needs the stop-evidence protocol"
    ),
    "quarantine_release_conflict": (
        "this lease is not an unreleased, completed execution; read it again before "
        "choosing an exit"
    ),
    "invalid": (
        "the testimony digest does not cover the facts this release decided on, or "
        "the observed time is outside the execution's own bounds"
    ),
    "not_found": "no admission lease carries this id in the configured schema",
    "idempotency_conflict": "the idempotency key already identifies other evidence",
}


def _stamp(value_us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value_us)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


class DrainResponseError(ValueError):
    """The recorded drain response is not a gateway drain response."""


def _count(payload: Mapping[str, Any], name: str) -> int:
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DrainResponseError(f"drain response field {name} is not a non-negative integer")
    return value


def parse_drain_response(payload: object) -> DrainRecord:
    """Read one recorded drain response, refusing anything that is not one.

    The record is what the release decision trusts, so it is parsed strictly: a
    missing counter, a stringified number or a boolean counter is a wrong file,
    not a value to coerce.
    """
    if not isinstance(payload, dict):
        raise DrainResponseError("drain response must be a JSON object")
    missing = _DRAIN_FIELDS - payload.keys()
    if missing:
        raise DrainResponseError("drain response is missing " + ", ".join(sorted(missing)))
    safe = payload["safe_to_stop_instance"]
    if not isinstance(safe, bool):
        raise DrainResponseError("drain response safe_to_stop_instance is not a boolean")
    state = payload["drain_state"]
    if not isinstance(state, str):
        raise DrainResponseError("drain response drain_state is not a string")
    instance_id = payload["instance_id"]
    if not isinstance(instance_id, str):
        raise DrainResponseError("drain response instance_id is not a string")
    updated_at = payload["updated_at"]
    if not isinstance(updated_at, str):
        raise DrainResponseError("drain response updated_at is not a timestamp")
    try:
        updated_at_us = runtime_timestamp_us(updated_at)
    except ValueError:
        raise DrainResponseError("drain response updated_at is not an RFC 3339 instant") from None
    generation = payload["instance_generation"]
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise DrainResponseError("drain response instance_generation is not an integer")
    try:
        return DrainRecord(
            instance_id=instance_id,
            instance_generation=generation,
            drain_state=state,
            active_intent_count=_count(payload, "active_intent_count"),
            submitting_intent_count=_count(payload, "submitting_intent_count"),
            quarantined_intent_count=_count(payload, "quarantined_intent_count"),
            safe_to_stop_instance=safe,
            updated_at_us=updated_at_us,
        )
    except ValueError as error:
        raise DrainResponseError("drain response is not a drain record") from error


def read_drain_response(path: Path) -> DrainRecord:
    """Load the recorded response from a bounded file."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        detail = error.strerror or type(error).__name__
        raise DrainResponseError(f"cannot read {path}: {detail}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DrainResponseError(
            f"{path} exceeds the {MAX_RESPONSE_BYTES}-byte drain response bound"
        )
    try:
        payload = json.loads(raw)
    except ValueError:
        raise DrainResponseError(f"{path} is not JSON") from None
    return parse_drain_response(payload)


def completed_facts(
    snapshot: CompletedLeaseSnapshot,
    *,
    drain: DrainRecord,
    observed_at_us: int,
    reason: str,
) -> CompletedReleaseFacts:
    """Project one read-only snapshot into the fact the gate decides on.

    The lease carries at most one submit intent; a snapshot without one cannot be
    a completed execution, and a JSON ``null`` is never read as a default.
    """
    if snapshot.submit_intent_id is None or snapshot.execution_state is None:
        raise ValueError("lease has no submit intent, so no execution completed")
    return CompletedReleaseFacts(
        lease_id=snapshot.lease_id,
        operation_id=snapshot.operation_id,
        instance_id=snapshot.instance_id,
        instance_generation=snapshot.instance_generation,
        lease_state=snapshot.lease_state,
        lease_cleanup_state=snapshot.lease_cleanup_state,
        budget_released_at_us=snapshot.budget_released_at_us,
        quarantine_observed_at_us=snapshot.quarantine_observed_at_us,
        intents=(
            CompletedIntent(
                submit_intent_id=snapshot.submit_intent_id,
                execution_state=snapshot.execution_state,
                cleanup_state=snapshot.execution_cleanup_state or "",
                recorded_at_us=snapshot.recorded_at_us or 0,
                dispatched_at_us=snapshot.dispatched_at_us,
                terminated_at_us=snapshot.terminated_at_us,
                version=snapshot.intent_version or 1,
            ),
        ),
        stop=snapshot.stop,
        drain=drain,
        observed_at_us=observed_at_us,
        reason=reason,
    )


def _target(out: TextIO, snapshot: CompletedLeaseSnapshot) -> None:
    print("target:", file=out)
    for name, value in (
        ("lease_id", snapshot.lease_id),
        ("operation_id", snapshot.operation_id),
        ("attempt_id", snapshot.attempt_id),
        ("stage_id", snapshot.stage_id),
        ("instance_id", snapshot.instance_id),
        ("instance_generation", str(snapshot.instance_generation)),
        ("submit_intent_id", snapshot.submit_intent_id or "none"),
        ("lease_state", snapshot.lease_state),
        ("lease_cleanup_state", snapshot.lease_cleanup_state),
        ("execution_state", snapshot.execution_state or "none"),
        ("execution_cleanup_state", snapshot.execution_cleanup_state or "none"),
        ("terminal_evidence", snapshot.stop.evidence_kind if snapshot.stop else "none"),
    ):
        print(f"  {name}: {value}", file=out)


def _capacity(out: TextIO, snapshot: CompletedLeaseSnapshot, *, label: str) -> None:
    print(f"capacity {label}:", file=out)
    print(f"  instance_held_slots: {snapshot.instance_held_slots}", file=out)
    print(f"  instance_quarantined_slots: {snapshot.instance_quarantined_slots}", file=out)


def _testimony(out: TextIO, drain: DrainRecord, digest: str, observed_at_us: int) -> None:
    print("testimony:", file=out)
    print("  evidence_kind: operator_verified_instance_terminated", file=out)
    print(f"  evidence_digest: {digest}", file=out)
    print(f"  observed_at: {_stamp(observed_at_us)}", file=out)
    print(f"  drain_instance_id: {drain.instance_id}", file=out)
    print(f"  drain_instance_generation: {drain.instance_generation}", file=out)
    print(f"  drain_state: {drain.drain_state}", file=out)
    print(f"  drain_safe_to_stop: {'true' if drain.safe_to_stop_instance else 'false'}", file=out)
    print(f"  drain_unfinished_intents: {drain.unfinished_intent_count}", file=out)
    print(f"  drain_updated_at: {_stamp(drain.updated_at_us)}", file=out)


def _evidence(
    snapshot: CompletedLeaseSnapshot,
    *,
    drain: DrainRecord,
    observed_at_text: str,
    reason: str,
    idempotency_key: str | None,
) -> tuple[Any, int, str]:
    """Build the evidence triple, or raise for a digest that cannot match."""
    assert snapshot.submit_intent_id is not None  # guarded by completed_facts
    observed_at_us = runtime_timestamp_us(observed_at_text)
    digest = completed_evidence_digest(
        lease_id=snapshot.lease_id,
        submit_intent_id=snapshot.submit_intent_id,
        instance_id=snapshot.instance_id,
        instance_generation=snapshot.instance_generation,
        drain=drain,
        observed_at_us=observed_at_us,
        reason=reason,
    )
    evidence = prepare_evidence(
        lease_id=snapshot.lease_id,
        evidence_kind="operator_verified_instance_terminated",
        evidence_digest=digest,
        observed_at=observed_at_text,
        reason=reason,
        idempotency_key=idempotency_key,
    )
    return evidence, observed_at_us, digest


def _released(out: TextIO, outcome: ReleaseOutcome) -> None:
    ledger = outcome.ledger
    print("release:", file=out)
    print(f"  outcome: {'replayed' if outcome.idempotent_replay else 'committed'}", file=out)
    print(f"  budget_released_at: {_stamp(ledger.budget_released_at_us)}", file=out)
    print(
        "  released_submit_intent_ids: " + ", ".join(ledger.released_submit_intent_ids),
        file=out,
    )


def execute(
    *,
    adapter: CompletedLeaseReader,
    service: CompletedReleaseReleaser,
    lease_id: str,
    drain: DrainRecord,
    observed_at_text: str,
    reason: str,
    idempotency_key: str | None,
    dry_run: bool,
    out: TextIO,
) -> int:
    """The whole exit, from one read-only snapshot to the released capacity."""
    try:
        snapshot = adapter.completed_snapshot(lease_id)
    except QuarantineUnavailableError:
        print("unavailable: the lease snapshot could not be read", file=out)
        return EXIT_UNAVAILABLE
    except QuarantineSurfaceError as error:
        print(f"unavailable: {error.code}", file=out)
        return EXIT_UNAVAILABLE
    if snapshot is None:
        print(f"refused: not_found ({_REFUSAL_HINTS['not_found']})", file=out)
        return EXIT_REFUSED
    _target(out, snapshot)
    _capacity(out, snapshot, label="before")
    already_released = snapshot.lease_state == "released"
    if already_released:
        # The lease is released, so a committed ledger row (or its absence) is the
        # decision. Re-deciding the gate here would refuse every legitimate retry.
        print("lease: already released; the committed ledger decides this retry", file=out)
    try:
        facts = completed_facts(
            snapshot,
            drain=drain,
            observed_at_us=runtime_timestamp_us(observed_at_text),
            reason=reason,
        )
        if not already_released:
            intent = require_completed_releasable(facts)
            require_observed_at(facts, intent, _now_us())
        evidence, observed_at_us, digest = _evidence(
            snapshot,
            drain=drain,
            observed_at_text=observed_at_text,
            reason=reason,
            idempotency_key=idempotency_key,
        )
    except QuarantineReleaseRejectedError as error:
        print(f"refused: {error.code} ({_REFUSAL_HINTS.get(error.code, 'no hint')})", file=out)
        return EXIT_REFUSED
    except ValueError as error:
        print(f"refused: invalid ({error})", file=out)
        return EXIT_USAGE
    _testimony(out, drain, digest, observed_at_us)
    if dry_run:
        print(
            "verdict: already released (a retry replays the committed ledger row)"
            if already_released
            else "verdict: releasable (dry run, nothing written)",
            file=out,
        )
        return EXIT_RELEASED
    try:
        outcome = service.release(evidence, drain)
    except QuarantineReleaseRejectedError as error:
        print(f"refused: {error.code} ({_REFUSAL_HINTS.get(error.code, 'no hint')})", file=out)
        return EXIT_REFUSED
    except QuarantineUnavailableError:
        print("unavailable: the release transaction did not start; retry is safe", file=out)
        return EXIT_UNAVAILABLE
    except QuarantineUnknownError:
        print(
            "unknown: the commit outcome is unknown; read the lease and the release "
            "ledger before retrying",
            file=out,
        )
        return EXIT_UNAVAILABLE
    except QuarantineSurfaceError as error:
        print(f"unavailable: {error.code}", file=out)
        return EXIT_UNAVAILABLE
    _released(out, outcome)
    try:
        after = adapter.completed_snapshot(lease_id)
    except QuarantineUnavailableError, QuarantineSurfaceError:
        after = None
    if after is None:
        print("capacity after: unavailable (read again after the release)", file=out)
    else:
        _capacity(out, after, label="after")
        if after.instance_held_slots != 0:
            print(
                "capacity note: this instance generation still holds "
                f"{after.instance_held_slots} slot(s) from another lease; this release "
                "returned only its own",
                file=out,
            )
    print(
        "next: the accepted cancellation stays claimable, so the reconciliation worker "
        "closes the Job as failed/cancellation_unconfirmed on its next pass",
        file=out,
    )
    return EXIT_RELEASED


def _now_us() -> int:
    """Local time is only an input bound here; storage re-reads the database clock."""
    return int(datetime.now(UTC).timestamp() * 1_000_000)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m speechnexus_backend.completed_release_cli",
        description=(
            "Release the admission reservation of a completed execution whose "
            "accepted cancellation cannot be closed automatically."
        ),
    )
    parser.add_argument("--lease-id", required=True, help="admission lease to release")
    parser.add_argument(
        "--drain-response",
        required=True,
        type=Path,
        help="file holding the gateway drain response for that instance generation",
    )
    parser.add_argument(
        "--observed-at",
        required=True,
        help="RFC 3339 instant of the drain observation recorded as testimony",
    )
    parser.add_argument("--reason", required=True, help="operator reason, at most 2000 chars")
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the target, testimony and verdict without writing",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    stream = sys.stdout if out is None else out
    args = _parser().parse_args(argv)
    reason = args.reason.strip()
    if not reason or len(args.reason) > MAX_REASON:
        print("usage: --reason must be 1-2000 characters", file=stream)
        return EXIT_USAGE
    try:
        runtime_timestamp_us(args.observed_at)
    except ValueError:
        print("usage: --observed-at must be an RFC 3339 instant", file=stream)
        return EXIT_USAGE
    try:
        drain = read_drain_response(args.drain_response)
    except DrainResponseError as error:
        print(f"usage: {error}", file=stream)
        return EXIT_USAGE
    dsn, schema = os.environ.get(DSN_VARIABLE), os.environ.get(SCHEMA_VARIABLE, DEFAULT_SCHEMA)
    if not dsn:
        print(f"usage: set {DSN_VARIABLE} to the dedicated release identity", file=stream)
        return EXIT_USAGE
    if len(args.lease_id) < 3:
        print("usage: --lease-id is too short to be an admission lease", file=stream)
        return EXIT_USAGE
    name = "speech-completed-release-" + uuid.uuid4().hex[:8]
    pool = None
    try:
        pool = speech_pool(dsn, schema=schema, max_size=1, name=name)
        pool.open()
    except Exception:
        if pool is not None:
            pool.close()
        print("unavailable: the release identity could not open a pool", file=stream)
        return EXIT_UNAVAILABLE
    try:
        adapter = PostgresQuarantineReleases(pool)
        return execute(
            adapter=adapter,
            service=CompletedReleaseService(adapter),
            lease_id=args.lease_id,
            drain=drain,
            observed_at_text=args.observed_at,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
            dry_run=bool(args.dry_run),
            out=stream,
        )
    finally:
        pool.close()


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
