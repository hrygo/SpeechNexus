"""Persist public Job cancellation acceptance and the cancelling state."""

from alembic import op
from sqlalchemy import text

from speechnexus_backend.speech.infrastructure.public_job_cancellation_schema import (
    PUBLIC_JOB_CANCELLATION_DDL,
    TABLE,
)
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    PUBLIC_JOB_STATUS_GUARD_REPLACE,
)

revision = "0034_public_job_cancellations"
down_revision = "0033_profile_publications"
branch_labels = None
depends_on = None

_NEW_STATES = "'pending', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled', 'expired'"
_OLD_STATES = "'pending', 'running', 'succeeded', 'failed', 'cancelled', 'expired'"

_OLD_STATUS_GUARD = """CREATE OR REPLACE FUNCTION speech_jobs_status_guard() RETURNS trigger
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
          (OLD.state = 'pending' AND NEW.state IN ('running', 'failed', 'cancelled', 'expired'))
          OR (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed', 'cancelled'))
     ) THEN
       RAISE EXCEPTION 'public Job state transition is not allowed' USING ERRCODE = '23514';
     END IF;
     RETURN NEW;
   END
   $$"""


def upgrade() -> None:
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT IF EXISTS speech_jobs_state_check")
    op.execute(
        "ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_state_check "
        f"CHECK (state IN ({_NEW_STATES}))"
    )
    op.execute(
        "ALTER TABLE speech_job_events DROP CONSTRAINT IF EXISTS speech_job_events_state_check"
    )
    op.execute(
        "ALTER TABLE speech_job_events ADD CONSTRAINT speech_job_events_state_check "
        f"CHECK (state IN ({_NEW_STATES}))"
    )
    op.execute(PUBLIC_JOB_STATUS_GUARD_REPLACE)
    for statement in PUBLIC_JOB_CANCELLATION_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    # Freeze both sides before deciding whether the older shape can represent
    # the committed facts; a concurrent writer must not appear after the check.
    op.execute("LOCK TABLE speech_jobs, speech_job_events IN ACCESS EXCLUSIVE MODE")
    if connection.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar_one():
        raise RuntimeError("Public Job cancellation history requires preservation")
    if connection.execute(
        text("SELECT count(*) FROM speech_jobs WHERE state = 'cancelling'")
    ).scalar_one():
        raise RuntimeError("Public Job cancellation state requires reconciliation")
    op.execute(f"DROP TABLE {TABLE}")
    op.execute("ALTER TABLE speech_jobs DROP CONSTRAINT IF EXISTS speech_jobs_state_check")
    op.execute(
        "ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_state_check "
        f"CHECK (state IN ({_OLD_STATES}))"
    )
    op.execute(
        "ALTER TABLE speech_job_events DROP CONSTRAINT IF EXISTS speech_job_events_state_check"
    )
    op.execute(
        "ALTER TABLE speech_job_events ADD CONSTRAINT speech_job_events_state_check "
        f"CHECK (state IN ({_OLD_STATES}))"
    )
    op.execute(_OLD_STATUS_GUARD)
