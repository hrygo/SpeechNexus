"""Two-level quarantine release: an immutable ledger read, then one short write.

Level one is a ``READ ONLY`` transaction that only reads the ledger row. A
committed ledger row is the final fact, so a replay never walks the global lock
order and never competes with admission for pool/host row locks. Level two
re-checks the ledger after acquiring every business lock in the global order,
then conditionally closes B's submission and releases A's projections.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import rfc8785

from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import LeaseRejectedError, LeaseResult
from speechnexus_backend.speech.domain.quarantine_release import (
    QuarantineReleaseRejectedError,
    ReleaseEvidence,
    ReleaseIntent,
    ReleaseLeaseFacts,
    ReleaseLedger,
    ReleaseOutcome,
    decide_replay,
    decode_released_intent_ids,
    evidence_matches_stored,
    require_observed_at,
    require_releasable,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.lease_release_sql import release_in_transaction
from speechnexus_backend.speech.infrastructure.leases_sql import (
    LeaseCursor,
    _one,
    _rows,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import TABLE

type Row = dict[str, Any]

_OPERATOR_STOP_VERSION = "operator-stop/v1"
_TERMINAL_INTENTS = frozenset({"succeeded", "failed", "aborted"})


def operator_stop_evidence_id(submit_intent_id: str) -> str:
    """Deterministic evidence id, so a rerun hits the primary key, not a new row."""
    encoded = rfc8785.dumps([_OPERATOR_STOP_VERSION, submit_intent_id])
    return "stop-operator-" + hashlib.sha256(encoded).hexdigest()[:48]


def _ledger(row: Row) -> ReleaseLedger:
    return ReleaseLedger(
        row["lease_id"],
        row["evidence_kind"],
        row["evidence_digest"],
        row["evidence_observed_at_us"],
        row["reason"],
        row["budget_released_at_us"],
        decode_released_intent_ids(row["released_submit_intent_ids"]),
        row["idempotency_key_digest"],
        row["request_digest"],
    )


def read_ledger(cursor: LeaseCursor, lease_id: str) -> ReleaseLedger | None:
    """Read the immutable ledger row; no business lock is taken here."""
    rows = _rows(
        cursor,
        "SELECT lease_id, evidence_kind, evidence_digest, evidence_observed_at_us, "
        "reason, budget_released_at_us, released_submit_intent_ids, "
        "idempotency_key_digest, request_digest "
        f"FROM {TABLE} WHERE lease_id = %s",
        (lease_id,),
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    return _ledger(rows[0])


def _intents(cursor: LeaseCursor, lease_id: str, *, locked: bool) -> list[Row]:
    suffix = " FOR UPDATE" if locked else ""
    return _rows(
        cursor,
        "SELECT submit_intent_id, execution_state, cleanup_state, recorded_at_us, "
        "dispatched_at_us, terminated_at_us, version FROM submit_intents "
        "WHERE lease_id = %s ORDER BY submit_intent_id" + suffix,
        (lease_id,),
    )


def _decide(ledger: ReleaseLedger | None, evidence: ReleaseEvidence) -> ReleaseOutcome | None:
    if ledger is None:
        return None
    outcome = decide_replay(ledger, evidence)
    if outcome == "replay":
        return ReleaseOutcome(ledger, True)
    raise QuarantineReleaseRejectedError(outcome)


def _branch_f(cursor: LeaseCursor, lease: Row, evidence: ReleaseEvidence) -> ReleaseLedger:
    budget = lease["budget_released_at_us"]
    if budget is None:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    rows = _rows(
        cursor,
        "SELECT submit_intent_id, execution_state, cleanup_state FROM submit_intents "
        "WHERE lease_id = %s",
        (lease["lease_id"],),
    )
    if len(rows) != 1 or rows[0]["execution_state"] not in _TERMINAL_INTENTS:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    if rows[0]["cleanup_state"] != "complete":
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    stops = _rows(
        cursor,
        "SELECT evidence_kind, evidence_digest, observed_at_us, reason "
        "FROM execution_stop_evidence WHERE submit_intent_id = %s",
        (rows[0]["submit_intent_id"],),
    )
    if len(stops) != 1:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    stop = stops[0]
    if not evidence_matches_stored(
        evidence,
        evidence_kind=stop["evidence_kind"],
        evidence_digest=stop["evidence_digest"],
        observed_at_us=stop["observed_at_us"],
        reason=stop["reason"],
    ):
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    # The replay carries the stored facts, not the request's: the four fields were
    # just proven equal, and building the answer from the rows keeps the response
    # correct even if the comparison above is later weakened.
    return ReleaseLedger(
        lease["lease_id"],
        stop["evidence_kind"],
        stop["evidence_digest"],
        stop["observed_at_us"],
        stop["reason"],
        budget,
        (rows[0]["submit_intent_id"],),
        None,
        evidence.request_digest,
    )


def _insert_stop_evidence(
    cursor: LeaseCursor, intent: ReleaseIntent, evidence: ReleaseEvidence
) -> None:
    cursor.execute(
        "INSERT INTO execution_stop_evidence "
        "(evidence_id, submit_intent_id, evidence_kind, evidence_digest, observed_at_us, "
        "reason, execution_stopped, resources_released) "
        "VALUES (%s, %s, %s, %s, %s, %s, 1, 1)",
        (
            operator_stop_evidence_id(intent.submit_intent_id),
            intent.submit_intent_id,
            evidence.evidence_kind,
            evidence.evidence_digest,
            evidence.observed_at_us,
            evidence.reason,
        ),
    )
    if cursor.rowcount != 1:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")


def _close_intent(
    cursor: LeaseCursor, intent: ReleaseIntent, evidence: ReleaseEvidence, now_us: int
) -> None:
    """Cross-owner CAS: the gateway's own version check loses or wins, never both."""
    cursor.execute(
        "UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete', "
        "terminated_at_us = %s, updated_at_us = %s, version = version + 1 "
        "WHERE submit_intent_id = %s AND execution_state = 'unknown_quarantined' "
        "AND cleanup_state = 'quarantined' AND version = %s",
        (evidence.observed_at_us, now_us, intent.submit_intent_id, intent.version),
    )
    if cursor.rowcount != 1:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")


