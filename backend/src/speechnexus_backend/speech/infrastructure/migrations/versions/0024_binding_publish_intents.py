"""Persist publication intents and a stable schema installation identity."""

from alembic import op

revision = "0024_binding_publish_intents"
down_revision = "0023_publication_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE speech_publication_installation ("
        "singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton), "
        "installation_id UUID NOT NULL UNIQUE)"
    )
    op.execute(
        "INSERT INTO speech_publication_installation (singleton, installation_id) "
        "VALUES (TRUE, gen_random_uuid())"
    )
    op.execute(
        "CREATE TABLE speech_binding_publish_intents ("
        'key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY '
        "CHECK (key_digest ~ '^sha256:[0-9a-f]{64}$'), "
        "intent JSONB NOT NULL, "
        "kind VARCHAR(16) COLLATE \"C\" NOT NULL DEFAULT 'binding' CHECK (kind = 'binding'), "
        'binding_id VARCHAR(128) COLLATE "C" NOT NULL, '
        'revision VARCHAR(19) COLLATE "C" NOT NULL, '
        "original_created BOOLEAN NOT NULL, "
        "FOREIGN KEY (kind, binding_id, revision) "
        "REFERENCES speech_publications (kind, object_id, revision))"
    )


def downgrade() -> None:
    raise RuntimeError("Publication intent and installation identity require preservation")
