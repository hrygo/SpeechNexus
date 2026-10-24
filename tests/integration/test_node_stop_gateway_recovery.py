"""Native node/SQLite/TLS → gateway stop ledger → A lease release on PG/InnoDB.

The daemon and engine dispatch are synthetic. PostgreSQL uses the production
gateway adapter; InnoDB executes the same SQL/domain rules through test plumbing.
"""

import ssl
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from tests.fake.controller import IMAGE_DIGEST, SAMPLE_IMAGE_REFERENCE
from tests.fake.docker_api import FakeDockerEngine
from tests.fake.execution import ScriptedEngine, fake_ingress
from tests.fake.runtime_controller import TLS_REQUEST
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_recovery_sweep import LossyEngine
from tests.integration.test_gateway_stop_evidence import counters
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import (
    LEASE_DEADLINE_AT_US,
    identity_for,
    request_for,
)
from tests.integration.test_lease_release import command
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_leases import LEASE_ID, LeaseDatabase, _second_stage
from tests.integration.test_production_leases import lease_database as lease_database
from tests.support.runtime_controller_tls import TOKEN, TLSFiles, controller_server, tls_files
from tests.support.runtime_controller_tls import controller_tls as controller_tls

from speechnexus_backend.speech.infrastructure.runtime_codec import request_to_wire
from speechnexus_gateway.application.recover_executions import ExecutionRecoveryService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.recovery import whole_instance_stop_evidence
from speechnexus_gateway.domain.submission import ExecutionState, RecordStop
from speechnexus_gateway.infrastructure.node_stop_evidence import (
    NodeStopBinding,
    NodeStopEndpoint,
    NodeStopEvidenceSource,
)
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore

pytestmark = pytest.mark.integration
NODE_PREPARE = replace(TLS_REQUEST, instance_id="instance-test")
NODE_STOP = replace(NODE_PREPARE, intent_id="runtime-stop", action="stop")
BINDING = NodeStopBinding(
    "host-test",
    "gateway-test",
    "tts-realtime",
    "binding-test",
    "template-test",
    "manifest-test",
    NODE_PREPARE.binding.config_digest,
)
INTENTS = "/internal/v1/nodes/host-test/instance-intents"


def source_for(
    port: int, tls: TLSFiles, *, token: str = TOKEN, context: ssl.SSLContext | None = None
) -> NodeStopEvidenceSource:
    return NodeStopEvidenceSource(
        NodeStopEndpoint("host-test", "127.0.0.1", port, token),
        bindings=(BINDING,),
        ssl_context=tls.client_context() if context is None else context,
        timeout_seconds=2,
    )


def uncertain_pair(
    lease: LeaseDatabase, ledger: Ledger
) -> tuple[SubmitExecutionService, ScriptedEngine]:
    db = lease.foundation.database
    _second_stage(lease)
    # Replace synthetic setup rows before behavior starts, preserving their leases.
    db.query("DELETE FROM submit_intents")
    db.query("UPDATE admission_leases SET deadline_at_us = %s", (LEASE_DEADLINE_AT_US,))
    db.query("UPDATE engine_instances SET config_digest = %s", (BINDING.config_digest,))
    engine = LossyEngine()
    submissions = SubmitExecutionService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=ledger,
        dispatch=engine,
        observation=engine,
    )
    for lease_id, submit_id, stage in [
        (LEASE_ID, "native-intent-one", "stage-main"),
        ("lease-other", "native-intent-two", "stage-other"),
    ]:
        identity = identity_for(
            lease_id, submit_intent_id=submit_id, attempt_id="attempt-test", stage_id=stage
        )
        current = submissions.submit(request_for(identity)).intent
        assert current.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    for lease_id in (LEASE_ID, "lease-other"):
        assert lease.port.quarantine_lease(lease.request(lease_id)).outcome == "committed"
    return submissions, engine


