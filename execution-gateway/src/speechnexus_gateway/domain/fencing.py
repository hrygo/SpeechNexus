"""Gateway ingress proof and durable barrier facts, independent of locks and drivers."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    StaleFencingTokenError,
    SubmissionStoreError,
    contract_instant_text,
)

POOLS = frozenset(
    {"asr-realtime", "tts-realtime", "asr-batch", "tts-batch", "voice-creation", "auxiliary"}
)


@dataclass(frozen=True, slots=True)
class BarrierRequest:
    pool_id: str
    new_owner_scheduler_id: str
    min_accepted_fencing_token: int
    abort_in_flight: bool

    def __post_init__(self) -> None:
        if self.pool_id not in POOLS:
            raise InvalidExecutionRequestError("unknown ingress pool")
        if (
            type(self.min_accepted_fencing_token) is not int
            or not 1 <= self.min_accepted_fencing_token <= 2**53 - 1
        ):
            raise InvalidExecutionRequestError("fencing token exceeds the safe integer domain")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", self.new_owner_scheduler_id):
            raise InvalidExecutionRequestError("invalid owner identity")
        if type(self.abort_in_flight) is not bool:
            raise InvalidExecutionRequestError("abort_in_flight must be explicit boolean")


@dataclass(frozen=True, slots=True)
class IngressFence:
    """Produced only while the gateway owns its process lease and has drained local sends."""

    gateway_id: str
    host_node_id: str
    request: BarrierRequest

    @property
    def evidence_digest(self) -> str:
        # The persistent fact attests the enforced minimum. Inventory and observation time
        # are fresh response observations, not a second immutable execution ledger.
        return command_digest(
            {
                "gateway_id": self.gateway_id,
                "host_node_id": self.host_node_id,
                "pool_id": self.request.pool_id,
                "owner_id": self.request.new_owner_scheduler_id,
                "min_fencing_token": self.request.min_accepted_fencing_token,
                "barrier_sealed": True,
            }
        )


@dataclass(frozen=True, slots=True)
class BarrierSnapshot:
    gateway_id: str
    pool_id: str
    min_fencing_token: int
    evidence_digest: str
    version: int
    observed_at_us: int
    submitting_intent_ids: tuple[str, ...]
    executing_intent_ids: tuple[str, ...]
    quarantined_intent_ids: tuple[str, ...]

    def response(self) -> dict[str, object]:
        return {
            "gateway_id": self.gateway_id,
            "pool_id": self.pool_id,
            "enforced_min_fencing_token": self.min_fencing_token,
            "barrier_sealed": True,
            "submitting_intent_ids": list(self.submitting_intent_ids),
            "executing_intent_ids": list(self.executing_intent_ids),
            "quarantined_intent_ids": list(self.quarantined_intent_ids),
            "enforced_at": contract_instant_text(
                datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=self.observed_at_us)
            ),
        }


def verify_barrier_authority(
    fence: IngressFence,
    *,
    owner_id: str | None,
    fencing_token: int,
    expires_at_us: int,
    now_us: int,
) -> None:
    if (
        owner_id != fence.request.new_owner_scheduler_id
        or fencing_token != fence.request.min_accepted_fencing_token
        or now_us >= expires_at_us
    ):
        raise StaleFencingTokenError("barrier does not belong to the current unexpired owner")


class IngressUnavailableError(SubmissionStoreError):
    """No sealed acknowledgment: another local send or process still owns the entry."""

    recovery = "retry_transaction"


class BarrierConflictError(SubmissionStoreError):
    code = "idempotency_conflict"
    recovery = "query_current_authority"
