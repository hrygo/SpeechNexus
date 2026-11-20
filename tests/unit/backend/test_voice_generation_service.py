"""The plan-A service path for voice generation, with the #1080 refusal bypassed.

`voice_design` and `voice_clone` are refused at creation in this release, so these
cases drive the submission path the voice recipe will use once it lands: Job
identity, idempotent replay, conflict on a changed input, the pinned voice fact
and the clone asset gate. They keep the coverage that path already had, and they
are the reason the store's no-admission writer must stay: a Job created without an
admission pool is what the recipe sandbox resolves.

The refusal itself is pinned by `tests/unit/backend/test_voice_creation_jobs.py`
and by the HTTP cases in `test_undispatchable_voice_jobs_http.py`. The fixture is
requested here on purpose -- nothing in this module asserts a refusal.
"""

from typing import Any

import pytest

from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    IdempotencyConflictError,
    PublicJobs,
)
from speechnexus_backend.speech.interfaces.http.jobs import public_job
from tests.support.voice_recipe import plan_a_write_path  # noqa: F401
from tests.unit.backend.test_public_jobs import Store
from tests.unit.backend.test_voice_creation_jobs import VoiceFacts


@pytest.mark.parametrize("kind", ["voice_design", "voice_clone"])
def test_voice_generation_uses_existing_job_submission_and_replays(kind: str) -> None:
    store = Store()
    jobs = PublicJobs(VoiceFacts(), store)
    inputs: dict[str, Any] = (
        {"description": "Synthetic warm voice", "preview_text": "Synthetic preview"}
        if kind == "voice_design"
        else {"reference_audio_asset_id": "synthetic-reference", "source_use_confirmed": True}
    )
    body = {"kind": kind, "model": "tts-quality", "input": inputs}
    first = jobs.submit(body, request_id="request-first", idempotency_key="voice-key-123")
    replay = jobs.submit(body, request_id="request-replay", idempotency_key="voice-key-123")
    assert first == replay
    assert len(store.requests) == 1
    assert public_job(first)["effective_configuration"]["models"]["voice"]["revision"] == "2"
    assert public_job(first)["kind"] == kind
    with pytest.raises(IdempotencyConflictError):
        jobs.submit(
            {**body, "input": {**inputs, "display_name": "Changed"}},
            request_id="request-conflict",
            idempotency_key="voice-key-123",
        )


def test_clone_rejects_unconfirmed_or_unready_reference() -> None:
    store = Store()
    jobs = PublicJobs(VoiceFacts(), store)
    for asset, confirmation in [("missing-reference", True), ("synthetic-reference", False)]:
        with pytest.raises(AssetNotReadyError):
            jobs.submit(
                {
                    "kind": "voice_clone",
                    "model": "tts-quality",
                    "input": {
                        "reference_audio_asset_id": asset,
                        "source_use_confirmed": confirmation,
                    },
                },
                request_id="request-clone",
                idempotency_key=None,
            )
    assert not store.requests