def _insert_ledger(
    cursor: LeaseCursor,
    lease: Row,
    evidence: ReleaseEvidence,
    release: LeaseResult,
    intent_ids: tuple[str, ...],
) -> ReleaseLedger:
    if release.fact is None:
        raise LeaseRejectedError("reconciliation_required")
    released_at = release.fact.budget_released_at_us
    if released_at is None:
        raise LeaseRejectedError("reconciliation_required")
    cursor.execute(
        f"INSERT INTO {TABLE} (lease_id, operation_id, attempt_id, stage_id, host_node_id, "
        "instance_id, instance_generation, fencing_token, idempotency_key_digest, "
        "request_digest, evidence_kind, evidence_digest, evidence_observed_at_us, reason, "
        "budget_released_at_us, released_submit_intent_ids) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            lease["lease_id"],
            lease["operation_id"],
            lease["attempt_id"],
            lease["stage_id"],
            lease["host_node_id"],
            lease["instance_id"],
            lease["instance_generation"],
            lease["fencing_token"],
            evidence.idempotency_key_digest,
            evidence.request_digest,
            evidence.evidence_kind,
            evidence.evidence_digest,
            evidence.observed_at_us,
            evidence.reason,
            released_at,
            json.dumps(list(intent_ids)),
        ),
    )
    if cursor.rowcount != 1:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    return ReleaseLedger(
        lease["lease_id"],
        evidence.evidence_kind,
        evidence.evidence_digest,
        evidence.observed_at_us,
        evidence.reason,
        released_at,
        intent_ids,
        evidence.idempotency_key_digest,
        evidence.request_digest,
    )


