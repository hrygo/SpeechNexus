"""The public Job surface resolves published facts and never echoes request defaults."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from speechnexus_contracts import parse

from speechnexus_backend.speech.application.public_jobs import (
    BindingFact,
    IdempotencyConflictError,
    JobOutcomeUnknownError,
    ModelBindingError,
    ModelNotPublishedError,
    PublicJobs,
    VoiceNotPublishedError,
    WorkspaceFact,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCreateResult,
    JobState,
    JobSubmission,
    NewPublicJob,
    PublicJob,
    PublicJobConflictError,
    PublicJobNotFoundError,
    PublicJobUnavailableError,
    check_transition,
    cleanup_state,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.interfaces.http.jobs import public_job

BINDING = Publication("binding", "tts-quality", 2, "binding-rev-2", 1_000)
VOICE = Publication("voice", "voice-vivian", 4, "voice-rev-4", 1_000)
NOW = 1_700_000_000_000_000


class Facts:
    def __init__(self) -> None:
        self.binding = BindingFact(BINDING, {"tasks": ["tts_file"]})
        self.published: dict[tuple[str, str, int], Publication] = {
            ("voice", VOICE.object_id, VOICE.revision): VOICE
        }

    def workspace(self, workspace_id: str) -> WorkspaceFact | None:
        return WorkspaceFact(7, True) if workspace_id == "workspace-default" else None

    def latest_binding(self, model_id: str) -> BindingFact | None:
        return self.binding if model_id == "tts-quality" else None

    def binding_revision(self, binding_id: str, revision: int) -> BindingFact | None:
        fact = self.binding
        return (
            fact
            if fact.publication.reference == {"id": binding_id, "revision": str(revision)}
            else None
        )

    def publication(self, kind: str, object_id: str, revision: int) -> Publication | None:
        return self.published.get((kind, object_id, revision))

    def pronunciation_exists(self, object_id: str, revision: int) -> bool:
        return False

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None:
        return None

    def voice_draft_revision(self, draft_id: str, revision: str) -> None:
        return None

    def catalog_revision(self) -> int:
        return 31

    def now_us(self) -> int:
        return NOW


class Store:
    def __init__(self) -> None:
        self.jobs: dict[str, PublicJob] = {}
        self.keys: dict[str, tuple[str, str]] = {}
        self.requests: list[NewPublicJob] = []

    def find_by_key(
        self,
        workspace_id: str,
        key_digest: str,
        request_digest: str,
        retry_of_job_id: str | None,
    ) -> JobCreateResult | None:
        original = self.keys.get(key_digest)
        if original is None:
            return None
        digest, job_id = original
        job = self.jobs[job_id]
        if job.workspace_id != workspace_id:
            return None
        if digest != request_digest or job.retry_of_job_id != retry_of_job_id:
            return JobCreateResult("conflict", error_code="idempotency_conflict")
        return JobCreateResult("replayed", job)

    def create(self, job: NewPublicJob) -> JobCreateResult:
        self.requests.append(job)
        if job.idempotency_key_digest is not None:
            original = self.keys.get(job.idempotency_key_digest)
            if original is not None:
                digest, job_id = original
                if digest != job.request_digest:
                    return JobCreateResult("conflict", error_code="idempotency_conflict")
                return JobCreateResult("replayed", self.jobs[job_id])
        public = PublicJob(
            job.job_id,
            job.workspace_id,
            job.kind,
            job.model_id,
            "pending",
            1,
            None,
            None,
            job.submission.submitted_at_us,
            job.submission.submitted_at_us,
            job.submission,
        )
        self.jobs[job.job_id] = public
        if job.idempotency_key_digest is not None:
            self.keys[job.idempotency_key_digest] = (job.request_digest, job.job_id)
        return JobCreateResult("created", public)

    def create_revision(self, job: NewPublicJob, *, draft_id: str, expected_revision: str) -> Any:
        raise NotImplementedError

    def revision_for_job(self, draft_id: str, job_id: str) -> str | None:
        raise NotImplementedError

    def get(self, job_id: str) -> PublicJob | None:
        return self.jobs.get(job_id)

    def list_jobs(
        self,
        *,
        workspace_id: str,
        state: str | None,
        kind: str | None,
        after: tuple[int, str] | None,
        limit: int,
    ) -> tuple[PublicJob, ...]:
        raise AssertionError("this fake does not implement listing")

    def intent_document(self, job_id: str) -> Any:
        raise AssertionError("this fake does not implement retries")

    def events(self, job_id: str, *, after_seq: int, limit: int) -> Any:
        raise AssertionError("this fake does not implement events")

    def artifact(self, workspace_id: str, artifact_id: str) -> Any:
        return None

    def cancel(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("this fake does not implement cancellation")

    def settle_cancelled(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("this fake does not implement cancellation")

    def advance(
        self,
        current: PublicJob,
        target: JobState,
        *,
        result_json: str | None,
        error_code: str | None,
        now_us: int,
    ) -> PublicJob:
        updated = replace(
            current,
            state=target,
            status_version=current.status_version + 1,
            result_json=result_json,
            error_code=error_code,
            updated_at_us=now_us,
        )
        self.jobs[current.job_id] = updated
        return updated


def _body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic hello"},
        "voice": {"id": "voice-vivian", "revision": "4"},
    }
    body.update(changes)
    return body


def _service() -> tuple[PublicJobs, Facts, Store]:
    facts, store = Facts(), Store()
    return PublicJobs(facts, store, now_us=lambda: NOW), facts, store


def test_submission_pins_published_revisions_instead_of_echoing_the_request() -> None:
    service, _, _ = _service()
    command = parse("JobCreateRequest", _body())

    job = service.submit(command, request_id="req-create", idempotency_key="idem-key-0001")
    configuration = json.loads(job.submission.effective_configuration_json)

    assert job.state == "pending" and job.status_version == 1
    assert configuration["models"]["tts"] == {"id": "tts-quality", "revision": "2"}
    assert configuration["voice"] == {"id": "voice-vivian", "revision": "4"}
    assert configuration["policy_revision"] == "7"
    assert configuration["catalog_revision"] == "31"
    assert "text" not in configuration


def test_same_key_same_intent_replays_and_a_changed_intent_conflicts() -> None:
    service, _, store = _service()
    command = parse("JobCreateRequest", _body())
    first = service.submit(command, request_id="req-create", idempotency_key="idem-key-0001")
    replay = service.submit(command, request_id="req-create", idempotency_key="idem-key-0001")

    assert replay.job_id == first.job_id
    assert len(store.jobs) == 1
    changed = parse("JobCreateRequest", _body(client_reference="different"))
    with pytest.raises(IdempotencyConflictError):
        service.submit(changed, request_id="req-create", idempotency_key="idem-key-0001")


def test_key_lookup_failure_never_creates_a_new_job(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, store = _service()

    def unavailable(
        workspace_id: str,
        key_digest: str,
        request_digest: str,
        retry_of_job_id: str | None,
    ) -> None:
        raise PublicJobUnavailableError("synthetic failure")

    monkeypatch.setattr(store, "find_by_key", unavailable)
    with pytest.raises(JobOutcomeUnknownError):
        service.submit(
            parse("JobCreateRequest", _body()),
            request_id="req-create",
            idempotency_key="synthetic-key-0001",
        )
    assert store.jobs == {}


def test_explicit_contract_defaults_are_the_same_canonical_intent() -> None:
    service, _, store = _service()
    key = "idem-key-0001"
    first = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-create", idempotency_key=key
    )
    explicit = _body(
        workspace_id="workspace-default",
        output={"audio_format": "mp3", "save_artifacts": True},
        constraints={"allow_degradation": False, "require_exact_parameters": False},
    )
    replay = service.submit(
        parse("JobCreateRequest", explicit), request_id="req-create", idempotency_key=key
    )

    assert replay.job_id == first.job_id
    assert len(store.jobs) == 1


def test_unknown_or_unpublished_facts_are_rejected() -> None:
    service, facts, _ = _service()
    # A preview Job is a supported kind now, but its target revision must exist.
    with pytest.raises(PublicJobNotFoundError):
        service.submit(
            parse(
                "JobCreateRequest",
                {
                    "kind": "voice_preview",
                    "model": "x",
                    "input": {
                        "voice_draft_id": "voice-draft",
                        "draft_revision": "1",
                        "text": "hello",
                    },
                },
            ),
            request_id="req-create",
            idempotency_key=None,
        )
    with pytest.raises(ModelNotPublishedError):
        service.submit(
            parse("JobCreateRequest", _body(model="missing-model")),
            request_id="req-create",
            idempotency_key=None,
        )
    facts.published.clear()
    with pytest.raises(VoiceNotPublishedError):
        service.submit(
            parse("JobCreateRequest", _body()),
            request_id="req-create",
            idempotency_key=None,
        )


def test_wire_document_satisfies_the_frozen_job_contract() -> None:
    service, _, _ = _service()
    job = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-create", idempotency_key=None
    )

    wire = public_job(job)

    assert parse("Job", wire)["status"] == "queued"
    assert wire["cleanup_state"] == "not_required"
    assert wire["request_id"] == "req-create"
    assert wire["progress"] == {"unit": "audio_ms", "completed": 0, "total": None}
    assert wire["artifacts"] == [] and wire["result"] is None


def test_cancelling_cleanup_projection_is_always_pending() -> None:
    service, _, _ = _service()
    job = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-create", idempotency_key=None
    )

    assert cleanup_state(replace(job, state="cancelling")) == "pending"


def test_read_unknown_job_is_absent_not_invented() -> None:
    service, _, _ = _service()
    with pytest.raises(PublicJobNotFoundError):
        service.get("job-missing")


def test_transition_invariants_are_checked_before_the_database() -> None:
    with pytest.raises(PublicJobConflictError):
        check_transition("pending", "succeeded", result_json=None, error_code=None)
    with pytest.raises(PublicJobConflictError):
        check_transition("pending", "failed", result_json=None, error_code=None)
    with pytest.raises(PublicJobConflictError):
        check_transition("succeeded", "running", result_json='{"kind":"tts"}', error_code=None)
    check_transition("pending", "running", result_json=None, error_code=None)
    check_transition("running", "succeeded", result_json='{"kind":"tts"}', error_code=None)


def test_storage_model_rejects_success_without_a_result() -> None:
    submission = JobSubmission("req-create", None, "{}", 1, "text_codepoints", 1, NOW)
    with pytest.raises(ValueError):
        PublicJob(
            "job-synthetic",
            "workspace-default",
            "tts",
            "tts-quality",
            "succeeded",
            1,
            None,
            None,
            NOW,
            NOW,
            submission,
        )


def _pinned_body() -> dict[str, Any]:
    return _body(model="narrator-tts", model_binding=BINDING.reference)


def _pin_facts(facts: Facts) -> None:
    facts.binding = BindingFact(
        BINDING,
        {
            "binding_id": BINDING.object_id,
            "revision": str(BINDING.revision),
            "revision_id": BINDING.revision_id,
            "logical_model": "narrator-tts",
            "tasks": ["tts_file"],
        },
    )


def test_pin_resolves_distinct_logical_model_and_preserves_original_request() -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    command = parse("JobCreateRequest", _pinned_body())
    job = service.submit(command, request_id="req-pin", idempotency_key="idem-pin-0001")
    assert (
        json.loads(job.submission.effective_configuration_json)["models"]["tts"]
        == BINDING.reference
    )
    assert json.loads(store.requests[0].request_document)["model_binding"] == BINDING.reference
    assert store.requests[0].expected_binding == BINDING


def test_replay_precedes_changed_facts_and_conflicting_pin_is_not_reresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    command = _pinned_body()
    first = service.submit(command, request_id="req-pin", idempotency_key="idem-pin-0001")

    # Fail any mutable lookup after the first commit. The original result wins.
    def unavailable(*args: Any) -> Any:
        raise AssertionError("Replay must not resolve current facts")

    monkeypatch.setattr(facts, "workspace", unavailable)
    monkeypatch.setattr(facts, "binding_revision", unavailable)
    assert service.submit(command, request_id="req-retry", idempotency_key="idem-pin-0001") == first
    with pytest.raises(IdempotencyConflictError):
        service.submit(
            _body(model_binding={**BINDING.reference, "revision": "3"}),
            request_id="req-retry",
            idempotency_key="idem-pin-0001",
        )
    assert len(store.jobs) == 1
    assert len(store.requests) == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"current": False}, "model_binding_not_current"),
        ({"retired": True}, "model_binding_retired"),
        (
            {"definition": {"logical_model": "other-tts", "tasks": ["tts_file"]}},
            "model_binding_mismatch",
        ),
        (
            {"definition": {"logical_model": "narrator-tts", "tasks": ["tts_realtime"]}},
            "model_binding_task_not_supported",
        ),
    ],
)
def test_invalid_selection_never_writes_or_falls_back(change: dict[str, Any], code: str) -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    facts.binding = replace(facts.binding, **change)
    with pytest.raises(ModelBindingError) as failure:
        service.submit(_pinned_body(), request_id="req-pin", idempotency_key=None)
    assert failure.value.code == code
    assert not store.requests and not store.jobs


def test_missing_pin_is_rejected_even_when_latest_binding_exists() -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    command = _body(model_binding={**BINDING.reference, "revision": "1"})
    with pytest.raises(ModelBindingError, match="model_binding_not_published"):
        service.submit(command, request_id="req-pin", idempotency_key=None)
    assert not store.jobs


@pytest.mark.parametrize("revision", ["01", "0", "1\n", "١", "1.0", "1e3", "1" * 20, 2, None])
def test_application_rejects_non_decimal_or_inexact_pin(revision: Any) -> None:
    service, _, store = _service()
    with pytest.raises(ValueError):
        service.submit(
            _body(model_binding={"id": BINDING.object_id, "revision": revision}),
            request_id="req-pin",
            idempotency_key=None,
        )
    assert not store.jobs


def test_maximum_decimal_revision_is_not_rounded_or_cast_to_bigint() -> None:
    service, facts, store = _service()
    maximum = replace(BINDING, revision=9_999_999_999_999_999_999)
    facts.binding = BindingFact(maximum, {"logical_model": "narrator-tts", "tasks": ["tts_file"]})
    job = service.submit(
        parse("JobCreateRequest", _body(model="narrator-tts", model_binding=maximum.reference)),
        request_id="req-pin",
        idempotency_key=None,
    )
    assert (
        json.loads(job.submission.effective_configuration_json)["models"]["tts"]
        == maximum.reference
    )
    assert json.loads(store.requests[0].request_document)["model_binding"] == maximum.reference


@pytest.mark.parametrize("field", ["binding_id", "revision", "revision_id"])
def test_poisoned_embedded_binding_identity_fails_closed(field: str) -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    facts.binding = replace(facts.binding, definition={**facts.binding.definition, field: "wrong"})
    with pytest.raises(PublicJobUnavailableError):
        service.submit(_pinned_body(), request_id="req-pin", idempotency_key=None)
    assert not store.jobs


def test_unpinned_legacy_request_keeps_binding_id_resolution() -> None:
    service, facts, store = _service()
    _pin_facts(facts)
    service.submit(_body(), request_id="req-old", idempotency_key=None)
    assert store.requests[0].expected_binding is None
    assert "model_binding" not in json.loads(store.requests[0].request_document)
    with pytest.raises(ModelNotPublishedError):
        service.submit(_body(model="narrator-tts"), request_id="req-old", idempotency_key=None)
