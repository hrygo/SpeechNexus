"""Real HTTP handlers and independently owned minimum-role PostgreSQL writer."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, cast

import pytest
import uvicorn
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from tests.integration.test_production_jobs import ROOT, FaultConnection, JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.binding_examples import binding_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend import binding_runtime as runtime_module
from speechnexus_backend.app import create_app
from speechnexus_backend.binding_runtime import BindingRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.interfaces.http.binding_ingress import CREATE_PATH as PATH

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
KEY = {"Idempotency-Key": "original-http-key"}
TABLES = ("speech_binding_commands", "speech_binding_families", "speech_binding_revisions")


@dataclass
class HTTPDatabase:
    db: JobDatabase
    role: str
    reader_role: str
    settings: Settings

    def app(self, **updates: Any) -> Any:
        return create_app(settings=self.settings.model_copy(update=updates))

    def snapshot(self) -> dict[str, Any]:
        return {t: self.db.query(f"SELECT * FROM {t} ORDER BY 1,2") for t in TABLES}

    def waiters(self, minimum: int = 1) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (
                self.db.query(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                    "AND cardinality(pg_blocking_pids(pid))>0",
                    (self.role,),
                )[0]["n"]
                >= minimum
            ):
                return
            time.sleep(0.01)
        pytest.fail("No actual HTTP binding writer lock wait observed")


@pytest.fixture
def http_db(database: JobDatabase) -> Iterator[HTTPDatabase]:
    role = database.role.replace("sn_jobs_role_", "sn_binding_")
    reader_role = database.role.replace("sn_jobs_role_", "sn_binding_reader_")
    database.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_binding_http'")
    database.query(f"CREATE ROLE \"{reader_role}\" LOGIN PASSWORD 'synthetic_binding_reader'")
    try:
        for grant in binding_draft_grants(database.schema, role):
            database.query(grant)
        for grant in binding_admin_reader_grants(database.schema, reader_role):
            database.query(grant)
        settings = Settings(
            **{
                **_settings().model_dump(),
                "database_schema": database.schema,
                "binding_writer_database_url": make_conninfo(
                    database.url, user=role, password="synthetic_binding_http"
                ),
                "binding_admin_reader_database_url": make_conninfo(
                    database.url, user=reader_role, password="synthetic_binding_reader"
                ),
                "asset_allowed_hosts": ("testserver", "test", "127.0.0.1"),
                "binding_receive_concurrency": 1,
                "binding_transaction_timeout_seconds": 1,
            }
        )
        yield HTTPDatabase(database, role, reader_role, settings)
    finally:
        database.query(f'DROP OWNED BY "{reader_role}"')
        database.query(f'DROP ROLE "{reader_role}"')
        database.query(f'DROP OWNED BY "{role}"')
        database.query(f'DROP ROLE "{role}"')


def fixed(body: dict[str, Any], revision: str = "1") -> str:
    return str(PATH) + f"/{body['binding_id']}/revisions/{revision}"


def test_real_create_history_original_replay_conflict_and_lifecycle(http_db: HTTPDatabase) -> None:
    body, app = binding_request(), http_db.app()
    runtime = app.state.binding_runtime
    assert runtime.provider is None
    with TestClient(app) as client:
        first = client.post(PATH, json=body, headers=KEY)
        assert first.status_code == 201, first.text
        assert first.json()["publication"]["status"] == "draft"
        assert client.get(fixed(body)).json() == first.json()
        assert client.get(fixed(body, "99")).status_code == 404
        before = http_db.snapshot()
        assert client.post(PATH, json=body, headers=KEY).json() == first.json()
        for changes in (
            {"logical_model": "other-model"},
            {"binding_id": "other-family"},
            {"supersedes_revision": "1"},
        ):
            conflict = client.post(PATH, json={**body, **changes}, headers=KEY)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "idempotency_conflict"
        assert http_db.snapshot() == before
        second = client.post(
            PATH,
            json={**body, "supersedes_revision": "1"},
            headers={"Idempotency-Key": "second-key"},
        )
        assert second.status_code == 201 and second.json()["revision"] == "2"
        assert first.json()["revision_id"] != second.json()["revision_id"]
        snapshot = http_db.snapshot()
        assert client.post(PATH, json=body, headers=KEY).json() == first.json()
        original_pool = runtime._pool
    assert runtime.provider is None and original_pool.closed
    with TestClient(app) as restarted:
        assert runtime._pool is not original_pool
        assert restarted.get(fixed(body)).json() == first.json()
        assert restarted.post(PATH, json=body, headers=KEY).json() == first.json()
    with TestClient(http_db.app()) as rebuilt:
        assert rebuilt.get(fixed(body)).json() == first.json()
    assert http_db.snapshot() == snapshot and http_db.db.counts() == (0, 0)


def fault_pool(url: str, *, schema: str, max_size: int, name: str) -> SpeechPool:
    def configure(connection: FaultConnection) -> None:
        connection.execute(f'SET search_path TO "{schema}",pg_catalog,pg_temp')
        connection.commit()

    return cast(
        SpeechPool,
        ConnectionPool[FaultConnection](
            url,
            connection_class=FaultConnection,
            kwargs={"row_factory": dict_row},
            configure=configure,
            min_size=0,
            max_size=max_size,
            open=False,
            name=name,
        ),
    )


@pytest.mark.parametrize("keyed", [True, False])
def test_actual_commit_reply_loss_pressure_and_original_identity(
    http_db: HTTPDatabase,
    monkeypatch: pytest.MonkeyPatch,
    keyed: bool,
) -> None:
    monkeypatch.setattr(runtime_module, "speech_pool", fault_pool)
    body, app = binding_request(), http_db.app(binding_transaction_timeout_seconds=0.05)
    headers = KEY if keyed else {}
    with TestClient(app) as client:
        pool = app.state.binding_runtime._pool
        with pool.connection() as connection:
            armed = connection
        armed.fault = "after"
        unknown = client.post(PATH, json=body, headers=headers)
        assert (
            unknown.status_code == 503
            and unknown.json()["error"]["code"] == "binding_result_unknown"
        )
        assert unknown.json()["error"]["retryable"] is False
        assert "nexus" not in unknown.json()["error"] and "location" not in unknown.headers
        before = http_db.snapshot()
        assert (
            len(before["speech_binding_revisions"]) == len(before["speech_binding_commands"]) == 1
        )
        with pool.connection():
            for path in (fixed(body), fixed(body, "99")):
                read = client.get(path)
                assert (
                    read.status_code == 503
                    and read.json()["error"]["code"] == "binding_unavailable"
                )
            if keyed:
                response = client.post(PATH, json=body, headers=KEY)
                assert (
                    response.status_code == 503 and response.json()["error"]["retryable"] is False
                )
        assert http_db.snapshot() == before
        assert client.get(fixed(body)).status_code == 200
        assert client.get(fixed(body, "99")).status_code == 404
        if keyed:
            replay = client.post(PATH, json=body, headers=KEY)
            assert replay.status_code == 201 and replay.json()["revision"] == "1"
        # Keyless uncertainty is observed, never assigned a guessed recovery key.
        assert http_db.snapshot() == before and http_db.db.counts() == (0, 0)


def test_original_key_waits_for_real_transaction_then_replays(http_db: HTTPDatabase) -> None:
    from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import _Cursor
    from speechnexus_backend.speech.infrastructure.binding_drafts_sql import create_draft
    from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft

    body = binding_request()
    with TestClient(http_db.app()) as client, ThreadPoolExecutor(max_workers=1) as workers:
        with http_db.db.blocker() as blocker:
            with blocker.cursor() as raw:
                cursor = _Cursor(raw, time.monotonic() + 3)
                original = create_draft(
                    cursor,
                    prepare_binding_draft(body, KEY["Idempotency-Key"]),
                    cursor.clock,
                    lambda: "mbrev-first-writer",
                )
            replay = workers.submit(client.post, PATH, json=body, headers=KEY)
            http_db.waiters()
            assert not replay.done()
        response = replay.result(timeout=5)
        assert original.outcome == "created" and response.status_code == 201
    assert len(http_db.snapshot()["speech_binding_revisions"]) == 1


async def test_cancel_after_complete_request_keeps_original_transaction_and_permit(
    http_db: HTTPDatabase,
) -> None:
    app, body = http_db.app(binding_transaction_timeout_seconds=3), binding_request()
    # Start only our owned API lifetime, not any external worker or deployment.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        http_db.db.query(
            "INSERT INTO speech_binding_families (binding_id) VALUES (%s)",
            (body["binding_id"],),
        )
        blocker = http_db.db.blocker()
        try:
            blocker.execute("SELECT * FROM speech_binding_families FOR UPDATE").fetchall()
            task = asyncio.create_task(client.post(PATH, json=body, headers=KEY))
            await asyncio.to_thread(http_db.waiters)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            full = await client.post(PATH, json=body, headers=KEY)
            assert full.status_code == 429
            blocker.rollback()
            with pytest.raises(asyncio.CancelledError):
                await task
            replay = await client.post(PATH, json=body, headers=KEY)
            assert replay.status_code == 201 and replay.json()["revision"] == "1"
        finally:
            blocker.rollback()
            blocker.close()
    assert len(http_db.snapshot()["speech_binding_commands"]) == 1
    assert app.state.binding_runtime.provider is None and app.state.binding_runtime._pool.closed


@pytest.mark.parametrize(
    "privilege", ["owner", "missing_insert", "update_history", "delete", "truncate", "job_table"]
)
def test_startup_rejects_inappropriate_authority_without_writing_or_granting(
    http_db: HTTPDatabase,
    privilege: str,
) -> None:
    db, role = http_db.db, http_db.role
    if privilege == "owner":
        # Explicit real admin URL, rather than unrelated synthetic health settings.
        from pydantic import SecretStr

        app = http_db.app(binding_writer_database_url=SecretStr(db.url))
    else:
        table = f'"{db.schema}".speech_binding_revisions'
        statement = {
            "missing_insert": f'REVOKE INSERT ON {table} FROM "{role}"',
            "update_history": f'GRANT UPDATE (definition_json) ON {table} TO "{role}"',
            "delete": f'GRANT DELETE ON {table} TO "{role}"',
            "truncate": f'GRANT TRUNCATE ON {table} TO "{role}"',
            "job_table": f'GRANT SELECT ON "{db.schema}".coordination_jobs TO "{role}"',
        }[privilege]
        db.query(statement)
        app = http_db.app()
    before = http_db.snapshot()
    with pytest.raises(ValueError, match="Binding writer startup rejected"), TestClient(app):
        pytest.fail("Startup with inappropriate writer authority was accepted")
    assert app.state.binding_runtime.provider is None
    assert app.state.binding_runtime._pool.closed
    assert http_db.snapshot() == before


def test_pool_constructor_failure_and_partial_lifespan_failure_cleanup(
    http_db: HTTPDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("psycopg.pool")
    before = list(logger.filters)
    runtime = BindingRuntime(http_db.settings)
    with monkeypatch.context() as scoped:

        def fail(*_: Any, **__: Any) -> Any:
            raise ValueError("private-dsn-secret")

        scoped.setattr(runtime_module, "speech_pool", fail)
        with pytest.raises(ValueError, match="Binding writer startup rejected") as caught:
            runtime.open()
        assert "private-dsn-secret" not in str(caught.value)
    assert list(logger.filters) == before
    runtime.close()
    runtime.open()
    pool = runtime._pool
    runtime.close()
    runtime.close()
    assert pool is not None and pool.closed
    assert runtime.provider is None and list(logger.filters) == before
    # A later owned runtime's startup failure must close the already opened binding pool.
    import speechnexus_backend.app as app_module

    class FailedAssets:
        service, workspaces = object(), object()

        def __init__(self, _: Any) -> None:
            pass

        def open(self) -> None:
            raise RuntimeError("synthetic later lifecycle failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr(app_module, "AssetRuntime", FailedAssets)
    from pydantic import SecretStr

    app = http_db.app(object_storage_access_key=SecretStr("synthetic"))
    with pytest.raises(RuntimeError, match="synthetic later lifecycle failure"), TestClient(app):
        pytest.fail("Failed asset runtime accepted")
    assert app.state.binding_runtime._pool.closed
    assert list(logger.filters) == before


def test_generated_typescript_client_consumes_actual_loopback_http(http_db: HTTPDatabase) -> None:
    app = http_db.app()
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
            script = """
