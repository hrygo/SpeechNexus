"""Production gateway composition root with real restricted PostgreSQL authority."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_submit_http import (
    LEASE,
    TOKEN,
    abort_body,
    contract_command,
    submit_body,
)
from tests.integration.test_gateway_submit_ledger import identity_for, seed_second_attempt
from tests.integration.test_production_jobs import database as database

from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants,
    gateway_binding_barrier_grants,
)
from speechnexus_gateway.app import create_app
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.ports import AbortOutcome, DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.submission import DispatchResult, SubmissionIntent, SubmitRequest
from speechnexus_gateway.settings import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


class SyntheticEngine:
    """No fabricated termination evidence; record the original identities observed."""

    running = False
    max_command_bytes = 1_000_000

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.aborted: list[tuple[str, str, int]] = []

    def open(self) -> None:
        self.running = True

    def close(self) -> None:
        self.running = False

    def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
        self.dispatched.append(request.identity.submit_intent_id)
        return DispatchOutcome(DispatchResult.ACKNOWLEDGED, datetime.now(UTC), "synthetic-engine")

    def observe(self, intent: SubmissionIntent) -> EngineObservation | None:
        return None

    def start(self, intent: SubmissionIntent) -> None:
        pass

    def discard(self, submit_intent_id: str) -> None:
        pass

    def abort(self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int) -> AbortOutcome:
        self.aborted.append((intent.identity.submit_intent_id, reason, wait_timeout_ms))
        return AbortOutcome(observed_at=datetime.now(UTC))


def seed_binding(foundation: Foundation) -> None:
    db = foundation.database
    key = "sha256:" + "a" * 64
    db.query(
        "INSERT INTO speech_binding_families (binding_id,latest_revision) VALUES ('synthetic','1')"
    )
    db.query(
        "INSERT INTO speech_binding_commands "
        "(key_digest,binding_id,document_version,intent_json,request_digest) "
        "VALUES (%s,'synthetic',1,'{}',%s)",
        (key, key),
    )
    db.query(
        "INSERT INTO speech_binding_revisions "
        "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
        "definition_digest,request_digest,created_at_us) "
        "VALUES ('synthetic','1','binding-test',%s,1,%s,%s,%s,1)",
        (key, '{"binding_id":"synthetic","tasks":["tts"]}', key, key),
    )
    db.query(
        "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('binding','synthetic','1','binding-test',1)"
    )
    for statement in gateway_binding_barrier_grants(db.schema, foundation.gateway_role):
        db.query(statement)


@pytest.mark.parametrize("abort_authorized", [True, False])
async def test_real_gateway_composition_root_abort_authority(
    foundation: Foundation, tmp_path: Path, abort_authorized: bool
) -> None:
    seed_second_attempt(foundation)
    seed_binding(foundation)
    db = foundation.database
    if abort_authorized:
        for statement in gateway_abort_grants(db.schema, foundation.gateway_role):
            db.query(statement)
    settings = Settings(
        control_database_url=SecretStr(
            make_conninfo(
                db.url, user=foundation.gateway_role, password="synthetic_foundation_test"
            )
        ),
        control_schema=db.schema,
        node_id="host-test",
        gateway_id="gateway-test",
        component_token=SecretStr(TOKEN),
        ingress_lock_directory=tmp_path,
    )
    engine = SyntheticEngine()
    app = create_app(settings=settings, file_execution_factory=lambda ingress, ledger: engine)
    identity = identity_for(LEASE, submit_intent_id="intent-composition")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        submitted = await client.post(
            "/internal/v1/executions/submit", json=submit_body(identity, contract_command())
        )
        assert submitted.status_code == 200, submitted.text
        assert engine.dispatched == ["intent-composition"]
        path = "/internal/v1/executions/intent-composition/abort-intents"
        payload = abort_body(reason="resource_force_disabled")
        first = await client.post(path, json=payload)
        if abort_authorized:
            assert first.status_code == 200, first.text
            assert first.json()["execution_state"] == "unknown_quarantined"
            replay = await client.post(path, json=payload)
            assert replay.status_code == 200, replay.text
            assert replay.json()["execution_state"] == "unknown_quarantined"
            conflict = await client.post(path, json={**payload, "wait_timeout_ms": 2000})
            assert conflict.status_code == 409
            assert engine.aborted == [("intent-composition", "resource_force_disabled", 1000)]
        else:
            assert first.status_code == 503, first.text
            assert engine.aborted == []
    rows = db.query(
        "SELECT count(*) AS n FROM gateway_abort_intents WHERE submit_intent_id=%s",
        ("intent-composition",),
    )
    assert rows[0]["n"] == int(abort_authorized)
    lease = db.query("SELECT lease_state FROM admission_leases WHERE lease_id=%s", (LEASE,))[0]
    assert lease["lease_state"] != "released"


async def test_worker_composition_root_opens_two_restricted_roles(
    foundation: Foundation, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from speechnexus_backend.binding_disable_worker import BindingDisableWorkerSettings, run
    from speechnexus_backend.speech.application.binding_disable_coordinator import (
        BindingDisableCoordinator,
        DisablePassResult,
    )
    from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
        disable_coordinator_grants,
        disable_settlement_grants,
    )

    db = foundation.database
    scanner, settlement = db.schema + "_scan", db.schema + "_settle"
    for role in (scanner, settlement):
        db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        for statement in disable_settlement_grants(db.schema, settlement):
            db.query(statement)
        config = BindingDisableWorkerSettings(
            SPEECHNEXUS_PROCESS_ROLE="binding-disable-worker",
            SPEECHNEXUS_DISABLE_DATABASE_URL=make_conninfo(
                db.url, user=scanner, password="synthetic_settlement_test"
            ),
            SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL=make_conninfo(
                db.url, user=settlement, password="synthetic_settlement_test"
            ),
            SPEECHNEXUS_DATABASE_SCHEMA=db.schema,
            SPEECHNEXUS_DISABLE_GATEWAY_ID="gateway-test",
            SPEECHNEXUS_DISABLE_NODE_ID="host-test",
            SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL="http://127.0.0.1:8010",
            SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL=TOKEN,
            SPEECHNEXUS_DISABLE_GATEWAY_TIMEOUT_SECONDS=3,
            SPEECHNEXUS_DISABLE_STORAGE_TIMEOUT_SECONDS=3,
            SPEECHNEXUS_DISABLE_POLL_SECONDS=0.1,
            SPEECHNEXUS_DISABLE_BATCH_SIZE=1,
            SPEECHNEXUS_DISABLE_WAIT_TIMEOUT_MS=1000,
        )
        done = asyncio.Event()
        real_pass = BindingDisableCoordinator.run_pass

        async def observed_pass(coordinator: BindingDisableCoordinator) -> DisablePassResult:
            result = await real_pass(coordinator)
            done.set()
            return result

        monkeypatch.setattr(BindingDisableCoordinator, "run_pass", observed_pass)
        task = asyncio.create_task(run(config))
        try:
            await asyncio.wait_for(done.wait(), timeout=10)
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        for role in (scanner, settlement):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')


async def test_worker_restarts_original_abort_via_real_gateway(
    foundation: Foundation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise both composition roots with a loopback HTTP hop and durable PG facts."""
    import asyncio
    import socket

    import uvicorn

    from speechnexus_backend.binding_disable_worker import BindingDisableWorkerSettings, run
    from speechnexus_backend.speech.application.binding_disable_coordinator import (
        BindingDisableCoordinator,
        DisablePassResult,
    )
    from speechnexus_backend.speech.domain.binding_disable_intent import DisableExecutionTarget
    from speechnexus_backend.speech.domain.binding_lifecycle import BindingRetirementCommand
    from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
        disable_coordinator_grants,
        disable_settlement_grants,
    )
    from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
        PostgresBindingLifecycle,
    )
    from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
        GatewayExecutionClient,
    )
    from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
    from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
        binding_lifecycle_grants,
    )

    db = foundation.database
    seed_second_attempt(foundation)
    seed_binding(foundation)
    # Exclude the foundation's unrelated synthetic execution from this disable.
    db.query(
        "UPDATE admission_attempts SET binding_revision_id='other-binding' "
        "WHERE attempt_id='attempt-test'"
    )
    db.query(
        "INSERT INTO speech_publication_heads (kind,object_id,revision,revision_id,updated_at_us) "
        "VALUES ('binding','synthetic','1','binding-test',1)"
    )
    db.query(
        "INSERT INTO speech_binding_publication_evidence "
        "(kind,object_id,revision,revision_id,capacity_manifest_id,qualification_report_id,"
        "evidence_source,recorded_at_us) VALUES "
        "('binding','synthetic','1','binding-test','manifest-test','qualification-test',"
        "'operator_cli',1)"
    )
    for statement in gateway_abort_grants(db.schema, foundation.gateway_role):
        db.query(statement)
    for statement in binding_lifecycle_grants(db.schema, foundation.speech_role):
        db.query(statement)
    scanner, settlement = db.schema + "_scan", db.schema + "_settle"
    for role in (scanner, settlement):
        db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    lifecycle_pool = speech_pool(
        make_conninfo(db.url, user=foundation.speech_role, password="synthetic_foundation_test"),
        schema=db.schema,
    )
    server_task: asyncio.Task[None] | None = None
    server: uvicorn.Server | None = None
    try:
        for statement in disable_coordinator_grants(db.schema, scanner):
            db.query(statement)
        for statement in disable_settlement_grants(db.schema, settlement):
            db.query(statement)
        lifecycle_pool.open()
        engine = SyntheticEngine()
        settings = Settings(
            control_database_url=SecretStr(
                make_conninfo(
                    db.url, user=foundation.gateway_role, password="synthetic_foundation_test"
                )
            ),
            control_schema=db.schema,
            node_id="host-test",
            gateway_id="gateway-test",
            component_token=SecretStr(TOKEN),
            ingress_lock_directory=tmp_path,
        )
        app = create_app(settings=settings, file_execution_factory=lambda ingress, ledger: engine)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(10):
            while not server.started:
                if server_task.done():
                    await server_task
                    raise AssertionError("gateway failed to start")
                await asyncio.sleep(0.01)
        origin = f"http://127.0.0.1:{port}"
        identity = identity_for(LEASE, submit_intent_id="intent-worker")
        async with httpx.AsyncClient(
            base_url=origin, headers={"Authorization": f"Bearer {TOKEN}"}
        ) as client:
            submitted = await client.post(
                "/internal/v1/executions/submit", json=submit_body(identity, contract_command())
            )
            assert submitted.status_code == 200, submitted.text
        command = BindingRetirementCommand(
            binding_id="synthetic",
            revision="1",
            request_id="disable-worker-test",
            actor_type="anonymous",
            credential_label=None,
            key_digest=None,
            intent_digest="sha256:" + "d" * 64,
            mode="disable",
            reason="synthetic",
        )
        receipt = await asyncio.to_thread(PostgresBindingLifecycle(lifecycle_pool).retire, command)
        assert receipt.status == "disabled" and receipt.revision_id == "binding-test"
        root = db.query("SELECT event_id FROM speech_binding_disable_intents")
        assert len(root) == 1
        event_id = root[0]["event_id"]
        config = BindingDisableWorkerSettings(
            SPEECHNEXUS_PROCESS_ROLE="binding-disable-worker",
            SPEECHNEXUS_DISABLE_DATABASE_URL=make_conninfo(
                db.url, user=scanner, password="synthetic_settlement_test"
            ),
            SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL=make_conninfo(
                db.url, user=settlement, password="synthetic_settlement_test"
            ),
            SPEECHNEXUS_DATABASE_SCHEMA=db.schema,
            SPEECHNEXUS_DISABLE_GATEWAY_ID="gateway-test",
            SPEECHNEXUS_DISABLE_NODE_ID="host-test",
            SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL=origin,
            SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL=TOKEN,
            SPEECHNEXUS_DISABLE_GATEWAY_TIMEOUT_SECONDS=3,
            SPEECHNEXUS_DISABLE_STORAGE_TIMEOUT_SECONDS=3,
            SPEECHNEXUS_DISABLE_POLL_SECONDS=10,
            SPEECHNEXUS_DISABLE_BATCH_SIZE=10,
            SPEECHNEXUS_DISABLE_WAIT_TIMEOUT_MS=1000,
        )
        passes: asyncio.Queue[DisablePassResult] = asyncio.Queue()
        real_pass = BindingDisableCoordinator.run_pass
        real_query = GatewayExecutionClient.query_disable_target
        queried: list[str] = []

        async def observed_pass(coordinator: BindingDisableCoordinator) -> DisablePassResult:
            result = await real_pass(coordinator)
            passes.put_nowait(result)
            return result

        async def observed_query(
            client: GatewayExecutionClient, target: DisableExecutionTarget
        ) -> object:
            queried.append(target.submit_intent_id)
            return await real_query(client, target)

        monkeypatch.setattr(BindingDisableCoordinator, "run_pass", observed_pass)
        monkeypatch.setattr(GatewayExecutionClient, "query_disable_target", observed_query)

        async def one_worker_pass() -> DisablePassResult:
            task = asyncio.create_task(run(config))
            try:
                result = await asyncio.wait_for(passes.get(), timeout=10)
                assert not task.done()
                return result
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        first = await one_worker_pass()
        assert first.attempted == 1 and first.unavailable == 0
        assert engine.aborted == [("intent-worker", "resource_force_disabled", 1000)]
        target_rows = db.query(
            "SELECT event_id,submit_intent_id,operation_id,attempt_id,lease_id,"
            "stage_id,workspace_id,pool_id,gateway_id,host_node_id,instance_id,instance_generation,"
            "binding_revision_id,policy_revision_id,voice_revision_id,fencing_token,command_digest,"
            "wait_timeout_ms FROM speech_binding_disable_targets "
            "WHERE event_id=%s",
            (event_id,),
        )
        assert target_rows == [
            {
                "event_id": event_id,
                "submit_intent_id": "intent-worker",
                "operation_id": "operation-test",
                "attempt_id": "attempt-alt",
                "lease_id": LEASE,
                "stage_id": identity.stage_id,
                "workspace_id": identity.workspace_id,
                "pool_id": identity.pool_id,
                "gateway_id": identity.gateway_id,
                "host_node_id": identity.host_node_id,
                "instance_id": identity.instance_id,
                "instance_generation": identity.instance_generation,
                "binding_revision_id": identity.binding_revision_id,
                "policy_revision_id": identity.policy_revision_id,
                "voice_revision_id": identity.voice_revision_id,
                "fencing_token": identity.fencing_token,
                "command_digest": command_digest(contract_command()),
                "wait_timeout_ms": 1000,
            }
        ]
        assert (
            db.query(
                "SELECT count(*) AS n FROM speech_binding_disable_abort_intents "
                "WHERE event_id=%s AND submit_intent_id='intent-worker'",
                (event_id,),
            )[0]["n"]
            == 1
        )
        assert (
            db.query(
                "SELECT count(*) AS n FROM gateway_abort_intents "
                "WHERE submit_intent_id='intent-worker'"
            )[0]["n"]
            == 1
        )
        second = await one_worker_pass()
        assert second.queried == 1 and second.attempted == 1 and second.unavailable == 0
        assert queried == ["intent-worker"]
        assert engine.aborted == [("intent-worker", "resource_force_disabled", 1000)]
        intent = db.query(
            "SELECT execution_state,cleanup_state FROM submit_intents "
            "WHERE submit_intent_id='intent-worker'"
        )[0]
        assert intent == {"execution_state": "unknown_quarantined", "cleanup_state": "quarantined"}
        lease = db.query("SELECT lease_state FROM admission_leases WHERE lease_id=%s", (LEASE,))[0]
        assert lease["lease_state"] != "released"
    finally:
        if server is not None and server_task is not None:
            server.should_exit = True
            await server_task
        lifecycle_pool.close()
        for role in (scanner, settlement):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')