def sweep_for(
    ledger: Ledger, submissions: SubmitExecutionService, source: NodeStopEvidenceSource
) -> ExecutionRecoveryService:
    return ExecutionRecoveryService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=ledger,
        submissions=submissions,
        terminations=source,
    )


@pytest.mark.parametrize("batch_size", [1, 2])
def test_checkpoint_backed_stop_closes_shared_executions_then_only_a_releases(
    lease_database: LeaseDatabase,
    recovery_ledger: Ledger,
    controller_tls: TLSFiles,
    tmp_path: Path,
    batch_size: int,
) -> None:
    lease, ledger = lease_database, recovery_ledger
    submissions, dispatch = uncertain_pair(lease, ledger)
    before = counters(lease.foundation)
    engine = FakeDockerEngine(
        images={SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST},
        fail_remove_and_keep=True,
        exit_code=143,
        memory_peak_bytes=4 * 1024**2,
    )

    def native(store: SqliteIntentStore) -> DockerNodeRuntime:
        return DockerNodeRuntime(api=engine, node_id="host-test", stop_checkpoints=store)

    directory = tmp_path / "checkpoint-node"
    with controller_server(directory, controller_tls, runtime_factory=native) as node:
        source = source_for(node.endpoint.port, node.tls)
        with httpx.Client(
            base_url=node.endpoint.base_url,
            verify=node.tls.client_context(),
            trust_env=False,
            headers={"Authorization": "Bearer " + TOKEN},
            timeout=2,
        ) as control:
            assert control.post(INTENTS, json=request_to_wire(NODE_PREPARE)).status_code == 200
            stopped = control.post(INTENTS, json=request_to_wire(NODE_STOP))
            assert stopped.status_code == 200
            assert stopped.json()["intent_state"] == "quarantined"
        pending = ledger.get("native-intent-one")
        assert pending is not None
        assert source.termination_evidence(pending) is None
        report = sweep_for(ledger, submissions, source).sweep()
        assert report.examined == report.still_unknown == 2 and report.closed == 0
        assert counters(lease.foundation) == before
        assert lease.port.release_lease(command(lease)).error_code == "execution_cleanup_pending"
        assert len(engine.containers) == 1 and not engine.created[0].running
    # Node restart automatically resumes the original stop; consumers only GET.
    engine.fail_remove_and_keep = False
    with controller_server(
        directory, controller_tls, runtime_factory=native, local_observer_enabled=True
    ) as node:
        source = source_for(node.endpoint.port, node.tls)
        deadline = time.monotonic() + 3
        while (
            fact := source.termination_evidence(pending)
        ) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fact is not None and fact.is_complete
        query = sweep_for(ledger, submissions, source)
        first = query.sweep(limit=batch_size)
        assert first.closed == batch_size
        if batch_size == 1:
            assert query.sweep(limit=1).closed == 1
        assert query.sweep().examined == 0
        assert counters(lease.foundation) == before  # B never releases A's budgets.
        rows = lease.foundation.database.query(
            "SELECT evidence_id, evidence_digest, observed_at_us FROM execution_stop_evidence"
        )
        assert len(rows) == 2 and len({row["evidence_id"] for row in rows}) == 2
        assert all(row["evidence_digest"] == fact.evidence_digest for row in rows)
        assert rows[0]["observed_at_us"] == rows[1]["observed_at_us"]
        current = ledger.get("native-intent-one")
        assert current is not None and current.execution_state is ExecutionState.ABORTED
        replay = whole_instance_stop_evidence(current, fact)
        assert replay is not None
        assert ledger.record_stop(RecordStop(replay, ExecutionState.ABORTED)) == current
        assert set(node.methods) == {"GET"} and not engine.containers
        assert len(engine.stopped) == 1 and dispatch.dispatch_count == 2

        commands = [command(lease), command(lease, "lease-other")]
        assert lease.port.release_lease(commands[0]).outcome == "committed"
        assert lease.port.release_lease(commands[1]).outcome == "committed"
        assert lease.foundation.database.query(
            "SELECT held_unified_physical_memory_bytes AS held, "
            "quarantined_unified_physical_memory_bytes AS q FROM host_nodes"
        ) == [{"held": 0, "q": 0}]
        assert lease.foundation.database.query(
            "SELECT held_attempts, quarantined_attempts FROM workspace_quotas"
        ) == [{"held_attempts": 0, "quarantined_attempts": 0}]
        state = lease.snapshot()
        assert all(lease.port.release_lease(cmd).outcome == "replayed" for cmd in commands)
        assert lease.snapshot() == state
        assert (
            lease.foundation.database.query(
                "SELECT evidence_id, evidence_digest, observed_at_us FROM execution_stop_evidence"
            )
            == rows
        )


