"""Native A/PG+InnoDB → real TLS → native B/SQLite → A observation, no GPU qualification."""

import asyncio
import ssl
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from tests.fake.controller import ScriptedNodeRuntime
from tests.fake.runtime_controller import TLS_COMMAND as COMMAND
from tests.fake.runtime_controller import TLS_HOST as HOST
from tests.fake.runtime_controller import TLS_PLAN as PLAN
from tests.fake.runtime_controller import TLS_REQUEST as REQUEST
from tests.integration.test_production_runtime_intents import RuntimeDatabase
from tests.integration.test_production_runtime_intents import database as database
from tests.integration.test_production_runtime_intents import foundation as foundation
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_runtime_observations import _receipt
from tests.support.runtime_controller_tls import TLSFiles, controller_server, tls_files
from tests.support.runtime_controller_tls import controller_tls as controller_tls

from speechnexus_backend.speech.domain.runtime_controller import (
    ControllerCallError,
    ControllerFailure,
    ControllerObservation,
)
from speechnexus_backend.speech.domain.runtime_intents import CreateRuntimeIntent, RuntimeResult
from speechnexus_backend.speech.domain.runtime_observations import ObserveRuntimeIntent
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.runtime_controller_http import HttpRuntimeController
from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    ObservedInstanceState,
    RuntimeInstanceIntentRequest,
)
from speechnexus_node_controller.domain.payloads import request_payload
from speechnexus_node_controller.domain.templates import ApprovedRuntimeTemplate

pytestmark = pytest.mark.integration


@pytest.fixture
def tls_runtime(runtime_database: RuntimeDatabase) -> RuntimeDatabase:
    assignments: list[str] = []
    parameters: tuple[object, ...] = ()
    for prefix, vector in (
        ("allocatable_", HOST.capacity),
        ("held_", HOST.held),
        ("quarantined_", HOST.quarantined),
    ):
        assignments.extend(prefix + dimension + " = %s" for dimension in DIMENSIONS)
        parameters += vector.values
    runtime_database.foundation.database.query(
        "UPDATE host_nodes SET " + ", ".join(assignments), parameters
    )
    return runtime_database


def held(runtime: RuntimeDatabase) -> tuple[int, ...]:
    row = runtime.foundation.database.query(
        "SELECT * FROM host_nodes WHERE host_node_id = %s", (HOST.node_id,)
    )[0]
    return tuple(row["held_" + name] for name in DIMENSIONS)


def observe(
    runtime: RuntimeDatabase, value: ControllerObservation, *, qualified: bool = False
) -> RuntimeResult:
    db = runtime.foundation.database
    request = value.observation
    instance = db.query(
        "SELECT version FROM engine_instances WHERE instance_id = %s AND instance_generation = %s",
        (request.instance_id, request.instance_generation),
    )[0]
    intent = db.query(
        "SELECT version FROM runtime_instance_intents WHERE intent_id = %s",
        (request.intent_id,),
    )[0]
    return runtime.port.observe_runtime(
        ObserveRuntimeIntent(
            request,
            instance["version"],
            intent["version"],
            COMMAND.owner_id,
            value.original_document,
            _receipt(runtime) if qualified else None,
        )
    )


async def test_native_tls_warming_ready_and_stop_preserve_the_single_budget_authority(
    tls_runtime: RuntimeDatabase, controller_tls: TLSFiles, tmp_path: Path
) -> None:
    runtime = tls_runtime
    created = runtime.port.create_runtime_intent(COMMAND)
    assert created.outcome == "committed" and created.fact is not None
    original = created.fact.request
    with controller_server(tmp_path / "node", controller_tls) as node:
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            loading = await client.submit(original)
            assert loading.observation.observed_instance_state == "loading"
            assert observe(runtime, loading).outcome == "committed"
            assert held(runtime) == HOST.held.add(PLAN.peak).values
            controller_request = node.runtime.prepared[0]
            node.runtime.observe_as(controller_request, ObservedInstanceState.WARMING_UP)
            # GET and exact POST replay only read stored facts. A cannot drive
            # daemon inspections by inventing a NodeReconcileRequest snapshot.
            assert (await client.query(original)).observation.observed_instance_state == "loading"
            node.service.reconcile_instance(original.instance_id, original.instance_generation)
            warming = await client.query(original)
            assert warming.observation.observed_instance_state == "warming_up"
            observed = observe(runtime, warming)
            assert observed.outcome == "committed", observed
            assert held(runtime) == HOST.held.add(PLAN.peak).values
            node.runtime.mark_ready(controller_request)
            assert (
                await client.query(original)
            ).observation.observed_instance_state == "warming_up"
            node.service.reconcile_instance(original.instance_id, original.instance_generation)
            ready = await client.query(original)
            before = runtime.snapshot()
            assert observe(runtime, ready).error_code == "reconciliation_required"
            assert runtime.snapshot() == before  # A does not trust HTTP ready as qualification.
            assert observe(runtime, ready, qualified=True).outcome == "committed"
            assert held(runtime) == HOST.held.add(PLAN.steady).values
            head = runtime.foundation.database.query("SELECT version FROM runtime_instance_heads")[
                0
            ]
            stop = replace(original, intent_id="runtime-stop", action="stop")
            stopped = runtime.port.create_runtime_intent(
                CreateRuntimeIntent(stop, head["version"], None, COMMAND.owner_id)
            )
            assert stopped.outcome == "committed"
            stopped_fact = await client.submit(stop)
            assert stopped_fact.observation.stop_evidence is not None
            assert observe(runtime, stopped_fact).outcome == "committed"
            assert held(runtime) == HOST.held.values
        assert node.methods == ["POST", "GET", "GET", "GET", "GET", "POST"]
        assert len(node.runtime.prepared) == len(node.runtime.stopped) == 1


