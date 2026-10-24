"""Materialize bounded synthetic PCM inside tests; store no encoded audio fixtures."""

from __future__ import annotations

import base64
import copy
from typing import Any


def example_value(example: dict[str, Any]) -> Any:
    value = copy.deepcopy(example["value"])
    if "synthetic_pcm_bytes" in example:
        count = example["synthetic_pcm_bytes"]
        if type(count) is not int or not 0 < count <= 8192:
            raise ValueError("Synthetic PCM recipes must be bounded")
        if value.get("type") not in ("input_audio_buffer.append", "nexus.tts.audio.delta"):
            raise ValueError("Synthetic PCM recipes require an audio event")
        value["audio"] = base64.b64encode(bytes(count)).decode("ascii")
    return value
