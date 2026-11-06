"""Durable replacement lineage for public Jobs.

A retry never rewrites its parent: the parent stays in its frozen terminal state
and the replacement names it through `retry_of_job_id`. The retry request uses
the ordinary public Job create key namespace, so one key still identifies one
intent and one operation; the parent identity is part of that identity.
"""

RETRY_COLUMN = "retry_of_job_id"

PUBLIC_JOB_RETRY_DDL = (
    f"""ALTER TABLE speech_jobs ADD COLUMN {RETRY_COLUMN} VARCHAR(128) COLLATE "C"
        REFERENCES speech_jobs (job_id) ON DELETE RESTRICT""",
    f"""ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_retry_of_check
        CHECK ({RETRY_COLUMN} IS NULL OR {RETRY_COLUMN} <> job_id)""",
    f"CREATE INDEX speech_jobs_retry_of ON speech_jobs ({RETRY_COLUMN})",
)

# The 0016/0034 guard never mentions the retry column because it did not exist
# then. This replacement keeps every original check and additionally freezes the
# replacement lineage after insert.
PUBLIC_JOB_RETRY_GUARD_REPLACE = (
    """CREATE OR REPLACE FUNCTION speech_jobs_status_guard() RETURNS trigger
   LANGUAGE plpgsql AS $$
   BEGIN
     IF NEW.job_id <> OLD.job_id
        OR NEW.workspace_id <> OLD.workspace_id
        OR NEW.kind <> OLD.kind
        OR NEW.model_id <> OLD.model_id
        OR NEW.request_digest <> OLD.request_digest
        OR NEW.idempotency_key_digest IS DISTINCT FROM OLD.idempotency_key_digest
        OR NEW.retry_of_job_id IS DISTINCT FROM OLD.retry_of_job_id
        OR NEW.created_at_us <> OLD.created_at_us THEN
       RAISE EXCEPTION 'public Job identity is immutable' USING ERRCODE = '23514';
     END IF;
     IF NEW.status_version <> OLD.status_version + 1 THEN
       RAISE EXCEPTION 'public Job status_version must advance by one'
         USING ERRCODE = '23514';
     END IF;
     IF NOT (
          (OLD.state = 'pending' AND NEW.state IN
             ('running', 'cancelling', 'failed', 'cancelled', 'expired'))
          OR (OLD.state = 'running' AND NEW.state IN
             ('succeeded', 'failed', 'cancelling', 'cancelled'))
          OR (OLD.state = 'cancelling' AND NEW.state IN ('cancelled', 'failed'))
     ) THEN
       RAISE EXCEPTION 'public Job state transition is not allowed' USING ERRCODE = '23514';
     END IF;
     RETURN NEW;
   END
   $$"""
)
