"""Default file-TTS composition, explicit bounds and lifecycle; no model or network."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from speechnexus_gateway.app import create_app
from speechnexus_gateway.file_speech_runtime import load_file_speech_configuration
from speechnexus_gateway.file_tts_runtime import load_file_tts_configuration
from speechnexus_gateway.settings import Settings
from tests.fake.file_asr_configuration import write_configuration
from tests.fake.file_tts_configuration import write_tts_configuration
from tests.unit.gateway.test_barrier_http import settings_for


def configured(tmp_path: Path, path: Path) -> Settings:
    return settings_for(
        tmp_path,
        file_tts_config_file=path,
        file_tts_object_access_key=SecretStr("synthetic-access"),
        file_tts_object_secret_key=SecretStr("synthetic-secret"),
    )


def test_valid_deployment_is_one_explicit_snapshot(tmp_path: Path) -> None:
    path, _ = write_tts_configuration(tmp_path / "deployment")
    configuration = load_file_tts_configuration(configured(tmp_path, path))

    assert configuration is not None
    assert configuration.policy.config_revision == "policy-tts"
    assert configuration.policy.sample_rate_hz == 24000
    assert configuration.worker.max_result_bytes == 1024 * 1024
    target = configuration.endpoint.execution_target
    assert target is not None
    assert target.pool_id == "tts-batch"
    assert target.binding_revision_id == "binding-tts"
    assert create_app(settings=configured(tmp_path, path)).state is not None


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "protocol",
        "missing",
        "implicit-policy",
        "extra-policy",
        "payload-bound",
        "pending-bound",
        "sample-rate",
        "task",
        "engine-format",
        "endpoint-node",
        "endpoint-pool",
        "public-storage",
        "bad-bucket",
        "endpoint-mode",
        "duplicate",
        "nonfinite",
    ],
)
def test_invalid_deployment_refuses_before_authority_or_network(
    tmp_path: Path, change: str
) -> None:
    path, document = write_tts_configuration(tmp_path / "deployment")
    if change == "unknown":
        document["unexpected"] = "synthetic-secret"
    elif change == "protocol":
        document["protocol"] = "future-v2"
    elif change == "missing":
        del document["worker_limits"]
    elif change == "implicit-policy":
        document["tts_policy"].pop("voice")
    elif change == "extra-policy":
        document["tts_policy"]["vendor_default"] = 1
    elif change == "payload-bound":
        document["worker_limits"]["max_result_bytes"] = 1023
    elif change == "pending-bound":
        document["worker_limits"]["max_pending"] = 65
    elif change == "sample-rate":
        document["tts_policy"]["sample_rate_hz"] = 8000
    elif change == "task":
        document["tts_policy"]["task"] = "Base"
    elif change == "engine-format":
        document["tts_policy"]["engine_format"] = "mp3"
    elif change.startswith("endpoint-") and change != "endpoint-mode":
        endpoint = Path(document["controlled_endpoint_file"])
        value = json.loads(endpoint.read_text())
        key, replacement = {
            "endpoint-node": ("host_node_id", "node-other"),
            "endpoint-pool": ("pool_id", "asr-batch"),
        }[change]
        value["target"][key] = replacement
        endpoint.write_text(json.dumps(value))
    elif change == "endpoint-mode":
        Path(document["controlled_endpoint_file"]).chmod(0o644)
    elif change == "public-storage":
        document["objects"]["endpoint"] = "https://8.8.8.8"
    elif change == "bad-bucket":
        document["objects"]["result_bucket"] = "INVALID"
    elif change == "nonfinite":
        document["tts_policy"]["speed"] = float("nan")
    payload = json.dumps(document)
    if change == "duplicate":
        payload = payload[:-1] + ', "protocol": "file_tts_v1"}'
    path.write_text(payload)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid gateway file TTS configuration"):
        load_file_tts_configuration(configured(tmp_path, path))


@pytest.mark.parametrize(
    "override",
    [
        {
            "file_tts_config_file": Path("/private/unused"),
            "file_tts_object_access_key": SecretStr("synthetic-access"),
            "file_tts_object_secret_key": SecretStr("synthetic-secret"),
        },
        {
            "file_tts_config_file": Path("relative.json"),
            "file_tts_object_access_key": SecretStr("synthetic-access"),
            "file_tts_object_secret_key": SecretStr("synthetic-secret"),
        },
    ],
)
def test_settings_require_complete_owned_configuration(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
            component_token=SecretStr("synthetic-token"),
            ingress_lock_directory=tmp_path,
            **override,
        )


def test_configured_deployment_cannot_be_mixed_with_an_injected_factory(
    tmp_path: Path,
) -> None:
    path, _ = write_tts_configuration(tmp_path / "deployment")
    settings = configured(tmp_path, path)
    with pytest.raises(ValueError, match="cannot be mixed"):
        create_app(
            settings=settings,
            file_execution_factory=lambda *_: None,  # type: ignore[arg-type]
        )


def test_absent_configuration_does_not_create_a_lane(tmp_path: Path) -> None:
    assert load_file_tts_configuration(settings_for(tmp_path)) is None


def test_asr_and_tts_share_one_persisted_file_worker(tmp_path: Path) -> None:
    asr_path, _ = write_configuration(tmp_path / "asr")
    tts_path, tts_document = write_tts_configuration(tmp_path / "tts")
    settings = settings_for(
        tmp_path,
        file_asr_config_file=asr_path,
        file_asr_object_access_key=SecretStr("synthetic-access"),
        file_asr_object_secret_key=SecretStr("synthetic-secret"),
        file_tts_config_file=tts_path,
        file_tts_object_access_key=SecretStr("synthetic-access"),
        file_tts_object_secret_key=SecretStr("synthetic-secret"),
    )
    configuration = load_file_speech_configuration(settings)
    assert configuration is not None
    assert configuration.asr is not None and configuration.tts is not None
    assert configuration.limits.max_command_bytes == 16384

    tts_document["worker_limits"]["max_pending"] = 2
    tts_path.write_text(json.dumps(tts_document))
    with pytest.raises(ValueError, match="share command, pending and shutdown limits"):
        load_file_speech_configuration(settings)
