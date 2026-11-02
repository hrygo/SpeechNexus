"""Private single-execution qualification authorizations and admission receipts."""

from alembic import op

revision = "0030_builtin_voice_qual_admit"
down_revision = "0029_builtin_voice_qualification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE speech_builtin_voice_probe_authorizations (
        authorization_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        source_approval_id VARCHAR(128) COLLATE "C" NOT NULL,
        voice_id VARCHAR(128) COLLATE "C" NOT NULL,
        intent JSONB NOT NULL,
        approved_by NAME NOT NULL DEFAULT CURRENT_USER,
        approved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (source_approval_id, voice_id)
            REFERENCES speech_builtin_voice_source_approvals (source_approval_id, voice_id)
    )""")
    op.execute("""CREATE TABLE speech_builtin_voice_probe_admissions (
        authorization_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES speech_builtin_voice_probe_authorizations (authorization_id),
        operation_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
        attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        FOREIGN KEY (operation_id, attempt_id)
            REFERENCES admission_attempts (operation_id, attempt_id)
    )""")
    op.execute("""ALTER TABLE speech_builtin_voice_approvals
        ADD COLUMN probe_authorization_id VARCHAR(128) COLLATE "C"
            REFERENCES speech_builtin_voice_probe_authorizations (authorization_id),
        ADD COLUMN result_intent JSONB,
        ADD CONSTRAINT builtin_approval_probe_intent_pair CHECK (
            (probe_authorization_id IS NULL) = (result_intent IS NULL)
        ),
        ADD CONSTRAINT builtin_approval_probe_authorization_unique UNIQUE (probe_authorization_id)
    """)


def downgrade() -> None:
    raise RuntimeError("Qualification probe intent history requires preservation")
