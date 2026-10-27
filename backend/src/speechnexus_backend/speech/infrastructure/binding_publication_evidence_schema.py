"""Evidence table for published binding revisions; provisioning stays explicit.

The table references the publication row it qualifies, so evidence cannot exist
without a publication. Grants are declared here for deployment provisioning and
are never applied by application startup.
"""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

TABLE = "speech_binding_publication_evidence"
INDEX = TABLE + "_family_recorded"
FOREIGN_KIND = "binding"
_IDENTIFIER = r"'^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'"
_DIGEST = r"'^sha256:[0-9a-f]{64}$'"
_SOURCES = "'operator_cli', 'publish_http', 'backfill'"

PUBLICATION_EVIDENCE_DDL = (
    f"""CREATE TABLE {TABLE} (
        kind VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (kind = 'binding'),
        object_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (object_id ~ {_IDENTIFIER}),
        revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (revision ~ '^[1-9][0-9]{{0,18}}$'),
        revision_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (revision_id ~ {_IDENTIFIER}),
        capacity_manifest_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (capacity_manifest_id ~ {_IDENTIFIER}),
        qualification_report_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (qualification_report_id ~ {_IDENTIFIER}),
        evidence_source VARCHAR(32) COLLATE "C" NOT NULL
            CHECK (evidence_source IN ({_SOURCES})),
        recorded_at_us BIGINT NOT NULL
            CHECK (recorded_at_us BETWEEN 0 AND 253402300799999999),
        capacity_manifest_digest VARCHAR(71) COLLATE "C"
            CHECK (capacity_manifest_digest IS NULL
                   OR capacity_manifest_digest ~ {_DIGEST}),
        qualification_report_digest VARCHAR(71) COLLATE "C"
            CHECK (qualification_report_digest IS NULL
                   OR qualification_report_digest ~ {_DIGEST}),
        PRIMARY KEY (kind, object_id, revision),
        FOREIGN KEY (kind, object_id, revision)
            REFERENCES speech_publications (kind, object_id, revision)
    )""",
    f"CREATE INDEX {INDEX} ON {TABLE} (kind, object_id, recorded_at_us)",
)

# Relations an admin GET needs: the immutable draft read joins revisions to
# commands, and the projected state joins publications to this evidence table.
ADMIN_READER_TABLES = (
    "speech_binding_families",
    "speech_binding_revisions",
    "speech_binding_commands",
    "speech_publications",
    TABLE,
)


def publication_evidence_grants(schema: str, role: str) -> tuple[str, ...]:
    """Publication writer: may record evidence, never rewrite a publication."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{TABLE} TO "{role}"',
    )


def binding_admin_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """Admin GET reader: SELECT only, never the public catalogue reader."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    tables = ", ".join(f'"{schema}".{table}' for table in ADMIN_READER_TABLES)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON {tables} TO "{role}"',
    )