def quarantine_release_in_transaction(
    cursor: LeaseCursor,
    evidence: ReleaseEvidence,
    *,
    clock: Callable[[], int],
) -> ReleaseOutcome:
    """Step 2-12 of the release transaction; the caller owns COMMIT and retries.

    A returned ledger is the committed-or-replayed fact for this lease. Every
    guard raises ``QuarantineReleaseRejectedError`` so the adapter rolls back and
    maps one stable HTTP conflict instead of leaking a driver diagnosis.
    """
    rows = _rows(cursor, "SELECT * FROM admission_leases WHERE lease_id = %s", (evidence.lease_id,))
    if not rows:
        raise QuarantineReleaseRejectedError("not_found")
    if len(rows) != 1:
        raise LeaseRejectedError("reconciliation_required")
    hint = rows[0]
    identity = (hint["operation_id"], hint["attempt_id"])
    attempt_hint = _one(
        cursor,
        "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s",
        identity,
    )
    # Ordered locks shared with admission/quarantine/settlement: pool, quota,
    # host, instance, attempt, lease, then the submit intents.
    _one(
        cursor,
        "SELECT pool_id FROM resource_pools WHERE pool_id = %s FOR UPDATE",
        (hint["pool_id"],),
    )
    _one(
        cursor,
        "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE",
        (attempt_hint["workspace_id"],),
    )
    _one(
        cursor,
        "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE",
        (hint["host_node_id"],),
    )
    instance_key = (hint["instance_id"], hint["instance_generation"])
    instance = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = %s "
        "AND instance_generation = %s FOR UPDATE",
        instance_key,
    )
    attempt = _one(
        cursor,
        "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s FOR UPDATE",
        identity,
    )
    lease = _one(
        cursor,
        "SELECT * FROM admission_leases WHERE lease_id = %s FOR UPDATE",
        (evidence.lease_id,),
    )
    frozen = (
        "lease_id",
        "operation_id",
        "attempt_id",
        "stage_id",
        "pool_id",
        "host_node_id",
        "instance_id",
        "instance_generation",
        "gateway_id",
        "fencing_token",
        "reserved_slots",
        *("reserved_" + name for name in DIMENSIONS),
    )
    if (
        any(hint[key] != lease[key] for key in frozen)
        or attempt["workspace_id"] != attempt_hint["workspace_id"]
        or any(instance[key] != lease[key] for key in ("host_node_id", "pool_id", "gateway_id"))
    ):
        raise LeaseRejectedError("reconciliation_required")
    ledger = _decide(read_ledger(cursor, evidence.lease_id), evidence)
    if ledger is not None:
        return ledger
    if lease["lease_state"] == "released":
        return ReleaseOutcome(_branch_f(cursor, lease, evidence), True)
    intents = _intents(cursor, evidence.lease_id, locked=True)
    intent = require_releasable(
        ReleaseLeaseFacts(
            lease["lease_state"],
            lease["cleanup_state"],
            lease["quarantine_observed_at_us"],
            tuple(
                ReleaseIntent(
                    row["submit_intent_id"],
                    row["execution_state"],
                    row["cleanup_state"],
                    row["recorded_at_us"],
                    row["dispatched_at_us"],
                    row["version"],
                )
                for row in intents
            ),
        )
    )
    now_us = clock()  # Database time after the last possible lock wait.
    require_observed_at(evidence, intent, now_us)
    _insert_stop_evidence(cursor, intent, evidence)
    _close_intent(cursor, intent, evidence, now_us)
    result = release_in_transaction(
        cursor, ReleaseLease(evidence.lease_id, lease["version"]), clock=clock
    )
    if result.outcome != "committed":
        raise LeaseRejectedError("reconciliation_required")
    return ReleaseOutcome(
        _insert_ledger(cursor, lease, evidence, result, (intent.submit_intent_id,)), False
    )


__all__ = [
    "operator_stop_evidence_id",
    "quarantine_release_in_transaction",
    "read_ledger",
]
