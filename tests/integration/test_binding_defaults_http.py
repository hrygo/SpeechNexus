"""Actual producer HTTP with an independently verified four-table PG writer."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from speechnexus_contracts.binding_defaults_http import validate_response
from tests.integration.test_binding_http import HTTPDatabase, fault_pool
from tests.integration.test_binding_http import http_db as http_db
from tests.integration.test_production_jobs import ROOT
from tests.integration.test_production_jobs import database as database
from tests.support.binding_examples import binding_request

from speechnexus_backend import binding_runtime as runtime_module
from speechnexus_backend.app import create_app
from speechnexus_backend.binding_runtime import BindingDefaultsRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.binding_defaults_schema import (
    TABLE,
    binding_defaults_grants,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
PATH = "/v1/nexus/admin/model-bindings"
KEY = {"Idempotency-Key": "defaults-original-http-key"}
CREATE, GET = "createModelBindingDraft", "getModelBindingParameterDefaults"


def fixed(body: dict[str, Any], revision: str = "1") -> str:
    return PATH + f"/{body['binding_id']}/revisions/{revision}"


def snapshot_path(body: dict[str, Any], revision: str = "1") -> str:
    return fixed(body, revision) + "/parameter-defaults"


@dataclass
class DefaultsHTTPDatabase(HTTPDatabase):
    def snapshot(self) -> dict[str, Any]:
        return {**super().snapshot(), TABLE: self.db.query(f"SELECT * FROM {TABLE} ORDER BY 1,2")}


@pytest.fixture
def producer_db(http_db: HTTPDatabase) -> DefaultsHTTPDatabase:
    for grant in binding_defaults_grants(http_db.db.schema, http_db.role):
        http_db.db.query(grant)
    settings = Settings(
        **{
            **http_db.settings.model_dump(),
            "binding_writer_database_url": None,
            "binding_defaults_writer_database_url": http_db.settings.binding_writer_database_url,
        }
    )
    return DefaultsHTTPDatabase(http_db.db, http_db.role, settings)


@pytest.mark.parametrize("declaration", [None, {}, {"asr": {"max_segment_ms": 15000}}])
def test_actual_atomic_create_replay_fixed_reads_and_restart(
    producer_db: DefaultsHTTPDatabase,
    declaration: dict[str, Any] | None,
) -> None:
    body = binding_request()
    if declaration is not None:
        body["parameter_defaults"] = declaration
    app = producer_db.app()
    runtime = app.state.binding_defaults_runtime
    assert runtime.provider is None and app.state.binding_runtime is None
    with TestClient(app) as client:
        first = client.post(PATH, json=body, headers=KEY)
        assert first.status_code == 201, first.text
        assert first.json()["publication"]["status"] == "draft"
        assert client.get(fixed(body)).json() == first.json()
        stored = client.get(snapshot_path(body))
        assert stored.status_code == (404 if declaration is None else 200)
        if declaration is not None:
            validate_response(GET, 200, {"body": stored.json()})
            assert stored.json()["parameters"] == declaration
            assert stored.json()["binding_revision_id"] == first.json()["revision_id"]
        before = producer_db.snapshot()
        assert client.post(PATH, json=body, headers=KEY).json() == first.json()
        changed = {
            **body,
            "parameter_defaults": {} if declaration != {} else {"asr": {"rollback_tokens": 0}},
        }
        conflict = client.post(PATH, json=changed, headers=KEY)
        assert conflict.status_code == 409
        if declaration is not None:
            omitted = {k: v for k, v in body.items() if k != "parameter_defaults"}
            assert client.post(PATH, json=omitted, headers=KEY).status_code == 409
        assert producer_db.snapshot() == before
        assert client.get(snapshot_path(body, "99")).status_code == 404
    assert runtime.provider is None and runtime._pool.closed
    with TestClient(producer_db.app()) as client:
        assert client.post(PATH, json=body, headers=KEY).json() == first.json()
        if declaration is not None:
            assert client.get(snapshot_path(body)).json() == stored.json()
        else:
            assert client.get(snapshot_path(body)).status_code == 404
    assert producer_db.snapshot() == before and producer_db.db.counts() == (0, 0)


def test_legacy_mode_and_old_history_remain_explicit_without_backfill(
    http_db: HTTPDatabase,
) -> None:
    body, legacy = binding_request(), http_db.app()
    with TestClient(legacy) as client:
        assert client.post(PATH, json=body, headers=KEY).status_code == 201
        template = PATH + "/{binding_id}/revisions/{revision}/parameter-defaults"
        assert template not in client.get("/openapi.json").json()["paths"]
        assert (
            client.post(PATH, json={**body, "parameter_defaults": {}}, headers=KEY).status_code
            == 400
        )
    old = http_db.snapshot()
    for grant in binding_defaults_grants(http_db.db.schema, http_db.role):
        http_db.db.query(grant)
    settings = Settings(
        **{
            **http_db.settings.model_dump(),
            "binding_writer_database_url": None,
            "binding_defaults_writer_database_url": http_db.settings.binding_writer_database_url,
        }
    )
    with TestClient(create_app(settings=settings)) as client:
        assert client.get(snapshot_path(body)).status_code == 404
        assert client.post(PATH, json=body, headers=KEY).status_code == 201
    assert http_db.snapshot() == old and http_db.db.query(f"SELECT * FROM {TABLE}") == []
    # The legacy validator must continue refusing the broader four-table role.
    with (
        pytest.raises(ValueError, match="Binding writer startup rejected"),
        TestClient(http_db.app()),
    ):
        pytest.fail("Legacy writer accepted extra snapshot privileges")


@pytest.mark.parametrize("keyed", [False, True])
def test_committed_reply_loss_then_pressure_never_becomes_snapshot_absence(
    producer_db: DefaultsHTTPDatabase,
    monkeypatch: pytest.MonkeyPatch,
    keyed: bool,
) -> None:
    monkeypatch.setattr(runtime_module, "speech_pool", fault_pool)
    body = {**binding_request(), "parameter_defaults": {}}
    app = producer_db.app(binding_transaction_timeout_seconds=0.05)
    headers = KEY if keyed else {}
    with TestClient(app) as client:
        pool = app.state.binding_defaults_runtime._pool
        with pool.connection() as connection:
            armed = connection
        armed.fault = "after"
        unknown = client.post(PATH, json=body, headers=headers)
        assert unknown.status_code == 503 and unknown.json()["error"]["retryable"] is False
        assert "location" not in unknown.headers and "nexus" not in unknown.json()["error"]
        before = producer_db.snapshot()
        assert len(before[TABLE]) == len(before["speech_binding_revisions"]) == 1
        with pool.connection():
            for revision in ("1", "99"):
                unavailable = client.get(snapshot_path(body, revision))
                assert (
                    unavailable.status_code == 503
                    and unavailable.json()["error"]["retryable"] is True
                )
            if keyed:
                assert client.post(PATH, json=body, headers=KEY).status_code == 503
        restored = client.get(snapshot_path(body))
        assert restored.status_code == 200
        assert restored.json()["snapshot_id"] == before[TABLE][0]["snapshot_id"]
        if keyed:
            assert client.post(PATH, json=body, headers=KEY).json()["revision"] == "1"
        assert producer_db.snapshot() == before


@pytest.mark.parametrize("damage", ["missing", "parameters"])
def test_declared_missing_or_corrupt_child_is_503_and_original_never_reallocated(
    producer_db: DefaultsHTTPDatabase,
    damage: str,
) -> None:
    body = {**binding_request(), "parameter_defaults": {}}
    with TestClient(producer_db.app()) as client:
        assert client.post(PATH, json=body, headers=KEY).status_code == 201
        # Synthetic test-owner corruption only, never a product mutation endpoint.
        if damage == "missing":
            producer_db.db.query(f"DELETE FROM {TABLE}")
        else:
            producer_db.db.query(
                f"UPDATE {TABLE} SET parameters_json=%s", ('{"asr":{"rollback_tokens":0}}',)
            )
        before = producer_db.snapshot()
        missing = client.get(snapshot_path(body))
        assert (
            missing.status_code == 503 and missing.json()["error"]["code"] == "binding_unavailable"
        )
        assert client.post(PATH, json=body, headers=KEY).status_code == 503
        assert producer_db.snapshot() == before


async def test_cancelled_complete_post_retains_permit_and_atomic_original(
    producer_db: DefaultsHTTPDatabase,
) -> None:
    body = {**binding_request(), "parameter_defaults": {}}
    app = producer_db.app(binding_transaction_timeout_seconds=3)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        producer_db.db.query(
            "INSERT INTO speech_binding_families (binding_id) VALUES (%s)", (body["binding_id"],)
        )
        blocker = producer_db.db.blocker()
        try:
            blocker.execute("SELECT * FROM speech_binding_families FOR UPDATE").fetchall()
            pending = asyncio.create_task(client.post(PATH, json=body, headers=KEY))
            await asyncio.to_thread(producer_db.waiters)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            assert (await client.post(PATH, json=body, headers=KEY)).status_code == 429
            blocker.rollback()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert (await client.post(PATH, json=body, headers=KEY)).status_code == 201
            assert (await client.get(snapshot_path(body))).status_code == 200
        finally:
            blocker.rollback()
            blocker.close()
    facts = producer_db.snapshot()
    assert len(facts[TABLE]) == len(facts["speech_binding_commands"]) == 1
    assert app.state.binding_defaults_runtime.provider is None


@pytest.mark.parametrize(
    "privilege", ["owner", "missing_insert", "update_snapshot", "delete_snapshot", "job_table"]
)
def test_producer_startup_refuses_nonminimum_authority_before_exposing_provider(
    producer_db: DefaultsHTTPDatabase,
    privilege: str,
) -> None:
    db, role = producer_db.db, producer_db.role
    if privilege == "owner":
        app = producer_db.app(binding_defaults_writer_database_url=SecretStr(db.url))
    else:
        table = f'"{db.schema}".{TABLE}'
        db.query(
            {
                "missing_insert": f'REVOKE INSERT ON {table} FROM "{role}"',
                "update_snapshot": f'GRANT UPDATE (parameters_json) ON {table} TO "{role}"',
                "delete_snapshot": f'GRANT DELETE ON {table} TO "{role}"',
                "job_table": f'GRANT SELECT ON "{db.schema}".coordination_jobs TO "{role}"',
            }[privilege]
        )
        app = producer_db.app()
    before = producer_db.snapshot()
    with pytest.raises(ValueError, match="Binding writer startup rejected"), TestClient(app):
        pytest.fail("Nonminimum producer authority was exposed")
    runtime = app.state.binding_defaults_runtime
    assert runtime.provider is None and runtime._pool.closed
    assert producer_db.snapshot() == before


@pytest.mark.parametrize("login_kind", ["superuser", "schema_owner"])
def test_privileged_session_cannot_hide_with_restricted_producer_startup_role(
    producer_db: DefaultsHTTPDatabase,
    caplog: pytest.LogCaptureFixture,
    login_kind: str,
) -> None:
    db = producer_db.db
    login = producer_db.role + "_owner"
    created = False
    try:
        original_url = db.url
        if login_kind == "schema_owner":
            db.query(f"CREATE ROLE \"{login}\" LOGIN PASSWORD 'synthetic_defaults_owner' NOINHERIT")
            created = True
            db.query(f'GRANT "{producer_db.role}" TO "{login}"')
            db.query(f'ALTER SCHEMA "{db.schema}" OWNER TO "{login}"')
            original_url = make_conninfo(db.url, user=login, password="synthetic_defaults_owner")
        url = make_conninfo(
            original_url,
            options=f"-c role={producer_db.role}",
            application_name="producer-login-canary",
        )
        with psycopg.connect(url) as probe:
            identities = probe.execute("SELECT SESSION_USER,CURRENT_USER").fetchone()
            assert identities is not None
            session, current = identities
            assert session != current and current == producer_db.role
        app = producer_db.app(binding_defaults_writer_database_url=SecretStr(url))
        before = producer_db.snapshot()
        with (
            pytest.raises(ValueError, match="Binding writer startup rejected") as failure,
            TestClient(app),
        ):
            pass
        runtime = app.state.binding_defaults_runtime
        assert runtime.provider is None and runtime._pool.closed
        assert producer_db.snapshot() == before
        assert url not in caplog.text and "producer-login-canary" not in str(failure.value)
    finally:
        if created:
            from psycopg import sql

            db.admin.execute(
                sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                    sql.Identifier(db.schema), sql.Identifier(db.admin.info.user)
                )
            )
            db.query(f'DROP OWNED BY "{login}"')
            db.query(f'DROP ROLE "{login}"')


def test_constructor_failure_closes_owned_lifecycle_and_redacts_diagnostics(
    producer_db: DefaultsHTTPDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = BindingDefaultsRuntime(producer_db.settings)
    logger = logging.getLogger("psycopg.pool")
    filters = list(logger.filters)
    with monkeypatch.context() as scoped:

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("private-producer-dsn")

        scoped.setattr(runtime_module, "speech_pool", fail)
        with pytest.raises(ValueError) as failure:
            runtime.open()
        assert "private-producer-dsn" not in str(failure.value)
    assert runtime.provider is None and list(logger.filters) == filters
    runtime.open()
    pool = runtime._pool
    runtime.close()
    runtime.close()
    assert pool is not None and pool.closed and list(logger.filters) == filters


def test_generated_typescript_client_consumes_actual_loopback_http(
    producer_db: DefaultsHTTPDatabase,
) -> None:
    app, body = producer_db.app(), {**binding_request(), "parameter_defaults": {}}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            base = f"http://127.0.0.1:{listener.getsockname()[1]}"
            with httpx.Client(base_url=base) as client:
                created = client.post(PATH, json=body, headers=KEY)
                validate_response(CREATE, created.status_code, {"body": created.json()})
                stored = client.get(snapshot_path(body))
                validate_response(GET, stored.status_code, {"body": stored.json()})
                assert stored.status_code == 200 and stored.json()["parameters"] == {}
            script = """
