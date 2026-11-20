"""Both public create surfaces refuse a Job kind no queue can claim (#1080).

Before this, ``POST /v1/nexus/jobs`` and the voice-creation routes answered ``202``
for ``voice_design``/``voice_clone``/``voice_preview`` and committed rows whose
admission attempt and lease did not exist, so the production claim (which joins
both) never matched them and the caller watched ``pending`` forever. The refusal
is the declared 422, decided before the store is called: nothing is written and
the answer does not change until a dispatch recipe for the kind ships.

The opposite direction is asserted in the same module: ``tts`` still reaches
``202`` and the store still receives it, so a rule that refused everything could
not pass here.
"""

import json
from typing import Any, cast

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import (
    PublicJobs,
)
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import NOW, Facts
from tests.unit.backend.test_voice_creation_jobs import INPUTS, RecordingStore, VoiceFacts
from tests.unit.backend.test_voice_drafts_http import DraftStore

VOICE_CREATES = (
    ("voice_design", "/v1/nexus/voice-designs", "design"),
    ("voice_clone", "/v1/nexus/voice-clones", "clone"),
)


def _app(*, drafts: bool) -> tuple[Any, RecordingStore, DraftStore]:
    store = RecordingStore()
    draft_store = DraftStore(cast(Any, store))
    app: Any = create_app(
        settings=_settings(),
        jobs=cast_jobs(store),
        voice_drafts=VoiceDrafts(PublicJobs(VoiceFacts(), store), draft_store) if drafts else None,
    )
    return app, store, draft_store


def cast_jobs(store: RecordingStore) -> PublicJobs:
    return PublicJobs(VoiceFacts(), store, now_us=lambda: NOW)


def _assert_refusal(response: httpx.Response, code: str, *, needle: str) -> None:
    body = response.json()
    assert response.status_code == 422, response.text
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is False
    assert body["error"]["type"] == "invalid_request_error"
    # The message must tell the caller the kind is not dispatchable, never name an
    # internal relation. #1080's complaint was an unreadable permanent pending.
    assert needle in body["error"]["message"], body["error"]["message"]
    for internal in ("admission_attempts", "admission_leases", "coordination_jobs"):
        assert internal not in json.dumps(body)


@pytest.mark.parametrize("kind", ["voice_design", "voice_clone", "voice_preview"])
async def test_the_generic_job_surface_refuses_an_undispatchable_kind(kind: str) -> None:
    store = RecordingStore()
    app = create_app(settings=_settings(), jobs=cast_jobs(store))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.post(
            "/v1/nexus/jobs",
            json={"kind": kind, "model": "tts-quality", "input": INPUTS[kind]},
        )
    _assert_refusal(response, "job_kind_not_dispatchable", needle="cannot be dispatched")
    # Not one row: the store's only write ports were never called.
    assert store.created == 0 and store.jobs == {} and store.requests == []


