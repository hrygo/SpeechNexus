"""The production bundle against the real C runtime: real Chromium, real lifespan, real PG.

Every other browser test in this suite assembles the ASGI app by hand with a synthetic
service set, and the probes that drive their own loopback replace the backend entirely.
This one runs the deployment's own composition root behind a web tier, so the ingress
boundary, the startup check and the deletion ledger replay are the real ones. The API is
restarted mid-flow on purpose: ADR 0005 requires owed erasure to be replayed before any
content is served, and this test refuses to run the worker by hand.

Opt-in: needs the disposable PG probe, the built production bundle and a local Chromium
(`--browser`). Speech stays absent, so this is not speech qualification.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.integration.test_workbench_runtime import Database
from tests.integration.test_workbench_runtime import database as database
from tests.integration.workbench_real_runtime import ROOT, Tier, erased

pytestmark = pytest.mark.integration


async def test_real_runtime_serves_the_deletion_page_and_replays_the_ledger(
    database: Database,
) -> None:
    tier = Tier(
        database,
        fault_suffixes=frozenset({"deletion-plans", "deletions"}),
        # Long enough that no sweep can race the page's "queued, awaiting cleanup" state.
        cleanup_interval_seconds=60,
    )
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-deletions"
    )
    output.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    async with (
        httpx.AsyncClient(base_url=tier.origin, timeout=30) as client,
        tier.running(),
    ):
        status = await client.get("/v1/workbench/status")
        assert status.status_code == 200, status.text
        project = (
            await client.post("/v1/workbench/projects", json={"name": "合成待删除项目"})
        ).json()
        transcript = (
            await client.post(
                f"/v1/workbench/projects/{project['id']}/transcripts",
                json={
                    "display_name": "合成待删除稿件",
                    "source": {"kind": "manual", "text": "合成需清理正文 e\u0301😀"},
                },
            )
        ).json()["transcript"]
        other = (await client.post("/v1/workbench/projects", json={"name": "合成独立项目"})).json()
        copy = (
            await client.post(
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
            tier.origin,
            "--output",
            str(output),
            cwd=ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout and process.stderr and process.stdin
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            async with asyncio.timeout(180):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    events.append(event)
                    if event["stage"] == "done":
                        break
                    assert event["stage"] == "cleanup"
                    # No hand-driven worker: restarting the real lifespan must replay
                    # the owed erasure before it serves content again.
                    await tier.restart_api()
                    cleared = False
                    for _ in range(150):
                        cleared = await erased(
                            database.owner_dsn, database.schema, transcript["id"]
                        )
                        if cleared:
                            break
                        await asyncio.sleep(0.1)
                    assert cleared, "the restarted runtime did not replay the owed erasure"
                    assert (
                        await client.get("/v1/workbench/transcripts/" + copy["id"])
                    ).status_code == 200
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
    assert tier.lost == {"/v1/workbench/deletion-plans", "/v1/workbench/deletions"}
    assert events[-1]["writeRequests"] == 5
    assert events[-1]["physicalCleanupCurrent"] is True
    assert events[-1]["outbound"] == events[-1]["unexpectedErrors"] == 0
