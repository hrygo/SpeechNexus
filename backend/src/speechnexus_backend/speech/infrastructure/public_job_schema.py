"""Public job read model separated from coordination facts.

``coordination_jobs`` holds dispatch identity while this table holds the public
request summary, lifecycle state and result reference. ``succeeded`` cannot exist
without a published artifact reference, and no other state may carry one.
"""

from speechnexus_backend.speech.infrastructure.artifact_schema import ARTIFACTS
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    job_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    JOB_EVENT_INSERT_COLUMNS,
    JOB_INSERT_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.public_job_cancellation_schema import (
    public_job_cancellation_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    SUBMISSIONS,
)

JOBS = "speech_jobs"
EVENTS = "speech_job_events"
STATES = (
    "pending",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
)

PUBLIC_JOB_DDL = (
    f"""CREATE TABLE {JOBS} (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL,
        kind VARCHAR(16) COLLATE "C" NOT NULL CHECK (kind IN ('asr', 'tts')),
        model_id VARCHAR(128) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        idempotency_key_digest VARCHAR(71) COLLATE "C",
        state VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'running', 'cancelling', 'succeeded',
                             'failed', 'cancelled', 'expired')),
        status_version BIGINT NOT NULL DEFAULT 1 CHECK (status_version >= 1),
        result_json TEXT
            CHECK (result_json IS NULL OR OCTET_LENGTH(result_json) BETWEEN 2 AND 8192),
        error_code VARCHAR(64) COLLATE "C",
        created_at_us BIGINT NOT NULL
            CHECK (created_at_us BETWEEN 0 AND 253402300799999999),
        updated_at_us BIGINT NOT NULL
            CHECK (updated_at_us >= created_at_us),
        CHECK ((state = 'succeeded') = (result_json IS NOT NULL)),
        CHECK ((state = 'failed') = (error_code IS NOT NULL)),
        UNIQUE (workspace_id, idempotency_key_digest)
    )""",
    f"""CREATE TABLE {EVENTS} (
        job_id VARCHAR(128) COLLATE "C" NOT NULL
            REFERENCES {JOBS} (job_id) ON DELETE RESTRICT,
        event_seq BIGINT NOT NULL CHECK (event_seq >= 0),
        state VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (state IN ('pending', 'running', 'cancelling', 'succeeded',
                             'failed', 'cancelled', 'expired')),
        occurred_at_us BIGINT NOT NULL
            CHECK (occurred_at_us BETWEEN 0 AND 253402300799999999),
        PRIMARY KEY (job_id, event_seq)
    )""",
    f"CREATE INDEX {JOBS}_workspace_created ON {JOBS} (workspace_id, created_at_us DESC)",
)


def public_job_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provisioning declaration; the runtime role may not delete or truncate."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    prefix, target = f'"{schema}".', f'"{role}"'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        f"GRANT SELECT ON {prefix}{JOBS} TO {target}",
        f"GRANT INSERT ({', '.join(JOB_INSERT_COLUMNS)}) ON {prefix}{JOBS} TO {target}",
        f"GRANT UPDATE (state, status_version, result_json, error_code, updated_at_us) "
        f"ON {prefix}{JOBS} TO {target}",
        f"GRANT SELECT ON {prefix}{EVENTS} TO {target}",
        f"GRANT INSERT ({', '.join(JOB_EVENT_INSERT_COLUMNS)}) ON {prefix}{EVENTS} TO {target}",
        f"GRANT SELECT ON {prefix}{SUBMISSIONS} TO {target}",
        # A succeeded Job projection and its terminal event read the committed
        # result Artifact metadata; object bytes stay behind the artifact routes.
        f"GRANT SELECT ON {prefix}{ARTIFACTS} TO {target}",
        *public_job_cancellation_grants(schema, role),
        *job_cancellation_fence_grants(schema, role),
    )
