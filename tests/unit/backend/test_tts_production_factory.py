"""Factory configuration, private recipe, and qualification fail-closed checks."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import rfc8785
from pydantic import SecretStr

from speechnexus_backend import speech_runtime
from speechnexus_backend.speech.domain.admission import AdmissionRejectedError
from speechnexus_backend.speech.infrastructure.admission_postgres import _is_tts_business
from speechnexus_backend.speech.infrastructure.builtin_voice_qualification import (
    require_qualified_parameters,
)
from speechnexus_backend.speech.infrastructure.qualified_speech_facts_postgres import (
    QualifiedPostgresSpeechFacts,
)
from speechnexus_backend.speech.infrastructure.tts_recipe import load_tts_recipe
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_transcription_recipe import _document


def recipe_file(tmp_path: Path) -> Path:
    data = _document()
    data.pop("asr")
    data.pop("binding_publication_id")
    data["protocol"] = "tts_admission_v1"
    data["recipe_digest"] = (
        "sha256:"
        + hashlib.sha256(
            rfc8785.dumps({key: value for key, value in data.items() if key != "recipe_digest"})
        ).hexdigest()
    )
    path = tmp_path / "tts.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_private_recipe_rejects_untrusted_input(tmp_path: Path) -> None:
    path = recipe_file(tmp_path)
    assert load_tts_recipe(path).approved.binding_revision_id == "binding-rev-3"
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_tts_recipe(path)
    path.chmod(0o600)
    path.write_text('{"protocol":"tts_admission_v1","protocol":"tts_admission_v1"}')
    with pytest.raises(ValueError):
        load_tts_recipe(path)


def test_tts_command_cannot_bypass_voice_barrier_by_relabeling_business() -> None:
    with pytest.raises(AdmissionRejectedError):
        _is_tts_business({"kind": "asr_file", "gateway_request": {"command": {"kind": "tts"}}})
    with pytest.raises(AdmissionRejectedError):
        _is_tts_business(
            {"kind": "other", "gateway_request": {"command": {"task_kind": "tts_file"}}}
        )
    with pytest.raises(AdmissionRejectedError):
        _is_tts_business({"execution_request": {"command": {"kind": "tts"}}})
    assert not _is_tts_business({"kind": "asr_file"})
    assert _is_tts_business({"kind": "tts_file"})


def test_factory_without_qualified_voice_closes_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings().model_copy(
        update={
            "speech_recipe_file": recipe_file(tmp_path),
            "speech_default_language": "en",
            "speech_settlement_database_url": SecretStr("postgresql://synthetic@127.0.0.1/test"),
            "execution_gateway_base_url": "http://127.0.0.1:9",
            "execution_gateway_component_credential": SecretStr("synthetic"),
            "object_storage_access_key": SecretStr("synthetic"),
            "object_storage_secret_key": SecretStr("synthetic"),
        }
    )

    class Pool:
        closed = True

        def open(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    pool = Pool()
    monkeypatch.setattr(speech_runtime, "speech_pool", lambda *a, **k: pool)
    monkeypatch.setattr(speech_runtime, "require_complete_tts_roots", lambda *a, **k: None)
    monkeypatch.setattr(QualifiedPostgresSpeechFacts, "has_qualified_voice", lambda self: False)
    assert speech_runtime.build_speech(settings) is None
    assert pool.closed


@pytest.mark.parametrize(
    "field,value",
    [
        ("language", "fr"),
        ("response_format", "mp3"),
        ("instructions", "expressive"),
        ("speed", 1.5),
        ("seed", 42),
    ],
)
def test_unqualified_capability_is_rejected(field: str, value: Any) -> None:
    facts = {
        "qualified_capabilities": {
            "languages": ["en"],
            "audio_formats": ["wav"],
            "sample_rates_hz": [24000],
            "modes": ["complete"],
            "max_input_codepoints": 100,
        }
    }
    facts["public_document"] = {"capabilities": dict(facts["qualified_capabilities"])}
    command: dict[str, Any] = {
        "text": "synthetic",
        "parameters": {
            "language": "en",
            "response_format": "wav",
            "instructions": None,
            "speed": 1,
            "seed": None,
            "audio_delivery": "complete",
            "text_commit_mode": "explicit",
        },
    }
    require_qualified_parameters(facts, command, 24000)
    command["parameters"][field] = value
    with pytest.raises(AdmissionRejectedError):
        require_qualified_parameters(facts, command, 24000)


@pytest.mark.asyncio
async def test_default_factory_builds_and_lifespan_closes_owned_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from speechnexus_backend import app as backend_app
    from speechnexus_backend import transcription_runtime
    from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
        GatewayExecutionClient,
    )

    settings = _settings().model_copy(
        update={
            "speech_recipe_file": recipe_file(tmp_path),
            "speech_default_language": "en",
            "speech_settlement_database_url": SecretStr("postgresql://synthetic@127.0.0.1/test"),
            "execution_gateway_base_url": "http://127.0.0.1:9",
            "execution_gateway_component_credential": SecretStr("synthetic"),
            "object_storage_access_key": SecretStr("synthetic"),
            "object_storage_secret_key": SecretStr("synthetic"),
        }
    )

    class Pool:
        closed = True

        def open(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Gateway:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Results:
        def __init__(self, **kwargs: Any) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeSettlement:
        def __init__(self, settings: Any) -> None:
            pass

        def open(self) -> Any:
            return object()

        def close(self) -> None:
            pass

    monkeypatch.setattr(speech_runtime, "TtsSettlementRuntime", FakeSettlement)
    pool, gateway, results = Pool(), Gateway(), Results()
    monkeypatch.setattr(speech_runtime, "speech_pool", lambda *a, **k: pool)
    monkeypatch.setattr(speech_runtime, "require_complete_tts_roots", lambda *a, **k: None)
    monkeypatch.setattr(QualifiedPostgresSpeechFacts, "has_qualified_voice", lambda self: True)
    monkeypatch.setattr(speech_runtime, "MinioExecutionResultObjects", lambda **kwargs: results)

    class Assets:
        class Service:
            def maintain(self, batch_size: int) -> None:
                pass

        service = Service()
        workspaces = None

        def __init__(self, config: Any) -> None:
            pass

        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(backend_app, "AssetRuntime", Assets)
    monkeypatch.setattr(GatewayExecutionClient, "from_settings", lambda settings: gateway)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("unit test must not open network connections")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("psycopg.connect", forbidden)
    monkeypatch.setattr(transcription_runtime, "Settings", lambda: settings)
    monkeypatch.setattr(transcription_runtime, "build_transcription", lambda settings: None)
    app = transcription_runtime.create_app_from_env()
    assert app.state.speech is not None
    assert not pool.closed
    async with app.router.lifespan_context(app):
        pass
    assert vars(pool)["closed"] is True
    assert vars(gateway)["closed"] is True
    assert vars(results)["closed"] is True


def test_factory_failure_closes_both_built_services(monkeypatch: pytest.MonkeyPatch) -> None:
    from speechnexus_backend import transcription_runtime

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    speech, transcription = Resource(), Resource()
    monkeypatch.setattr(transcription_runtime, "Settings", _settings)
    monkeypatch.setattr(transcription_runtime, "build_speech", lambda settings: speech)
    monkeypatch.setattr(
        transcription_runtime, "build_transcription", lambda settings: transcription
    )

    def fail(**kwargs: Any) -> None:
        raise RuntimeError("synthetic composition failure")

    monkeypatch.setattr(transcription_runtime, "create_app", fail)
    with pytest.raises(RuntimeError, match="synthetic composition failure"):
        transcription_runtime.create_app_from_env()
    assert speech.closed and transcription.closed


def test_factory_requires_separate_settlement_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings().model_copy(
        update={
            "speech_recipe_file": recipe_file(tmp_path),
            "speech_default_language": "en",
            "speech_settlement_database_url": None,
            "execution_gateway_base_url": "http://127.0.0.1:9",
            "execution_gateway_component_credential": SecretStr("synthetic-gateway-token"),
            "object_storage_access_key": SecretStr("synthetic"),
            "object_storage_secret_key": SecretStr("synthetic"),
        }
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("missing settlement identity must fail before opening a pool")

    monkeypatch.setattr(speech_runtime, "speech_pool", forbidden)
    assert speech_runtime.build_speech(settings) is None
