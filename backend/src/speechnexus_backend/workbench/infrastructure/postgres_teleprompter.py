"""Teleprompter position and request receipts share the bounded workbench transaction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.teleprompter import TeleprompterState
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)


@dataclass(frozen=True)
class _Row:
    project_id: str
    script_revision: Decimal
    visible_position: Decimal
    manual_hold: bool
    updated_at: datetime

    def domain(self) -> TeleprompterState:
        return TeleprompterState(
            project_id=self.project_id,
            script_revision=int(self.script_revision),
            visible_position=int(self.visible_position),
            manual_hold=self.manual_hold,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    script_revision: Decimal
    visible_position: Decimal
    manual_hold: bool
    updated_at: datetime

    def domain(self, project_id: str) -> TeleprompterState:
        return TeleprompterState(
            project_id=project_id,
            script_revision=int(self.script_revision),
            visible_position=int(self.visible_position),
            manual_hold=self.manual_hold,
            updated_at=self.updated_at,
        )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class PostgresTeleprompter:
    """One project lock serializes read-modify-write; no advisory lock or retry is added."""

    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions

    async def _project(
        self, base: _Session, project_id: str, workspace_id: str, *, lock: bool
    ) -> None:
        # The project row is the single serialization point for that project's position.
        # A tombstoned project is invisible here, so deletion wins without extra state.
        project = await base.get_project(project_id, for_update=lock)
        if project is None or project.workspace_id != workspace_id:
            raise ResourceNotFoundError

    async def _state(self, base: _Session, project_id: str) -> TeleprompterState | None:
        row = await base._one(
            _Row,
            """
            SELECT project_id, script_revision, visible_position, manual_hold, updated_at
            FROM {s}.teleprompter_states WHERE project_id = %s
        """,
            (project_id,),
        )
        return None if row is None else row.domain()

    async def get(self, *, project_id: str, workspace_id: str) -> TeleprompterState:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await self._project(base, project_id, workspace_id, lock=False)
            state = await self._state(base, project_id)
            if state is None:
                # A project without a saved position is not an empty position.
                raise ResourceNotFoundError
            return state

    async def update(
        self,
        *,
        project_id: str,
        workspace_id: str,
        script_revision: int,
        visible_position: int,
        manual_hold: bool,
        key: str | None,
    ) -> TeleprompterState:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await self._project(base, project_id, workspace_id, lock=True)
            digest: str | None = None
            intent_digest: str | None = None
            if key is not None:
                digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
                intent_digest = _digest(
                    ["teleprompter.update", script_revision, visible_position, manual_hold]
                )
                stored = await base._one(
                    _ReceiptRow,
                    """
                    SELECT intent_digest, script_revision, visible_position, manual_hold,
                        updated_at
                    FROM {s}.teleprompter_receipts
                    WHERE project_id = %s AND key_digest = %s
                """,
                    (project_id, digest),
                )
                if stored is not None:
                    if stored.intent_digest != intent_digest:
                        raise IdempotencyConflictError
                    return stored.domain(project_id)
            current = await self._state(base, project_id)
            now = await base.now()
            state = (
                TeleprompterState.initial(
                    project_id=project_id,
                    script_revision=script_revision,
                    visible_position=visible_position,
                    manual_hold=manual_hold,
                    updated_at=now,
                )
                if current is None
                else current.advance(
                    script_revision=script_revision,
                    visible_position=visible_position,
                    manual_hold=manual_hold,
                    updated_at=now,
                )
            )
            await base._execute(
                """
                INSERT INTO {s}.teleprompter_states
                    (project_id, script_revision, visible_position, manual_hold, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (project_id) DO UPDATE SET
                    script_revision = EXCLUDED.script_revision,
                    visible_position = EXCLUDED.visible_position,
                    manual_hold = EXCLUDED.manual_hold,
                    updated_at = EXCLUDED.updated_at
            """,
                (
                    project_id,
                    state.script_revision,
                    state.visible_position,
                    state.manual_hold,
                    state.updated_at,
                ),
            )
            if digest is not None:
                assert intent_digest is not None
                await base._execute(
                    """
                    INSERT INTO {s}.teleprompter_receipts
                        (project_id, key_digest, intent_digest, script_revision,
                            visible_position, manual_hold, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        project_id,
                        digest,
                        intent_digest,
                        state.script_revision,
                        state.visible_position,
                        state.manual_hold,
                        state.updated_at,
                    ),
                )
            return state
