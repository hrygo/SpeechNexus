"""Prepare and stop intents for approved runtime instances on one node.

The service is the controller side of ``internal/runtime-control``: it admits
only intents addressed to its own node and backed by an approved template, keys
idempotency on ``(node_id, intent_id)``, refuses a prepare generation that is
not newer than the persisted high water, refuses to start a replacement while the
previous generation is not provably stopped, and reports stop facts only from
verified observations. Admission accounting stays with the scheduler: this
service records intent and observation facts and never deducts a budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from speechnexus_node_controller.domain.intents import (
    GatewayDrainIncompleteError,
    InstanceHead,
    InstanceObservation,
    IntentConflictError,
    InvalidRuntimeRequestError,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
    StaleFencingTokenError,
    StaleInstanceGenerationError,
    TargetNodeMismatchError,
    UnapprovedRuntimeTemplateError,
)
from speechnexus_node_controller.domain.ports import (
    IntentStore,
    NodeReconcileSnapshot,
    NodeRuntimePort,
    ReportedInstanceFact,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ApprovedTemplateRegistry,
)


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    """Durable intent plus whether the caller replayed an existing intent."""

    intent: RuntimeInstanceIntent
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """Result of folding one node snapshot, mirroring the contract response."""

    node_id: str
    accepted_at: datetime
    quarantined_instance_ids: tuple[str, ...]


class NodeRuntimeControlService:
    """Owns the node-scoped intent path and its observation facts."""

    def __init__(
        self,
        *,
        node_id: str,
        templates: ApprovedTemplateRegistry,
        store: IntentStore,
        runtime: NodeRuntimePort,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._node_id = node_id
        self._templates = templates
        self._store = store
        self._runtime = runtime
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def node_id(self) -> str:
        return self._node_id

    def submit_intent(self, request: RuntimeInstanceIntentRequest) -> IntentOutcome:
        """Replay or admit one intent, then act outside the intent transaction."""
        existing = self._store.get(request.intent_id)
        if existing is not None:
            if existing.request != request:
                raise IntentConflictError(
                    "intent_id is already recorded with another canonical request"
                )
            return IntentOutcome(intent=existing, idempotent_replay=True)

        template = self._admit(request)
        at = self._now()
        intent = RuntimeInstanceIntent(request=request, created_at=at, updated_at=at)
        self._store.add(intent)
        try:
            observation = (
                self._runtime.prepare(request, template)
                if request.action is RuntimeActionKind.PREPARE
                else self._runtime.stop(request, template)
            )
        except RuntimeControllerUnavailableError:
            version = intent.version
            intent.mark_runtime_unavailable(at=self._now())
            self._store.save(intent, expected_version=version)
            raise

        version = intent.version
        self._apply_observation(intent, observation)
        self._store.save(intent, expected_version=version)
        return IntentOutcome(intent=intent, idempotent_replay=False)

    def get_intent(self, intent_id: str) -> RuntimeInstanceIntent | None:
        """Read one intent; reading never starts or stops anything."""
        return self._store.get(intent_id)

    def head(self, instance_id: str) -> InstanceHead:
        """Derived lifecycle head used by admission and reconcile."""
        return InstanceHead.derive(instance_id, self._store.list_for_instance(instance_id))

    def reconcile_instance(self, instance_id: str, instance_generation: int) -> list[IntentOutcome]:
        """Fold the current node observation into the unresolved intents of one generation."""
        outcomes: list[IntentOutcome] = []
        for intent in self._unresolved(instance_id, instance_generation):
            template = self._resolve_template(intent.request.approved_template_id)
            if template is None:
                continue
            observation = self._runtime.inspect(intent.request, template)
            if observation is None:
                continue
            version = intent.version
            self._apply_observation(intent, observation)
            self._store.save(intent, expected_version=version)
            outcomes.append(IntentOutcome(intent=intent, idempotent_replay=False))
        return outcomes

    def reconcile(self, snapshot: NodeReconcileSnapshot) -> ReconcileOutcome:
        """Fold a node snapshot; duplicate generations and missing evidence are refused."""
        if snapshot.controller_id != self._node_id:
            raise TargetNodeMismatchError(
                f"snapshot belongs to controller {snapshot.controller_id}, not {self._node_id}"
            )
        self._require_unique_generations(snapshot.instances)

        quarantined: list[str] = []
        for fact in snapshot.instances:
            unresolved = [
                intent
                for intent in self._store.list_for_instance(fact.instance_id)
                if intent.instance_generation == fact.instance_generation
                and intent.intent_state.is_unresolved
            ]
            if not unresolved:
                if fact.observed_instance_state is not ObservedInstanceState.ABSENT:
                    quarantined.append(fact.instance_id)
                continue
            for intent in unresolved:
                version = intent.version
                self._apply_observation(intent, self._observation_from(fact))
                self._store.save(intent, expected_version=version)
                if intent.intent_state is RuntimeIntentState.QUARANTINED:
                    quarantined.append(intent.instance_id)
        return ReconcileOutcome(
            node_id=self._node_id,
            accepted_at=snapshot.reported_at,
            quarantined_instance_ids=tuple(dict.fromkeys(quarantined)),
        )

    def _admit(self, request: RuntimeInstanceIntentRequest) -> ApprovedRuntimeTemplate:
        """Apply node scope, approved template, reservation and lifecycle gates."""
        if request.target_node_id != self._node_id:
            raise TargetNodeMismatchError(
                f"intent targets node {request.target_node_id}, not {self._node_id}"
            )
        template = self._templates.require(request.approved_template_id)
        if request.pool_id not in template.allowed_pool_ids:
            raise UnapprovedRuntimeTemplateError(
                f"template {template.template_id} is not approved for pool {request.pool_id}"
            )
        self._require_reservation_within_template(request, template)

        head = self.head(request.instance_id)
        if request.fencing_token < head.last_fencing_token:
            raise StaleFencingTokenError(
                f"fencing token {request.fencing_token} is below the observed "
                f"{head.last_fencing_token}",
                observed_fencing_token=head.last_fencing_token,
            )
        if request.action is RuntimeActionKind.PREPARE:
            high_water = head.generation_high_water
            if high_water is not None and request.instance_generation <= high_water:
                raise StaleInstanceGenerationError(
                    f"generation {request.instance_generation} is not above the persisted "
                    f"high water {high_water}",
                    observed_fencing_token=head.last_fencing_token,
                )
            if high_water is not None and not head.is_drained:
                raise GatewayDrainIncompleteError(
                    f"generation {head.current_generation} has not been stopped with evidence"
                )
        return template

    def _unresolved(
        self, instance_id: str, instance_generation: int
    ) -> list[RuntimeInstanceIntent]:
        """Unresolved intents of one instance generation, in store order."""
        return [
            intent
            for intent in self._store.list_for_instance(instance_id)
            if intent.instance_generation == instance_generation
            and intent.intent_state.is_unresolved
        ]

    def _resolve_template(self, template_id: str) -> ApprovedRuntimeTemplate | None:
        """Resolve a recorded template; a withdrawn template leaves the intent unresolved."""
        if not self._templates.contains(template_id):
            return None
        return self._templates.require(template_id)

    @staticmethod
    def _require_reservation_within_template(
        request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> None:
        reservation = request.reserved_memory
        if reservation.architecture is not template.memory_architecture:
            raise InvalidRuntimeRequestError(
                f"reservation architecture {reservation.architecture} does not match template "
                f"{template.memory_architecture}"
            )
        peak_bytes = sum(reservation.peak.active_counters(template.memory_architecture).values())
        if peak_bytes > template.max_peak_memory_bytes:
            raise InvalidRuntimeRequestError(
                f"peak reservation {peak_bytes} exceeds the approved template bound "
                f"{template.max_peak_memory_bytes}"
            )

    @staticmethod
    def _require_unique_generations(instances: tuple[ReportedInstanceFact, ...]) -> None:
        seen: set[tuple[str, int]] = set()
        for fact in instances:
            key = (fact.instance_id, fact.instance_generation)
            if key in seen:
                raise InvalidRuntimeRequestError(
                    f"snapshot reports {fact.instance_id} generation "
                    f"{fact.instance_generation} twice"
                )
            seen.add(key)

    def _apply_observation(
        self, intent: RuntimeInstanceIntent, observation: InstanceObservation
    ) -> None:
        """Fold an observation only when it belongs to the intent's own identity."""
        if (
            observation.instance_id != intent.instance_id
            or observation.instance_generation != intent.instance_generation
            or observation.approved_template_id != intent.request.approved_template_id
            or observation.config_digest != intent.request.config_digest
        ):
            intent.quarantine(RuntimeIntentErrorCode.OBSERVED_IDENTITY_MISMATCH, at=self._now())
            return
        intent.mark_observed(observation, at=self._now())

    @staticmethod
    def _observation_from(fact: ReportedInstanceFact) -> InstanceObservation:
        return InstanceObservation(
            instance_id=fact.instance_id,
            instance_generation=fact.instance_generation,
            approved_template_id=fact.approved_template_id,
            config_digest=fact.config_digest,
            observed_instance_state=fact.observed_instance_state,
            stop_evidence=fact.stop_evidence,
        )
