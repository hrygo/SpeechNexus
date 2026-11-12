"""Decide an operator-verified quarantine release from immutable evidence.

The domain owns three questions and no storage concern: what evidence is valid,
whether a stored ledger row already answers the request, and whether the locked
lease facts still permit a first release. Capacity arithmetic stays in
``lease_release.release``; this module never computes a projection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

import rfc8785

from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.domain.runtime_intents import (
    digest,
    identifier,
    integer,
    runtime_timestamp_us,
)

EVIDENCE_KIND = "operator_verified_instance_terminated"
EVIDENCE_VERSION = "quarantine-release-evidence/v1"
MAX_REASON = 2000


class QuarantineReleaseRejectedError(Exception):
    """One stable decision; the HTTP boundary maps the code, never a message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    """The caller-supplied evidence quadruple and the two canonical digests."""

    lease_id: str
    evidence_kind: str
    evidence_digest: str
    observed_at_us: int
    reason: str
    idempotency_key_digest: str | None
    request_digest: str

    def __post_init__(self) -> None:
        identifier(self.lease_id)
        if self.evidence_kind != EVIDENCE_KIND:
            raise ValueError("Unsupported quarantine release evidence kind")
        digest(self.evidence_digest)
        integer(self.observed_at_us)
        if (
            not isinstance(self.reason, str)
            or len(self.reason) > MAX_REASON
            or not self.reason.strip()
        ):
            raise ValueError("Invalid quarantine release reason")
        if self.idempotency_key_digest is not None:
            digest(self.idempotency_key_digest)
        digest(self.request_digest)


def evidence_request_digest(
    lease_id: str,
    evidence_kind: str,
    evidence_digest: str,
    observed_at_us: int,
    reason: str,
) -> str:
    """Canonical evidence digest: all four fields, reason untrimmed, RFC 8785.

    The digest is the whole comparison for "same evidence": changing the
    kind, the operator material digest, the observed time or the reason must
    produce a different value, so a reused key cannot silently mean new intent.
    """
    value: Any = [
        EVIDENCE_VERSION,
        {
            "lease_id": lease_id,
            "evidence_kind": evidence_kind,
            "evidence_digest": evidence_digest,
            "observed_at_us": observed_at_us,
            "reason": reason,
        },
    ]
    try:
        encoded = rfc8785.dumps(value)
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid quarantine release evidence") from None
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def idempotency_key_digest(key: str | None) -> str | None:
    """Absent key stays absent; the ledger must not fabricate a random one."""
    if key is None:
        return None
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise ValueError("Invalid idempotency key")
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def prepare_evidence(
    *,
    lease_id: str,
    evidence_kind: object,
    evidence_digest: object,
    observed_at: object,
    reason: object,
    idempotency_key: str | None,
) -> ReleaseEvidence:
    """Build the evidence from the wire objects validated by the contract."""
    if not isinstance(evidence_kind, str) or evidence_kind != EVIDENCE_KIND:
        raise ValueError("Unsupported quarantine release evidence kind")
    if not isinstance(evidence_digest, str):
        raise ValueError("Invalid quarantine evidence digest")
    if not isinstance(observed_at, str):
        raise ValueError("Invalid quarantine observed time")
    if not isinstance(reason, str):
        raise ValueError("Invalid quarantine release reason")
    observed_at_us = runtime_timestamp_us(observed_at)
    return ReleaseEvidence(
        lease_id,
        evidence_kind,
        evidence_digest,
        observed_at_us,
        reason,
        idempotency_key_digest(idempotency_key),
        evidence_request_digest(lease_id, evidence_kind, evidence_digest, observed_at_us, reason),
    )


@dataclass(frozen=True, slots=True)
class ReleaseIntent:
    """One submit intent of the lease as the release decision needs it."""

    submit_intent_id: str
    execution_state: str
    cleanup_state: str
    recorded_at_us: int
    dispatched_at_us: int | None
    version: int

    def __post_init__(self) -> None:
        identifier(self.submit_intent_id)
        integer(self.recorded_at_us)
        if self.dispatched_at_us is not None:
            integer(self.dispatched_at_us)
        integer(self.version, minimum=1, maximum=MAX_VERSION)


@dataclass(frozen=True, slots=True)
class ReleaseLeaseFacts:
    lease_state: str
    cleanup_state: str
    quarantine_observed_at_us: int | None
    intents: tuple[ReleaseIntent, ...]

    def __post_init__(self) -> None:
        if self.lease_state not in ("reserved", "executing", "stopping", "quarantined", "released"):
            raise ValueError("Invalid lease state")
        if self.cleanup_state not in ("not_required", "pending", "complete", "quarantined"):
            raise ValueError("Invalid lease cleanup")
        if self.quarantine_observed_at_us is not None:
            integer(self.quarantine_observed_at_us)
        if len(self.intents) > 1:
            # The model is one submit intent per lease; a release decision for
            # more than one is a reconciliation problem, never a silent loop.
            raise ValueError("A lease carries at most one submit intent")


