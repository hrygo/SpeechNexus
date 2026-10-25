"""Five public meeting operations over a real loopback socket and private PostgreSQL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import psycopg
import pytest
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_http_management import acceptance
from tests.integration.test_workbench_http_postgres import http
from tests.integration.test_workbench_meetings_postgres import deletion_service, meeting_cleanup
from tests.integration.test_workbench_meetings_postgres import meetings as meetings
from tests.integration.test_workbench_minutes_tasks_postgres import BINDING, PROFILE, Llm
from tests.integration.test_workbench_minutes_tasks_postgres import tasks as tasks
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.app import create_app
from speechnexus_backend.workbench.application.llm import LlmError
from speechnexus_backend.workbench.application.meetings import WorkbenchMeetings
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.infrastructure.postgres_meetings import PostgresMeetings
from speechnexus_backend.workbench.infrastructure.postgres_minutes_tasks import PostgresMinutesTasks

pytestmark = pytest.mark.integration


@asynccontextmanager
async def connection(store: Store, meetings: WorkbenchMeetings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        store.app,
        deletion_service(store),
        meetings=meetings,
        default_workspace_id="workspace-demo",
        deployment_id="meeting-synthetic",
        cursor_key=b"synthetic-cursor-key-32-bytes-only",
        max_json_bytes=4096,
        body_timeout_seconds=1,
    )
    async with http(store, application=app) as c:
        yield c


async def setup(c: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    r = await c.post(
        "/v1/workbench/projects",
        json={"name": "合成会议项目"},
        headers={"Idempotency-Key": "project-original"},
    )
    assert r.status_code == 201, r.text
    project = r.json()
    p = f"/v1/workbench/projects/{project['id']}"
    r = await c.post(
        p + "/transcripts",
        json={
            "display_name": "合成会议稿",
            "source": {"kind": "manual", "text": " 原稿 e\u0301😀\n "},
        },
        headers={"Idempotency-Key": "source-original"},
    )
    assert r.status_code == 201, r.text
    source = r.json()
    r = await c.post(
        p + "/meetings",
        json={"display_name": "合成会议", "transcript_id": source["transcript"]["id"]},
        headers={"Idempotency-Key": "meeting-original"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "capturing" and r.json()["save_audio"] is False
    return project, source, r.json()


async def seal(c: httpx.AsyncClient, meeting: dict[str, Any]) -> dict[str, Any]:
    r = await c.post(
        f"/v1/workbench/meetings/{meeting['id']}/seal",
        json={"expected_transcript_revision": "1", "text": " 精确会议 e\u0301😀\n "},
        headers={"Idempotency-Key": "seal-original"},
    )
    assert r.status_code == 200 and r.json()["sealed_revision"] == "2", r.text
    return cast(dict[str, Any], r.json())


async def enqueue(c: httpx.AsyncClient, meeting: dict[str, Any]) -> dict[str, Any]:
    r = await c.post(
        f"/v1/workbench/meetings/{meeting['id']}/minutes",
        json={"template_revision": "1"},
        headers={"Idempotency-Key": "minutes-original"},
    )
    assert r.status_code == 202 and r.json()["status"] == "queued", r.text
    return cast(dict[str, Any], r.json())


async def test_real_http_full_meeting_minutes_delete_and_physical_cleanup(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
    cleanup: Cleanup,
) -> None:
    async with connection(store, meetings) as c:
        project, source, meeting = await setup(c)
        path = f"/v1/workbench/meetings/{meeting['id']}"
        assert (await c.get("/v1/workbench/status")).json()["minutes_profile"] == {
            "template_revision": "1",
            "llm_model": PROFILE.model,
        }
        sealed = await seal(c, meeting)
        job = await enqueue(c, meeting)
        current = (await c.get(path)).json()
        assert current["status"] == "minutes_pending" and current["minutes_id"] == job["id"]
        llm = Llm(store)
        final = await MinutesTasks(tasks, llm).execute(job["id"])
        assert final.task.status == "succeeded" and len(llm.calls) == 1
        assert (await c.get(path)).json()["status"] == "minutes_ready"
        result = await c.get(path + "/minutes/" + job["id"])
        assert result.status_code == 200 and result.json()["minutes_text"] == final.task.text
        assert not set(result.json()) & {"usage", "binding_id", "system_prompt", "attempt_token"}
        assert await enqueue(c, meeting) == job
        assert await seal(c, meeting) == sealed
        r = await c.post(
            f"/v1/workbench/projects/{project['id']}/meetings",
            json={"display_name": "合成会议", "transcript_id": source["transcript"]["id"]},
            headers={"Idempotency-Key": "meeting-original"},
        )
        assert r.json() == meeting
        assert (
            await store.app.get_revision(source["transcript"]["id"], 1)
        ).text == " 原稿 e\u0301😀\n "
        ordinary = await c.post(
            "/v1/workbench/deletion-plans",
            json={
                "mode": "ordinary",
                "target": {
                    "owner": "workbench",
                    "kind": "transcript",
                    "resource": {"id": meeting["transcript_id"], "revision": "2"},
                },
            },
            headers={"Idempotency-Key": "ordinary-preview"},
        )
        assert ordinary.status_code == 200 and not ordinary.json()["eligible"]
        r = await c.post(
            "/v1/workbench/deletion-plans",
            json={
                "mode": "cascade",
                "target": {
                    "owner": "workbench",
                    "kind": "project",
                    "resource": {"id": project["id"], "revision": "1"},
                },
            },
            headers={"Idempotency-Key": "cascade-preview"},
        )
        assert r.status_code == 200, r.text
        plan = r.json()
        body = acceptance(plan)
        assert any(t["kind"] == "meeting" for t in body["confirm_targets"])
        incomplete = {
            **body,
            "confirm_targets": [t for t in body["confirm_targets"] if t["kind"] != "meeting"],
        }
        assert (
            await c.post(
                "/v1/workbench/deletions",
                json=incomplete,
                headers={"Idempotency-Key": "incomplete-delete"},
            )
        ).status_code == 422
        deleted = await c.post(
            "/v1/workbench/deletions", json=body, headers={"Idempotency-Key": "complete-delete"}
        )
        assert deleted.status_code == 202, deleted.text
        for target in (path, path + "/minutes/" + job["id"]):
            assert (await c.get(target)).status_code == 404
        worker = await meeting_cleanup(store, cleanup)
        result_record = await worker.run(deleted.json()["id"])
        assert result_record is not None
        assert (await c.get("/v1/workbench/deletions/" + deleted.json()["id"])).json()[
            "cleanup_state"
        ] == "complete"
        assert all(r.headers["cache-control"] == "no-store" for r in (result, deleted))


async def test_real_http_disabled_llm_profile_and_unsupported_source_fail_honestly(
    store: Store,
    meetings: WorkbenchMeetings,
) -> None:
    async with connection(store, meetings) as c:
        project, source, meeting = await setup(c)
        path = f"/v1/workbench/meetings/{meeting['id']}"
        for body in (
            {"display_name": "合成", "speech_session_id": "session-unverified"},
            {
                "display_name": "合成",
                "transcript_id": source["transcript"]["id"],
                "save_audio": True,
            },
        ):
            r = await c.post(f"/v1/workbench/projects/{project['id']}/meetings", json=body)
            assert (
                r.status_code == 422 and r.json()["error"]["code"] == "meeting_source_unsupported"
            )
        r = await c.post(path + "/minutes", json={"template_revision": "1"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "llm_source_not_sealed"
        await seal(c, meeting)
        r = await c.post(
            path + "/minutes",
            json={"template_revision": "1", "expected_source_revision": "1"},
            headers={"Idempotency-Key": "wrong-source"},
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "llm_source_changed"
        r = await c.post(path + "/minutes", json={"template_revision": "2"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "llm_profile_mismatch"
        job = await enqueue(c, meeting)
    disabled = WorkbenchMeetings(PostgresMeetings(store.uow))
    async with connection(store, disabled) as c:
        assert not (await c.get("/v1/workbench/status")).json()["llm_enabled"]
        assert "minutes_profile" not in (await c.get("/v1/workbench/status")).json()
        assert await enqueue(c, meeting) == job
        r = await c.post(
            path + "/minutes",
            json={"template_revision": "1"},
            headers={"Idempotency-Key": "new-intent"},
        )
        assert r.status_code == 503 and r.json()["error"]["code"] == "llm_disabled"


@pytest.mark.parametrize("unknown", [False, True])
async def test_real_http_failure_or_running_unknown_never_changes_sealed_capture(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
    unknown: bool,
) -> None:
    async with connection(store, meetings) as c:
        _, source, meeting = await setup(c)
        await seal(c, meeting)
        job = await enqueue(c, meeting)
        if unknown:
            await tasks.claim(job["id"], profile=PROFILE, binding_id=BINDING)
            with pytest.raises(MinutesTaskConflictError):
                await MinutesTasks(tasks, Llm(store)).execute(job["id"])
        else:
            llm = Llm(store)
            llm.failure = LlmError("unavailable")
            await MinutesTasks(tasks, llm).execute(job["id"])
        path = f"/v1/workbench/meetings/{meeting['id']}"
        current = (await c.get(path + "/minutes/" + job["id"])).json()
        assert current["status"] == ("running" if unknown else "failed")
        assert bool(current["error"]) == (not unknown)
        assert (await c.get(path)).json()["status"] == "minutes_pending"
        await store.app.revise_transcript(
            transcript_id=source["transcript"]["id"], expected_revision=2, text="合成修订"
        )
        expired = (await c.get(path + "/minutes/" + job["id"])).json()
        assert (
            expired["status"] == "expired"
            and expired["minutes_text"] is None
            and expired["error"] is None
        )


async def test_real_http_commit_unknown_recovers_exact_original_key(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with connection(store, meetings) as c:
        _, _, meeting = await setup(c)
        await seal(c, meeting)
        transaction = psycopg.AsyncConnection.transaction

        @asynccontextmanager
        async def lose_reply(
            connection: psycopg.AsyncConnection[tuple[object, ...]],
            savepoint_name: str | None = None,
            force_rollback: bool = False,
        ) -> AsyncIterator[psycopg.AsyncTransaction]:
            async with transaction(connection, savepoint_name, force_rollback) as tx:
                yield tx
            raise psycopg.OperationalError("synthetic private reply loss")

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "transaction", lose_reply)
            r = await c.post(
                f"/v1/workbench/meetings/{meeting['id']}/minutes",
                json={"template_revision": "1"},
                headers={"Idempotency-Key": "minutes-original"},
            )
            assert r.status_code == 503 and r.json()["error"]["code"] == "outcome_unknown"
        job = await enqueue(c, meeting)
        assert await tasks.queued_ids("workspace-demo", limit=100) == (job["id"],)
