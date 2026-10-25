"""Assistant operations over the real ASGI runtime and disposable PostgreSQL.

No server-side intranet LLM is configured here, so these tests hold the public
surface to the default-off guarantee: a stable refusal that stores nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import psycopg
import pytest
from psycopg import sql
from tests.integration.test_workbench_runtime import Database, create_project, serve
from tests.integration.test_workbench_runtime import database as database

pytestmark = pytest.mark.integration
CREATE_KEY = "assistant-http-key-0001"
INPUT = "合成提问：请总结这段合成内容"
BODY: dict[str, Any] = {"input_text": INPUT, "voice": {"id": "voice_demo", "revision": "1"}}


@pytest.fixture
async def client(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    from speechnexus_backend.workbench.runtime import create_app_from_env

    for key, value in database.settings().model_dump().items():
        if value is None:
            # An unset optional service stays unconfigured instead of becoming "None".
            continue
        plain = (
            value.get_secret_value()
            if hasattr(value, "get_secret_value")
            else json.dumps(value)
            if isinstance(value, tuple)
            else str(value)
        )
        monkeypatch.setenv("SPEECHNEXUS_WORKBENCH_" + key.upper(), plain)
    async with serve(create_app_from_env()) as client:
        yield client


async def rows(database: Database, table: str) -> int:
    async with await psycopg.AsyncConnection.connect(database.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(database.schema), sql.Identifier(table)
                )
            )
        ).fetchone()
    assert row is not None
    return int(row[0])


async def test_creation_is_refused_and_stores_nothing_while_the_llm_is_off(
    client: httpx.AsyncClient, database: Database
) -> None:
    project = await create_project(client, "assistant-http-project-key", "合成助手项目")

    refused = await client.post(
        f"/v1/workbench/projects/{project}/assistant-turns",
        json=BODY,
        headers={"Idempotency-Key": CREATE_KEY},
    )
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "llm_disabled"
    # The refusal echoes neither the prompt nor any private pin, and accepts nothing.
    assert INPUT not in refused.text
    assert "voice_demo" not in refused.text
    assert await rows(database, "assistant_turns") == 0
    assert await rows(database, "assistant_turn_receipts") == 0


async def test_assistant_requests_are_validated_and_unknown_turns_are_absent(
    client: httpx.AsyncClient,
) -> None:
    project = await create_project(client, "assistant-http-project-key-2", "合成助手项目二")

    missing = await client.post(
        f"/v1/workbench/projects/{project}/assistant-turns",
        json={key: value for key, value in BODY.items() if key != "voice"},
        headers={"Idempotency-Key": CREATE_KEY},
    )
    assert missing.status_code in {400, 422}, missing.text
    assert "voice_demo" not in missing.text

    unknown = await client.get("/v1/workbench/assistant-turns/turn_absent")
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "resource_not_found"

    cancel = await client.post(
        "/v1/workbench/assistant-turns/turn_absent/cancellations",
        headers={"Idempotency-Key": "assistant-http-cancel-key-0001"},
    )
    assert cancel.status_code == 404, cancel.text
    assert cancel.json()["error"]["code"] == "resource_not_found"
