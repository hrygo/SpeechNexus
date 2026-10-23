"""Node controller admission, idempotency, drain gate and verify-before-release."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    GIB,
    SAMPLE_DIGEST,
    SAMPLE_INSTANCE_ID,
    SAMPLE_NODE_ID,
    SAMPLE_TEMPLATE_ID,
    InMemoryIntentStore,
    ScriptedNodeRuntime,
    build_request,
    build_reservation,
    build_template,
)

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    GatewayDrainIncompleteError,
    InstanceObservation,
    IntentConflictError,
    InvalidRuntimeRequestError,
    MemoryArchitecture,
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntentRequest,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
    RuntimeMemoryReservation,
    RuntimeMemoryVector,
    StaleFencingTokenError,
    StaleInstanceGenerationError,
    TargetNodeMismatchError,
    UnapprovedRuntimeTemplateError,
)
from speechnexus_node_controller.domain.ports import (
    NodeReconcileSnapshot,
    ReportedInstanceFact,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ApprovedTemplateRegistry,
)


def _service(
    *,
    runtime: ScriptedNodeRuntime | None = None,
    store: InMemoryIntentStore | None = None,
    templates: tuple[ApprovedRuntimeTemplate, ...] | None = None,
) -> tuple[NodeRuntimeControlService, ScriptedNodeRuntime, InMemoryIntentStore]:
    node_runtime = runtime if runtime is not None else ScriptedNodeRuntime()
    intent_store = store if store is not None else InMemoryIntentStore()
    service = NodeRuntimeControlService(
        node_id=SAMPLE_NODE_ID,
        templates=ApprovedTemplateRegistry(templates or (build_template(),)),
        store=intent_store,
        runtime=node_runtime,
        now=lambda: FIXED_NOW,
    )
    return service, node_runtime, intent_store


def test_prepare_for_another_node_is_refused() -> None:
    service, runtime, store = _service()
    with pytest.raises(TargetNodeMismatchError):
        service.submit_intent(build_request(target_node_id="node-local-2"))
    assert runtime.prepared == []
    assert store.get("intent-1") is None


def test_unapproved_template_is_refused() -> None:
    service, runtime, _ = _service()
    with pytest.raises(UnapprovedRuntimeTemplateError):
        service.submit_intent(build_request(approved_template_id="template-unvetted"))
    assert runtime.prepared == []


def test_pool_outside_the_template_is_refused() -> None:
    service, runtime, _ = _service()
    with pytest.raises(UnapprovedRuntimeTemplateError):
        service.submit_intent(build_request(pool_id="tts-batch"))
    assert runtime.prepared == []


def test_reservation_architecture_must_match_the_template() -> None:
    service, runtime, _ = _service(
        runtime=ScriptedNodeRuntime(),
        templates=(
            build_template(
                architecture=MemoryArchitecture.DISCRETE_DEVICE_MEMORY,
            ),
        ),
    )
    with pytest.raises(InvalidRuntimeRequestError, match="reservation architecture"):
        service.submit_intent(build_request())
    assert runtime.prepared == []


def test_peak_reservation_above_the_template_bound_is_refused() -> None:
    service, runtime, _ = _service(
        templates=(build_template(max_peak_memory_bytes=8 * GIB),),
    )
    with pytest.raises(InvalidRuntimeRequestError, match="exceeds the approved template bound"):
        service.submit_intent(build_request())
    assert runtime.prepared == []


def test_first_prepare_keeps_peak_until_readiness_is_observed() -> None:
    service, runtime, _ = _service()
    outcome = service.submit_intent(build_request())
    assert outcome.idempotent_replay is False
    assert outcome.intent.intent_state is RuntimeIntentState.RECONCILING
    assert outcome.intent.observed_instance_state is ObservedInstanceState.LOADING
    assert outcome.intent.memory_hold is MemoryHold.PEAK
    assert runtime.prepared == [build_request()]


def test_readiness_switches_the_reported_hold_to_steady() -> None:
    runtime = ScriptedNodeRuntime(
        ready_on_prepare=True,
        peak_memory=RuntimeMemoryVector(unified_memory_bytes=9 * GIB),
    )
    service, _, _ = _service(runtime=runtime)
    outcome = service.submit_intent(build_request())
    assert outcome.intent.intent_state is RuntimeIntentState.SUCCEEDED
    assert outcome.intent.observed_instance_state is ObservedInstanceState.READY
    assert outcome.intent.memory_hold is MemoryHold.STEADY
    assert outcome.intent.observed_peak_memory == RuntimeMemoryVector(unified_memory_bytes=9 * GIB)


def test_replaying_the_same_intent_never_prepares_twice() -> None:
    service, runtime, _ = _service()
    service.submit_intent(build_request())
    replay = service.submit_intent(build_request())
    assert replay.idempotent_replay is True
    assert len(runtime.prepared) == 1


def test_same_intent_id_with_another_request_conflicts() -> None:
    service, runtime, _ = _service()
    service.submit_intent(build_request())
    with pytest.raises(IntentConflictError):
        service.submit_intent(replace(build_request(), config_digest="sha256:" + "b" * 64))
    assert len(runtime.prepared) == 1


def test_new_intent_for_an_already_claimed_generation_is_stale() -> None:
    service, _, _ = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))
    service.submit_intent(build_request())
    with pytest.raises(StaleInstanceGenerationError, match="high water"):
        service.submit_intent(build_request(intent_id="intent-2", fencing_token=2))


def test_lower_fencing_token_is_refused_and_reports_the_observed_token() -> None:
    service, _, _ = _service()
    service.submit_intent(build_request(fencing_token=5))
    with pytest.raises(StaleFencingTokenError) as raised:
        service.submit_intent(
            build_request(
                intent_id="intent-stop-1",
                action=RuntimeActionKind.STOP,
                fencing_token=3,
            )
        )
    assert raised.value.observed_fencing_token == 5


def test_replacement_generation_waits_for_a_verified_stop() -> None:
    service, runtime, _ = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))
    service.submit_intent(build_request())
    with pytest.raises(GatewayDrainIncompleteError):
        service.submit_intent(
            build_request(intent_id="intent-2", instance_generation=2, fencing_token=2)
        )

    service.submit_intent(
        build_request(intent_id="intent-stop-1", action=RuntimeActionKind.STOP, fencing_token=3)
    )
    replacement = service.submit_intent(
        build_request(intent_id="intent-2", instance_generation=2, fencing_token=4)
    )
    assert replacement.intent.intent_state is RuntimeIntentState.SUCCEEDED
    assert [request.instance_generation for request in runtime.prepared] == [1, 2]


def test_unverified_stop_is_quarantined_and_still_reports_peak() -> None:
    service, _, store = _service(runtime=ScriptedNodeRuntime(verify_stop=False))
    service.submit_intent(build_request())
    stopped = service.submit_intent(
        build_request(intent_id="intent-stop-1", action=RuntimeActionKind.STOP, fencing_token=2)
    )
    assert stopped.intent.intent_state is RuntimeIntentState.QUARANTINED
    assert stopped.intent.error_code is RuntimeIntentErrorCode.STOP_EVIDENCE_UNVERIFIED
    assert stopped.intent.memory_hold is MemoryHold.PEAK
    assert stopped.intent.stop_evidence is None
    assert store.get("intent-stop-1") == stopped.intent


def test_verified_stop_releases_the_reported_hold() -> None:
    service, _, _ = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))
    service.submit_intent(build_request())
    stopped = service.submit_intent(
        build_request(intent_id="intent-stop-1", action=RuntimeActionKind.STOP, fencing_token=2)
    )
    assert stopped.intent.intent_state is RuntimeIntentState.SUCCEEDED
    assert stopped.intent.memory_hold is MemoryHold.NONE
    assert stopped.intent.stop_evidence is not None
    assert stopped.intent.stop_evidence.exit_code == 0


def test_late_stop_for_a_previous_generation_leaves_the_replacement_alone() -> None:
    service, _, store = _service(runtime=ScriptedNodeRuntime(ready_on_prepare=True))
    service.submit_intent(build_request())
    service.submit_intent(
        build_request(intent_id="intent-stop-1", action=RuntimeActionKind.STOP, fencing_token=2)
    )
    replacement = service.submit_intent(
        build_request(intent_id="intent-2", instance_generation=2, fencing_token=3)
    )

    late = service.submit_intent(
        build_request(
            intent_id="intent-stop-late",
            action=RuntimeActionKind.STOP,
            instance_generation=1,
            fencing_token=4,
        )
    )
    assert late.intent.intent_state is RuntimeIntentState.SUCCEEDED
    assert late.intent.instance_generation == 1
    assert store.get("intent-2") == replacement.intent
    head = service.head(SAMPLE_INSTANCE_ID)
    assert head.current_generation == 2
    assert head.stopped_generations == frozenset({1})


def test_unavailable_node_runtime_keeps_the_intent_unresolved() -> None:
    runtime = ScriptedNodeRuntime()
    runtime.unavailable = True
    service, _, store = _service(runtime=runtime)
    with pytest.raises(RuntimeControllerUnavailableError):
        service.submit_intent(build_request())

    stored = store.get("intent-1")
    assert stored is not None
    assert stored.intent_state is RuntimeIntentState.RECONCILING
    assert stored.error_code is RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE
    assert stored.memory_hold is MemoryHold.PEAK

    runtime.unavailable = False
    replay = service.submit_intent(build_request())
    assert replay.idempotent_replay is True
    assert runtime.prepared == []


def test_reconcile_instance_adopts_a_later_ready_observation() -> None:
    service, runtime, _ = _service()
    service.submit_intent(build_request())
    runtime.mark_ready(build_request())
    outcomes = service.reconcile_instance(SAMPLE_INSTANCE_ID, 1)
    assert len(outcomes) == 1
    assert outcomes[0].intent.intent_state is RuntimeIntentState.SUCCEEDED
    assert outcomes[0].intent.memory_hold is MemoryHold.STEADY


def test_observation_for_another_generation_is_quarantined() -> None:
    class MisreportingRuntime(ScriptedNodeRuntime):
        def inspect(
            self,
            request: RuntimeInstanceIntentRequest,
            template: ApprovedRuntimeTemplate,
        ) -> InstanceObservation | None:
            observation = super().inspect(request, template)
            if observation is None:
                return None
            return replace(observation, instance_generation=request.instance_generation + 1)

    service, runtime, _ = _service(runtime=MisreportingRuntime())
    service.submit_intent(build_request())
    runtime.mark_ready(build_request())
    outcomes = service.reconcile_instance(SAMPLE_INSTANCE_ID, 1)
    assert outcomes[0].intent.intent_state is RuntimeIntentState.QUARANTINED
    assert outcomes[0].intent.error_code is RuntimeIntentErrorCode.OBSERVED_IDENTITY_MISMATCH


def test_reconcile_snapshot_requires_its_own_controller_identity() -> None:
    service, _, _ = _service()
    snapshot = NodeReconcileSnapshot(
        controller_id="node-controller-2",
        fencing_token=1,
        reported_at=FIXED_NOW,
        instances=(),
    )
    with pytest.raises(TargetNodeMismatchError):
        service.reconcile(snapshot)


def test_reconcile_snapshot_rejects_a_duplicated_generation() -> None:
    service, _, _ = _service()
    fact = ReportedInstanceFact(
        instance_id=SAMPLE_INSTANCE_ID,
        instance_generation=1,
        approved_template_id=SAMPLE_TEMPLATE_ID,
        config_digest=SAMPLE_DIGEST,
        observed_instance_state=ObservedInstanceState.LOADING,
    )
    snapshot = NodeReconcileSnapshot(
        controller_id=SAMPLE_NODE_ID,
        fencing_token=1,
        reported_at=FIXED_NOW,
        instances=(fact, fact),
    )
    with pytest.raises(InvalidRuntimeRequestError, match="generation 1 twice"):
        service.reconcile(snapshot)


def test_reconcile_reports_stopped_without_evidence_as_quarantined() -> None:
    runtime = ScriptedNodeRuntime()
    runtime.unavailable = True
    service, _, _ = _service(runtime=runtime)
    with pytest.raises(RuntimeControllerUnavailableError):
        service.submit_intent(build_request())
    with pytest.raises(RuntimeControllerUnavailableError):
        service.submit_intent(
            build_request(intent_id="intent-stop-1", action=RuntimeActionKind.STOP, fencing_token=2)
        )

    snapshot = NodeReconcileSnapshot(
        controller_id=SAMPLE_NODE_ID,
        fencing_token=2,
        reported_at=datetime(2026, 10, 24, 9, 5, tzinfo=UTC),
        instances=(
            ReportedInstanceFact(
                instance_id=SAMPLE_INSTANCE_ID,
                instance_generation=1,
                approved_template_id=SAMPLE_TEMPLATE_ID,
                config_digest=SAMPLE_DIGEST,
                observed_instance_state=ObservedInstanceState.STOPPED,
            ),
        ),
    )
    outcome = service.reconcile(snapshot)
    assert outcome.quarantined_instance_ids == (SAMPLE_INSTANCE_ID,)
    assert outcome.node_id == SAMPLE_NODE_ID


def test_reconcile_reports_an_untracked_instance_without_adopting_it() -> None:
    service, _, store = _service()
    snapshot = NodeReconcileSnapshot(
        controller_id=SAMPLE_NODE_ID,
        fencing_token=7,
        reported_at=FIXED_NOW,
        instances=(
            ReportedInstanceFact(
                instance_id="instance-rogue-1",
                instance_generation=1,
                approved_template_id=SAMPLE_TEMPLATE_ID,
                config_digest=SAMPLE_DIGEST,
                observed_instance_state=ObservedInstanceState.READY,
            ),
        ),
    )
    outcome = service.reconcile(snapshot)
    assert outcome.quarantined_instance_ids == ("instance-rogue-1",)
    assert store.list_for_instance("instance-rogue-1") == []


def test_absent_fact_for_an_untracked_instance_is_not_quarantined() -> None:
    service, _, _ = _service()
    snapshot = NodeReconcileSnapshot(
        controller_id=SAMPLE_NODE_ID,
        fencing_token=7,
        reported_at=FIXED_NOW,
        instances=(
            ReportedInstanceFact(
                instance_id="instance-gone-1",
                instance_generation=3,
                approved_template_id=SAMPLE_TEMPLATE_ID,
                config_digest=SAMPLE_DIGEST,
                observed_instance_state=ObservedInstanceState.ABSENT,
            ),
        ),
    )
    assert service.reconcile(snapshot).quarantined_instance_ids == ()


def test_reservation_of_a_stop_intent_is_still_validated() -> None:
    service, runtime, _ = _service()
    with pytest.raises(InvalidRuntimeRequestError, match="peak unified_memory_bytes"):
        service.submit_intent(
            build_request(
                intent_id="intent-stop-1",
                action=RuntimeActionKind.STOP,
                reserved_memory=RuntimeMemoryReservation(
                    architecture=MemoryArchitecture.UNIFIED_MEMORY,
                    peak=RuntimeMemoryVector(unified_memory_bytes=1 * GIB),
                    steady=RuntimeMemoryVector(unified_memory_bytes=2 * GIB),
                ),
            )
        )
    assert runtime.stopped == []


def test_request_helper_defaults_stay_aligned_with_the_service() -> None:
    """Guard the helper the other assertions depend on."""
    request: RuntimeInstanceIntentRequest = build_request()
    assert request.reserved_memory == build_reservation()
    assert request.target_node_id == SAMPLE_NODE_ID
