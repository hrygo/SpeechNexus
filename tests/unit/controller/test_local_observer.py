"""The node produces observations itself; HTTP reads and missing proof cannot do so."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    SAMPLE_NODE_ID,
    InMemoryIntentStore,
    ScriptedNodeRuntime,
    build_request,
    build_template,
)

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    InvalidRuntimeRequestError,
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControllerUnavailableError,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.ports import IntentStore
from speechnexus_node_controller.domain.templates import (
    ApprovedRuntimeTemplate,
    ApprovedTemplateRegistry,
)
from speechnexus_node_controller.infrastructure.local_observer import LocalObservationLoop
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore


def service_for(
    store: IntentStore, runtime: ScriptedNodeRuntime, *, approved: bool = True
) -> NodeRuntimeControlService:
    return NodeRuntimeControlService(
        node_id=SAMPLE_NODE_ID,
        templates=ApprovedTemplateRegistry((build_template(),) if approved else ()),
        store=store,
        runtime=runtime,
        now=lambda: FIXED_NOW,
    )


def wait_ready(store: IntentStore, intent_id: str = "intent-1") -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        intent = store.get(intent_id)
        if intent is not None and intent.intent_state is RuntimeIntentState.SUCCEEDED:
            return
        time.sleep(0.01)
    pytest.fail("the local observer did not record its own ready observation")


def test_pending_observation_advances_without_repeating_a_lifecycle_action() -> None:
    store, runtime = InMemoryIntentStore(), ScriptedNodeRuntime()
    service = service_for(store, runtime)
    request = build_request()
    service.submit_intent(request)
    runtime.mark_ready(request)

    report = service.observe_pending()

    assert (report.examined, report.updated, report.skipped, report.failed) == (1, 1, 0, 0)
    intent = service.get_intent(request.intent_id)
    assert intent is not None and intent.observed_instance_state is ObservedInstanceState.READY
    assert len(runtime.prepared) == 1 and runtime.stopped == []
    assert service.observe_pending().examined == 0


def test_pagination_eventually_visits_ready_work_after_permanently_loading_work() -> None:
    store, runtime = InMemoryIntentStore(), ScriptedNodeRuntime()
    service = service_for(store, runtime)
    requests = [
        build_request(intent_id=f"intent-{index}", instance_id=f"instance-{index}")
        for index in range(3)
    ]
    for request in reversed(requests):
        service.submit_intent(request)
    runtime.mark_ready(requests[2])
    cursor = None
    for index in range(3):
        report = service.observe_pending(after_intent_id=cursor, limit=1)
        assert report.after_intent_id == requests[index].intent_id
        cursor = report.after_intent_id
    settled = store.get(requests[2].intent_id)
    assert settled is not None and settled.intent_state is RuntimeIntentState.SUCCEEDED
    assert service.observe_pending(after_intent_id=cursor, limit=1).after_intent_id is None
    assert service.observe_pending(limit=1).after_intent_id == requests[0].intent_id


def test_missing_stop_proof_stays_quarantined_without_retrying_stop() -> None:
    store, runtime = (
        InMemoryIntentStore(),
        ScriptedNodeRuntime(ready_on_prepare=True, verify_stop=False),
    )
    service = service_for(store, runtime)
    service.submit_intent(build_request())
    request = build_request(intent_id="intent-stop", action=RuntimeActionKind.STOP)
    service.submit_intent(request)

    assert service.observe_pending().updated == 1

    intent = store.get(request.intent_id)
    assert intent is not None and intent.intent_state is RuntimeIntentState.QUARANTINED
    assert intent.stop_evidence is None and intent.memory_hold is MemoryHold.PEAK
    assert len(runtime.stopped) == 1


@pytest.mark.parametrize("foreign_node,approved", ((True, True), (False, False)))
def test_foreign_or_withdrawn_work_is_not_inspected(foreign_node: bool, approved: bool) -> None:
    store, runtime = InMemoryIntentStore(), ScriptedNodeRuntime()
    request = build_request(target_node_id="node-other" if foreign_node else SAMPLE_NODE_ID)
    recorded = RuntimeInstanceIntent(request, FIXED_NOW, FIXED_NOW)
    store.add(recorded)

    report = service_for(store, runtime, approved=approved).observe_pending()

    assert report.skipped == 1 and report.updated == 0
    assert store.get(request.intent_id) == recorded and runtime.inspections == []


class UnavailableInspection(ScriptedNodeRuntime):
    def inspect(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        if request.instance_id == "instance-bad":
            raise RuntimeControllerUnavailableError("SYNTHETIC_PRIVATE_DETAIL")
        return super().inspect(request, template)


def test_an_unavailable_instance_does_not_block_the_rest_of_the_page() -> None:
    store, runtime = InMemoryIntentStore(), UnavailableInspection()
    service = service_for(store, runtime)
    bad = build_request(intent_id="intent-a", instance_id="instance-bad")
    good = build_request(intent_id="intent-b", instance_id="instance-good")
    service.submit_intent(bad)
    before = store.get(bad.intent_id)
    service.submit_intent(good)
    runtime.mark_ready(good)

    report = service.observe_pending()

    assert (report.examined, report.updated, report.failed) == (2, 1, 1)
    assert store.get(bad.intent_id) == before
    ready = service.get_intent(good.intent_id)
    assert ready is not None and ready.intent_state is RuntimeIntentState.SUCCEEDED


class BlockingPrepare(ScriptedNodeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = Event(), Event()

    def prepare(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        self.entered.set()
        assert self.release.wait(3)
        return super().prepare(request, template)


def test_an_inflight_action_is_skipped_and_does_not_block_record_reads() -> None:
    store, runtime = InMemoryIntentStore(), BlockingPrepare()
    service = service_for(store, runtime)
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(service.submit_intent, build_request())
        try:
            assert runtime.entered.wait(2)
            report = service.observe_pending()
            assert (report.examined, report.skipped, report.updated) == (1, 1, 0)
            assert service.get_intent("intent-1") is not None
            assert runtime.inspections == []
        finally:
            runtime.release.set()
        pending.result(timeout=2)


def test_shutdown_can_stop_between_items_without_inspecting_the_rest() -> None:
    store, runtime = InMemoryIntentStore(), ScriptedNodeRuntime()
    service = service_for(store, runtime)
    for index in range(3):
        service.submit_intent(build_request(intent_id=f"intent-{index}", instance_id=f"i-{index}"))

    report = service.observe_pending(should_stop=lambda: bool(runtime.inspections))

    assert report.examined == 1
    assert len(runtime.inspections) == 1


def test_a_fact_settled_after_enumeration_is_reread_before_inspecting() -> None:
    runtime = ScriptedNodeRuntime()
    request = build_request()

    class SettledAfterPage(InMemoryIntentStore):
        def list_unresolved(
            self, *, after_intent_id: str | None, limit: int
        ) -> list[RuntimeInstanceIntent]:
            page = super().list_unresolved(after_intent_id=after_intent_id, limit=limit)
            current = self.get(request.intent_id)
            assert current is not None
            version = current.version
            current.mark_observed(runtime.mark_ready(request), at=FIXED_NOW)
            self.save(current, expected_version=version)
            return page

    store = SettledAfterPage()
    service = service_for(store, runtime)
    service.submit_intent(request)

    report = service.observe_pending()

    assert report.skipped == 1 and report.updated == 0
    assert runtime.inspections == []


def test_a_concurrent_sqlite_writer_cannot_be_overwritten_by_an_old_observation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipts.sqlite3"
    first, second = SqliteIntentStore(path), SqliteIntentStore(path)

    class RacingInspection(ScriptedNodeRuntime):
        def inspect(
            self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
        ) -> InstanceObservation | None:
            current = second.get(request.intent_id)
            assert current is not None
            version = current.version
            current.quarantine(RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE, at=FIXED_NOW)
            second.save(current, expected_version=version)
            return self.mark_ready(request)

    runtime = RacingInspection()
    service = service_for(first, runtime)
    service.submit_intent(build_request())
    try:
        report = service.observe_pending()
        assert report.failed == 1 and report.updated == 0
        recorded = first.get("intent-1")
        assert recorded is not None
        assert recorded.intent_state is RuntimeIntentState.QUARANTINED
        assert recorded.error_code is RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE
    finally:
        first.close()
        second.close()


def test_restarting_the_loop_resumes_durable_pending_work_without_prepare(tmp_path: Path) -> None:
    path = tmp_path / "intents.sqlite3"
    first = SqliteIntentStore(path)
    request, runtime = build_request(), ScriptedNodeRuntime()
    service_for(first, runtime).submit_intent(request)
    first.close()
    restarted = SqliteIntentStore(path)
    runtime.mark_ready(request)
    observer = LocalObservationLoop(
        service_for(restarted, runtime), interval_seconds=0.01, batch_size=1
    )
    try:
        observer.start()
        wait_ready(restarted)
        with pytest.raises(RuntimeError, match="already started"):
            observer.start()
    finally:
        observer.close()
        observer.close()
    assert len(runtime.prepared) == 1 and runtime.stopped == []
    restarted.close()


def test_loop_recovers_from_a_store_failure_without_logging_its_private_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class OnceUnavailable(InMemoryIntentStore):
        fail = True

        def list_unresolved(
            self, *, after_intent_id: str | None, limit: int
        ) -> list[RuntimeInstanceIntent]:
            if self.fail:
                self.fail = False
                raise OSError("SYNTHETIC_PRIVATE_DETAIL")
            return super().list_unresolved(after_intent_id=after_intent_id, limit=limit)

    store, runtime = OnceUnavailable(), ScriptedNodeRuntime()
    service = service_for(store, runtime)
    request = build_request()
    service.submit_intent(request)
    runtime.mark_ready(request)
    observer = LocalObservationLoop(service, interval_seconds=0.01, batch_size=1)
    with caplog.at_level(logging.WARNING):
        observer.start()
        try:
            wait_ready(store)
        finally:
            observer.close()
    assert "observation pass unavailable" in caplog.text
    assert "SYNTHETIC_PRIVATE_DETAIL" not in caplog.text


@pytest.mark.parametrize("limit", (0, 501, True, 1.5))
def test_the_observation_batch_is_bounded_before_accessing_the_store(limit: int) -> None:
    service = service_for(InMemoryIntentStore(), ScriptedNodeRuntime())
    with pytest.raises(InvalidRuntimeRequestError, match="limit within"):
        service.observe_pending(limit=limit)
