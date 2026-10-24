"""Node runtime and intent store doubles; they never touch a real container.

The builders below are the single source of synthetic runtime intents used by
unit and contract tests: every value is synthetic and no artifact, image, or
credential is referenced.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime

from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    MemoryArchitecture,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeMemoryReservation,
    RuntimeMemoryVector,
    RuntimeStopEvidence,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ReadinessKind,
    ReadinessRule,
    ReadOnlyBind,
)

GIB = 1024**3
FIXED_NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
SAMPLE_DIGEST = "sha256:" + "a" * 64
IMAGE_DIGEST = "sha256:" + "c" * 64
SAMPLE_IMAGE_REFERENCE = "local/synthetic-engine:0.1.0"
SAMPLE_NODE_ID = "node-local-1"
SAMPLE_INSTANCE_ID = "instance-asr-1"
SAMPLE_TEMPLATE_ID = "template-qwen3-asr-0p6b"


def build_reservation(
    *,
    architecture: MemoryArchitecture = MemoryArchitecture.UNIFIED_MEMORY,
    peak: RuntimeMemoryVector | None = None,
    steady: RuntimeMemoryVector | None = None,
) -> RuntimeMemoryReservation:
    return RuntimeMemoryReservation(
        architecture=architecture,
        peak=peak if peak is not None else RuntimeMemoryVector(unified_memory_bytes=10 * GIB),
        steady=steady if steady is not None else RuntimeMemoryVector(unified_memory_bytes=4 * GIB),
    )


def build_request(**overrides: object) -> RuntimeInstanceIntentRequest:
    """Build a synthetic prepare intent; override any field by keyword."""
    fields: dict[str, object] = {
        "intent_id": "intent-1",
        "target_node_id": SAMPLE_NODE_ID,
        "pool_id": "asr-batch",
        "gateway_id": "gateway-1",
        "instance_id": SAMPLE_INSTANCE_ID,
        "instance_generation": 1,
        "approved_template_id": SAMPLE_TEMPLATE_ID,
        "binding_revision_id": "binding-rev-1",
        "manifest_id": "manifest-1",
        "config_digest": SAMPLE_DIGEST,
        "action": RuntimeActionKind.PREPARE,
        "fencing_token": 1,
        "reserved_memory": build_reservation(),
        "deadline_at": datetime(2026, 10, 24, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return RuntimeInstanceIntentRequest(**fields)  # type: ignore[arg-type]


def build_template(
    *,
    template_id: str = SAMPLE_TEMPLATE_ID,
    architecture: MemoryArchitecture = MemoryArchitecture.UNIFIED_MEMORY,
    pools: frozenset[str] = frozenset({"asr-batch", "asr-realtime"}),
    max_peak_memory_bytes: int = 16 * GIB,
    container_memory_limit_bytes: int = GIB,
    image_reference: str = SAMPLE_IMAGE_REFERENCE,
    image_digest: str = IMAGE_DIGEST,
    readiness: ReadinessRule | None = None,
    command: tuple[str, ...] | None = None,
    gpu_device_count: int = 0,
    read_only_binds: tuple[ReadOnlyBind, ...] = (),
    network_name: str | None = None,
) -> ApprovedRuntimeTemplate:
    resolved_readiness = (
        readiness if readiness is not None else ReadinessRule(kind=ReadinessKind.PROCESS_RUNNING)
    )
    return ApprovedRuntimeTemplate(
        template_id=template_id,
        image_reference=image_reference,
        image_digest=image_digest,
        memory_architecture=architecture,
        allowed_pool_ids=pools,
        max_peak_memory_bytes=max_peak_memory_bytes,
        container_memory_limit_bytes=container_memory_limit_bytes,
        readiness=resolved_readiness,
        command=command,
        gpu_device_count=gpu_device_count,
        read_only_binds=read_only_binds,
        network_name=network_name,
    )


class InMemoryIntentStore:
    """Keeps copies so tests observe durable state instead of live objects."""

    def __init__(self) -> None:
        self._intents: dict[str, RuntimeInstanceIntent] = {}

    def get(self, intent_id: str) -> RuntimeInstanceIntent | None:
        stored = self._intents.get(intent_id)
        return copy.deepcopy(stored) if stored is not None else None

    def add(self, intent: RuntimeInstanceIntent) -> None:
        if intent.intent_id in self._intents:
            raise ValueError(f"runtime intent {intent.intent_id} already stored")
        self._intents[intent.intent_id] = copy.deepcopy(intent)

    def save(self, intent: RuntimeInstanceIntent, *, expected_version: int) -> None:
        stored = self._intents.get(intent.intent_id)
        if stored is None:
            raise ValueError(f"runtime intent {intent.intent_id} is not stored")
        if stored.version != expected_version:
            raise ValueError(f"runtime intent {intent.intent_id} version conflict")
        self._intents[intent.intent_id] = copy.deepcopy(intent)

    def list_for_instance(self, instance_id: str) -> list[RuntimeInstanceIntent]:
        return [
            copy.deepcopy(intent)
            for intent in self._intents.values()
            if intent.instance_id == instance_id
        ]

    def list_unresolved(
        self, *, after_intent_id: str | None, limit: int
    ) -> list[RuntimeInstanceIntent]:
        return [
            copy.deepcopy(intent)
            for key, intent in sorted(self._intents.items())
            if key > (after_intent_id or "") and intent.intent_state.is_unresolved
        ][:limit]


class ScriptedNodeRuntime:
    """Deterministic node runtime: prepare, stop and inspect are fully scripted."""

    def __init__(
        self,
        *,
        ready_on_prepare: bool = False,
        verify_stop: bool = True,
        peak_memory: RuntimeMemoryVector | None = None,
        exit_code: int | None = 0,
        runtime_container_id: str | None = "container-1",
    ) -> None:
        self.ready_on_prepare = ready_on_prepare
        self.verify_stop = verify_stop
        self.peak_memory = peak_memory
        self.exit_code = exit_code
        self.runtime_container_id = runtime_container_id
        self.unavailable = False
        self.prepared: list[RuntimeInstanceIntentRequest] = []
        self.stopped: list[RuntimeInstanceIntentRequest] = []
        self.inspections: list[tuple[str, int]] = []
        self._observations: dict[tuple[str, int], InstanceObservation] = {}

    def prepare(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        self._require_available()
        self.prepared.append(request)
        del template
        state = (
            ObservedInstanceState.READY if self.ready_on_prepare else ObservedInstanceState.LOADING
        )
        observation = self._build(request, state)
        self._observations[(request.instance_id, request.instance_generation)] = observation
        return observation

    def stop(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        self._require_available()
        self.stopped.append(request)
        del template
        evidence = (
            RuntimeStopEvidence(
                verified_at=datetime.now(UTC),
                exit_code=self.exit_code,
                runtime_container_id=self.runtime_container_id,
            )
            if self.verify_stop
            else None
        )
        observation = self._build(request, ObservedInstanceState.STOPPED, stop_evidence=evidence)
        self._observations[(request.instance_id, request.instance_generation)] = observation
        return observation

    def inspect(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        del template
        key = (request.instance_id, request.instance_generation)
        self.inspections.append(key)
        return self._observations.get(key)

    def observe_as(
        self,
        request: RuntimeInstanceIntentRequest,
        state: ObservedInstanceState,
        *,
        stop_evidence: RuntimeStopEvidence | None = None,
    ) -> InstanceObservation:
        """Force the state the next inspection of this identity reports."""
        observation = self._build(request, state, stop_evidence=stop_evidence)
        self._observations[(request.instance_id, request.instance_generation)] = observation
        return observation

    def mark_ready(self, request: RuntimeInstanceIntentRequest) -> InstanceObservation:
        return self.observe_as(request, ObservedInstanceState.READY)

    def _build(
        self,
        request: RuntimeInstanceIntentRequest,
        state: ObservedInstanceState,
        *,
        stop_evidence: RuntimeStopEvidence | None = None,
    ) -> InstanceObservation:
        return InstanceObservation(
            instance_id=request.instance_id,
            instance_generation=request.instance_generation,
            approved_template_id=request.approved_template_id,
            config_digest=request.config_digest,
            observed_instance_state=state,
            observed_peak_memory=self.peak_memory,
            stop_evidence=stop_evidence,
        )

    def _require_available(self) -> None:
        if self.unavailable:
            raise RuntimeControllerUnavailableError("node runtime did not answer")
