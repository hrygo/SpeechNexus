"""The workbench status must report the deployment's real capability states.

Consumers may not infer readiness from configuration they cannot see and must not fill a
missing capability in themselves, so the serving runtime reports one of
available / disabled / unavailable per capability. These cases pin the mapping for an
unconfigured deployment, for a deployment with the intranet LLM and a speech base, and
for export production with and without a producer.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tests.integration.test_workbench_runtime import Database, serve
from tests.integration.test_workbench_runtime import database as database

from speechnexus_backend.workbench.application.export_worker import ProducedExport
from speechnexus_backend.workbench.runtime import create_runtime_app

pytestmark = pytest.mark.integration
LLM_MODEL = "synthetic-local-model"


class _Bundle:
    async def produce_export(self, job: Any, *, report: Any) -> ProducedExport:
        return ProducedExport(content=b"synthetic\n", media_type="application/zip")


async def status_of(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/v1/workbench/status")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def states(body: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item["status"] for item in body["capabilities"]}


async def test_an_unconfigured_deployment_reports_no_fabricated_capability(
    database: Database,
) -> None:
    async with serve(create_runtime_app(database.settings())) as client:
        body = await status_of(client)

    assert body["workbench_enabled"] is True
    assert body["llm_enabled"] is False
    assert "minutes_profile" not in body
    assert states(body) == {
        "transcription": "disabled",
        "meeting_minutes": "disabled",
        "assistant": "disabled",
        "teleprompter": "available",
        "audiobook_export": "disabled",
    }


async def test_a_configured_llm_and_speech_base_report_the_capabilities_they_enable(
    database: Database,
) -> None:
    settings = database.settings().model_copy(
        update={
            "llm_endpoint": "http://127.0.0.1:59999/v1/chat/completions",
            "llm_model": LLM_MODEL,
            "llm_qualification_reference": "adr-0003-approved-local-intranet-text-llm",
            "speech_base_url": "http://127.0.0.1:59999",
            "object_endpoint": "http://127.0.0.1:59998",
            "object_bucket": "synthetic-capability-bucket",
            "object_access_key": SecretStr("synthetic-access"),
            "object_secret_key": SecretStr("synthetic-object-secret"),
        }
    )
    async with serve(create_runtime_app(settings)) as client:
        body = await status_of(client)

    assert body["llm_enabled"] is True
    assert body["minutes_profile"]["llm_model"] == LLM_MODEL
    assert states(body) == {
        "transcription": "available",
        "meeting_minutes": "available",
        "assistant": "available",
        "teleprompter": "available",
        "audiobook_export": "disabled",
    }


async def test_export_production_is_available_only_with_a_producer_and_a_running_loop(
    database: Database,
) -> None:
    async with serve(create_runtime_app(database.settings())) as client:
        off = states(await status_of(client))

    asked_for_it = database.settings().model_copy(update={"export_interval_seconds": 0.5})
    async with serve(create_runtime_app(asked_for_it)) as client:
        without_producer = states(await status_of(client))

    # Producing needs both a producer and somewhere to publish its bytes.
    with_producer = asked_for_it.model_copy(
        update={
            "object_endpoint": "http://127.0.0.1:59998",
            "object_bucket": "synthetic-capability-bucket",
            "object_access_key": SecretStr("synthetic-access"),
            "object_secret_key": SecretStr("synthetic-object-secret"),
        }
    )
    async with serve(create_runtime_app(with_producer, export_producer=_Bundle())) as client:
        body = await status_of(client)

    assert off["audiobook_export"] == "disabled"
    assert without_producer["audiobook_export"] == "unavailable"
    assert states(body)["audiobook_export"] == "available"
