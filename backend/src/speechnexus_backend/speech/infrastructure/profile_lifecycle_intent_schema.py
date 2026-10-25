"""A-owned append-only original intents, outside the shared coordination grants."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

PROFILE_LIFECYCLE_INTENT_DDL = (
    """CREATE TABLE speech_profile_lifecycle_intents (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY,
        operation VARCHAR(8) NOT NULL CHECK (operation IN ('publish','activate')),
        profile_id VARCHAR(128) COLLATE "C" NOT NULL,
        revision VARCHAR(19) COLLATE "C" NOT NULL,
        intent_json TEXT NOT NULL CHECK (OCTET_LENGTH(intent_json) BETWEEN 1 AND 262144),
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        recorded_at_us BIGINT NOT NULL CHECK (recorded_at_us >= 0)
    )""",
)


def profile_lifecycle_intent_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provisioning declaration only. No UPDATE, DELETE, DDL or publication privilege."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_profile_lifecycle_intents TO "{role}"',
    )
