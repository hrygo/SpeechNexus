"""Ordinary pool → submit → logical-stream row locks protect append-only interval facts."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    MeterKind,
    OutputIntervalRecord,
    OutputIntervalWrite,
    OutputLimitExceededError,
)
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    lock_submit_intent,
    read_submit_intent,
    to_unix_micros,
)


def read_output_interval(cursor: SubmitCursor, interval_id: str) -> OutputIntervalRecord | None:
    """Read an immutable original fact without any locks, private content or side effects."""
    cursor.execute(
        "SELECT i.interval_id, i.submit_intent_id, i.operation_id, i.attempt_id, "
        "i.output_stream_id, h.sample_rate_hz, i.meter_kind, i.sample_start, i.sample_end "
        "FROM output_sample_intervals AS i JOIN output_stream_heads AS h "
        "ON h.operation_id = i.operation_id AND h.attempt_id = i.attempt_id "
        "AND h.output_stream_id = i.output_stream_id WHERE i.interval_id = %s",
        (interval_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise SubmissionStoreError("interval does not resolve to one immutable fact")
    row = rows[0]
    current = read_submit_intent(cursor, row["submit_intent_id"])
    if current is None:
        raise SubmissionStoreError("interval has no authoritative execution")
    try:
        record = OutputIntervalRecord(
            AppendInterval(
                row["interval_id"],
                row["submit_intent_id"],
                row["output_stream_id"],
                row["sample_rate_hz"],
                MeterKind(row["meter_kind"]),
                row["sample_start"],
                row["sample_end"],
            ),
            current.identity,
        )
        if record.stream_key != (row["operation_id"], row["attempt_id"], row["output_stream_id"]):
            raise ValueError("interval changed logical stream identity")
        record.validate_authority(current, stream_sample_rate_hz=row["sample_rate_hz"])
        return record
    except (
        KeyError,
        TypeError,
        ValueError,
        InvalidExecutionRequestError,
        OutputLimitExceededError,
    ) as error:
        raise SubmissionStoreError("invalid persisted output interval") from error


def append_interval_in_transaction(
    cursor: SubmitCursor, record: OutputIntervalRecord, *, clock: Callable[[], datetime]
) -> OutputIntervalWrite:
    interval = record.interval
    current = lock_submit_intent(cursor, interval.submit_intent_id)
    record.validate_execution(current)
    cursor.execute(
        "SELECT sample_rate_hz FROM output_stream_heads WHERE operation_id = %s "
        "AND attempt_id = %s AND output_stream_id = %s FOR UPDATE",
        record.stream_key,
    )
    heads = cursor.fetchall()
    if len(heads) != 1:
        raise InvalidExecutionRequestError("execution has no matching PCM stream authority")
    previous = read_output_interval(cursor, interval.interval_id)
    overlaps = False
    if previous is None:
        cursor.execute(
            "SELECT interval_id FROM output_sample_intervals WHERE operation_id = %s "
            "AND attempt_id = %s AND output_stream_id = %s AND meter_kind = %s "
            "AND sample_start < %s AND sample_end > %s LIMIT 1",
            (
                *record.stream_key,
                interval.meter_kind.value,
                interval.sample_end,
                interval.sample_start,
            ),
        )
        overlaps = bool(cursor.fetchall())
    result = record.apply(
        current,
        stream_sample_rate_hz=heads[0]["sample_rate_hz"],
        previous=previous,
        overlaps=overlaps,
        at=clock(),
    )
    if result.replayed:
        return result
    cursor.execute(
        "INSERT INTO output_sample_intervals (interval_id, submit_intent_id, operation_id, "
        "attempt_id, output_stream_id, meter_kind, sample_start, sample_end) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            interval.interval_id,
            interval.submit_intent_id,
            *record.stream_key,
            interval.meter_kind.value,
            interval.sample_start,
            interval.sample_end,
        ),
    )
    if interval.meter_kind is MeterKind.GENERATED:
        cursor.execute(
            "UPDATE submit_intents SET generated_pcm_samples = %s, updated_at_us = %s, "
            "version = version + 1 WHERE submit_intent_id = %s AND version = %s",
            (
                result.intent.generated_pcm_samples,
                to_unix_micros(result.intent.updated_at),
                interval.submit_intent_id,
                current.version,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleSubmissionVersionError("execution output projection changed before append")
    return result
