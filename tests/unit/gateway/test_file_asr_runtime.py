"""Default composition, explicit bounds and lifecycle; no real model or DB."""

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.file_asr_runtime import load_file_asr_configuration
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from speechnexus_gateway.settings import Settings
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.fake.file_asr_configuration import write_configuration
from tests.fake.vad import ScriptedVadModel
from tests.unit.gateway.test_barrier_http import settings_for


def configured(tmp_path: Path, path: Path) -> Settings:
    return settings_for(
        tmp_path,
        file_asr_config_file=path,
        file_asr_object_access_key=SecretStr("synthetic-access"),
        file_asr_object_secret_key=SecretStr("synthetic-secret"),
    )


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "protocol",
        "missing",
        "implicit-vad",
        "extra-vad",
        "implicit-input",
        "implicit-decode",
        "implicit-worker",
        "bool-count",
        "pending-bound",
        "shutdown-bound",
        "result-bound",
        "result-too-large",
        "command-bound",
        "tokens-bound",
        "call-bound",
        "decode-bound",
        "input-bound",
        "model-sha",
        "model-path",
        "spool-mode",
        "public-storage",
        "bad-bucket",
        "endpoint-node",
        "endpoint-gateway",
        "endpoint-generation",
        "echo-type",
        "duplicate",
        "nonfinite",
    ],
)
def test_invalid_deployment_refuses_before_authority_or_model_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path, document = write_configuration(tmp_path / "deployment")
    if change == "unknown":
        document["unexpected"] = "synthetic-secret"
    elif change == "protocol":
        document["protocol"] = "future-v2"
    elif change == "missing":
        del document["max_call_seconds"]
    elif change.startswith("implicit-"):
        group = {
            "implicit-vad": "vad_tuning",
            "implicit-input": "input_limits",
            "implicit-decode": "decode_limits",
            "implicit-worker": "worker_limits",
        }[change]
        document[group].pop(next(iter(document[group])))
    elif change == "extra-vad":
        document["vad_tuning"]["vendor_default"] = 1
    elif change == "bool-count":
        document["worker_limits"]["max_pending"] = True
    elif change.endswith("-bound"):
        group, key, value = {
            "pending-bound": ("worker_limits", "max_pending", 65),
            "shutdown-bound": ("worker_limits", "shutdown_seconds", 31),
            "result-bound": ("worker_limits", "max_result_bytes", 0),
            "command-bound": ("worker_limits", "max_command_bytes", 0),
            "tokens-bound": ("", "max_completion_tokens", 4097),
            "call-bound": ("", "max_call_seconds", 301),
            "decode-bound": ("decode_limits", "max_frame_samples", 0),
            "input-bound": ("input_limits", "memory_bytes", 1000001),
        }[change]
        (document[group] if group else document)[key] = value
    elif change == "result-too-large":
        document["worker_limits"]["max_result_bytes"] = 4 * 1024 * 1024 + 1
    elif change == "model-sha":
        document["vad_model_sha256"] = "not-a-digest"
    elif change == "model-path":
        document["vad_model_file"] = "relative-model.onnx"
    elif change == "spool-mode":
        Path(document["objects"]["spool_directory"]).chmod(0o755)
    elif change == "public-storage":
        document["objects"]["endpoint"] = "https://8.8.8.8"
    elif change == "bad-bucket":
        document["objects"]["input_bucket"] = "INVALID"
    elif change.startswith("endpoint-"):
        endpoint = Path(document["controlled_endpoint_file"])
        value = json.loads(endpoint.read_text())
        key, replacement = {
            "endpoint-node": ("host_node_id", "node-other"),
            "endpoint-gateway": ("gateway_id", "gateway-other"),
            "endpoint-generation": ("instance_generation", True),
        }[change]
        value["target"][key] = replacement
        endpoint.write_text(json.dumps(value))
    elif change == "echo-type":
        document["require_correlation_echo"] = 1
    if change == "nonfinite":
        document["max_call_seconds"] = float("nan")
    payload = json.dumps(document)
    if change == "duplicate":
        payload = payload[:-1] + ', "max_call_seconds": 1}'
    path.write_text(payload)

    def forbidden(*_: Any, **__: Any) -> Any:
        pytest.fail("invalid deployment must not acquire resources or activate a model")

    monkeypatch.setattr("speechnexus_gateway.app._build_control", forbidden)
    monkeypatch.setattr("speechnexus_gateway.file_asr_runtime.SileroOnnxCpuModel", forbidden)
    monkeypatch.setattr("speechnexus_gateway.file_asr_runtime.PyAvAudioDecoder", forbidden)
    with pytest.raises(ValueError, match="invalid gateway file ASR configuration") as error:
        create_app(settings=configured(tmp_path, path))
    assert "synthetic-secret" not in str(error.value) and str(tmp_path) not in str(error.value)


