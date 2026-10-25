"""Speech imports copy verified source text, never network inside a transaction."""

import asyncio
from datetime import UTC, datetime

import pytest

from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import Transcript
from tests.fake.workbench_versions import MemoryVersionUnitOfWork


class Reader:
    def __init__(self, memory: MemoryVersionUnitOfWork) -> None:
        self.memory = memory
        self.calls = 0
        self.fail = False
        self.remove_parent = False
        self.fail_commit = False

    async def read(self, *, artifact_id: str, receipt_id: str, workspace_id: str) -> str:
        assert not self.memory._lock.locked(), "network entered a transaction"
        assert (artifact_id, receipt_id, workspace_id) == (
            "artifact-source",
            "receipt-source",
            "workspace-one",
        )
        self.calls += 1
        await asyncio.sleep(0)
        if self.fail:
            raise TranscriptSourceError("source_unavailable")
        if self.remove_parent:
            self.memory.projects.clear()
        if self.fail_commit:
            self.memory.fail_commit = True
        return "  合成原文\n"


def setup() -> tuple[MemoryVersionUnitOfWork, Reader, WorkbenchVersions]:
    memory = MemoryVersionUnitOfWork(now=datetime(2026, 10, 24, tzinfo=UTC))
    reader = Reader(memory)
    return memory, reader, WorkbenchVersions(memory, source_reader=reader)


async def import_one(
    app: WorkbenchVersions, project_id: str, *, key: str = "import-one"
) -> Transcript:
    return await app.import_speech_transcript(
        project_id=project_id,
        display_name="合成原稿",
        artifact_id="artifact-source",
        receipt_id="receipt-source",
        idempotency_key=key,
    )


async def test_import_seals_original_and_replay_keeps_original_without_refetch() -> None:
    memory, reader, app = setup()
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    first = await import_one(app, project.id)
    assert first.head.status == "sealed" and first.head.number == 1
    assert first.head.text == "  合成原文\n" and first.head.source_revision is None
    assert first.head.source is not None
    assert (first.head.source.artifact_id, first.head.source.receipt_id) == (
        "artifact-source",
        "receipt-source",
    )
    corrected = await app.revise_transcript(
        transcript_id=first.id, expected_revision=1, text="校对正文"
    )
    assert corrected.head.status == "draft" and corrected.head.source_revision == 1
    reader.fail = True
    assert await import_one(app, project.id) == first
    assert reader.calls == 1
    assert len(memory.transcripts) == 1 and len(memory.revisions) == 2


async def test_import_and_manual_source_share_one_create_idempotency_scope() -> None:
    memory, reader, app = setup()
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    await import_one(app, project.id)
    for artifact, receipt, title in (
        ("artifact-other", "receipt-source", "合成原稿"),
        ("artifact-source", "receipt-other", "合成原稿"),
        ("artifact-source", "receipt-source", "不同标题"),
    ):
        with pytest.raises(IdempotencyConflictError):
            await app.import_speech_transcript(
                project_id=project.id,
                display_name=title,
                artifact_id=artifact,
                receipt_id=receipt,
                idempotency_key="import-one",
            )
    with pytest.raises(IdempotencyConflictError):
        await app.create_manual_transcript(
            project_id=project.id,
            display_name="合成原稿",
            text="其他文本",
            idempotency_key="import-one",
        )
    assert reader.calls == 1 and len(memory.transcripts) == 1


async def test_concurrent_import_fetches_outside_transaction_but_publishes_one_identity() -> None:
    memory, reader, app = setup()
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    results = await asyncio.gather(*(import_one(app, project.id) for _ in range(8)))
    assert len({r.id for r in results}) == 1
    assert 1 <= reader.calls <= 8
    assert len(memory.transcripts) == len(memory.revisions) == len(memory.receipts) == 1


async def test_source_failure_and_absent_parent_do_not_claim_create_key() -> None:
    memory, reader, app = setup()
    with pytest.raises(ResourceNotFoundError):
        await import_one(app, "missing")
    assert reader.calls == 0
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    reader.fail = True
    with pytest.raises(TranscriptSourceError):
        await import_one(app, project.id)
    assert not memory.transcripts and not memory.receipts
    reader.fail = False
    assert (await import_one(app, project.id)).head.status == "sealed"


async def test_parent_disappearing_during_download_cannot_be_resurrected() -> None:
    memory, reader, app = setup()
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    reader.remove_parent = True
    with pytest.raises(ResourceNotFoundError):
        await import_one(app, project.id)
    assert not memory.transcripts and not memory.receipts


async def test_failed_final_commit_can_retry_same_key_without_partial_content() -> None:
    memory, reader, app = setup()
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    reader.fail_commit = True
    with pytest.raises(RuntimeError, match="commit failure"):
        await import_one(app, project.id)
    assert not memory.transcripts and not memory.receipts
    memory.fail_commit = reader.fail_commit = False
    await import_one(app, project.id)
    assert len(memory.transcripts) == 1


async def test_missing_reader_has_stable_failure_and_no_content() -> None:
    memory, _, _ = setup()
    app = WorkbenchVersions(memory)
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    with pytest.raises(TranscriptSourceError, match=r"^source_unavailable$"):
        await import_one(app, project.id)
    assert not memory.transcripts and not memory.receipts
