"""Production Admin + profile composition/SQL, on a disposable PostgreSQL only.

The front tier injects transport faults after actual producer commits. No model,
published lifecycle, physical browser visibility or target-topology qualification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.probe_execution import ObservedProbe, require_probe_execution
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.profile_draft_schema import profile_draft_grants

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/v1/nexus/admin/profiles"
SCRIPT = "scripts/dev-env/admin-profiles-live-probe.mjs"


def output_dir() -> Path:
    return Path(
        os.environ.get("SPEECHNEXUS_ADMIN_PROFILES_OUTPUT", ROOT / ".runtime/team-c/admin-profiles")
    )


@pytest.fixture(autouse=True)
def require_profile_probe_execution(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if not os.environ.get("SPEECHNEXUS_WORKBENCH_TEST_DSN"):
        yield
        return
    observed: list[ObservedProbe] = []
    create_process = asyncio.create_subprocess_exec

    async def tracked(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await create_process(*args, **kwargs)
        observed.append(ObservedProbe(args, process))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracked)
    yield
    execution = require_probe_execution(SCRIPT, observed)
    output = output_dir()
    output.mkdir(parents=True, exist_ok=True)
    (output / "execution.json").write_text(json.dumps(execution, indent=2) + "\n")


def fault(code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "type": "service_unavailable_error" if status == 503 else "permission_error",
                "message": "PRIVATE_PROFILE_PROBE_DETAIL",
                "param": None,
                "request_id": "req-profile-browser",
                "retryable": False,
            }
        },
        status_code=status,
    )


def bind() -> tuple[socket.socket, str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock, f"http://127.0.0.1:{sock.getsockname()[1]}"


async def start(app: FastAPI, sock: socket.socket) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(10):
        while not server.started:
            if task.done():
                await task
                raise AssertionError("profile browser server stopped during startup")
            await asyncio.sleep(0.005)
    return server, task


async def test_admin_profiles_real_http_pg_browser() -> None:
    dsn = os.environ.get("SPEECHNEXUS_WORKBENCH_TEST_DSN")
    if not dsn:
        pytest.skip("disposable Unix-socket-only PostgreSQL probe not configured")
    assert (ROOT / "web/admin/dist/index.html").is_file(), "build the production Admin first"
    output = output_dir()
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    schema, role = "c_profiles_" + uuid4().hex, "c_profiles_writer_" + uuid4().hex
    sockets: list[socket.socket] = []
    servers: list[tuple[uvicorn.Server, asyncio.Task[None]]] = []
    async with await psycopg.AsyncConnection.connect(
        dsn, autocommit=True, row_factory=dict_row
    ) as owner:
        guard = await (
            await owner.execute(
                "SELECT current_database() AS db,current_user AS usr,"
                "current_setting('listen_addresses') AS tcp,"
                "current_setting('speechnexus.disposable_workbench_probe',true) AS disposable"
            )
        ).fetchone()
        assert guard == {
            "db": "speechnexus_workbench_test",
            "usr": "workbench_test_owner",
            "tcp": "",
            "disposable": "on",
        }, "refusing a shared or network PostgreSQL"
        engine = create_engine(
            "postgresql+psycopg://",
            connect_args=conninfo_to_dict(dsn),
            poolclass=NullPool,
            hide_parameters=True,
        )
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["schema"] = schema
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        requests: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        delayed_committed, release_delayed = asyncio.Event(), asyncio.Event()
        seen: dict[str, int] = {}
        external_created = False
        try:
            front_sock, origin = bind()
            api_sock, api_origin = bind()
            sockets.extend([front_sock, api_sock])
            for grant in profile_draft_grants(schema, role):
                await owner.execute(grant)
            settings = Settings(
                **{
                    **_settings().model_dump(),
                    "database_schema": schema,
                    "profile_writer_database_url": make_conninfo(dsn, user=role),
                    "asset_allowed_hosts": ("127.0.0.1",),
                    "asset_allowed_origins": (origin,),
                }
            )
            app = create_app(settings=settings)
            servers.append(await start(app, api_sock))
            assert app.state.profile_runtime.provider is not None

            def writer_identity() -> Any:
                with app.state.profile_runtime._pool.connection() as connection:
                    return connection.execute(
                        "SELECT current_user AS usr,"
                        "(SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS super,"
                        "has_schema_privilege(current_schema(),'CREATE') AS ddl,"
                        "has_table_privilege('speech_profile_revisions','DELETE') AS delete_history"
                    ).fetchone()

            assert await asyncio.to_thread(writer_identity) == {
                "usr": role,
                "super": False,
                "ddl": False,
                "delete_history": False,
            }
            front = FastAPI()
            async with httpx.AsyncClient(
                base_url=api_origin, trust_env=False, timeout=15
            ) as upstream:

                @front.api_route("/v1/{rest:path}", methods=["GET", "POST"])
                async def forward(rest: str, request: Request) -> Response:
                    nonlocal external_created
                    path = "/v1/" + rest
                    assert path.startswith(PREFIX)
                    raw = await request.body()
                    body: dict[str, Any] = json.loads(raw) if raw else {}
                    assert isinstance(body, dict)
                    key = request.headers.get("idempotency-key", "")
                    record: dict[str, Any] = {
                        "method": request.method,
                        "path": path,
                        "body": body,
                        "key": key,
                        "forwarded": True,
                    }
                    requests.append(record)
                    if request.method == "POST":
                        seen[key] = seen.get(key, 0) + 1
                        if body["profile_id"] == "browser-profile" and seen[key] == 2:
                            record.update(status=403, client_status=403, forwarded=False)
                            return fault("profile_origin_rejected", 403)
                        if (
                            body["profile_id"] == "browser-profile"
                            and seen[key] == 3
                            and body["expected_latest_revision"] is None
                            and not external_created
                        ):
                            other = await upstream.post(
                                PREFIX,
                                json={
                                    **body,
                                    "expected_latest_revision": "1",
                                    "display_name": "External revision",
                                },
                                headers={"Idempotency-Key": "external-profile-writer"},
                            )
                            assert other.status_code == 201 and other.json()["revision"] == "2"
                            external_created = True
                    response = await upstream.request(
                        request.method,
                        path,
                        content=raw,
                        headers={
                            k: v
                            for k, v in request.headers.items()
                            if k.lower() in {"origin", "content-type", "idempotency-key"}
                        },
                    )
                    record["status"] = response.status_code
                    record["client_status"] = response.status_code
                    if (
                        request.method == "POST"
                        and body["profile_id"] == "browser-profile"
                        and seen[key] == 1
                        and body["expected_latest_revision"] is None
                    ):
                        assert response.status_code == 201
                        record["committed_reply_dropped"] = True
                        record["client_status"] = 503
                        return fault("profile_result_unknown", 503)
                    if (
                        request.method == "POST"
                        and body["profile_id"] == "browser-late"
                        and seen[key] == 1
                    ):
                        assert response.status_code == 201
                        record["delayed_after_commit"] = True
                        delayed_committed.set()
                        await release_delayed.wait()
                    if request.method == "POST" and body["profile_id"] == "browser-foreign":
                        assert response.status_code == 201
                        record["wrong_identity_transport_injected"] = True
                        return JSONResponse(
                            {**response.json(), "profile_id": "unrelated-profile"}, status_code=201
                        )
                    return Response(
                        response.content,
                        status_code=response.status_code,
                        headers={
                            k: v
                            for k, v in response.headers.items()
                            if k.lower() in {"content-type", "cache-control"}
                        },
                    )

                front.mount("/", StaticFiles(directory=ROOT / "web/admin/dist", html=True))
                servers.append(await start(front, front_sock))
                process = await asyncio.create_subprocess_exec(
                    "node",
                    SCRIPT,
                    "--url",
                    origin,
                    "--output",
                    str(output),
                    cwd=ROOT,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stdout and process.stderr and process.stdin
                stderr = asyncio.create_task(process.stderr.read())
                try:
                    async with asyncio.timeout(90):
                        while line := await process.stdout.readline():
                            event = json.loads(line)
                            events.append(event)
                            if event["stage"] == "await-late-commit":
                                await delayed_committed.wait()
                                process.stdin.write(b"committed\n")
                                await process.stdin.drain()
                            elif event["stage"] == "hidden":
                                release_delayed.set()
                                process.stdin.write(b"released\n")
                                await process.stdin.drain()
                        assert await process.wait() == 0, (await stderr).decode()
                finally:
                    release_delayed.set()
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    (output / "stderr.log").write_bytes(await stderr)
            assert [e["stage"] for e in events] == ["await-late-commit", "hidden", "done"]
            result = json.loads((output / "result.json").read_text())
            assert result["real_profile_http_pg"] is True
            for name in (
                "unknown_survives_reload",
                "fixed_get_not_write_result",
                "ingress_refusal_not_write_result",
                "original_key_replay",
                "explicit_cas_conflict",
                "history_immutable",
                "late_reply_ignored",
                "storage_failure_zero_writes",
                "wrong_identity_rejected",
                "viewport360",
                "confirmation_focus",
            ):
                assert result[name] is True, name
            assert result["outbound"] == result["page_errors"] == 0
            posts = [r for r in requests if r["method"] == "POST"]
            assert len(posts) == 8
            assert posts[0]["body"] == posts[1]["body"] == posts[2]["body"]
            assert posts[0]["key"] == posts[1]["key"] == posts[2]["key"]
            assert posts[0]["committed_reply_dropped"] and posts[1]["status"] == 403
            assert posts[3]["status"] == 409 and posts[4]["status"] == 201
            assert posts[5]["body"] == posts[6]["body"] and posts[5]["key"] == posts[6]["key"]
            assert (
                posts[5]["delayed_after_commit"] and posts[7]["wrong_identity_transport_injected"]
            )
            revisions = await (
                await owner.execute(
                    sql.SQL(
                        "SELECT profile_id,revision FROM {}.speech_profile_revisions "
                        "ORDER BY profile_id,revision"
                    ).format(sql.Identifier(schema))
                )
            ).fetchall()
            assert revisions == [
                {"profile_id": "browser-foreign", "revision": "1"},
                {"profile_id": "browser-late", "revision": "1"},
                *[{"profile_id": "browser-profile", "revision": str(i)} for i in (1, 2, 3)],
            ]
            commands = await (
                await owner.execute(
                    sql.SQL(
                        "SELECT command_state,count(*) AS n FROM {}.speech_profile_commands "
                        "GROUP BY command_state ORDER BY command_state"
                    ).format(sql.Identifier(schema))
                )
            ).fetchall()
            assert commands == [
                {"command_state": "created", "n": 5},
                {"command_state": "rejected", "n": 1},
            ]
            (output / "server-result.json").write_text(
                json.dumps(
                    {
                        "real_composition_and_migrations": True,
                        "restricted_writer": True,
                        "injected_transport_faults": True,
                        "revisions": revisions,
                        "commands": commands,
                        "requests": [
                            {
                                "method": r["method"],
                                "path": r["path"],
                                "upstream_status": r["status"] if r["forwarded"] else None,
                                "client_status": r["client_status"],
                                "forwarded": r["forwarded"],
                                "key_sha256": hashlib.sha256(r["key"].encode()).hexdigest(),
                            }
                            for r in requests
                        ],
                        "speech_or_published_lifecycle": False,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            release_delayed.set()
            for server, task in reversed(servers):
                server.should_exit = True
                await asyncio.wait_for(task, 10)
            for sock in sockets:
                sock.close()
            await owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            await owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
