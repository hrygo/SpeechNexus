"""Production fixed GET, independently provisioned SELECT role and real PG/TCP."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from speechnexus_contracts.pronunciation_read_http import validate_response
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.pronunciation_examples import pronunciation_request
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, speech_pool
from speechnexus_backend.speech.interfaces.http.pronunciations import OPERATION, PATH
from speechnexus_backend.speech.interfaces.pronunciation_drafts import prepare_pronunciation_draft

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
GET = PATH.format(pronunciation_id="pron-synthetic", revision="1")
TABLES = (
    "speech_pronunciation_commands",
    "speech_pronunciation_families",
    "speech_pronunciation_revisions",
)


@dataclass
class ReadDatabase:
    url: str = field(repr=False)
    schema: str
    role: str
    admin: SpeechConnection = field(repr=False)
    settings: Settings = field(repr=False)

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return cursor.fetchall() if cursor.description else []

    def snapshot(self) -> dict[str, Any]:
        return {table: self.query(f"SELECT * FROM {table} ORDER BY 1,2") for table in TABLES}

    def app(self, **changes: Any) -> Any:
        return create_app(settings=self.settings.model_copy(update=changes))

    def wait_reader(self) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = self.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE usename=%s "
                "AND cardinality(pg_blocking_pids(pid))>0",
                (self.role,),
            )
            if rows[0]["n"]:
                return
            time.sleep(0.01)
        pytest.fail("No actual blocked pronunciation SELECT observed")


@pytest.fixture
def read_db() -> Iterator[ReadDatabase]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Configure isolated PostgreSQL; missing environment is not qualification")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_pron_" + suffix, "sn_pron_reader_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    admin.execute(f'SET search_path TO "{schema}",pg_catalog')
    admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_pron_reader'")
    try:
        admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        admin.execute(f'GRANT SELECT ON "{schema}".speech_pronunciation_revisions TO "{role}"')
        # Synthetic fixture creation uses the existing private port as the
        # isolated test administrator, never the product's read-only identity.
        with speech_pool(url, schema=schema) as seed_pool:
            # Pin only the synthetic fixture allocator, not a public create API.
            from speechnexus_backend.speech.infrastructure import pronunciation_drafts_sql as sql
            from speechnexus_backend.speech.infrastructure.pronunciation_drafts_postgres import (
                _Cursor,
            )

            prepared = prepare_pronunciation_draft(pronunciation_request(), "synthetic-seed-key")
            with seed_pool.connection() as seed, seed.cursor() as raw:
                cursor = _Cursor(raw, time.monotonic() + 3)
                first = sql.create_draft(cursor, prepared, cursor.clock, lambda: "pron-synthetic")
                assert first.outcome == "created"
        settings = Settings(
            **{
                **_settings().model_dump(),
                "database_schema": schema,
                "pronunciation_reader_database_url": make_conninfo(
                    url, user=role, password="synthetic_pron_reader"
                ),
                "pronunciation_read_concurrency": 1,
                "pronunciation_read_timeout_seconds": 1,
                "asset_allowed_hosts": ("testserver", "test", "127.0.0.1"),
            }
        )
        yield ReadDatabase(url, schema, role, admin, settings)
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.execute(f'DROP OWNED BY "{role}"')
        admin.execute(f'DROP ROLE "{role}"')
        admin.close()


def test_read_only_role_history_restart_and_absence_do_not_mutate_facts(
    read_db: ReadDatabase,
) -> None:
    db, app = read_db, read_db.app()
    before = db.snapshot()
    runtime = app.state.pronunciation_runtime
    assert runtime.provider is None
    with TestClient(app) as client:
        response = client.get(GET)
        assert response.status_code == 200, response.text
        original = response.json()
        assert original["status"] == "draft" and original["revision"] == "1"
        assert original["entries"] == pronunciation_request()["entries"]
        assert client.get(GET[:-1] + "99").status_code == 404
        with runtime._pool.connection() as connection:
            assert connection.execute("SELECT SESSION_USER, CURRENT_USER").fetchone() == {
                "session_user": db.role,
                "current_user": db.role,
            }
        old_pool = runtime._pool
    assert runtime.provider is None and old_pool.closed
    with TestClient(app) as restarted:
        assert runtime._pool is not old_pool and restarted.get(GET).json() == original
    with TestClient(db.app()) as rebuilt:
        assert rebuilt.get(GET).json() == original
    assert db.snapshot() == before


@pytest.mark.parametrize(
    "privilege",
    [
        "superuser",
        "startup_owner",
        "schema_owner",
        "table_owner_member",
        "insert",
        "update_column",
        "other_table",
        "missing_select",
    ],
)
def test_nonminimal_login_and_effective_identities_rejected_before_exposure(
    read_db: ReadDatabase,
    privilege: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = read_db
    updates: dict[str, Any] = {}
    if privilege in ("superuser", "startup_owner"):
        updates["pronunciation_reader_database_url"] = SecretStr(
            make_conninfo(
                db.url,
                options=f"-c role={db.role}" if privilege == "startup_owner" else "",
            )
        )
    elif privilege == "schema_owner":
        db.query(f'ALTER SCHEMA "{db.schema}" OWNER TO "{db.role}"')
    elif privilege == "table_owner_member":
        owner = db.query("SELECT CURRENT_USER AS name")[0]["name"]
        db.query(f'ALTER ROLE "{db.role}" NOINHERIT')
        db.query(f'GRANT "{owner}" TO "{db.role}"')
    elif privilege == "missing_select":
        db.query(f'REVOKE SELECT ON speech_pronunciation_revisions FROM "{db.role}"')
    else:
        grant = {
            "insert": "INSERT ON speech_pronunciation_revisions",
            "update_column": "UPDATE (definition_json) ON speech_pronunciation_revisions",
            "other_table": "SELECT ON speech_pronunciation_commands",
        }[privilege]
        db.query(f'GRANT {grant} TO "{db.role}"')
    before = db.snapshot()
    app = db.app(**updates)
    filters = list(logging.getLogger("psycopg.pool").filters)
    with pytest.raises(ValueError, match="Pronunciation reader startup rejected"), TestClient(app):
        pytest.fail("Nonminimal reader accepted")
    runtime = app.state.pronunciation_runtime
    assert runtime.provider is None and runtime._pool.closed
    assert list(logging.getLogger("psycopg.pool").filters) == filters
    assert db.snapshot() == before
    assert "synthetic_pron_reader" not in caplog.text


def test_pool_pressure_closed_and_corrupt_history_are_503_not_404(read_db: ReadDatabase) -> None:
    db = read_db
    app = db.app(pronunciation_read_timeout_seconds=0.05)
    before = db.snapshot()
    with TestClient(app) as client:
        pool = app.state.pronunciation_runtime._pool
        with pool.connection():
            for path in (GET, GET[:-1] + "99"):
                response = client.get(path)
                assert response.status_code == 503 and response.json()["error"]["retryable"] is True
        assert client.get(GET).status_code == 200
        assert db.snapshot() == before
        db.query(
            "UPDATE speech_pronunciation_revisions SET definition_json=%s",
            ('{"synthetic_corrupt":true}',),
        )
        corrupt = db.snapshot()
        response = client.get(GET)
        assert response.status_code == 503 and "synthetic_corrupt" not in response.text
        assert db.snapshot() == corrupt
        pool.close()
        assert client.get(GET).status_code == 503


def test_sql_read_deadline_retains_history_and_recovers_same_revision(
    read_db: ReadDatabase,
) -> None:
    db = read_db
    before = db.snapshot()
    with TestClient(db.app(pronunciation_read_timeout_seconds=0.05)) as client:
        with psycopg.connect(db.url) as blocker:
            blocker.execute(
                f'LOCK TABLE "{db.schema}".speech_pronunciation_revisions IN ACCESS EXCLUSIVE MODE'
            )
            for path in (GET, GET[:-1] + "99"):
                response = client.get(path)
                assert response.status_code == 503
                assert response.json()["error"]["code"] == "pronunciation_unavailable"
        assert client.get(GET).status_code == 200
        assert client.get(GET[:-1] + "99").status_code == 404
    assert db.snapshot() == before


def test_later_startup_failure_closes_reader_and_removes_diagnostic_filter(
    read_db: ReadDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speechnexus_backend.asset_runtime import AssetRuntime

    def fail(_: AssetRuntime) -> None:
        raise RuntimeError("synthetic later startup failure")

    monkeypatch.setattr(AssetRuntime, "open", fail)
    app = read_db.app(
        object_storage_access_key=SecretStr("synthetic"),
        object_storage_secret_key=SecretStr("synthetic"),
    )
    filters = list(logging.getLogger("psycopg.pool").filters)
    before = read_db.snapshot()
    with pytest.raises(RuntimeError, match="synthetic later startup failure"), TestClient(app):
        pytest.fail("Failed asset startup accepted")
    runtime = app.state.pronunciation_runtime
    assert runtime.provider is None and runtime._pool.closed
    assert list(logging.getLogger("psycopg.pool").filters) == filters
    assert read_db.snapshot() == before


async def test_cancel_real_blocked_select_retains_capacity_and_original_facts(
    read_db: ReadDatabase,
) -> None:
    db, app = read_db, read_db.app(pronunciation_read_timeout_seconds=3)
    before = db.snapshot()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        blocker = psycopg.connect(db.url)
        blocker.execute(
            f'LOCK TABLE "{db.schema}".speech_pronunciation_revisions IN ACCESS EXCLUSIVE MODE'
        )
        task = asyncio.create_task(client.get(GET))
        try:
            await asyncio.to_thread(db.wait_reader)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert (await client.get(GET)).status_code == 429
            task.cancel()
            assert (await client.get(GET)).status_code == 429
        finally:
            blocker.rollback()
            blocker.close()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (await client.get(GET)).status_code == 200
    assert db.snapshot() == before and app.state.pronunciation_runtime._pool.closed


def test_generated_consumers_read_actual_loopback_http(read_db: ReadDatabase) -> None:
    db, app = read_db, read_db.app()
    before = db.snapshot()
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
            with httpx.Client(base_url=base, trust_env=False, timeout=3) as client:
                with app.state.pronunciation_runtime._pool.connection():
                    unavailable = client.get(GET)
                    assert unavailable.status_code == 503
                    validate_response(
                        OPERATION,
                        503,
                        {
                            "body": unavailable.json(),
                            "headers": dict(unavailable.headers),
                        },
                    )
                for path, status in ((GET, 200), (GET[:-1] + "99", 404)):
                    response = client.get(path)
                    assert response.status_code == status
                    validate_response(
                        OPERATION,
                        status,
                        {"body": response.json(), "headers": dict(response.headers)},
                    )
            script = """
