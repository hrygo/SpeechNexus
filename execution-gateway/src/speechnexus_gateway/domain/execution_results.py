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