@pytest.mark.parametrize(("kind", "path", "draft_kind"), VOICE_CREATES)
async def test_the_voice_creation_routes_refuse_their_own_kind(
    kind: str, path: str, draft_kind: str
) -> None:
    app, store, _ = _app(drafts=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.post(path, json={"model": "tts-quality", "input": INPUTS[kind]})
    _assert_refusal(response, "voice_draft_job_not_dispatchable", needle="dispatch path")
    # No draft and no Job: the refusal precedes the transaction that writes both.
    assert store.created == 0 and store.jobs == {} and store.requests == []
    # The handler already validated this reply against the generated 422, so the code
    # is machine-visible rather than prose-only.
    assert response.headers["X-Should-Retry"] == "false"


class CandidateTargetStore:
    """Answers the draft target the two draft-scoped submit routes read first.

    A refused kind is still refused only after the route resolved *which* draft it
    targets: ``generateVoiceDraftRevision`` and ``createVoicePreview`` read the target
    before they submit, so this fake decides between "no such draft" and "this draft
    exists". It has no write port: reaching one would be the defect the assertions
    below look for.
    """

    def __init__(self, *, exists: bool, kind: str = "design") -> None:
        self._exists, self._kind = exists, kind

    def draft_exists(self, draft_id: str) -> bool:
        return self._exists

    def revision_target(self, draft_id: str, revision: str) -> dict[str, Any] | None:
        if not self._exists:
            return None
        return {
            "kind": self._kind,
            "status": "candidate",
            "head_revision": revision,
            "workspace_id": "workspace-default",
            "binding": {"id": "tts-quality", "revision": "2"},
        }

    def get(self, draft_id: str, revision: str | None = None) -> dict[str, Any] | None:
        raise AssertionError("a refused submission must not read the draft view")

    def revision(self, draft_id: str, revision: str) -> dict[str, Any] | None:
        raise AssertionError("a refused append must not read the appended revision")


DRAFT_SCOPED = (
    (
        "previews",
        "createVoicePreview",
        {"expected_draft_revision": "1", "text": "synthetic preview"},
    ),
    (
        "generations",
        "generateVoiceDraftRevision",
        {
            "expected_draft_revision": "1",
            "model": "tts-quality",
            "input": {
                "description": "Synthetic next voice",
                "preview_text": "Synthetic next preview",
            },
        },
    ),
)


def _draft_app(store: RecordingStore, *, exists: bool) -> Any:
    target = CandidateTargetStore(exists=exists)
    service = PublicJobs(VoiceFacts(), store, now_us=lambda: NOW)
    return create_app(
        settings=_settings(),
        jobs=service,
        voice_drafts=VoiceDrafts(service, cast(Any, target)),
    )


@pytest.mark.parametrize(("segment", "operation", "body"), DRAFT_SCOPED)
async def test_a_missing_draft_answers_404_before_the_kind_refusal(
    segment: str, operation: str, body: dict[str, Any]
) -> None:
    """The route resolves its target first, so the 404 is the target's own answer.

    This is the ordering the contract declares: a request naming a draft that does
    not exist is a 404 on every version of this surface, refused kind or not.
    """

    store = RecordingStore()
    app = _draft_app(store, exists=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.post("/v1/nexus/voice-drafts/draft-absent/" + segment, json=body)
    assert response.status_code == 404, (operation, response.text)
    assert response.json()["error"]["code"] == "voice_draft_not_found"
    assert store.created == 0 and store.revisions == 0


@pytest.mark.parametrize(("segment", "operation", "body"), DRAFT_SCOPED)
async def test_an_existing_draft_reaches_the_kind_refusal_with_no_write(
    segment: str, operation: str, body: dict[str, Any]
) -> None:
    """The append and preview kinds are refused, and neither row nor revision exists.

    ``generateVoiceDraftRevision`` commits a revision *and* a Job, so a refusal that
    ran after the append would leave a head nobody generates; the store's revision
    port raises if it is reached.
    """

    store = RecordingStore()
    app = _draft_app(store, exists=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.post("/v1/nexus/voice-drafts/draft-1/" + segment, json=body)
    _assert_refusal(response, "voice_draft_job_not_dispatchable", needle="dispatch path")
    assert store.created == 0 and store.revisions == 0
    assert store.jobs == {} and store.requests == []


async def test_tts_still_reaches_the_store_and_is_accepted() -> None:
    # The control for the whole module: the same service and the same fake facts
    # accept the kinds this release can dispatch, so the refusal is not "everything".
    store = RecordingStore()
    app = create_app(
        settings=_settings(),
        jobs=PublicJobs(Facts(), store, now_us=lambda: NOW),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.post(
            "/v1/nexus/jobs",
            json={
                "kind": "tts",
                "model": "tts-quality",
                "input": {"text": "synthetic hello"},
                "voice": {"id": "voice-vivian", "revision": "4"},
            },
        )
    assert response.status_code == 202, response.text
    assert response.json()["kind"] == "tts"
    assert store.created == 1 and len(store.jobs) == 1
