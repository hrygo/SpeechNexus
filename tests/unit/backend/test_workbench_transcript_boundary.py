"""Generated C1 requests reach existing content transactions and fixed speech source reads."""

import asyncio
import copy
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError
from speechnexus_backend.workbench.application.version_ports import VersionUnitOfWork
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import RevisionConflictError
from speechnexus_backend.workbench.interfaces.transcript_commands import TranscriptCommandBoundary
from tests.fake.workbench_versions import MemoryVersionUnitOfWork


async def setup() -> tuple[MemoryVersionUnitOfWork, WorkbenchVersions, str]:
    store = MemoryVersionUnitOfWork(now=datetime(2026, 10, 24, tzinfo=UTC))
    app = WorkbenchVersions(store)
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    return store, app, project.id


async def test_manual_unicode_original_key_replay_and_empty_revision_are_preserved() -> None:
    store, app, project = await setup()
    boundary = TranscriptCommandBoundary(app)
    request = {"display_name": "  合成稿  ", "source": {"kind": "manual", "text": " e\u0301\n😀 "}}
    before = copy.deepcopy(request)
    first = await boundary.create_transcript(project, request, idempotency_key="original-create")
    changed = await boundary.revise_transcript(
        first.id, {"expected_revision": "1", "text": ""}, idempotency_key="original-revise"
    )
    assert first.head.text == " e\u0301\n😀 " and first.display_name == "  合成稿  "
    assert changed.head.text == "" and changed.head.number == 2
    assert changed.head.source_revision == 1
    assert (
        await boundary.create_transcript(project, request, idempotency_key="original-create")
        == first
    )
    assert (
        await boundary.revise_transcript(
            first.id, {"expected_revision": "1", "text": ""}, idempotency_key="original-revise"
        )
        == changed
    )
    assert request == before and len(store.transcripts) == 1 and len(store.revisions) == 2
    with pytest.raises(IdempotencyConflictError):
        await boundary.create_transcript(
            project,
            {"display_name": "不同", "source": {"kind": "manual", "text": "不同"}},
            idempotency_key="original-create",
        )


async def test_explicit_historical_source_and_concurrent_cas_are_not_reinterpreted() -> None:
    _, app, project = await setup()
    boundary = TranscriptCommandBoundary(app)
    original = await boundary.create_transcript(
        project, {"display_name": "合成稿", "source": {"kind": "manual", "text": "原文"}}
    )
    two = await boundary.revise_transcript(original.id, {"expected_revision": "1", "text": "校对"})
    results = await asyncio.gather(
        *(
            boundary.revise_transcript(
                two.id, {"expected_revision": "2", "source_revision": "1", "text": text}
            )
            for text in ("分支一", "分支二")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, RevisionConflictError) for value in results) == 1
    latest = await app.get_transcript(original.id)
    assert latest.head.source_revision == 1 and latest.head.number == 3
    with pytest.raises(ResourceNotFoundError):
        await boundary.revise_transcript(
            original.id, {"expected_revision": "3", "source_revision": "9", "text": "不存在的源"}
        )


async def test_speech_source_uses_fixed_pair_outside_transaction_and_replay_does_not_refetch() -> (
    None
):
    store, _, project = await setup()

    class Reader:
        calls = 0

        async def read(self, *, artifact_id: str, receipt_id: str, workspace_id: str) -> str:
            assert not store._lock.locked()
            assert (artifact_id, receipt_id, workspace_id) == (
                "artifact-one",
                "receipt-one",
                "workspace-one",
            )
            self.calls += 1
            return "合成源\n"

    reader = Reader()
    boundary = TranscriptCommandBoundary(WorkbenchVersions(store, source_reader=reader))
    payload = {
        "display_name": "语音稿",
        "source": {
            "kind": "speech_artifact",
            "artifact_id": "artifact-one",
            "receipt_id": "receipt-one",
        },
    }
    first = await boundary.create_transcript(project, payload, idempotency_key="original-key")
    assert first.head.status == "sealed" and first.head.source is not None
    assert first.head.text == "合成源\n" and first.head.source.artifact_id == "artifact-one"
    assert (
        await boundary.create_transcript(project, payload, idempotency_key="original-key") == first
    )
    assert reader.calls == 1
    with pytest.raises(IdempotencyConflictError):
        await boundary.create_transcript(
            project,
            {"display_name": "语音稿", "source": {"kind": "manual", "text": "不同来源"}},
            idempotency_key="original-key",
        )


