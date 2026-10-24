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
from threading import Lock

from speechnexus_node_controller.domain.intents import (
    GatewayDrainIncompleteError,
    InstanceHead,
    InstanceObservation,
    IntentConflictError,
    InvalidRuntimeRequestError,
    MemoryArchitecture,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeControlRejectedError,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeIntentErrorCode,
    RuntimeIntentNotFoundError,
    RuntimeIntentState,
    StaleFencingTokenError,
    StaleInstanceGenerationError,
    TargetNodeMismatchError,
    UnapprovedRuntimeTemplateError,
    require_identifier,
)
from speechnexus_node_controller.domain.ports import (
    IntentStore,
    NodeReconcileSnapshot,
    NodeRuntimePort,
    ReportedInstanceFact,
    StopCheckpointStore,
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
    """Diagnostic discrepancies; neither quarantine nor caller facts are persisted."""

    node_id: str
    accepted_at: datetime
    quarantined_instance_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservationBatch:
    """One local observation page; the cursor is scheduling, never runtime authority."""

    after_intent_id: str | None
    examined: int
    updated: int
    skipped: int
    failed: int


class NodeRuntimeControlService:
    """Owns the node-scoped intent path and its observation facts."""

    def __init__(
        self,
        *,
        node_id: str,
        templates: ApprovedTemplateRegistry,
        store: IntentStore,
        runtime: NodeRuntimePort,
        stop_checkpoints: StopCheckpointStore | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._node_id = node_id
        self._templates = templates
        self._store = store
        self._runtime = runtime
        self._stop_checkpoints = stop_checkpoints
        self._now = now or (lambda: datetime.now(UTC))
        # One node's lifecycle actions and warmup inspections must not overlap.
        # HTTP reads remain independent; authoritative budgets stay with A.
        self._actions = Lock()

    @property
    def node_id(self) -> str:
        return self._node_id

    def submit_intent(self, request: RuntimeInstanceIntentRequest) -> IntentOutcome:
        """Replay or admit one intent, then act outside the intent transaction."""
        with self._actions:
            return self._submit_intent(request)

    def _submit_intent(self, request: RuntimeInstanceIntentRequest) -> IntentOutcome:
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

    def get_verified_stop(
        self, instance_id: str, instance_generation: int
    ) -> RuntimeInstanceIntent:
        """Read one original stop whose proof matches this node's released checkpoint."""
        require_identifier(instance_id, "instance_id")
        if type(instance_generation) is not int or not 1 <= instance_generation <= 2**53 - 1:
            raise InvalidRuntimeRequestError("instance_generation must be a safe positive integer")
        if self._stop_checkpoints is None:
            raise RuntimeControllerUnavailableError("the local stop proof store is not configured")
        verified: list[RuntimeInstanceIntent] = []
        for intent in self._store.list_for_instance(instance_id):
            if (
                intent.request.target_node_id != self._node_id
                or intent.instance_generation != instance_generation
                or intent.action is not RuntimeActionKind.STOP
                or intent.intent_state is not RuntimeIntentState.SUCCEEDED
            ):
                continue
            try:
                checkpoint = self._stop_checkpoints.get_stop_checkpoint(intent.request)
            except IntentConflictError, InvalidRuntimeRequestError:
                raise RuntimeControllerUnavailableError(
                    "the local stop proof is inconsistent"
                ) from None
            if checkpoint is None:
                continue  # A legacy success receipt cannot authenticate its own source.
            if (
                checkpoint.stage != "released"
                or checkpoint.evidence() != intent.stop_evidence
                or checkpoint.exited_at is None
                or checkpoint.released_at is None
                or not intent.created_at
                <= checkpoint.exited_at
                <= checkpoint.released_at
                <= intent.updated_at
            ):
                raise RuntimeControllerUnavailableError("the local stop proof is inconsistent")
            verified.append(intent)
        if len(verified) > 1:
            raise RuntimeControllerUnavailableError("the local stop proof has ambiguous sources")
        if not verified:
            raise RuntimeIntentNotFoundError("no checkpoint-backed stop proof is available")
        return verified[0]

    def head(self, instance_id: str) -> InstanceHead:
        """Derived lifecycle head used by admission and reconcile."""
        return InstanceHead.derive(instance_id, self._store.list_for_instance(instance_id))

    def reconcile_instance(self, instance_id: str, instance_generation: int) -> list[IntentOutcome]:
        """Fold the current node observation into the unresolved intents of one generation."""
        with self._actions:
            outcomes: list[IntentOutcome] = []
            for intent in self._unresolved(instance_id, instance_generation):
                outcome = self._inspect_intent(intent)
                if outcome is not None:
                    outcomes.append(outcome)
            return outcomes

    def observe_pending(
        self,
        *,
        after_intent_id: str | None = None,
        limit: int = 50,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> ObservationBatch:
        """Inspect a bounded page, skipping busy lifecycle actions and keeping failed facts."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise InvalidRuntimeRequestError("an observation batch requires a limit within 1..500")
        candidates = self._store.list_unresolved(after_intent_id=after_intent_id, limit=limit)
        cursor = after_intent_id if candidates else None
        examined = updated = skipped = failed = 0
        for candidate in candidates:
            if should_stop():
                break
            cursor = candidate.intent_id
            examined += 1
            if not self._actions.acquire(blocking=False):
                skipped += 1
                continue
            try:
                # Re-read after enumeration: another actor may already have settled it.
                current = self._store.get(candidate.intent_id)
                if (
                    current is None
                    or not current.intent_state.is_unresolved
                    or current.request.target_node_id != self._node_id
                    or self._inspect_intent(current) is None
                ):
                    skipped += 1
                else:
                    updated += 1
            except RuntimeControlRejectedError, ValueError:
                # Failed inspection or stale CAS is not an observation or release fact.
                failed += 1
            finally:
                self._actions.release()
        return ObservationBatch(cursor, examined, updated, skipped, failed)

    def _inspect_intent(self, intent: RuntimeInstanceIntent) -> IntentOutcome | None:
        template = self._resolve_template(intent.request.approved_template_id)
        if template is None:
            return None
        observation = (
            self._runtime.recover_stop(intent.request, template)
            if intent.action is RuntimeActionKind.STOP
            else self._runtime.inspect(intent.request, template)
        )
        if observation is None:
            return None
        version = intent.version
        self._apply_observation(intent, observation)
        self._store.save(intent, expected_version=version)
        return IntentOutcome(intent=intent, idempotent_replay=False)

    def reconcile(self, snapshot: NodeReconcileSnapshot) -> ReconcileOutcome:
        """Compare caller assertions without mutating facts or invoking the runtime."""
        with self._actions:
            return self._reconcile(snapshot)

    def _reconcile(self, snapshot: NodeReconcileSnapshot) -> ReconcileOutcome:
        if snapshot.controller_id != self._node_id:
            raise TargetNodeMismatchError(
                f"snapshot belongs to controller {snapshot.controller_id}, not {self._node_id}"
            )
        self._require_unique_generations(snapshot.instances)

        quarantined: list[str] = []
        for fact in snapshot.instances:
            self._report_snapshot_discrepancy(fact, quarantined)
        return ReconcileOutcome(
            node_id=self._node_id,
            accepted_at=self._now(),
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

    def _report_snapshot_discrepancy(
        self, fact: ReportedInstanceFact, quarantined: list[str]
    ) -> None:
        """Report identity/presence discrepancies, never accept caller stop proof.

        Unresolved claims still hold resources. Neither a caller's absence nor
        its complete-looking proof can drain them. A verified local stop takes
        precedence over an older unresolved prepare for the same generation.
        """
        recorded = [
            intent
            for intent in self._store.list_for_instance(fact.instance_id)
            if intent.instance_generation == fact.instance_generation
        ]
        observed_gone = fact.observed_instance_state in {
            ObservedInstanceState.ABSENT,
            ObservedInstanceState.STOPPED,
        }
        if not recorded:
            if not observed_gone:
                quarantined.append(fact.instance_id)
            return
        if any(
            intent.request.approved_template_id != fact.approved_template_id
            or intent.request.config_digest != fact.config_digest
            for intent in recorded
        ):
            quarantined.append(fact.instance_id)
            return
        verified_stopped = any(
            intent.action is RuntimeActionKind.STOP
            and intent.intent_state is RuntimeIntentState.SUCCEEDED
            for intent in recorded
        )
        holds_live = (
            any(
                intent.intent_state.is_unresolved
                or (
                    intent.action is RuntimeActionKind.PREPARE
                    and intent.intent_state is RuntimeIntentState.SUCCEEDED
                )
                for intent in recorded
            )
            and not verified_stopped
        )
        # Contradiction in either direction: vanished while still held live, or
        # present although no succeeded prepare still holds that generation.
        if (observed_gone and holds_live) or (not observed_gone and not holds_live):
            quarantined.append(fact.instance_id)

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
        container_peak = (
            reservation.peak.unified_memory_bytes
            if reservation.architecture is MemoryArchitecture.UNIFIED_MEMORY
            else reservation.peak.host_memory_bytes
        )
        if container_peak < template.container_memory_limit_bytes:
            raise InvalidRuntimeRequestError(
                "peak host/unified reservation must cover the approved container memory limit"
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
