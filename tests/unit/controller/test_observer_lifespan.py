"""The production lifespan runs observation off the event loop and joins before store close."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from threading import Event

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.fake.controller import (
    ScriptedNodeRuntime,
    build_request,
)
from tests.fake.readiness import StaticProbe
from tests.unit.controller.test_local_observer import service_for

import speechnexus_node_controller.app as app_module
from speechnexus_node_controller.application.readiness import ReadinessService
from speechnexus_node_controller.domain.intents import (
    InstanceObservation,
    RuntimeInstanceIntentRequest,
)
from speechnexus_node_controller.domain.templates import ApprovedRuntimeTemplate
from speechnexus_node_controller.infrastructure.sqlite_intent_store import SqliteIntentStore
from speechnexus_node_controller.settings import Settings

TOKEN = "synthetic-controller-component-token"


def owned_app(
    tmp_path: Path, runtime: ScriptedNodeRuntime, monkeypatch: pytest.MonkeyPatch
) -> tuple[FastAPI, SqliteIntentStore]:
    store = SqliteIntentStore(tmp_path / "intents.sqlite3")
    service = service_for(store, runtime)
    service.submit_intent(build_request())
    readiness = ReadinessService([StaticProbe("synthetic_daemon")], timeout_seconds=1)
    monkeypatch.setattr(
        app_module, "_build_dependencies", lambda _: (service, readiness, store, None)
    )
    settings = Settings(
        component_token=TOKEN,
        approved_templates_file=tmp_path / "unused-template.json",
        intent_store_path=store.path,
        observation_interval_seconds=0.01,
        observation_batch_size=1,
    )
    return app_module.create_app(settings=settings), store


async def test_read_only_get_observes_background_progress_and_owned_store_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = ScriptedNodeRuntime()
    app, store = owned_app(tmp_path, runtime, monkeypatch)
    async with app.router.lifespan_context(app):
        runtime.mark_ready(build_request())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async with asyncio.timeout(3):
                while True:
                    response = await client.get(
                        "/internal/v1/nodes/node-local-1/instance-intents/intent-1",
                        headers={"Authorization": f"Bearer {TOKEN}"},
                    )
                    assert response.status_code == 200
                    if response.json()["intent_state"] == "succeeded":
                        break
                    await asyncio.sleep(0.01)
        assert len(runtime.prepared) == 1 and runtime.stopped == []
        assert runtime.inspections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.probe()


class BlockingInspection(ScriptedNodeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.entered, self.release = Event(), Event()

    def inspect(
        self, request: RuntimeInstanceIntentRequest, template: ApprovedRuntimeTemplate
    ) -> InstanceObservation | None:
        self.entered.set()
        assert self.release.wait(3)
        return super().inspect(request, template)


async def test_shutdown_finishes_active_inspection_before_closing_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = BlockingInspection()
    app, store = owned_app(tmp_path, runtime, monkeypatch)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    try:
        assert await asyncio.to_thread(runtime.entered.wait, 2)
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.sleep(0.01)
        assert not closing.done()
        store.probe()
    finally:
        runtime.release.set()
    await asyncio.wait_for(closing, timeout=2)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.probe()
