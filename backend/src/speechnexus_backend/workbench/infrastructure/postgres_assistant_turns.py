"""Assistant turn rows with their project scope, receipts and stable failures.

A turn's transcript and voice are fixed at creation; only progress, the answer and a
provider-free error move afterwards. The owning project row is the serialization point.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

from speechnexus_backend.workbench.application.assistant import AssistantTurnStore
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.assistant import (
    AssistantError,
    AssistantStateConflictError,
    AssistantStatus,
    AssistantTurn,
    ResourcePin,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)

_COLUMNS = """
    id, project_id, status, revision, input_text, output_text, voice_id, voice_revision,
    model, profile_id, profile_revision, speech_job_id, playback_generation, error,
    created_at, updated_at
"""


@dataclass(frozen=True)
class _TurnRow:
    id: str
    project_id: str
    status: str
    revision: Decimal
    input_text: str
    output_text: str | None
    voice_id: str
    voice_revision: Decimal
    model: str | None
    profile_id: str | None
    profile_revision: Decimal | None
    speech_job_id: str | None
    playback_generation: int
    error: dict[str, object] | None
    created_at: datetime
    updated_at: datetime

    def profile(self) -> ResourcePin | None:
        if self.profile_id is None or self.profile_revision is None:
            return None
        return ResourcePin(self.profile_id, int(self.profile_revision))

    def failure(self) -> AssistantError | None:
        if self.error is None:
            return None
        code, message, retryable = (
            self.error.get("code"),
            self.error.get("message"),
            self.error.get("retryable"),
        )
        if (
            not isinstance(code, str)
            or not isinstance(message, str)
            or not isinstance(retryable, bool)
        ):
            raise ValueError("invalid stored assistant error")
        return AssistantError(code, message, retryable)

    def domain(self) -> AssistantTurn:
        return AssistantTurn(
            id=self.id,
            project_id=self.project_id,
            status=cast(AssistantStatus, self.status),
            revision=int(self.revision),
            input_text=self.input_text,
            voice=ResourcePin(self.voice_id, int(self.voice_revision)),
            playback_generation=int(self.playback_generation),
            created_at=self.created_at,
            updated_at=self.updated_at,
            model=self.model,
            profile=self.profile(),
            speech_job_id=self.speech_job_id,
            output_text=self.output_text,
            error=self.failure(),
        )


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    turn_id: str


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class PostgresAssistantTurns(AssistantTurnStore):
    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions

    async def _project(
        self, base: _Session, project_id: str, workspace_id: str, *, lock: bool
    ) -> None:
        project = await base.get_project(project_id, for_update=lock)
        if project is None or project.workspace_id != workspace_id:
            raise ResourceNotFoundError

    async def _row(self, base: _Session, turn_id: str, *, lock: bool = False) -> _TurnRow | None:
        suffix = " FOR UPDATE" if lock else ""
        return await base._one(
            _TurnRow,
            f"SELECT {_COLUMNS} FROM {{s}}.assistant_turns WHERE id = %s{suffix}",
            (turn_id,),
        )

    async def _receipt(self, base: _Session, scope_id: str, key_digest: str) -> _ReceiptRow | None:
        return await base._one(
            _ReceiptRow,
            """
            SELECT intent_digest, turn_id FROM {s}.assistant_turn_receipts
            WHERE scope_id = %s AND key_digest = %s
        """,
            (scope_id, key_digest),
        )

    @staticmethod
    def _notes(key: str | None, intent: list[object]) -> tuple[str | None, str]:
        if key is None:
            return None, ""
        return hashlib.sha256(key.encode("utf-8")).hexdigest(), _digest(intent)

    async def create_turn(
        self,
        *,
        workspace_id: str,
        project_id: str,
        input_text: str,
        voice_id: str,
        voice_revision: int,
        model: str | None,
        profile_id: str | None,
        profile_revision: int | None,
        key: str | None,
    ) -> AssistantTurn:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await self._project(base, project_id, workspace_id, lock=True)
            digest, intent = self._notes(
                key,
                [
                    "assistant.create",
                    project_id,
                    input_text,
                    voice_id,
                    voice_revision,
                    model,
                    profile_id,
                    profile_revision,
                ],
            )
            if digest is not None:
                stored = await self._receipt(base, project_id, digest)
                if stored is not None:
                    if stored.intent_digest != intent:
                        raise IdempotencyConflictError
                    previous = await self._row(base, stored.turn_id)
                    if previous is None:
                        raise ResourceNotFoundError
                    return previous.domain()
            now = await base.now()
            turn = AssistantTurn.create(
                turn_id=uuid4().hex,
                project_id=project_id,
                input_text=input_text,
                voice=ResourcePin(voice_id, voice_revision),
                now=now,
                model=model,
                profile=None
                if profile_id is None or profile_revision is None
                else ResourcePin(profile_id, profile_revision),
            )
            await base._execute(
                """
                INSERT INTO {s}.assistant_turns (
                    id, project_id, status, revision, input_text, output_text,
                    voice_id, voice_revision, model, profile_id, profile_revision,
                    speech_job_id, playback_generation, error, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, NULL, %s, NULL, %s, %s
                )
            """,
                (
                    turn.id,
                    project_id,
                    turn.status,
                    turn.revision,
                    turn.input_text,
                    turn.voice.id,
                    turn.voice.revision,
                    turn.model,
                    None if turn.profile is None else turn.profile.id,
                    None if turn.profile is None else turn.profile.revision,
                    turn.playback_generation,
                    now,
                    now,
                ),
            )
            if digest is not None:
                await base._execute(
                    """
                    INSERT INTO {s}.assistant_turn_receipts
                        (scope_id, key_digest, intent_digest, turn_id, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """,
                    (project_id, digest, intent, turn.id, now),
                )
            return turn

    async def get_turn(self, *, turn_id: str, workspace_id: str) -> AssistantTurn:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._row(base, turn_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=False)
            return row.domain()

    async def cancel_current_turn(
        self, *, turn_id: str, workspace_id: str
    ) -> tuple[AssistantTurn, bool]:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._row(base, turn_id, lock=True)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=True)
            current = row.domain()
            try:
                # The domain owns the terminal-state rule; a turn already finished is
                # reported as unchanged so a repeated cancellation stays idempotent.
                cancelled = current.cancel(expected_revision=current.revision, now=await base.now())
            except AssistantStateConflictError:
                return current, False
            await base._execute(
                """
                UPDATE {s}.assistant_turns SET status = %s, revision = %s, updated_at = %s
                WHERE id = %s
            """,
                (cancelled.status, cancelled.revision, cancelled.updated_at, turn_id),
            )
            return cancelled, True
