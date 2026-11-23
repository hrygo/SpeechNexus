"""A kind no queue can claim is refused before the Job store is touched (#1080).

The dispatch rule itself lives in ``domain/public_jobs.py`` (one lane column, one
stored command kind); the service applies it so no entry point can commit a Job
the production claim can never match. These cases are the service boundary: they
assert the refusal and that the store is never called, which is what makes "zero
rows" true for every caller rather than only for the HTTP routes.
"""

from dataclasses import replace
from typing import Any

import pytest
from speechnexus_contracts import parse

from speechnexus_backend.speech.application.public_jobs import (
    BindingFact,
    JobKindNotDispatchableError,
    JobRetryNotAllowedError,
    PublicJobs,
)
from speechnexus_backend.speech.domain.public_jobs import (
    UNDISPATCHABLE_JOB_KINDS,
    JobCreateResult,
    NewPublicJob,
)
from tests.unit.backend.test_public_jobs import Facts, Store


class VoiceFacts(Facts):
    def __init__(self) -> None:
        super().__init__()
        self.binding = BindingFact(
            self.binding.publication, {"tasks": ["voice_design", "voice_clone"]}
        )

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None:
        return ("ready", "voice_reference") if asset_id == "synthetic-reference" else None


class RecordingStore(Store):
    """The fake store, plus every write port the refusal must keep untouched."""

    def __init__(self) -> None:
        super().__init__()
        self.created = 0
        self.revisions = 0

    def create(self, job: NewPublicJob) -> JobCreateResult:
        self.created += 1
        return super().create(job)

    def create_revision(self, job: NewPublicJob, *, draft_id: str, expected_revision: str) -> Any:
        self.revisions += 1
        raise AssertionError("a refused revision append must not reach the store")


INPUTS: dict[str, dict[str, Any]] = {
    "voice_design": {"description": "Synthetic warm voice", "preview_text": "Synthetic preview"},
    "voice_clone": {
        "reference_audio_asset_id": "synthetic-reference",
        "source_use_confirmed": True,
    },
    "voice_preview": {"voice_draft_id": "voice-draft", "draft_revision": "1", "text": "hello"},
}


def test_the_domain_names_every_laneless_or_commandless_kind() -> None:
    # The refusal's scope is the declaration, not a list kept in the service. A new
    # kind with no lane or no stored command is refused the moment it is declared.
    assert UNDISPATCHABLE_JOB_KINDS == ("voice_design", "voice_clone", "voice_preview")


@pytest.mark.parametrize("kind", ["voice_design", "voice_clone", "voice_preview"])
def test_an_undispatchable_kind_is_refused_before_any_write(kind: str) -> None:
    store = RecordingStore()
    jobs = PublicJobs(VoiceFacts(), store)
    body = parse(
        "JobCreateRequest",
        {"kind": kind, "model": "tts-quality", "input": INPUTS[kind]},
    )

    with pytest.raises(JobKindNotDispatchableError) as refused:
        jobs.submit(body, request_id="request-refused", idempotency_key=None)

    assert refused.value.kind == kind
    # Zero writes, not "zero committed writes": the store was never asked.
    assert store.created == 0 and store.revisions == 0
    assert store.requests == [] and store.jobs == {}


@pytest.mark.parametrize("kind", ["voice_design", "voice_clone", "voice_preview"])
def test_the_refusal_precedes_the_idempotency_lookup(kind: str) -> None:
    # An old key does not replay a Job this release cannot dispatch: the refusal is a
    # fact about the kind, so it is decided before any stored intent is read. The
    # committed Job stays readable through its own id.
    store = RecordingStore()
    jobs = PublicJobs(VoiceFacts(), store)
    body = {"kind": kind, "model": "tts-quality", "input": INPUTS[kind]}
    reads: list[int] = []
    original = store.find_by_key

    def counting_find_by_key(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        return original(*args, **kwargs)

    store.find_by_key = counting_find_by_key  # type: ignore[method-assign]
    with pytest.raises(JobKindNotDispatchableError):
        jobs.submit(body, request_id="request-refused", idempotency_key="voice-key-123")
    assert reads == []


def test_a_revision_append_of_a_refused_kind_never_reaches_the_store() -> None:
    # A revision append commits another Job of the draft's own kind and advances the
    # draft head, so it is the same refused execution.
    store = RecordingStore()
    jobs = PublicJobs(VoiceFacts(), store)
    body = {
        "kind": "voice_design",
        "model": "tts-quality",
        "input": INPUTS["voice_design"],
        "workspace_id": "workspace-default",
        "draft_target": {"id": "draft-abc", "expected_revision": "1"},
    }
    with pytest.raises(JobKindNotDispatchableError):
        jobs.submit_revision(
            body,
            draft_id="draft-abc",
            expected_revision="1",
            request_id="request-revision",
            idempotency_key=None,
        )
    assert store.revisions == 0 and store.created == 0


def test_a_retry_of_a_refused_kind_commits_no_replacement() -> None:
    """A Job an older release left behind cannot be retried into a new one.

    ``retry`` rebuilds the parent's kind from its stored intent, so it is the same
    unclaimable execution; the guard sits with the state check and before the intent
    is read. The parent is created through the plan-A path (refusal emptied for that
    call only) and then frozen as ``cancelled``, which is what an upgrade finds on a
    Job committed before this release. The fake's ``intent_document`` raises, so a
    missing guard fails here instead of committing a replacement.
    """

    store = RecordingStore()
    jobs = PublicJobs(VoiceFacts(), store)
    body = {"kind": "voice_design", "model": "tts-quality", "input": INPUTS["voice_design"]}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "speechnexus_backend.speech.application.public_jobs.UNDISPATCHABLE_JOB_KINDS", ()
        )
        created = jobs.submit(body, request_id="request-parent", idempotency_key=None)
    parent = store.jobs[created.job_id]
    store.jobs[parent.job_id] = replace(parent, state="cancelled", status_version=2)
    before = store.created

    with pytest.raises(JobRetryNotAllowedError):
        jobs.retry(parent.job_id, request_id="request-retry", idempotency_key=None)

    assert store.created == before == 1
    assert len(store.jobs) == 1
