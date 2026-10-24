"""Caller snapshots cannot become persisted readiness, stop or fencing authority."""

from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    SAMPLE_INSTANCE_ID,
    SAMPLE_NODE_ID,
    ScriptedNodeRuntime,
    build_request,
    build_template,
)

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    GatewayDrainIncompleteError,
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeIntentState,
    RuntimeStopEvidence,
)
from speechnexus_node_controller.domain.payloads import intent_record_payload
from speechnexus_node_controller.domain.ports import NodeReconcileSnapshot, ReportedInstanceFact
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore


def _snapshot(
    state: ObservedInstanceState,
    *,
    evidence: RuntimeStopEvidence | None = None,
    token: int = 2,
    template_id: str | None = None,
) -> NodeReconcileSnapshot:
    request = build_request()
    return NodeReconcileSnapshot(
        controller_id=SAMPLE_NODE_ID,
        fencing_token=token,
        reported_at=FIXED_NOW + timedelta(days=365),
        instances=(
            ReportedInstanceFact(
                instance_id=request.instance_id,
                instance_generation=request.instance_generation,
                approved_template_id=template_id or request.approved_template_id,
                config_digest=request.config_digest,
                observed_instance_state=state,
                stop_evidence=evidence,
            ),
        ),
    )


def _service(store: SqliteIntentStore, runtime: ScriptedNodeRuntime) -> NodeRuntimeControlService:
    return NodeRuntimeControlService(
        node_id=SAMPLE_NODE_ID,
        templates=ApprovedTemplateRegistry((build_template(),)),
        store=store,
        runtime=runtime,
        now=lambda: FIXED_NOW,
    )


@pytest.mark.parametrize("state", list(ObservedInstanceState))
def test_snapshot_cannot_settle_or_rewrite_a_pending_prepare(
    tmp_path: Path, state: ObservedInstanceState
) -> None:
    runtime = ScriptedNodeRuntime()
    with closing(SqliteIntentStore(tmp_path / "intents.sqlite3")) as store:
        service = _service(store, runtime)
        original = service.submit_intent(build_request()).intent
        before = intent_record_payload(original)

        service.reconcile(_snapshot(state))

        persisted = store.get(original.intent_id)
        assert persisted is not None and intent_record_payload(persisted) == before
        assert persisted.memory_hold is MemoryHold.PEAK
        assert runtime.prepared == [original.request]
        assert runtime.stopped == [] and runtime.inspections == []
        assert service.submit_intent(original.request).idempotent_replay
        with pytest.raises(GatewayDrainIncompleteError):
            service.submit_intent(
                build_request(intent_id="replacement", instance_generation=2, fencing_token=3)
            )


@pytest.mark.parametrize("state", [ObservedInstanceState.ABSENT, ObservedInstanceState.STOPPED])
@pytest.mark.parametrize("evidence_present", [False, True])
def test_claimed_stop_proof_cannot_drain_a_generation_or_survive_as_a_fact(
    tmp_path: Path, state: ObservedInstanceState, evidence_present: bool
) -> None:
    path = tmp_path / "intents.sqlite3"
    runtime = ScriptedNodeRuntime(ready_on_prepare=True)
    request = build_request(intent_id="stop", action=RuntimeActionKind.STOP, fencing_token=2)
    proof = RuntimeStopEvidence(
        verified_at=FIXED_NOW + timedelta(days=365),
        exit_code=0,
        runtime_container_id="caller-invented-container",
    )
    with closing(SqliteIntentStore(path)) as store:
        service = _service(store, runtime)
        service.submit_intent(build_request())
        runtime.unavailable = True
        with pytest.raises(RuntimeControllerUnavailableError):
            service.submit_intent(request)
        before = store.list_for_instance(SAMPLE_INSTANCE_ID)

        outcome = service.reconcile(_snapshot(state, evidence=proof if evidence_present else None))

        assert outcome.quarantined_instance_ids == (SAMPLE_INSTANCE_ID,)
        assert outcome.accepted_at == FIXED_NOW
        assert store.list_for_instance(SAMPLE_INSTANCE_ID) == before
        assert not service.head(SAMPLE_INSTANCE_ID).is_drained
        assert store.get_stop_checkpoint(request) is None
        assert runtime.stopped == [] and runtime.inspections == []

    with closing(SqliteIntentStore(path)) as reopened:
        service = _service(reopened, runtime)
        assert reopened.list_for_instance(SAMPLE_INSTANCE_ID) == before
        original = service.submit_intent(request)
        assert original.idempotent_replay
        assert original.intent.intent_state is RuntimeIntentState.RECONCILING
        assert original.intent.stop_evidence is None
        with pytest.raises(GatewayDrainIncompleteError):
            service.submit_intent(
                build_request(intent_id="replacement", instance_generation=2, fencing_token=3)
            )

        # Only a subsequent node-local observation can settle the original stop.
        runtime.unavailable = False
        trusted = replace(proof, verified_at=FIXED_NOW, runtime_container_id="node-observed")
        runtime.observe_as(request, ObservedInstanceState.STOPPED, stop_evidence=trusted)
        recovered = service.reconcile_instance(SAMPLE_INSTANCE_ID, 1)
        assert recovered[0].intent.stop_evidence == trusted
        assert service.head(SAMPLE_INSTANCE_ID).is_drained


@pytest.mark.parametrize("token", [1, 10**12])
def test_snapshot_token_neither_fences_other_pools_nor_clears_identity_conflicts(
    tmp_path: Path, token: int
) -> None:
    runtime = ScriptedNodeRuntime()
    with closing(SqliteIntentStore(tmp_path / "intents.sqlite3")) as store:
        service = _service(store, runtime)
        original = service.submit_intent(build_request(fencing_token=50)).intent
        other_pool = build_request(
            intent_id="realtime-prepare",
            instance_id="instance-realtime",
            pool_id="asr-realtime",
            fencing_token=1,
        )
        service.submit_intent(other_pool)
        before = store.list_for_instance(SAMPLE_INSTANCE_ID)

        outcome = service.reconcile(
            _snapshot(ObservedInstanceState.READY, token=token, template_id="unapproved-template")
        )

        assert outcome.quarantined_instance_ids == (SAMPLE_INSTANCE_ID,)
        assert store.list_for_instance(SAMPLE_INSTANCE_ID) == before
        assert service.head(SAMPLE_INSTANCE_ID).last_fencing_token == 50
        assert service.head(other_pool.instance_id).last_fencing_token == 1
        stop = service.submit_intent(
            replace(other_pool, intent_id="realtime-stop", action=RuntimeActionKind.STOP)
        )
        assert stop.intent.intent_state is RuntimeIntentState.SUCCEEDED
        assert service.submit_intent(original.request).idempotent_replay
