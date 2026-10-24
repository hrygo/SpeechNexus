"""A-owned draft allocation and immutable command/revision history (PostgreSQL)."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

PROFILE_DRAFT_DDL = (
    """CREATE TABLE speech_profile_families (
        profile_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        latest_revision VARCHAR(19) COLLATE "C"
    )""",
    """CREATE TABLE speech_profile_revisions (
        profile_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        definition_json TEXT NOT NULL CHECK (OCTET_LENGTH(definition_json) BETWEEN 1 AND 262144),
        definition_digest VARCHAR(71) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
        PRIMARY KEY (profile_id, revision),
        UNIQUE (profile_id, revision, request_digest),
        FOREIGN KEY (profile_id) REFERENCES speech_profile_families (profile_id)
    )""",
    """CREATE TABLE speech_profile_commands (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY,
        profile_id VARCHAR(128) COLLATE "C" NOT NULL,
        intent_json TEXT NOT NULL CHECK (OCTET_LENGTH(intent_json) BETWEEN 1 AND 262144),
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        command_state VARCHAR(16) NOT NULL
            CHECK (command_state IN ('pending','created','rejected')),
        revision VARCHAR(19) COLLATE "C",
        error_code VARCHAR(32),
        FOREIGN KEY (profile_id, revision, request_digest)
            REFERENCES speech_profile_revisions (profile_id, revision, request_digest),
        CHECK ((command_state = 'created' AND revision IS NOT NULL AND error_code IS NULL)
            OR (command_state = 'rejected' AND revision IS NULL
                AND error_code = 'configuration_changed')
            OR (command_state = 'pending' AND revision IS NULL AND error_code IS NULL))
    )""",
)


def profile_draft_grants(schema: str, role: str) -> tuple[str, ...]:
    """Declaration for explicit provisioning; never applied by an application."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    prefix = f'"{schema}".'
    target = f'"{role}"'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        *(
            f"GRANT SELECT, INSERT ON {prefix}{table} TO {target}"
            for table in (
                "speech_profile_families",
                "speech_profile_revisions",
                "speech_profile_commands",
            )
        ),
        f"GRANT UPDATE (latest_revision) ON {prefix}speech_profile_families TO {target}",
        f"GRANT UPDATE (command_state, revision, error_code) ON {prefix}speech_profile_commands "
        f"TO {target}",
    )
