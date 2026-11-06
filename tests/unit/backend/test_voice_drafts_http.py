"""Generated wire validation and actual voice router over synthetic Job facts."""

import json
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts.voice_drafts_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from speechnexus_backend.speech.domain.public_jobs import JobState
from speechnexus_backend.speech.infrastructure.voice_drafts_postgres import _view
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import NOW, Store
from tests.unit.backend.test_voice_creation_jobs import VoiceFacts


class DraftStore:
    def __init__(self) -> None:
        self.jobs_store = Store()
        self.jobs, self.requests = self.jobs_store.jobs, self.jobs_store.requests

    def get(self, draft_id: str, revision: str | None = None) -> dict[str, Any] | None:
        job = self.jobs.get(draft_id.removeprefix("draft-"))
        if job is None or (revision is not None and revision != "1"):
            return None
        request = next(item for item in self.requests if item.job_id == job.job_id)
        kind = job.kind.removeprefix("voice_")
        return _view(
            {
                "draft_id": draft_id,
                "generation_job_id": job.job_id,
                "kind": kind,
                "status": "generating",
                "head_revision": "1",
                "metadata_revision": 1,
                "display_name": "Voice " + kind,
                "input_json": json.dumps(
                    {**json.loads(request.request_document)["input"], "kind": kind}
                ),
                "binding_json": '{"id":"tts-quality","revision":"2"}',
                "created_at_us": NOW,
                "updated_at_us": NOW,
                "job_updated_at_us": NOW,
                "job_state": job.state,
                "result_json": job.result_json,
                "error_code": job.error_code,
            }
        )

    def update_metadata(
        self, draft_id: str, command: Any, idempotency_key: str | None
    ) -> dict[str, Any]:
        raise AssertionError("Invalid HTTP metadata must never reach the write port")


def _service() -> tuple[VoiceDrafts, DraftStore]:
    store = DraftStore()
    return VoiceDrafts(PublicJobs(VoiceFacts(), store.jobs_store), store), store


@pytest.mark.parametrize("kind", ["design", "clone"])
async def test_create_and_read_validate_against_generated_surface(kind: str) -> None:
    provider, store = _service()
    app = create_app(settings=_settings(), voice_drafts=provider)
    payload = {
        "model": "tts-quality",
        "input": (
            {"description": "Synthetic warm voice", "preview_text": "Synthetic preview"}
            if kind == "design"
            else {"reference_audio_asset_id": "synthetic-reference", "source_use_confirmed": True}
        ),
    }
    path = "/v1/nexus/voice-" + ("designs" if kind == "design" else "clones")
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        first = await http.post(path, json=payload, headers={"Idempotency-Key": "test-voice-key"})
        assert first.status_code == 202, first.text
        validate_response(
            "createVoiceDesign" if kind == "design" else "createVoiceClone",
            202,
            {"body": first.json(), "headers": dict(first.headers)},
        )
        replay = await http.post(path, json=payload, headers={"Idempotency-Key": "test-voice-key"})
        assert replay.json() == first.json()
        assert len(store.requests) == 1
        read = await http.get("/v1/nexus/voice-drafts/" + first.json()["draft"]["id"])
        assert read.status_code == 200, read.text
        validate_response("getVoiceDraft", 200, {"body": read.json()})
        assert read.json()["revision"]["input"]["kind"] == kind


