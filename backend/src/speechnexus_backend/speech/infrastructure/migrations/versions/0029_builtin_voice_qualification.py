"""Independent builtin voice approvals and immutable publication evidence."""

from alembic import op

revision = "0029_builtin_voice_qualification"
down_revision = "0028_execution_release_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE speech_builtin_voice_source_approvals (
        source_approval_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        voice_id VARCHAR(128) COLLATE "C" NOT NULL,
        source_revision TEXT NOT NULL, source_digest TEXT NOT NULL,
        native_speaker_id TEXT NOT NULL CHECK (native_speaker_id = 'Vivian'),
        expires_at TIMESTAMPTZ NOT NULL,
        approved_by NAME NOT NULL DEFAULT CURRENT_USER,
        approved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (source_approval_id, voice_id)
    )""")
    op.execute("""CREATE TABLE speech_builtin_voice_source_revocations (
        source_approval_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES speech_builtin_voice_source_approvals (source_approval_id),
        revoked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000)
    )""")
    op.execute("""CREATE TABLE speech_builtin_voice_approvals (
        approval_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        voice_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        revision_id VARCHAR(128) COLLATE "C" NOT NULL,
        binding_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
        source_approval_id VARCHAR(128) COLLATE "C" NOT NULL,
        facts JSONB NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        approved_by NAME NOT NULL DEFAULT CURRENT_USER,
        approved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (source_approval_id, voice_id)
            REFERENCES speech_builtin_voice_source_approvals (source_approval_id, voice_id),
        UNIQUE (approval_id, voice_id, revision, revision_id, binding_revision_id)
    )""")
    op.execute("""CREATE TABLE speech_builtin_voice_revocations (
        approval_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES speech_builtin_voice_approvals (approval_id),
        revoked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000)
    )""")
    op.execute("""CREATE TABLE speech_builtin_voice_qualifications (
        voice_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        revision_id VARCHAR(128) COLLATE "C" NOT NULL,
        binding_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
        approval_id VARCHAR(128) COLLATE "C" NOT NULL,
        document JSONB NOT NULL,
        native_speaker_id TEXT NOT NULL CHECK (native_speaker_id = 'Vivian'),
        PRIMARY KEY (voice_id, revision),
        FOREIGN KEY (approval_id, voice_id, revision, revision_id, binding_revision_id)
            REFERENCES speech_builtin_voice_approvals
            (approval_id, voice_id, revision, revision_id, binding_revision_id)
    )""")
    op.execute("""CREATE TABLE speech_builtin_voice_publish_intents (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY
            CHECK (key_digest ~ '^sha256:[0-9a-f]{64}$'),
        intent JSONB NOT NULL,
        voice_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        FOREIGN KEY (voice_id, revision)
            REFERENCES speech_builtin_voice_qualifications (voice_id, revision)
    )""")


def downgrade() -> None:
    raise RuntimeError("Builtin voice qualification history requires preservation")
