"""A-owned immutable pronunciation draft storage and explicit provisioning only."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

PRONUNCIATION_DRAFT_DDL = (
    """CREATE TABLE speech_pronunciation_families (
        pronunciation_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        latest_revision VARCHAR(19) COLLATE "C"
    )""",
    """CREATE TABLE speech_pronunciation_revisions (
        pronunciation_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        definition_json TEXT NOT NULL CHECK (OCTET_LENGTH(definition_json) BETWEEN 1 AND 33554432),
        definition_digest VARCHAR(71) COLLATE "C" NOT NULL,
        key_digest VARCHAR(71) COLLATE "C" NOT NULL UNIQUE,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
        PRIMARY KEY (pronunciation_id, revision),
        UNIQUE (pronunciation_id, revision, key_digest, request_digest),
        FOREIGN KEY (pronunciation_id)
            REFERENCES speech_pronunciation_families (pronunciation_id)
    )""",
    """CREATE TABLE speech_pronunciation_commands (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY,
        intent_json TEXT NOT NULL CHECK (OCTET_LENGTH(intent_json) BETWEEN 1 AND 33554432),
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        command_state VARCHAR(16) NOT NULL
            CHECK (command_state IN ('pending','created','rejected')),
        pronunciation_id VARCHAR(128) COLLATE "C",
        revision VARCHAR(19) COLLATE "C",
        error_code VARCHAR(32),
        FOREIGN KEY (pronunciation_id, revision, key_digest, request_digest)
            REFERENCES speech_pronunciation_revisions
                (pronunciation_id, revision, key_digest, request_digest),
        CHECK ((command_state = 'created' AND pronunciation_id IS NOT NULL
                AND revision IS NOT NULL AND error_code IS NULL)
            OR (command_state = 'rejected' AND pronunciation_id IS NULL AND revision IS NULL
                AND error_code = 'configuration_changed')
            OR (command_state = 'pending' AND pronunciation_id IS NULL
                AND revision IS NULL AND error_code IS NULL))
    )""",
)


def pronunciation_draft_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    prefix, target = f'"{schema}".', f'"{role}"'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        *(
            f"GRANT SELECT, INSERT ON {prefix}{table} TO {target}"
            for table in (
                "speech_pronunciation_families",
                "speech_pronunciation_revisions",
                "speech_pronunciation_commands",
            )
        ),
        f"GRANT UPDATE (latest_revision) ON {prefix}speech_pronunciation_families TO {target}",
        f"GRANT UPDATE (command_state, pronunciation_id, revision, error_code) "
        f"ON {prefix}speech_pronunciation_commands TO {target}",
    )
