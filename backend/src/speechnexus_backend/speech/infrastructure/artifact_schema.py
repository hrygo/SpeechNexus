"""Committed public Artifact registry; object bytes remain private and version pinned."""

ARTIFACTS = "speech_artifacts"

ARTIFACT_DDL = (
    """CREATE TABLE speech_artifacts (
        artifact_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL
            REFERENCES speech_workspaces (workspace_id),
        artifact_kind VARCHAR(32) COLLATE "C" NOT NULL CHECK
            (artifact_kind IN
                ('transcript','audio','preview','validation_report','export_manifest')),
        artifact_state VARCHAR(16) COLLATE "C" NOT NULL CHECK
            (artifact_state IN ('ready','expired','deleted')),
        media_type VARCHAR(128) NOT NULL CHECK
            (media_type ~ '^[!-~]([ -~]{0,126}[!-~])?$'),
        bytes BIGINT NOT NULL CHECK (bytes > 0),
        sha256 VARCHAR(71) COLLATE "C" NOT NULL CHECK
            (sha256 ~ '^sha256:[0-9a-f]{64}$'),
        receipt_id VARCHAR(128) COLLATE "C",
        source_operation_id VARCHAR(128) COLLATE "C",
        object_key VARCHAR(512) COLLATE "C" NOT NULL UNIQUE CHECK
            (OCTET_LENGTH(object_key) BETWEEN 1 AND 512),
        storage_version_id VARCHAR(1024) COLLATE "C" NOT NULL CHECK
            (storage_version_id <> 'null' AND storage_version_id !~ '[[:cntrl:]]'),
        created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
        expires_at_us BIGINT CHECK (expires_at_us > created_at_us),
        CHECK (receipt_id IS NULL OR receipt_id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'),
        CHECK (source_operation_id IS NULL OR
            source_operation_id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$')
    )""",
    """CREATE INDEX speech_artifacts_workspace_created
       ON speech_artifacts (workspace_id, created_at_us DESC, artifact_id)""",
)


def artifact_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provisioning declaration for a read-only public Artifact principal."""
    from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{ARTIFACTS}, "{schema}".speech_workspaces TO "{role}"',
    )


def artifact_publisher_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provisioning declaration for the dispatcher's committed Artifact writes."""
    from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

    schema, role = checked_sql_name(schema), checked_sql_name(role)
    columns = (
        "artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
        "sha256, receipt_id, source_operation_id, object_key, storage_version_id, "
        "created_at_us, expires_at_us"
    )
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{ARTIFACTS} TO "{role}"',
        f'GRANT INSERT ({columns}) ON "{schema}".{ARTIFACTS} TO "{role}"',
    )


__all__ = ["ARTIFACTS", "ARTIFACT_DDL", "artifact_publisher_grants", "artifact_reader_grants"]
