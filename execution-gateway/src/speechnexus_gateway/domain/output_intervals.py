"""Immutable PCM intervals own usage; execution watermarks are only max-end projections."""

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmissionIntent,
    SubmissionStoreError,
    hard_output_ceiling_samples,
)


class MeterKind(StrEnum):
    GENERATED = "generated"
    DELIVERED = "delivered"
    PLAYED = "played"


class OutputIntervalConflictError(SubmissionStoreError):
    code = "idempotency_conflict"
    recovery = "query_original"


class OutputOverlapError(SubmissionStoreError):
    code = "output_overlap"
    recovery = "reconcile"


class OutputLimitExceededError(SubmissionStoreError):
    code = "output_limit_exceeded"
    recovery = "reconcile"


@dataclass(frozen=True, slots=True)
class AppendInterval:
    interval_id: str
    submit_intent_id: str
    output_stream_id: str
    sample_rate_hz: int
    meter_kind: MeterKind
    sample_start: int
    sample_end: int

    def __post_init__(self) -> None:
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]", value)
            for value in (self.interval_id, self.submit_intent_id, self.output_stream_id)
        ):
            raise InvalidExecutionRequestError("invalid output interval identity")
        if (
            type(self.sample_rate_hz) is not int
            or not 1 <= self.sample_rate_hz <= 384000
            or not isinstance(self.meter_kind, MeterKind)
            or type(self.sample_start) is not int
            or type(self.sample_end) is not int
            or not 0 <= self.sample_start < self.sample_end <= 2**53 - 1
        ):
            raise InvalidExecutionRequestError("invalid canonical sample interval")

    def document(self) -> dict[str, JsonValue]:
        return {
            "interval_id": self.interval_id,
            "submit_intent_id": self.submit_intent_id,
            "output_stream_id": self.output_stream_id,
            "sample_rate_hz": self.sample_rate_hz,
            "meter_kind": self.meter_kind.value,
            "sample_start": self.sample_start,
            "sample_end": self.sample_end,
        }


@dataclass(frozen=True, slots=True)
class OutputIntervalRecord:
    interval: AppendInterval
    identity: SubmissionIdentity

    def __post_init__(self) -> None:
        if self.interval.submit_intent_id != self.identity.submit_intent_id:
            raise InvalidExecutionRequestError("interval belongs to another execution")

    @property
    def stream_key(self) -> tuple[str, str, str]:
        return (
            self.identity.operation_id,
            self.identity.attempt_id,
            self.interval.output_stream_id,
        )

    @property
    def request_digest(self) -> str:
        return command_digest(self.interval.document())

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": "fact",
            "fact_id": self.interval.interval_id,
            "request_digest": self.request_digest,
        }

    def validate_execution(self, current: SubmissionIntent) -> None:
        interval = self.interval
        if (
            self.identity != current.identity
            or interval.output_stream_id != current.output_stream_id
            or interval.sample_rate_hz != current.identity.sample_rate_hz
        ):
            raise InvalidExecutionRequestError("interval changes frozen execution or stream facts")
        ceiling = hard_output_ceiling_samples(
            max_output_audio_ms=current.identity.max_output_audio_ms,
            sample_rate_hz=current.identity.sample_rate_hz,
        )
        if ceiling is not None and interval.sample_end > ceiling:
            raise OutputLimitExceededError("output interval exceeds the frozen sample ceiling")

    def validate_authority(self, current: SubmissionIntent, *, stream_sample_rate_hz: int) -> None:
        self.validate_execution(current)
        if self.interval.sample_rate_hz != stream_sample_rate_hz:
            raise InvalidExecutionRequestError("logical stream authority uses another sample rate")

    def apply(
        self,
        current: SubmissionIntent,
        *,
        stream_sample_rate_hz: int,
        previous: OutputIntervalRecord | None,
        overlaps: bool,
        at: datetime,
    ) -> OutputIntervalWrite:
        self.validate_authority(current, stream_sample_rate_hz=stream_sample_rate_hz)
        if previous is not None:
            if previous != self:
                raise OutputIntervalConflictError("interval identity already has another fact")
            return OutputIntervalWrite(previous, current, replayed=True)
        if overlaps:
            raise OutputOverlapError("another interval overlaps this logical stream and meter")
        updated = deepcopy(current)
        updated.has_output_intervals = True
        if self.interval.meter_kind is MeterKind.GENERATED:
            if current.version >= 2**63 - 1 or at < current.updated_at:
                raise SubmissionStoreError("cannot advance the execution's output projection")
            updated.generated_pcm_samples = max(
                current.generated_pcm_samples, self.interval.sample_end
            )
        try:
            if self.interval.meter_kind is MeterKind.GENERATED:
                updated._touch(at)
            else:
                updated.validate()
        except ValueError:
            raise ExecutionCleanupPendingError(
                "output interval contradicts the recorded execution cleanup"
            ) from None
        return OutputIntervalWrite(self, updated, replayed=False)


@dataclass(frozen=True, slots=True)
class OutputIntervalWrite:
    record: OutputIntervalRecord
    intent: SubmissionIntent
    replayed: bool
