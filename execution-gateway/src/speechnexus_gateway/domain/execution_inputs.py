"""Immutable object inputs consumed inside an already-admitted execution."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol


class ExecutionInputError(ValueError):
    """Safe preparation error; contains no object path, credentials or content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class ExecutionObjectReference:
    asset_or_artifact_id: str
    workspace_id: str
    storage_bucket: str
    object_key: str
    content_sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", value) is None
                for value in (self.asset_or_artifact_id, self.workspace_id)
            )
            or not isinstance(self.storage_bucket, str)
            or not 1 <= len(self.storage_bucket) <= 128
            or not isinstance(self.object_key, str)
            or not 1 <= len(self.object_key) <= 512
            or not isinstance(self.content_sha256, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_sha256) is None
            or type(self.byte_length) is not int
            or self.byte_length < 1
        ):
            raise ExecutionInputError("execution_input_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionInputLimits:
    """Local bounded consumption; the caller must already reserve the complete recipe."""

    max_bytes: int
    memory_bytes: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= 1024 * 1024 * 1024
            or type(self.memory_bytes) is not int
            or not 1 <= self.memory_bytes <= min(self.max_bytes, 8 * 1024 * 1024)
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 3600
        ):
            raise ExecutionInputError("execution_input_limits_invalid")


@dataclass(frozen=True, slots=True)
class VerifiedExecutionObject:
    reference: ExecutionObjectReference = field(repr=False)
    stream: BinaryIO = field(repr=False)
    storage_version: str = field(repr=False)


class ExecutionObjectInputs(Protocol):
    """Yield seekable input only after exact length, digest and EOF verification."""

    def open_verified(
        self,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> AbstractContextManager[VerifiedExecutionObject]: ...
