"""Test-only serialized transactions; never installed as a production adapter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from speechnexus_backend.workbench.application.listings import ListingPosition
from speechnexus_backend.workbench.application.version_ports import (
    MutationReceipt,
    MutationScope,
    VersionSession,
)
from speechnexus_backend.workbench.domain.listings import TranscriptSummary
from speechnexus_backend.workbench.domain.versions import Project, Transcript, TranscriptRevision


class _Session:
    def __init__(self, parent: MemoryVersionUnitOfWork) -> None:
        self._now = parent.current_time
        self.projects = parent.projects.copy()
        self.transcripts = parent.transcripts.copy()
        self.revisions = parent.revisions.copy()
        self.receipts = parent.receipts.copy()
        self._locked_projects: set[str] = set()
        self._locked_transcripts: set[str] = set()

    async def now(self) -> datetime:
        return self._now

    async def list_projects(
        self, workspace_id: str, *, limit: int, after: ListingPosition | None
    ) -> tuple[Project, ...]:
        rows = [
            p
            for p in self.projects.values()
            if p.workspace_id == workspace_id
            and (after is None or (p.created_at, p.id) < (after.created_at, after.item_id))
        ]
        return tuple(sorted(rows, key=lambda p: (p.created_at, p.id), reverse=True)[:limit])

    async def list_transcripts(
        self, project_id: str, *, limit: int, after: ListingPosition | None
    ) -> tuple[TranscriptSummary, ...] | None:
        if project_id not in self.projects:
            return None
        rows = [
            t
            for t in self.transcripts.values()
            if t.project_id == project_id
            and (after is None or (t.created_at, t.id) < (after.created_at, after.item_id))
        ]
        return tuple(
            TranscriptSummary.from_transcript(t)
            for t in sorted(rows, key=lambda t: (t.created_at, t.id), reverse=True)[:limit]
        )

    async def get_project(self, project_id: str, *, for_update: bool = False) -> Project | None:
        if for_update and project_id in self.projects:
            self._locked_projects.add(project_id)
        return self.projects.get(project_id)

    async def save_project(self, project: Project) -> None:
        if project.id in self.projects and project.id not in self._locked_projects:
            raise AssertionError("project write requires a locked read")
        self.projects[project.id] = project

    async def get_transcript(
        self,
        transcript_id: str,
        *,
        for_update: bool = False,
    ) -> Transcript | None:
        if for_update and transcript_id in self.transcripts:
            self._locked_transcripts.add(transcript_id)
        return self.transcripts.get(transcript_id)

    async def save_transcript(self, transcript: Transcript) -> None:
        if transcript.id in self.transcripts:
            if transcript.id not in self._locked_transcripts:
                raise AssertionError("transcript write requires a locked read")
        elif transcript.project_id not in self._locked_projects:
            raise AssertionError("transcript creation requires a parent locked read")
        key = (transcript.id, transcript.head.number)
        if key in self.revisions:
            raise AssertionError("immutable revision already exists")
        self.revisions[key] = transcript.head
        self.transcripts[transcript.id] = transcript

    async def get_revision(self, transcript_id: str, revision: int) -> TranscriptRevision | None:
        return self.revisions.get((transcript_id, revision))

    async def get_transcript_view(
        self, transcript_id: str, revision: int | None
    ) -> tuple[Transcript, TranscriptRevision] | None:
        transcript = self.transcripts.get(transcript_id)
        if transcript is None or transcript.project_id not in self.projects:
            return None
        selected = (
            transcript.head if revision is None else self.revisions.get((transcript_id, revision))
        )
        return None if selected is None else (transcript, selected)

    async def claim_receipt(self, scope: MutationScope, key: str) -> MutationReceipt | None:
        # The parent transaction lock covers absent keys as well as existing rows.
        return self.receipts.get((scope, key))

    async def save_receipt(self, scope: MutationScope, key: str, receipt: MutationReceipt) -> None:
        self.receipts[(scope, key)] = receipt


class MemoryVersionUnitOfWork:
    """Copy-on-commit fake; cancellation/exception leaves the parent state intact."""

    def __init__(self, *, now: datetime) -> None:
        self.current_time = now
        self.projects: dict[str, Project] = {}
        self.transcripts: dict[str, Transcript] = {}
        self.revisions: dict[tuple[str, int], TranscriptRevision] = {}
        self.receipts: dict[tuple[MutationScope, str], MutationReceipt] = {}
        self.fail_commit = False
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[VersionSession]:
        async with self._lock:
            session = _Session(self)
            # Make competing tasks actually contend for the transaction lock.
            await asyncio.sleep(0)
            yield session
            if self.fail_commit:
                raise RuntimeError("synthetic commit failure")
            self.projects = session.projects
            self.transcripts = session.transcripts
            self.revisions = session.revisions
            self.receipts = session.receipts
