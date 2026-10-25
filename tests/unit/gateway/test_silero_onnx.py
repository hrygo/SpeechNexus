"""CPU-only, pinned model and per-session recurrent tensors against an injected runtime."""

import hashlib
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from speechnexus_gateway.domain.vad import VadError
from speechnexus_gateway.infrastructure.silero_onnx import SileroOnnxCpuModel

MODEL = b"synthetic ONNX placeholder"
DIGEST = hashlib.sha256(MODEL).hexdigest()
FRAME = np.arange(512, dtype="<i2").tobytes()


class FakeInferenceSession:
    def __init__(self, document: bytes, *, sess_options: Any, providers: list[str]) -> None:
        assert document == MODEL
        assert providers == ["CPUExecutionProvider"]
        assert sess_options.intra_op_num_threads == sess_options.inter_op_num_threads == 1
        self.feeds: list[dict[str, Any]] = []
        self.providers = providers
        self.bad_interface = False
        self.bad_result: str | None = None

    def get_providers(self) -> list[str]:
        return self.providers

    def get_inputs(self) -> list[Any]:
        names = ["wrong", "state", "sr"] if self.bad_interface else ["input", "state", "sr"]
        return [
            SimpleNamespace(name=name, type=kind)
            for name, kind in zip(
                names, ["tensor(float)", "tensor(float)", "tensor(int64)"], strict=True
            )
        ]

    def get_outputs(self) -> list[Any]:
        return [
            SimpleNamespace(name="output", type="tensor(float)"),
            SimpleNamespace(name="stateN", type="tensor(float)"),
        ]

    def run(self, names: list[str], feed: dict[str, Any]) -> list[Any]:
        assert names == ["output", "stateN"]
        self.feeds.append({name: value.copy() for name, value in feed.items()})
        output = np.array([[float(feed["state"][0, 0, 0]) / 10 + 0.1]], dtype="float32")
        state = feed["state"] + 1
        if self.bad_result == "shape":
            state = np.zeros((1, 1, 128), dtype="float32")
        if self.bad_result == "nan":
            state[0, 0, 0] = np.nan
        if self.bad_result == "probability":
            output[0, 0] = 1.1
        if self.bad_result == "dtype":
            state = state.astype("int32")
        return [output, state]


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> list[FakeInferenceSession]:
    sessions: list[FakeInferenceSession] = []

    def create(document: bytes, **kwargs: Any) -> FakeInferenceSession:
        result = FakeInferenceSession(document, **kwargs)
        sessions.append(result)
        return result

    real_import = importlib.import_module

    def load(name: str) -> Any:
        if name == "onnxruntime":
            return SimpleNamespace(SessionOptions=SimpleNamespace, InferenceSession=create)
        return real_import(name)

    monkeypatch.setattr(importlib, "import_module", load)
    return sessions


def model_at(tmp_path: Path) -> SileroOnnxCpuModel:
    path = tmp_path / "synthetic.onnx"
    path.write_bytes(MODEL)
    return SileroOnnxCpuModel(model_path=path, expected_sha256=DIGEST)


def test_model_pinning_happens_before_loading_vendor_runtime(
    tmp_path: Path, runtime: list[FakeInferenceSession]
) -> None:
    path = tmp_path / "synthetic.onnx"
    path.write_bytes(MODEL)
    with pytest.raises(VadError, match="digest mismatch"):
        SileroOnnxCpuModel(model_path=path, expected_sha256="0" * 64)
    assert not runtime
    model = model_at(tmp_path)
    assert model.model_sha256 == DIGEST and len(runtime) == 1


def test_recurrent_state_and_context_are_private_even_though_inference_session_is_shared(
    tmp_path: Path, runtime: list[FakeInferenceSession]
) -> None:
    model = model_at(tmp_path)
    first, second = model.new_detector(), model.new_detector()
    assert first.probability(FRAME) == pytest.approx(0.1)
    assert first.probability(FRAME) == pytest.approx(0.2)
    assert second.probability(FRAME) == pytest.approx(0.1)
    first.reset()
    assert first.probability(FRAME) == pytest.approx(0.1)
    assert second.probability(FRAME) == pytest.approx(0.2)
    feeds = runtime[0].feeds
    assert feeds[0]["input"].shape == (1, 576)
    assert feeds[0]["sr"].shape == () and feeds[0]["sr"].item() == 16000
    assert np.count_nonzero(feeds[0]["input"][:, :64]) == 0
    np.testing.assert_array_equal(feeds[1]["input"][:, :64], feeds[0]["input"][:, -64:])
    assert np.count_nonzero(feeds[2]["state"]) == 0
    assert np.count_nonzero(feeds[3]["state"]) == 0
    assert np.count_nonzero(feeds[3]["input"][:, :64]) == 0
    assert feeds[4]["state"][0, 0, 0] == 1


