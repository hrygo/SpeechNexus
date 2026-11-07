"""Declared column surfaces for the combined public Job writer (#561).

The writer never receives a table-level INSERT: every writable relation is
granted by column, and startup compares the real grant against these tuples.
Keeping the declaration here lets provisioning and the startup verifier share
one source of truth instead of drifting apart.
"""

from speechnexus_backend.speech.infrastructure.artifact_schema import ARTIFACT_INSERT_COLUMNS

COORDINATION_JOB_INSERT_COLUMNS = (
    "job_id",
    "operation_id",
    "attempt_id",
    "initial_event_id",
    "initial_event_digest",
    "request_digest",
)
COORDINATION_OUTBOX_INSERT_COLUMNS = ("event_id", "job_id", "event_digest")
ADMISSION_OPERATION_INSERT_COLUMNS = (
    "operation_id",
    "workspace_id",
    "request_digest",
    "request_document",
    "initial_attempt_id",
    "current_attempt_id",
    "recorded_at_us",
)
JOB_INTENT_INSERT_COLUMNS = (
    "job_id",
    "request_digest",
    "request_document",
    "recorded_at_us",
)
CANCELLATION_NOT_SUBMITTED_INSERT_COLUMNS = ("job_id", "observed_at_us", "observer")
JOB_INSERT_COLUMNS = (
    "job_id",
    "workspace_id",
    "kind",
    "model_id",
    "request_digest",
    "idempotency_key_digest",
    "state",
    "status_version",
    "result_json",
    "error_code",
    "created_at_us",
    "updated_at_us",
    "retry_of_job_id",
)
JOB_EVENT_INSERT_COLUMNS = (
    "job_id",
    "event_seq",
    "state",
    "occurred_at_us",
    "kind",
    "progress_unit",
    "progress_completed",
    "progress_total",
)
JOB_SUBMISSION_INSERT_COLUMNS = (
    "job_id",
    "request_id",
    "client_reference",
    "effective_configuration_json",
    "execution_revision",
    "progress_unit",
    "progress_total",
    "submitted_at_us",
)
JOB_CANCELLATION_INSERT_COLUMNS = (
    "job_id",
    "workspace_id",
    "operation_id",
    "attempt_id",
    "request_id",
    "request_digest",
    "idempotency_key_digest",
    "accepted",
    "requested_at_us",
)
CANCELLATION_FENCE_INSERT_COLUMNS = ("job_id", "accepted_at_us", "request_id")
CANCELLATION_RECONCILIATION_INSERT_COLUMNS = (
    "job_id",
    "attempts",
    "next_check_at_us",
    "last_outcome",
    "settled_at_us",
    "updated_at_us",
)
VOICE_DRAFT_INSERT_COLUMNS = (
    "draft_id",
    "generation_job_id",
    "kind",
    "status",
    "head_revision",
    "metadata_revision",
    "display_name",
    "input_json",
    "binding_json",
    "created_at_us",
    "updated_at_us",
)
VOICE_DRAFT_REVISION_INSERT_COLUMNS = (
    "draft_id",
    "revision",
    "kind",
    "generation_job_id",
    "input_json",
    "binding_json",
    "created_at_us",
)
VOICE_METADATA_COMMAND_INSERT_COLUMNS = (
    "draft_id",
    "key_digest",
    "request_digest",
    "result_json",
)

JOB_WRITER_INSERT_COLUMNS: dict[str, tuple[str, ...]] = {
    "coordination_jobs": COORDINATION_JOB_INSERT_COLUMNS,
    "coordination_outbox": COORDINATION_OUTBOX_INSERT_COLUMNS,
    "admission_operations": ADMISSION_OPERATION_INSERT_COLUMNS,
    "speech_job_intents": JOB_INTENT_INSERT_COLUMNS,
    "speech_artifacts": ARTIFACT_INSERT_COLUMNS,
    "speech_job_cancellation_not_submitted": CANCELLATION_NOT_SUBMITTED_INSERT_COLUMNS,
    "speech_jobs": JOB_INSERT_COLUMNS,
    "speech_job_events": JOB_EVENT_INSERT_COLUMNS,
    "speech_job_submissions": JOB_SUBMISSION_INSERT_COLUMNS,
    "speech_job_cancellations": JOB_CANCELLATION_INSERT_COLUMNS,
    "speech_job_cancellation_fences": CANCELLATION_FENCE_INSERT_COLUMNS,
    "speech_job_cancellation_reconciliations": CANCELLATION_RECONCILIATION_INSERT_COLUMNS,
    "speech_voice_drafts": VOICE_DRAFT_INSERT_COLUMNS,
    "speech_voice_draft_revisions": VOICE_DRAFT_REVISION_INSERT_COLUMNS,
    "speech_voice_metadata_commands": VOICE_METADATA_COMMAND_INSERT_COLUMNS,
}

__all__ = [
    "ADMISSION_OPERATION_INSERT_COLUMNS",
    "ARTIFACT_INSERT_COLUMNS",
    "CANCELLATION_FENCE_INSERT_COLUMNS",
    "CANCELLATION_NOT_SUBMITTED_INSERT_COLUMNS",
    "CANCELLATION_RECONCILIATION_INSERT_COLUMNS",
    "COORDINATION_JOB_INSERT_COLUMNS",
    "COORDINATION_OUTBOX_INSERT_COLUMNS",
    "JOB_CANCELLATION_INSERT_COLUMNS",
    "JOB_EVENT_INSERT_COLUMNS",
    "JOB_INSERT_COLUMNS",
    "JOB_INTENT_INSERT_COLUMNS",
    "JOB_SUBMISSION_INSERT_COLUMNS",
    "JOB_WRITER_INSERT_COLUMNS",
    "VOICE_DRAFT_INSERT_COLUMNS",
    "VOICE_DRAFT_REVISION_INSERT_COLUMNS",
    "VOICE_METADATA_COMMAND_INSERT_COLUMNS",
]
