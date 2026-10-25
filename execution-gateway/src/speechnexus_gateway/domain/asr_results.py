"""Private file-ASR results; storage acknowledgment is never execution or release evidence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from speechnexus_gateway.domain.execution_inputs import ExecutionObjectReference

MAX_ASR_RESULT_BYTES = 4 * 1024 * 1024
ASR_RESULT_MEDIA_TYPE = "application/json"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class AsrResultError(ValueError):
    """Stable local error, without content, storage addresses or vendor messages."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_asr_result_limit(max_result_bytes: int) -> None:
    if type(max_result_bytes) is not int or not 11 <= max_result_bytes <= MAX_ASR_RESULT_BYTES:
        raise AsrResultError("asr_result_limit_invalid")


@dataclass(frozen=True, slots=True, repr=False)
class PreparedAsrResult:
    """Complete UTF-8 Transcription JSON, bounded before any object write."""

    workspace_id: str
    request_digest: str
    body: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.workspace_id) is not str
            or _IDENTIFIER.fullmatch(self.workspace_id) is None
            or type(self.request_digest) is not str
            or _DIGEST.fullmatch(self.request_digest) is None
            or type(self.body) is not bytes
            or not 11 <= len(self.body) <= MAX_ASR_RESULT_BYTES
        ):
            raise AsrResultError("asr_result_invalid")

    @property
    def content_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.body).hexdigest()

    def reference(self, bucket: str) -> ExecutionObjectReference:
        token = hashlib.sha256(
            (self.request_digest + "\n" + self.content_sha256).encode("ascii")
        ).hexdigest()
        return ExecutionObjectReference(
            "asr-result-" + token,
            self.workspace_id,
            bucket,
            "private/execution-results/" + token + "/transcription.json",
            self.content_sha256,
            len(self.body),
        )


@dataclass(frozen=True, slots=True, repr=False)
class StagedAsrResult:
    """A verified private version, to be persisted and published by its authorized owner."""

    request_digest: str
    reference: ExecutionObjectReference
    storage_version: str

    def __post_init__(self) -> None:
        if (
            type(self.request_digest) is not str
            or _DIGEST.fullmatch(self.request_digest) is None
            or not isinstance(self.reference, ExecutionObjectReference)
            or type(self.storage_version) is not str
            or not 1 <= len(self.storage_version) <= 1024
            or self.storage_version == "null"
            or any(ord(char) < 32 or ord(char) == 127 for char in self.storage_version)
        ):
            raise AsrResultError("asr_result_identity_mismatch")


class AsrResultStore(Protocol):
    """One bounded write and readback; no publication, retry, deletion or budget authority."""

    async def stage(
        self, result: PreparedAsrResult, *, cancelled: Callable[[], bool]
    ) -> StagedAsrResult: ...
