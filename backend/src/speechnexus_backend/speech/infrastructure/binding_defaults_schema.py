"""Append-only A-owned default snapshots and explicit original declaration facts."""

from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

PARENT_KEY = "uq_speech_binding_defaults_parent"
TABLE = "speech_binding_defaults"
BINDING_DEFAULTS_DDL = (
    "ALTER TABLE speech_binding_commands ADD COLUMN defaults_declared BOOLEAN",
    f"ALTER TABLE speech_binding_revisions ADD CONSTRAINT {PARENT_KEY} "
    "UNIQUE (binding_id,revision,revision_id,key_digest,request_digest,"
    "document_version,created_at_us)",
    f"""CREATE TABLE {TABLE} (
        snapshot_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        binding_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        revision_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
        key_digest VARCHAR(71) COLLATE "C" NOT NULL UNIQUE,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        document_version SMALLINT NOT NULL CHECK (document_version = 1),
        schema_version VARCHAR(16) COLLATE "C" NOT NULL CHECK (schema_version = '1.0.0'),
        parameters_json TEXT NOT NULL CHECK (OCTET_LENGTH(parameters_json) BETWEEN 1 AND 262144),
        parameters_digest VARCHAR(71) COLLATE "C" NOT NULL,
        record_digest VARCHAR(71) COLLATE "C" NOT NULL,
        created_at_us BIGINT NOT NULL CHECK (created_at_us BETWEEN 0 AND 253402300799999999),
        UNIQUE (binding_id, revision),
        FOREIGN KEY (binding_id,revision,revision_id,key_digest,request_digest,
                     document_version,created_at_us)
            REFERENCES speech_binding_revisions
            (binding_id,revision,revision_id,key_digest,request_digest,document_version,created_at_us)
    )""",
)


def binding_defaults_grants(schema: str, role: str) -> tuple[str, ...]:
    """A provisioning declaration; never executed by provider construction/startup."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        *binding_draft_grants(schema, role),
        f'GRANT SELECT, INSERT ON "{schema}".{TABLE} TO "{role}"',
    )
