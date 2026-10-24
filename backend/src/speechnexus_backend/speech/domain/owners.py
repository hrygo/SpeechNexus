"""Owner epochs and lease decisions; no clock, database or boundary DTO imports."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

MAX_COUNTER = 2**53 - 1
MAX_VERSION = 2**63 - 1
POOLS = frozenset(
    {"asr-realtime", "tts-realtime", "asr-batch", "tts-batch", "voice-creation", "auxiliary"}
)
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
type OwnerState = Literal["reconciling_takeover", "active"]


def counter(value: int, *, minimum: int = 0, maximum: int = MAX_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid owner counter")
    return value


def identity(pool_id: str, owner_id: str | None) -> None:
    if (
        not isinstance(pool_id, str)
        or pool_id not in POOLS
        or (
            owner_id is not None
            and (not isinstance(owner_id, str) or not IDENTIFIER.fullmatch(owner_id))
        )
    ):
        raise ValueError("Invalid owner identity")


@dataclass(frozen=True, slots=True)
class OwnerAuthority:
    pool_id: str
    owner_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str):
            raise ValueError("Invalid owner identity")
        identity(self.pool_id, self.owner_id)
        counter(self.fencing_token, minimum=1)


@dataclass(frozen=True, slots=True)
class AcquireOwner:
    pool_id: str
    owner_id: str
    expected_version: int
    ttl_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str):
            raise ValueError("Invalid owner identity")
        identity(self.pool_id, self.owner_id)
        counter(self.expected_version, minimum=1, maximum=MAX_VERSION)
        counter(self.ttl_ms, minimum=1, maximum=60000)


@dataclass(frozen=True, slots=True)
class RenewOwner:
    authority: OwnerAuthority
    expected_version: int
    ttl_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.authority, OwnerAuthority):
            raise ValueError("Invalid owner authority")
        counter(self.expected_version, minimum=1, maximum=MAX_VERSION)
        counter(self.ttl_ms, minimum=1, maximum=60000)


@dataclass(frozen=True, slots=True)
class ActivateOwner:
    authority: OwnerAuthority
    expected_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.authority, OwnerAuthority):
            raise ValueError("Invalid owner authority")
        counter(self.expected_version, minimum=1, maximum=MAX_VERSION)


@dataclass(frozen=True, slots=True)
class PoolOwnership:
    pool_id: str
    owner_id: str | None
    fencing_token: int
    expires_at_us: int
    owner_state: OwnerState
    version: int

    def __post_init__(self) -> None:
        identity(self.pool_id, self.owner_id)
        counter(self.fencing_token)
        counter(self.expires_at_us)
        counter(self.version, minimum=1, maximum=MAX_VERSION)
        if self.owner_state not in ("reconciling_takeover", "active"):
            raise ValueError("Invalid owner state")
        if self.owner_id is None:
            if (
                self.fencing_token
                or self.expires_at_us
                or self.owner_state != "reconciling_takeover"
            ):
                raise ValueError("Unowned pool cannot expose an authority")
        elif not self.fencing_token:
            raise ValueError("An owner requires a positive fencing token")


class OwnerRejectedError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BarrierFact:
    gateway_id: str
    min_fencing_token: int
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.gateway_id, str) or not IDENTIFIER.fullmatch(self.gateway_id):
            raise ValueError("Invalid gateway identity")
        counter(self.min_fencing_token, minimum=1)
        if not isinstance(self.evidence_digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.evidence_digest
        ):
            raise ValueError("Invalid barrier evidence digest")


@dataclass(frozen=True, slots=True)
class ActivationFacts:
    affected_gateways: frozenset[str]
    barriers: tuple[BarrierFact, ...]
    unresolved_submissions: bool
    unaccounted_quarantines: bool

    def ready_for(self, token: int) -> bool:
        sealed = {fact.gateway_id: fact.min_fencing_token for fact in self.barriers}
        return (
            not self.unresolved_submissions
            and not self.unaccounted_quarantines
            and all(sealed.get(gateway, 0) >= token for gateway in self.affected_gateways)
        )


@dataclass(frozen=True, slots=True)
class OwnerResult:
    outcome: Literal["committed", "replayed", "conflict", "rejected", "unknown"]
    pool_id: str
    fact: PoolOwnership | None = None
    error_code: str | None = None
    recovery: Literal["none", "retry_transaction", "query_current_authority", "reconcile"] = "none"

    def __post_init__(self) -> None:
        if self.pool_id not in POOLS:
            raise ValueError("Invalid owner pool")
        if self.outcome in ("committed", "replayed"):
            if (
                self.fact is None
                or self.fact.owner_id is None
                or self.fact.pool_id != self.pool_id
                or self.error_code is not None
                or self.recovery != "none"
            ):
                raise ValueError("Owner success requires its complete authority snapshot")
        elif self.fact is not None or not self.error_code:
            raise ValueError("Unresolved owner result cannot expose a successful snapshot")
        if self.outcome == "unknown" and (
            self.error_code != "outcome_unknown" or self.recovery != "query_current_authority"
        ):
            raise ValueError("Unknown owner outcome requires a current authority query")


def reject(pool_id: str, code: str) -> OwnerResult:
    query = code in {"version_conflict", "owner_busy", "stale_fencing_token"}
    return OwnerResult(
        "conflict" if code == "version_conflict" else "rejected",
        pool_id,
        error_code=code,
        recovery="query_current_authority" if query else "reconcile",
    )


def unknown(pool_id: str) -> OwnerResult:
    return OwnerResult(
        "unknown", pool_id, error_code="outcome_unknown", recovery="query_current_authority"
    )


def next_version(current: PoolOwnership, expected: int) -> int:
    if current.version != expected:
        raise OwnerRejectedError("version_conflict")
    if current.version == MAX_VERSION:
        raise OwnerRejectedError("invalid_counter")
    return current.version + 1


def require_authority(current: PoolOwnership, authority: OwnerAuthority, now_us: int) -> None:
    try:
        counter(now_us)
    except ValueError:
        raise OwnerRejectedError("invalid_counter") from None
    if (
        current.pool_id != authority.pool_id
        or current.owner_id != authority.owner_id
        or current.fencing_token != authority.fencing_token
        or current.expires_at_us <= now_us
    ):
        raise OwnerRejectedError("stale_fencing_token")


def acquired(current: PoolOwnership, request: AcquireOwner, now_us: int) -> PoolOwnership:
    try:
        counter(now_us)
    except ValueError:
        raise OwnerRejectedError("invalid_counter") from None
    version = next_version(current, request.expected_version)
    if current.pool_id != request.pool_id:
        raise OwnerRejectedError("reconciliation_required")
    if current.expires_at_us > now_us:
        raise OwnerRejectedError("owner_busy")
    try:
        return PoolOwnership(
            current.pool_id,
            request.owner_id,
            current.fencing_token + 1,
            counter(now_us) + request.ttl_ms * 1000,
            "reconciling_takeover",
            version,
        )
    except ValueError:
        raise OwnerRejectedError("invalid_counter") from None


def renewed(current: PoolOwnership, request: RenewOwner, now_us: int) -> PoolOwnership:
    require_authority(current, request.authority, now_us)
    version = next_version(current, request.expected_version)
    try:
        return replace(
            current, expires_at_us=counter(now_us) + request.ttl_ms * 1000, version=version
        )
    except ValueError:
        raise OwnerRejectedError("invalid_counter") from None


def activated(
    current: PoolOwnership, request: ActivateOwner, now_us: int, evidence: ActivationFacts
) -> PoolOwnership:
    require_authority(current, request.authority, now_us)
    version = next_version(current, request.expected_version)
    if not evidence.ready_for(current.fencing_token):
        raise OwnerRejectedError("reconciliation_required")
    return replace(current, owner_state="active", version=version)
