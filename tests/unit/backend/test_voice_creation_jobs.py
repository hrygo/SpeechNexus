"""Voice generation reuses public Job identity, pinning and replay decisions."""

from typing import Any

import pytest

from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    BindingFact,
    IdempotencyConflictError,
    PublicJobs,
)
from speechnexus_backend.speech.interfaces.http.jobs import public_job
from tests.unit.backend.test_public_jobs import Facts, Store


class VoiceFacts(Facts):
    def __init__(self) -> None:
        super().__init__()
        self.binding = BindingFact(
            self.binding.publication, {"tasks": ["voice_design", "voice_clone"]}
        )

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None:
        return ("ready", "voice_reference") if asset_id == "synthetic-reference" else None


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
