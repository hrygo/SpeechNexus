"""Ports the node controller needs from outside its domain.

Implementations live in ``infrastructure`` (adapters) or in tests (fakes); the
domain and application layers only see these protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    ObservedInstanceState,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeStopEvidence,
)
from speechnexus_node_controller.domain.stop_checkpoint import StopCheckpoint
from speechnexus_node_controller.domain.templates import ApprovedRuntimeTemplate


class IntentStore(Protocol):
    """Durable intent facts; the adapter owns the transaction and row version."""

    def get(self, intent_id: str) -> RuntimeInstanceIntent | None: ...

    def add(self, intent: RuntimeInstanceIntent) -> None: ...

    def save(self, intent: RuntimeInstanceIntent, *, expected_version: int) -> None: ...

    def list_for_instance(self, instance_id: str) -> list[RuntimeInstanceIntent]: ...

    def list_unresolved(
        self, *, after_intent_id: str | None, limit: int
    ) -> list[RuntimeInstanceIntent]:
        """One bounded page in ascending intent ID order; terminal facts are excluded."""
        ...


class NodeRuntimePort(Protocol):
    """Node-local container runtime restricted to approved templates.

    Every call carries the recorded intent and its approved template so the
    adapter can evaluate the template's readiness rule and cannot widen what the
    recorded intent allowed.
    """

    def prepare(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation: ...

    def stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation: ...

    def inspect(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None: ...

    def recover_stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        """Resume only an accepted original stop, with durable target/exit checkpoints."""
        ...


class StopCheckpointStore(Protocol):
    """Private node evidence; commits finish before the next daemon action starts."""

    def get_stop_checkpoint(
        self, request: RuntimeInstanceIntentRequest
    ) -> StopCheckpoint | None: ...

    def save_stop_checkpoint(self, checkpoint: StopCheckpoint) -> StopCheckpoint:
        """Pin first; advance exit then release monotonically; retain original timestamps."""
        ...


@dataclass(frozen=True, slots=True)
class ReportedInstanceFact:
    """Caller assertions for diagnosis; stop evidence here has no runtime authority."""

    instance_id: str
    instance_generation: int
    approved_template_id: str
    config_digest: str
    observed_instance_state: ObservedInstanceState
    stop_evidence: RuntimeStopEvidence | None = None


@dataclass(frozen=True, slots=True)
class NodeReconcileSnapshot:
    """Diagnostic snapshot; its compatibility token never fences resource-pool intents."""

    controller_id: str
    fencing_token: int
    reported_at: datetime
    instances: tuple[ReportedInstanceFact, ...]
