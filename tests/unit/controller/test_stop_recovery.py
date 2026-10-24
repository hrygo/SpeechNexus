"""Crash boundaries use the real controller and reopened SQLite with a fake daemon."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    IMAGE_DIGEST,
    SAMPLE_IMAGE_REFERENCE,
    build_request,
    build_template,
)
from tests.fake.docker_api import FakeDockerEngine

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.stop_checkpoint import StopCheckpoint
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore

PREPARE = build_request()
STOP = replace(PREPARE, intent_id="intent-stop", action=RuntimeActionKind.STOP, fencing_token=2)
TEMPLATE = build_template()


class SimulatedCrash(BaseException):
    """Bypass application error handling, as process death would."""


class CrashingDaemon(FakeDockerEngine):
    fault: str | None = None

    def stop_container(self, container_id: str, *, timeout_seconds: float) -> None:
        if self.fault == "before_stop":
            raise SimulatedCrash
        super().stop_container(container_id, timeout_seconds=timeout_seconds)

    def wait_container(self, container_id: str, *, timeout_seconds: float) -> int:
        result = super().wait_container(container_id, timeout_seconds=timeout_seconds)
        if self.fault == "after_exit":
            raise SimulatedCrash
        return result

    def remove_container(self, container_id: str) -> None:
        if self.fault == "before_remove":
            raise SimulatedCrash
        super().remove_container(container_id)
        if self.fault == "after_remove":
            raise SimulatedCrash


class ControlledStore(SqliteIntentStore):
    crash_before_receipt = False
    fail_stage: str | None = None

    def save(self, intent: RuntimeInstanceIntent, *, expected_version: int) -> None:
        if (
            self.crash_before_receipt
            and intent.action is RuntimeActionKind.STOP
            and intent.intent_state is RuntimeIntentState.SUCCEEDED
        ):
            raise SimulatedCrash
        super().save(intent, expected_version=expected_version)

    def save_stop_checkpoint(self, checkpoint: StopCheckpoint) -> StopCheckpoint:
        if checkpoint.stage == self.fail_stage:
            raise RuntimeControllerUnavailableError("synthetic local storage unavailable")
        return super().save_stop_checkpoint(checkpoint)


def daemon() -> CrashingDaemon:
    return CrashingDaemon(images={SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST}, exit_code=143)


def service(
    store: SqliteIntentStore, engine: FakeDockerEngine
) -> tuple[NodeRuntimeControlService, DockerNodeRuntime]:
    runtime = DockerNodeRuntime(api=engine, node_id=PREPARE.target_node_id, stop_checkpoints=store)
    return (
        NodeRuntimeControlService(
            node_id=PREPARE.target_node_id,
            templates=ApprovedTemplateRegistry((TEMPLATE,)),
            store=store,
            runtime=runtime,
            now=lambda: FIXED_NOW,
        ),
        runtime,
    )


@pytest.mark.parametrize(
    "fault",
    ("before_stop", "after_exit", "before_remove", "after_remove", "before_receipt"),
)
def test_restart_resumes_the_original_stop_at_every_destructive_boundary(
    tmp_path: Path, fault: str
) -> None:
    path = tmp_path / "intents.sqlite3"
    first, engine = ControlledStore(path), daemon()
    original, _ = service(first, engine)
    original.submit_intent(PREPARE)
    first.crash_before_receipt = fault == "before_receipt"
    engine.fault = fault
    with pytest.raises(SimulatedCrash):
        original.submit_intent(STOP)
    checkpoint = first.get_stop_checkpoint(STOP)
    assert checkpoint is not None
    assert checkpoint.stage == (
        "targeted"
        if fault in {"before_stop", "after_exit"}
        else "released"
        if fault == "before_receipt"
        else "exited"
    )
    pending = first.get(STOP.intent_id)
    assert pending is not None and pending.intent_state is RuntimeIntentState.PENDING
    first.close()

    engine.fault = None
    reopened = SqliteIntentStore(path)
    recovered, runtime = service(reopened, engine)
    try:
        # Reads are still passive; the background/manual observation producer owns recovery.
        assert recovered.get_intent(STOP.intent_id) == pending
        report = recovered.observe_pending()
        assert report.updated == 1 and report.failed == 0
        completed = reopened.get(STOP.intent_id)
        assert completed is not None and completed.intent_state is RuntimeIntentState.SUCCEEDED
        assert completed.stop_evidence is not None
        assert completed.stop_evidence.exit_code == 143
        assert completed.stop_evidence.runtime_container_id == engine.created[0].container_id
        assert not engine.containers and len(engine.created) == 1
        assert engine.stopped == [engine.created[0].container_id]
        assert engine.removed == [engine.created[0].container_id]
        # A repeated original stop or recovery cannot create a new timestamp or daemon mutation.
        stops, removals = engine.stopped[:], engine.removed[:]
        repeated = runtime.recover_stop(STOP, TEMPLATE)
        assert repeated.stop_evidence == completed.stop_evidence
        assert recovered.submit_intent(STOP).idempotent_replay
        assert engine.stopped == stops and engine.removed == removals
    finally:
        reopened.close()


@pytest.mark.parametrize("stage", ("targeted", "exited", "released"))
def test_failed_checkpoint_commit_cannot_skip_its_destructive_barrier(
    tmp_path: Path, stage: str
) -> None:
    store, engine = ControlledStore(tmp_path / "intents.sqlite3"), daemon()
    controller, _ = service(store, engine)
    try:
        controller.submit_intent(PREPARE)
        store.fail_stage = stage
        with pytest.raises(RuntimeControllerUnavailableError):
            controller.submit_intent(STOP)
        if stage == "targeted":
            assert engine.stopped == [] and engine.removed == []
        elif stage == "exited":
            assert len(engine.stopped) == 1 and engine.removed == []
        else:
            assert len(engine.removed) == 1 and not engine.containers
        pending = store.get(STOP.intent_id)
        assert pending is not None and pending.stop_evidence is None
        store.fail_stage = None
        assert controller.observe_pending().updated == 1
        completed = store.get(STOP.intent_id)
        assert completed is not None and completed.stop_evidence is not None
        assert completed.intent_state is RuntimeIntentState.SUCCEEDED
    finally:
        store.close()


def crashed_target(
    path: Path, *, fault: str
) -> tuple[SqliteIntentStore, CrashingDaemon, NodeRuntimeControlService, DockerNodeRuntime]:
    first, engine = ControlledStore(path), daemon()
    original, _ = service(first, engine)
    original.submit_intent(PREPARE)
    engine.fault = fault
    with pytest.raises(SimulatedCrash):
        original.submit_intent(STOP)
    first.close()
    engine.fault = None
    reopened = SqliteIntentStore(path)
    controller, runtime = service(reopened, engine)
    return reopened, engine, controller, runtime


def test_a_missing_target_without_persisted_exit_remains_quarantined(tmp_path: Path) -> None:
    store, engine, controller, _ = crashed_target(tmp_path / "intents.sqlite3", fault="before_stop")
    try:
        engine.containers.clear()  # External disappearance is not an exit confirmation.
        assert controller.observe_pending().updated == 1
        pending = store.get(STOP.intent_id)
        assert pending is not None and pending.intent_state is RuntimeIntentState.QUARANTINED
        assert pending.stop_evidence is None
        checkpoint = store.get_stop_checkpoint(STOP)
        assert checkpoint is not None and checkpoint.stage == "targeted"
        assert engine.stopped == [] and engine.removed == []
    finally:
        store.close()


@pytest.mark.parametrize("same_generation", (True, False))
def test_recovery_never_stops_a_replacement_container(
    tmp_path: Path, same_generation: bool
) -> None:
    store, engine, controller, runtime = crashed_target(
        tmp_path / "intents.sqlite3", fault="after_remove"
    )
    try:
        replacement = replace(
            PREPARE,
            intent_id="replacement-prepare",
            instance_generation=1 if same_generation else 2,
            fencing_token=3,
        )
        runtime.prepare(replacement, TEMPLATE)
        successor = engine.created[-1]
        original_stops, original_removes = engine.stopped[:], engine.removed[:]
        assert controller.observe_pending().updated == 1
        fact = store.get(STOP.intent_id)
        assert fact is not None
        if same_generation:
            assert fact.intent_state is RuntimeIntentState.QUARANTINED
            assert fact.stop_evidence is None
        else:
            assert fact.intent_state is RuntimeIntentState.SUCCEEDED
            assert fact.stop_evidence is not None
            assert fact.stop_evidence.runtime_container_id != successor.container_id
        assert engine.containers[successor.container_id].running
        assert engine.stopped == original_stops and engine.removed == original_removes
    finally:
        store.close()


def test_a_restarted_pinned_container_invalidates_the_old_exit_for_recovery(tmp_path: Path) -> None:
    store, engine, controller, _ = crashed_target(
        tmp_path / "intents.sqlite3", fault="before_remove"
    )
    try:
        engine.start_container(engine.created[0].container_id)
        assert controller.observe_pending().updated == 1
        pending = store.get(STOP.intent_id)
        assert pending is not None and pending.intent_state is RuntimeIntentState.QUARANTINED
        assert pending.stop_evidence is None and engine.removed == []
        assert engine.containers[engine.created[0].container_id].running
    finally:
        store.close()


def test_inspect_alone_never_replays_a_stop_or_uses_a_checkpoint_as_its_own_proof(
    tmp_path: Path,
) -> None:
    store, engine, _, runtime = crashed_target(tmp_path / "intents.sqlite3", fault="after_remove")
    try:
        current = store.get_stop_checkpoint(STOP)
        assert current is not None
        mutations = engine.stopped[:], engine.removed[:]
        observation = runtime.inspect(STOP, TEMPLATE)
        assert observation is not None
        assert observation.observed_instance_state is ObservedInstanceState.ABSENT
        assert observation.stop_evidence is None
        assert store.get_stop_checkpoint(STOP) == current and current.stage == "exited"
        assert (engine.stopped, engine.removed) == mutations
    finally:
        store.close()


def test_a_lost_receipt_cas_preserves_the_winner_and_the_committed_stop_proof(
    tmp_path: Path,
) -> None:
    path = tmp_path / "intents.sqlite3"
    original, engine, _, _ = crashed_target(path, fault="after_remove")
    original.close()
    competing = SqliteIntentStore(path)

    class RacingReceipt(SqliteIntentStore):
        race = True

        def save(self, intent: RuntimeInstanceIntent, *, expected_version: int) -> None:
            if self.race and intent.action is RuntimeActionKind.STOP:
                self.race = False
                winner = competing.get(STOP.intent_id)
                assert winner is not None
                version = winner.version
                winner.quarantine(RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE, at=FIXED_NOW)
                competing.save(winner, expected_version=version)
            super().save(intent, expected_version=expected_version)

    store = RacingReceipt(path)
    controller, _ = service(store, engine)
    try:
        report = controller.observe_pending()
        assert report.failed == 1 and report.updated == 0
        winner = store.get(STOP.intent_id)
        assert winner is not None and winner.intent_state is RuntimeIntentState.QUARANTINED
        assert winner.error_code is RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE
        checkpoint = store.get_stop_checkpoint(STOP)
        assert checkpoint is not None and checkpoint.stage == "released"
        assert controller.observe_pending().updated == 1
        completed = store.get(STOP.intent_id)
        assert completed is not None and completed.stop_evidence == checkpoint.evidence()
        assert len(engine.stopped) == len(engine.removed) == 1
    finally:
        store.close()
        competing.close()
