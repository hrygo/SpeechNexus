"""Additive profile publication/history storage; provisioning is explicit."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

PROFILE_PUBLICATION_DDL = (
    """CREATE TABLE speech_profile_publications (
        profile_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        validation_json TEXT NOT NULL CHECK (OCTET_LENGTH(validation_json) BETWEEN 1 AND 262144),
        validation_digest VARCHAR(71) COLLATE "C" NOT NULL,
        authority_digest VARCHAR(71) COLLATE "C" NOT NULL,
        published_at_us BIGINT NOT NULL CHECK (published_at_us >= 0),
        status VARCHAR(16) NOT NULL CHECK (status IN ('published','retired','disabled')),
        retired_at_us BIGINT,
        disabled_reason VARCHAR(2000),
        PRIMARY KEY (profile_id,revision),
        FOREIGN KEY (profile_id,revision)
            REFERENCES speech_profile_revisions (profile_id,revision),
        CHECK ((status='published' AND retired_at_us IS NULL AND disabled_reason IS NULL)
            OR (status='retired' AND retired_at_us IS NOT NULL
                AND retired_at_us >= published_at_us AND disabled_reason IS NULL)
            OR (status='disabled' AND disabled_reason IS NOT NULL))
    )""",
    'ALTER TABLE speech_profile_families ADD COLUMN current_revision VARCHAR(19) COLLATE "C"',
    """ALTER TABLE speech_profile_families ADD CONSTRAINT speech_profile_current_publication
        FOREIGN KEY (profile_id,current_revision)
            REFERENCES speech_profile_publications (profile_id,revision)""",
    """CREATE TABLE speech_profile_lifecycle_results (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY
            REFERENCES speech_profile_lifecycle_intents (key_digest),
        error_code VARCHAR(40),
        result_json TEXT,
        result_digest VARCHAR(71) COLLATE "C",
        CHECK ((error_code IS NOT NULL AND result_json IS NULL AND result_digest IS NULL)
            OR (error_code IS NULL AND result_json IS NOT NULL AND result_digest IS NOT NULL
                AND OCTET_LENGTH(result_json) BETWEEN 1 AND 262144))
    )""",
)


def profile_publication_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        *(
            f'GRANT SELECT, INSERT ON "{schema}".{t} TO "{role}"'
            for t in (
                "speech_profile_publications",
                "speech_profile_lifecycle_intents",
                "speech_profile_lifecycle_results",
            )
        ),
        f'GRANT UPDATE (current_revision) ON "{schema}".speech_profile_families TO "{role}"',
        # SELECT ... FOR UPDATE needs UPDATE on at least one column. The intent key is
        # never mutated; this grant only admits the serializing row lock.
        f'GRANT UPDATE (key_digest) ON "{schema}".speech_profile_lifecycle_intents TO "{role}"',
    )


def profile_publication_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """Readers never see command identities; contents remain in the speech owner."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        *(
            f'GRANT SELECT ON "{schema}".{t} TO "{role}"'
            for t in (
                "speech_profile_families",
                "speech_profile_revisions",
                "speech_profile_publications",
            )
        ),
    )
