"""The deployment teleprompter state in real Chromium against the real C runtime and PG.

The contract says an unsaved project is a 404 and that a missing state is not position
zero. This drives the production bundle's panel through read, write and an older-revision
conflict, then verifies the stored row over the real HTTP API.

Opt-in: needs the disposable PG probe, the built production bundle and a local Chromium
(`--browser`). No speech base is involved, so this is not speech qualification.
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


async def test_prompter_state_panel_reads_writes_and_refuses_an_older_revision(
    database: Database,
) -> None:
    tier = Tier(database)
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-prompter-state"
    )
    output.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    async with (
        httpx.AsyncClient(base_url=tier.origin, timeout=30) as client,
        tier.running(),
    ):
        project = (
            await client.post("/v1/workbench/projects", json={"name": "合成提词项目"})
        ).json()
        path = f"/v1/workbench/projects/{project['id']}/teleprompter"
        # Nothing is saved yet: the deployment answers 404, not an empty state.
        assert (await client.get(path)).status_code == 404
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-prompter-state-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
            "--project-name",
            project["name"],
            cwd=ROOT,
            stdin=asyncio.subprocess.DEVNULL,
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
                    assert event["stage"] == "done"
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
        # The refused older revision must not have moved what the deployment stores.
        stored = await client.get(path)
        assert stored.status_code == 200, stored.text
        body = stored.json()
        assert body["project_id"] == project["id"]
        assert body["script_revision"] == "2"
        assert body["visible_position"] == 42
        assert body["manual_hold"] is True
    result = events[-1]
    assert result["missingReportedAsMissing"] is True
    assert result["conflictRetried"] is False
    assert result["putRequests"] == ["2", "1"]
    assert result["outbound"] == result["unexpectedErrors"] == 0
    # The only non-2xx answers are the two this flow provokes on purpose: the 404 for a
    # project with nothing saved, and the 409 for the older script revision.
    assert sorted(item["status"] for item in result["failed"]) == [404, 409], result["failed"]
    assert all(item["path"].endswith("/teleprompter") for item in result["failed"])
