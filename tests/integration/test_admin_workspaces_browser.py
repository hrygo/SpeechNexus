"""Production Admin consumes A's real HTTP/SQL on disposable PostgreSQL only.

This is a qualification fixture, not a C product import of speech internals.
Explicit service injection does not qualify AssetRuntime/S3 or full migrations.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg.conninfo import make_conninfo
from starlette.responses import Response

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.workspaces import WorkspaceService
from speechnexus_backend.speech.infrastructure.asset_schema import ASSET_DDL
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.workspaces_postgres import PostgresWorkspaceReads

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PREFIX = "/v1/nexus/workspaces"


async def test_admin_real_workspace_http_pg_browser() -> None:
    dsn = os.environ.get("SPEECHNEXUS_WORKBENCH_TEST_DSN")
    if not dsn:
        pytest.skip("disposable workbench PG probe not configured")
    assert (ROOT / "web/admin/dist/index.html").is_file(), "run pnpm build first"
    schema, role = "c_admin_" + uuid4().hex, "c_reader_" + uuid4().hex
    output = Path(
        os.environ.get("SPEECHNEXUS_ADMIN_WORKSPACES_OUTPUT", ROOT / ".runtime/team-c/admin-live")
    )
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as owner:
        guard = await (
            await owner.execute(
                "SELECT current_database(),current_user,current_setting('listen_addresses'),"
                "current_setting('speechnexus.disposable_workbench_probe',true)"
            )
        ).fetchone()
        assert guard == ("speechnexus_workbench_test", "workbench_test_owner", "", "on"), (
            "refusing a database outside the disposable Unix-socket-only probe"
        )
        pool = speech_pool(make_conninfo(dsn, user=role), schema=schema, max_size=2)
        sock = socket.socket()
        task: asyncio.Task[None] | None = None
        server: uvicorn.Server | None = None
        try:
            await owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            await owner.execute(
                "SELECT set_config('search_path',%s,false)", (schema + ",pg_catalog,pg_temp",)
            )
            # The exact frozen 0003 table and seeded default, not a duplicate schema.
            for statement in ASSET_DDL[:2]:
                await owner.execute(statement)
            await owner.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
            await owner.execute(
                sql.SQL("GRANT SELECT ON {}.speech_workspaces TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
            async with owner.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO speech_workspaces VALUES (%s,%s,%s,7,9007199254740993,1)",
                    [
                        (
                            f"workspace-{i:03}",
                            f"<script>共享😀 {i:03}</script>",
                            "disabled" if i == 1 else "active",
                        )
                        for i in range(102)
                    ],
                )
            pool.open(wait=True)
            with pool.connection() as connection:
                privileges = connection.execute(
                    "SELECT current_user AS principal, "
                    "has_schema_privilege(%s,'CREATE') AS ddl, "
                    "has_table_privilege('speech_workspaces','SELECT') AS can_read, "
                    "has_table_privilege('speech_workspaces','INSERT,UPDATE,DELETE') AS can_write",
                    (schema,),
                ).fetchone()
            assert privileges == {
                "principal": role,
                "ddl": False,
                "can_read": True,
                "can_write": False,
            }
            sock.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
            settings = Settings(
                _env_file=None,
                database_url=make_conninfo(dsn, user=role),
                object_storage_endpoint="http://127.0.0.1:1",
                object_storage_healthcheck_url="http://127.0.0.1:1",
                object_storage_bucket="synthetic-workspace-test",
                object_storage_access_key=None,
                object_storage_secret_key=None,
                asset_allowed_hosts=("127.0.0.1",),
                asset_allowed_origins=(origin,),
            )
            app = create_app(
                settings=settings, workspaces=WorkspaceService(PostgresWorkspaceReads(pool))
            )
            requests: list[dict[str, Any]] = []
            deleted = False

            @app.middleware("http")
            async def delete_fixture_before_real_get(request: Request, call_next: Any) -> Response:
                nonlocal deleted
                if request.url.path.startswith(PREFIX):
                    # This owner-only fixture mutation simulates a concurrent deletion.
                    # The response still comes from the actual A handler and SQL reader.
                    if request.url.path == PREFIX + "/workspace-000" and not deleted:
                        await owner.execute(
                            "DELETE FROM speech_workspaces WHERE workspace_id='workspace-000'"
                        )
                        deleted = True
                    response = await call_next(request)
                    requests.append(
                        {
                            "method": request.method,
                            "path": request.url.path,
                            "cursor": request.query_params.get("cursor"),
                            "status": response.status_code,
                        }
                    )
                    return response  # type: ignore[no-any-return]
                return await call_next(request)  # type: ignore[no-any-return]

            app.mount("/", StaticFiles(directory=ROOT / "web/admin/dist", html=True))
            server = uvicorn.Server(
                uvicorn.Config(app, log_level="error", access_log=False, lifespan="on")
            )
            task = asyncio.create_task(server.serve(sockets=[sock]))
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise AssertionError("HTTP server stopped before startup")
                    await asyncio.sleep(0.005)
            process = await asyncio.create_subprocess_exec(
                "node",
                "scripts/dev-env/admin-workspaces-live-probe.mjs",
                "--url",
                origin,
                "--output",
                str(output),
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
                assert process.returncode == 0, (stdout + stderr).decode()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            result = json.loads((output / "result.json").read_text())
            assert result["ids_seen"] == 103 and result["writes"] == 0
            assert result["real_http_pg"] is True and result["asset_runtime_s3"] is False
            assert deleted and len(requests) >= 5
            assert all(r["method"] == "GET" for r in requests)
            assert sum(r["status"] == 404 for r in requests) == 1
            assert sum(r["path"] == PREFIX for r in requests) >= 2
            # Real authority stays empty when its last rows disappear; no fabricated default.
            await owner.execute("DELETE FROM speech_workspaces")
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                empty = await client.get(PREFIX)
                missing = await client.get(PREFIX + "/workspace-default")
                assert empty.status_code == 200 and empty.json()["data"] == []
                assert empty.json()["has_more"] is False
                assert missing.status_code == 404
                assert missing.json()["error"]["code"] == "workspace_not_found"
            (output / "server-result.json").write_text(
                json.dumps(
                    {
                        "requests": requests,
                        "non_owner": True,
                        "runtime_select_only": True,
                        "empty_authority": True,
                        "missing_default": True,
                        "real_handlers_and_sql": True,
                        "asset_runtime_s3": False,
                        "full_migrations": False,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            if server is not None:
                server.should_exit = True
            try:
                if task is not None:
                    await asyncio.wait_for(task, 5)
            finally:
                sock.close()
                pool.close()
                await owner.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
                await owner.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
