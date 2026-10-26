"""Share pinned-source verification without importing the engine on the host."""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Any


def verify_pin(pin: dict[str, Any]) -> None:
    version = importlib.import_module("vllm._version")
    if version.__version__ != pin["version"] or version.__commit_id__ != pin["commit"]:
        raise RuntimeError("ASR cleanup requires its pinned vLLM runtime")
    module_file = importlib.import_module("vllm").__file__
    if not isinstance(module_file, str):
        raise RuntimeError("ASR cleanup native source is unavailable")
    root = Path(module_file).resolve().parent
    try:
        matches = all(
            hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest
            for relative, digest in pin["source_sha256"].items()
        )
    except OSError:
        raise RuntimeError("ASR cleanup native source verification failed") from None
    if not matches:
        raise RuntimeError("ASR cleanup native source verification failed")
