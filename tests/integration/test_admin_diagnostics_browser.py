"""The production AdminUI diagnostics page against the real C runtime and private PG.

The page reports the deployment's own verdict: it reads nothing until an operator asks,
then renders every capability the server sent, localizes the shared-deployment code, and
keeps the uncollected runtime facts (queues, budgets, leases, GPU) explicitly empty.

Opt-in: needs the disposable PG probe, the built admin bundle and a local Chromium
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


async def drive(
    database: Database, name: str, model: str | None, **overrides: Any
) -> dict[str, Any]:
    tier = Tier(database, bundle="web/admin/dist", **overrides)
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
        # The page is only asked to render what the deployment itself reports.
        assert body["llm_enabled"] is (model is not None), body
        assert len(body["capabilities"]) == 5, body
        assert {item["name"] for item in body["capabilities"]} == {
            "transcription",
            "meeting_minutes",
            "assistant",
            "teleprompter",
            "audiobook_export",
        }, body
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/admin-diagnostics-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
            "--expect-model",
            model if model is not None else "-",
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


async def test_admin_diagnostics_reports_the_unconfigured_deployment(database: Database) -> None:
    result = await drive(database, "admin-diagnostics-default", None)
    assert result["capabilityCards"] == 5
    assert result["statusReadMethods"] == ["GET"]
    assert result["modelShown"] == "disabled"
    assert result["outbound"] == result["unexpectedErrors"] == 0
    assert result["failed"] == []


async def test_admin_diagnostics_reports_the_configured_intranet_model(
    database: Database,
) -> None:
    result = await drive(
        database,
        "admin-diagnostics-llm",
        "synthetic-local-model",
        # An approved intranet text LLM is configured; the speech base is still absent.
        llm_endpoint="http://127.0.0.1:59999/v1/chat/completions",
        llm_model="synthetic-local-model",
        llm_system_prompt="Synthetic intranet system prompt.",
        llm_qualification_reference="adr-0003-approved-local-intranet-text-llm",
    )
    assert result["capabilityCards"] == 5
    assert result["statusReadMethods"] == ["GET"]
    assert result["modelShown"] == "synthetic-local-model"
    assert result["outbound"] == result["unexpectedErrors"] == 0
    assert result["failed"] == []
