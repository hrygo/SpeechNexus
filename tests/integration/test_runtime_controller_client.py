"""Native A/PG+InnoDB → real TLS → native B/SQLite → A observation, no GPU qualification."""

import asyncio
import ssl
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from tests.fake.controller import IMAGE_DIGEST, SAMPLE_IMAGE_REFERENCE, ScriptedNodeRuntime
from tests.fake.docker_api import FakeDockerEngine
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
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore

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


@pytest.mark.parametrize("claimed_state", ["ready", "absent", "stopped"])
async def test_external_snapshot_cannot_change_pending_prepare_or_authoritative_peak(
    tls_runtime: RuntimeDatabase,
    controller_tls: TLSFiles,
    tmp_path: Path,
    claimed_state: str,
) -> None:
    runtime = tls_runtime
    created = runtime.port.create_runtime_intent(COMMAND)
    assert created.outcome == "committed" and created.fact is not None
    original = created.fact.request
    with controller_server(tmp_path / "diagnostic-node", controller_tls) as node:
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            loading = await client.submit(original)
            assert observe(runtime, loading).outcome == "committed"
            peak = held(runtime)
            persisted = node.store.get(original.intent_id)
            assert persisted is not None
            state_before = runtime.snapshot()
            fact: dict[str, object] = {
                "instance_id": original.instance_id,
                "instance_generation": original.instance_generation,
                "approved_template_id": original.binding.approved_template_id,
                "config_digest": original.binding.config_digest,
                "observed_instance_state": claimed_state,
            }
            if claimed_state == "stopped":
                fact["stop_evidence"] = {
                    "process_or_container_exited": True,
                    "resources_released_confirmed": True,
                    "verified_at": "2099-01-01T00:00:00Z",
                    "exit_code": 0,
                    "runtime_container_id": "caller-invented-container",
                }
            async with httpx.AsyncClient(
                base_url=node.endpoint.base_url,
                verify=node.tls.client_context(),
                trust_env=False,
                timeout=2,
                headers={"Authorization": "Bearer " + node.endpoint.bearer_token},
            ) as diagnostic:
                response = await diagnostic.post(
                    f"/internal/v1/nodes/{node.endpoint.node_id}/reconcile",
                    json={
                        "controller_id": node.endpoint.node_id,
                        "fencing_token": 10**12,
                        "reported_at": "2099-01-01T00:00:00Z",
                        "instances": [fact],
                    },
                )
            assert response.status_code == 200, response.text
            assert datetime.fromisoformat(response.json()["accepted_at"]).year != 2099
            assert node.store.get(original.intent_id) == persisted
            assert runtime.snapshot() == state_before
            assert not node.runtime.inspections and not node.runtime.stopped
            reread = await client.query(original)
            assert reread.observation == loading.observation
            assert observe(runtime, reread, qualified=True).outcome == "replayed"
            assert runtime.snapshot() == state_before
            assert held(runtime) == peak == HOST.held.add(PLAN.peak).values
            assert node.service.submit_intent(persisted.request).idempotent_replay
            assert len(node.runtime.prepared) == 1


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


