"""The assistant surface in real Chromium against the real C runtime and private PG.

The honest state is a server verdict: a deployment that does not offer the assistant, and
one whose dependency is missing, must both leave the create form out of the production
bundle. The probe drives the built bundle behind the web tier and refuses to find a form.

Opt-in: needs the disposable PG probe, the built production bundle and a local Chromium
(`--browser`). No speech base is configured, so this is not speech qualification.
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


async def drive(database: Database, expected: str, name: str, **overrides: Any) -> dict[str, Any]:
    tier = Tier(database, **overrides)
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / name
    )
    output.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    async with (
        httpx.AsyncClient(base_url=tier.origin, timeout=30) as client,
        tier.running(),
    ):
        status = await client.get("/v1/workbench/status")
        assert status.status_code == 200, status.text
        body = status.json()
        states = {item["name"]: item["status"] for item in body["capabilities"]}
        # The browser is only asked to confirm what the deployment itself reports.
        assert states["assistant"] == expected, states
        assert body["llm_enabled"] is (expected == "unavailable"), body
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-assistant-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
            "--expect",
            expected,
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
    return events[-1]


async def test_real_runtime_assistant_surface_stays_closed_by_default(
    database: Database,
) -> None:
    result = await drive(database, "disabled", "real-runtime-assistant-disabled")
    assert result["capability"] == "disabled"
    assert result["createFormPresent"] is False
    assert result["turnRequests"] == 0
    assert result["capabilityAfterReload"] == "disabled"
    assert result["outbound"] == result["unexpectedErrors"] == 0
    assert result["failed"] == []


async def test_real_runtime_assistant_surface_names_the_missing_dependency(
    database: Database,
) -> None:
    result = await drive(
        database,
        "unavailable",
        "real-runtime-assistant-unavailable",
        # An approved intranet text LLM is configured; the speech base is still absent.
        llm_endpoint="http://127.0.0.1:59999/v1/chat/completions",
        llm_model="synthetic-local-model",
        llm_system_prompt="Synthetic intranet system prompt.",
        llm_qualification_reference="adr-0003-approved-local-intranet-text-llm",
    )
    assert result["capability"] == "unavailable"
    assert result["createFormPresent"] is False
    assert result["turnRequests"] == 0
    assert result["capabilityAfterReload"] == "unavailable"
    assert result["outbound"] == result["unexpectedErrors"] == 0
    assert result["failed"] == []
