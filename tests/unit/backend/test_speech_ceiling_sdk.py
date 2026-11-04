"""Official SDK over a bounded task-owned loopback; synthetic audio only."""

from dataclasses import replace
from typing import Any

import pytest

from speechnexus_backend.app import create_app
from tests.integration.test_tts_factory_pg import factory_client
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_speech_http import REQUEST, Synthesizer, mp3, pcm, service, wav


@pytest.mark.parametrize("audio_format", ["mp3", "wav", "pcm"])
@pytest.mark.parametrize("request_ms", [None, 100])
async def test_sdk_omission_and_request_ceiling(audio_format: str, request_ms: int | None) -> None:
    audio = (
        replace(mp3(), duration_ms=200)
        if audio_format == "mp3"
        else wav(200)
        if audio_format == "wav"
        else pcm(200)
    )
    chain, synth = service(synth=Synthesizer(audio))
    body: dict[str, Any] = {**REQUEST, "nexus": {"voice_revision": "4"}}
    if audio_format != "mp3":
        body["response_format"] = audio_format
    if request_ms is not None:
        body["nexus"]["max_output_audio_ms"] = request_ms
    async with factory_client(create_app(settings=_settings(), speech=chain), "sdk") as http:
        response = await http.post("/v1/audio/speech", json=body)
    assert response.status_code == (200 if request_ms is None else 503)
    assert len(synth.plans) == 1
    assert synth.plans[0].max_audio_ms == (
        chain.limits.max_output_audio_ms if request_ms is None else request_ms
    )
    if request_ms is None:
        assert response.content == audio.payload
    else:
        assert response.json()["error"]["code"] == "speech_output_limit_exceeded"
