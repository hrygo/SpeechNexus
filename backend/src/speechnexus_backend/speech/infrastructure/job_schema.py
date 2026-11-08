"""Reviewed PostgreSQL DDL for the first production coordination unit."""

from __future__ import annotations

import re

from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    ADMISSION_OPERATION_INSERT_COLUMNS,
    COORDINATION_JOB_INSERT_COLUMNS,
    COORDINATION_OUTBOX_INSERT_COLUMNS,
    JOB_INTENT_INSERT_COLUMNS,
)

JOB_DDL: tuple[str, ...] = (
    """CREATE TABLE coordination_jobs (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        operation_id VARCHAR(128) COLLATE "C" NOT NULL,
        attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
        initial_event_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
        initial_event_digest VARCHAR(71) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        claim_owner_id VARCHAR(128) COLLATE "C",
        claim_expires_at_us BIGINT NOT NULL DEFAULT 0 CHECK (claim_expires_at_us >= 0),
        claim_version BIGINT NOT NULL DEFAULT 0 CHECK (claim_version >= 0),
        job_state VARCHAR(24) NOT NULL DEFAULT 'pending',
        UNIQUE (operation_id, attempt_id)
    )""",
    """CREATE TABLE coordination_outbox (
        event_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        job_id VARCHAR(128) COLLATE "C" NOT NULL,
        event_digest VARCHAR(71) COLLATE "C" NOT NULL,
        delivered_at_us BIGINT CHECK (delivered_at_us >= 0),
        FOREIGN KEY (job_id) REFERENCES coordination_jobs (job_id)
    )""",
)


def checked_sql_name(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", value):
        raise ValueError("Invalid database object name")
    return value


def job_runtime_grants(schema: str, role: str) -> tuple[str, ...]:
    """Apply as migration owner to a fresh non-owner role; no broad inherited grants.

    Role provisioning/authentication belongs to deployment, not application startup.
    No DDL, claim UPDATE, delivery UPDATE, DELETE, or control-plane writes are granted.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".coordination_jobs TO "{role}"',
        f'GRANT SELECT ON "{schema}".coordination_outbox TO "{role}"',
        f"GRANT INSERT ({', '.join(COORDINATION_JOB_INSERT_COLUMNS)}) "
        f'ON "{schema}".coordination_jobs TO "{role}"',
        f"GRANT INSERT ({', '.join(COORDINATION_OUTBOX_INSERT_COLUMNS)}) "
        f'ON "{schema}".coordination_outbox TO "{role}"',
    )


def job_dispatch_grants(schema: str, role: str) -> tuple[str, ...]:
    """Privileges the dispatcher adds on top of the base Job writer.

    Kept separate because ``admission_operations`` only exists from migration
    ``0007_admission_commands`` and ``speech_job_intents`` only from
    ``0046_public_job_intents``, while ``job_runtime_grants`` is also applied to
    historical revisions (the upgrade sandbox starts at ``0001_jobs``).
    Provisioning applies this set after a full upgrade.
    """

    from speechnexus_backend.speech.infrastructure.artifact_schema import (
        artifact_publisher_grants,
    )
    from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
        cancellation_not_submitted_grants,
    )

    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        # Legacy voice kinds still record their non-dispatchable public intent
        # here. TTS/ASR operations are written by the admission role.
        f"GRANT INSERT ({', '.join(ADMISSION_OPERATION_INSERT_COLUMNS)}) "
        f'ON "{schema}".admission_operations TO "{role}"',
        # The public intent is authoritative from 0046 on and stays append-only.
        f'GRANT SELECT ON "{schema}".speech_job_intents TO "{role}"',
        f"GRANT INSERT ({', '.join(JOB_INTENT_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_job_intents TO "{role}"',
        *artifact_publisher_grants(schema, role),
        *cancellation_not_submitted_grants(schema, role),
    )
