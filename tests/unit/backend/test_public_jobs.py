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
    ModelNotPublishedError,
    PublicJobs,
    UnsupportedJobKindError,
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
    check_transition,
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

    def publication(self, kind: str, object_id: str, revision: int) -> Publication | None:
        return self.published.get((kind, object_id, revision))

    def pronunciation_exists(self, object_id: str, revision: int) -> bool:
        return False

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None:
        return None

    def catalog_revision(self) -> int:
        return 31

    def now_us(self) -> int:
        return NOW


class Store:
    def __init__(self) -> None:
        self.jobs: dict[str, PublicJob] = {}
        self.keys: dict[str, tuple[str, str]] = {}

    def create(self, job: NewPublicJob) -> JobCreateResult:
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

    def get(self, job_id: str) -> PublicJob | None:
        return self.jobs.get(job_id)

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
    with pytest.raises(UnsupportedJobKindError):
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
    assert wire["progress"] == {"unit": "text_codepoints", "completed": 0, "total": 15}
    assert wire["artifacts"] == [] and wire["result"] is None


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