@pytest.mark.parametrize("kind", ["fifo", "symlink", "directory", "empty", "oversize", "readable"])
def test_configuration_file_is_private_regular_bounded_and_nonblocking(
    tmp_path: Path, kind: str
) -> None:
    path, _ = write_configuration(tmp_path / "deployment")
    if kind == "readable":
        path.chmod(0o644)
    elif kind == "oversize":
        path.write_bytes(b" " * 65537)
    elif kind == "empty":
        path.write_bytes(b"")
    else:
        saved = path.with_suffix(".saved")
        path.rename(saved)
        if kind == "fifo":
            os.mkfifo(path, 0o600)
        elif kind == "symlink":
            path.symlink_to(saved)
        else:
            path.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="invalid gateway file ASR configuration"):
        create_app(settings=configured(tmp_path, path))


@pytest.mark.parametrize(
    "fields",
    [
        {"file_asr_config_file": Path("/private/unused")},
        {"file_asr_object_access_key": SecretStr("synthetic-access")},
        {"file_asr_object_secret_key": SecretStr("synthetic-secret")},
        {
            "file_asr_config_file": Path("relative.json"),
            "file_asr_object_access_key": SecretStr("synthetic-access"),
            "file_asr_object_secret_key": SecretStr("synthetic-secret"),
        },
    ],
)
def test_settings_require_complete_explicit_deployment(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        settings_for(tmp_path, **fields)


def test_configuration_cannot_override_injected_services_or_factory(tmp_path: Path) -> None:
    path, _ = write_configuration(tmp_path / "deployment")
    settings = configured(tmp_path, path)
    with pytest.raises(ValueError, match="mixed"):
        create_app(settings=settings, file_execution_factory=lambda *_: None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="default owned"):
        create_app(settings=settings, instance_drain=SimpleNamespace())  # type: ignore[arg-type]


def test_absent_configuration_preserves_unavailable_default(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    assert load_file_asr_configuration(settings) is None
    app = create_app(settings=settings)
    assert not app.state.file_execution_required
    assert app.state.submit_execution is app.state.abort_execution is None


@pytest.mark.parametrize(("configured_value", "expected"), [(None, True), (False, False)])
def test_correlation_echo_requirement_defaults_to_strict_and_is_explicitly_configurable(
    tmp_path: Path, configured_value: bool | None, expected: bool
) -> None:
    path, document = write_configuration(tmp_path / "deployment")
    if configured_value is not None:
        document["require_correlation_echo"] = configured_value
    path.write_text(json.dumps(document))
    loaded = load_file_asr_configuration(configured(tmp_path, path))
    assert loaded is not None and loaded.endpoint.require_correlation_echo is expected


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("fails", [False, True])
async def test_default_factory_model_activation_joins_before_releasing_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool, fails: bool
) -> None:
    path, _ = write_configuration(tmp_path / "deployment")
    closed: list[str] = []
    entered, release = threading.Event(), threading.Event()
    ingress, ledger = fake_ingress(), InMemorySubmissionLedger()

    def build(_: Settings) -> tuple[Any, ...]:
        return (
            GatewayBarrierService(ingress=ingress, ledger=ledger),
            ExecutionQueryService(ingress=ingress, ledger=ledger),
            InstanceDrainService(ingress=ingress, ledger=ledger),
            SimpleNamespace(close=lambda: closed.append("ownership")),
            SimpleNamespace(close=lambda: closed.append("pool")),
            None,
            ingress,
            ledger,
        )

    def model(**_: Any) -> ScriptedVadModel:
        entered.set()
        assert release.wait(3)
        if fails:
            raise ValueError("synthetic model activation failed")
        return ScriptedVadModel()

    monkeypatch.setattr("speechnexus_gateway.app._build_control", build)
    monkeypatch.setattr("speechnexus_gateway.file_asr_runtime.SileroOnnxCpuModel", model)
    app = create_app(settings=configured(tmp_path, path))
    # The loaded configuration is a snapshot; startup cannot read new policy defaults.
    path.write_text("invalid replacement after preflight")
    lifespan = app.router.lifespan_context(app)
    startup = asyncio.create_task(lifespan.__aenter__())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert app.state.file_execution_required and not closed
        if cancelled:
            startup.cancel()
            await asyncio.sleep(0.01)
            startup.cancel()
            assert not startup.done() and not closed
        release.set()
        if fails or cancelled:
            with pytest.raises(ValueError if fails else asyncio.CancelledError):
                await startup
        else:
            await startup
            runtime = app.state.file_execution_runtime
            assert isinstance(runtime, FileExecutionWorker) and runtime.running
            assert app.state.submit_execution is not None and app.state.abort_execution is not None
            await lifespan.__aexit__(None, None, None)
            assert not runtime.running
        assert closed == ["pool", "ownership"]
        assert app.state.file_execution_runtime is app.state.submit_execution is None
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
