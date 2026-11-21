"""Meetings and task links share the bounded workbench transaction lifecycle."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from speechnexus_backend.workbench.application.llm import LlmError, LlmProfile, valid_text
from speechnexus_backend.workbench.application.meetings import MeetingView
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTaskView
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.meetings import Meeting
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.domain.versions import Transcript, Unset
from speechnexus_backend.workbench.infrastructure.postgres_minutes_tasks import PostgresMinutesTasks
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)


@dataclass(frozen=True)
class _Row:
    id: str
    workspace_id: str
    project_id: str
    transcript_id: str
    display_name: str
    save_audio: bool
    started_at: datetime
    updated_at: datetime
    version: Decimal
    ended_at: datetime | None
    sealed_revision: Decimal | None
    sealed_revision_id: str | None
    source_sha256: str | None
    minutes_id: str | None

    def domain(self) -> Meeting:
        values = asdict(self)
        values["version"] = int(self.version)
        values["sealed_revision"] = (
            None if self.sealed_revision is None else int(self.sealed_revision)
        )
        return Meeting(**values)


@dataclass(frozen=True)
class _Receipt:
    intent_digest: str
    meeting_id: str | None
    version: Decimal
    updated_at: datetime
    ended_at: datetime | None
    sealed_revision: Decimal | None
    sealed_revision_id: str | None
    source_sha256: str | None
    minutes_id: str | None

    def snapshot(self, current: Meeting) -> Meeting:
        if self.meeting_id != current.id:
            raise ResourceNotFoundError
        return replace(
            current,
            version=int(self.version),
            updated_at=self.updated_at,
            ended_at=self.ended_at,
            sealed_revision=None if self.sealed_revision is None else int(self.sealed_revision),
            sealed_revision_id=self.sealed_revision_id,
            source_sha256=self.source_sha256,
            minutes_id=self.minutes_id,
        )


@dataclass(frozen=True)
class _Id:
    id: str


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class PostgresMeetings:
    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions
        self._tasks = PostgresMinutesTasks(versions)

    async def _row(self, base: _Session, meeting_id: str, *, lock: bool = False) -> Meeting:
        row = await base._one(
            _Row,
            "SELECT * FROM {s}.meetings WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (meeting_id,),
        )
        if row is None:
            raise ResourceNotFoundError
        return row.domain()

    async def _source(
        self, base: _Session, *, workspace: str, project: str, transcript: str
    ) -> Transcript:
        parent = await base.get_project(project, for_update=True)
        if parent is None or parent.workspace_id != workspace:
            raise ResourceNotFoundError
        source = await base.get_transcript(transcript, for_update=True)
        if source is None or source.project_id != project:
            raise ResourceNotFoundError
        return source

    async def _locked(
        self, base: _Session, meeting_id: str, workspace: str
    ) -> tuple[Meeting, Transcript]:
        probe = await self._row(base, meeting_id)
        source = await self._source(
            base, workspace=workspace, project=probe.project_id, transcript=probe.transcript_id
        )
        if await base._deleted("meeting", meeting_id):
            raise ResourceNotFoundError
        current = await self._row(base, meeting_id, lock=True)
        if current.workspace_id != workspace:
            raise ResourceNotFoundError
        return current, source

    async def _claim(
        self,
        base: _Session,
        workspace: str,
        operation: str,
        scope: str,
        key: str | None,
        intent: object,
    ) -> tuple[str, str, _Receipt | None]:
        if key is not None and not valid_text(key, 128):
            raise ValueError("invalid meeting request key")
        digest = _digest(["provided", key] if key is not None else ["unkeyed", uuid4().hex])
        intent_digest = _digest(intent)
        bucket = int.from_bytes(
            sha256(
                json.dumps([base._schema.as_string(), workspace, operation, scope, digest]).encode()
            ).digest()[:8],
            signed=True,
        )
        await base._execute("SELECT pg_advisory_xact_lock(%s)", (bucket,))
        row = await base._one(
            _Receipt,
            """SELECT intent_digest,meeting_id,version,updated_at,ended_at,
            sealed_revision,sealed_revision_id,source_sha256,minutes_id FROM {s}.meeting_receipts
            WHERE workspace_id=%s AND operation=%s AND scope_id=%s AND key_digest=%s""",
            (workspace, operation, scope, digest),
        )
        if row is not None and row.intent_digest != intent_digest:
            raise IdempotencyConflictError
        if row is not None and row.meeting_id is None:
            raise ResourceNotFoundError
        return digest, intent_digest, row

    async def _receipt(
        self, base: _Session, operation: str, scope: str, digest: str, intent: str, meeting: Meeting
    ) -> None:
        await base._execute(
            """INSERT INTO {s}.meeting_receipts
            (workspace_id,operation,scope_id,key_digest,intent_digest,meeting_id,version,updated_at,
             ended_at,sealed_revision,sealed_revision_id,source_sha256,minutes_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                meeting.workspace_id,
                operation,
                scope,
                digest,
                intent,
                meeting.id,
                meeting.version,
                meeting.updated_at,
                meeting.ended_at,
                meeting.sealed_revision,
                meeting.sealed_revision_id,
                meeting.source_sha256,
                meeting.minutes_id,
            ),
        )

    async def _save(self, base: _Session, previous: Meeting, changed: Meeting) -> None:
        row = await base._one(
            _Id,
            """UPDATE {s}.meetings SET version=%s,updated_at=%s,ended_at=%s,
            sealed_revision=%s,sealed_revision_id=%s,source_sha256=%s,minutes_id=%s
            WHERE id=%s AND version=%s RETURNING id""",
            (
                changed.version,
                changed.updated_at,
                changed.ended_at,
                changed.sealed_revision,
                changed.sealed_revision_id,
                changed.source_sha256,
                changed.minutes_id,
                changed.id,
                previous.version,
            ),
        )
        if row is None:
            raise MinutesTaskConflictError

    async def _view(self, base: _Session, meeting: Meeting) -> MeetingView:
        minutes = None
        if meeting.minutes_id is not None:
            minutes = (await self._tasks._visible(base, meeting.minutes_id))[0]
        return MeetingView(meeting, minutes)

    async def create(
        self,
        *,
        project_id: str,
        workspace_id: str,
        transcript_id: str,
        display_name: str,
        key: str | None,
    ) -> Meeting:
        async with self._versions.transaction() as session:
            assert isinstance(session, _Session)
            source = await self._source(
                session, workspace=workspace_id, project=project_id, transcript=transcript_id
            )
            digest, intent, prior = await self._claim(
                session,
                workspace_id,
                "create",
                project_id,
                key,
                {"transcript_id": transcript_id, "display_name": display_name, "save_audio": False},
            )
            if prior is not None:
                assert prior.meeting_id is not None
                current, _ = await self._locked(session, prior.meeting_id, workspace_id)
                return prior.snapshot(current)
            meeting = Meeting.create(
                meeting_id=uuid4().hex,
                workspace_id=workspace_id,
                project_id=project_id,
                transcript=source,
                display_name=display_name,
                now=await session.now(),
            )
            await session._execute(
                """INSERT INTO {s}.meetings
                (id,workspace_id,project_id,transcript_id,display_name,save_audio,started_at,updated_at,version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    meeting.id,
                    workspace_id,
                    project_id,
                    transcript_id,
                    display_name,
                    False,
                    meeting.started_at,
                    meeting.updated_at,
                    meeting.version,
                ),
            )
            await self._receipt(session, "create", project_id, digest, intent, meeting)
        return meeting

    async def get(self, *, meeting_id: str, workspace_id: str) -> MeetingView:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            meeting, _ = await self._locked(base, meeting_id, workspace_id)
            return await self._view(base, meeting)

    async def seal(
        self,
        *,
        meeting_id: str,
        workspace_id: str,
        expected_revision: int,
        text: str | Unset,
        key: str | None,
    ) -> MeetingView:
        intent_body: dict[str, object] = {"expected_transcript_revision": expected_revision}
        if not isinstance(text, Unset):
            intent_body["text"] = text
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            meeting, source = await self._locked(base, meeting_id, workspace_id)
            digest, intent, prior = await self._claim(
                base, workspace_id, "seal", meeting_id, key, intent_body
            )
            if prior is not None:
                return MeetingView(prior.snapshot(meeting))
            now = await base.now()
            changed = source.seal(
                expected_revision=expected_revision, revision_id=uuid4().hex, now=now, text=text
            )
            sealed = meeting.seal(changed.head, now=now)
            await base.save_transcript(changed)
            await self._save(base, meeting, sealed)
            await self._receipt(base, "seal", meeting_id, digest, intent, sealed)
        return MeetingView(sealed)

    async def _original_minutes(
        self, base: _Session, receipt: _Receipt, meeting: Meeting
    ) -> MinutesTaskView:
        if receipt.minutes_id is None:
            raise ResourceNotFoundError
        view = (await self._tasks._row(base, receipt.minutes_id)).view()
        snapshot = receipt.snapshot(meeting)
        if not snapshot.matches_minutes(view.task):
            raise ResourceNotFoundError
        # This task was created as queued in the same transaction as this receipt.
        initial = replace(
            view.task,
            status="queued",
            version=1,
            attempt=0,
            attempt_token=None,
            updated_at=view.task.created_at,
            text=None,
            error=None,
        )
        return MinutesTaskView(initial, view.profile)

    async def enqueue_minutes(
        self,
        *,
        meeting_id: str,
        workspace_id: str,
        template_revision: int,
        llm_model: str | None,
        profile: LlmProfile | None,
        binding_id: str | None,
        key: str | None,
        expected_source_revision: int | None = None,
    ) -> MinutesTaskView:
        # Preserve pre-CAS receipt hashes for callers that omit the new optional field.
        command: dict[str, object] = {
            "template_revision": template_revision,
            "llm_model": llm_model,
        }
        if expected_source_revision is not None:
            command["expected_source_revision"] = expected_source_revision
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            meeting, _ = await self._locked(base, meeting_id, workspace_id)
            digest, intent, prior = await self._claim(
                base,
                workspace_id,
                "minutes",
                meeting_id,
                key,
                command,
            )
            if prior is not None:
                return await self._original_minutes(base, prior, meeting)
            if profile is None or binding_id is None:
                raise LlmError("disabled")
            if template_revision != profile.template_revision or (
                llm_model is not None and llm_model != profile.model
            ):
                raise LlmError("profile_mismatch")
            if meeting.sealed_revision is None:
                raise LlmError("source_not_sealed")
            if (
                expected_source_revision is not None
                and expected_source_revision != meeting.sealed_revision
            ):
                raise LlmError("source_changed")
            current = await self._view(base, meeting)
            if current.minutes is not None and current.minutes.task.status in {"queued", "running"}:
                raise MinutesTaskConflictError
            view = await self._tasks.enqueue_in_session(
                base,
                workspace_id=workspace_id,
                transcript_id=meeting.transcript_id,
                source_revision=meeting.sealed_revision,
                profile=profile,
                binding_id=binding_id,
                key=_digest([meeting_id, digest]),
            )
            linked = meeting.attach_minutes(view.task, now=await base.now())
            await self._save(base, meeting, linked)
            await self._receipt(base, "minutes", meeting_id, digest, intent, linked)
        return view

    async def get_minutes(
        self, *, meeting_id: str, workspace_id: str, minutes_id: str
    ) -> MinutesTaskView:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            meeting, _ = await self._locked(base, meeting_id, workspace_id)
            receipt = await base._one(
                _Receipt,
                """SELECT intent_digest,meeting_id,version,updated_at,ended_at,
                sealed_revision,sealed_revision_id,source_sha256,minutes_id
                FROM {s}.meeting_receipts
                WHERE meeting_id=%s AND minutes_id=%s AND operation='minutes' LIMIT 1""",
                (meeting_id, minutes_id),
            )
            if receipt is None:
                raise ResourceNotFoundError
            await self._original_minutes(base, receipt, meeting)
            return (await self._tasks._visible(base, minutes_id))[0]
