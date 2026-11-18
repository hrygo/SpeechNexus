"""Frozen non-content identity of one durable synchronous ASR settlement root."""

from __future__ import annotations

from dataclasses import dataclass

from speechnexus_backend.speech.domain.runtime_intents import identifier


@dataclass(frozen=True, slots=True)
class AsrSettlementRoot:
    """A work item, not evidence: it never carries execution state or stop facts."""

    lease_id: str
    operation_id: str
    attempt_id: str
    gateway_id: str
    host_node_id: str

    def __post_init__(self) -> None:
        for value in (
            self.lease_id,
            self.operation_id,
            self.attempt_id,
            self.gateway_id,
            self.host_node_id,
        ):
            identifier(value)