async def test_local_observer_publishes_ready_over_tls_without_granting_budget_authority(
    tls_runtime: RuntimeDatabase, controller_tls: TLSFiles, tmp_path: Path
) -> None:
    runtime = tls_runtime
    created = runtime.port.create_runtime_intent(COMMAND)
    assert created.outcome == "committed" and created.fact is not None
    original = created.fact.request
    with controller_server(
        tmp_path / "observing-node", controller_tls, local_observer_enabled=True
    ) as node:
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            loading = await client.submit(original)
            assert loading.observation.observed_instance_state == "loading"
            assert observe(runtime, loading).outcome == "committed"
            peak = held(runtime)
            assert peak == HOST.held.add(PLAN.peak).values
            node.runtime.mark_ready(node.runtime.prepared[0])
            async with asyncio.timeout(3):
                while True:
                    ready = await client.query(original)
                    if ready.observation.observed_instance_state == "ready":
                        break
                    await asyncio.sleep(0.01)
            # The loop produced the observation, but only A's qualified transaction
            # may change the authoritative physical budget from peak to steady.
            before = runtime.snapshot()
            assert observe(runtime, ready).error_code == "reconciliation_required"
            assert runtime.snapshot() == before and held(runtime) == peak
            assert observe(runtime, ready, qualified=True).outcome == "committed"
            assert held(runtime) == HOST.held.add(PLAN.steady).values
        assert node.runtime.inspections
        assert len(node.runtime.prepared) == 1 and node.runtime.stopped == []
        assert node.methods[0] == "POST" and set(node.methods[1:]) == {"GET"}


async def test_local_observer_absence_without_stop_proof_keeps_the_authoritative_peak(
    tls_runtime: RuntimeDatabase, controller_tls: TLSFiles, tmp_path: Path
) -> None:
    runtime = tls_runtime
    created = runtime.port.create_runtime_intent(COMMAND)
    assert created.outcome == "committed" and created.fact is not None
    original = created.fact.request
    with controller_server(
        tmp_path / "absent-node", controller_tls, local_observer_enabled=True
    ) as node:
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            assert observe(runtime, await client.submit(original)).outcome == "committed"
            peak = held(runtime)
            node.runtime.observe_as(node.runtime.prepared[0], ObservedInstanceState.ABSENT)
            async with asyncio.timeout(3):
                while True:
                    missing = await client.query(original)
                    if missing.observation.intent_state == "failed":
                        break
                    await asyncio.sleep(0.01)
            assert missing.observation.stop_evidence is None
            assert observe(runtime, missing).outcome == "committed"
            assert held(runtime) == peak == HOST.held.add(PLAN.peak).values
            row = runtime.foundation.database.query(
                "SELECT base_budget_kind FROM engine_instances "
                "WHERE instance_id = %s AND instance_generation = %s",
                (original.instance_id, original.instance_generation),
            )[0]
            assert row["base_budget_kind"] == "peak"
        assert node.runtime.stopped == []


