"""Immutable private result receipts, separate from execution/stop and publication."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from speechnexus_gateway.domain.asr_results import ASR_RESULT_MEDIA_TYPE, MAX_ASR_RESULT_BYTES
from speechnexus_gateway.domain.canonical import canonical_json
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionObjectReference,
    is_fixed_storage_version,
)

MAX_RESULT_DOCUMENT_BYTES = 16 * 1024
MAX_AUDIO_RESULT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_PCM_SAMPLES = MAX_AUDIO_RESULT_BYTES // 2


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionStagingResult:
    request_digest: str
    reference: ExecutionObjectReference
    storage_version: str
    media_type: str = "application/json"

    def __post_init__(self) -> None:
        if (
            type(self.request_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_digest) is None
            or not isinstance(self.reference, ExecutionObjectReference)
            or not is_fixed_storage_version(self.storage_version)
            or self.media_type != ASR_RESULT_MEDIA_TYPE
            or not 11 <= self.reference.byte_length <= MAX_ASR_RESULT_BYTES
        ):
            raise ValueError("invalid private result receipt")
        if len(self.document.encode("utf-8")) > MAX_RESULT_DOCUMENT_BYTES:
            raise ValueError("private result receipt exceeds its byte bound")

    def value(self) -> dict[str, Any]:
        return {
            "request_digest": self.request_digest,
            "reference": asdict(self.reference),
            "storage_version": self.storage_version,
            "media_type": self.media_type,
        }

    @property
    def document(self) -> str:
        return canonical_json(self.value())

    @classmethod
    def from_document(cls, document: str) -> ExecutionStagingResult:
        try:
            if (
                type(document) is not str
                or not 1 <= len(document.encode()) <= MAX_RESULT_DOCUMENT_BYTES
            ):
                raise ValueError
            value = json.loads(document)
            if type(value) is not dict or set(value) != {
                "request_digest",
                "reference",
                "storage_version",
                "media_type",
            }:
                raise ValueError
            result = cls(
                request_digest=value["request_digest"],
                reference=ExecutionObjectReference(**value["reference"]),
                storage_version=value["storage_version"],
                media_type=value["media_type"],
            )
            # Also rejects duplicate keys, noncanonical encodings and hidden metadata.
            if result.document != document:
                raise ValueError
            return result
        except ValueError, TypeError, RecursionError:
            raise ValueError("invalid stored result receipt") from None


@dataclass(frozen=True, slots=True)
class ExecutionAudioMetadata:
    """Source clock before container encoding, never inferred from MP3 padding."""

    sample_rate_hz: int
    channels: int
    source_pcm_samples: int
    duration_ms: int
    source_pcm_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.sample_rate_hz) is not int
            or self.sample_rate_hz != 24000
            or type(self.channels) is not int
            or self.channels != 1
            or type(self.source_pcm_samples) is not int
            or not 24 <= self.source_pcm_samples <= MAX_SOURCE_PCM_SAMPLES
            or type(self.duration_ms) is not int
            or self.duration_ms != self.source_pcm_samples * 1000 // self.sample_rate_hz
            or type(self.source_pcm_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.source_pcm_digest) is None
        ):
            raise ValueError("invalid source audio metadata")


def validate_audio_payload(
    *, media_type: str, audio: ExecutionAudioMetadata, byte_length: int, content_sha256: str
) -> None:
    if (
        media_type not in {"audio/wav", "audio/pcm", "audio/mpeg"}
        or not isinstance(audio, ExecutionAudioMetadata)
        or not 1 <= byte_length <= MAX_AUDIO_RESULT_BYTES
    ):
        raise ValueError("invalid private audio payload")
    pcm_bytes = audio.source_pcm_samples * 2
    if (
        media_type == "audio/pcm"
        and (byte_length != pcm_bytes or content_sha256 != audio.source_pcm_digest)
    ) or (media_type == "audio/wav" and byte_length < pcm_bytes + 44):
        raise ValueError("audio container contradicts its source clock")


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionAudioStagingResult:
    """A separate audio receipt; the legacy JSON receipt remains unchanged."""

    request_digest: str
    reference: ExecutionObjectReference
    storage_version: str
    media_type: str
    audio: ExecutionAudioMetadata

    def __post_init__(self) -> None:
        if (
            type(self.request_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_digest) is None
            or not isinstance(self.reference, ExecutionObjectReference)
            or not is_fixed_storage_version(self.storage_version)
        ):
            raise ValueError("invalid private audio receipt")
        validate_audio_payload(
            media_type=self.media_type,
            audio=self.audio,
            byte_length=self.reference.byte_length,
            content_sha256=self.reference.content_sha256,
        )
        if len(self.document.encode("utf-8")) > MAX_RESULT_DOCUMENT_BYTES:
            raise ValueError("private audio receipt exceeds its byte bound")

    def value(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def document(self) -> str:
        return canonical_json(self.value())

    @classmethod
    def from_document(cls, document: str) -> ExecutionAudioStagingResult:
        try:
            if (
                type(document) is not str
                or not 1 <= len(document.encode()) <= MAX_RESULT_DOCUMENT_BYTES
            ):
                raise ValueError
            value = json.loads(document)
            if type(value) is not dict or set(value) != {
                "request_digest",
                "reference",
                "storage_version",
                "media_type",
                "audio",
            }:
                raise ValueError
            result = cls(
                request_digest=value["request_digest"],
                reference=ExecutionObjectReference(**value["reference"]),
                storage_version=value["storage_version"],
                media_type=value["media_type"],
                audio=ExecutionAudioMetadata(**value["audio"]),
            )
            if result.document != document:
                raise ValueError
            return result
        except ValueError, TypeError, RecursionError:
            raise ValueError("invalid stored audio receipt") from None


type StagingResult = ExecutionStagingResult | ExecutionAudioStagingResult


def staging_result_from_document(document: str) -> StagingResult:
    """Read both persisted formats without relaxing either closed parser."""
    try:
        return ExecutionStagingResult.from_document(document)
    except ValueError:
        return ExecutionAudioStagingResult.from_document(document)


def result_matches_output(
    result: StagingResult,
    *,
    output_stream_id: str | None,
    sample_rate_hz: int | None,
    generated_pcm_samples: int,
    has_output_intervals: bool,
) -> bool:
    """Registration checks the existing meter, never creates a parallel meter."""
    if isinstance(result, ExecutionStagingResult):
        return output_stream_id is None and sample_rate_hz is None
    return (
        output_stream_id is not None
        and has_output_intervals
        and sample_rate_hz == result.audio.sample_rate_hz
        and generated_pcm_samples == result.audio.source_pcm_samples
    )
