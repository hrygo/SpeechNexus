"""Pinned sealed sources and discard of late results, independent of speech/HTTP."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from speechnexus_backend.workbench.application.llm import (
    DisabledTextLlm,
    LlmCompletion,
    LlmError,
    LlmProfile,
    LlmRequest,
    LlmUsage,
)
from speechnexus_backend.workbench.application.minutes import MinutesGeneration
from speechnexus_backend.workbench.application.versions import WorkbenchVersions
from tests.fake.workbench_versions import MemoryVersionUnitOfWork

PROFILE = LlmProfile(model="synthetic-model", template_revision=7, system_prompt="合成纪要模板")
BODY = "\ufeff合成封存会议\r\n🙂e\u0301 "
OUTPUT = "<script>untrusted synthetic candidate</script>\r\n"


class Llm:
    binding_id = "sha256:" + "a" * 64

    def __init__(self) -> None:
        self.profile = PROFILE
        self.requests: list[LlmRequest] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.failure: Exception | None = None
        self.result = LlmCompletion(PROFILE, OUTPUT, LlmUsage(10, 4))

    async def generate(self, request: LlmRequest) -> LlmCompletion:
        self.requests.append(request)
        self.entered.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return self.result


async def fixture() -> tuple[MemoryVersionUnitOfWork, WorkbenchVersions, Llm, str]:
    uow = MemoryVersionUnitOfWork(now=datetime(2026, 10, 24, tzinfo=UTC))
    app = WorkbenchVersions(uow)
    parent = await app.create_project(workspace_id="w", name="合成")
    original = await app.create_manual_transcript(
        project_id=parent.id, display_name="合成", text=BODY
    )
    await app.seal_transcript(transcript_id=original.id, expected_revision=1)
    return uow, app, Llm(), original.id


async def test_generate_selected_body_once_and_pin_all_provenance_without_writes() -> None:
    uow, app, llm, identity = await fixture()
    original = uow.transcripts.copy(), uow.revisions.copy(), uow.receipts.copy()
    candidate = await MinutesGeneration(app, llm).generate(
        transcript_id=identity,
        source_revision=2,
        template_revision=7,
    )
    assert candidate.transcript_id == identity and candidate.source_revision == 2
    assert candidate.model == PROFILE.model and candidate.template_revision == 7
    assert candidate.max_output_tokens == 2048 and candidate.temperature == 0
    assert candidate.source_sha256 == "sha256:" + hashlib.sha256(BODY.encode()).hexdigest()
    assert candidate.text == OUTPUT and candidate.usage == LlmUsage(10, 4)
    assert len(llm.requests) == 1 and llm.requests[0].text == BODY
    assert (uow.transcripts, uow.revisions, uow.receipts) == original
    assert BODY not in repr(candidate) and OUTPUT not in repr(candidate)


async def test_disabled_default_does_not_even_read_source() -> None:
    _, app, _, _ = await fixture()
    for service in (MinutesGeneration(app), MinutesGeneration(app, DisabledTextLlm())):
        with pytest.raises(LlmError, match="disabled"):
            await service.generate(transcript_id="missing", source_revision=2, template_revision=7)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_revision": True},
        {"source_revision": 0},
        {"source_revision": 10**19},
        {"template_revision": True},
        {"template_revision": 0},
    ],
)
async def test_invalid_intent_does_not_connect(changes: dict[str, object]) -> None:
    _, app, llm, identity = await fixture()
    values: dict[str, Any] = {
        "transcript_id": identity,
        "source_revision": 2,
        "template_revision": 7,
    }
    with pytest.raises(LlmError, match="invalid_input"):
        await MinutesGeneration(app, llm).generate(**(values | changes))
    assert not llm.requests


@pytest.mark.parametrize("changes", [{"template_revision": 8}, {"llm_model": "different"}])
async def test_requested_binding_must_match_server_profile(changes: dict[str, object]) -> None:
    _, app, llm, identity = await fixture()
    values: dict[str, Any] = {
        "transcript_id": identity,
        "source_revision": 2,
        "template_revision": 7,
    }
    with pytest.raises(LlmError, match="profile_mismatch"):
        await MinutesGeneration(app, llm).generate(**(values | changes))
    assert not llm.requests


async def test_draft_and_missing_and_old_source_do_not_connect() -> None:
    uow, app, llm, identity = await fixture()
    service = MinutesGeneration(app, llm)
    with pytest.raises(LlmError, match="source_changed"):
        await service.generate(transcript_id=identity, source_revision=1, template_revision=7)
    with pytest.raises(LlmError, match="source_unavailable"):
        await service.generate(transcript_id="missing", source_revision=2, template_revision=7)
    current = uow.transcripts[identity]
    unsealed = replace(current.head, status="draft")
    uow.transcripts[identity] = replace(current, head=unsealed)
    uow.revisions[identity, 2] = unsealed
    with pytest.raises(LlmError, match="source_not_sealed"):
        await service.generate(transcript_id=identity, source_revision=2, template_revision=7)
    assert not llm.requests


@pytest.mark.parametrize("text", ["", " \r\n", "x" * 100_001])
async def test_empty_or_over_budget_source_does_not_connect(text: str) -> None:
    uow, app, llm, identity = await fixture()
    current = uow.transcripts[identity]
    head = replace(current.head, text=text)
    uow.transcripts[identity] = replace(current, head=head)
    uow.revisions[identity, 2] = head
    with pytest.raises(LlmError, match="invalid_input"):
        await MinutesGeneration(app, llm).generate(
            transcript_id=identity, source_revision=2, template_revision=7
        )
    assert not llm.requests


@pytest.mark.parametrize("change", ["delete", "head", "body", "identity"])
async def test_late_result_is_discarded_after_source_change(change: str) -> None:
    uow, app, llm, identity = await fixture()
    llm.release.clear()
    task = asyncio.create_task(
        MinutesGeneration(app, llm).generate(
            transcript_id=identity, source_revision=2, template_revision=7
        )
    )
    await asyncio.wait_for(llm.entered.wait(), 1)
    # If a transaction leaked across generation this read would deadlock.
    current = await asyncio.wait_for(app.get_transcript(identity), 1)
    if change == "delete":
        uow.transcripts.pop(identity)
        uow.revisions.pop((identity, 2))
    elif change == "head":
        uow.transcripts[identity] = replace(current, head=replace(current.head, number=3))
    else:
        changed = (
            replace(current.head, text="new synthetic")
            if change == "body"
            else replace(current.head, transcript_id="different")
        )
        uow.transcripts[identity] = replace(current, head=changed)
        uow.revisions[identity, 2] = changed
    llm.release.set()
    with pytest.raises(LlmError, match=r"source_unavailable|source_changed"):
        await task
    assert len(llm.requests) == 1


async def test_provider_failure_redacted_and_no_source_mutation() -> None:
    uow, app, llm, identity = await fixture()
    original = uow.transcripts.copy(), uow.revisions.copy()
    llm.failure = RuntimeError("synthetic-private-provider-error")
    with pytest.raises(LlmError, match="unavailable") as caught:
        await MinutesGeneration(app, llm).generate(
            transcript_id=identity, source_revision=2, template_revision=7
        )
    assert "synthetic-private" not in str(caught.value) and caught.value.__suppress_context__
    assert (uow.transcripts, uow.revisions) == original and len(llm.requests) == 1


async def test_wrong_result_binding_is_rejected() -> None:
    _, app, llm, identity = await fixture()
    llm.result = replace(llm.result, profile=replace(PROFILE, template_revision=8))
    with pytest.raises(LlmError, match="profile_mismatch"):
        await MinutesGeneration(app, llm).generate(
            transcript_id=identity, source_revision=2, template_revision=7
        )


async def test_typed_provider_failure_is_preserved_without_retry() -> None:
    _, app, llm, identity = await fixture()
    failure = LlmError("unavailable")
    llm.failure = failure
    with pytest.raises(LlmError) as caught:
        await MinutesGeneration(app, llm).generate(
            transcript_id=identity,
            source_revision=2,
            template_revision=7,
        )
    assert caught.value is failure and len(llm.requests) == 1


async def test_cancellation_propagates_without_late_candidate_or_rerun() -> None:
    uow, app, llm, identity = await fixture()
    original = uow.transcripts.copy(), uow.revisions.copy()
    llm.release.clear()
    task = asyncio.create_task(
        MinutesGeneration(app, llm).generate(
            transcript_id=identity, source_revision=2, template_revision=7
        )
    )
    await asyncio.wait_for(llm.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    llm.release.set()
    assert len(llm.requests) == 1 and (uow.transcripts, uow.revisions) == original
