"""Append-only publication records; a draft stays private until one exists.

The public catalog reads this table only. Insert-only grants keep published
identities immutable: a later revision number is a new row, never an edit.
"""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

TABLE = "speech_publications"
_IDENTIFIER = r"'^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'"

PUBLICATION_DDL = (
    f"""CREATE TABLE {TABLE} (
        kind VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (kind IN ('profile', 'binding', 'voice')),
        object_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (object_id ~ {_IDENTIFIER}),
        revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (revision ~ '^[1-9][0-9]{{0,18}}$'),
        revision_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (revision_id ~ {_IDENTIFIER}),
        published_at_us BIGINT NOT NULL
            CHECK (published_at_us BETWEEN 0 AND 253402300799999999),
        PRIMARY KEY (kind, object_id, revision)
    )""",
    f"CREATE INDEX {TABLE}_kind_published ON {TABLE} (kind, published_at_us)",
)


def publication_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provisioning declaration; never executed by provider construction."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    target = f'"{role}"'
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        f'GRANT SELECT, INSERT ON "{schema}".{TABLE} TO {target}',
    )
