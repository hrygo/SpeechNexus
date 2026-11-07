"""Publication HTTP boundary: declared statuses, stable codes, no placeholder."""

from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.voice_publications import (
    VoicePublicationConflictError,
    VoicePublicationNotEligibleError,
    VoicePublicationUnavailableError,
    build_publication_view,
)
from tests.unit.backend.test_health import _settings

PATH = "/v1/nexus/voice-drafts/draft-synthetic/publications"
BODY = {
    "expected_draft_revision": "1",
    "validation_job_id": "job-synthetic-validation",
    "manual_confirmation": True,
}
PUBLICATION = build_publication_view(
    publication_id="vpub-1",
    draft_id="draft-synthetic",
    draft_revision="1",
    voice_id="voice-synthetic",
    voice_revision="1",
    validation_job_id="job-synthetic-validation",
    validation_id="validation-1",
    published_at_us=1_700_000_000_000_000,
)


class FakePublications:
    def __init__(self, *, created: bool = True, error: BaseException | None = None) -> None:
        self.created, self.error = created, error
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    def publish(
        self,
        draft_id: str,
        payload: dict[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        self.calls.append((draft_id, payload, idempotency_key))
        if self.error is not None:
            raise self.error
        return PUBLICATION, self.created


async def _post(provider: FakePublications, body: dict[str, Any] = BODY) -> Any:
    app = create_app(
        settings=_settings(),
        voice_publications=cast(Any, provider),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        return await http.post(PATH, json=body, headers={"Idempotency-Key": "publish-key-1"})


async def test_publish_created_and_replayed_are_201_and_200() -> None:
    created = await _post(FakePublications(created=True))
    assert created.status_code == 201, created.text
    assert created.json() == PUBLICATION
    replayed = await _post(FakePublications(created=False))
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == PUBLICATION


async def test_manual_confirmation_false_is_rejected_before_the_store() -> None:
    provider = FakePublications()
    response = await _post(provider, {**BODY, "manual_confirmation": False})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "voice_draft_invalid_request"
    assert provider.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code", "retryable"),
    [
        (
            VoicePublicationNotEligibleError("evidence_missing"),
            422,
            "voice_publication_not_eligible",
            False,
        ),
        (VoicePublicationConflictError("head_moved"), 409, "voice_draft_conflict", False),
        (VoicePublicationUnavailableError(), 503, "voice_draft_unavailable", True),
    ],
)
async def test_failures_keep_their_declared_status_and_code(
    error: BaseException, status: int, code: str, retryable: bool
) -> None:
    response = await _post(FakePublications(error=error))
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["retryable"] is retryable
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Should-Retry"] == str(retryable).lower()
