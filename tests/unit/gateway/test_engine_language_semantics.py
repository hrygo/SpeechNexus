"""Engine language and seed promises verified against the real engine (DEL-522).

The measured engine answers ``language="auto"`` with 200, accepts only its ten
vendor names, and returns 400 for a bare BCP47 tag. A fixed ``seed`` keeps the
text content stable but never makes the waveform byte-reproducible, so neither
code nor the engine templates may promise that.

The seed guard is paragraph-scoped: a claim can span two lines, and the same
promise may be written with the Chinese word 种子 instead of ``seed``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
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
SEED_MARKER = re.compile(r"seed|种子", re.IGNORECASE)
DETERMINISM_WORDS = re.compile(
    r"reproduc|bitwise|byte-identical|determinism|deterministic|完全一致|逐字节|可复现|确定性",
    re.IGNORECASE,
)
# ``不同`` is a denial of sameness, not of reproducibility, so it must not excuse
# a promise in the same paragraph.
NEGATIONS = re.compile(r"\bnot\b|\bnever\b|\bno\b|不(?!同)|无|未|非", re.IGNORECASE)
SEED_LIMIT = re.compile(r"不(保证|承诺)[^\n]{0,20}复现|不可复现")


def _paragraphs(text: str) -> Iterator[tuple[int, str]]:
    """Yield blank-line separated paragraphs with their first line number."""
    first_line = 1
    current: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not current:
                first_line = number
            current.append(line)
        elif current:
            yield first_line, "\n".join(current)
            current = []
    if current:
        yield first_line, "\n".join(current)


def _seed_promises(text: str) -> list[tuple[int, str]]:
    """Lines in seed paragraphs that assert determinism without a nearby denial.

    The check is paragraph-scoped but evaluated per determinism line with one
    neighbouring line on each side: a paragraph-level negation is too coarse
    (a claim appended to an unrelated bullet list inherits its "不"), while the
    narrower window still catches a two-line claim and a denial wrapped across
    a line break.
    """
    claims: list[tuple[int, str]] = []
    for first_line, paragraph in _paragraphs(text):
        if not SEED_MARKER.search(paragraph):
            continue
        lines = paragraph.splitlines()
        for index, line in enumerate(lines):
            if not DETERMINISM_WORDS.search(line):
                continue
            window = "\n".join(lines[max(0, index - 1) : index + 2])
            if NEGATIONS.search(window):
                continue
            claims.append((first_line + index, line))
    return claims


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


@pytest.mark.parametrize("name", ["file-voice-design", "file-voice-clone"])
def test_the_engine_template_states_that_a_seed_is_not_byte_reproducible(name: str) -> None:
    text = (ROOT / "deploy" / "engines" / name / "README.md").read_text(encoding="utf-8")
    assert SEED_LIMIT.search(text), "the template must state the seed reproducibility limit"


def test_no_seed_documentation_promises_reproducible_bytes() -> None:
    for path in SEED_DOCUMENTS:
        relative = path.relative_to(ROOT)
        for line, paragraph in _seed_promises(path.read_text(encoding="utf-8")):
            pytest.fail(
                f"{relative}:{line} promises deterministic seed output:\n{paragraph}",
            )


@pytest.mark.parametrize(
    "claim",
    [
        # A claim split over two lines must still be caught.
        "固定 seed 后，\n三次 SHA256 完全一致。",
        # The same promise without the literal English word ``seed``.
        "同一种子连续三次合成，输出逐字节可复现。",
        "固定 seed 后波形 bitwise identical。",
    ],
)
def test_a_seed_reproducibility_claim_is_detected(claim: str) -> None:
    assert _seed_promises(claim), "the guard must flag a reproducibility claim"


def test_a_negated_seed_limit_is_not_flagged() -> None:
    assert _seed_promises("固定 seed 只稳定文本内容，不保证逐字节可复现。") == []
