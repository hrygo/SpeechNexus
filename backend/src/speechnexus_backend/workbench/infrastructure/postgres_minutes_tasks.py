"""Owned PG task state; reuse the existing bounded transaction/connection lifecycle."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from speechnexus_backend.workbench.application.llm import (
    LlmCompletion,
    LlmError,
    LlmProfile,
    LlmRequest,
    LlmUsage,
    valid_text,
)
from speechnexus_backend.workbench.application.minutes_tasks import (
    MinutesAttempt,
    MinutesTaskView,
    MinutesWork,
    profile_digest,
)
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.minutes_tasks import (
    MinutesTask,
    MinutesTaskConflictError,
    MinutesTaskFailure,
    MinutesTaskStatus,
)
from speechnexus_backend.workbench.domain.versions import Transcript, validate_revision_number
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)


@dataclass(frozen=True)
class _TaskRow:
    id: str
    workspace_id: str
    project_id: str
    transcript_id: str
    source_revision: Decimal
    source_revision_id: str
    source_sha256: str
    profile_sha256: str
    binding_id: str
    profile: dict[str, Any]
    status: MinutesTaskStatus
    attempt: int
    attempt_token: str | None
    version: Decimal
    created_at: datetime
    updated_at: datetime
    result_text: str | None
    error_code: MinutesTaskFailure | None

    def view(self) -> MinutesTaskView:
        profile = LlmProfile(**self.profile)
        if profile_digest(profile) != self.profile_sha256:
            raise ValueError("invalid persisted minutes profile")
        task = MinutesTask(
            self.id,
            self.workspace_id,
            self.project_id,
            self.transcript_id,
            int(self.source_revision),
            self.source_revision_id,
            self.source_sha256,
            self.profile_sha256,
            self.binding_id,
            self.status,
            self.attempt,
            self.attempt_token,
            int(self.version),
            self.created_at,
            self.updated_at,
            self.result_text,
            self.error_code,
        )
        return MinutesTaskView(task, profile)


@dataclass(frozen=True)
class _Parent:
    id: str
    workspace_id: str


@dataclass(frozen=True)
class _Id:
    id: str


@dataclass(frozen=True)
class _Key:
    task_id: str
    intent_digest: str


@dataclass(frozen=True)
class _Retry:
    expected_attempt: int


@dataclass(frozen=True)
class _AttemptRow:
    task_id: str
    number: int
    token: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    input_tokens: int | None
    output_tokens: int | None
    completion_digest: str | None
    completion_confirmed: bool

    def fact(self) -> MinutesAttempt:
        usage = None
        if self.input_tokens is not None and self.output_tokens is not None:
            usage = LlmUsage(self.input_tokens, self.output_tokens)
        return MinutesAttempt(
            self.number,
            self.token,
            self.started_at,
            self.finished_at,
            self.outcome,
            usage,
            self.completion_confirmed,
        )


def _hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _key(value: str) -> str:
    if not valid_text(value, 128):
        raise ValueError("invalid minutes request key")
    return sha256(value.encode()).hexdigest()


class PostgresMinutesTasks:
    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[_Session]:
        async with self._versions.transaction() as session:
            assert isinstance(session, _Session)
            yield session

    async def _source(self, base: _Session, transcript_id: str, workspace_id: str) -> Transcript:
        probe = await base.get_transcript(transcript_id)
        if probe is None:
            raise ResourceNotFoundError
        parent = await base.get_project(probe.project_id, for_update=True)
        if parent is None or parent.workspace_id != workspace_id:
            raise ResourceNotFoundError
        current = await base.get_transcript(transcript_id, for_update=True)
        if current is None:
            raise ResourceNotFoundError
        return current

    async def _row(self, base: _Session, task_id: str, *, lock: bool = False) -> _TaskRow:
        row = await base._one(
            _TaskRow,
            "SELECT * FROM {s}.minutes_tasks WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (task_id,),
        )
        if row is None:
            raise ResourceNotFoundError
        return row

    async def _locked(
        self, base: _Session, task_id: str, *, include_deleted: bool = False
    ) -> tuple[MinutesTaskView, Transcript | None]:
        probe = await self._row(base, task_id)
        # Raw headers are needed only to record a completed request after deletion
        # acceptance. Public reads still deny tombstones, and no text is published.
        parent = await base._one(
            _Parent,
            "SELECT id,workspace_id FROM {s}.projects WHERE id=%s FOR UPDATE",
            (probe.project_id,),
        )
        if parent is None or parent.workspace_id != probe.workspace_id:
            raise ResourceNotFoundError
        source = await base.get_transcript(probe.transcript_id, for_update=True)
        deleted = source is None or await base._deleted("project", probe.project_id)
        if deleted and not include_deleted:
            raise ResourceNotFoundError
        view = (await self._row(base, task_id, lock=True)).view()
        return view, None if deleted else source

    @staticmethod
    def _matches(task: MinutesTask, source: Transcript | None) -> bool:
        if source is None:
            return False
        head = source.head
        return (
            head.number == task.source_revision
            and head.id == task.source_revision_id
            and head.status == "sealed"
            and "sha256:" + sha256(head.text.encode()).hexdigest() == task.source_sha256
        )

    async def _save(self, base: _Session, previous: MinutesTask, task: MinutesTask) -> None:
        if previous == task:
            return
        row = await base._one(
            _Id,
            """UPDATE {s}.minutes_tasks SET status=%s,attempt=%s,
            attempt_token=%s,version=%s,updated_at=%s,result_text=%s,error_code=%s
            WHERE id=%s AND version=%s RETURNING id""",
            (
                task.status,
                task.attempt,
                task.attempt_token,
                task.version,
                task.updated_at,
                task.text,
                task.error,
                task.id,
                previous.version,
            ),
        )
        if row is None:
            raise MinutesTaskConflictError

    async def _visible(self, base: _Session, task_id: str) -> tuple[MinutesTaskView, Transcript]:
        view, source = await self._locked(base, task_id)
        assert source is not None
        if not self._matches(view.task, source):
            expired = view.task.expire(now=await base.now())
            await self._save(base, view.task, expired)
            view = MinutesTaskView(expired, view.profile)
        return view, source

    async def enqueue(
        self,
        *,
        workspace_id: str,
        transcript_id: str,
        source_revision: int,
        profile: LlmProfile,
        binding_id: str,
        key: str,
    ) -> MinutesTaskView:
        validate_revision_number(source_revision)
        _key(key)
        async with self._transaction() as base:
            return await self.enqueue_in_session(
                base,
                workspace_id=workspace_id,
                transcript_id=transcript_id,
                source_revision=source_revision,
                profile=profile,
                binding_id=binding_id,
                key=key,
            )

    async def enqueue_in_session(
        self,
        base: _Session,
        *,
        workspace_id: str,
        transcript_id: str,
        source_revision: int,
        profile: LlmProfile,
        binding_id: str,
        key: str,
    ) -> MinutesTaskView:
        validate_revision_number(source_revision)
        digest = _key(key)
        intent = _hash([workspace_id, transcript_id, source_revision, asdict(profile), binding_id])
        source = await self._source(base, transcript_id, workspace_id)
        bucket = int.from_bytes(
            sha256((base._schema.as_string() + workspace_id + digest).encode()).digest()[:8],
            signed=True,
        )
        await base._execute("SELECT pg_advisory_xact_lock(%s)", (bucket,))
        prior = await base._one(
            _Key,
            "SELECT task_id,intent_digest FROM {s}.minutes_request_keys "
            "WHERE workspace_id=%s AND key_digest=%s",
            (workspace_id, digest),
        )
        if prior is not None:
            if prior.intent_digest != intent:
                raise IdempotencyConflictError
            return (await self._visible(base, prior.task_id))[0]
        if source.head.number != source_revision:
            raise LlmError("source_changed")
        if source.head.status != "sealed":
            raise LlmError("source_not_sealed")
        LlmRequest(profile, source.head.text)
        task = MinutesTask.create(
            task_id=uuid4().hex,
            workspace_id=workspace_id,
            project_id=source.project_id,
            transcript_id=source.id,
            source_revision=source_revision,
            source_revision_id=source.head.id,
            source_sha256="sha256:" + sha256(source.head.text.encode()).hexdigest(),
            profile_sha256=profile_digest(profile),
            binding_id=binding_id,
            now=await base.now(),
        )
        # Execution identity and metering survive content erasure. Keep only
        # immutable dependencies/parameters here, never source or prompt text.
        await base._execute(
            """INSERT INTO {s}.minutes_intents
            (task_id,workspace_id,project_id,transcript_id,source_revision,source_revision_id,
             source_sha256,profile_sha256,binding_id,model,template_revision,
             max_input_characters,max_output_characters,max_output_tokens,created_at,intent_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                task.id,
                task.workspace_id,
                task.project_id,
                task.transcript_id,
                task.source_revision,
                task.source_revision_id,
                task.source_sha256,
                task.profile_sha256,
                task.binding_id,
                profile.model,
                profile.template_revision,
                profile.max_input_characters,
                profile.max_output_characters,
                profile.max_output_tokens,
                task.created_at,
                intent,
            ),
        )
        await base._execute(
            """INSERT INTO {s}.minutes_tasks
            (id,workspace_id,project_id,transcript_id,source_revision,source_revision_id,
             source_sha256,profile_sha256,binding_id,profile,status,attempt,attempt_token,
             version,created_at,updated_at,result_text,error_code)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL)""",
            (
                task.id,
                task.workspace_id,
                task.project_id,
                task.transcript_id,
                task.source_revision,
                task.source_revision_id,
                task.source_sha256,
                task.profile_sha256,
                task.binding_id,
                Jsonb(asdict(profile)),
                task.status,
                task.attempt,
                task.attempt_token,
                task.version,
                task.created_at,
                task.updated_at,
            ),
        )
        await base._execute(
            "INSERT INTO {s}.minutes_request_keys VALUES (%s,%s,%s,%s)",
            (workspace_id, digest, intent, task.id),
        )
        return MinutesTaskView(task, profile)

    async def get(self, task_id: str) -> MinutesTaskView:
        async with self._transaction() as base:
            return (await self._visible(base, task_id))[0]

    async def queued_ids(self, workspace_id: str, *, limit: int) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid minutes queue limit")
        async with self._transaction() as base:
            rows = await base._many(
                _Id,
                """SELECT j.id FROM {s}.minutes_tasks j
                WHERE j.workspace_id=%s AND j.status='queued' AND NOT EXISTS (
                    SELECT 1 FROM {s}.resource_tombstones d
                    WHERE (d.resource_kind='project' AND d.resource_id=j.project_id)
                       OR (d.resource_kind='transcript' AND d.resource_id=j.transcript_id)
                    UNION ALL
                    SELECT 1 FROM {s}.deletion_ledger l, jsonb_array_elements(l.deleted) e
                    WHERE (e->>1='project' AND e->>2=j.project_id)
                       OR (e->>1='transcript' AND e->>2=j.transcript_id))
                ORDER BY j.created_at,j.id LIMIT %s""",
                (workspace_id, limit),
            )
            return tuple(row.id for row in rows)

    @staticmethod
    def _binding(view: MinutesTaskView, profile: LlmProfile, binding_id: str) -> None:
        if view.profile != profile or view.task.binding_id != binding_id:
            raise LlmError("profile_mismatch")

    async def claim(self, task_id: str, *, profile: LlmProfile, binding_id: str) -> MinutesWork:
        async with self._transaction() as base:
            view, source = await self._visible(base, task_id)
            self._binding(view, profile, binding_id)
            request = LlmRequest(profile, source.head.text)
            running = view.task.claim(token=uuid4().hex, now=await base.now())
            await self._save(base, view.task, running)
            await base._execute(
                """INSERT INTO {s}.minutes_attempts
                (task_id,number,token,started_at,outcome) VALUES (%s,%s,%s,%s,'running')""",
                (task_id, running.attempt, running.attempt_token, running.updated_at),
            )
            return MinutesWork(MinutesTaskView(running, profile), request.text)

    async def cancel(self, task_id: str) -> MinutesTaskView:
        async with self._transaction() as base:
            view, _ = await self._visible(base, task_id)
            cancelled = view.task.cancel(now=await base.now())
            await self._save(base, view.task, cancelled)
            return MinutesTaskView(cancelled, view.profile)

    async def retry(
        self, task_id: str, *, expected_attempt: int, key: str, profile: LlmProfile, binding_id: str
    ) -> MinutesTaskView:
        digest = _key(key)
        if type(expected_attempt) is not int or expected_attempt < 0:
            raise MinutesTaskConflictError
        async with self._transaction() as base:
            view, _ = await self._visible(base, task_id)
            prior = await base._one(
                _Retry,
                "SELECT expected_attempt FROM {s}.minutes_retry_keys "
                "WHERE task_id=%s AND key_digest=%s",
                (task_id, digest),
            )
            if prior is not None:
                if prior.expected_attempt != expected_attempt:
                    raise IdempotencyConflictError
                return view
            self._binding(view, profile, binding_id)
            queued = view.task.retry(expected_attempt=expected_attempt, now=await base.now())
            await self._save(base, view.task, queued)
            await base._execute(
                "INSERT INTO {s}.minutes_retry_keys VALUES (%s,%s,%s)",
                (task_id, digest, expected_attempt),
            )
            return MinutesTaskView(queued, view.profile)

    async def finish(
        self, work: MinutesWork, *, result: LlmCompletion | None, error: MinutesTaskFailure | None
    ) -> MinutesTaskView:
        if (result is None) == (error is None):
            raise ValueError("minutes completion needs exactly one outcome")
        if result is not None and (
            result.profile != work.view.profile
            or (
                result.usage is not None
                and result.usage.output_tokens > work.view.profile.max_output_tokens
            )
        ):
            result, error = None, "invalid_response"
        usage = None if result is None else result.usage
        digest = "sha256:" + _hash(
            [
                None if result is None else profile_digest(result.profile),
                None if result is None else sha256(result.text.encode()).hexdigest(),
                None if usage is None else [usage.input_tokens, usage.output_tokens],
                error,
            ]
        )
        async with self._transaction() as base:
            view, source = await self._locked(base, work.view.task.id, include_deleted=True)
            row = await base._one(
                _AttemptRow,
                "SELECT * FROM {s}.minutes_attempts WHERE task_id=%s AND number=%s AND token=%s",
                (view.task.id, work.view.task.attempt, work.view.task.attempt_token),
            )
            if row is None:
                raise MinutesTaskConflictError
            if row.completion_digest is not None:
                if row.completion_digest != digest:
                    raise IdempotencyConflictError
                current = (
                    view.task
                    if self._matches(view.task, source)
                    else view.task.expire(now=await base.now())
                )
                await self._save(base, view.task, current)
                return MinutesTaskView(current, view.profile)
            if view.task.attempt_token != work.view.task.attempt_token:
                raise MinutesTaskConflictError
            now = await base.now()
            previous = view.task
            current = previous if self._matches(previous, source) else previous.expire(now=now)
            finished = current.finish(
                token=row.token, text=None if result is None else result.text, error=error, now=now
            )
            await self._save(base, previous, finished)
            outcome = (
                "succeeded"
                if result is not None
                else "interrupted"
                if error == "interrupted"
                else "failed"
            )
            await base._execute(
                """UPDATE {s}.minutes_attempts SET outcome=%s,finished_at=%s,
                input_tokens=%s,output_tokens=%s,completion_digest=%s,completion_confirmed=%s
                WHERE task_id=%s AND number=%s AND token=%s""",
                (
                    outcome,
                    now,
                    None if usage is None else usage.input_tokens,
                    None if usage is None else usage.output_tokens,
                    digest,
                    result is not None,
                    view.task.id,
                    row.number,
                    row.token,
                ),
            )
            return MinutesTaskView(finished, view.profile)

    async def attempts(self, task_id: str) -> tuple[MinutesAttempt, ...]:
        async with self._transaction() as base:
            await self._visible(base, task_id)
            rows = await base._many(
                _AttemptRow,
                "SELECT * FROM {s}.minutes_attempts WHERE task_id=%s ORDER BY number",
                (task_id,),
            )
            return tuple(row.fact() for row in rows)
