"""Composition-root selection between the text and HTTP audio Asset inspectors.

The default text inspector keeps ``asr_input`` unavailable with the domain error
code; ``audio`` fails startup naming the setting that is missing, unusable or
self-contradictory instead of falling back to text. Protocol behaviour and the
public error mapping live in ``test_audio_inspection_http``; no inspector socket
is opened here.
"""

from __future__ import annotations

import io
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import AnyHttpUrl, SecretStr, ValidationError

from speechnexus_backend.app import create_app
from speechnexus_backend.asset_runtime import (
    AssetRuntime,
    build_asset_inspector,
)
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.assets import (
    AssetInspector,
    AssetService,
    TextAssetInspector,
)
from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetPolicy,
    AssetRecord,
    Inspection,
    UploadIntent,
    reserve_asset,
)
from speechnexus_backend.speech.infrastructure.audio_inspection_http import (
    HttpAudioAssetInspector,
)
from tests.unit.backend.test_health import _settings

BODY = b"RIFF-not-decoded-here"
POLICY = AssetPolicy(60)
URL = "http://asset-inspector.invalid:8080"
TOKEN = SecretStr("inspection-token-0001")

# Container-only settings retired from this process. Any non-empty value is an
# operator intent this process can no longer honour.
RETIRED = [
    (
        "audio_inspection_runtime_root",
        "/opt/speechnexus-inspect/image",
        "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
    ),
    ("audio_inspection_command", "/opt/inspect --receipt", "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"),
    (
        "audio_inspection_launcher_python",
        "/opt/inspect/bin/python3",
        "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
    ),
    ("audio_inspection_bubblewrap", "/usr/bin/bwrap", "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP"),
    ("audio_inspection_prlimit", "/usr/bin/prlimit", "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT"),
]


def _runtime_settings(**changes: Any) -> Settings:
    return Settings(
        **_settings().model_dump()
        | {
            "object_storage_access_key": SecretStr("test-key"),
            "object_storage_secret_key": SecretStr("test-secret"),
        }
        | changes
    )


def _audio_settings(**changes: Any) -> Settings:
    return _runtime_settings(
        **{
            "asset_inspector": "audio",
            "asset_inspection_url": URL,
            "asset_inspection_token": TOKEN,
        }
        | changes
    )


def _service(inspector: AssetInspector) -> tuple[AssetService, Mock, Mock]:
    metadata = Mock()
    objects = Mock()

    def reserve(
        intent: UploadIntent,
        *,
        asset_id: str,
        object_key: str,
        inspection: Inspection,
        policy: AssetPolicy,
    ) -> AssetRecord:
        return reserve_asset(
            intent,
            inspection,
            policy,
            asset_id=asset_id,
            object_key=object_key,
            now_us=0,
        )

    metadata.reserve.side_effect = reserve
    return AssetService(metadata, objects, inspector, POLICY), metadata, objects


def test_default_configuration_installs_the_text_inspector_and_rejects_asr_input() -> None:
    settings = _settings()
    assert settings.asset_inspector == "text"
    runtime = AssetRuntime(_runtime_settings())
    try:
        assert type(runtime.inspector) is TextAssetInspector
        text = runtime.inspector.inspect("text_input", io.BytesIO("合成文本".encode()), POLICY)
        assert text.available and text.facts is not None
        assert text.facts.media_type == "text/plain; charset=utf-8"
        assert runtime.inspector.inspect("asr_input", io.BytesIO(BODY), POLICY) == Inspection(
            available=False
        )
        service, metadata, objects = _service(runtime.inspector)
        with pytest.raises(AssetError) as caught:
            service.upload(io.BytesIO(BODY), workspace_id="workspace-default", purpose="asr_input")
    finally:
        runtime.close()
    assert caught.value.code == "asset_inspection_unavailable"
    metadata.reserve.assert_called_once()
    objects.check_writes_available.assert_not_called()


