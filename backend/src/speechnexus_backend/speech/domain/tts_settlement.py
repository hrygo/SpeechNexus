"""Frozen non-content execution identity for durable synchronous TTS settlement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.runtime_intents import digest, identifier, integer

AbortReason = Literal["client_cancelled", "deadline_exceeded", "stream_disconnected"]


@dataclass(frozen=True, slots=True)
class TtsSettlementTarget:
    submit_intent_id: str
    lease_id: str
    operation_id: str
    attempt_id: str
    stage_id: str
    workspace_id: str
    pool_id: str
    gateway_id: str
    host_node_id: str
    instance_id: str
    instance_generation: int
    binding_revision_id: str
    policy_revision_id: str
    voice_revision_id: str | None
    fencing_token: int
    command_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.submit_intent_id,
            self.lease_id,
            self.operation_id,
            self.attempt_id,
            self.stage_id,
            self.workspace_id,
            self.pool_id,
            self.gateway_id,
            self.host_node_id,
            self.instance_id,
            self.binding_revision_id,
            self.policy_revision_id,
        ):
            identifier(value)
        if self.voice_revision_id is not None:
            identifier(self.voice_revision_id)
        integer(self.instance_generation, minimum=1)
        integer(self.fencing_token, minimum=1)
        digest(self.command_digest)


@dataclass(frozen=True, slots=True)
class SettlementRoot:
    target: TtsSettlementTarget
    deadline_at_us: int
    now_us: int
    abort_reason: AbortReason | None
