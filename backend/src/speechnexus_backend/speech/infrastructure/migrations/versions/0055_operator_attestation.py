"""Operator-attested sound qualifications and their recorded provenance.

Adds facts only: no existing qualification, approval or publication row is altered
or backfilled. A qualification published before this revision has no provenance row
and is read as ``legacy_pre_0055``; from this revision on, every published builtin
qualification records which of the two producers created it.
"""

from alembic import op
from sqlalchemy import text

revision = "0055_operator_attestation"
down_revision = "0054_artifact_deleted_ledger"
branch_labels = None
depends_on = None

_DIGEST = r"^sha256:[0-9a-f]{64}$"


def upgrade() -> None:
    op.execute(f"""CREATE TABLE speech_builtin_voice_operator_attestations (
        attestation_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        approval_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE
            REFERENCES speech_builtin_voice_approvals (approval_id),
        voice_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        revision_id VARCHAR(128) COLLATE "C" NOT NULL,
        binding_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
        source_approval_id VARCHAR(128) COLLATE "C" NOT NULL,
        instance_id VARCHAR(128) COLLATE "C" NOT NULL,
        instance_generation BIGINT NOT NULL CHECK (instance_generation > 0),
        fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
        host_node_id VARCHAR(128) COLLATE "C" NOT NULL,
        gateway_id VARCHAR(128) COLLATE "C" NOT NULL,
        pool_id VARCHAR(128) COLLATE "C" NOT NULL,
        policy_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
        gateway_policy_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (gateway_policy_digest ~ '{_DIGEST}'),
        template_id VARCHAR(128) COLLATE "C" NOT NULL,
        image_digest VARCHAR(71) COLLATE "C" NOT NULL CHECK (image_digest ~ '{_DIGEST}'),
        template_digest VARCHAR(71) COLLATE "C" NOT NULL CHECK (template_digest ~ '{_DIGEST}'),
        controller_artifact_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (controller_artifact_digest ~ '{_DIGEST}'),
        model_id TEXT NOT NULL,
        model_revision TEXT NOT NULL,
        model_artifact_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (model_artifact_digest ~ '{_DIGEST}'),
        observed_at_us BIGINT NOT NULL CHECK (observed_at_us > 0),
        expires_at_us BIGINT NOT NULL CHECK (expires_at_us > observed_at_us),
        declaration JSONB NOT NULL,
        declaration_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (declaration_digest ~ '{_DIGEST}'),
        attestation_intent JSONB NOT NULL,
        declared_by NAME NOT NULL DEFAULT CURRENT_USER,
        declared_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (source_approval_id, voice_id)
            REFERENCES speech_builtin_voice_source_approvals (source_approval_id, voice_id),
        UNIQUE (voice_id, revision, revision_id, binding_revision_id),
        UNIQUE (attestation_id, voice_id, revision, revision_id, binding_revision_id)
    )""")
    op.execute("""CREATE TABLE speech_builtin_voice_qualification_provenance (
        voice_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        source_kind TEXT NOT NULL
            CHECK (source_kind IN ('controller_observed', 'operator_attested')),
        attestation_id VARCHAR(128) COLLATE "C"
            REFERENCES speech_builtin_voice_operator_attestations (attestation_id),
        recorded_by NAME NOT NULL DEFAULT CURRENT_USER,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (voice_id, revision),
        FOREIGN KEY (voice_id, revision)
            REFERENCES speech_builtin_voice_qualifications (voice_id, revision),
        CHECK ((source_kind = 'operator_attested') = (attestation_id IS NOT NULL))
    )""")


def downgrade() -> None:
    # Dropping the provenance table forgets which source produced every published
    # qualification. That is only safe while no operator attestation exists: an
    # attested approval would otherwise survive as an unattributable fact.
    op.execute("LOCK TABLE speech_builtin_voice_operator_attestations IN ACCESS EXCLUSIVE MODE")
    connection = op.get_bind()
    if connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM speech_builtin_voice_operator_attestations)")
    ).scalar_one():
        raise RuntimeError("Operator attestation history requires preservation")
    op.execute("DROP TABLE speech_builtin_voice_qualification_provenance")
    op.execute("DROP TABLE speech_builtin_voice_operator_attestations")