import assert from "node:assert/strict";
import { createValidatedClient } from "./packages/contracts/src/binding-http.ts";
const body=JSON.parse(process.argv[1]);
const api=createValidatedClient(process.argv[2],262144);
const path="/v1/nexus/admin/model-bindings";
const input={body,params:{header:{"Idempotency-Key":"ts-original-key"}}};
const first=await api.POST(path,input);
assert.equal(first.response.status,201);
assert.equal(first.data.publication.status,"draft");
assert.equal(Object.hasOwn(first.data,"validation"),false);
assert.deepEqual((await api.POST(path,input)).data,first.data);
const read=await api.GET(path+"/{binding_id}/revisions/{revision}",{
  params:{path:{binding_id:body.binding_id,revision:"1"}}});
assert.deepEqual(read.data,first.data);
const conflict=await api.POST(path,{...input,body:{...body,logical_model:"other-model"}});
assert.equal(conflict.response.status,409);
assert.equal(conflict.error.error.code,"idempotency_conflict");
console.log("Generated client: create/replay/pinned GET/conflict passed");
"""
            result = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    script,
                    json.dumps(binding_request()),
                    f"http://127.0.0.1:{listener.getsockname()[1]}",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "conflict passed" in result.stdout
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            assert not worker.is_alive()
    assert app.state.binding_runtime._pool.closed
    assert len(http_db.snapshot()["speech_binding_revisions"]) == 1


def test_http_concurrent_family_commands_both_append_without_cas(http_db: HTTPDatabase) -> None:
    body = binding_request()
    app = http_db.app(binding_receive_concurrency=2, binding_transaction_timeout_seconds=3)
    http_db.db.query(
        "INSERT INTO speech_binding_families (binding_id) VALUES (%s)", (body["binding_id"],)
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as workers:
        blocker = http_db.db.blocker()
        try:
            blocker.execute("SELECT * FROM speech_binding_families FOR UPDATE").fetchall()
            keys = [{"Idempotency-Key": "append-left-key"}, {"Idempotency-Key": "append-right-key"}]
            requests = [workers.submit(client.post, PATH, json=body, headers=key) for key in keys]
            http_db.waiters(2)
            assert all(not request.done() for request in requests)
            blocker.rollback()
            responses = [request.result(timeout=5) for request in requests]
        finally:
            blocker.rollback()
            blocker.close()
        assert {response.status_code for response in responses} == {201}
        assert {response.json()["revision"] for response in responses} == {"1", "2"}
        assert len({response.json()["revision_id"] for response in responses}) == 2
        before = http_db.snapshot()
        for key, response in zip(keys, responses, strict=True):
            assert client.post(PATH, json=body, headers=key).json() == response.json()
        assert http_db.snapshot() == before and http_db.db.counts() == (0, 0)


def test_sql_deadline_returns_unknown_without_committing_partial_command(
    http_db: HTTPDatabase,
) -> None:
    body = binding_request()
    http_db.db.query(
        "INSERT INTO speech_binding_families (binding_id) VALUES (%s)", (body["binding_id"],)
    )
    before = http_db.snapshot()
    with TestClient(http_db.app(binding_transaction_timeout_seconds=0.1)) as client:
        blocker = http_db.db.blocker()
        try:
            blocker.execute("SELECT * FROM speech_binding_families FOR UPDATE").fetchall()
            unknown = client.post(PATH, json=body, headers=KEY)
            assert unknown.status_code == 503 and unknown.json()["error"]["retryable"] is False
            assert http_db.snapshot() == before
        finally:
            blocker.rollback()
            blocker.close()
        # Only the caller's explicit original-key POST may attempt its original intent again.
        assert client.post(PATH, json=body, headers=KEY).status_code == 201
    assert len(http_db.snapshot()["speech_binding_commands"]) == 1


@pytest.mark.parametrize("login_kind", ["superuser", "schema_owner"])
def test_privileged_login_cannot_hide_behind_restricted_startup_role(
    http_db: HTTPDatabase,
    caplog: pytest.LogCaptureFixture,
    login_kind: str,
) -> None:
    import psycopg
    from pydantic import SecretStr

    db, binding_role = http_db.db, http_db.role
    session = db.query("SELECT SESSION_USER AS name")[0]["name"]
    login_role = binding_role + "_owner"
    created = False
    try:
        if login_kind == "schema_owner":
            db.query(
                f"CREATE ROLE \"{login_role}\" LOGIN PASSWORD 'synthetic_http_owner' NOINHERIT"
            )
            created = True
            db.query(f'GRANT "{binding_role}" TO "{login_role}"')
            db.query(f'ALTER SCHEMA "{db.schema}" OWNER TO "{login_role}"')
            url = make_conninfo(db.url, user=login_role, password="synthetic_http_owner")
            session = login_role
        else:
            url = db.url
        # Actual libpq startup role, not a mocked CURRENT_USER observation.
        url = make_conninfo(
            url, options=f"-c role={binding_role}", application_name="synthetic_login_canary"
        )
        with psycopg.connect(url) as probe:
            assert probe.execute("SELECT SESSION_USER, CURRENT_USER").fetchone() == (
                session,
                binding_role,
            )
            probe.execute("SET ROLE NONE")
            assert probe.execute("SELECT CURRENT_USER").fetchone() == (session,)
            # RESET restores the startup role; it does not reveal the login's authority.
            probe.execute("RESET ROLE")
            assert probe.execute("SELECT CURRENT_USER").fetchone() == (binding_role,)
        app = http_db.app(binding_writer_database_url=SecretStr(url))
        logger = logging.getLogger("psycopg.pool")
        filters = list(logger.filters)
        before = http_db.snapshot()
        with (
            pytest.raises(ValueError, match="Binding writer startup rejected") as caught,
            TestClient(app),
        ):
            pass
        runtime = app.state.binding_runtime
        assert runtime.provider is None and runtime._pool.closed
        assert list(logger.filters) == filters
        assert http_db.snapshot() == before and http_db.db.counts() == (0, 0)
        for secret in (url, "synthetic_http_owner", "synthetic_login_canary"):
            assert secret not in str(caught.value) and secret not in caplog.text
    finally:
        if created:
            from psycopg import sql

            db.admin.execute(
                sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                    sql.Identifier(db.schema), sql.Identifier(db.admin.info.user)
                )
            )
            db.query(f'DROP OWNED BY "{login_role}"')
            db.query(f'DROP ROLE "{login_role}"')


@pytest.mark.parametrize(
    "commit_original", [True, False], ids=["different-intent-conflict", "original-rollback"]
)
def test_concurrent_original_key_after_commit_or_rollback(
    http_db: HTTPDatabase, commit_original: bool
) -> None:
    from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import _Cursor
    from speechnexus_backend.speech.infrastructure.binding_drafts_sql import create_draft
    from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft

    body = binding_request()
    app = http_db.app(binding_transaction_timeout_seconds=3)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as workers:
        blocker = http_db.db.blocker()
        try:
            with blocker.cursor() as raw:
                cursor = _Cursor(raw, time.monotonic() + 3)
                original = create_draft(
                    cursor,
                    prepare_binding_draft(body, KEY["Idempotency-Key"]),
                    cursor.clock,
                    lambda: "mbrev-first-writer",
                )
            assert original.draft is not None
            pending_body = {**body, "logical_model": "other-model"} if commit_original else body
            pending = workers.submit(client.post, PATH, json=pending_body, headers=KEY)
            http_db.waiters()
            assert not pending.done()
            blocker.commit() if commit_original else blocker.rollback()
            response = pending.result(timeout=5)
        finally:
            blocker.rollback()
            blocker.close()
        snapshot = http_db.snapshot()
        assert (
            len(snapshot["speech_binding_commands"])
            == len(snapshot["speech_binding_revisions"])
            == 1
        )
        assert http_db.db.counts() == (0, 0)
        if commit_original:
            assert (
                response.status_code == 409
                and response.json()["error"]["code"] == "idempotency_conflict"
            )
            assert client.get(fixed(body)).json()["revision_id"] == original.draft.revision_id
        else:
            assert response.status_code == 201 and response.json()["revision"] == "1"
            assert response.json()["revision_id"] != original.draft.revision_id
            assert client.post(PATH, json=body, headers=KEY).json() == response.json()
        assert http_db.snapshot() == snapshot


@pytest.mark.parametrize("phase", ["before", "after"])
def test_successful_but_delayed_commit_is_unknown_then_original_replay(
    http_db: HTTPDatabase, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    saved_commit = FaultConnection.commit

    def delayed(connection: FaultConnection) -> None:
        if connection.fault == "delayed-success":
            connection.fault = None
            if phase == "before":
                time.sleep(0.15)
            saved_commit(connection)
            if phase == "after":
                time.sleep(0.15)
        else:
            saved_commit(connection)

    monkeypatch.setattr(FaultConnection, "commit", delayed)
    monkeypatch.setattr(runtime_module, "speech_pool", fault_pool)
    body = binding_request()
    app = http_db.app(binding_transaction_timeout_seconds=0.05)
    with TestClient(app) as client:
        pool = app.state.binding_runtime._pool
        with pool.connection() as connection:
            armed = connection
        armed.fault = "delayed-success"
        started = time.monotonic()
        unknown = client.post(PATH, json=body, headers=KEY)
        assert time.monotonic() - started >= 0.15
        assert (
            unknown.status_code == 503
            and unknown.json()["error"]["code"] == "binding_result_unknown"
        )
        assert unknown.json()["error"]["retryable"] is False
        before = http_db.snapshot()
        assert len(before["speech_binding_revisions"]) == 1
        replay = client.post(PATH, json=body, headers=KEY)
        assert replay.status_code == 201
        assert replay.json()["revision_id"] == before["speech_binding_revisions"][0]["revision_id"]
        assert http_db.snapshot() == before and http_db.db.counts() == (0, 0)


def test_actual_pg_http_preserves_large_integer_definition(http_db: HTTPDatabase) -> None:
    from decimal import Decimal

    from speechnexus_backend.speech.application.binding_documents import encode_json

    body = binding_request()
    body["artifact_set"][0]["size_bytes"] = 10**5000
    raw = encode_json(body, max_bytes=262144)
    with TestClient(http_db.app()) as client:
        response = client.post(
            PATH, content=raw, headers={**KEY, "Content-Type": "application/json"}
        )
        assert response.status_code == 201
        first = json.loads(response.content, parse_int=Decimal)
        assert first["artifact_set"][0]["size_bytes"] == body["artifact_set"][0]["size_bytes"]
        read = client.get(fixed(body))
        replay = client.post(PATH, content=raw, headers={**KEY, "Content-Type": "application/json"})
        assert read.content == replay.content == response.content
        assert http_db.db.counts() == (0, 0)


def test_binding_and_profile_use_distinct_minimum_roles_and_lifetimes(
    http_db: HTTPDatabase,
) -> None:
    from tests.support.profile_examples import profile_request

    from speechnexus_backend.speech.infrastructure.profile_draft_schema import profile_draft_grants

    db = http_db.db
    role = http_db.role.replace("sn_binding_", "sn_binding_peer_profile_")
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_peer_profile'")
    try:
        for grant in profile_draft_grants(db.schema, role):
            db.query(grant)
        settings = Settings(
            **{
                **http_db.settings.model_dump(),
                "profile_writer_database_url": make_conninfo(
                    db.url, user=role, password="synthetic_peer_profile"
                ),
            }
        )
        app = create_app(settings=settings)
        with TestClient(app) as client:
            binding = client.post(PATH, json=binding_request(), headers=KEY)
            profile = client.post("/v1/nexus/admin/profiles", json=profile_request(), headers=KEY)
            assert binding.status_code == profile.status_code == 201
            assert app.state.binding_runtime._pool is not app.state.profile_runtime._pool
            assert app.state.binding_runtime.provider is not app.state.profile_runtime.provider
            assert client.get(fixed(binding_request())).json() == binding.json()
        assert app.state.binding_runtime._pool.closed and app.state.profile_runtime._pool.closed
        assert db.counts() == (0, 0)
    finally:
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')
