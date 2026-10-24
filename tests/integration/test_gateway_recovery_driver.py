"""Actual default gateway processes automatically consume native TLS/SQLite stop facts.

PG uses the restricted gateway role. The node daemon and initial dispatch are
synthetic; this verifies process/lifecycle/transaction behavior, not GPU qualification.
"""

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from psycopg.conninfo import make_conninfo
from tests.fake.controller import IMAGE_DIGEST, SAMPLE_IMAGE_REFERENCE
from tests.fake.docker_api import FakeDockerEngine
from tests.fake.execution import fake_ingress
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import counters
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_lease_release import command
from tests.integration.test_node_stop_gateway_recovery import (
    BINDING,
    INTENTS,
    NODE_PREPARE,
    NODE_STOP,
    source_for,
    uncertain_pair,
)
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_leases import LeaseDatabase
from tests.integration.test_production_leases import lease_database as lease_database
from tests.support.gateway_recovery import recovery_document
from tests.support.runtime_controller_tls import TOKEN, TLSFiles, controller_server
from tests.support.runtime_controller_tls import controller_tls as controller_tls

from speechnexus_backend.speech.infrastructure.runtime_codec import request_to_wire
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.recover_executions import ExecutionRecoveryService
from speechnexus_gateway.domain.submission import ExecutionState
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.recovery_worker import RecoveryWorker
from speechnexus_node_controller.infrastructure.docker_runtime import DockerNodeRuntime
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore

pytestmark = pytest.mark.integration
GATEWAY_TOKEN = "synthetic-default-process-gateway-token"


