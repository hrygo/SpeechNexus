"""Loopback HTTP and visible version snapshots on the disposable owner-only PG."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from tests.integration.test_workbench_deletions_postgres import service as deletions
from tests.integration.test_workbench_deletions_postgres import target
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.infrastructure.postgres_versions import _Session
from speechnexus_backend.workbench.interfaces.http_versions import create_workbench_app

pytestmark = pytest.mark.integration


@asynccontextmanager
async def http(
    store: Store, *, application: FastAPI | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    app = (
        application
        if application is not None
        else create_workbench_app(
            store.app,
            default_workspace_id="workspace-synthetic",
            max_json_bytes=4096,
            body_timeout_seconds=1,
        )
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.005)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3
        ) as client:
            yield client
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()


async def test_real_http_pg_versions_replay_concurrent_cas_and_deleted_history(
    store: Store,
) -> None:
    async with http(store) as c:
        r = await c.post(
            "/v1/workbench/projects",
            json={"name": "合成项目"},
            headers={"Idempotency-Key": "project-original"},
        )
        assert r.status_code == 201
        p = r.json()
        pp = f"/v1/workbench/projects/{p['id']}"
        replies = await asyncio.gather(
            *(
                c.patch(
                    pp,
                    json={"expected_metadata_revision": 1, "name": name},
                    headers={"Idempotency-Key": key},
                )
                for name, key in [("甲", "update-one"), ("乙", "update-two")]
            )
        )
        assert sorted(r.status_code for r in replies) == [200, 409]
        assert (
            await c.post(
                "/v1/workbench/projects",
                json={"name": "合成项目"},
                headers={"Idempotency-Key": "project-original"},
            )
        ).json() == p
        r = await c.post(
            pp + "/transcripts",
            json={"display_name": "合成稿", "source": {"kind": "manual", "text": " e\u0301😀\n "}},
            headers={"Idempotency-Key": "transcript-original"},
        )
        assert r.status_code == 201
        t = r.json()
        tp = f"/v1/workbench/transcripts/{t['transcript']['id']}"
        revision = await c.post(
            tp + "/revisions",
            json={"expected_revision": "1", "source_revision": "1", "text": "校对"},
            headers={"Idempotency-Key": "revision-original"},
        )
        assert revision.status_code == 201 and revision.json()["revision"] == "2"
        view = await c.get(tp, params={"revision": "1"})
        assert view.status_code == 200 and view.json()["transcript"]["head_revision"] == "2"
        assert view.json()["revision"] == t["revision"]
        assert (await c.get(tp + "/revisions/1")).json() == t["revision"]
        assert (
            await c.post(
                tp + "/revisions",
                json={"expected_revision": "1", "source_revision": "1", "text": "校对"},
                headers={"Idempotency-Key": "revision-original"},
            )
        ).json() == revision.json()
        current = await store.app.get_project(p["id"])
        delete = deletions(store)
        plan = await delete.preview(
            workspace_id=current.workspace_id,
            target=target(current),
            mode="cascade",
            key="delete-plan",
        )
        from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest

        await delete.accept(
            workspace_id=current.workspace_id,
            plan_id=plan.id,
            expected_revision=1,
            digest=plan_digest(plan),
            targets=plan.confirmed_targets,
            key="delete-accept",
        )
        for path in [pp, tp, tp + "?revision=1", tp + "/revisions/1"]:
            gone = await c.get(path)
            assert gone.status_code == 404 and "校对" not in gone.text and " e" not in gone.text
        replay = await c.post(
            "/v1/workbench/projects",
            json={"name": "合成项目"},
            headers={"Idempotency-Key": "project-original"},
        )
        assert replay.status_code == 404 and replay.json()["error"]["code"] == "resource_not_found"


async def test_pg_view_uses_one_statement_and_selected_revision_is_not_current_head(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = store.app
    p = await service.create_project(workspace_id="workspace-synthetic", name="合成")
    t = await service.create_manual_transcript(project_id=p.id, display_name="合成", text="原文")
    changed = await service.revise_transcript(transcript_id=t.id, expected_revision=1, text="校对")
    statements: list[str] = []
    original_one = _Session._one
    original_many = _Session._many

    async def one(self: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append("one")
        return await original_one(self, *args, **kwargs)

    async def many(self: Any, *args: Any, **kwargs: Any) -> Any:
        statements.append("many")
        return await original_many(self, *args, **kwargs)

    monkeypatch.setattr(_Session, "_one", one)
    monkeypatch.setattr(_Session, "_many", many)
    current, selected = await service.get_transcript_view(t.id, 1)
    assert current == changed and selected == t.head and statements == ["many"]
    statements.clear()
    assert await service.get_transcript_view(t.id) == (changed, changed.head)
    assert statements == ["many"]
    with pytest.raises(ResourceNotFoundError):
        await service.get_transcript_view(t.id, 99)


async def test_concurrent_revision_reads_always_pair_matching_single_snapshot(store: Store) -> None:
    service = store.app
    p = await service.create_project(workspace_id="workspace-synthetic", name="合成")
    t = await service.create_manual_transcript(project_id=p.id, display_name="合成", text="原文")

    async def edit() -> None:
        for n in range(1, 8):
            await service.revise_transcript(
                transcript_id=t.id, expected_revision=n, text=f"合成-{n + 1}"
            )

    async def read() -> None:
        for _ in range(12):
            current, head = await service.get_transcript_view(t.id)
            assert current.head == head
            current, selected = await service.get_transcript_view(t.id, 1)
            assert current.head.number >= selected.number and selected == t.head

    await asyncio.gather(edit(), read(), read())
