"""Synthetic production gate checks; no database or gateway is contacted."""

import httpx
import pytest

from speechnexus_backend import transcription_runtime
from tests.unit.backend.test_health import _settings


@pytest.mark.asyncio
async def test_production_factory_keeps_historical_tts_configuration_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speechnexus_backend import speech_runtime

    monkeypatch.setenv("SPEECHNEXUS_TTS_RECIPE_FILE", "/synthetic/nonexistent-recipe.json")
    monkeypatch.setenv("SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL", "synthetic-only")
    monkeypatch.setattr(transcription_runtime, "build_transcription", lambda settings: None)
    settings = _settings()
    monkeypatch.setattr(transcription_runtime, "Settings", lambda: settings)
    factory_calls: list[object] = []

    def build_closed(config: object) -> None:
        assert config is settings
        factory_calls.append(config)
        assert speech_runtime.build_speech(settings) is None

    monkeypatch.setattr(transcription_runtime, "build_speech", build_closed)
    calls: list[object] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("The closed TTS factory must not access external resources")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)
    monkeypatch.setattr("psycopg_pool.ConnectionPool.open", forbidden)
    monkeypatch.setattr("httpx.Client.send", forbidden)
    assert speech_runtime.build_speech(settings) is None
    app = transcription_runtime.create_app_from_env()
    assert app.state.speech is None
    assert factory_calls == [settings]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/audio/speech", json={"model": "model", "voice": "voice", "input": "synthetic"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "backend_unavailable"
    assert calls == []