@pytest.mark.parametrize("field,value,variable", RETIRED)
def test_text_mode_rejects_a_retired_local_prerequisite(
    field: str, value: str, variable: str
) -> None:
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(_runtime_settings(**{field: value}))
    message = str(caught.value)
    assert variable in message
    # The message must say where the capability went, not just that it is wrong.
    assert "asset-inspector deployment unit" in message
    assert "SPEECHNEXUS_ASSET_INSPECTION_URL" in message
    assert "SPEECHNEXUS_ASSET_INSPECTION_TOKEN" in message


@pytest.mark.parametrize("field,value,variable", RETIRED)
def test_audio_mode_rejects_a_retired_local_prerequisite(
    field: str, value: str, variable: str
) -> None:
    with pytest.raises(ValueError, match=variable):
        build_asset_inspector(_audio_settings(**{field: value}))


def test_text_mode_rejects_an_environment_configured_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT", "/opt/speechnexus-inspect/image"
    )
    with pytest.raises(ValueError, match="SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"):
        build_asset_inspector(_settings())


@pytest.mark.parametrize(
    "field,value,variable",
    [
        ("asset_inspection_url", URL, "SPEECHNEXUS_ASSET_INSPECTION_URL"),
        ("asset_inspection_token", TOKEN, "SPEECHNEXUS_ASSET_INSPECTION_TOKEN"),
    ],
)
def test_text_mode_rejects_a_configured_inspection_endpoint(
    field: str, value: Any, variable: str
) -> None:
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(_runtime_settings(**{field: value}))
    message = str(caught.value)
    assert variable in message
    assert "SPEECHNEXUS_ASSET_INSPECTOR=text" in message


def test_text_mode_names_every_configured_inspection_setting() -> None:
    settings = _runtime_settings(asset_inspection_url=URL, asset_inspection_token=TOKEN)
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(settings)
    message = str(caught.value)
    assert "SPEECHNEXUS_ASSET_INSPECTION_URL" in message
    assert "SPEECHNEXUS_ASSET_INSPECTION_TOKEN" in message


def test_text_mode_ignores_settings_that_always_carry_defaults() -> None:
    settings = _runtime_settings(
        audio_inspection_max_duration_ms=99,
        audio_inspection_max_input_bytes=1024,
        audio_inspection_max_receipt_bytes=256,
    )
    assert type(build_asset_inspector(settings)) is TextAssetInspector


def test_text_mode_without_inspection_settings_still_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "SPEECHNEXUS_ASSET_INSPECTOR",
        "SPEECHNEXUS_ASSET_INSPECTION_URL",
        "SPEECHNEXUS_ASSET_INSPECTION_TOKEN",
        "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
        "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND",
        "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
        "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP",
        "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT",
    ):
        monkeypatch.delenv(variable, raising=False)
    settings = _settings()
    assert settings.asset_inspector == "text"
    assert type(build_asset_inspector(settings)) is TextAssetInspector


def test_audio_mode_requires_the_inspector_url() -> None:
    with pytest.raises(ValueError, match="SPEECHNEXUS_ASSET_INSPECTION_URL"):
        build_asset_inspector(_audio_settings(asset_inspection_url=None))


def test_audio_mode_requires_the_inspector_token() -> None:
    with pytest.raises(ValueError, match="SPEECHNEXUS_ASSET_INSPECTION_TOKEN"):
        build_asset_inspector(_audio_settings(asset_inspection_token=None))


@pytest.mark.parametrize("token", ["", "   ", "\t\n"])
def test_audio_mode_treats_a_blank_token_as_unconfigured(token: str) -> None:
    """An empty or whitespace-only token is absence, not an unauthenticated caller.

    Compose forwards ``SPEECHNEXUS_ASSET_INSPECTION_TOKEN=""`` for a key it does
    not set, so a blank value must stop startup naming the variable instead of
    starting and sending ``Authorization: Bearer `` — answered 401 by the
    inspector and reported as a runtime ``503 asset_inspection_unavailable``.
    """
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(_audio_settings(asset_inspection_token=token))
    message = str(caught.value)
    assert "SPEECHNEXUS_ASSET_INSPECTION_TOKEN" in message
    assert "SPEECHNEXUS_ASSET_INSPECTION_URL" not in message


