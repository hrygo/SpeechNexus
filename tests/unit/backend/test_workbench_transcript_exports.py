"""Fixed workbench text downloads; synthetic content only, no public DTO substitute."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from speechnexus_backend.workbench.application.transcript_exports import (
    TranscriptExportError,
    TranscriptTextExports,
)
from speechnexus_backend.workbench.application.versions import (
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import (
    SpeechSource,
    Transcript,
    TranscriptRevision,
)
from tests.fake.workbench_versions import MemoryVersionUnitOfWork

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def revision(text: str = "合成正文") -> TranscriptRevision:
    return Transcript.create(
        transcript_id="transcript-one",
        project_id="project-one",
        display_name="不可用于文件名\r\n",
        revision_id="revision-one",
        text=text,
        now=NOW,
        source=SpeechSource("artifact-one", "receipt-one"),
    ).head


class Reader:
    def __init__(self, snapshot: TranscriptRevision) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, int]] = []
        self.error: BaseException | None = None

    async def get_revision(self, transcript_id: str, revision: int) -> TranscriptRevision:
        self.calls.append((transcript_id, revision))
        if self.error is not None:
            raise self.error
        return self.snapshot


@pytest.mark.parametrize(
    "text", ["", "\ufeff合成\r\n第二行\n", "  😀e\u0301 文  ", "😀" * 1_000_000]
)
async def test_exact_utf8_including_empty_corrected_text_and_maximum_unicode(text: str) -> None:
    reader = Reader(replace(revision(), text=text, status="draft"))
    exported = await TranscriptTextExports(reader).export_revision("transcript-one", 1)
    assert reader.calls == [("transcript-one", 1)]
    assert exported.content == text.encode("utf-8")
    assert exported.sha256 == "sha256:" + hashlib.sha256(exported.content).hexdigest()
    assert exported.content_length == len(exported.content) <= 4_000_000
    assert exported.filename == "speech-transcript.txt"
    assert exported.media_type == "text/plain; charset=utf-8"
    assert exported.status == "draft" and exported.source == reader.snapshot.source
    assert exported.source_revision is None
    assert "合成" not in repr(exported) and "😀" not in repr(exported)
    with pytest.raises(FrozenInstanceError):
        exported.revision = 99  # type: ignore[misc]


async def test_fixed_old_and_corrected_revisions_do_not_follow_later_head_or_write_state() -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app = WorkbenchVersions(store)
    parent = await app.create_project(workspace_id="workspace-one", name="合成项目")
    original = await app.create_manual_transcript(
        project_id=parent.id, display_name="合成", text="原文"
    )
    await app.revise_transcript(
        transcript_id=original.id, expected_revision=1, text="校对", idempotency_key="edit"
    )
    before = (store.transcripts.copy(), store.revisions.copy(), store.receipts.copy())
    service = TranscriptTextExports(app)
    old = await service.export_revision(original.id, 1)
    corrected = await service.export_revision(original.id, 2)
    assert (old.content, corrected.content, corrected.source_revision) == (
        "原文".encode(),
        "校对".encode(),
        1,
    )
    assert before == (store.transcripts, store.revisions, store.receipts)
    await app.revise_transcript(transcript_id=original.id, expected_revision=2, text="后续")
    assert await service.export_revision(original.id, 1) == old
    assert await service.export_revision(original.id, 2) == corrected


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", 10**19, None])
async def test_invalid_revision_is_rejected_before_reader(value: Any) -> None:
    reader = Reader(revision())
    with pytest.raises(TranscriptExportError) as caught:
        await TranscriptTextExports(reader).export_revision("transcript-one", value)
    assert caught.value.code == "invalid_revision" and reader.calls == []


@pytest.mark.parametrize("change", [{"transcript_id": "other"}, {"number": 2}])
async def test_reader_cannot_substitute_a_different_transcript_or_current_revision(
    change: dict[str, Any],
) -> None:
    reader = Reader(replace(revision(), **change))
    with pytest.raises(TranscriptExportError) as caught:
        await TranscriptTextExports(reader).export_revision("transcript-one", 1)
    assert caught.value.code == "revision_mismatch"
    assert "合成正文" not in str(caught.value)


@pytest.mark.parametrize(
    "text", ["合成\x00正文", "合成\ud800正文", "合成\udfff正文", "文" * 1_000_001]
)
async def test_corrupt_content_is_rejected_without_normalizing_or_exposing_it(text: str) -> None:
    reader = Reader(replace(revision(), text=text))
    with pytest.raises(TranscriptExportError) as caught:
        await TranscriptTextExports(reader).export_revision("transcript-one", 1)
    assert caught.value.code == "invalid_content"
    assert "合成" not in str(caught.value) and "合成" not in repr(caught.value)
    assert reader.snapshot.text == text


async def test_unknown_revision_is_not_an_empty_success() -> None:
    reader = Reader(revision())
    reader.error = ResourceNotFoundError()
    with pytest.raises(ResourceNotFoundError):
        await TranscriptTextExports(reader).export_revision("transcript-one", 1)


async def test_storage_errors_are_safe_failures_and_cancellation_propagates() -> None:
    reader = Reader(revision())
    reader.error = RuntimeError("合成私密正文")
    with pytest.raises(TranscriptExportError) as caught:
        await TranscriptTextExports(reader).export_revision("transcript-one", 1)
    assert caught.value.code == "source_unavailable"
    assert "合成" not in str(caught.value) and caught.value.__suppress_context__
    reader.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await TranscriptTextExports(reader).export_revision("transcript-one", 1)
