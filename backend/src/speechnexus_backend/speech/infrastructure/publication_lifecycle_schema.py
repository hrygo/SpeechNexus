"""Append-only lifecycle evidence and the mutable selected-revision pointer."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    TABLE as EVIDENCE_TABLE,
)
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import (
    RETIREMENT_TABLE,
    STATE_TABLE,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.publication_schema import TABLE as PUBLICATION_TABLE

HEAD_TABLE = "speech_publication_heads"
EVENT_TABLE = "speech_publication_lifecycle_events"
DISABLE_INTENT_TABLE = "speech_binding_disable_intents"

_IDENTIFIER = r"'^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'"

LIFECYCLE_DDL = (
    f"""ALTER TABLE {RETIREMENT_TABLE}
    ADD COLUMN mode VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'retire'
        CHECK (mode IN ('retire', 'disable')),
    ADD COLUMN reason TEXT,
    ADD CONSTRAINT {RETIREMENT_TABLE}_disable_reason
        CHECK (mode <> 'disable' OR (reason IS NOT NULL AND length(reason) BETWEEN 1 AND 2000)),
    ADD CONSTRAINT {RETIREMENT_TABLE}_retire_reason
        CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 2000)""",
    f"""CREATE TABLE {HEAD_TABLE} (
        kind VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (kind IN ('profile', 'binding', 'voice', 'pronunciation')),
        object_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (object_id ~ {_IDENTIFIER}),
        revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (revision ~ '^[1-9][0-9]{{0,18}}$'),
        revision_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (revision_id ~ {_IDENTIFIER}),
        updated_at_us BIGINT NOT NULL
            CHECK (updated_at_us BETWEEN 0 AND 253402300799999999),
        PRIMARY KEY (kind, object_id),
        FOREIGN KEY (kind, object_id, revision, revision_id)
            REFERENCES {PUBLICATION_TABLE} (kind, object_id, revision, revision_id)
    )""",
    f"""INSERT INTO {HEAD_TABLE}
    (kind, object_id, revision, revision_id, updated_at_us)
    SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us
    FROM {PUBLICATION_TABLE} AS p
    WHERE NOT EXISTS (
        SELECT 1 FROM {PUBLICATION_TABLE} AS newer
        WHERE newer.kind = p.kind AND newer.object_id = p.object_id
          AND newer.revision::numeric > p.revision::numeric
    )
      AND NOT EXISTS (
        SELECT 1 FROM {RETIREMENT_TABLE} AS retired
        WHERE retired.kind = p.kind AND retired.object_id = p.object_id
          AND retired.revision = p.revision AND retired.revision_id = p.revision_id
    )""",
    f"""CREATE TABLE {EVENT_TABLE} (
        event_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        kind VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'binding' CHECK (kind = 'binding'),
        action VARCHAR(64) COLLATE "C" NOT NULL
            CHECK (action IN ('model_binding.publish', 'model_binding.retire',
                              'model_binding.disable', 'model_binding.activate')),
        binding_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (binding_id ~ {_IDENTIFIER}),
        revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (revision ~ '^[1-9][0-9]{{0,18}}$'),
        revision_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (revision_id ~ {_IDENTIFIER}),
        result_status VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (result_status IN ('published', 'retired', 'disabled')),
        retired_at_us BIGINT
            CHECK (retired_at_us IS NULL OR retired_at_us BETWEEN 0 AND 253402300799999999),
        disabled_reason TEXT
            CHECK (disabled_reason IS NULL OR length(disabled_reason) BETWEEN 1 AND 2000),
        request_id VARCHAR(128) COLLATE "C" NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
        actor_type VARCHAR(32) COLLATE "C" NOT NULL
            CHECK (actor_type IN ('anonymous', 'shared_credential')),
        credential_label VARCHAR(128),
        occurred_at_us BIGINT NOT NULL
            CHECK (occurred_at_us BETWEEN 0 AND 253402300799999999),
        policy_revision BIGINT NOT NULL CHECK (policy_revision >= 1),
        intent_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (intent_digest ~ '^sha256:[0-9a-f]{{64}}$'),
        idempotency_key_digest VARCHAR(71) COLLATE "C"
            CHECK (idempotency_key_digest IS NULL OR
                   idempotency_key_digest ~ '^sha256:[0-9a-f]{{64}}$'),
        change_summary_json JSONB NOT NULL,
        FOREIGN KEY (kind, binding_id, revision, revision_id)
            REFERENCES {PUBLICATION_TABLE} (kind, object_id, revision, revision_id)
    )""",
    f"""CREATE UNIQUE INDEX {EVENT_TABLE}_idempotency
    ON {EVENT_TABLE} (idempotency_key_digest)
    WHERE idempotency_key_digest IS NOT NULL""",
    f"""CREATE INDEX {EVENT_TABLE}_audit_order
    ON {EVENT_TABLE} (occurred_at_us DESC, event_id DESC)""",
    f"""CREATE TABLE {DISABLE_INTENT_TABLE} (
        event_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES {EVENT_TABLE} (event_id)
    )""",
)

LIFECYCLE_DOWNGRADE_MESSAGE = "Publication lifecycle history requires preservation"


def binding_lifecycle_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provision the binding lifecycle writer without granting immutable rewrites."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    target = f'"{role}"'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        f'GRANT SELECT, INSERT, DELETE ON "{schema}".{HEAD_TABLE} TO {target}',
        f"GRANT UPDATE (revision, revision_id, updated_at_us) ON "
        f'"{schema}".{HEAD_TABLE} TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{EVENT_TABLE} TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{DISABLE_INTENT_TABLE} TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{PUBLICATION_TABLE} TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{RETIREMENT_TABLE} TO {target}',
        f'GRANT SELECT ON "{schema}".speech_binding_revisions, '
        f'"{schema}".speech_binding_publication_evidence TO {target}',
        f"GRANT SELECT, UPDATE (revision, generated_at_us) ON "
        f'"{schema}".speech_catalog_state TO {target}',
        # Voice retirement releases the retired revision's material reference in
        # the retirement transaction; the lifecycle writer owns that tombstone and
        # reads only the columns its predicate needs.
        f"GRANT SELECT (consumer_kind, consumer_id, consumer_revision, released_at_us), "
        f'UPDATE (released_at_us) ON "{schema}".speech_asset_references TO {target}',
    )


def binding_publication_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provision the HTTP publication writer without immutable rewrites.

    This is the additive privilege set the publication port needs on top of the
    lifecycle declaration: it records evidence and append-only lifecycle events,
    advances the selected head and the catalogue version, and never receives
    DELETE on the pointer, UPDATE on append-only facts, or any other speech table.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    target = f'"{role}"'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{PUBLICATION_TABLE} TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{EVIDENCE_TABLE} TO {target}',
        f'GRANT SELECT ON "{schema}".speech_binding_revisions, '
        f'"{schema}".{RETIREMENT_TABLE} TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{HEAD_TABLE} TO {target}',
        f"GRANT UPDATE (revision, revision_id, updated_at_us) ON "
        f'"{schema}".{HEAD_TABLE} TO {target}',
        f'GRANT SELECT ON "{schema}".{STATE_TABLE} TO {target}',
        f'GRANT UPDATE (revision, generated_at_us) ON "{schema}".{STATE_TABLE} TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{EVENT_TABLE} TO {target}',
    )


def audit_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{EVENT_TABLE} TO "{role}"',
    )
