"""遥提示状态的写入是「先更新、未命中再插入」,不是 upsert."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from speechnexus_backend.workbench.domain.teleprompter import TeleprompterState
from speechnexus_backend.workbench.infrastructure.postgres_teleprompter import _write_state

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


class Session:
    def __init__(self, *, changed: int) -> None:
        self.changed = changed
        self.statements: list[tuple[str, str]] = []

    async def _execute_changed(self, query: str, params: Any = ()) -> int:
        self.statements.append(("update", query))
        return self.changed

    async def _execute(self, query: str, params: Any = ()) -> None:
        self.statements.append(("insert", query))


def state() -> TeleprompterState:
    return TeleprompterState.initial(
        project_id="project-demo",
        script_revision=3,
        visible_position=120,
        manual_hold=False,
        updated_at=NOW,
    )


@pytest.mark.anyio
async def test_an_existing_row_is_updated_and_never_inserted() -> None:
    session = Session(changed=1)
    await _write_state(session, "project-demo", state())  # type: ignore[arg-type]
    assert [kind for kind, _ in session.statements] == ["update"]


@pytest.mark.anyio
async def test_a_missing_row_falls_through_to_one_insert() -> None:
    session = Session(changed=0)
    await _write_state(session, "project-demo", state())  # type: ignore[arg-type]
    assert [kind for kind, _ in session.statements] == ["update", "insert"]
    update, insert = (query for _, query in session.statements)
    assert "ON CONFLICT" not in update and "ON CONFLICT" not in insert
    assert insert.lstrip().startswith("INSERT INTO {s}.teleprompter_states")