@pytest.mark.parametrize(
    "body",
    [
        {"model": "tts-quality", "input": {}},
        {
            "model": "tts-quality",
            "input": {
                "reference_audio_asset_id": "synthetic-reference",
                "source_use_confirmed": False,
            },
        },
        {
            "model": "tts-quality",
            "input": {
                "reference_audio_asset_id": "synthetic-reference",
                "source_use_confirmed": True,
            },
            "status": "published",
        },
    ],
)
async def test_invalid_clone_is_400_without_submission(body: dict[str, Any]) -> None:
    provider, store = _service()
    app = create_app(settings=_settings(), voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        response = await http.post("/v1/nexus/voice-clones", json=body)
    assert response.status_code == 400
    assert not store.requests


async def test_metadata_status_rejected_and_missing_draft_distinguished() -> None:
    provider, _ = _service()
    app = create_app(settings=_settings(), voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        path = "/v1/nexus/voice-drafts/missing-draft"
        response = await http.patch(
            path,
            json={
                "expected_metadata_revision": 1,
                "display_name": "Synthetic",
                "status": "published",
            },
        )
        assert response.status_code == 422
        assert (await http.get(path)).status_code == 404
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
            assert (await http.get(path, headers=headers)).status_code == 403
        assert (await http.get(path + "?unknown=1")).status_code == 400
        assert (
            await http.post(
                "/v1/nexus/voice-designs",
                content=b'{"model":NaN}',
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 400


async def test_voice_provider_absence_is_a_real_unavailable_error() -> None:
    app = create_app(settings=_settings())
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        response = await http.get("/v1/nexus/voice-drafts/missing-draft")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "voice_draft_unavailable"
    validate_response(
        "getVoiceDraft", 503, {"body": response.json(), "headers": dict(response.headers)}
    )


@pytest.mark.parametrize("state", ["failed", "cancelled", "expired"])
async def test_terminal_generation_projects_a_named_failure(state: str) -> None:
    store = DraftStore()
    jobs = PublicJobs(VoiceFacts(), store.jobs_store)
    provider = VoiceDrafts(jobs, store)
    app = create_app(settings=_settings(), voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        created = await http.post(
            "/v1/nexus/voice-designs",
            json={
                "model": "tts-quality",
                "input": {"description": "Synthetic voice", "preview_text": "Synthetic preview"},
            },
        )
        assert created.status_code == 202, created.text
        draft_id = created.json()["draft"]["id"]
        job_id = created.json()["job"]["id"]
        # pending may expire directly; failed and cancelled are reached while running.
        if state != "expired":
            jobs.advance(job_id, "running")
        jobs.advance(
            job_id,
            cast(JobState, state),
            error_code="synthetic_generation_failure" if state == "failed" else None,
        )
        read = await http.get("/v1/nexus/voice-drafts/" + draft_id)
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["draft"]["status"] == "failed"
    failure = body["revision"]["failure"]
    assert failure["code"] == (
        "synthetic_generation_failure" if state == "failed" else "voice_generation_" + state
    )
    assert failure["retryable"] is False
    validate_response("getVoiceDraft", 200, {"body": body})


@pytest.mark.parametrize("language", ["EN-US", "english", "x!"])
async def test_invalid_language_is_rejected_before_submission(language: str) -> None:
    provider, store = _service()
    app = create_app(settings=_settings(), voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        response = await http.post(
            "/v1/nexus/voice-designs",
            json={
                "model": "tts-quality",
                "input": {
                    "description": "Synthetic voice",
                    "preview_text": "Synthetic preview",
                    "language": language,
                },
            },
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "voice_draft_invalid_request"
    assert not store.requests


@pytest.mark.parametrize("language", ["auto", "zh-CN"])
async def test_declared_language_tags_are_accepted(language: str) -> None:
    provider, store = _service()
    app = create_app(settings=_settings(), voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        response = await http.post(
            "/v1/nexus/voice-designs",
            json={
                "model": "tts-quality",
                "input": {
                    "description": "Synthetic voice",
                    "preview_text": "Synthetic preview",
                    "language": language,
                },
            },
        )
    assert response.status_code == 202, response.text
    assert len(store.requests) == 1


async def test_draft_revision_selects_an_exact_revision() -> None:
    provider, _store = _service()
    app = create_app(settings=_settings(), voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        created = await http.post(
            "/v1/nexus/voice-designs",
            json={
                "model": "tts-quality",
                "input": {"description": "Synthetic voice", "preview_text": "Synthetic preview"},
            },
        )
        path = "/v1/nexus/voice-drafts/" + created.json()["draft"]["id"]
        head = await http.get(path, params={"draft_revision": "1"})
        other = await http.get(path, params={"draft_revision": "9"})
    assert head.status_code == 200, head.text
    assert head.json()["revision"]["revision"] == "1"
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "voice_draft_not_found"


async def test_contract_violation_is_a_500_not_a_retryable_503() -> None:
    class BrokenStore(DraftStore):
        def get(self, draft_id: str, revision: str | None = None) -> dict[str, Any] | None:
            # A reply that cannot satisfy VoiceDraftView: the draft has no fields.
            return {"object": "nexus.voice_draft_view", "draft": {}, "revision": None}

    store = BrokenStore()
    provider = VoiceDrafts(PublicJobs(VoiceFacts(), store.jobs_store), store)
    app = create_app(settings=_settings(), voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        response = await http.get("/v1/nexus/voice-drafts/draft-anything")
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "voice_draft_contract_violation"
    assert response.json()["error"]["retryable"] is False
    assert response.headers["X-Should-Retry"] == "false"
    validate_response(
        "getVoiceDraft", 500, {"body": response.json(), "headers": dict(response.headers)}
    )