async def test_missing_reader_or_project_remains_a_safe_failure_without_synthetic_source() -> None:
    store, app, project = await setup()
    boundary = TranscriptCommandBoundary(app)
    payload = {
        "display_name": "语音稿",
        "source": {
            "kind": "speech_artifact",
            "artifact_id": "artifact-one",
            "receipt_id": "receipt-one",
        },
    }
    with pytest.raises(TranscriptSourceError) as caught:
        await boundary.create_transcript(project, payload, idempotency_key="failure")
    assert caught.value.code == "source_unavailable"
    with pytest.raises(ResourceNotFoundError):
        await boundary.create_transcript("missing-project", payload)
    with pytest.raises(ResourceNotFoundError):
        await boundary.revise_transcript(
            "missing-transcript", {"expected_revision": "1", "text": ""}
        )
    assert store.transcripts == {} and store.receipts == {}


class NoIo:
    def __getattr__(self, name: str) -> Any:
        pytest.fail("invalid JSON reached persistence or source I/O")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"display_name": "x", "source": {"kind": "manual", "text": ""}},
        {"display_name": "x", "source": {"kind": "manual", "text": False}},
        {
            "display_name": "x",
            "source": {"kind": "manual", "text": "x", "artifact_id": "artifact-one"},
        },
        {"display_name": "x", "source": {"kind": "speech_artifact", "artifact_id": "artifact-one"}},
        {
            "display_name": "x",
            "source": {
                "kind": "speech_artifact",
                "artifact_id": "artifact-one",
                "receipt_id": None,
            },
        },
        {
            "display_name": "x",
            "source": {
                "kind": "speech_artifact",
                "artifact_id": "PRIVATE-PATH",
                "receipt_id": "receipt-one",
            },
        },
        {"display_name": "x", "source": {"kind": "wrong", "text": "secret-synthetic"}},
        {
            "display_name": "x",
            "source": {"kind": "manual", "text": "x"},
            "private": "secret-synthetic",
        },
    ],
)
async def test_invalid_create_rejected_before_any_io_without_input_or_diagnostic_leak(
    payload: Any,
) -> None:
    boundary = TranscriptCommandBoundary(WorkbenchVersions(cast(VersionUnitOfWork, NoIo())))
    before = copy.deepcopy(payload)
    with pytest.raises(ContractValidationError) as caught:
        await boundary.create_transcript("project-one", payload)
    assert "secret-synthetic" not in str(caught.value) and "PRIVATE-PATH" not in str(caught.value)
    assert before == payload


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"expected_revision": "1"},
        {"expected_revision": 1, "text": "x"},
        {"expected_revision": True, "text": "x"},
        {"expected_revision": "01", "text": "x"},
        {"expected_revision": "0", "text": "x"},
        {"expected_revision": "1" * 20, "text": "x"},
        {"expected_revision": "1", "text": False},
        {"expected_revision": "1", "text": "x", "source_revision": None},
        {"expected_revision": "1", "text": "x", "source_revision": 1},
        {"expected_revision": "1", "text": "x", "private": "secret-synthetic"},
    ],
)
async def test_invalid_revision_rejected_before_io(payload: Any) -> None:
    boundary = TranscriptCommandBoundary(WorkbenchVersions(cast(VersionUnitOfWork, NoIo())))
    with pytest.raises(ContractValidationError) as caught:
        await boundary.revise_transcript("transcript-one", payload, idempotency_key="unchanged")
    assert "secret-synthetic" not in str(caught.value)
