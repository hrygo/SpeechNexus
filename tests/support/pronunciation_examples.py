"""Synthetic ordered pronunciation material; no engine or voice qualification."""

from typing import Any


def pronunciation_request(**changes: Any) -> dict[str, Any]:
    return {
        "encoding": "pinyin",
        "entries": [
            {"grapheme": "SpeechNexus", "phonemes": "si pi chi ne ke si", "language": "zh-CN"},
            {"grapheme": "synthetic", "phonemes": "sin the tik"},
        ],
        "publish": False,
        **changes,
    }