@dataclass(frozen=True, slots=True)
class ReleaseLedger:
    """The append-only row a committed release leaves behind."""

    lease_id: str
    evidence_kind: str
    evidence_digest: str
    evidence_observed_at_us: int
    reason: str
    budget_released_at_us: int
    released_submit_intent_ids: tuple[str, ...]
    idempotency_key_digest: str | None
    request_digest: str

    def __post_init__(self) -> None:
        identifier(self.lease_id)
        if self.evidence_kind != EVIDENCE_KIND:
            raise ValueError("Unsupported quarantine release evidence kind")
        digest(self.evidence_digest)
        integer(self.evidence_observed_at_us)
        if (
            not isinstance(self.reason, str)
            or len(self.reason) > MAX_REASON
            or not self.reason.strip()
        ):
            raise ValueError("Invalid quarantine release reason")
        integer(self.budget_released_at_us)
        if self.evidence_observed_at_us > self.budget_released_at_us:
            raise ValueError("Quarantine evidence was observed after budget release")
        for intent_id in self.released_submit_intent_ids:
            identifier(intent_id)
        if (
            len(self.released_submit_intent_ids) > 1
            or tuple(sorted(self.released_submit_intent_ids)) != self.released_submit_intent_ids
        ):
            raise ValueError("Released submit intent ids are not bounded and sorted")
        if self.idempotency_key_digest is not None:
            digest(self.idempotency_key_digest)
        digest(self.request_digest)

    def released_intent_ids(self) -> list[str]:
        return list(self.released_submit_intent_ids)


def decode_released_intent_ids(value: str) -> tuple[str, ...]:
    """The stored TEXT is canonical JSON; malformed history is not "empty"."""
    try:
        parsed = json.loads(value)
    except ValueError, TypeError:
        raise ValueError("Invalid released submit intent list") from None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("Invalid released submit intent list")
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class ReleaseOutcome:
    """The final fact for this lease plus whether this call wrote it."""

    ledger: ReleaseLedger
    idempotent_replay: bool


def _key_matches(evidence: ReleaseEvidence, ledger: ReleaseLedger) -> bool:
    return (
        evidence.idempotency_key_digest is not None
        and evidence.idempotency_key_digest == ledger.idempotency_key_digest
    )


def decide_replay(
    ledger: ReleaseLedger, evidence: ReleaseEvidence
) -> Literal["replay", "idempotency_conflict", "quarantine_release_conflict"]:
    """The whole idempotency table for an existing, immutable ledger row.

    Same key and same canonical evidence replays; the same key with any other
    evidence is a conflict; a different (or absent) key with the same evidence is
    a read-only replay of the same release; anything else is a conflict.
    """
    same_evidence = evidence.request_digest == ledger.request_digest
    if _key_matches(evidence, ledger):
        return "replay" if same_evidence else "idempotency_conflict"
    return "replay" if same_evidence else "quarantine_release_conflict"


def require_releasable(facts: ReleaseLeaseFacts) -> ReleaseIntent:
    """A first release needs one unknown-quarantined intent on a quarantined lease."""
    if (
        facts.lease_state != "quarantined"
        or facts.cleanup_state != "quarantined"
        or facts.quarantine_observed_at_us is None
    ):
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    if len(facts.intents) != 1:
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    intent = facts.intents[0]
    if intent.execution_state != "unknown_quarantined" or intent.cleanup_state != "quarantined":
        raise QuarantineReleaseRejectedError("quarantine_release_conflict")
    return intent


def require_observed_at(evidence: ReleaseEvidence, intent: ReleaseIntent, now_us: int) -> None:
    """Thousands of operator clocks may be wrong; the database clock decides.

    ``observed_at`` may not precede the recorded submission (or its dispatch) and
    may not be later than the server time taken after the last possible lock
    wait. Both are request content errors, never state conflicts.
    """
    integer(now_us)
    lower = intent.recorded_at_us
    if intent.dispatched_at_us is not None:
        lower = max(lower, intent.dispatched_at_us)
    if evidence.observed_at_us < lower or evidence.observed_at_us > now_us:
        raise QuarantineReleaseRejectedError("invalid")


def evidence_matches_stored(
    evidence: ReleaseEvidence,
    *,
    evidence_kind: str | None,
    evidence_digest: str | None,
    observed_at_us: int | None,
    reason: str | None,
) -> bool:
    """Branch F: compare against a machine release's stored facts, field by field.

    All four fields must match. A machine release always has a different
    ``evidence_kind`` and a null ``reason``, so this comparison exists for a
    future release design that splits evidence from release, not to accept the
    current machine path.
    """
    return (
        evidence_kind == evidence.evidence_kind
        and evidence_digest == evidence.evidence_digest
        and observed_at_us == evidence.observed_at_us
        and reason == evidence.reason
    )


__all__ = [
    "EVIDENCE_KIND",
    "QuarantineReleaseRejectedError",
    "ReleaseEvidence",
    "ReleaseIntent",
    "ReleaseLeaseFacts",
    "ReleaseLedger",
    "ReleaseOutcome",
    "decide_replay",
    "decode_released_intent_ids",
    "evidence_matches_stored",
    "evidence_request_digest",
    "idempotency_key_digest",
    "prepare_evidence",
    "require_observed_at",
    "require_releasable",
]
