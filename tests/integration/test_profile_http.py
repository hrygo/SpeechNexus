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
from tests.support.profile_examples import profile_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend import profile_runtime as runtime_module
from speechnexus_backend.app import create_app
from speechnexus_backend.profile_runtime import ProfileRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.profile_draft_schema import profile_draft_grants
from speechnexus_backend.speech.interfaces.http.profile_ingress import CREATE_PATH as PATH

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
KEY = {"Idempotency-Key": "original-http-key"}
TABLES = ("speech_profile_commands", "speech_profile_families", "speech_profile_revisions")


@dataclass
class HTTPDatabase:
    db: JobDatabase
    role: str
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
        pytest.fail("No actual HTTP profile writer lock wait observed")


@pytest.fixture
def http_db(database: JobDatabase) -> Iterator[HTTPDatabase]:
    role = database.role.replace("sn_jobs_role_", "sn_profile_")
    database.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_profile_http'")
    try:
        for grant in profile_draft_grants(database.schema, role):
            database.query(grant)
        settings = Settings(
            **{
                **_settings().model_dump(),
                "database_schema": database.schema,
                "profile_writer_database_url": make_conninfo(
                    database.url, user=role, password="synthetic_profile_http"
                ),
                "asset_allowed_hosts": ("testserver", "test", "127.0.0.1"),
                "profile_receive_concurrency": 1,
                "profile_transaction_timeout_seconds": 1,
            }
        )
        yield HTTPDatabase(database, role, settings)
    finally:
        database.query(f'DROP OWNED BY "{role}"')
        database.query(f'DROP ROLE "{role}"')


def fixed(body: dict[str, Any], revision: str = "1") -> str:
    return str(PATH) + f"/{body['profile_id']}/revisions/{revision}"


def test_real_create_history_original_replay_conflicts_and_lifecycle(http_db: HTTPDatabase) -> None:
    body = profile_request()
    app = http_db.app()
    runtime = app.state.profile_runtime
    assert runtime.provider is None
    with TestClient(app) as client:
        first = client.post(PATH, json=body, headers=KEY)
        assert first.status_code == 201, first.text
        assert first.json()["publication"]["status"] == "draft" and "validation" not in first.json()
        assert client.get(fixed(body)).json() == first.json()
        assert client.get(fixed(body, "99")).status_code == 404
        before = http_db.snapshot()
        assert client.post(PATH, json=body, headers=KEY).json() == first.json()
        for changed in (
            {"display_name": "Changed"},
            {"profile_id": "other-profile"},
            {"expected_latest_revision": "1"},
        ):
            response = client.post(PATH, json={**body, **changed}, headers=KEY)
            assert (
                response.status_code == 409
                and response.json()["error"]["code"] == "idempotency_conflict"
            )
        assert http_db.snapshot() == before
        rejected = {**body, "expected_latest_revision": "2"}
        rejected_key = {"Idempotency-Key": "original-rejected-key"}
        assert client.post(PATH, json=rejected, headers=rejected_key).status_code == 409
        second = client.post(
            PATH,
            json={**body, "expected_latest_revision": "1"},
            headers={"Idempotency-Key": "second-key"},
        )
        assert second.status_code == 201 and second.json()["revision"] == "2"
        snapshot = http_db.snapshot()
        assert (
            client.post(PATH, json=rejected, headers=rejected_key).json()["error"]["code"]
            == "configuration_changed"
        )
        assert client.post(PATH, json=body, headers=KEY).json() == first.json()
        assert http_db.snapshot() == snapshot
        original_pool = runtime._pool
    assert runtime.provider is None and original_pool.closed
    # Same app/runtime can reopen with a fresh pool and original durable history.
    with TestClient(app) as restarted:
        assert runtime._pool is not original_pool
        assert restarted.get(fixed(body)).json() == first.json()
        assert restarted.post(PATH, json=body, headers=KEY).json() == first.json()
    # A separately built process composition also reads the same immutable revision.
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
    body, app = profile_request(), http_db.app(profile_transaction_timeout_seconds=0.05)
    headers = KEY if keyed else {}
    with TestClient(app) as client:
        pool = app.state.profile_runtime._pool
        with pool.connection() as connection:
            armed = connection
        armed.fault = "after"
        unknown = client.post(PATH, json=body, headers=headers)
        assert (
            unknown.status_code == 503
            and unknown.json()["error"]["code"] == "profile_result_unknown"
        )
        assert unknown.json()["error"]["retryable"] is False
        assert "nexus" not in unknown.json()["error"] and "location" not in unknown.headers
        before = http_db.snapshot()
        assert (
            len(before["speech_profile_revisions"]) == len(before["speech_profile_commands"]) == 1
        )
        with pool.connection():
            for path in (fixed(body), fixed(body, "99")):
                read = client.get(path)
                assert (
                    read.status_code == 503
                    and read.json()["error"]["code"] == "profile_unavailable"
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
    from speechnexus_backend.speech.infrastructure.profile_drafts_postgres import _Cursor
    from speechnexus_backend.speech.infrastructure.profile_drafts_sql import create_draft
    from speechnexus_backend.speech.interfaces.profile_drafts import prepare_profile_draft

    body = profile_request()
    with TestClient(http_db.app()) as client, ThreadPoolExecutor(max_workers=1) as workers:
        with http_db.db.blocker() as blocker:
            with blocker.cursor() as raw:
                cursor = _Cursor(raw, time.monotonic() + 3)
                original = create_draft(
                    cursor, prepare_profile_draft(body, KEY["Idempotency-Key"]), cursor.clock
                )
            replay = workers.submit(client.post, PATH, json=body, headers=KEY)
            http_db.waiters()
            assert not replay.done()
        response = replay.result(timeout=5)
        assert original.outcome == "created" and response.status_code == 201
    assert len(http_db.snapshot()["speech_profile_revisions"]) == 1


async def test_cancel_after_complete_request_keeps_original_transaction_and_permit(
    http_db: HTTPDatabase,
) -> None:
    app, body = http_db.app(profile_transaction_timeout_seconds=3), profile_request()
    # Start only our owned API lifetime, not any external worker or deployment.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        http_db.db.query(
            "INSERT INTO speech_profile_families (profile_id) VALUES (%s)",
            (body["profile_id"],),
        )
        blocker = http_db.db.blocker()
        try:
            blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
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
    assert len(http_db.snapshot()["speech_profile_commands"]) == 1
    assert app.state.profile_runtime.provider is None and app.state.profile_runtime._pool.closed


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

        app = http_db.app(profile_writer_database_url=SecretStr(db.url))
    else:
        table = f'"{db.schema}".speech_profile_revisions'
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
    with pytest.raises(ValueError, match="Profile writer startup rejected"), TestClient(app):
        pytest.fail("Startup with inappropriate writer authority was accepted")
    assert app.state.profile_runtime.provider is None
    assert app.state.profile_runtime._pool.closed
    assert http_db.snapshot() == before


