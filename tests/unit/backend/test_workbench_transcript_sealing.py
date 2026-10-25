"""Explicit immutable meeting sources, separate from public transport DTOs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from speechnexus_backend.workbench.application.transcript_exports import TranscriptTextExports
from speechnexus_backend.workbench.application.version_ports import VersionSession
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
    SpeechSource,
    Transcript,
)
from tests.fake.workbench_versions import MemoryVersionUnitOfWork

NOW = datetime(2026, 10, 24, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)
BODY = "\ufeff合成会议原文\r\n🙂e\u0301 "


def draft(*, imported: bool = False) -> Transcript:
    value = Transcript.create(
        transcript_id="t",
        project_id="p",
        display_name="合成",
        revision_id="r1",
        text=BODY,
        now=NOW,
        source=SpeechSource("artifact-test", "receipt-test") if imported else None,
    )
    return (
        value.revise(expected_revision=1, revision_id="r2", text=BODY, now=NOW)
        if imported
        else value
    )


@pytest.mark.parametrize("imported", [False, True])
def test_seal_appends_and_keeps_previous_text_status_and_source(imported: bool) -> None:
    previous = draft(imported=imported)
    sealed = previous.seal(expected_revision=previous.head.number, revision_id="sealed", now=LATER)
    assert sealed.head.number == previous.head.number + 1
    assert sealed.head.status == "sealed" and previous.head.status == "draft"
    assert sealed.head.text == previous.head.text == BODY
    assert sealed.head.source_revision == previous.head.number
    assert sealed.head.source == previous.head.source
    assert sealed.head.created_at == LATER and sealed.created_at == previous.created_at
    assert sealed.id == previous.id and sealed.project_id == previous.project_id
    assert BODY not in repr(sealed) and BODY not in repr(sealed.head)


@pytest.mark.parametrize("text", ["", "最终人工校对", "\r\n🙂e\u0301 "])
def test_seal_explicit_final_text_is_lossless_and_does_not_rewrite_draft(text: str) -> None:
    previous = draft()
    sealed = previous.seal(expected_revision=1, revision_id="r2", text=text, now=LATER)
    assert sealed.head.text == text and previous.head.text == BODY


@pytest.mark.parametrize("bad", ["a\x00b", "a\ud800b", "a\udfffb", "x" * 1_000_001])
def test_seal_rejects_invalid_final_body_without_echoing_it(bad: str) -> None:
    with pytest.raises(InvalidVersionChangeError) as caught:
        draft().seal(expected_revision=1, revision_id="r2", text=bad, now=LATER)
    assert bad not in str(caught.value)


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", None, 0, -1, 10**19])
def test_seal_rejects_non_domain_revision_values(bad: object) -> None:
    with pytest.raises(InvalidVersionChangeError):
        draft().seal(expected_revision=cast(int, bad), revision_id="r2", now=LATER)


def test_seal_rejects_stale_head_resealing_overflow_and_bad_time() -> None:
    original = draft()
    with pytest.raises(RevisionConflictError):
        original.seal(expected_revision=2, revision_id="r2", now=LATER)
    sealed = original.seal(expected_revision=1, revision_id="r2", now=LATER)
    with pytest.raises(InvalidVersionChangeError):
        sealed.seal(expected_revision=2, revision_id="r3", now=LATER)
    imported = Transcript.create(
        transcript_id="t",
        project_id="p",
        display_name="合成",
        revision_id="r1",
        text=BODY,
        now=NOW,
        source=SpeechSource("a", "r"),
    )
    with pytest.raises(InvalidVersionChangeError):
        imported.seal(expected_revision=1, revision_id="r2", now=LATER)
    maximum = replace(original, head=replace(original.head, number=10**19 - 1))
    with pytest.raises(InvalidVersionChangeError):
        maximum.seal(expected_revision=10**19 - 1, revision_id="r2", now=LATER)
    for now in (NOW - timedelta(seconds=1), datetime(2026, 10, 24)):
        with pytest.raises(InvalidVersionChangeError):
            original.seal(expected_revision=1, revision_id="r2", now=now)


async def setup(store: MemoryVersionUnitOfWork) -> tuple[WorkbenchVersions, Transcript]:
    app = WorkbenchVersions(store)
    parent = await app.create_project(workspace_id="w", name="合成")
    transcript = await app.create_manual_transcript(
        project_id=parent.id, display_name="合成", text=BODY
    )
    return app, transcript


async def test_seal_historical_replay_precedes_new_head_and_exports_each_revision() -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app, original = await setup(store)
    sealed = await app.seal_transcript(
        transcript_id=original.id, expected_revision=1, text="", idempotency_key="seal"
    )
    assert sealed.head.number == 2 and sealed.head.text == ""
    revised = await app.revise_transcript(
        transcript_id=original.id, expected_revision=2, text="后续校对", idempotency_key="seal"
    )
    assert revised.head.number == 3  # Distinct operation scopes permit the same raw key.
    assert (
        await app.seal_transcript(
            transcript_id=original.id, expected_revision=1, text="", idempotency_key="seal"
        )
        == sealed
    )
    assert await app.get_sealed_revision(original.id, 2) == sealed.head
    assert (await app.get_transcript(original.id)).head == revised.head
    exports = TranscriptTextExports(app)
    assert (await exports.export_revision(original.id, 1)).content == BODY.encode()
    assert (await exports.export_revision(original.id, 2)).content == b""
    assert (await exports.export_revision(original.id, 3)).content == "后续校对".encode()
    with pytest.raises(InvalidVersionChangeError):
        await app.get_sealed_revision(original.id, 1)
    with pytest.raises(InvalidVersionChangeError):
        await app.get_sealed_revision(original.id, 3)
    assert len(store.revisions) == 3 and len(store.receipts) == 2


async def test_same_key_different_intents_including_omitted_and_explicit_body_conflict() -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app, original = await setup(store)
    sealed = await app.seal_transcript(
        transcript_id=original.id, expected_revision=1, idempotency_key="k"
    )
    for text in (BODY, "", "其他合成内容"):
        with pytest.raises(IdempotencyConflictError) as caught:
            await app.seal_transcript(
                transcript_id=original.id, expected_revision=1, text=text, idempotency_key="k"
            )
        assert text == "" or text not in str(caught.value)
    assert (
        await app.seal_transcript(
            transcript_id=original.id, expected_revision=1, idempotency_key="k"
        )
        == sealed
    )
    with pytest.raises(InvalidVersionChangeError):
        await app.seal_transcript(
            transcript_id=original.id, expected_revision=2, idempotency_key="different"
        )
    assert len(store.revisions) == 2 and len(store.receipts) == 1


async def test_same_key_concurrent_seal_publishes_one_version() -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app, original = await setup(store)
    results = await asyncio.gather(
        *(
            app.seal_transcript(
                transcript_id=original.id, expected_revision=1, idempotency_key="same"
            )
            for _ in range(5)
        )
    )
    assert all(value == results[0] for value in results)
    assert len(store.revisions) == 2 and len(store.receipts) == 1


async def test_seal_and_revision_share_cas_without_orphaned_content() -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app, original = await setup(store)
    results = await asyncio.gather(
        app.seal_transcript(transcript_id=original.id, expected_revision=1, idempotency_key="seal"),
        app.revise_transcript(
            transcript_id=original.id, expected_revision=1, text="新稿", idempotency_key="revise"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, RevisionConflictError) for value in results) == 1
    assert len(store.revisions) == 2 and len(store.receipts) == 1


async def test_failed_commit_and_invalid_body_leave_key_and_head_available() -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app, original = await setup(store)
    with pytest.raises(InvalidVersionChangeError):
        await app.seal_transcript(
            transcript_id=original.id, expected_revision=1, text="\x00", idempotency_key="k"
        )
    store.fail_commit = True
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await app.seal_transcript(
            transcript_id=original.id, expected_revision=1, idempotency_key="k"
        )
    store.fail_commit = False
    assert await app.get_transcript(original.id) == original
    assert not store.receipts and len(store.revisions) == 1
    assert (
        await app.seal_transcript(
            transcript_id=original.id, expected_revision=1, idempotency_key="k"
        )
    ).head.status == "sealed"


class PausedStore(MemoryVersionUnitOfWork):
    def __init__(self) -> None:
        super().__init__(now=NOW)
        self.pause = False
        self.writes = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[VersionSession]:
        async with super().transaction() as session:
            yield session
            if self.pause:
                self.writes.set()
                await self.release.wait()


async def test_cancelled_seal_rolls_back_and_can_be_explicitly_retried() -> None:
    store = PausedStore()
    app, original = await setup(store)
    store.pause = True
    task = asyncio.create_task(
        app.seal_transcript(transcript_id=original.id, expected_revision=1, idempotency_key="k")
    )
    try:
        await asyncio.wait_for(store.writes.wait(), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    store.pause = False
    assert await app.get_transcript(original.id) == original
    assert len(store.revisions) == 1 and not store.receipts
    assert (
        await app.seal_transcript(
            transcript_id=original.id, expected_revision=1, idempotency_key="k"
        )
    ).head.number == 2


@pytest.mark.parametrize("bad", [True, 1.0, "1", 0, -1, 10**19])
async def test_sealed_reader_requires_an_explicit_valid_revision(bad: object) -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app, original = await setup(store)
    with pytest.raises(InvalidVersionChangeError):
        await app.get_sealed_revision(original.id, cast(int, bad))
    assert len(store.revisions) == 1 and not store.receipts


async def test_missing_seal_or_source_does_not_create_receipts() -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    app = WorkbenchVersions(store)
    with pytest.raises(ResourceNotFoundError):
        await app.seal_transcript(transcript_id="missing", expected_revision=1, idempotency_key="k")
    with pytest.raises(ResourceNotFoundError):
        await app.get_sealed_revision("missing", 1)
    assert not store.receipts and not store.revisions