def test_audio_mode_rejects_a_blank_token_without_settings_normalization() -> None:
    """The composition root keeps its own blank check.

    ``Settings`` maps a blank token to ``None`` through one validator shared with
    unrelated optional settings. The constructor promise "audio fails startup
    naming the variable" must not depend on that normalization: narrowing it
    would let a whitespace-only token start the API.
    """
    settings = Settings.model_construct(
        **(_audio_settings().model_dump() | {"asset_inspection_token": SecretStr("   ")})
    )
    with pytest.raises(ValueError, match="SPEECHNEXUS_ASSET_INSPECTION_TOKEN"):
        build_asset_inspector(settings)


def test_audio_mode_rejects_an_empty_url_as_unconfigured() -> None:
    """A blank URL is absence too, and the message names the variable."""
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(_audio_settings(asset_inspection_url=""))
    message = str(caught.value)
    assert "SPEECHNEXUS_ASSET_INSPECTION_URL" in message
    assert "SPEECHNEXUS_ASSET_INSPECTION_TOKEN" not in message


def test_audio_mode_requires_a_url_and_a_token_together() -> None:
    """One half of the pair never silently configures an unauthenticated caller."""
    with pytest.raises(ValueError, match="SPEECHNEXUS_ASSET_INSPECTION_TOKEN"):
        build_asset_inspector(_runtime_settings(asset_inspector="audio", asset_inspection_url=URL))
    with pytest.raises(ValueError, match="SPEECHNEXUS_ASSET_INSPECTION_URL"):
        build_asset_inspector(
            _runtime_settings(asset_inspector="audio", asset_inspection_token=TOKEN)
        )


def test_a_trailing_slash_origin_is_accepted() -> None:
    inspector = cast(
        HttpAudioAssetInspector,
        build_asset_inspector(_audio_settings(asset_inspection_url=URL + "/")),
    )
    try:
        assert type(inspector) is HttpAudioAssetInspector
    finally:
        inspector.close()


@pytest.mark.parametrize(
    "url",
    [URL + "/internal/v1/inspection/audio", URL + "?token=leak", URL + "#fragment"],
)
def test_audio_mode_rejects_a_url_with_a_path_query_or_fragment(url: str) -> None:
    with pytest.raises(ValueError, match="SPEECHNEXUS_ASSET_INSPECTION_URL"):
        build_asset_inspector(_audio_settings(asset_inspection_url=url))


