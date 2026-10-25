"""A real local text model completes queued Minutes work on the disposable PG probe.

The endpoint, pinned model and optional credential come from the environment, so this
suite skips unless a deployment has approved one. The transcript body is synthetic, so no
business content reaches the model. This is a real generation, not a fake transport.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_minutes_tasks_postgres import enqueue
from tests.integration.test_workbench_minutes_tasks_postgres import tasks as tasks
from tests.integration.test_workbench_runtime import Database
from tests.integration.test_workbench_runtime import database as database
from tests.integration.test_workbench_sealing_postgres import BODY
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks, MinutesTaskView
from speechnexus_backend.workbench.infrastructure.openai_text_llm import OpenAiTextLlm
from speechnexus_backend.workbench.infrastructure.postgres_minutes_tasks import (
    PostgresMinutesTasks,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
)
from speechnexus_backend.workbench.runtime import _dispatch_minutes_once, create_runtime_app
from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings

LIVE_WORKSPACE = "workspace-live-model"
ENDPOINT = os.environ.get("SPEECHNEXUS_TEST_LLM_ENDPOINT", "")
MODEL = os.environ.get("SPEECHNEXUS_TEST_LLM_MODEL", "")

pytestmark = [
    pytest.mark.qualification,
    pytest.mark.skipif(
        not (ENDPOINT and MODEL),
        reason="no approved local text LLM endpoint is exported for this run",
    ),
]


def deployment() -> OpenAiTextLlm:
    settings = WorkbenchSettings(
        database_url="postgresql://synthetic-unused-by-this-test",
        database_schema="workbench_live_model",
        workspace_id="workspace-live-model",
        deployment_id="live-model-probe",
        cursor_key="C" * 40,
        llm_endpoint=ENDPOINT,
        llm_model=MODEL,
        llm_api_key=os.environ.get("SPEECHNEXUS_TEST_LLM_API_KEY"),
        llm_qualification_reference="adr-0003-approved-local-intranet-text-llm",
        llm_deadline_seconds=120,
    )
    return OpenAiTextLlm(settings.llm_configuration())


async def test_a_real_local_model_completes_a_queued_minutes_task(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = deployment()
    assert llm.profile is not None and llm.profile.model == MODEL
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    await _dispatch_minutes_once(service, "w")
    final = await service.get(initial.task.id)
    assert final.task.status == "succeeded", final.task.error
    assert final.task.text is not None and final.task.text.strip()
    assert final.profile.model == MODEL and final.profile == initial.profile
    attempts = await tasks.attempts(initial.task.id)
    assert attempts[-1].outcome == "succeeded" and attempts[-1].completion_confirmed
    assert attempts[-1].usage is not None
    assert attempts[-1].usage.input_tokens > 0 and attempts[-1].usage.output_tokens > 0


async def queued_task(store: Store, service: MinutesTasks, workspace: str) -> MinutesTaskView:
    """Seed one sealed source and queue its Minutes work under a serving workspace."""
    parent = await store.app.create_project(workspace_id=workspace, name="合成")
    transcript = await store.app.create_manual_transcript(
        project_id=parent.id, display_name="合成", text=BODY, idempotency_key="create"
    )
    await store.app.seal_transcript(transcript_id=transcript.id, expected_revision=1)
    return await service.enqueue(
        workspace_id=workspace,
        transcript_id=transcript.id,
        source_revision=2,
        template_revision=1,
        key="create",
    )


async def settled(
    service: MinutesTasks, task_id: str, *, attempts: int = 1800, delay: float = 0.1
) -> MinutesTaskView:
    """Bounded wait for the serving loop; no assertion depends on the exact timing."""
    view = await service.get(task_id)
    for _ in range(attempts):
        if view.task.status in {"succeeded", "failed"}:
            return view
        await asyncio.sleep(delay)
        view = await service.get(task_id)
    return view


def deployment_settings(database: Database) -> WorkbenchSettings:
    return WorkbenchSettings(
        database_url=make_conninfo(database.owner_dsn, user=database.role),
        database_schema=database.schema,
        workspace_id=LIVE_WORKSPACE,
        deployment_id="live-model-probe",
        cursor_key="C" * 40,
        llm_endpoint=ENDPOINT,
        llm_model=MODEL,
        llm_api_key=os.environ.get("SPEECHNEXUS_TEST_LLM_API_KEY"),
        llm_qualification_reference="adr-0003-approved-local-intranet-text-llm",
        llm_deadline_seconds=120,
        minutes_interval_seconds=0.05,
    )


async def test_the_serving_lifespan_dispatch_loop_completes_a_queued_task(
    database: Database,
) -> None:
    settings = deployment_settings(database)
    uow = PostgresVersionUnitOfWork(
        settings.database_url.get_secret_value(),
        schema=settings.database_schema,
        max_connections=2,
    )
    store = Store(owner_dsn=database.owner_dsn, schema=database.schema, role=database.role, uow=uow)
    app = create_runtime_app(settings)
    async with app.router.lifespan_context(app):
        # The serving instance owns the dispatch loop; this test only queues the work.
        service = MinutesTasks(
            PostgresMinutesTasks(uow), OpenAiTextLlm(settings.llm_configuration())
        )
        initial = await queued_task(store, service, LIVE_WORKSPACE)
        # The deployment-visible status must publish the same approved binding.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            status = await client.get("/v2/workbench/status")
        assert status.status_code == 200, status.text
        published = status.json()
        assert published["llm_enabled"] is True
        assert published["minutes_profile"]["llm_model"] == MODEL
        assert published["minutes_profile"]["template_revision"] == "1"
        final = await settled(service, initial.task.id)
    assert final.task.status == "succeeded", final.task.error
    assert final.task.text is not None and final.task.text.strip()
    assert final.profile.model == MODEL
    attempts = await service.get(initial.task.id)
    assert attempts.task.status == "succeeded"
