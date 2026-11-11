"""Composition-root selection between the text and audio Asset inspectors.

The default text inspector keeps ``asr_input`` unavailable with the domain error
code, and ``audio`` fails startup naming its own setting instead of falling back.
Audio decoding itself is qualified separately; these tests never run a worker.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import AnyHttpUrl, SecretStr

from speechnexus_backend import asset_runtime as asset_runtime_module
from speechnexus_backend.app import create_app
from speechnexus_backend.asset_runtime import AssetRuntime, build_asset_inspector
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
from speechnexus_backend.speech.infrastructure.audio_asset_inspector import (
    ProcessAudioAssetInspector,
)
from tests.unit.backend.test_health import _settings

BODY = b"RIFF-not-decoded-here"
POLICY = AssetPolicy(60)
COMMAND = "/opt/speechnexus-inspect/bin/inspect --receipt"
LAUNCHER = "/opt/speechnexus-inspect/bin/python3"


def _runtime_settings(**changes: Any) -> Settings:
    return Settings(
        **_settings().model_dump()
        | {
            "object_storage_access_key": SecretStr("test-key"),
            "object_storage_secret_key": SecretStr("test-secret"),
        }
        | changes
    )


def _audio_settings(root: Path, **changes: Any) -> Settings:
    tools = root.parent / "host-tools"
    tools.mkdir(exist_ok=True)
    for name in ("bwrap", "prlimit"):
        (tools / name).touch()
    return _runtime_settings(
        **{
            "asset_inspector": "audio",
            "audio_inspection_runtime_root": root,
            "audio_inspection_command": COMMAND,
            "audio_inspection_launcher_python": LAUNCHER,
            "audio_inspection_bubblewrap": tools / "bwrap",
            "audio_inspection_prlimit": tools / "prlimit",
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


@pytest.mark.parametrize(
    "field,value,variable",
    [
        (
            "audio_inspection_runtime_root",
            Path("/opt/speechnexus-inspect/image"),
            "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
        ),
        (
            "audio_inspection_command",
            COMMAND,
            "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND",
        ),
        (
            "audio_inspection_launcher_python",
            LAUNCHER,
            "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
        ),
        (
            "audio_inspection_bubblewrap",
            Path("/usr/bin/bwrap"),
            "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP",
        ),
        (
            "audio_inspection_prlimit",
            Path("/usr/bin/prlimit"),
            "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT",
        ),
    ],
)
def test_text_mode_rejects_a_configured_audio_prerequisite(
    field: str, value: Any, variable: str
) -> None:
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(_runtime_settings(**{field: value}))
    message = str(caught.value)
    assert variable in message
    assert "SPEECHNEXUS_ASSET_INSPECTOR=text" in message


def test_text_mode_rejects_an_environment_configured_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT", "/opt/speechnexus-inspect/image"
    )
    with pytest.raises(ValueError, match="SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"):
        build_asset_inspector(_settings())


def test_text_mode_names_every_configured_audio_prerequisite() -> None:
    settings = _runtime_settings(
        audio_inspection_runtime_root=Path("/opt/speechnexus-inspect/image"),
        audio_inspection_command=COMMAND,
        audio_inspection_launcher_python=LAUNCHER,
    )
    with pytest.raises(ValueError) as caught:
        build_asset_inspector(settings)
    message = str(caught.value)
    for variable in (
        "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
        "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND",
        "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
    ):
        assert variable in message


def test_text_mode_ignores_settings_that_always_carry_defaults() -> None:
    settings = _runtime_settings(
        audio_inspection_max_duration_ms=99,
        audio_inspection_max_input_bytes=1024,
        audio_inspection_max_receipt_bytes=256,
    )
    assert type(build_asset_inspector(settings)) is TextAssetInspector


def test_text_mode_without_audio_prerequisites_still_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "SPEECHNEXUS_ASSET_INSPECTOR",
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


@pytest.mark.parametrize(
    "field,variable",
    [
        ("audio_inspection_runtime_root", "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"),
        ("audio_inspection_command", "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"),
        ("audio_inspection_launcher_python", "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON"),
    ],
)
def test_audio_mode_without_a_selector_fails_startup_naming_it(
    tmp_path: Path, field: str, variable: str
) -> None:
    root = tmp_path / "image"
    root.mkdir()
    missing = "" if field != "audio_inspection_runtime_root" else None
    settings = _audio_settings(root, **{field: missing})
    with pytest.raises(ValueError, match=variable):
        AssetRuntime(settings)


def test_api_startup_rejects_a_missing_audio_runtime_root(tmp_path: Path) -> None:
    settings = _audio_settings(
        tmp_path / "image", audio_inspection_runtime_root=tmp_path / "absent"
    )
    with pytest.raises(ValueError, match="SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"):
        create_app(settings=settings)


@pytest.mark.parametrize(
    "field,value,variable",
    [
        ("audio_inspection_runtime_root", Path("/"), "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"),
        (
            "audio_inspection_runtime_root",
            Path("relative-image"),
            "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
        ),
        (
            "audio_inspection_launcher_python",
            "python3",
            "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
        ),
        (
            "audio_inspection_launcher_python",
            "../python3",
            "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
        ),
        ("audio_inspection_command", "inspect --receipt", "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"),
        (
            "audio_inspection_command",
            "/opt/inspect 'unterminated",
            "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND",
        ),
        ("audio_inspection_command", "# comment only", "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"),
        (
            "audio_inspection_bubblewrap",
            Path("/nonexistent/bwrap"),
            "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP",
        ),
        (
            "audio_inspection_prlimit",
            Path("/nonexistent/prlimit"),
            "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT",
        ),
    ],
)
def test_audio_mode_with_an_unusable_selector_fails_startup_naming_it(
    tmp_path: Path, field: str, value: Any, variable: str
) -> None:
    root = tmp_path / "image"
    root.mkdir()
    with pytest.raises(ValueError, match=variable):
        build_asset_inspector(_audio_settings(root, **{field: value}))


def test_audio_mode_with_absent_paths_names_them(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"):
        build_asset_inspector(_audio_settings(tmp_path / "absent"))
    root = tmp_path / "image"
    root.mkdir()
    settings = _audio_settings(root)
    bubblewrap = settings.audio_inspection_bubblewrap
    assert bubblewrap is not None
    bubblewrap.unlink()
    with pytest.raises(ValueError, match="SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP"):
        build_asset_inspector(settings)


def test_audio_mode_assembles_the_process_inspector_and_keeps_voice_reference_unavailable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "image"
    root.mkdir()
    inspector = build_asset_inspector(_audio_settings(root))
    assert type(inspector) is ProcessAudioAssetInspector
    assert inspector.inspect("text_input", io.BytesIO(b"plain text"), POLICY).facts is not None
    assert inspector.inspect("voice_reference", io.BytesIO(BODY), POLICY) == Inspection(
        available=False
    )


def test_configured_selectors_and_limits_reach_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[Any, ...]] = []

    class _Process:
        def __init__(self, root: Path, command: tuple[str, ...], limits: Any, **kwargs: Any):
            captured.append((root, command, limits, kwargs))

    class _Inspector:
        def __init__(self, process: Any, *, max_duration_ms: int, max_receipt_bytes: int = 4096):
            self.process = process
            self.max_duration_ms = max_duration_ms
            self.max_receipt_bytes = max_receipt_bytes

    monkeypatch.setattr(asset_runtime_module, "AudioInspectionProcess", _Process)
    monkeypatch.setattr(asset_runtime_module, "ProcessAudioAssetInspector", _Inspector)
    root = tmp_path / "image"
    root.mkdir()
    settings = _audio_settings(
        root,
        audio_inspection_max_duration_ms=99,
        audio_inspection_max_input_bytes=1024,
        audio_inspection_memory_bytes=64 * 1024**2,
        audio_inspection_cpu_seconds=2,
        audio_inspection_wall_seconds=1.5,
        audio_inspection_output_bytes=512,
        audio_inspection_max_receipt_bytes=256,
    )
    inspector = build_asset_inspector(settings)
    resolved, command, limits, kwargs = captured[0]
    assert resolved == root.resolve()
    assert command == (COMMAND.split()[0], "--receipt")
    assert kwargs["launcher_python"] == LAUNCHER
    assert kwargs["bubblewrap"] == root.parent / "host-tools" / "bwrap"
    assert kwargs["prlimit"] == root.parent / "host-tools" / "prlimit"
    assert (
        limits.input_bytes,
        limits.memory_bytes,
        limits.cpu_seconds,
        limits.wall_seconds,
        limits.output_bytes,
    ) == (1024, 64 * 1024**2, 2, 1.5, 512)
    assert isinstance(inspector, _Inspector)
    assert inspector.max_duration_ms == 99
    assert inspector.max_receipt_bytes == 256


def test_blank_forwarded_values_mean_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_ASSET_INSPECTOR", "")
    monkeypatch.setenv("SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT", "")
    monkeypatch.setenv("SPEECHNEXUS_AUDIO_INSPECTION_COMMAND", "")
    monkeypatch.setenv("SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON", "")
    monkeypatch.setenv("SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP", "")
    monkeypatch.setenv("SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT", "")
    settings = Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )
    assert settings.asset_inspector == "text"
    assert settings.audio_inspection_runtime_root is None
    assert settings.audio_inspection_bubblewrap is None
    assert settings.audio_inspection_prlimit is None
    assert settings.audio_inspection_command == ""
    assert settings.audio_inspection_launcher_python == ""
    assert type(build_asset_inspector(settings)) is TextAssetInspector
