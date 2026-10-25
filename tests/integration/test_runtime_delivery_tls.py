"""Actual A outbox→TLS→native B/SQLite; only the daemon/template are synthetic."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from tests.fake.runtime_controller import TLS_COMMAND, TLS_REQUEST
from tests.integration.test_production_runtime_intents import RuntimeDatabase
from tests.integration.test_runtime_controller_client import (
    controller_tls as controller_tls,
)
from tests.integration.test_runtime_controller_client import (
    database as database,
)
from tests.integration.test_runtime_controller_client import (
    foundation as foundation,
)
from tests.integration.test_runtime_controller_client import (
    runtime_database as runtime_database,
)
from tests.integration.test_runtime_controller_client import (
    tls_runtime as tls_runtime,
)
from tests.integration.test_runtime_delivery import CLAIM, prepared_delivery
from tests.support.runtime_controller_tls import TLSFiles, controller_server

from speechnexus_backend.runtime_delivery_worker import (
    RuntimeDeliveryRuntime,
    RuntimeDeliverySettings,
)
from speechnexus_backend.speech.application.runtime_delivery import RuntimeDeliveryService
from speechnexus_backend.speech.infrastructure.runtime_codec import request_to_wire
from speechnexus_backend.speech.infrastructure.runtime_controller_http import HttpRuntimeController
from speechnexus_backend.speech.infrastructure.runtime_delivery_schema import (
    ADOPT_RUNTIME_DELIVERIES_SQL,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_native_delivery_and_unknown_recovery_never_repeat_post_or_qualify_ready(
    tls_runtime: RuntimeDatabase,
    controller_tls: TLSFiles,
    tmp_path: Path,
    lost_reply: bool,
) -> None:
    with (
        prepared_delivery(tls_runtime, TLS_COMMAND) as delivery,
        controller_server(
            tmp_path / "native",
            controller_tls,
            lose_first_post_reply=lost_reply,
        ) as node,
    ):
        before = delivery.physical()
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            service = RuntimeDeliveryService(
                delivery.port, {TLS_REQUEST.binding.target_node_id: client}, retry_delay_us=100000
            )
            first = await service.run_once(CLAIM, limit=1)
            if lost_reply:
                assert first.unknown == 1 and first.acknowledged == 0
                db = tls_runtime.foundation.database
                assert (
                    db.query("SELECT delivery_phase FROM runtime_intent_deliveries")[0][
                        "delivery_phase"
                    ]
                    == "unknown"
                )
                db.query("UPDATE runtime_intent_deliveries SET next_attempt_at_us=0")
                second = await service.run_once(CLAIM, limit=1)
                assert second.acknowledged == 1
                assert node.methods == ["POST", "GET"]
            else:
                assert first.acknowledged == 1
                assert node.methods == ["POST"]
            assert len(node.runtime.prepared) == 1
            assert [json.loads(body) for body in node.request_bodies] == [
                request_to_wire(TLS_REQUEST)
            ]
        assert delivery.physical() == before
        # Transport ACK never invokes observe_runtime, readiness qualification or a stop.
        fact = tls_runtime.port.query_runtime_intent(TLS_REQUEST.identity).fact
        assert (
            fact is not None
            and fact.intent_state == "pending"
            and fact.observation_document is None
        )


async def test_native_original_404_does_not_prove_absence_or_allow_a_new_post(
    tls_runtime: RuntimeDatabase,
    controller_tls: TLSFiles,
    tmp_path: Path,
) -> None:
    with (
        prepared_delivery(tls_runtime, TLS_COMMAND) as delivery,
        controller_server(tmp_path / "native", controller_tls) as node,
    ):
        db = tls_runtime.foundation.database
        db.query("DELETE FROM runtime_intent_deliveries")
        db.query(ADOPT_RUNTIME_DELIVERIES_SQL, (tls_runtime.now(),))
        before = delivery.physical()
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            service = RuntimeDeliveryService(
                delivery.port, {TLS_REQUEST.binding.target_node_id: client}, retry_delay_us=100000
            )
            result = await service.run_once(CLAIM, limit=1)
        assert result.unknown == 1 and result.acknowledged == 0
        assert node.methods == ["GET"] and node.runtime.prepared == []
        assert delivery.physical() == before


async def test_native_stopped_generation_rejects_a_late_unseen_prepare(
    tls_runtime: RuntimeDatabase,
    controller_tls: TLSFiles,
    tmp_path: Path,
) -> None:
    with (
        prepared_delivery(tls_runtime, TLS_COMMAND) as delivery,
        controller_server(tmp_path / "native", controller_tls) as node,
    ):
        before = delivery.physical()
        async with HttpRuntimeController(
            node.endpoint, timeout_seconds=2, ssl_context=node.tls.client_context()
        ) as client:
            # An independently accepted node stop fences this generation even
            # before A observes it. The worker cannot revive the unseen prepare.
            stop = replace(TLS_REQUEST, intent_id="stop-before-prepare", action="stop")
            await client.submit(stop)
            service = RuntimeDeliveryService(
                delivery.port, {TLS_REQUEST.binding.target_node_id: client}, retry_delay_us=100000
            )
            result = await service.run_once(CLAIM, limit=1)
        assert result.unknown == 1 and result.acknowledged == 0
        assert node.runtime.prepared == []
        assert delivery.physical() == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("once", [True, False])
async def test_actual_worker_process_has_no_object_store_dependency_and_stops_without_remote_stop(
    tls_runtime: RuntimeDatabase,
    controller_tls: TLSFiles,
    tmp_path: Path,
    once: bool,
) -> None:
    with (
        prepared_delivery(tls_runtime, TLS_COMMAND) as delivery,
        controller_server(tmp_path / "native", controller_tls) as node,
    ):
        db = tls_runtime.foundation.database
        before = delivery.physical()
        environment = {
            **os.environ,
            "SPEECHNEXUS_PROCESS_ROLE": "job-worker",
            "SPEECHNEXUS_NODE_ID": "cloud-test",
            "SPEECHNEXUS_INSTANCE_ID": "runtime-delivery-test",
            "SPEECHNEXUS_RUNTIME_DELIVERY_DATABASE_URL": make_conninfo(
                db.url, user=delivery.role, password="synthetic_delivery_test"
            ),
            "SPEECHNEXUS_RUNTIME_DELIVERY_DATABASE_SCHEMA": db.schema,
            "SPEECHNEXUS_RUNTIME_DELIVERY_TARGET_NODE_ID": node.endpoint.node_id,
            "SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_ADDRESS": node.endpoint.address,
            "SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_PORT": str(node.endpoint.port),
            "SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_TOKEN": node.endpoint.bearer_token,
            "SPEECHNEXUS_RUNTIME_DELIVERY_CA_FILE": str(node.tls.certificate),
            "SPEECHNEXUS_RUNTIME_DELIVERY_BATCH_SIZE": "1",
            "SPEECHNEXUS_RUNTIME_DELIVERY_INTERVAL_SECONDS": "60",
        }
        args = [sys.executable, "-m", "speechnexus_backend.runtime_delivery_worker"]
        if once:
            args.append("--once")
        process = await asyncio.to_thread(
            subprocess.Popen,
            args,
            env=environment,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=10)
            result = json.loads(line)
            assert result == {
                "claimed": 1,
                "acknowledged": 1,
                "unknown": 0,
                "stale": 0,
                "blocked": 0,
            }
            if not once:
                assert process.poll() is None
                process.send_signal(signal.SIGTERM)
            _, diagnostic = await asyncio.to_thread(process.communicate, timeout=10)
            assert process.returncode == 0, diagnostic
            assert node.endpoint.bearer_token not in line + diagnostic
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.communicate, timeout=5)
        assert node.methods == ["POST"] and node.runtime.stopped == []
        assert delivery.physical() == before
        assert db.query(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s", (delivery.role,)
        ) == [{"n": 0}]


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
async def test_worker_root_rejects_migration_owner_before_contacting_controller(
    tls_runtime: RuntimeDatabase,
    controller_tls: TLSFiles,
    tmp_path: Path,
) -> None:
    db = tls_runtime.foundation.database
    with controller_server(tmp_path / "native", controller_tls) as node:
        settings = RuntimeDeliverySettings(
            process_role="job-worker",
            node_id="cloud-test",
            instance_id="owner-test",
            database_url=SecretStr(db.url),
            database_schema=db.schema,
            target_node_id=node.endpoint.node_id,
            controller_address=node.endpoint.address,
            controller_port=node.endpoint.port,
            controller_token=SecretStr(node.endpoint.bearer_token),
            ca_file=node.tls.certificate,
        )
        with pytest.raises(ValueError, match="non-owner"):
            async with RuntimeDeliveryRuntime(settings):
                pytest.fail("A migration owner became the runtime delivery principal")
        assert node.methods == [] and node.runtime.prepared == []


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize(
    "login_kind",
    ["superuser", "schema_owner", "owner_member", "session_authorization", "unprivileged"],
)
async def test_startup_role_does_not_hide_the_delivery_login_identity(
    tls_runtime: RuntimeDatabase,
    controller_tls: TLSFiles,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    login_kind: str,
) -> None:
    with (
        prepared_delivery(tls_runtime, TLS_COMMAND) as delivery,
        controller_server(tmp_path / "native", controller_tls) as node,
    ):
        db = tls_runtime.foundation.database
        login_role = db.schema + "_login"
        owner_role = db.schema + "_table_owner"
        created = False
        owner_created = False
        try:
            url = db.url
            session = db.query("SELECT SESSION_USER AS name")[0]["name"]
            if login_kind not in ("superuser", "session_authorization"):
                db.query(
                    f'CREATE ROLE "{login_role}" LOGIN '
                    "PASSWORD 'synthetic_delivery_login' NOINHERIT"
                )
                created = True
                db.query(f'GRANT "{delivery.role}" TO "{login_role}"')
                if login_kind == "schema_owner":
                    db.query(f'ALTER SCHEMA "{db.schema}" OWNER TO "{login_role}"')
                elif login_kind == "owner_member":
                    db.query(f'CREATE ROLE "{owner_role}" NOLOGIN')
                    owner_created = True
                    db.query(f'GRANT "{owner_role}" TO "{login_role}"')
                    db.query(f'ALTER TABLE runtime_intent_deliveries OWNER TO "{owner_role}"')
                    # NOINHERIT does not remove permission to SET ROLE to an owner.
                    assert db.query(
                        "SELECT pg_has_role(%s,%s,'USAGE') AS inherited, "
                        "pg_has_role(%s,%s,'MEMBER') AS member",
                        (login_role, owner_role, login_role, owner_role),
                    ) == [{"inherited": False, "member": True}]
                url = make_conninfo(db.url, user=login_role, password="synthetic_delivery_login")
                session = login_role
            url = make_conninfo(
                url,
                options=(
                    f"-c session_authorization={delivery.role}"
                    if login_kind == "session_authorization"
                    else f"-c role={delivery.role}"
                ),
                application_name="synthetic_delivery_login_canary",
            )
            with psycopg.connect(url) as probe:
                # PG resets session_authorization during authenticated startup;
                # this remains a rejection control, not a claimed bypass.
                observed_effective = (
                    session if login_kind == "session_authorization" else delivery.role
                )
                assert probe.execute("SELECT SESSION_USER, CURRENT_USER").fetchone() == (
                    session,
                    observed_effective,
                )
                probe.execute("SET ROLE NONE")
                assert probe.execute("SELECT CURRENT_USER").fetchone() == (session,)
                probe.execute("RESET ROLE")
                assert probe.execute("SELECT CURRENT_USER").fetchone() == (observed_effective,)
            settings = RuntimeDeliverySettings(
                process_role="job-worker",
                node_id="cloud-test",
                instance_id="identity-test",
                database_url=SecretStr(url),
                database_schema=db.schema,
                target_node_id=node.endpoint.node_id,
                controller_address=node.endpoint.address,
                controller_port=node.endpoint.port,
                controller_token=SecretStr(node.endpoint.bearer_token),
                ca_file=node.tls.certificate,
            )
            runtime = RuntimeDeliveryRuntime(settings)
            before = tls_runtime.snapshot()
            if login_kind == "unprivileged":
                async with runtime:
                    assert runtime.service is not None
                assert tls_runtime.snapshot() == before
            else:
                with pytest.raises(ValueError, match="non-owner") as caught:
                    async with runtime:
                        pass
                assert tls_runtime.snapshot() == before
                assert runtime.service is None
                for private in (
                    url,
                    "synthetic_delivery_login",
                    "synthetic_delivery_login_canary",
                    node.endpoint.bearer_token,
                ):
                    assert private not in str(caught.value) and private not in caplog.text
            assert runtime._pool.closed and runtime.service is None
            assert node.methods == [] and node.runtime.prepared == []
            assert db.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name=%s",
                ("synthetic_delivery_login_canary",),
            ) == [{"n": 0}]
        finally:
            if created:
                from psycopg import sql

                db.admin.execute(
                    sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                        sql.Identifier(db.schema), sql.Identifier(db.admin.info.user)
                    )
                )
                if owner_created:
                    db.admin.execute(
                        sql.SQL("ALTER TABLE {}.runtime_intent_deliveries OWNER TO {}").format(
                            sql.Identifier(db.schema), sql.Identifier(db.admin.info.user)
                        )
                    )
                db.query(f'DROP OWNED BY "{login_role}"')
                db.query(f'DROP ROLE "{login_role}"')
                if owner_created:
                    db.query(f'DROP OWNED BY "{owner_role}"')
                    db.query(f'DROP ROLE "{owner_role}"')


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
async def test_worker_failed_database_connection_does_not_print_private_sdk_diagnostics(
    database: object, tmp_path: Path
) -> None:
    from tests.integration.test_production_jobs import JobDatabase

    assert isinstance(database, JobDatabase)
    environment = {
        **os.environ,
        "SPEECHNEXUS_PROCESS_ROLE": "job-worker",
        "SPEECHNEXUS_NODE_ID": "cloud-test",
        "SPEECHNEXUS_INSTANCE_ID": "runtime-delivery-test",
        "SPEECHNEXUS_RUNTIME_DELIVERY_DATABASE_URL": make_conninfo(
            database.url, user=database.role, password="incorrect-synthetic-secret"
        ),
        "SPEECHNEXUS_RUNTIME_DELIVERY_DATABASE_SCHEMA": database.schema,
        "SPEECHNEXUS_RUNTIME_DELIVERY_TARGET_NODE_ID": "host-test",
        "SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_ADDRESS": "127.0.0.1",
        "SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_PORT": "8443",
        "SPEECHNEXUS_RUNTIME_DELIVERY_CONTROLLER_TOKEN": "synthetic-private-node-token",
        "SPEECHNEXUS_RUNTIME_DELIVERY_STORAGE_TIMEOUT_SECONDS": "0.2",
    }
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "speechnexus_backend.runtime_delivery_worker", "--once"],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "Runtime delivery unavailable; original records retained.\n"
