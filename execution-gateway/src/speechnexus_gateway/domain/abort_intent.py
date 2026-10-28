"""Original bounded abort identity, shared by receipt and dispatch transactions."""

from dataclasses import dataclass

from speechnexus_gateway.domain.submission import InvalidExecutionRequestError

ABORT_REASONS = frozenset(
    {
        "client_cancelled",
        "deadline_exceeded",
        "quota_exceeded",
        "resource_force_disabled",
        "gateway_takeover_reconcile",
        "stream_disconnected",
    }
)


@dataclass(frozen=True, slots=True)
class AbortExecutionRequest:
    submit_intent_id: str
    operation_id: str
    attempt_id: str
    fencing_token: int
    reason: str
    wait_timeout_ms: int

    def __post_init__(self) -> None:
        if self.reason not in ABORT_REASONS:
            raise InvalidExecutionRequestError("unknown abort reason")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise InvalidExecutionRequestError("abort requires a positive fencing token")
        if type(self.wait_timeout_ms) is not int or not 100 <= self.wait_timeout_ms <= 60000:
            raise InvalidExecutionRequestError("abort wait must be within the contract bounds")