@contextmanager
def gateway_process(
    foundation: Foundation,
    directory: Path,
    config: Path,
    *,
    node_token: str = TOKEN,
    kill: bool = False,
) -> Iterator[httpx.Client]:
    directory.mkdir(exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SPEECHNEXUS_GATEWAY_")
    }
    values = {
        "control_database_url": make_conninfo(
            foundation.database.url,
            user=foundation.gateway_role,
            password="synthetic_foundation_test",
        ),
        "control_schema": foundation.database.schema,
        "node_id": "host-test",
        "gateway_id": "gateway-test",
        "component_token": GATEWAY_TOKEN,
        "ingress_lock_directory": str(directory),
        "recovery_config_file": str(config),
        "recovery_node_component_token": node_token,
    }
    environment.update(
        {"SPEECHNEXUS_GATEWAY_" + key.upper(): value for key, value in values.items()}
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    log = directory / "synthetic-process.log"
    try:
        with log.open("wb") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "speechnexus_gateway.app:create_app",
                    "--factory",
                    "--fd",
                    str(sock.fileno()),
                    "--log-level",
                    "info",
                    "--no-access-log",
                    "--no-proxy-headers",
                ],
                env=environment,
                pass_fds=(sock.fileno(),),
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{sock.getsockname()[1]}",
                    trust_env=False,
                    timeout=1,
                    headers={
                        "Authorization": "Bearer " + GATEWAY_TOKEN,
                        "X-Request-ID": "synthetic-process-recovery",
                    },
                ) as client:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        assert process.poll() is None, "default gateway exited before readiness"
                        try:
                            if client.get("/readyz").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.01)
                    else:
                        pytest.fail("default gateway did not become ready")
                    yield client
            finally:
                if process.poll() is None:
                    process.kill() if kill else process.terminate()
                try:
                    process.wait(8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(3)
                    pytest.fail("gateway did not join its bounded recovery worker")
                # Uvicorn restores and re-raises SIGTERM after graceful shutdown.
                assert process.returncode == (-9 if kill else -15)
    finally:
        sock.close()
    content = log.read_text()
    assert TOKEN not in content and GATEWAY_TOKEN not in content and str(config) not in content
    if not kill:
        assert "Application shutdown complete." in content
    replacement = ProcessIngressOwnership(directory, node_id="host-test", gateway_id="gateway-test")
    replacement.close()


def write_configuration(path: Path, tls: TLSFiles, port: int) -> None:
    value = recovery_document(ca=tls.certificate, port=port)
    value["bindings"] = [
        {
            "pool_id": BINDING.pool_id,
            "binding_revision_id": BINDING.binding_revision_id,
            "approved_template_id": BINDING.approved_template_id,
            "manifest_id": BINDING.manifest_id,
            "config_digest": BINDING.config_digest,
        }
    ]
    value |= {
        "batch_size": 1,
        "scan_interval_seconds": 0.05,
        "pass_budget_seconds": 1,
        "request_timeout_seconds": 0.5,
    }
    path.write_text(json.dumps(value))


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("first_failure", ["half-proof", "credential"])
def test_default_process_recovers_after_restart_without_manual_sweep_or_budget_writes(
    lease_database: LeaseDatabase,
    recovery_ledger: Ledger,
    controller_tls: TLSFiles,
    tmp_path: Path,
    first_failure: str,
) -> None:
    lease, ledger = lease_database, recovery_ledger
    _, dispatch = uncertain_pair(lease, ledger)
    before = counters(lease.foundation)
    engine = FakeDockerEngine(
        images={SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST},
        fail_remove_and_keep=first_failure == "half-proof",
        memory_peak_bytes=4 * 1024**2,
    )

    def native(store: SqliteIntentStore) -> DockerNodeRuntime:
        return DockerNodeRuntime(api=engine, node_id="host-test", stop_checkpoints=store)

    node_directory, gateway_directory = tmp_path / "node", tmp_path / "gateway"
    config = tmp_path / "recovery.json"
    with controller_server(node_directory, controller_tls, runtime_factory=native) as node:
        with httpx.Client(
            base_url=node.endpoint.base_url,
            verify=controller_tls.client_context(),
            trust_env=False,
            headers={"Authorization": "Bearer " + TOKEN},
            timeout=2,
        ) as control:
            assert control.post(INTENTS, json=request_to_wire(NODE_PREPARE)).status_code == 200
            assert control.post(INTENTS, json=request_to_wire(NODE_STOP)).status_code == 200
        write_configuration(config, controller_tls, node.endpoint.port)
        prior_methods = len(node.methods)
        with gateway_process(
            lease.foundation,
            gateway_directory,
            config,
            node_token="synthetic-wrong-node-token" if first_failure == "credential" else TOKEN,
            kill=True,
        ) as client:
            deadline = time.monotonic() + 3
            while len(node.methods) < prior_methods + 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(node.methods) >= prior_methods + 3
            for submit_id in ("native-intent-one", "native-intent-two"):
                response = client.get("/internal/v1/executions/" + submit_id)
                assert response.status_code == 200
                assert response.json()["execution_state"] == "unknown_quarantined"
            assert counters(lease.foundation) == before
            assert (
                lease.port.release_lease(command(lease)).error_code == "execution_cleanup_pending"
            )
        assert set(node.methods[prior_methods:]) == {"GET"}

    engine.fail_remove_and_keep = False
    # Node restart resumes its original stop checkpoint; gateway restarts its own scan.
    with controller_server(
        node_directory, controller_tls, runtime_factory=native, local_observer_enabled=True
    ) as node:
        write_configuration(config, controller_tls, node.endpoint.port)
        with gateway_process(lease.foundation, gateway_directory, config) as client:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                states = [
                    client.get("/internal/v1/executions/" + submit_id).json()["execution_state"]
                    for submit_id in ("native-intent-one", "native-intent-two")
                ]
                if states == ["aborted", "aborted"]:
                    break
                time.sleep(0.01)
            assert states == ["aborted", "aborted"]
            assert counters(lease.foundation) == before
            assert dispatch.dispatch_count == 2 and not engine.containers
            assert set(node.methods) == {"GET"}
            for submit_id in ("native-intent-one", "native-intent-two"):
                current = ledger.get(submit_id)
                assert current is not None and current.execution_state is ExecutionState.ABORTED
            assert lease.port.release_lease(command(lease)).outcome == "committed"
            assert lease.port.release_lease(command(lease, "lease-other")).outcome == "committed"
            assert lease.foundation.database.query(
                "SELECT held_unified_physical_memory_bytes AS held, "
                "quarantined_unified_physical_memory_bytes AS q FROM host_nodes"
            ) == [{"held": 0, "q": 0}]


def test_native_periodic_readonly_query_keeps_a_budget_on_pg_and_innodb(
    lease_database: LeaseDatabase,
    recovery_ledger: Ledger,
    controller_tls: TLSFiles,
    tmp_path: Path,
) -> None:
    """InnoDB remains a SQL/domain oracle, not a production gateway composition."""
    lease, ledger = lease_database, recovery_ledger
    _, dispatch = uncertain_pair(lease, ledger)
    before = counters(lease.foundation)
    engine = FakeDockerEngine(
        images={SAMPLE_IMAGE_REFERENCE: IMAGE_DIGEST}, memory_peak_bytes=4 * 1024**2
    )

    def native(store: SqliteIntentStore) -> DockerNodeRuntime:
        return DockerNodeRuntime(api=engine, node_id="host-test", stop_checkpoints=store)

    with controller_server(tmp_path / "node", controller_tls, runtime_factory=native) as node:
        with httpx.Client(
            base_url=node.endpoint.base_url,
            verify=controller_tls.client_context(),
            trust_env=False,
            headers={"Authorization": "Bearer " + TOKEN},
            timeout=2,
        ) as control:
            assert control.post(INTENTS, json=request_to_wire(NODE_PREPARE)).status_code == 200
            assert control.post(INTENTS, json=request_to_wire(NODE_STOP)).status_code == 200
        prior_methods = len(node.methods)
        ingress = fake_ingress(node_id="host-test", gateway_id="gateway-test")
        service = ExecutionRecoveryService(
            ingress=ingress,
            ledger=ledger,
            submissions=ExecutionQueryService(ingress=ingress, ledger=ledger),
            terminations=source_for(node.endpoint.port, controller_tls),
        )
        worker = RecoveryWorker(service, interval_seconds=0.05, batch_size=1, pass_budget_seconds=1)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = [ledger.get(name) for name in ("native-intent-one", "native-intent-two")]
                if all(
                    row is not None and row.execution_state is ExecutionState.ABORTED
                    for row in rows
                ):
                    break
                time.sleep(0.01)
            assert all(
                row is not None and row.execution_state is ExecutionState.ABORTED for row in rows
            )
            assert counters(lease.foundation) == before and dispatch.dispatch_count == 2
            assert set(node.methods[prior_methods:]) == {"GET"}
        finally:
            worker.close()
