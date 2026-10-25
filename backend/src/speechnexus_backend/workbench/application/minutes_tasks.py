"""Durable workbench minutes orchestration; every store call is a short transaction."""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from speechnexus_backend.workbench.application.llm import (
    DisabledTextLlm,
    LlmCompletion,
    LlmError,
    LlmProfile,
    LlmRequest,
    LlmUsage,
    TextLlm,
)
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.minutes_tasks import (
    MinutesTask,
    MinutesTaskConflictError,
    MinutesTaskFailure,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    validate_revision_number,
)


def profile_digest(profile: LlmProfile) -> str:
    return (
        "sha256:"
        + sha256(
            json.dumps(
                asdict(profile), sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )


@dataclass(frozen=True, slots=True)
class MinutesTaskView:
    task: MinutesTask
    profile: LlmProfile


@dataclass(frozen=True, slots=True)
class MinutesWork:
    view: MinutesTaskView
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MinutesAttempt:
    number: int
    token: str = field(repr=False)
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    usage: LlmUsage | None
    completion_confirmed: bool


class MinutesTaskStore(Protocol):
    async def enqueue(
        self,
        *,
        workspace_id: str,
        transcript_id: str,
        source_revision: int,
        profile: LlmProfile,
        binding_id: str,
        key: str,
    ) -> MinutesTaskView: ...
    async def get(self, task_id: str) -> MinutesTaskView: ...
    async def queued_ids(self, workspace_id: str, *, limit: int) -> tuple[str, ...]: ...
    async def claim(self, task_id: str, *, profile: LlmProfile, binding_id: str) -> MinutesWork: ...
    async def finish(
        self, work: MinutesWork, *, result: LlmCompletion | None, error: MinutesTaskFailure | None
    ) -> MinutesTaskView: ...
    async def cancel(self, task_id: str) -> MinutesTaskView: ...
    async def retry(
        self, task_id: str, *, expected_attempt: int, key: str, profile: LlmProfile, binding_id: str
    ) -> MinutesTaskView: ...


class MinutesTasks:
    def __init__(
        self, store: MinutesTaskStore, llm: TextLlm | None = None, *, poll_seconds: float = 0.2
    ) -> None:
        if (
            type(poll_seconds) not in {float, int}
            or not math.isfinite(poll_seconds)
            or not 0.01 <= poll_seconds <= 2
        ):
            raise ValueError("invalid minutes cancellation polling interval")
        self._store = store
        self._llm: TextLlm = llm if llm is not None else DisabledTextLlm()
        self._poll_seconds = poll_seconds

    def _binding(self) -> tuple[LlmProfile, str]:
        profile, binding = self._llm.profile, self._llm.binding_id
        if profile is None or binding is None:
            raise LlmError("disabled")
        return profile, binding

    async def enqueue(
        self,
        *,
        workspace_id: str,
        transcript_id: str,
        source_revision: int,
        template_revision: int,
        key: str,
        llm_model: str | None = None,
    ) -> MinutesTaskView:
        profile, binding = self._binding()
        try:
            validate_revision_number(source_revision)
            validate_revision_number(template_revision)
        except InvalidVersionChangeError:
            raise LlmError("invalid_input") from None
        if template_revision != profile.template_revision or (
            llm_model is not None and llm_model != profile.model
        ):
            raise LlmError("profile_mismatch")
        return await self._store.enqueue(
            workspace_id=workspace_id,
            transcript_id=transcript_id,
            source_revision=source_revision,
            profile=profile,
            binding_id=binding,
            key=key,
        )

    async def get(self, task_id: str) -> MinutesTaskView:
        return await self._store.get(task_id)

    async def queued_ids(self, workspace_id: str, *, limit: int = 100) -> tuple[str, ...]:
        return await self._store.queued_ids(workspace_id, limit=limit)

    async def cancel(self, task_id: str) -> MinutesTaskView:
        return await self._store.cancel(task_id)

    async def retry(self, task_id: str, *, expected_attempt: int, key: str) -> MinutesTaskView:
        profile, binding = self._binding()
        return await self._store.retry(
            task_id, expected_attempt=expected_attempt, key=key, profile=profile, binding_id=binding
        )

    async def execute(self, task_id: str) -> MinutesTaskView:
        profile, binding = self._binding()
        current = await self._store.get(task_id)
        if current.task.status == "running":
            # Unknown/live running work is never automatically dispatched again.
            raise MinutesTaskConflictError
        if current.task.status != "queued":
            return current
        work = await self._store.claim(task_id, profile=profile, binding_id=binding)
        request = LlmRequest(work.view.profile, work.text)
        call = asyncio.create_task(self._llm.generate(request))
        result: LlmCompletion | None = None
        failure: MinutesTaskFailure | None = None
        try:
            while True:
                done, _ = await asyncio.wait({call}, timeout=self._poll_seconds)
                if done:
                    try:
                        result = call.result()
                    except LlmError as error:
                        failure = (
                            "invalid_response"
                            if error.code == "invalid_response"
                            else "unavailable"
                        )
                    except Exception:
                        failure = "unavailable"
                    break
                visible: MinutesTaskView | None
                try:
                    visible = await self._store.get(task_id)
                except ResourceNotFoundError:
                    # A source tombstone hides public reads. The original attempt
                    # still needs its final response/usage or interruption fact.
                    visible = None
                if (
                    visible is None
                    or visible.task.status != "running"
                    or visible.task.attempt_token != work.view.task.attempt_token
                ):
                    call.cancel()
                    try:
                        result = await call
                    except asyncio.CancelledError:
                        failure = "interrupted"
                    except Exception:
                        failure = "unavailable"
                    if visible is None or visible.task.status in {"cancelled", "expired"}:
                        break
                    return visible
        except asyncio.CancelledError:
            call.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await call
            # Best effort preserves a surviving worker's interruption fact. If
            # persistence is unavailable, running remains unknown, never requeued.
            with suppress(Exception):
                await self._store.finish(work, result=None, error="interrupted")
            raise
        finally:
            if not call.done():
                call.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await call
        # Persistence failures are not model failures. An uncertain COMMIT must
        # be queried by original task identity, never rewritten with another outcome.
        return await self._store.finish(work, result=result, error=failure)
