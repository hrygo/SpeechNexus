"""Reviewed PostgreSQL DDL for the first production coordination unit."""

from __future__ import annotations

import re

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
        "GRANT INSERT (job_id, operation_id, attempt_id, initial_event_id, "
        f'initial_event_digest, request_digest) ON "{schema}".coordination_jobs TO "{role}"',
        "GRANT INSERT (event_id, job_id, event_digest) "
        f'ON "{schema}".coordination_outbox TO "{role}"',
    )


def job_dispatch_grants(schema: str, role: str) -> tuple[str, ...]:
    """Privileges the dispatcher adds on top of the base Job writer.

    Kept separate because ``admission_operations`` only exists from migration
    ``0007_admission_commands`` onwards, while ``job_runtime_grants`` is also
    applied to historical revisions (the upgrade sandbox starts at
    ``0001_jobs``). Provisioning applies this set after a full upgrade.
    """

    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        "GRANT INSERT (operation_id, workspace_id, request_digest, request_document, "
        f"initial_attempt_id, current_attempt_id, recorded_at_us) "
        f'ON "{schema}".admission_operations TO "{role}"',
    )