import assert from 'node:assert/strict';
import {createValidatedClient} from './packages/contracts/src/pronunciation_read-http.ts';
const client=createValidatedClient(process.argv[1],4194304);
const path='/v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision}';
const options={params:{path:{pronunciation_id:'pron-synthetic',revision:'1'}}};
const first=await client.GET(path,options);
if(process.argv[2]==='unavailable'){
  assert.equal(first.response.status,503);
  assert.equal(first.error.error.code,'pronunciation_unavailable');
  assert.equal(first.error.error.retryable,true);
  assert.equal(Object.hasOwn(first.error.error,'nexus'),false);
  console.log('Actual unavailable fixed read passed');
}else{
assert.equal(first.response.status,200);
assert.equal(first.data.id,'pron-synthetic');
assert.equal(first.data.revision,'1');
assert.equal(first.data.status,'draft');
assert.equal(first.data.entries[0].grapheme,'SpeechNexus');
assert.deepEqual((await client.GET(path,options)).data,first.data);
const missingOptions={params:{path:{pronunciation_id:'pron-synthetic',revision:'99'}}};
const missing=await client.GET(path,missingOptions);
assert.equal(missing.response.status,404);
assert.equal(missing.error.error.code,'pronunciation_not_found');
assert.equal(missing.response.headers.get('x-should-retry'),'false');
console.log('Actual pronunciation GET and confirmed absence passed');
}
"""
            with app.state.pronunciation_runtime._pool.connection():
                pressure = subprocess.run(
                    ["node", "--input-type=module", "-e", script, base, "unavailable"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            assert pressure.returncode == 0, pressure.stdout + pressure.stderr
            assert "unavailable fixed read passed" in pressure.stdout
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, base],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "confirmed absence passed" in result.stdout
        finally:
            server.should_exit = True
            worker.join(timeout=10)
            assert not worker.is_alive()
    assert db.snapshot() == before and app.state.pronunciation_runtime._pool.closed
