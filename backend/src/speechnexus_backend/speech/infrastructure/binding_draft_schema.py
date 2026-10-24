"""A-owned configuration history; no shared coordination or publication writes."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

BINDING_DRAFT_DDL = (
    """CREATE TABLE speech_binding_families (
        binding_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        latest_revision VARCHAR(19) COLLATE "C"
    )""",
    """CREATE TABLE speech_binding_commands (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY,
        binding_id VARCHAR(128) COLLATE "C" NOT NULL,
        document_version SMALLINT NOT NULL CHECK (document_version = 1),
        intent_json TEXT NOT NULL CHECK (OCTET_LENGTH(intent_json) BETWEEN 1 AND 262144),
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        UNIQUE (key_digest, binding_id, request_digest, document_version)
    )""",
    """CREATE TABLE speech_binding_revisions (
        binding_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        revision_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
        key_digest VARCHAR(71) COLLATE "C" NOT NULL UNIQUE,
        document_version SMALLINT NOT NULL CHECK (document_version = 1),
        definition_json TEXT NOT NULL CHECK (OCTET_LENGTH(definition_json) BETWEEN 1 AND 262144),
        definition_digest VARCHAR(71) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        created_at_us BIGINT NOT NULL CHECK (created_at_us BETWEEN 0 AND 253402300799999999),
        PRIMARY KEY (binding_id, revision),
        FOREIGN KEY (binding_id) REFERENCES speech_binding_families (binding_id),
        FOREIGN KEY (key_digest, binding_id, request_digest, document_version)
            REFERENCES speech_binding_commands
            (key_digest, binding_id, request_digest, document_version)
    )""",
)


def binding_draft_grants(schema: str, role: str) -> tuple[str, ...]:
    """Explicit provisioning declaration; never executed by application startup."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    prefix, target = f'"{schema}".', f'"{role}"'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        *(
            f"GRANT SELECT, INSERT ON {prefix}{table} TO {target}"
            for table in (
                "speech_binding_families",
                "speech_binding_revisions",
                "speech_binding_commands",
            )
        ),
        f"GRANT UPDATE (latest_revision) ON {prefix}speech_binding_families TO {target}",
    )
