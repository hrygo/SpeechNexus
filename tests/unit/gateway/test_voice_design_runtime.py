"""Private VoiceDesign activation, exclusive variant lanes and warmup readiness."""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from speechnexus_gateway.app import create_app
from speechnexus_gateway.file_speech_runtime import load_file_speech_configuration
from speechnexus_gateway.file_voice_design_runtime import load_voice_design_configuration
from speechnexus_gateway.settings import Settings
from tests.fake.file_tts_configuration import write_tts_configuration
from tests.fake.file_voice_design_configuration import write_voice_design_configuration

TOKEN = "a" * 32


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "control_database_url": SecretStr("postgresql://unused@127.0.0.1/unused"),
        "node_id": "node-1",
        "gateway_id": "gateway-1",
        "component_token": SecretStr(TOKEN),
        "ingress_lock_directory": tmp_path,
    }
    return Settings(**(fields | overrides))


def design_settings(tmp_path: Path, **overrides: Any) -> tuple[Settings, dict[str, Any]]:
    path, document = write_voice_design_configuration(tmp_path)
    settings = settings_for(
        tmp_path,
        file_voice_design_config_file=path,
        file_voice_design_object_access_key=SecretStr("synthetic-access"),
        file_voice_design_object_secret_key=SecretStr("synthetic-secret"),
        **overrides,
    )
    return settings, document


def test_a_complete_private_activation_loads_but_grants_nothing(tmp_path: Path) -> None:
    settings, document = design_settings(tmp_path)
    configuration = load_voice_design_configuration(settings)

    assert configuration is not None
    assert configuration.policy.config_revision == "policy-voice-design"
    assert configuration.policy.task.value == "VoiceDesign"
    assert configuration.policy.sample_rate_hz == 24000
    assert configuration.policy.max_audio_ms == 60000
    assert configuration.worker.max_result_bytes == document["worker_limits"]["max_result_bytes"]
    assert configuration.endpoint.address == "127.0.0.1"
    assert configuration.warmup_probe().name == "voice_design_warmup"
    assert type(configuration.execution()).__name__ == "CommandFileVoiceDesign"


def test_no_configuration_means_no_voice_design_lane(tmp_path: Path) -> None:
    assert load_voice_design_configuration(settings_for(tmp_path)) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: {**document, "protocol": "file_tts_v1"},
        lambda document: {
            **document,
            "voice_design_policy": {**document["voice_design_policy"], "task": "Base"},
        },
        lambda document: {
            **document,
            "voice_design_policy": {**document["voice_design_policy"], "sample_rate_hz": 16000},
        },
        lambda document: {
            **document,
            "worker_limits": {**document["worker_limits"], "max_result_bytes": 1},
        },
    ],
)
def test_an_unsupported_configuration_is_refused(mutate: Any, tmp_path: Path) -> None:
    path, document = write_voice_design_configuration(tmp_path / "config")
    path.write_text(json.dumps(mutate(document)))
    settings = settings_for(
        tmp_path,
        file_voice_design_config_file=path,
        file_voice_design_object_access_key=SecretStr("synthetic-access"),
        file_voice_design_object_secret_key=SecretStr("synthetic-secret"),
    )
    with pytest.raises(ValueError):
        load_voice_design_configuration(settings)


def test_an_endpoint_for_another_node_is_refused(tmp_path: Path) -> None:
    from speechnexus_gateway.domain.voice_design import VoiceDesignExecutionTarget

    path, _ = write_voice_design_configuration(
        tmp_path / "config",
        target=VoiceDesignExecutionTarget(
            "node-2",
            "gateway-1",
            "voice-creation",
            "instance-voice-design",
            1,
            "binding-voice-design",
        ),
    )
    settings = settings_for(
        tmp_path,
        file_voice_design_config_file=path,
        file_voice_design_object_access_key=SecretStr("synthetic-access"),
        file_voice_design_object_secret_key=SecretStr("synthetic-secret"),
    )
    with pytest.raises(ValueError):
        load_voice_design_configuration(settings)


def test_one_gateway_cannot_serve_both_tts_variants(tmp_path: Path) -> None:
    design_path, _ = write_voice_design_configuration(tmp_path / "design")
    tts_path, _ = write_tts_configuration(tmp_path / "tts")

    with pytest.raises(ValueError):
        settings_for(
            tmp_path,
            file_tts_config_file=tts_path,
            file_tts_object_access_key=SecretStr("synthetic-access"),
            file_tts_object_secret_key=SecretStr("synthetic-secret"),
            file_voice_design_config_file=design_path,
            file_voice_design_object_access_key=SecretStr("synthetic-access"),
            file_voice_design_object_secret_key=SecretStr("synthetic-secret"),
        )


def test_partial_voice_design_settings_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        settings_for(
            tmp_path,
            file_voice_design_config_file=tmp_path / "unused.json",
        )


def test_a_configured_lane_refuses_to_share_a_gateway_with_tts(tmp_path: Path) -> None:
    design_path, _ = write_voice_design_configuration(tmp_path / "design")
    settings = settings_for(
        tmp_path,
        file_voice_design_config_file=design_path,
        file_voice_design_object_access_key=SecretStr("synthetic-access"),
        file_voice_design_object_secret_key=SecretStr("synthetic-secret"),
    )
    configuration = load_file_speech_configuration(settings)
    assert configuration is not None
    assert configuration.voice_design is not None
    assert configuration.tts is None and configuration.asr is None


async def test_readiness_requires_a_warmup_synthesis_not_a_health_answer(
    tmp_path: Path,
) -> None:
    settings, _ = design_settings(tmp_path)
    app = create_app(settings=settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert any(check["name"] == "voice_design_warmup" for check in checks)
    warmup = next(check for check in checks if check["name"] == "voice_design_warmup")
    assert warmup["status"] == "failed"