async def test_persisted_stop_recovery_over_tls_releases_only_after_both_facts(
    tls_runtime: RuntimeDatabase, controller_tls: TLSFiles, tmp_path: Path
) -> None:
    runtime = tls_runtime
    created = runtime.port.create_runtime_intent(COMMAND)
    assert created.outcome == "committed" and created.fact is not None
    original = created.fact.request
    engine = FakeDockerEngine(
        images={SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST}, fail_remove_and_keep=True, exit_code=143
    )

    def native(store: SqliteIntentStore) -> DockerNodeRuntime:
        return DockerNodeRuntime(
            api=engine, node_id=REQUEST.binding.target_node_id, stop_checkpoints=store
        )

    def charge() -> tuple[int, ...]:
        row = runtime.foundation.database.query(
            "SELECT * FROM host_nodes WHERE host_node_id = %s", (HOST.node_id,)
        )[0]
        return tuple(row["held_" + name] + row["quarantined_" + name] for name in DIMENSIONS)

    directory = tmp_path / "recovering-native-node"
    with controller_server(directory, controller_tls, runtime_factory=native) as node:
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            assert observe(runtime, await client.submit(original), qualified=True).outcome == (
                "committed"
            )
            baseline_charge = charge()
            stop = replace(original, intent_id="runtime-stop-recovery", action="stop")
            head = runtime.foundation.database.query("SELECT version FROM runtime_instance_heads")[
                0
            ]
            accepted = runtime.port.create_runtime_intent(
                CreateRuntimeIntent(stop, head["version"], None, COMMAND.owner_id)
            )
            assert accepted.outcome == "committed"
            incomplete = await client.submit(stop)
            assert incomplete.observation.stop_evidence is None
            assert incomplete.observation.intent_state == "quarantined"
            assert observe(runtime, incomplete).outcome == "committed"
            assert charge() == baseline_charge  # Exit alone cannot return capacity.
            recorded = node.store.get(stop.intent_id)
            assert recorded is not None
            checkpoint = node.store.get_stop_checkpoint(recorded.request)
            assert checkpoint is not None and checkpoint.stage == "exited"
            assert len(engine.containers) == 1 and not engine.created[0].running
            before = runtime.snapshot()
            calls = (len(engine.stopped), len(engine.removed))
            # Even the exact pinned container identity and exit code cannot make
            # a caller's claimed release authoritative while that container remains.
            async with httpx.AsyncClient(
                base_url=node.endpoint.base_url,
                verify=node.tls.client_context(),
                trust_env=False,
                timeout=2,
                headers={"Authorization": "Bearer " + node.endpoint.bearer_token},
            ) as diagnostic:
                response = await diagnostic.post(
                    f"/internal/v1/nodes/{node.endpoint.node_id}/reconcile",
                    json={
                        "controller_id": node.endpoint.node_id,
                        "fencing_token": 10**12,
                        "reported_at": datetime.now(UTC).isoformat(),
                        "instances": [
                            {
                                "instance_id": stop.instance_id,
                                "instance_generation": stop.instance_generation,
                                "approved_template_id": stop.binding.approved_template_id,
                                "config_digest": stop.binding.config_digest,
                                "observed_instance_state": "absent",
                                "stop_evidence": {
                                    "process_or_container_exited": True,
                                    "resources_released_confirmed": True,
                                    "verified_at": datetime.now(UTC).isoformat(),
                                    "exit_code": checkpoint.exit_code,
                                    "runtime_container_id": checkpoint.container_id,
                                },
                            }
                        ],
                    },
                )
            assert response.status_code == 200, response.text
            assert response.json()["quarantined_instance_ids"] == [stop.instance_id]
            assert node.store.get(stop.intent_id) == recorded
            assert node.store.get_stop_checkpoint(recorded.request) == checkpoint
            assert runtime.snapshot() == before
            assert (len(engine.stopped), len(engine.removed)) == calls
            unchanged = await client.query(stop)
            assert unchanged.observation == incomplete.observation
            assert observe(runtime, unchanged).outcome == "replayed"
            assert runtime.snapshot() == before
            assert charge() == baseline_charge
    # Restart the native app/SQLite, retaining the stopped daemon target and A's database.
    engine.fail_remove_and_keep = False
    with controller_server(
        directory, controller_tls, runtime_factory=native, local_observer_enabled=True
    ) as node:
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            async with asyncio.timeout(3):
                while True:
                    recovered = await client.query(stop)
                    if recovered.observation.intent_state == "succeeded":
                        break
                    await asyncio.sleep(0.01)
            assert recovered.observation.stop_evidence is not None
            assert recovered.observation.stop_evidence.process_or_container_exited
            assert recovered.observation.stop_evidence.resources_released_confirmed
            assert observe(runtime, recovered).outcome == "committed"
            assert held(runtime) == HOST.held.values
            assert charge() == HOST.held.add(HOST.quarantined).values
            reread = await client.query(stop)
            assert reread.observation.stop_evidence == recovered.observation.stop_evidence
        assert set(node.methods) == {"GET"} and not engine.containers
        assert len(engine.created) == len(engine.stopped) == 1


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
