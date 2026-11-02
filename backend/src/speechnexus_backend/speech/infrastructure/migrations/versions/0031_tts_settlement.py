"""Durable non-content roots for synchronous TTS lease settlement."""

from alembic import op

revision = "0031_tts_settlement"
down_revision = "0030_builtin_voice_qual_admit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE speech_tts_cancellation (
        operation_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        reason VARCHAR(32) COLLATE "C" NOT NULL CHECK
            (reason IN ('client_cancelled', 'stream_disconnected', 'deadline_exceeded')),
        requested_at_us BIGINT NOT NULL CHECK (requested_at_us > 0)
    )""")
    op.execute("""CREATE TABLE speech_tts_settlement (
        lease_id VARCHAR(128) COLLATE "C" PRIMARY KEY REFERENCES admission_leases (lease_id),
        operation_id VARCHAR(128) COLLATE "C" NOT NULL,
        attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
        submit_intent_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
        target_document TEXT NOT NULL,
        deadline_at_us BIGINT NOT NULL CHECK (deadline_at_us > 0),
        abort_reason VARCHAR(32) COLLATE "C" CHECK
            (abort_reason IN ('client_cancelled', 'deadline_exceeded', 'stream_disconnected')),
        next_check_at_us BIGINT NOT NULL DEFAULT 0 CHECK (next_check_at_us >= 0),
        settled_at_us BIGINT CHECK (settled_at_us > 0),
        FOREIGN KEY (operation_id, attempt_id)
            REFERENCES admission_attempts (operation_id, attempt_id)
    )""")
    op.execute(
        "CREATE INDEX speech_tts_settlement_operation ON speech_tts_settlement (operation_id)"
    )
    op.execute(
        "CREATE INDEX speech_tts_settlement_due ON speech_tts_settlement "
        "(next_check_at_us, lease_id) WHERE settled_at_us IS NULL"
    )


def downgrade() -> None:
    raise RuntimeError("TTS settlement history requires preservation")
