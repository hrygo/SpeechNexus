"""Reviewed PostgreSQL DDL for the first production coordination unit."""

from __future__ import annotations

import re

from speechnexus_backend.speech.infrastructure.deletion_plan_schema import (
    DELETION_PLAN_INSERT_COLUMNS,
)
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


#: 加在 ``JOB_DDL`` 之后的 canonical 列增量。生产由 alembic ``0064`` 加列,InnoDB 验证
#: schema 由 ``JOB_DDL`` 直接建表,所以需要同一份增量,否则「PG 有列、InnoDB 没有」会让
#: 同一段写入代码只在一边可用——那正是 #1176 段 B1 在 CI 上撞到的东西。
#: 单独成元组而不是改 ``JOB_DDL``:``JOB_DDL`` 被历史迁移 ``0001_jobs.py`` 引用,改它等于
#: 改写已发布迁移的行为。
CANONICAL_JOB_DDL: tuple[str, ...] = (
    "ALTER TABLE coordination_jobs ADD COLUMN bucket_no SMALLINT",
    # 0064 added ``due_at`` in the same way; the writer now fills it, so the InnoDB
    # verification schema must carry the column too or the shared INSERT fails only there.
    "ALTER TABLE coordination_jobs ADD COLUMN due_at TIMESTAMP(6)",
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


def deletion_plan_grants(schema: str, role: str) -> tuple[str, ...]:
    """Privileges the deletion planner adds after ``0058_speech_deletion_plans``.

    Kept out of ``job_runtime_grants`` because that set is also applied to
    historical revisions whose schema predates these tables; provisioning applies
    this set only after a full upgrade.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT SELECT ON "{schema}".speech_asset_objects TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_asset_references TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_deletion_plans TO "{role}"',
        # The artifact and voice_revision snapshots judge live references from
        # the evidence ledger and the immutable published revisions; the draft
        # row carries the current head revision they compare against.
        f'GRANT SELECT ON "{schema}".speech_voice_capability_evidence TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_revisions TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_drafts TO "{role}"',
        # PostgreSQL requires UPDATE privilege for a row lock, not only for an
        # assignment. The planner locks the workspace (FK parent) and the asset
        # row to pin the revision; neither column is assigned, so one column per
        # table is enough. The asset writer holds the same two lock columns.
        f'GRANT UPDATE (metadata_revision) ON "{schema}".speech_workspaces TO "{role}"',
        f'GRANT UPDATE (version) ON "{schema}".speech_assets TO "{role}"',
        f"GRANT INSERT ({', '.join(DELETION_PLAN_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_deletion_plans TO "{role}"',
    )


def deletion_execution_grants(schema: str, role: str) -> tuple[str, ...]:
    """Privileges the deletion executor adds after ``0059_speech_deletion_ledger``.

    Execution removes confirmed content: it expires and deletes an asset through
    the existing state machine (so it needs that machine's columns, not a second
    write path), moves a committed Artifact to the same ``deleted`` record 0054
    writes, deletes the authoritative public Job intent row plus the legacy
    command copy's tombstone, and deletes one unreferenced voice draft revision.
    Those are the only two DELETE relations; it gets no authority over execution,
    settlement, lease or fencing evidence.

    Kept out of ``job_runtime_grants`` for the same reason as the preview set: an
    existing deployment that has not applied this group still boots, and only the
    deletion route answers ``503 deletion_not_configured``.
    """
    from speechnexus_backend.speech.infrastructure.deletion_ledger_schema import (
        ARTIFACT_UPDATE_COLUMNS,
        ASSET_EVENT_INSERT_COLUMNS,
        ASSET_UPDATE_COLUMNS,
        CONTENT_DELETION_INSERT_COLUMNS,
        CONTENT_DELETION_UPDATE_COLUMNS,
        DELETION_DELETE_TABLES,
        DELETION_LEDGER_INSERT_COLUMNS,
        DELETION_LEDGER_UPDATE_COLUMNS,
        LEGACY_INTENT_UPDATE_COLUMNS,
    )

    schema, role = checked_sql_name(schema), checked_sql_name(role)

    def columns(items: tuple[str, ...]) -> str:
        return ", ".join(items)

    return (
        f'GRANT SELECT ON "{schema}".speech_content_deletions TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_capability_evidence TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_revisions TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_drafts TO "{role}"',
        # The asset event append uses ON CONFLICT DO NOTHING, which PostgreSQL
        # authorizes with a read on the conflict key's relation.
        f'GRANT SELECT ON "{schema}".speech_asset_events TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_deletion_ledger TO "{role}"',
        f"GRANT INSERT ({columns(DELETION_LEDGER_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_deletion_ledger TO "{role}"',
        f"GRANT UPDATE ({columns(DELETION_LEDGER_UPDATE_COLUMNS)}) "
        f'ON "{schema}".speech_deletion_ledger TO "{role}"',
        f"GRANT INSERT ({columns(CONTENT_DELETION_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_content_deletions TO "{role}"',
        f"GRANT UPDATE ({columns(CONTENT_DELETION_UPDATE_COLUMNS)}) "
        f'ON "{schema}".speech_content_deletions TO "{role}"',
        f"GRANT INSERT ({columns(ASSET_EVENT_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_asset_events TO "{role}"',
        f'GRANT UPDATE ({columns(ASSET_UPDATE_COLUMNS)}) ON "{schema}".speech_assets TO "{role}"',
        f"GRANT UPDATE ({columns(ARTIFACT_UPDATE_COLUMNS)}) "
        f'ON "{schema}".speech_artifacts TO "{role}"',
        # Row lock only: execution takes FOR UPDATE on the draft to serialize with
        # admission's FOR SHARE before it re-reads the revision dependencies. The
        # column is never assigned by this route.
        f'GRANT UPDATE ("head_revision") ON "{schema}".speech_voice_drafts TO "{role}"',
        f"GRANT UPDATE ({columns(LEGACY_INTENT_UPDATE_COLUMNS)}) "
        f'ON "{schema}".admission_operations TO "{role}"',
        *(f'GRANT DELETE ON "{schema}".{table} TO "{role}"' for table in DELETION_DELETE_TABLES),
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