class SlowPrepare(ScriptedNodeRuntime):
    def __init__(self) -> None:
        super().__init__(ready_on_prepare=True)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def prepare(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation:
        self.entered.set()
        assert self.release.wait(3), "The synthetic warmup was not released"
        result = super().prepare(request, template)
        self.finished.set()
        return result


async def test_real_unanswered_post_keeps_peak_then_queries_the_same_persistent_intent(
    tls_runtime: RuntimeDatabase, controller_tls: TLSFiles, tmp_path: Path
) -> None:
    runtime = tls_runtime
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    before = runtime.snapshot()
    daemon = SlowPrepare()
    with controller_server(tmp_path / "node", controller_tls, runtime=daemon) as node:
        try:
            async with HttpRuntimeController(
                node.endpoint, timeout_seconds=0.1, ssl_context=node.tls.client_context()
            ) as impatient:
                with pytest.raises(ControllerCallError) as failure:
                    await impatient.submit(REQUEST)
                assert failure.value.request_may_have_run
                assert daemon.entered.is_set()
                assert runtime.snapshot() == before
                assert held(runtime) == HOST.held.add(PLAN.peak).values
            daemon.release.set()
            assert await asyncio.to_thread(daemon.finished.wait, 2)
            async with HttpRuntimeController(
                node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
            ) as recovered:
                result = await recovered.query(REQUEST)
                assert result.observation.intent_state == "succeeded"
                assert observe(runtime, result, qualified=True).outcome == "committed"
            assert held(runtime) == HOST.held.add(PLAN.steady).values
            assert len(daemon.prepared) == 1
            assert node.methods == ["POST", "GET"]
        finally:
            daemon.release.set()


async def test_auth_error_and_original_not_found_never_change_a_budget(
    tls_runtime: RuntimeDatabase, controller_tls: TLSFiles, tmp_path: Path
) -> None:
    runtime = tls_runtime
    assert runtime.port.create_runtime_intent(COMMAND).outcome == "committed"
    before = runtime.snapshot()
    with controller_server(tmp_path / "node", controller_tls) as node:
        bad = replace(node.endpoint, bearer_token="synthetic-wrong-component-token")
        for endpoint, expected in [
            (bad, ControllerFailure.REJECTED),
            (node.endpoint, ControllerFailure.NOT_FOUND),
        ]:
            async with HttpRuntimeController(
                endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
            ) as client:
                with pytest.raises(ControllerCallError) as failure:
                    await client.query(REQUEST)
                assert failure.value.failure == expected
                assert failure.value.request_may_have_run
            assert runtime.snapshot() == before
        assert not node.runtime.prepared and node.methods == ["GET", "GET"]


@pytest.mark.parametrize("bad", ["untrusted-ca", "wrong-ip-san"])
async def test_real_tls_rejects_untrusted_certificates_and_wrong_ip_identity(
    bad: str, controller_tls: TLSFiles, tmp_path: Path
) -> None:
    files = (
        tls_files(tmp_path / "wrong-pki", address="127.0.0.2")
        if bad == "wrong-ip-san"
        else controller_tls
    )
    with controller_server(tmp_path / "node", files) as node:
        context = files.client_context() if bad == "wrong-ip-san" else ssl.create_default_context()
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=context
        ) as client:
            with pytest.raises(ControllerCallError) as failure:
                await client.submit(REQUEST)
        assert failure.value.failure == ControllerFailure.UNAVAILABLE
        assert node.methods == [] and node.runtime.prepared == []


async def test_native_controller_app_restart_recovers_original_sqlite_intent_without_new_prepare(
    controller_tls: TLSFiles, tmp_path: Path
) -> None:
    daemon = ScriptedNodeRuntime(ready_on_prepare=True)
    directory = tmp_path / "node"
    with controller_server(directory, controller_tls, runtime=daemon) as node:
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            original = await client.submit(REQUEST)
            assert original.observation.intent_state == "succeeded"
    with controller_server(directory, controller_tls, runtime=daemon) as restarted:
        async with HttpRuntimeController(
            restarted.endpoint, timeout_seconds=2, ssl_context=restarted.tls.client_context()
        ) as client:
            recovered = await client.query(REQUEST)
            assert recovered.observation == original.observation
            replay = await client.submit(REQUEST)
            assert '"idempotent_replay":true' in replay.original_document
            changed = replace(REQUEST, fencing_token=8)
            with pytest.raises(ControllerCallError) as failure:
                await client.submit(changed)
            assert failure.value.controller_code == "intent_conflict"
        saved = restarted.store.get(REQUEST.intent_id)
        assert saved is not None
        assert request_payload(saved.request)["fencing_token"] == REQUEST.fencing_token
        assert len(daemon.prepared) == 1
