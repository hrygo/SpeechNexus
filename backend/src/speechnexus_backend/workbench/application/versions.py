"""Project and manual-transcript use cases; no public route or database is implied."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import cast
from uuid import uuid4

from speechnexus_backend.workbench.application.listings import (
    ListingPage,
    ListingPosition,
    listing_page,
    validate_listing,
)
from speechnexus_backend.workbench.application.transcript_sources import (
    TranscriptSourceError,
    TranscriptSourceReader,
)
from speechnexus_backend.workbench.application.version_ports import (
    ErasedMutationReceipt,
    MutationReceipt,
    MutationScope,
    VersionSession,
    VersionSnapshot,
    VersionUnitOfWork,
)
from speechnexus_backend.workbench.domain.listings import TranscriptSummary
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    Project,
    ProjectChanges,
    SpeechSource,
    Transcript,
    TranscriptRevision,
    Unset,
    validate_revision_number,
)


class ResourceNotFoundError(Exception):
    """A referenced workbench resource or revision does not exist."""

    def __init__(self) -> None:
        super().__init__("workbench resource not found")


class IdempotencyConflictError(Exception):
    """A scoped key was previously committed for a different intent."""

    def __init__(self) -> None:
        super().__init__("workbench idempotency conflict")


def _intent_digest(intent: dict[str, object]) -> str:
    # Internal intents contain named strings/integers/null, no vendor DTO or floats.
    return hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


class WorkbenchVersions:
    """Operate on module-owned state through an injected transaction adapter.

    HTTP DTO validation/mapping and speech Artifact verification are separate
    adapters. A source reader is optional until production composition enables imports.
    """

    def __init__(
        self,
        unit_of_work: VersionUnitOfWork,
        *,
        source_reader: TranscriptSourceReader | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._source_reader = source_reader

    async def list_projects(
        self, *, workspace_id: str, limit: int = 50, after: ListingPosition | None = None
    ) -> ListingPage[Project]:
        validate_listing("projects", workspace_id, limit, after)
        async with self._unit_of_work.transaction() as session:
            rows = await session.list_projects(workspace_id, limit=limit + 1, after=after)
        return listing_page(rows, kind="projects", parent_id=workspace_id, limit=limit)

    async def list_transcripts(
        self, *, project_id: str, limit: int = 50, after: ListingPosition | None = None
    ) -> ListingPage[TranscriptSummary]:
        validate_listing("transcripts", project_id, limit, after)
        async with self._unit_of_work.transaction() as session:
            rows = await session.list_transcripts(project_id, limit=limit + 1, after=after)
            if rows is None:
                raise ResourceNotFoundError
        return listing_page(rows, kind="transcripts", parent_id=project_id, limit=limit)

    @staticmethod
    async def _replay(
        session: VersionSession, scope: MutationScope, key: str | None, digest: str
    ) -> VersionSnapshot | None:
        if key is None:
            return None
        receipt = await session.claim_receipt(scope, key)
        if receipt is None:
            return None
        if receipt.intent_digest != digest:
            raise IdempotencyConflictError
        if isinstance(receipt, ErasedMutationReceipt):
            raise ResourceNotFoundError
        return receipt.snapshot

    async def _mutate[T: VersionSnapshot](
        self,
        *,
        scope: MutationScope,
        key: str | None,
        intent: dict[str, object],
        action: Callable[[VersionSession], Awaitable[T]],
    ) -> T:
        digest = _intent_digest(intent)
        async with self._unit_of_work.transaction() as session:
            replay = await self._replay(session, scope, key, digest)
            if replay is not None:
                # Operation-scoped receipts keep the result type fixed across retries.
                return cast(T, replay)
            result = await action(session)
            if key is not None:
                await session.save_receipt(scope, key, MutationReceipt(digest, result))
        # Do not report success until the context manager has committed.
        return result

    async def create_project(
        self,
        *,
        workspace_id: str,
        name: str,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> Project:
        async def create(session: VersionSession) -> Project:
            project = Project.create(
                project_id=uuid4().hex,
                workspace_id=workspace_id,
                name=name,
                description=description,
                now=await session.now(),
            )
            await session.save_project(project)
            return project

        return await self._mutate(
            scope=("project.create", workspace_id),
            key=idempotency_key,
            intent={"name": name, "description": description},
            action=create,
        )

    async def edit_project(
        self,
        *,
        project_id: str,
        expected_revision: int,
        changes: ProjectChanges,
        idempotency_key: str | None = None,
    ) -> Project:
        intent: dict[str, object] = {"expected_revision": expected_revision}
        for name in ("name", "description", "status"):
            value = getattr(changes, name)
            if not isinstance(value, Unset):
                intent[name] = value

        async def edit(session: VersionSession) -> Project:
            project = await session.get_project(project_id, for_update=True)
            if project is None:
                raise ResourceNotFoundError
            changed = project.edit(
                expected_revision=expected_revision,
                changes=changes,
                now=await session.now(),
            )
            await session.save_project(changed)
            return changed

        return await self._mutate(
            scope=("project.edit", project_id),
            key=idempotency_key,
            intent=intent,
            action=edit,
        )

    async def create_manual_transcript(
        self,
        *,
        project_id: str,
        display_name: str,
        text: str,
        idempotency_key: str | None = None,
    ) -> Transcript:
        async def create(session: VersionSession) -> Transcript:
            if await session.get_project(project_id, for_update=True) is None:
                raise ResourceNotFoundError
            transcript = Transcript.create(
                transcript_id=uuid4().hex,
                project_id=project_id,
                display_name=display_name,
                revision_id=uuid4().hex,
                text=text,
                now=await session.now(),
            )
            await session.save_transcript(transcript)
            return transcript

        return await self._mutate(
            scope=("transcript.create", project_id),
            key=idempotency_key,
            intent={"display_name": display_name, "text": text},
            action=create,
        )

    async def import_speech_transcript(
        self,
        *,
        project_id: str,
        display_name: str,
        artifact_id: str,
        receipt_id: str,
        idempotency_key: str | None = None,
    ) -> Transcript:
        # Both manual and speech sources create the same public resource. They share
        # a key scope, while distinct intents ensure incompatible sources conflict.
        scope = ("transcript.create", project_id)
        intent: dict[str, object] = {
            "display_name": display_name,
            "source_artifact_id": artifact_id,
            "source_receipt_id": receipt_id,
        }
        async with self._unit_of_work.transaction() as session:
            replay = await self._replay(session, scope, idempotency_key, _intent_digest(intent))
            if replay is not None:
                return cast(Transcript, replay)
            parent = await session.get_project(project_id)
            if parent is None:
                raise ResourceNotFoundError
        if self._source_reader is None:
            raise TranscriptSourceError("source_unavailable")
        # Source HTTP/decoding never holds a transaction, connection or resource lock.
        text = await self._source_reader.read(
            artifact_id=artifact_id, receipt_id=receipt_id, workspace_id=parent.workspace_id
        )

        async def create(session: VersionSession) -> Transcript:
            # Downloads may overlap deletion, another import or a lost-response retry.
            current = await session.get_project(project_id, for_update=True)
            if current is None or current.workspace_id != parent.workspace_id:
                raise ResourceNotFoundError
            transcript = Transcript.create(
                transcript_id=uuid4().hex,
                project_id=project_id,
                display_name=display_name,
                revision_id=uuid4().hex,
                text=text,
                now=await session.now(),
                source=SpeechSource(artifact_id, receipt_id),
            )
            await session.save_transcript(transcript)
            return transcript

        return await self._mutate(scope=scope, key=idempotency_key, intent=intent, action=create)

    async def revise_transcript(
        self,
        *,
        transcript_id: str,
        expected_revision: int,
        text: str,
        source_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> Transcript:
        async def revise(session: VersionSession) -> Transcript:
            transcript = await session.get_transcript(transcript_id, for_update=True)
            if transcript is None:
                raise ResourceNotFoundError
            source = None
            if source_revision is not None:
                source = await session.get_revision(transcript_id, source_revision)
                if source is None:
                    raise ResourceNotFoundError
            changed = transcript.revise(
                expected_revision=expected_revision,
                revision_id=uuid4().hex,
                text=text,
                now=await session.now(),
                source_revision=source,
            )
            await session.save_transcript(changed)
            return changed

        return await self._mutate(
            scope=("transcript.revise", transcript_id),
            key=idempotency_key,
            intent={
                "expected_revision": expected_revision,
                "text": text,
                "source_revision": source_revision,
            },
            action=revise,
        )

    async def seal_transcript(
        self,
        *,
        transcript_id: str,
        expected_revision: int,
        text: str | Unset = Unset.VALUE,
        idempotency_key: str | None = None,
    ) -> Transcript:
        """Publish a new immutable source; historical key replay precedes CAS checks."""
        validate_revision_number(expected_revision)
        intent: dict[str, object] = {"expected_revision": expected_revision}
        if not isinstance(text, Unset):
            intent["text"] = text

        async def seal(session: VersionSession) -> Transcript:
            transcript = await session.get_transcript(transcript_id, for_update=True)
            if transcript is None:
                raise ResourceNotFoundError
            changed = transcript.seal(
                expected_revision=expected_revision,
                revision_id=uuid4().hex,
                now=await session.now(),
                text=text,
            )
            await session.save_transcript(changed)
            return changed

        return await self._mutate(
            scope=("transcript.seal", transcript_id),
            key=idempotency_key,
            intent=intent,
            action=seal,
        )

    async def get_sealed_revision(self, transcript_id: str, revision: int) -> TranscriptRevision:
        """Read exactly one frozen source, never substitute a draft or the current head."""
        validate_revision_number(revision)
        snapshot = await self.get_revision(transcript_id, revision)
        if snapshot.status != "sealed":
            raise InvalidVersionChangeError("requested transcript revision is not sealed")
        return snapshot

    async def get_project(self, project_id: str) -> Project:
        async with self._unit_of_work.transaction() as session:
            project = await session.get_project(project_id)
            if project is None:
                raise ResourceNotFoundError
            return project

    async def get_transcript(self, transcript_id: str) -> Transcript:
        async with self._unit_of_work.transaction() as session:
            transcript = await session.get_transcript(transcript_id)
            if transcript is None:
                raise ResourceNotFoundError
            return transcript

    async def get_transcript_view(
        self, transcript_id: str, revision: int | None = None
    ) -> tuple[Transcript, TranscriptRevision]:
        """Read current metadata and one selected immutable version consistently."""
        if revision is not None:
            validate_revision_number(revision)
        async with self._unit_of_work.transaction() as session:
            result = await session.get_transcript_view(transcript_id, revision)
            if result is None:
                raise ResourceNotFoundError
            return result

    async def get_revision(self, transcript_id: str, revision: int) -> TranscriptRevision:
        async with self._unit_of_work.transaction() as session:
            snapshot = await session.get_revision(transcript_id, revision)
            if snapshot is None:
                raise ResourceNotFoundError
            return snapshot
