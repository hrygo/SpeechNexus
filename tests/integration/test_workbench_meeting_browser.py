"""Production Chromium consumes actual C HTTP and disposable socket-only PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_http_management import acceptance
from tests.integration.test_workbench_http_postgres import http
from tests.integration.test_workbench_meetings_postgres import deletion_service, meeting_cleanup
from tests.integration.test_workbench_meetings_postgres import meetings as meetings
from tests.integration.test_workbench_minutes_tasks_postgres import Llm
from tests.integration.test_workbench_minutes_tasks_postgres import tasks as tasks
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.llm import LlmCompletion, LlmRequest
from speechnexus_backend.workbench.application.meetings import WorkbenchMeetings
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks
from speechnexus_backend.workbench.infrastructure.postgres_minutes_tasks import PostgresMinutesTasks

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


class BrowserLlm(Llm):
    async def generate(self, request: LlmRequest) -> LlmCompletion:
        return replace(
            await super().generate(request), text="合成纪要 <script>not executed</script>"
        )


async def test_browser_original_intents_real_http_pg_and_safe_minutes(
    store: Store, meetings: WorkbenchMeetings, tasks: PostgresMinutesTasks, cleanup: Cleanup
) -> None:
    # Explicit --browser requires a current production build; absence is a failure, not a skip.
    dist = ROOT / "web/workbench/dist"
    assert (dist / "index.html").is_file(), "run pnpm build before --browser"
    app = create_app(
        store.app,
        deletion_service(store),
        meetings=meetings,
        default_workspace_id="workspace-demo",
        deployment_id="meeting-browser-synthetic",
        cursor_key=b"synthetic-cursor-key-32-bytes-only",
        max_json_bytes=4096,
        body_timeout_seconds=1,
    )
    lost: set[str] = set()
    writes: list[tuple[str, str | None, bytes]] = []

    @app.middleware("http")
    async def lose_committed_response(request: Request, call_next: Any) -> Response:
        path = request.url.path
        mutation = request.method == "POST" and "/meetings" in path
        if mutation:
            writes.append((path, request.headers.get("Idempotency-Key"), await request.body()))
        response = await call_next(request)
        suffix = path.rsplit("/", 1)[-1]
        if (
            mutation
            and suffix in {"seal", "minutes"}
            and suffix not in lost
            and response.status_code in {200, 202}
        ):
            lost.add(suffix)
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
        / "meetings"
    )
    output.mkdir(parents=True, exist_ok=True)
    async with http(store, application=app) as c:
        r = await c.post("/v1/workbench/projects", json={"name": "合成浏览器会议项目"})
        assert r.status_code == 201, r.text
        project = r.json()
        r = await c.post(
            f"/v1/workbench/projects/{project['id']}/transcripts",
            json={
                "display_name": "合成浏览器会议稿",
                "source": {"kind": "manual", "text": " 合成原文 e\u0301😀\n "},
            },
        )
        assert r.status_code == 201, r.text
        transcript = r.json()["transcript"]
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-meetings-probe.mjs",
            "--url",
            str(c.base_url),
            "--output",
            str(output),
            cwd=ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert (
            process.stdout is not None and process.stderr is not None and process.stdin is not None
        )
        stderr_task = asyncio.create_task(process.stderr.read())
        events: list[dict[str, Any]] = []
        llm = BrowserLlm(store)
        try:
            async with asyncio.timeout(75):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        assert await process.wait() == 0, (await stderr_task).decode()
                        break
                    event = json.loads(line)
                    events.append(event)
                    stage = event["stage"]
                    if stage == "done":
                        assert await process.wait() == 0, (await stderr_task).decode()
                        break
                    if stage == "disable_llm":
                        # Injected fixture only: no production configuration or control route.
                        meetings._llm = None
                    elif stage == "execute":
                        result = await MinutesTasks(tasks, llm).execute(event["minutesId"])
                        assert result.task.status == "succeeded" and len(llm.calls) == 1
                    elif stage == "stale":
                        await store.app.revise_transcript(
                            transcript_id=transcript["id"],
                            expected_revision=2,
                            text="另一份合成校对",
                        )
                    elif stage == "delete":
                        preview = await c.post(
                            "/v1/workbench/deletion-plans",
                            headers={"Idempotency-Key": "browser-delete-preview"},
                            json={
                                "mode": "cascade",
                                "target": {
                                    "owner": "workbench",
                                    "kind": "project",
                                    "resource": {"id": project["id"], "revision": "1"},
                                },
                            },
                        )
                        assert preview.status_code == 200, preview.text
                        deleted = await c.post(
                            "/v1/workbench/deletions",
                            json=acceptance(preview.json()),
                            headers={"Idempotency-Key": "browser-delete"},
                        )
                        assert deleted.status_code == 202, deleted.text
                        worker = await meeting_cleanup(store, cleanup)
                        cleaned = await worker.run(deleted.json()["id"])
                        assert cleaned is not None
                        current = await c.get("/v1/workbench/deletions/" + deleted.json()["id"])
                        assert current.json()["cleanup_state"] == "complete"
                    else:
                        raise AssertionError("unknown browser stage")
                    process.stdin.write(b"continue\n")
                    await process.stdin.drain()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            process.stdin.close()
            (output / "stderr.log").write_bytes(await stderr_task)
            (output / "events.json").write_text(
                json.dumps(events, ensure_ascii=False, indent=2) + "\n"
            )
        assert [e["stage"] for e in events] == ["disable_llm", "execute", "stale", "delete", "done"]
        assert len(writes) == 5 and writes[1] == writes[2] and writes[3] == writes[4]
        assert lost == {"seal", "minutes"}
        assert events[-1]["unexpectedErrors"] == 0 and events[-1]["outbound"] == 0
