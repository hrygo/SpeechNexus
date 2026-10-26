"""Private AssetInspector consumer; default runtime activation stays separate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, BinaryIO, NoReturn, Protocol

from speechnexus_contracts.audio_inspection import parse

from speechnexus_backend.speech.application.assets import TextAssetInspector
from speechnexus_backend.speech.domain.assets import (
    AssetPolicy,
    AssetPurpose,
    Inspection,
    MediaFacts,
)
from speechnexus_backend.speech.infrastructure.audio_inspection_process import (
    InspectionProcessResult,
)


class InspectionProcess(Protocol):
    def run(
        self, source: BinaryIO, *, cancelled: Callable[[], bool]
    ) -> InspectionProcessResult: ...


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate inspection field")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> NoReturn:
    raise ValueError("Invalid inspection number")


class ProcessAudioAssetInspector:
    """Consume a server-configured PCM WAV worker for asr_input only.

    Failure, cancellation, unknown stop or invalid output remains unavailable,
    not a permanent content rejection. This port creates no metadata, object,
    public ready, execution-stop or budget fact by itself.
    """

    def __init__(
        self,
        process: InspectionProcess,
        *,
        max_duration_ms: int,
        max_receipt_bytes: int = 4096,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        if (
            type(max_duration_ms) is not int
            or not 1 <= max_duration_ms <= 86400000
            or type(max_receipt_bytes) is not int
            or not 1 <= max_receipt_bytes <= 65536
        ):
            raise ValueError("Invalid private audio inspection limits")
        self._process = process
        self._max_duration_ms = max_duration_ms
        self._max_receipt_bytes = max_receipt_bytes
        self._cancelled = cancelled
        self._text = TextAssetInspector()

    def inspect(self, purpose: AssetPurpose, body: BinaryIO, policy: AssetPolicy) -> Inspection:
        if purpose == "text_input":
            return self._text.inspect(purpose, body, policy)
        if purpose != "asr_input":
            return Inspection(available=False)
        try:
            result = self._inspect_audio(body, policy)
        except Exception:
            # No untrusted receipt, vendor message, path or source content is
            # propagated into a public error or logger.
            result = Inspection(available=False)
        try:
            body.seek(0)
        except OSError, ValueError:
            return Inspection(available=False)
        return result

    def _inspect_audio(self, body: BinaryIO, policy: AssetPolicy) -> Inspection:
        body.seek(0)
        digest = hashlib.sha256()
        encoded_bytes = 0
        while True:
            if self._cancelled():
                return Inspection(available=False)
            chunk = body.read(65536)
            if type(chunk) is not bytes:
                return Inspection(available=False)
            if not chunk:
                break
            encoded_bytes += len(chunk)
            if encoded_bytes > policy.max_bytes:
                return Inspection(available=False)
            digest.update(chunk)
        if not encoded_bytes or self._cancelled():
            return Inspection(available=False)
        body.seek(0)
        outcome = self._process.run(body, cancelled=self._cancelled)
        if (
            self._cancelled()
            or outcome.status != "complete"
            or not 0 < len(outcome.output) <= self._max_receipt_bytes
        ):
            return Inspection(available=False)
        receipt = parse(
            "AudioInspectionSuccess",
            json.loads(
                outcome.output.decode("utf-8"),
                object_pairs_hook=_unique_fields,
                parse_constant=_invalid_constant,
            ),
        )
        counters = (
            receipt["schema_version"],
            receipt["encoded_bytes"],
            receipt["source_samples"],
            receipt["sample_rate_hz"],
            receipt["channels"],
            receipt["duration_ms"],
        )
        if (
            any(type(value) is not int for value in counters)
            or receipt["sha256"] != "sha256:" + digest.hexdigest()
            or receipt["encoded_bytes"] != encoded_bytes
            or receipt["media_type"] != "audio/wav"
            or receipt["duration_ms"] > self._max_duration_ms
            or receipt["duration_ms"]
            != (receipt["source_samples"] * 1000 + receipt["sample_rate_hz"] - 1)
            // receipt["sample_rate_hz"]
        ):
            return Inspection(available=False)
        # Parsing/validation can overlap cancellation after the process has
        # returned. Recheck at the last local boundary; publication still needs
        # its own authoritative transaction-time checks.
        if self._cancelled():
            return Inspection(available=False)
        return Inspection(
            facts=MediaFacts(
                receipt["media_type"],
                receipt["duration_ms"],
                receipt["sample_rate_hz"],
                receipt["channels"],
            )
        )
