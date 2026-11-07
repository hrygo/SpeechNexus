"""Engine language and seed promises verified against the real engine (DEL-522).

The measured engine answers ``language="auto"`` with 200, accepts only its ten
vendor names, and returns 400 for a bare BCP47 tag. A fixed ``seed`` keeps the
text content stable but never makes the waveform byte-reproducible, so neither
code nor the engine templates may promise that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from speechnexus_gateway.domain.languages import AUTO_LANGUAGE, engine_language

ROOT = Path(__file__).resolve().parents[3]
GATEWAY = ROOT / "execution-gateway" / "src" / "speechnexus_gateway"
SEED_DOCUMENTS = (
    GATEWAY / "domain" / "tts.py",
    GATEWAY / "domain" / "voice_design.py",
    GATEWAY / "domain" / "voice_clone.py",
    ROOT / "deploy" / "engines" / "file-voice-design" / "README.md",
    ROOT / "deploy" / "engines" / "file-voice-clone" / "README.md",
)
DETERMINISM_WORDS = re.compile(
    r"reproduc|bitwise|byte-identical|determinism|deterministic|确定性|逐字节|可复现",
    re.IGNORECASE,
)
NEGATIONS = re.compile(r"\bnot\b|\bnever\b|\bno\b|不|无|未|非", re.IGNORECASE)
SEED_LIMIT = re.compile(r"不(保证|承诺)[^\n]{0,20}复现|不可复现")


def test_auto_is_forwarded_verbatim() -> None:
    assert AUTO_LANGUAGE == "auto"
    assert engine_language("auto") == "auto"


@pytest.mark.parametrize(("tag", "expected"), [("zh-Hans", "Chinese"), ("en-US", "English")])
def test_a_region_tag_still_maps_to_its_verified_vendor_name(tag: str, expected: str) -> None:
    assert engine_language(tag) == expected


@pytest.mark.parametrize("tag", ["ar", "nl", "auto-zh", "xx"])
def test_an_unverified_language_is_still_refused(tag: str) -> None:
    with pytest.raises(ValueError):
        engine_language(tag)


@pytest.mark.parametrize(
    "name",
    ["file-voice-design", "file-voice-clone"],
)
def test_the_engine_template_states_that_a_seed_is_not_byte_reproducible(name: str) -> None:
    text = (ROOT / "deploy" / "engines" / name / "README.md").read_text(encoding="utf-8")
    assert SEED_LIMIT.search(text), "the template must state the seed reproducibility limit"


def test_no_seed_documentation_promises_reproducible_bytes() -> None:
    for path in SEED_DOCUMENTS:
        relative = path.relative_to(ROOT)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "seed" not in line.lower() or not DETERMINISM_WORDS.search(line):
                continue
            assert NEGATIONS.search(line), (
                f"{relative}:{number} promises deterministic seed output: {line.strip()}"
            )
