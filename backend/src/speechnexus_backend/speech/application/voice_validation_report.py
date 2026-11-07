"""The measured report and public result one validation Job commits.

The report is the re-checkable evidence behind the public ``VoiceValidation``
document. It records the exact probe text, its digest, the generation preview
digest it must differ from, and the measured output facts. Capability claims
are derived from that single probe rather than asserted independently, so a
later reviewer can recompute every claim from the stored report.
"""

from __future__ import annotations

import hashlib
import io
import json
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator, FormatChecker

from speechnexus_backend.speech.domain.voice_capability_evidence import (
    REUSABLE_TTS_PATH,
    VoiceMaterial,
)

MAX_PROBE_TEXT = 2000
MAX_REFERENCE_TEXT = 10000
VALIDATION_AUDIO_FORMAT = "wav"
VALIDATION_MODE = "complete"
VALIDATION_OUTPUT_MEDIA_TYPE = "application/json"
VALIDATION_OUTPUT_ARTIFACT_KIND: Final[Literal["validation_report"]] = "validation_report"

_CHECKS: tuple[dict[str, Any], ...] = (
    {"name": "new_text_reuse", "required": True, "status": "passed"},
    {"name": "reusable_tts", "required": True, "status": "passed"},
)


@dataclass(frozen=True, slots=True)
class ValidationRunFacts:
    """Immutable facts a validation report may state, read from committed rows."""

    draft_id: str
    draft_revision: str
    validation_set_revision: str
    binding_id: str
    binding_revision: str
    generation_preview_text: str
    material: VoiceMaterial
    reference_text: str | None
    language: str


@dataclass(frozen=True, slots=True)
class WaveFacts:
    """Measured PCM facts; never inferred from the receipt or the Job."""

    sample_rate_hz: int
    channels: int
    duration_ms: int
    bytes: int
    sha256: str


def digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def validation_id_for_job(job_id: str) -> str:
    return "validation-" + hashlib.sha256(f"voice-validation:{job_id}".encode()).hexdigest()[:40]


def validation_timestamp(created_at_us: int) -> str:
    if type(created_at_us) is not int or created_at_us < 0:
        raise ValueError("Invalid validation timestamp")
    at = datetime.fromtimestamp(created_at_us / 1_000_000, UTC)
    return at.isoformat(timespec="microseconds").replace("+00:00", "Z")


def measure_wav(body: bytes) -> WaveFacts:
    """Measure the gateway output; reject anything but 24 kHz mono PCM WAV."""
    if not body:
        raise ValueError("Validation output is empty")
    try:
        with wave.open(io.BytesIO(body), "rb") as audio:
            channels = audio.getnchannels()
            width = audio.getsampwidth()
            rate = audio.getframerate()
            frames = audio.getnframes()
            compression = audio.getcomptype()
    except wave.Error, EOFError:
        raise ValueError("Validation output is not a readable WAV") from None
    if compression != "NONE" or width != 2:
        raise ValueError("Validation output is not 16-bit PCM WAV")
    if rate != 24_000 or channels != 1:
        raise ValueError("Validation output is not 24 kHz mono WAV")
    if frames < 1:
        raise ValueError("Validation output has no audio frames")
    duration_ms = frames * 1000 // rate
    if duration_ms < 1:
        raise ValueError("Validation output is shorter than one millisecond")
    return WaveFacts(
        sample_rate_hz=rate,
        channels=channels,
        duration_ms=duration_ms,
        bytes=len(body),
        sha256="sha256:" + hashlib.sha256(body).hexdigest(),
    )


def _probe(facts: ValidationRunFacts, *, probe_text: str, measured: WaveFacts) -> dict[str, Any]:
    text_digest = digest_text(probe_text)
    preview_digest = digest_text(facts.generation_preview_text)
    if text_digest == preview_digest:
        raise ValueError("Validation probe reuses the generation preview text")
    return {
        "name": "new_text_reuse",
        "text": probe_text,
        "text_sha256": text_digest,
        "generation_preview_text_sha256": preview_digest,
        "language": facts.language,
        "sample_rate_hz": measured.sample_rate_hz,
        "audio_format": VALIDATION_AUDIO_FORMAT,
        "audio_duration_ms": measured.duration_ms,
        "output_sha256": measured.sha256,
        "output_bytes": measured.bytes,
        "mode": VALIDATION_MODE,
    }


def _capabilities(facts: ValidationRunFacts, measured: WaveFacts) -> dict[str, Any]:
    return {
        "languages": [facts.language],
        "sample_rates_hz": [measured.sample_rate_hz],
        "audio_formats": [VALIDATION_AUDIO_FORMAT],
        "modes": [VALIDATION_MODE],
    }


def _checks() -> list[dict[str, Any]]:
    return [dict(check) for check in _CHECKS]


def build_validation_report(
    facts: ValidationRunFacts,
    *,
    probe_text: str,
    measured: WaveFacts,
) -> bytes:
    """Build the canonical report bytes committed as the ``validation_report``."""
    if not 1 <= len(probe_text) <= MAX_PROBE_TEXT:
        raise ValueError("Validation probe text is outside the frozen contract")
    if (
        facts.reference_text is not None
        and not 1 <= len(facts.reference_text) <= MAX_REFERENCE_TEXT
    ):
        raise ValueError("Validation reference text is outside the frozen contract")
    probe = _probe(facts, probe_text=probe_text, measured=measured)
    report = {
        "synthesis_path": REUSABLE_TTS_PATH,
        "capabilities": _capabilities(facts, measured),
        "coverage": {
            "validation_set_revision": facts.validation_set_revision,
            "checks": _checks(),
            "probes": [probe],
        },
        "material": facts.material.as_json(),
    }
    return json.dumps(report, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_validation_result(
    facts: ValidationRunFacts,
    *,
    job_id: str,
    artifact_id: str,
    receipt_id: str,
    created_at_us: int,
) -> str:
    """Build the closed public result that references the committed report."""
    result = {
        "kind": "voice_validation",
        "validation": {
            "resource_type": "voice_validation",
            "id": validation_id_for_job(job_id),
            "object": "nexus.voice_validation",
            "draft_id": facts.draft_id,
            "draft_revision": facts.draft_revision,
            "validation_set_revision": facts.validation_set_revision,
            "model_binding": {"id": facts.binding_id, "revision": facts.binding_revision},
            "status": "passed",
            "checks": _checks(),
            "publication_eligible": True,
            "created_at": validation_timestamp(created_at_us),
        },
        "validation_report_artifact_id": artifact_id,
        "receipt_id": receipt_id,
    }
    validate_voice_validation_result(result)
    return json.dumps(result, separators=(",", ":"), ensure_ascii=False)


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    bundle = json.loads(files("speechnexus_contracts").joinpath("file.schema.json").read_text())
    return Draft202012Validator(
        {"$ref": "#/$defs/VoiceValidationJobResult", "$defs": bundle["$defs"]},
        format_checker=FormatChecker(),
    )


def validate_voice_validation_result(value: object) -> None:
    """Validate the closed public result against the generated file contract."""
    if not isinstance(value, dict) or not _validator().is_valid(value):
        raise ValueError("Voice validation result is outside the frozen contract")


__all__ = [
    "MAX_PROBE_TEXT",
    "MAX_REFERENCE_TEXT",
    "VALIDATION_OUTPUT_ARTIFACT_KIND",
    "VALIDATION_OUTPUT_MEDIA_TYPE",
    "ValidationRunFacts",
    "WaveFacts",
    "build_validation_report",
    "build_validation_result",
    "digest_text",
    "measure_wav",
    "validate_voice_validation_result",
    "validation_id_for_job",
]