import assert from "node:assert/strict";
import {createValidatedClient} from "./packages/contracts/src/binding_defaults-http.ts";
const body=JSON.parse(process.argv[1]);
const api=createValidatedClient(process.argv[2],262144);
const path="/v1/nexus/admin/model-bindings";
const input={body,params:{header:{"Idempotency-Key":"defaults-ts-original-key"}}};
const first=await api.POST(path,input);
assert.equal(first.response.status,201);
assert.deepEqual((await api.POST(path,input)).data,first.data);
const pinned=path+"/{binding_id}/revisions/{revision}/parameter-defaults";
const params={path:{binding_id:body.binding_id,revision:first.data.revision}};
const stored=await api.GET(pinned,{params});
assert.equal(stored.response.status,200);
assert.deepEqual(stored.data.parameters,{});
assert.equal(stored.data.binding_revision_id,first.data.revision_id);
const changed={...body,parameter_defaults:{asr:{rollback_tokens:0}}};
const conflict=await api.POST(path,{...input,body:changed});
assert.equal(conflict.response.status,409);
console.log("Actual producer: create/replay/snapshot/conflict passed");
"""
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, json.dumps(body), base],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            assert not worker.is_alive()
    assert len(producer_db.snapshot()[TABLE]) == 2
    assert app.state.binding_defaults_runtime._pool.closed