def test_little_endian_int16_full_range_is_normalized_without_clipping(
    tmp_path: Path, runtime: list[FakeInferenceSession]
) -> None:
    model = model_at(tmp_path)
    frame = b"\x00\x80\xff\x7f" + b"\x00" * 1020
    model.new_detector().probability(frame)
    audio = runtime[0].feeds[0]["input"][0, 64:]
    assert audio[0] == -1.0 and audio[1] == 32767 / 32768


@pytest.mark.parametrize("kind", ["shape", "nan", "probability", "dtype"])
def test_bad_vendor_result_is_rejected(
    tmp_path: Path, runtime: list[FakeInferenceSession], kind: str
) -> None:
    model = model_at(tmp_path)
    runtime[0].bad_result = kind
    with pytest.raises(VadError, match="inference result"):
        model.new_detector().probability(FRAME)


@pytest.mark.parametrize("frame", [b"", b"\x00" * 1022, b"\x00" * 1026, bytearray(FRAME)])
def test_adapter_rejects_wrong_frame_before_inference(
    tmp_path: Path, runtime: list[FakeInferenceSession], frame: Any
) -> None:
    model = model_at(tmp_path)
    with pytest.raises(VadError):
        model.new_detector().probability(frame)
    assert not runtime[0].feeds


def test_missing_dependencies_and_missing_models_have_sanitized_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private" / "synthetic.onnx"
    with pytest.raises(VadError) as missing:
        SileroOnnxCpuModel(model_path=path, expected_sha256=DIGEST)
    assert "private" not in str(missing.value)
    path.parent.mkdir()
    path.write_bytes(MODEL)

    def unavailable(name: str) -> Any:
        raise ImportError("private runtime details")

    monkeypatch.setattr(importlib, "import_module", unavailable)
    with pytest.raises(VadError) as dependency:
        SileroOnnxCpuModel(model_path=path, expected_sha256=DIGEST)
    assert "private" not in str(dependency.value)


@pytest.mark.parametrize("kind", ["provider", "interface"])
def test_unsupported_runtime_is_rejected_before_any_inference(
    tmp_path: Path, runtime: list[FakeInferenceSession], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "provider":
        monkeypatch.setattr(
            FakeInferenceSession, "get_providers", lambda self: ["CUDAExecutionProvider"]
        )
    else:
        monkeypatch.setattr(FakeInferenceSession, "get_inputs", lambda self: [])
    with pytest.raises(VadError, match="cannot initialize"):
        model_at(tmp_path)
    assert not runtime[0].feeds


def test_model_read_is_bounded_before_loading_vendor_dependencies(
    tmp_path: Path, runtime: list[FakeInferenceSession]
) -> None:
    path = tmp_path / "oversized.onnx"
    with path.open("wb") as output:
        output.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(VadError, match="invalid local"):
        SileroOnnxCpuModel(model_path=path, expected_sha256=DIGEST)
    assert not runtime


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_model_cannot_block_activation_on_special_or_replaced_files(
    tmp_path: Path, runtime: list[FakeInferenceSession], kind: str
) -> None:
    path = tmp_path / "synthetic.onnx"
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir()
    with pytest.raises(VadError):
        SileroOnnxCpuModel(model_path=path, expected_sha256=DIGEST)
    assert not runtime


def test_regular_model_volume_symlinks_keep_digest_verification(
    tmp_path: Path, runtime: list[FakeInferenceSession]
) -> None:
    target = tmp_path / "target.onnx"
    target.write_bytes(MODEL)
    path = tmp_path / "synthetic.onnx"
    path.symlink_to(target)
    assert SileroOnnxCpuModel(model_path=path, expected_sha256=DIGEST).model_sha256 == DIGEST
    assert len(runtime) == 1
    target.write_bytes(b"changed model bytes")
    with pytest.raises(VadError, match="digest mismatch"):
        SileroOnnxCpuModel(model_path=path, expected_sha256=DIGEST)
    assert len(runtime) == 1
