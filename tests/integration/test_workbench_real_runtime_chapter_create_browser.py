"""Adding a chapter through the production bundle against the real C runtime and PG.

The audiobook flow listed chapters but had no way to create one, so a book could not be
filled from the UI. This drives the production bundle (create book, open it, add a chapter)
and then reads the same chapter back over the real HTTP API.

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

BOOK_TITLE = "合成有声书"
CHAPTER_TITLE = "合成第一章"
UNIT_TEXT = "合成旁白单元"


async def test_a_chapter_created_in_the_production_bundle_is_stored_and_readable(
    database: Database,
) -> None:
    tier = Tier(database)
    output = (
        Path(
            os.environ.get("SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT", ROOT / ".runtime/team-c/browser")
        )
        / "real-runtime-chapter-create"
    )
    output.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    result_name = ""
    async with httpx.AsyncClient(base_url=tier.origin, timeout=30) as client, tier.running():
        project = (
            await client.post("/v1/workbench/projects", json={"name": "合成有声书项目"})
        ).json()
        assert (await client.get(f"/v1/workbench/projects/{project['id']}/books")).json()[
            "data"
        ] == []
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/dev-env/workbench-chapter-create-probe.mjs",
            "--url",
            tier.origin,
            "--output",
            str(output),
            "--project-name",
            project["name"],
            "--book-title",
            BOOK_TITLE,
            "--chapter-title",
            CHAPTER_TITLE,
            "--unit-text",
            UNIT_TEXT,
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

        # The chapter the page showed must be the one the deployment stored, with its text
        # revision already at 1 — not a client-side row.
        books = (await client.get(f"/v1/workbench/projects/{project['id']}/books")).json()["data"]
        assert [book["title"] for book in books] == [BOOK_TITLE]
        book = books[0]
        chapters = (await client.get(f"/v1/workbench/books/{book['id']}/chapters")).json()["data"]
        assert [(item["title"], item["ordinal"]) for item in chapters] == [(CHAPTER_TITLE, 0)]
        read = await client.get(f"/v1/workbench/chapters/{chapters[0]['id']}")
        assert read.status_code == 200, read.text
        assert read.json() == chapters[0]
        assert read.json()["text_revision"] == "1"

        # The unit the page registered must be the one stored, with the pin it was given.
        result = events[-1]
        units = (
            await client.get(f"/v2/workbench/chapters/{chapters[0]['id']}/render-units")
        ).json()["data"]
        # Two units: the mouse-driven one and the keyboard-only one. Their order in the list
        # is the deployment's business, so compare as a set.
        assert sorted((item["text"], item["ordinal"]) for item in units) == sorted(
            [(UNIT_TEXT, 0), (result["keyboardUnitText"], 1)]
        )
        unit = next(item for item in units if item["text"] == UNIT_TEXT)
        assert unit["voice"] == {"id": "voice_synthetic", "revision": "1"}
        assert unit["model"] == "tts-quality"
        assert unit["model_binding"] == {"id": "binding_synthetic", "revision": "1"}
        assert unit["status"] == "pending"  # registered, not generated: generation is A's
        one = await client.get(f"/v2/workbench/render-units/{unit['id']}")
        assert one.status_code == 200, one.text
        assert one.json() == unit
        # The rename the test injected is the one that must be stored: the page's later
        # attempt was refused for a stale metadata revision and must not have won.
        assert events, "the probe must report what it did"
        result_name = events[-1]["injectedName"]
        stored = (await client.get(f"/v1/workbench/projects/{project['id']}")).json()
        assert stored["name"] == result_name, stored
        assert stored["metadata_revision"] == 3, stored
    result = events[-1]
    assert result["createdBookViaUi"] is True
    assert result["createdChapterViaUi"] is True
    assert result["chapterPosts"] == 1
    assert result["createdUnitViaUi"] is True
    assert result["keyboardOrderChecked"] is True
    assert result["narrowOverflow"] == 0
    assert result["outbound"] == result["unexpectedErrors"] == 0
    # The only non-2xx is the one this deployment must produce: no speech base means no
    # published-voice catalogue, which the panel reports instead of inventing pins.
    assert result["renamedViaUi"] and result["injectedName"]
    assert result["staleRenameRefused"] is True
    # Two non-2xx answers are provoked on purpose: the deployment has no speech base, so the
    # published-voice catalogue is unavailable, and the stale rename is refused (409).
    # Arrival order is the page's own order: catalogue, then the single revision, then the
    # stale rename. No speech base means both voice reads are refused.
    assert result["failed"] == [
        {"path": "/v1/workbench/voices", "status": 503},
        {"path": "/v2/workbench/voices/voice_synthetic/revisions/1", "status": 503},
        {
            "path": f"/v1/workbench/projects/{project['id']}",
            "status": 409,
        },
    ], result["failed"]