def test_a_url_with_embedded_credentials_is_rejected_by_settings() -> None:
    with pytest.raises(ValidationError) as caught:
        _audio_settings(asset_inspection_url="http://user:secret@inspector.invalid:8080")
    assert "SPEECHNEXUS_ASSET_INSPECTION_URL" in str(caught.value)
    assert "SPEECHNEXUS_ASSET_INSPECTION_TOKEN" in str(caught.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"asset_inspection_timeout_seconds": 11.9},
        {"audio_inspection_wall_seconds": 13.5},
    ],
)
def test_the_client_budget_must_outlive_the_decode_budget(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(_audio_settings(**changes))
    message = str(caught.value)
    assert "SPEECHNEXUS_ASSET_INSPECTION_TIMEOUT_SECONDS" in message
    assert "SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS" in message


def test_audio_mode_assembles_the_http_inspector_and_carries_the_reference_ceiling() -> None:
    runtime = AssetRuntime(_audio_settings(audio_inspection_max_reference_duration_ms=30_000))
    try:
        assert type(runtime.inspector) is HttpAudioAssetInspector
        assert (
            runtime.inspector.inspect("text_input", io.BytesIO(b"plain text"), POLICY).facts
            is not None
        )
        # The reference ceiling is its own setting, not the ASR decode budget:
        # the composition root hands it to the service policy the inspector
        # revalidates against. Purpose routing itself is asserted in
        # `test_audio_inspection_http`, which owns a transport for it.
        assert runtime.service.policy.max_reference_duration_ms == 30_000
    finally:
        runtime.close()


def test_configured_policy_reaches_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speechnexus_backend import asset_runtime as asset_runtime_module

    captured: list[tuple[Any, ...]] = []

    class _Inspector:
        def __init__(self, url: str, token: SecretStr, **limits: Any) -> None:
            captured.append((url, token, limits))

        def close(self) -> bool:
            return True

    monkeypatch.setattr(asset_runtime_module, "HttpAudioAssetInspector", _Inspector)
    settings = _audio_settings(
        audio_inspection_max_duration_ms=99,
        audio_inspection_max_input_bytes=1024,
        audio_inspection_wall_seconds=1.5,
        audio_inspection_max_receipt_bytes=256,
        asset_inspection_max_response_bytes=128,
        asset_inspection_timeout_seconds=4,
    )
    build_asset_inspector(settings)
    url, token, limits = captured[0]
    assert url == URL + "/"
    assert token.get_secret_value() == TOKEN.get_secret_value()
    assert limits["wall_seconds"] == 1.5
    assert limits["timeout_seconds"] == 4
    assert limits["max_duration_ms"] == 99
    assert limits["max_input_bytes"] == 1024
    # The receipt bound is the smaller of the inspector cap and this read bound.
    assert limits["max_receipt_bytes"] == 128


@pytest.mark.parametrize(
    "field,value,variable,target",
    [
        (
            "audio_inspection_memory_bytes",
            512 * 1024**2,
            "SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES",
            "SPEECHNEXUS_INSPECTION_MEMORY_BYTES",
        ),
        (
            "audio_inspection_cpu_seconds",
            2,
            "SPEECHNEXUS_AUDIO_INSPECTION_CPU_SECONDS",
            "SPEECHNEXUS_INSPECTION_CPU_SECONDS",
        ),
        (
            "audio_inspection_output_bytes",
            512,
            "SPEECHNEXUS_AUDIO_INSPECTION_OUTPUT_BYTES",
            "SPEECHNEXUS_INSPECTION_OUTPUT_BYTES",
        ),
    ],
)
def test_a_migrated_resource_limit_fails_startup_naming_both_keys(
    field: str, value: int, variable: str, target: str
) -> None:
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(_audio_settings(**{field: value}))
    message = str(caught.value)
    assert variable in message
    assert target in message


def test_the_compose_forwarded_resource_defaults_stay_inert() -> None:
    """Compose always forwards these values; only a change is an operator intent."""
    settings = _audio_settings(
        audio_inspection_memory_bytes=256 * 1024**2,
        audio_inspection_cpu_seconds=5,
        audio_inspection_output_bytes=4096,
    )
    inspector = cast(HttpAudioAssetInspector, build_asset_inspector(settings))
    try:
        assert type(inspector) is HttpAudioAssetInspector
    finally:
        inspector.close()


def test_api_startup_rejects_a_missing_inspector_url() -> None:
    with pytest.raises(ValueError, match="SPEECHNEXUS_ASSET_INSPECTION_URL"):
        create_app(settings=_audio_settings(asset_inspection_url=None))


def test_blank_forwarded_values_mean_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "SPEECHNEXUS_ASSET_INSPECTOR",
        "SPEECHNEXUS_ASSET_INSPECTION_URL",
        "SPEECHNEXUS_ASSET_INSPECTION_TOKEN",
        "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
        "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND",
        "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
        "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP",
        "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT",
    ):
        monkeypatch.setenv(variable, "")
    settings = Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )
    assert settings.asset_inspector == "text"
    assert settings.asset_inspection_url is None
    assert settings.asset_inspection_token is None
    assert settings.audio_inspection_runtime_root is None
    assert settings.audio_inspection_bubblewrap is None
    assert settings.audio_inspection_prlimit is None
    assert settings.audio_inspection_command == ""
    assert settings.audio_inspection_launcher_python == ""
    assert type(build_asset_inspector(settings)) is TextAssetInspector
