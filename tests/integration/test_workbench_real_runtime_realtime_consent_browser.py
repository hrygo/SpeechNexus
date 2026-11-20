"""The realtime tab in the production bundle, against a real C runtime.

The page tells the operator that the microphone opens only after an explicit step. This drives
the production bundle against a deployment with no realtime base and checks that promise, the
keyboard reachability of the form and the 360px layout.

The refusal is asserted per path, not per count: the create request must be refused either at
the protocol layer (`refused`, a `status >= 400` response) or at the transport layer
(`networkFailed`, a `requestfailed` with the browser's reason). A prompt that was already on
screen before the click, a create request that never left the page, a read that happened before
the answer arrived, and an answer that only arrived because the page gave up on its own
transport timeout are each their own failure — an empty `refused` is never compliance.

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
# The only path this probe may see refused: the session create the operator asked for.
CREATE_PATH = "/v1/nexus/realtime/sessions"
# The page aborts the create request after this long (`realtime-client.ts`, the transport's
# `AbortSignal.timeout(15000)`). A backend that hangs is therefore reported by the page as a
# transport failure; the refusal below must be the server's answer instead of that surrender.
PAGE_TIMEOUT_MS = 15000


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
    assert result["narrowOverflow"] == 0
    assert result["outbound"] == result["pageErrors"] == 0
    assert "实时字幕" in result["observedCopy"]

    # The warning is new: nothing may have been on screen before the click, so a prompt that
    # was already there cannot be counted as the answer to the click.
    assert result["answersBeforeClick"] == [], result["answersBeforeClick"]
    # The create request really left the page: "refused" is only meaningful if something was
    # sent, otherwise a page that never calls anything would pass as compliant.
    assert result["createRequestIssued"] is True, result
    # The probe waited for the request's own outcome, so this is an answer and not a bye.
    assert result["answer"] in {"refused", "networkFailed"}, result
    assert result["problemShown"] is True, "a refused session request must be answered openly"
    assert result["answersAfterClick"], result

    # The refusal came from the server, not from the page giving up on a hung backend: the
    # page aborts at PAGE_TIMEOUT_MS, so an answer that arrives at or after that is the page's
    # own timeout, not an answer to the operator.
    assert result["elapsedMs"] is not None, result
    assert result["pageTimeoutMs"] == PAGE_TIMEOUT_MS, result
    assert result["elapsedMs"] < PAGE_TIMEOUT_MS, (
        "the create request took "
        f"{result['elapsedMs']} ms, reaching the page's own {PAGE_TIMEOUT_MS} ms transport "
        f"timeout: a hung backend must not pass as a refusal: {result}"
    )

    refused = [item for item in result["refused"] if item["path"] == CREATE_PATH]
    network = [item for item in result["networkFailed"] if item["path"] == CREATE_PATH]
    # One answer, one layer: a response that arrived, or a transport that failed with a reason.
    assert result["answer"] == ("refused" if refused else "networkFailed"), result
    assert refused or network, (
        "the session create must be refused on the wire; an empty refused/networkFailed "
        f"pair means the request was never answered: {result}"
    )
    # No other path may fail, on either layer.
    assert [item["path"] for item in result["refused"]] in ([], [CREATE_PATH]), result["refused"]
    assert [item["path"] for item in result["networkFailed"]] in ([], [CREATE_PATH]), result[
        "networkFailed"
    ]
    # This deployment has no realtime base, so the realtime path is refused (404 here; a
    # mounted-but-unavailable base answers 503).
    for item in refused:
        assert item["status"] in {404, 503}, result["refused"]
    # A transport-level refusal must keep the browser's own reason, or it is indistinguishable
    # from "the probe did not look".
    for item in network:
        assert item["failure"], result["networkFailed"]
