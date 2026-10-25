"""Deletion page consumes real C HTTP/PG; only explicitly enabled browser probes run it."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import service
from tests.integration.test_workbench_http_postgres import http
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.app import create_app

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


async def test_browser_deletion_original_intents_complete_confirmation_and_cleanup(
    store: Store, cleanup: Cleanup
) -> None:
    dist = ROOT / "web/workbench/dist"
    assert (dist / "index.html").is_file(), "run pnpm build before --browser"
    app = create_app(
        store.app,
        service(store),
        default_workspace_id="workspace-demo",
        deployment_id="deletion-browser-synthetic",
        cursor_key=b"synthetic-cursor-key-32-bytes-only",
        max_json_bytes=4096,
        body_timeout_seconds=1,
    )
    lost: set[str] = set()
    writes: list[tuple[str, str | None, bytes]] = []

    @app.middleware("http")
    async def lose_committed_response(request: Request, call_next: Any) -> Response:
        path = request.url.path
        mutation = request.method == "POST" and path in {
            "/v1/workbench/deletion-plans",
            "/v1/workbench/deletions",
        }
        if mutation:
            writes.append((path, request.headers.get("Idempotency-Key"), await request.body()))
        response = await call_next(request)
        if mutation and path not in lost and response.status_code in {200, 202}:
            lost.add(path)
            return JSONResponse(
                {
                    "error": {
                        "code": "workbench_unavailable",
                        "type": "service_unavailable_error",
                        "message": "Synthetic committed reply loss.",
                        "param": None,
                        "request_id": "request-synthetic",
                        "retryable": True,
                    }
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return response  # type: ignore[no-any-return]

    app.mount("/", StaticFiles(directory=dist, html=True))
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "deletions"
    )
    output.mkdir(parents=True, exist_ok=True)
    async with http(store, application=app) as c:
        project = (await c.post("/v1/workbench/projects", json={"name": "合成待删除项目"})).json()
        transcript = (
            await c.post(
                f"/v1/workbench/projects/{project['id']}/transcripts",
                json={
                    "display_name": "合成待删除稿件",
                    "source": {"kind": "manual", "text": "合成需清理正文 e\u0301😀"},
                },
            )
        ).json()["transcript"]
        other = (await c.post("/v1/workbench/projects", json={"name": "合成独立项目"})).json()
        copy = (
            await c.post(
                f"/v1/workbench/projects/{other['id']}/transcripts",
                json={
                    "display_name": "合成独立稿件",
                    "source": {"kind": "manual", "text": "合成需清理正文 e\u0301😀"},
                },
            )
        ).json()["transcript"]
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-deletions-probe.mjs",
            "--url",
            str(c.base_url),
            "--output",
            str(output),
            cwd=ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout and process.stderr and process.stdin
        stderr_task = asyncio.create_task(process.stderr.read())
        events: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(75):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    events.append(event)
                    if event["stage"] == "done":
                        break
                    assert event["stage"] == "cleanup"
                    result = await cleanup.worker.run(event["deletionId"])
                    assert result is not None and len(result.deleted) == 2
                    assert (
                        await c.get("/v1/workbench/projects/" + project["id"])
                    ).status_code == 404
                    assert (
                        await c.get("/v1/workbench/transcripts/" + transcript["id"])
                    ).status_code == 404
                    assert (
                        await c.get("/v1/workbench/transcripts/" + copy["id"])
                    ).status_code == 200
                    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as conn:
                        row = await (
                            await conn.execute(
                                sql.SQL(
                                    "SELECT count(*) FROM {}.transcript_revisions "
                                    "WHERE transcript_id=%s"
                                ).format(sql.Identifier(store.schema)),
                                (transcript["id"],),
                            )
                        ).fetchone()
                        assert row == (0,)
                    process.stdin.write(b"continue\n")
                    await process.stdin.drain()
                assert await process.wait() == 0, (await stderr_task).decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            process.stdin.close()
            (output / "stderr.log").write_bytes(await stderr_task)
            (output / "events.json").write_text(
                json.dumps(events, ensure_ascii=False, indent=2) + "\n"
            )
        assert [e["stage"] for e in events] == ["cleanup", "done"]
        assert len(writes) == 5 and writes[0] == writes[1] and writes[3] == writes[4]
        assert len(lost) == 2
        assert events[-1]["outbound"] == events[-1]["unexpectedErrors"] == 0
