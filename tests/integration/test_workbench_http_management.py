"""Full C1 over a real loopback socket and the disposable owner-only PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import service
from tests.integration.test_workbench_http_postgres import http
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.infrastructure.postgres_deletions import _DeletionSession
from speechnexus_backend.workbench.infrastructure.postgres_versions import _Session

pytestmark = pytest.mark.integration
WORKSPACE = "workspace-synthetic"


@asynccontextmanager
async def management(store: Store) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(
        store.app,
        service(store),
        default_workspace_id=WORKSPACE,
        deployment_id="deployment-synthetic",
        cursor_key=b"synthetic-cursor-key-32-bytes-only",
        max_json_bytes=4096,
        body_timeout_seconds=1,
    )
    async with http(store, application=application) as client:
        yield client


def acceptance(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": plan["id"],
        "expected_plan_revision": plan["plan_revision"],
        "plan_digest": plan["plan_digest"],
        "confirm_targets": [
            plan["target"],
            *[d["target"] for d in plan["dependencies"] if d["disposition"] == "cascade"],
        ],
    }


async def preview(
    c: httpx.AsyncClient,
    project: dict[str, Any],
    key: str,
    *,
    mode: str = "cascade",
) -> httpx.Response:
    return await c.post(
        "/v1/workbench/deletion-plans",
        params={"workspace_id": project["workspace_id"]},
        headers={"Idempotency-Key": key},
        json={
            "mode": mode,
            "target": {
                "owner": "workbench",
                "kind": "project",
                "resource": {
                    "id": project["id"],
                    "revision": str(project["metadata_revision"]),
                },
            },
        },
    )


async def test_full_c1_delete_cleanup_scoped_replays_and_current_ledger(
    store: Store,
    cleanup: Cleanup,
) -> None:
    async with management(store) as c:
        status = await c.get("/v1/workbench/status")
        assert status.status_code == 200 and status.json()["workbench_enabled"]
        assert status.json()["llm_enabled"] is False and status.json()["capabilities"] == []
        created = await c.post(
            "/v1/workbench/projects",
            json={"name": "合成项目", "workspace_id": "workspace-other"},
            headers={"Idempotency-Key": "project-original"},
        )
        assert created.status_code == 201
        project = created.json()
        pp = f"/v1/workbench/projects/{project['id']}"
        updates = await asyncio.gather(
            *[
                c.patch(
                    pp,
                    json={"expected_metadata_revision": 1, "name": name},
                    headers={"Idempotency-Key": key},
                )
                for name, key in [("甲", "patch-one"), ("乙", "patch-two")]
            ]
        )
        assert sorted(r.status_code for r in updates) == [200, 409]
        project = (await c.get(pp)).json()
        texts = []
        for i in range(2):
            result = await c.post(
                pp + "/transcripts",
                json={
                    "display_name": "合成稿",
                    "source": {"kind": "manual", "text": "合成敏感正文"},
                },
                headers={"Idempotency-Key": f"transcript-{i}"},
            )
            assert result.status_code == 201
            texts.append(result.json())
        tid = texts[0]["transcript"]["id"]
        tp = f"/v1/workbench/transcripts/{tid}"
        revision = await c.post(
            tp + "/revisions",
            json={"expected_revision": "1", "source_revision": "1", "text": "合成校对"},
            headers={"Idempotency-Key": "revision-original"},
        )
        assert revision.status_code == 201
        assert (await c.get(tp + "/revisions/1")).json() == texts[0]["revision"]
        assert (await c.get(tp, params={"revision": "1"})).json()["transcript"][
            "head_revision"
        ] == "2"
        page = (await c.get(pp + "/transcripts", params={"limit": "1"})).json()
        assert len(page["data"]) == 1 and page["has_more"] and "合成敏感正文" not in str(page)
        assert (await c.get(pp + "/transcripts", params={"cursor": page["next_cursor"]})).json()[
            "has_more"
        ] is False
        ordinary = (await preview(c, project, "ordinary-preview", mode="ordinary")).json()
        assert ordinary["eligible"] is False
        blocked = await c.post(
            "/v1/workbench/deletions",
            params={"workspace_id": "workspace-other"},
            json=acceptance(ordinary),
            headers={"Idempotency-Key": "ordinary-accept"},
        )
        assert (
            blocked.status_code == 409
            and blocked.json()["error"]["code"] == "deletion_dependency_conflict"
        )
        response = await preview(c, project, "cascade-preview")
        assert response.status_code == 200
        plan = response.json()
        body = acceptance(plan)
        wrong_scope = await c.post(
            "/v1/workbench/deletions", json=body, headers={"Idempotency-Key": "accept-original"}
        )
        assert wrong_scope.status_code == 404
        missing = await c.post(
            "/v1/workbench/deletions",
            params={"workspace_id": "workspace-other"},
            json={**body, "confirm_targets": [plan["target"]]},
            headers={"Idempotency-Key": "missing-confirm"},
        )
        assert missing.status_code == 422
        accepted = await c.post(
            "/v1/workbench/deletions",
            params={"workspace_id": "workspace-other"},
            json=body,
            headers={"Idempotency-Key": "accept-original"},
        )
        assert accepted.status_code == 202 and accepted.json()["cleanup_state"] == "pending"
        record = accepted.json()
        dp = f"/v1/workbench/deletions/{record['id']}"
        assert (await c.get(dp)).status_code == 404
        assert (await c.get(dp, params={"workspace_id": "workspace-other"})).json() == record
        for path in [pp, tp, tp + "/revisions/1", pp + "/transcripts"]:
            assert (await c.get(path)).status_code == 404
        assert (
            await c.get("/v1/workbench/projects", params={"workspace_id": "workspace-other"})
        ).json()["data"] == []
        cleanup_result = await cleanup.worker.run(record["id"])
        assert cleanup_result is not None and len(cleanup_result.deleted) == 3
        completed = (await c.get(dp, params={"workspace_id": "workspace-other"})).json()
        assert completed["status"] == "succeeded" and completed["cleanup_state"] == "complete"
        assert len(completed["result"]["deleted"]) == 3
        # Replays preserve the original immutable plan and original queued acceptance snapshot.
        assert (await preview(c, project, "cascade-preview")).json() == plan
        replay = await c.post(
            "/v1/workbench/deletions",
            params={"workspace_id": "workspace-other"},
            json=body,
            headers={"Idempotency-Key": "accept-original"},
        )
        assert replay.status_code == 202 and replay.json() == record
        changed = await c.post(
            "/v1/workbench/deletions",
            params={"workspace_id": "workspace-other"},
            json={**body, "expected_plan_revision": "2"},
            headers={"Idempotency-Key": "accept-original"},
        )
        assert (
            changed.status_code == 409 and changed.json()["error"]["code"] == "idempotency_conflict"
        )


async def test_live_keyset_ties_insert_and_delete_do_not_repeat_or_silently_restart(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 24, tzinfo=UTC)

    async def clock(self: Any) -> datetime:
        return now

    monkeypatch.setattr(_Session, "now", clock)
    async with management(store) as c:
        projects = []
        for i in range(6):
            r = await c.post(
                "/v1/workbench/projects",
                json={"name": "合成分页"},
                headers={"Idempotency-Key": f"tie-project-{i}"},
            )
            assert r.status_code == 201
            projects.append(r.json())
        projects.sort(key=lambda p: p["id"], reverse=True)
        first = (await c.get("/v1/workbench/projects", params={"limit": "2"})).json()
        assert [p["id"] for p in first["data"]] == [p["id"] for p in projects[:2]]
        cursor = first["next_cursor"]
        now += timedelta(seconds=1)
        inserted = await c.post(
            "/v1/workbench/projects",
            json={"name": "后插入"},
            headers={"Idempotency-Key": "late-project"},
        )
        assert inserted.status_code == 201
        gone = projects[3]
        plan = (await preview(c, gone, "page-preview", mode="ordinary")).json()
        assert (
            await c.post(
                "/v1/workbench/deletions",
                json=acceptance(plan),
                headers={"Idempotency-Key": "page-delete"},
            )
        ).status_code == 202
        rows = first["data"]
        while cursor:
            page = (
                await c.get("/v1/workbench/projects", params={"limit": "1", "cursor": cursor})
            ).json()
            assert page["has_more"] == (page["next_cursor"] is not None)
            rows += page["data"]
            cursor = page["next_cursor"]
        assert [p["id"] for p in rows] == [p["id"] for p in projects if p["id"] != gone["id"]]
        assert (
            await c.get(
                "/v1/workbench/projects",
                params={"cursor": first["next_cursor"], "workspace_id": "other"},
            )
        ).status_code == 400


async def test_plan_replay_after_expiry_and_dependency_change_requires_new_intent(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 24, tzinfo=UTC)

    async def clock(self: Any) -> datetime:
        return now

    monkeypatch.setattr(_DeletionSession, "now", clock)
    async with management(store) as c:
        r = await c.post(
            "/v1/workbench/projects",
            json={"name": "合成"},
            headers={"Idempotency-Key": "project-one"},
        )
        assert r.status_code == 201
        project = r.json()
        expired = (await preview(c, project, "expired-preview")).json()
        now += timedelta(minutes=6)
        assert (await preview(c, project, "expired-preview")).json() == expired
        denied = await c.post(
            "/v1/workbench/deletions",
            json=acceptance(expired),
            headers={"Idempotency-Key": "expired-accept"},
        )
        assert (
            denied.status_code == 409 and denied.json()["error"]["code"] == "deletion_plan_expired"
        )
        before = (await preview(c, project, "before-dependency")).json()
        assert (
            await c.post(
                f"/v1/workbench/projects/{project['id']}/transcripts",
                json={"display_name": "合成", "source": {"kind": "manual", "text": "合成"}},
                headers={"Idempotency-Key": "added-child"},
            )
        ).status_code == 201
        changed = await c.post(
            "/v1/workbench/deletions",
            json=acceptance(before),
            headers={"Idempotency-Key": "changed-accept"},
        )
        assert (
            changed.status_code == 409
            and changed.json()["error"]["code"] == "deletion_plan_changed"
        )
        fresh = (await preview(c, project, "fresh-preview")).json()
        assert len(fresh["dependencies"]) == 1
        assert (
            await c.post(
                "/v1/workbench/deletions",
                json=acceptance(fresh),
                headers={"Idempotency-Key": "fresh-accept"},
            )
        ).status_code == 202


async def test_transcript_only_deletion_keeps_parent_and_other_transcript_visible(
    store: Store,
    cleanup: Cleanup,
) -> None:
    async with management(store) as c:
        project = (
            await c.post(
                "/v1/workbench/projects",
                json={"name": "合成项目"},
                headers={"Idempotency-Key": "project-original"},
            )
        ).json()
        path = f"/v1/workbench/projects/{project['id']}"
        transcripts = []
        for i in range(2):
            response = await c.post(
                path + "/transcripts",
                json={"display_name": "合成稿", "source": {"kind": "manual", "text": "合成正文"}},
                headers={"Idempotency-Key": f"transcript-{i}"},
            )
            assert response.status_code == 201
            transcripts.append(response.json()["transcript"])
        target = {
            "owner": "workbench",
            "kind": "transcript",
            "resource": {
                "id": transcripts[0]["id"],
                "revision": transcripts[0]["head_revision"],
            },
        }
        response = await c.post(
            "/v1/workbench/deletion-plans",
            json={"target": target, "mode": "ordinary"},
            headers={"Idempotency-Key": "transcript-plan"},
        )
        assert response.status_code == 200 and response.json()["eligible"]
        accepted = await c.post(
            "/v1/workbench/deletions",
            json=acceptance(response.json()),
            headers={"Idempotency-Key": "transcript-delete"},
        )
        assert accepted.status_code == 202
        record = accepted.json()
        assert (await c.get(f"/v1/workbench/transcripts/{transcripts[0]['id']}")).status_code == 404
        assert (await c.get(path)).status_code == 200
        visible = (await c.get(path + "/transcripts")).json()["data"]
        assert [t["id"] for t in visible] == [transcripts[1]["id"]]
        result = await cleanup.worker.run(record["id"])
        assert result is not None and len(result.deleted) == 1
        assert (await c.get(f"/v1/workbench/transcripts/{transcripts[1]['id']}")).status_code == 200
