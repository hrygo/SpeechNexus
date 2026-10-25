"""Assistant turns over the disposable PostgreSQL store.

These tests hold the adapter to stored state: what the row really holds, how a replayed
request replays, and that erasing the owning project erases the turn and its receipt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.assistant import (
    AssistantError,
    AssistantTurn,
)
from speechnexus_backend.workbench.infrastructure.assistant_schema import (
    assistant_migration_sql,
    grant_assistant_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.postgres_assistant_turns import (
    PostgresAssistantTurns,
)

pytestmark = pytest.mark.integration
WORKSPACE = "workspace-assistant"
OTHER_WORKSPACE = "workspace-assistant-other"
KEY = "assistant-request-key-0001"
INPUT = "合成提问：请总结这段合成内容"


@pytest.fixture
async def turns_store(store: Store) -> AsyncIterator[Store]:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(assistant_migration_sql(store.schema))
        await owner.execute(grant_assistant_runtime_sql(store.schema, store.role))
    yield store


def turns(store: Store) -> PostgresAssistantTurns:
    return PostgresAssistantTurns(store.uow)


async def project(store: Store) -> str:
    created = await WorkbenchVersions(store.uow).create_project(
        workspace_id=WORKSPACE, name="合成助手项目", idempotency_key="assistant-project-key"
    )
    return created.id


async def create(
    store: Store, project_id: str, *, key: str | None = KEY, text: str = INPUT
) -> AssistantTurn:
    return await turns(store).create_turn(
        workspace_id=WORKSPACE,
        project_id=project_id,
        input_text=text,
        voice_id="voice_demo",
        voice_revision=1,
        model="internal-llm",
        profile_id="binding_demo",
        profile_revision=3,
        key=key,
    )


async def stored(store: Store, turn_id: str) -> dict[str, Any]:
    async with await psycopg.AsyncConnection.connect(
        store.owner_dsn, row_factory=dict_row
    ) as owner:
        row = await (
            await owner.execute(
                sql.SQL(
                    "SELECT status, revision, input_text, output_text, voice_id,"
                    " voice_revision, model, profile_id, profile_revision,"
                    " playback_generation, error FROM {}.assistant_turns WHERE id = %s"
                ).format(sql.Identifier(store.schema)),
                (turn_id,),
            )
        ).fetchone()
    assert row is not None
    return row


async def rows(store: Store, table: str) -> int:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(store.schema), sql.Identifier(table)
                )
            )
        ).fetchone()
    assert row is not None
    return int(row[0])


async def test_create_pins_the_project_and_replays_its_receipt(turns_store: Store) -> None:
    project_id = await project(turns_store)
    turn = await create(turns_store, project_id)

    assert (turn.status, turn.revision, turn.playback_generation) == ("queued", 1, 1)
    assert (turn.output_text, turn.error, turn.speech_job_id) == (None, None, None)
    assert (turn.voice.id, turn.voice.revision) == ("voice_demo", 1)
    assert turn.profile is not None
    assert (turn.profile.id, turn.profile.revision) == ("binding_demo", 3)

    row = await stored(turns_store, turn.id)
    assert (row["status"], row["revision"], row["playback_generation"]) == ("queued", 1, 1)
    assert (row["input_text"], row["output_text"], row["error"]) == (INPUT, None, None)
    assert (row["voice_id"], int(row["voice_revision"])) == ("voice_demo", 1)
    assert (row["model"], row["profile_id"], int(row["profile_revision"])) == (
        "internal-llm",
        "binding_demo",
        3,
    )

    replay = await create(turns_store, project_id)
    assert replay == turn
    with pytest.raises(IdempotencyConflictError):
        await create(turns_store, project_id, text="合成提问：另一个问题")
    assert await rows(turns_store, "assistant_turns") == 1
    assert await rows(turns_store, "assistant_turn_receipts") == 1


async def test_get_refuses_missing_turns_and_foreign_workspaces(turns_store: Store) -> None:
    project_id = await project(turns_store)
    turn = await create(turns_store, project_id)
    store = turns(turns_store)

    assert await store.get_turn(turn_id=turn.id, workspace_id=WORKSPACE) == turn
    with pytest.raises(ResourceNotFoundError):
        await store.get_turn(turn_id="turn_absent", workspace_id=WORKSPACE)
    with pytest.raises(ResourceNotFoundError):
        await store.get_turn(turn_id=turn.id, workspace_id=OTHER_WORKSPACE)
    with pytest.raises(ResourceNotFoundError):
        await store.cancel_current_turn(turn_id=turn.id, workspace_id=OTHER_WORKSPACE)


async def test_cancel_persists_and_repeats_without_effect(
    turns_store: Store,
) -> None:
    project_id = await project(turns_store)
    turn = await create(turns_store, project_id)
    store = turns(turns_store)

    cancelled, changed = await store.cancel_current_turn(turn_id=turn.id, workspace_id=WORKSPACE)
    assert changed is True
    assert (cancelled.status, cancelled.revision, cancelled.error) == ("cancelled", 2, None)
    row = await stored(turns_store, turn.id)
    assert (row["status"], row["revision"]) == ("cancelled", 2)

    # Cancellation is idempotent by state: repeating it changes nothing and says so.
    again, changed_again = await store.cancel_current_turn(turn_id=turn.id, workspace_id=WORKSPACE)
    assert (again, changed_again) == (cancelled, False)
    assert await store.get_turn(turn_id=turn.id, workspace_id=WORKSPACE) == cancelled


async def test_a_stored_failure_reads_back_and_project_erasure_takes_the_turn(
    turns_store: Store,
) -> None:
    project_id = await project(turns_store)
    turn = await create(turns_store, project_id)

    # The executor will write this state; the adapter must read it back faithfully.
    async with await psycopg.AsyncConnection.connect(turns_store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL(
                "UPDATE {}.assistant_turns SET status = 'failed', revision = 2,"
                " error = %s, updated_at = now() WHERE id = %s"
            ).format(sql.Identifier(turns_store.schema)),
            (Jsonb({"code": "unavailable", "message": "合成失败原因", "retryable": True}), turn.id),
        )
    failed = await turns(turns_store).get_turn(turn_id=turn.id, workspace_id=WORKSPACE)
    assert failed.status == "failed"
    assert failed.error == AssistantError("unavailable", "合成失败原因", True)
    assert failed.output_text is None

    # Erasing the owning project must take the turn and its receipt, never the reverse.
    # The version rows are removed first, exactly as the deletion cleanup does.
    async with await psycopg.AsyncConnection.connect(turns_store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL("DELETE FROM {}.mutation_receipts WHERE project_id = %s").format(
                sql.Identifier(turns_store.schema)
            ),
            (project_id,),
        )
        await owner.execute(
            sql.SQL("DELETE FROM {}.project_versions WHERE project_id = %s").format(
                sql.Identifier(turns_store.schema)
            ),
            (project_id,),
        )
        await owner.execute(
            sql.SQL("DELETE FROM {}.projects WHERE id = %s").format(
                sql.Identifier(turns_store.schema)
            ),
            (project_id,),
        )
    assert await rows(turns_store, "assistant_turns") == 0
    assert await rows(turns_store, "assistant_turn_receipts") == 0
