"""Private Base clone activation, exclusive variant lanes and warmup readiness."""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from speechnexus_gateway.app import create_app
from speechnexus_gateway.domain.voice_clone import VoiceCloneExecutionTarget, VoiceClonePolicy
from speechnexus_gateway.file_speech_runtime import load_file_speech_configuration
from speechnexus_gateway.file_voice_clone_runtime import load_voice_clone_configuration
from speechnexus_gateway.settings import Settings
from tests.fake.file_tts_configuration import write_tts_configuration
from tests.fake.file_voice_clone_configuration import (
    write_voice_clone_configuration,
)
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


def clone_settings(tmp_path: Path, **overrides: Any) -> tuple[Settings, dict[str, Any]]:
    path, document = write_voice_clone_configuration(tmp_path)
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
        **overrides,
    )
    return settings, document


def test_a_complete_private_activation_loads_but_grants_nothing(tmp_path: Path) -> None:
    settings, document = clone_settings(tmp_path)
    configuration = load_voice_clone_configuration(settings)

    assert configuration is not None
    assert configuration.policy.config_revision == "policy-voice-clone"
    assert configuration.policy.task.value == "Base"
    assert configuration.policy.sample_rate_hz == 24000
    assert configuration.policy.allow_x_vector_only is False
    assert configuration.worker.max_result_bytes == document["worker_limits"]["max_result_bytes"]
    assert configuration.endpoint.address == "127.0.0.1"
    assert configuration.warmup_reference.duration_ms == 1500
    assert configuration.warmup_probe().name == "voice_clone_warmup"
    assert type(configuration.execution()).__name__ == "CommandFileVoiceClone"


def test_the_warmup_budget_is_read_from_its_own_configuration_block(tmp_path: Path) -> None:
    path, _ = write_voice_clone_configuration(
        tmp_path, readiness={"warmup_timeout_seconds": 120, "retry_interval_seconds": 5}
    )
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )

    configuration = load_voice_clone_configuration(settings)

    assert configuration is not None
    assert configuration.warmup_timeout_seconds == 120
    assert configuration.retry_interval_seconds == 5


def test_an_invalid_warmup_budget_is_refused(tmp_path: Path) -> None:
    path, _ = write_voice_clone_configuration(tmp_path, readiness={"warmup_timeout_seconds": 0.1})
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )

    with pytest.raises(ValueError):
        load_voice_clone_configuration(settings)


def test_no_configuration_means_no_voice_clone_lane(tmp_path: Path) -> None:
    assert load_voice_clone_configuration(settings_for(tmp_path)) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: {**document, "protocol": "file_voice_design_v1"},
        lambda document: {
            **document,
            "voice_clone_policy": {**document["voice_clone_policy"], "task": "VoiceDesign"},
        },
        lambda document: {
            **document,
            "voice_clone_policy": {**document["voice_clone_policy"], "sample_rate_hz": 16000},
        },
        lambda document: {
            **document,
            "voice_clone_policy": {
                **document["voice_clone_policy"],
                "reference_sample_rates": [8000],
            },
        },
        lambda document: {
            **document,
            "worker_limits": {**document["worker_limits"], "max_result_bytes": 1},
        },
        lambda document: {
            **document,
            "input_limits": {**document["input_limits"], "max_bytes": 16 * 1024 * 1024},
        },
        lambda document: {key: value for key, value in document.items() if key != "warmup"},
    ],
)
def test_an_unsupported_configuration_is_refused(mutate: Any, tmp_path: Path) -> None:
    path, document = write_voice_clone_configuration(tmp_path / "config")
    path.write_text(json.dumps(mutate(document)))
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )
    with pytest.raises(ValueError):
        load_voice_clone_configuration(settings)


def test_a_warmup_transcript_that_cannot_match_its_clip_fails_startup(tmp_path: Path) -> None:
    path, _ = write_voice_clone_configuration(tmp_path / "config", reference_text="x" * 31)
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )
    with pytest.raises(ValueError):
        load_voice_clone_configuration(settings)


def test_a_warmup_clip_that_is_not_an_approved_wav_fails_startup(tmp_path: Path) -> None:
    path, _ = write_voice_clone_configuration(tmp_path / "config", reference=b"not a wave file" * 4)
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )
    with pytest.raises(ValueError):
        load_voice_clone_configuration(settings)


def test_an_endpoint_for_another_node_is_refused(tmp_path: Path) -> None:
    path, _ = write_voice_clone_configuration(
        tmp_path / "config",
        target=VoiceCloneExecutionTarget(
            "node-2",
            "gateway-1",
            "voice-creation",
            "instance-voice-clone",
            1,
            "binding-voice-clone",
        ),
    )
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )
    with pytest.raises(ValueError):
        load_voice_clone_configuration(settings)


@pytest.mark.parametrize("other", ["design", "tts"])
def test_one_gateway_cannot_serve_two_resident_tts_variants(other: str, tmp_path: Path) -> None:
    clone_path, _ = write_voice_clone_configuration(tmp_path / "clone")
    path, _ = (
        write_voice_design_configuration(tmp_path / "design")
        if other == "design"
        else write_tts_configuration(tmp_path / "tts")
    )

    with pytest.raises(ValueError):
        settings_for(
            tmp_path,
            file_tts_config_file=path if other == "tts" else None,
            file_tts_object_access_key=SecretStr("synthetic-access") if other == "tts" else None,
            file_tts_object_secret_key=SecretStr("synthetic-secret") if other == "tts" else None,
            file_voice_design_config_file=path if other == "design" else None,
            file_voice_design_object_access_key=(
                SecretStr("synthetic-access") if other == "design" else None
            ),
            file_voice_design_object_secret_key=(
                SecretStr("synthetic-secret") if other == "design" else None
            ),
            file_voice_clone_config_file=clone_path,
            file_voice_clone_object_access_key=SecretStr("synthetic-access"),
            file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
        )


def test_partial_voice_clone_settings_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        settings_for(tmp_path, file_voice_clone_config_file=tmp_path / "unused.json")


def test_a_configured_clone_lane_loads_alone(tmp_path: Path) -> None:
    clone_path, _ = write_voice_clone_configuration(tmp_path / "clone")
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=clone_path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )
    configuration = load_file_speech_configuration(settings)
    assert configuration is not None
    assert configuration.voice_clone is not None
    assert configuration.voice_design is None and configuration.tts is None


async def test_readiness_requires_a_warmup_synthesis_not_a_health_answer(
    tmp_path: Path,
) -> None:
    settings, _ = clone_settings(tmp_path)
    app = create_app(settings=settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    checks = response.json()["checks"]
    assert any(check["name"] == "voice_clone_warmup" for check in checks)
    warmup = next(check for check in checks if check["name"] == "voice_clone_warmup")
    assert warmup["status"] == "failed"


def test_an_opted_in_x_vector_policy_is_a_startup_snapshot(tmp_path: Path) -> None:
    policy = VoiceClonePolicy("policy-voice-clone", allow_x_vector_only=True)
    path, _ = write_voice_clone_configuration(tmp_path, policy=policy)
    settings = settings_for(
        tmp_path,
        file_voice_clone_config_file=path,
        file_voice_clone_object_access_key=SecretStr("synthetic-access"),
        file_voice_clone_object_secret_key=SecretStr("synthetic-secret"),
    )

    configuration = load_voice_clone_configuration(settings)

    assert configuration is not None
    assert configuration.policy.allow_x_vector_only is True
