"""The realtime tab in the production bundle, against a real C runtime.

The page tells the operator that the microphone opens only after an explicit step. This drives
the production bundle against a deployment with no realtime base and checks that promise, the
keyboard reachability of the form and the 360px layout.

Opt-in: needs the disposable PG probe, the built production bundle and a local Chromium
(`--browser`). No speech base and no real microphone are involved: the probe counts requests
instead of granting a device, so this is not device, capture or speech qualification.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from tests.integration.test_workbench_runtime import Database
from tests.integration.test_workbench_runtime import database as database
from tests.integration.workbench_real_runtime import ROOT, Tier

pytestmark = pytest.mark.integration

REACHABLE = [
    "会话预设编号",
    "预设版本",
    "实时转写模型编号",
    "模型版本",
    "创建实时会话",
]


async def test_the_realtime_tab_never_opens_the_microphone_without_a_session(
    database: Database,
) -> None:
    tier = Tier(database)
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-realtime-consent"
    )
    output.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    async with tier.running():
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-realtime-consent-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
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

    assert events, "the probe must report what it did"
    result = events[-1]
    assert result["stage"] == "done"
    assert result["focusOnOpen"] is True
    assert result["consentCopyShown"] is True
    # The create form is reachable by keyboard, in order.
    assert result["formReachable"] == result["expectedForm"] == REACHABLE
    # Before a session exists the page does not even offer capture, and it never asks for the
    # microphone — not on open, not after asking for a session this deployment cannot serve.
    assert result["captureControlBeforeSession"] == 0
    assert result["microphoneAttempts"] == 0
    assert result["problemShown"] is True, "a refused session request must be answered openly"
    assert result["narrowOverflow"] == 0
    assert result["outbound"] == result["pageErrors"] == 0
    # The only failed request is the session create itself: this deployment has no realtime
    # base, so the realtime path is refused (404 here; a mounted-but-unavailable base answers
    # 503). No other path may fail.
    assert [item["path"] for item in result["failed"]] == ["/v1/nexus/realtime/sessions"], result[
        "failed"
    ]
    assert result["failed"][0]["status"] in {404, 503}, result["failed"]
    assert "实时字幕" in result["observedCopy"]
