"""A real workbench deployment behind a web tier, for browser qualification.

The API app is not a static host: its contract ingress rejects bundle assets, so the
deployment serves the bundle from a web tier that proxies `/v1`. These helpers reproduce
that topology with the real composition root, and can restart the API alone so the real
startup path (ledger replay, startup check) runs while the browser session stays open.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
import psycopg
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_runtime import Database

from speechnexus_backend.workbench.runtime import create_runtime_app
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = "workspace-demo"
LOST_REPLY = {
    "error": {
        "code": "workbench_unavailable",
        "type": "service_unavailable_error",
        "message": "Synthetic committed reply loss.",
        "param": None,
        "request_id": "request-synthetic",
        "retryable": True,
    }
}


def bind() -> tuple[socket.socket, int]:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


def settings_for(database: Database, *, origin: str, **overrides: Any) -> WorkbenchSettings:
    base: dict[str, Any] = {
        "database_url": make_conninfo(database.owner_dsn, user=database.role),
        "database_schema": database.schema,
        "workspace_id": WORKSPACE,
        "deployment_id": "real-runtime-browser",
        "cursor_key": "synthetic-32-byte-cursor-key-test-only",
        # The deletion routes and the sweeper only exist when a cleanup identity is set.
        "cleanup_database_url": database.cleanup_dsn,
        # The web tier is a second origin in this harness, exactly as a reverse proxy is.
        "allowed_origins": (origin,),
    }
    return WorkbenchSettings(**{**base, **overrides})


class Tier:
    """One browser origin: a web tier in front of a real API that can be restarted."""

    def __init__(
        self,
        database: Database,
        *,
        fault_suffixes: frozenset[str] = frozenset(),
        bundle: str = "web/workbench/dist",
        **overrides: Any,
    ) -> None:
        self._bundle = bundle
        self._front_sock, front_port = bind()
        self.origin = f"http://127.0.0.1:{front_port}"
        self._settings = settings_for(database, origin=self.origin, **overrides)
        self._fault_suffixes = fault_suffixes
        self.lost: set[str] = set()
        self._upstream: httpx.AsyncClient | None = None
        self._api_base = ""
        self._api_sock: socket.socket | None = None
        self._api_task: asyncio.Task[None] | None = None
        self._api_server: uvicorn.Server | None = None

    async def start_api(self) -> None:
        """Enter the real lifespan: startup check, ledger replay, configured workers."""
        sock, port = bind()
        server = uvicorn.Server(
            uvicorn.Config(
                create_runtime_app(self._settings),
                log_level="error",
                access_log=False,
                lifespan="on",
            )
        )
        self._api_sock, self._api_server = sock, server
        self._api_base = f"http://127.0.0.1:{port}"
        self._api_task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(20):
            while not server.started:
                if self._api_task.done():
                    await self._api_task
                await asyncio.sleep(0.01)

    async def stop_api(self) -> None:
        if self._api_server is not None:
            self._api_server.should_exit = True
        if self._api_task is not None:
            with suppress(asyncio.CancelledError):
                await self._api_task
        if self._api_sock is not None:
            self._api_sock.close()

    async def restart_api(self) -> None:
        """Restart the API only: the web tier keeps serving the open browser session."""
        await self.stop_api()
        await self.start_api()

    async def proxy(self, rest: str, request: Request) -> Response:
        path = f"/v1/{rest}"
        committed = request.method == "POST" and path.rsplit("/", 1)[-1] in self._fault_suffixes
        assert self._upstream is not None
        forwarded = await self._upstream.request(
            request.method,
            f"{self._api_base}/v1/{rest}",
            params=request.query_params,
            content=await request.body(),
            headers={
                name: value
                for name, value in request.headers.items()
                if name.lower() in {"accept", "content-type", "idempotency-key", "origin"}
            },
        )
        if committed and path not in self.lost and forwarded.status_code in {200, 202}:
            # The web tier drops one committed reply per route, so the accepted write
            # really is replayed with its original key instead of being created again.
            self.lost.add(path)
            return JSONResponse(LOST_REPLY, status_code=503, headers={"Cache-Control": "no-store"})
        return Response(
            forwarded.content,
            forwarded.status_code,
            headers={
                name: value
                for name, value in forwarded.headers.items()
                if name.lower() in {"content-type", "cache-control"}
            },
        )

    @asynccontextmanager
    async def running(self) -> AsyncIterator[Tier]:
        dist = ROOT / self._bundle
        assert (dist / "index.html").is_file(), "run pnpm build before the browser test"
        await self.start_api()
        front = FastAPI()

        @front.get("/")
        async def index() -> FileResponse:
            return FileResponse(dist / "index.html")

        @front.api_route(
            "/v1/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
        )
        async def forward(rest: str, request: Request) -> Response:
            return await self.proxy(rest, request)

        front.mount("/assets", StaticFiles(directory=dist / "assets"))
        server = uvicorn.Server(
            uvicorn.Config(front, log_level="error", access_log=False, lifespan="off")
        )
        task = asyncio.create_task(server.serve(sockets=[self._front_sock]))
        try:
            for _ in range(1500):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started, "the web tier did not start"
            async with httpx.AsyncClient(base_url=self._api_base, timeout=30) as upstream:
                self._upstream = upstream
                yield self
        finally:
            self._upstream = None
            server.should_exit = True
            with suppress(asyncio.CancelledError):
                await task
            self._front_sock.close()
            await self.stop_api()


async def revision_rows(owner_dsn: str, schema: str, transcript_id: str) -> int | None:
    async with await psycopg.AsyncConnection.connect(owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL(
                    "SELECT count(*) FROM {}.transcript_revisions WHERE transcript_id=%s"
                ).format(sql.Identifier(schema)),
                (transcript_id,),
            )
        ).fetchone()
    return None if row is None else int(row[0])


async def erased(owner_dsn: str, schema: str, transcript_id: str) -> bool:
    return await revision_rows(owner_dsn, schema, transcript_id) == 0
