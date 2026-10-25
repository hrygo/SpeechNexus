"""Sealed-source LLM calls outside actual PG transactions; synthetic model only."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest
from tests.integration.test_workbench_listings_postgres import delete
from tests.integration.test_workbench_sealing_postgres import BODY, content, counts, upgrade
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.llm import LlmError, LlmProfile
from speechnexus_backend.workbench.application.minutes import MinutesGeneration
from speechnexus_backend.workbench.domain.deletion import DeletionTarget
from speechnexus_backend.workbench.infrastructure.openai_text_llm import (
    OpenAiTextLlm,
    TextLlmSettings,
)

pytestmark = pytest.mark.integration
PROFILE = LlmProfile(model="synthetic-model", template_revision=1, system_prompt="合成纪要模板")


def configured(transport_factory: Callable[[], httpx.AsyncBaseTransport]) -> OpenAiTextLlm:
    return OpenAiTextLlm(
        TextLlmSettings(
            enabled=True,
            endpoint="http://127.0.0.1:8100/v1/chat/completions",
            profile=PROFILE,
            qualification_reference="synthetic-test-only",
        ),
        transport_factory=transport_factory,
    )


async def assert_no_runtime_connection(store: Store) -> None:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        row = await (
            await owner.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND usename = %s",
                (store.role,),
            )
        ).fetchone()
    assert row == (0,)


def result() -> dict[str, Any]:
    return {
        "model": PROFILE.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "合成纪要候选"},
                "finish_reason": "stop",
            }
        ],
    }


async def test_real_sealed_source_generated_outside_transaction_and_history_unchanged(
    store: Store,
) -> None:
    _, original = await content(store)
    await upgrade(store)
    sealed = await store.app.seal_transcript(transcript_id=original.id, expected_revision=1)
    before = await counts(store)
    calls = 0

    async def serve(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await assert_no_runtime_connection(store)
        assert json.loads(request.content)["messages"][1]["content"] == BODY
        assert await asyncio.wait_for(store.app.get_transcript(original.id), 1) == sealed
        return httpx.Response(200, json=result())

    candidate = await MinutesGeneration(
        store.app, configured(lambda: httpx.MockTransport(serve))
    ).generate(transcript_id=original.id, source_revision=2, template_revision=1)
    assert candidate.text == "合成纪要候选" and candidate.usage is None and calls == 1
    assert await counts(store) == before
    assert await store.app.get_revision(original.id, 1) == original.head
    assert await store.app.get_transcript(original.id) == sealed


@pytest.mark.parametrize("kind", ["project", "transcript"])
async def test_delete_acceptance_during_llm_denies_late_candidate_before_cleanup(
    store: Store, kind: str
) -> None:
    parent, original = await content(store)
    await upgrade(store)
    await store.app.seal_transcript(transcript_id=original.id, expected_revision=1)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def serve(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await assert_no_runtime_connection(store)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=result())

    task = asyncio.create_task(
        MinutesGeneration(store.app, configured(lambda: httpx.MockTransport(serve))).generate(
            transcript_id=original.id, source_revision=2, template_revision=1
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        identity = parent.id if kind == "project" else original.id
        revision = 1 if kind == "project" else 2
        record = await delete(store, parent, DeletionTarget("workbench", kind, identity, revision))  # type: ignore[arg-type]
        assert record.result is None
        release.set()
        with pytest.raises(LlmError, match="source_unavailable"):
            await task
        assert calls == 1
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def test_revision_during_llm_stales_candidate_and_preserves_original(store: Store) -> None:
    _, original = await content(store)
    await upgrade(store)
    sealed = await store.app.seal_transcript(transcript_id=original.id, expected_revision=1)

    async def serve(_: httpx.Request) -> httpx.Response:
        await assert_no_runtime_connection(store)
        edited = await asyncio.wait_for(
            store.app.revise_transcript(
                transcript_id=original.id,
                expected_revision=2,
                text="合成新校对版本",
            ),
            1,
        )
        assert edited.head.number == 3
        return httpx.Response(200, json=result())

    with pytest.raises(LlmError, match="source_changed"):
        await MinutesGeneration(store.app, configured(lambda: httpx.MockTransport(serve))).generate(
            transcript_id=original.id,
            source_revision=2,
            template_revision=1,
        )
    assert await store.app.get_revision(original.id, 2) == sealed.head
    assert await store.app.get_revision(original.id, 1) == original.head