@pytest.mark.parametrize("bad", ["credential", "tls", "wrong-ip", "legacy-no-checkpoint"])
def test_failed_sources_keep_gateway_unknown_and_a_budget_held(
    lease_database: LeaseDatabase,
    recovery_ledger: Ledger,
    controller_tls: TLSFiles,
    tmp_path: Path,
    bad: str,
) -> None:
    lease, ledger = lease_database, recovery_ledger
    submissions, _ = uncertain_pair(lease, ledger)
    before = counters(lease.foundation)
    engine = FakeDockerEngine(
        images={SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST}, memory_peak_bytes=4 * 1024**2
    )

    def native(store: SqliteIntentStore) -> DockerNodeRuntime:
        return DockerNodeRuntime(api=engine, node_id="host-test", stop_checkpoints=store)

    files = (
        tls_files(tmp_path / "wrong-ip-pki", address="127.0.0.2")
        if bad == "wrong-ip"
        else controller_tls
    )
    with controller_server(
        tmp_path / "untrusted-source",
        files,
        runtime_factory=native if bad != "legacy-no-checkpoint" else None,
    ) as node:
        request = replace(NODE_STOP, instance_id="instance-test")
        with httpx.Client(
            base_url=node.endpoint.base_url,
            # The test must establish the available proof before testing the
            # consumer against a bad IP SAN. Setup uses the in-process service.
            verify=node.tls.client_context(),
            trust_env=False,
            headers={"Authorization": "Bearer " + TOKEN},
        ) as control:
            if bad == "wrong-ip":
                from speechnexus_node_controller.domain.payloads import parse_request

                node.service.submit_intent(parse_request(request_to_wire(NODE_PREPARE)))
                node.service.submit_intent(parse_request(request_to_wire(request)))
            else:
                assert control.post(INTENTS, json=request_to_wire(NODE_PREPARE)).status_code == 200
                assert control.post(INTENTS, json=request_to_wire(request)).status_code == 200
        if bad != "legacy-no-checkpoint":
            assert node.service.get_verified_stop("instance-test", 1).stop_evidence is not None
        else:
            # The scripted runtime writes success but cannot create a local checkpoint.
            recorded = node.store.get(NODE_STOP.intent_id)
            assert recorded is not None
            assert node.store.get_stop_checkpoint(recorded.request) is None
        methods_before = list(node.methods)
        source = source_for(
            node.endpoint.port,
            node.tls,
            token="wrong-synthetic-token" if bad == "credential" else TOKEN,
            context=ssl.create_default_context() if bad == "tls" else None,
        )
        report = sweep_for(ledger, submissions, source).sweep()
        assert report.still_unknown == 2 and report.closed == 0
        assert counters(lease.foundation) == before
        assert lease.port.release_lease(command(lease)).error_code == "execution_cleanup_pending"
        assert not lease.foundation.database.query("SELECT * FROM execution_stop_evidence")
        if bad in {"tls", "wrong-ip"}:
            assert node.methods == methods_before  # TLS failed before any proof HTTP request.
