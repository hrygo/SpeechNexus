"""Pinned Silero ONNX CPU adapter with explicit, isolated vendor dependencies.

The runtime installs deploy/engines/cpu-vad/requirements.txt separately. Importing
the gateway does not import NumPy/ONNX Runtime, load weights, or download models.
The official Silero wrapper is the I/O reference, not a process-global state owner.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from pathlib import Path
from typing import Any

from speechnexus_gateway.domain.vad import (
    VAD_FRAME_BYTES,
    VAD_SAMPLE_RATE_HZ,
    VadDetector,
    VadError,
)


class SileroOnnxCpuModel:
    def __init__(self, *, model_path: Path, expected_sha256: str) -> None:
        if re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None:
            raise VadError("a pinned model SHA-256 is required")
        try:
            with model_path.open("rb") as source:
                document = source.read(16 * 1024 * 1024 + 1)
        except OSError:
            raise VadError("cannot read the local VAD model") from None
        if not 0 < len(document) <= 16 * 1024 * 1024:
            raise VadError("invalid local VAD model")
        if hashlib.sha256(document).hexdigest() != expected_sha256:
            raise VadError("VAD model digest mismatch")
        try:
            self._np: Any = importlib.import_module("numpy")
            ort: Any = importlib.import_module("onnxruntime")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self._session: Any = ort.InferenceSession(
                document, sess_options=options, providers=["CPUExecutionProvider"]
            )
            if self._session.get_providers() != ["CPUExecutionProvider"]:
                raise VadError("VAD requires the CPU execution provider")
            inputs = [(item.name, item.type) for item in self._session.get_inputs()]
            outputs = [(item.name, item.type) for item in self._session.get_outputs()]
            if inputs != [
                ("input", "tensor(float)"),
                ("state", "tensor(float)"),
                ("sr", "tensor(int64)"),
            ] or outputs != [("output", "tensor(float)"), ("stateN", "tensor(float)")]:
                raise VadError("unsupported VAD model interface")
        except Exception:
            raise VadError("cannot initialize the pinned CPU VAD runtime") from None
        self._model_sha256 = expected_sha256

    @property
    def model_sha256(self) -> str:
        return self._model_sha256

    def new_detector(self) -> VadDetector:
        return _SileroSession(self)


class _SileroSession:
    def __init__(self, model: SileroOnnxCpuModel) -> None:
        self._model = model
        self.reset()

    def reset(self) -> None:
        self._state = self._model._np.zeros((2, 1, 128), dtype="float32")
        self._context = self._model._np.zeros((1, 64), dtype="float32")

    def probability(self, pcm16: bytes) -> float:
        if type(pcm16) is not bytes or len(pcm16) != VAD_FRAME_BYTES:
            raise VadError("VAD inference requires exactly 512 PCM16LE samples")
        np = self._model._np
        audio = np.frombuffer(pcm16, dtype="<i2").astype("float32")[None, :] / 32768.0
        contextual = np.concatenate((self._context, audio), axis=1)
        probability, state = self._model._session.run(
            ["output", "stateN"],
            {
                "input": contextual,
                "state": self._state,
                "sr": np.array(VAD_SAMPLE_RATE_HZ, dtype="int64"),
            },
        )
        if (
            probability.shape != (1, 1)
            or state.shape != (2, 1, 128)
            or probability.dtype != np.float32
            or state.dtype != np.float32
            or not np.isfinite(probability).all()
            or not np.isfinite(state).all()
            or not 0 <= float(probability[0, 0]) <= 1
        ):
            raise VadError("invalid CPU VAD inference result")
        self._state = state.copy()
        self._context = contextual[:, -64:].copy()
        return float(probability[0, 0])