def test_pool_constructor_failure_and_partial_lifespan_failure_cleanup(
    http_db: HTTPDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("psycopg.pool")
    before = list(logger.filters)
    runtime = ProfileRuntime(http_db.settings)
    with monkeypatch.context() as scoped:

        def fail(*_: Any, **__: Any) -> Any:
            raise ValueError("private-dsn-secret")

        scoped.setattr(runtime_module, "speech_pool", fail)
        with pytest.raises(ValueError, match="Profile writer startup rejected") as caught:
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
    # A later owned runtime's startup failure must close the already opened profile pool.
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
    assert app.state.profile_runtime._pool.closed
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
import { createValidatedClient } from "./packages/contracts/src/profile-http.ts";
const body=JSON.parse(process.argv[1]);
const api=createValidatedClient(process.argv[2],262144);
const path="/v1/nexus/admin/profiles";
const input={body,params:{header:{"Idempotency-Key":"ts-original-key"}}};
const first=await api.POST(path,input);
assert.equal(first.response.status,201);
assert.equal(first.data.publication.status,"draft");
assert.equal(Object.hasOwn(first.data,"validation"),false);
assert.deepEqual((await api.POST(path,input)).data,first.data);
const read=await api.GET(path+"/{profile_id}/revisions/{revision}",{
  params:{path:{profile_id:body.profile_id,revision:"1"}}});
assert.deepEqual(read.data,first.data);
const conflict=await api.POST(path,{...input,body:{...body,display_name:"Changed"}});
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
                    json.dumps(profile_request()),
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
    assert app.state.profile_runtime._pool.closed
    assert len(http_db.snapshot()["speech_profile_revisions"]) == 1


def test_http_concurrent_family_cas_persists_winner_and_loser(http_db: HTTPDatabase) -> None:
    body = profile_request()
    app = http_db.app(profile_receive_concurrency=2, profile_transaction_timeout_seconds=3)
    http_db.db.query(
        "INSERT INTO speech_profile_families (profile_id) VALUES (%s)", (body["profile_id"],)
    )
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as workers:
        blocker = http_db.db.blocker()
        try:
            blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
            keys = [{"Idempotency-Key": "cas-left-key"}, {"Idempotency-Key": "cas-right-key"}]
            requests = [workers.submit(client.post, PATH, json=body, headers=key) for key in keys]
            http_db.waiters(2)
            assert all(not request.done() for request in requests)
            blocker.rollback()
            responses = [request.result(timeout=5) for request in requests]
        finally:
            blocker.rollback()
            blocker.close()
        assert sorted(response.status_code for response in responses) == [201, 409]
        before = http_db.snapshot()
        for key, response in zip(keys, responses, strict=True):
            replay = client.post(PATH, json=body, headers=key)
            assert replay.status_code == response.status_code
            if response.status_code == 201:
                assert replay.json() == response.json()
            else:
                assert replay.json()["error"]["code"] == "configuration_changed"
        assert http_db.snapshot() == before and len(before["speech_profile_revisions"]) == 1


def test_sql_deadline_returns_unknown_without_committing_partial_command(
    http_db: HTTPDatabase,
) -> None:
    body = profile_request()
    http_db.db.query(
        "INSERT INTO speech_profile_families (profile_id) VALUES (%s)", (body["profile_id"],)
    )
    before = http_db.snapshot()
    with TestClient(http_db.app(profile_transaction_timeout_seconds=0.1)) as client:
        blocker = http_db.db.blocker()
        try:
            blocker.execute("SELECT * FROM speech_profile_families FOR UPDATE").fetchall()
            unknown = client.post(PATH, json=body, headers=KEY)
            assert unknown.status_code == 503 and unknown.json()["error"]["retryable"] is False
            assert http_db.snapshot() == before
        finally:
            blocker.rollback()
            blocker.close()
        # Only the caller's explicit original-key POST may attempt its original intent again.
        assert client.post(PATH, json=body, headers=KEY).status_code == 201
    assert len(http_db.snapshot()["speech_profile_commands"]) == 1
