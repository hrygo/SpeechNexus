"""The meeting/minutes page against the real runtime and the configured local model.

The suite's other meeting browser test drives a fake LLM through in-process hooks: it
turns the model off inside the app and executes the queued minutes itself. This one keeps
the deployment's real configuration, so the page's queued job is claimed and finished by
the runtime's own dispatcher, and the text in the page is what the configured local model
actually returned. It stays out of the routine `--browser` selection because it needs
`SPEECHNEXUS_TEST_LLM_ENDPOINT`/`MODEL`, and it is not speech or device qualification.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.integration.test_workbench_http_management import acceptance
from tests.integration.test_workbench_runtime import Database
from tests.integration.test_workbench_runtime import database as database
from tests.integration.workbench_real_runtime import ROOT, Tier

ENDPOINT = os.environ.get("SPEECHNEXUS_TEST_LLM_ENDPOINT", "")
MODEL = os.environ.get("SPEECHNEXUS_TEST_LLM_MODEL", "")

pytestmark = [
    pytest.mark.qualification,
    pytest.mark.skipif(
        not (ENDPOINT and MODEL),
        reason="needs SPEECHNEXUS_TEST_LLM_ENDPOINT and SPEECHNEXUS_TEST_LLM_MODEL",
    ),
]


async def finished_minutes(client: httpx.AsyncClient, meeting_id: str, minutes_id: str) -> str:
    """Wait for the runtime's own dispatcher to finish the queued minutes."""
    for _ in range(900):
        response = await client.get(f"/v1/workbench/meetings/{meeting_id}/minutes/{minutes_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled", "expired"}:
            assert job["status"] == "succeeded", job
            assert job["llm_model"] == MODEL
            assert job["minutes_text"], "the configured model returned no minutes text"
            return str(job["minutes_text"])
        await asyncio.sleep(0.1)
    raise AssertionError("the runtime did not finish the queued minutes")


async def test_real_runtime_minutes_page_shows_the_configured_model_output(
    database: Database,
) -> None:
    tier = Tier(
        database,
        fault_suffixes=frozenset({"seal", "minutes"}),
        cleanup_interval_seconds=0.2,
        minutes_interval_seconds=0.5,
        llm_endpoint=ENDPOINT,
        llm_model=MODEL,
        llm_api_key=os.environ.get("SPEECHNEXUS_TEST_LLM_API_KEY"),
        llm_qualification_reference="adr-0003-approved-local-intranet-text-llm",
        llm_deadline_seconds=120,
    )
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-meetings"
    )
    output.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    model_text = ""
    async with (
        httpx.AsyncClient(base_url=tier.origin, timeout=30) as client,
        tier.running(),
    ):
        project = (
            await client.post("/v1/workbench/projects", json={"name": "合成浏览器会议项目"})
        ).json()
        transcript = (
            await client.post(
                f"/v1/workbench/projects/{project['id']}/transcripts",
                json={
                    "display_name": "合成浏览器会议稿",
                    "source": {"kind": "manual", "text": " 合成原文 e\u0301😀\n "},
                },
            )
        ).json()["transcript"]
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-meetings-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
            "--llm-always-on",
            "--minutes-any-text",
            cwd=ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout and process.stderr and process.stdin
        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            async with asyncio.timeout(300):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    events.append(event)
                    stage = event["stage"]
                    if stage == "done":
                        break
                    if stage == "execute":
                        # No hand-run task: the deployment's dispatcher owns this job.
                        model_text = await finished_minutes(
                            client, event["meetingId"], event["minutesId"]
                        )
                    elif stage == "stale":
                        revised = await client.post(
                            f"/v1/workbench/transcripts/{transcript['id']}/revisions",
                            json={"expected_revision": "2", "text": "另一份合成校对"},
                        )
                        assert revised.status_code == 201, revised.text
                    elif stage == "delete":
                        preview = await client.post(
                            "/v1/workbench/deletion-plans",
                            headers={"Idempotency-Key": "browser-meeting-preview"},
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
                        deleted = await client.post(
                            "/v1/workbench/deletions",
                            headers={"Idempotency-Key": "browser-meeting-delete"},
                            json=acceptance(preview.json()),
                        )
                        assert deleted.status_code in {200, 202}, deleted.text
                        for _ in range(300):
                            gone = await client.get(f"/v1/workbench/projects/{project['id']}")
                            if gone.status_code == 404:
                                break
                            await asyncio.sleep(0.1)
                        assert gone.status_code == 404, "the runtime did not erase the project"
                    else:
                        raise AssertionError(f"unexpected stage {stage}")
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
            (output / "model-text.txt").write_text(model_text, encoding="utf-8")
    assert [e["stage"] for e in events] == ["execute", "stale", "delete", "done"]
    assert tier.lost == {
        "/v1/workbench/meetings/" + events[0]["meetingId"] + "/seal",
        "/v1/workbench/meetings/" + events[0]["meetingId"] + "/minutes",
    }
    assert model_text.strip(), "the page-level run produced no model text"
    assert events[-1]["kind"] == ("production-browser-real-c-http-private-pg-configured-local-llm")
    assert sorted(f["status"] for f in events[-1]["expectedFailures"]) == [404, 503, 503]
    assert events[-1]["outbound"] == events[-1]["unexpectedErrors"] == 0
