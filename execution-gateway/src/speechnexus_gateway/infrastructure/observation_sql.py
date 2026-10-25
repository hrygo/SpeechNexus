"""Observe under ordinary pool/submit row locks, then write one guarded version."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.submission import SubmissionIntent
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    lock_result_registration,
    lock_submit_intent,
    save_staging_result,
    save_submit_intent,
)


def record_execution_observation_in_transaction(
    cursor: SubmitCursor, request: RecordExecutionObservation, *, clock: Callable[[], datetime]
) -> SubmissionIntent:
    current = (
        lock_submit_intent(cursor, request.observation.identity.submit_intent_id)
        if request.staging_result is None
        else lock_result_registration(cursor, request.observation.identity, clock=clock)
    )
    updated = request.apply(current, at=clock())
    if updated is not current:
        if isinstance(request.staging_result, ExecutionAudioStagingResult):
            # append_interval takes the same submit row lock before modifying
            # these immutable intervals, so coverage cannot race registration.
            cursor.execute(
                "SELECT MIN(sample_start) AS sample_start, MAX(sample_end) AS sample_end, "
                "COALESCE(SUM(sample_end - sample_start), 0) AS sample_count "
                "FROM output_sample_intervals WHERE submit_intent_id = %s "
                "AND meter_kind = 'generated'",
                (current.identity.submit_intent_id,),
            )
            coverage = cursor.fetchall()[0]
            request.validate_audio_coverage(
                sample_start=coverage["sample_start"],
                sample_end=coverage["sample_end"],
                sample_count=int(coverage["sample_count"]),
            )
        save_submit_intent(
            cursor,
            updated,
            expected_version=request.expected_version,
            registering_result=request.staging_result is not None,
        )
        if request.staging_result is not None:
            save_staging_result(cursor, updated)
    return updated
