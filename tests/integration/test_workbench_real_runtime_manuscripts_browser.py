"""The authoring/proofreading surface against the real runtime in real Chromium.

The suite's other manuscripts probe keeps a vite preview and contract fixtures and is
never invoked, so until now the editor's own requests were never proven against a real
deployment. This one drives the production bundle at the real composition root: the page
creates its project and original transcript, commits revisions, loads a fixed historical
revision, saves from it, and keeps a local draft across a reload. Nothing is seeded and
nothing is stubbed; the only injected part is a long cleanup interval, unused here.

Opt-in: needs the disposable PG probe, the built production bundle and a local Chromium.
Speech stays absent, so this is not speech qualification.
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
from tests.integration.workbench_real_runtime import ROOT, Tier

pytestmark = pytest.mark.integration


async def test_real_runtime_authoring_page_commits_revisions(database: Database) -> None:
    tier = Tier(database)
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-manuscripts"
    )
    output.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    async with (
        httpx.AsyncClient(base_url=tier.origin, timeout=30) as client,
        tier.running(),
    ):
        status = await client.get("/v1/workbench/status")
        assert status.status_code == 200, status.text
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-manuscripts-real-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout and process.stderr
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            async with asyncio.timeout(180):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    events.append(event)
                    if event["stage"] == "done":
                        break
                assert await process.wait() == 0, (await stderr_task).decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            (output / "stderr.log").write_bytes(await stderr_task)
            (output / "events.json").write_text(
                json.dumps(events, ensure_ascii=False, indent=2) + "\n"
            )
    assert [e["stage"] for e in events] == ["done"]
    done = events[-1]
    assert done["kind"] == "production-browser-real-c-http-private-pg"
    assert done["revisionsWritten"] == 2
    assert done["historyLoadRestoresOriginal"] is True
    assert done["saveFromHistoryKeepsSource"] is True
    assert done["draftSurvivesReload"] is True
    assert done["failed"] == []
    assert done["outbound"] == done["unexpectedErrors"] == 0
