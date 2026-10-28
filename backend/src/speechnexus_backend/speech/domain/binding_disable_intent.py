"""Immutable identities for a binding-disable cancellation coordinator.

This module does not persist intents, enumerate work, request a stop, or release
resources. A future transaction must persist the complete intent and target
before dispatch and recover them by their original identities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypedDict

from speechnexus_backend.speech.domain.binding_lifecycle import BindingRetirementCommand
from speechnexus_backend.speech.domain.runtime_intents import digest, identifier, integer


class DisableAbortPayload(TypedDict):
    operation_id: str
    attempt_id: str
    fencing_token: int
    reason: Literal["resource_force_disabled"]
    wait_timeout_ms: int


@dataclass(frozen=True, slots=True)
class DisableCancellationIntent:
    """Original disable command and the publication revision it affected."""

    event_id: str
    revision_id: str
    command: BindingRetirementCommand = field(repr=False)

    def __post_init__(self) -> None:
        identifier(self.event_id)
        identifier(self.revision_id)
        if not isinstance(self.command, BindingRetirementCommand) or self.command.mode != "disable":
            raise ValueError("Cancellation intent requires an original disable command")


@dataclass(frozen=True, slots=True)
class DisableExecutionTarget:
    """A submitted execution's original routing and fencing identities."""

    submit_intent_id: str
    operation_id: str
    attempt_id: str
    lease_id: str
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
    wait_timeout_ms: int

    def __post_init__(self) -> None:
        for value in (
            self.submit_intent_id,
            self.operation_id,
            self.attempt_id,
            self.lease_id,
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
        digest(self.command_digest)
        integer(self.instance_generation, minimum=1)
        integer(self.fencing_token, minimum=1)
        integer(self.wait_timeout_ms, minimum=100, maximum=60000)

    def abort_payload(self) -> DisableAbortPayload:
        """Build the internal request without inventing a stop or release fact."""
        return {
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "reason": "resource_force_disabled",
            "wait_timeout_ms": self.wait_timeout_ms,
        }
