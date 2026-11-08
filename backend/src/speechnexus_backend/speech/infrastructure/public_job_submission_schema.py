"""Immutable public Job submission document kept beside the mutable lifecycle row.

``speech_jobs`` stores identity, lifecycle and the result reference. The pinned
effective configuration and the original request correlation cannot be rebuilt
from later publications, so they are written once in this companion table.
"""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    JOB_SUBMISSION_INSERT_COLUMNS,
)

SUBMISSIONS = "speech_job_submissions"

PUBLIC_JOB_SUBMISSION_DDL = (
    f"""CREATE TABLE {SUBMISSIONS} (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES speech_jobs (job_id) ON DELETE RESTRICT,
        request_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (length(request_id) BETWEEN 1 AND 128),
        client_reference VARCHAR(256) COLLATE "C",
        effective_configuration_json TEXT NOT NULL
            CHECK (OCTET_LENGTH(effective_configuration_json) BETWEEN 2 AND 65536),
        execution_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (execution_revision ~ '^[1-9][0-9]{{0,18}}$'),
        progress_unit VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (progress_unit IN ('audio_ms', 'text_codepoints', 'items')),
        progress_total BIGINT CHECK (progress_total >= 0),
        submitted_at_us BIGINT NOT NULL
            CHECK (submitted_at_us BETWEEN 0 AND 253402300799999999)
    )""",
)


def _status_guard_function(create: str) -> str:
    return f"""{create} FUNCTION speech_jobs_status_guard() RETURNS trigger
       LANGUAGE plpgsql AS $$
       BEGIN
         IF NEW.job_id <> OLD.job_id
            OR NEW.workspace_id <> OLD.workspace_id
            OR NEW.kind <> OLD.kind
            OR NEW.model_id <> OLD.model_id
            OR NEW.request_digest <> OLD.request_digest
            OR NEW.idempotency_key_digest IS DISTINCT FROM OLD.idempotency_key_digest
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


PUBLIC_JOB_STATUS_GUARD_DDL = (
    _status_guard_function("CREATE"),
    """CREATE TRIGGER speech_jobs_status_guard
       BEFORE UPDATE ON speech_jobs
       FOR EACH ROW EXECUTE FUNCTION speech_jobs_status_guard()""",
)
# Used by later migrations that replace the function while retaining the trigger.
PUBLIC_JOB_STATUS_GUARD_REPLACE = _status_guard_function("CREATE OR REPLACE")


def public_job_submission_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{SUBMISSIONS} TO "{role}"',
        f"GRANT INSERT ({', '.join(JOB_SUBMISSION_INSERT_COLUMNS)}) "
        f'ON "{schema}".{SUBMISSIONS} TO "{role}"',
    )
