"""Frozen 0003 asset storage; content stays in private versioned object storage."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

ASSET_DDL = (
    """CREATE TABLE speech_workspaces (
        workspace_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        display_name VARCHAR(128) NOT NULL,
        workspace_state VARCHAR(16) NOT NULL CHECK (workspace_state IN ('active', 'disabled')),
        metadata_revision BIGINT NOT NULL DEFAULT 1 CHECK (metadata_revision >= 1),
        policy_revision BIGINT NOT NULL DEFAULT 1 CHECK (policy_revision >= 1),
        created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0)
    )""",
    """INSERT INTO speech_workspaces (workspace_id, display_name, workspace_state, created_at_us)
       VALUES ('workspace-default', 'Shared workspace', 'active',
               CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT))""",
    """CREATE TABLE speech_assets (
        asset_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL REFERENCES speech_workspaces (workspace_id),
        purpose VARCHAR(32) NOT NULL CHECK
            (purpose IN ('asr_input','voice_reference','text_input')),
        asset_state VARCHAR(16) NOT NULL CHECK
            (asset_state IN ('staging','ready','rejected','expired','deleted')),
        upload_phase VARCHAR(16) NOT NULL DEFAULT 'reserved' CHECK
            (upload_phase IN ('reserved','writing','settled')),
        bytes BIGINT NOT NULL CHECK (bytes >= 0),
        sha256 VARCHAR(71) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        object_key VARCHAR(256) COLLATE "C" NOT NULL UNIQUE,
        media_type VARCHAR(128) NOT NULL,
        duration_ms BIGINT CHECK (duration_ms >= 1),
        sample_rate_hz BIGINT CHECK (sample_rate_hz BETWEEN 8000 AND 192000),
        channels BIGINT CHECK (channels BETWEEN 1 AND 8),
        created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
        expires_at_us BIGINT NOT NULL CHECK (expires_at_us > created_at_us),
        writer_id VARCHAR(128) COLLATE "C",
        error_code VARCHAR(64),
        version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
        reconcile_polled_at_us BIGINT NOT NULL DEFAULT 0 CHECK (reconcile_polled_at_us >= 0),
        expiry_polled_at_us BIGINT NOT NULL DEFAULT 0 CHECK (expiry_polled_at_us >= 0),
        UNIQUE (asset_id, workspace_id, request_digest),
        UNIQUE (asset_id, object_key, bytes, sha256),
        UNIQUE (asset_id, sha256),
        CHECK (upload_phase <> 'writing' OR writer_id IS NOT NULL),
        CHECK (asset_state <> 'ready' OR (bytes > 0 AND upload_phase = 'settled'
            AND error_code IS NULL)),
        CHECK (asset_state <> 'ready' OR purpose = 'text_input' OR
            (duration_ms IS NOT NULL AND sample_rate_hz IS NOT NULL AND channels IS NOT NULL)),
        CHECK (purpose <> 'text_input' OR
            (duration_ms IS NULL AND sample_rate_hz IS NULL AND channels IS NULL))
    )""",
    """CREATE INDEX idx_assets_expiry ON speech_assets (asset_state, expires_at_us)""",
    """CREATE INDEX idx_assets_reconcile_poll
       ON speech_assets (reconcile_polled_at_us, expires_at_us, asset_id)""",
    """CREATE INDEX idx_assets_expiry_poll
       ON speech_assets (expiry_polled_at_us, expires_at_us, asset_id)""",
    """CREATE TABLE speech_asset_commands (
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL,
        key_digest VARCHAR(71) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        asset_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
        PRIMARY KEY (workspace_id, key_digest),
        FOREIGN KEY (asset_id, workspace_id, request_digest)
            REFERENCES speech_assets (asset_id, workspace_id, request_digest)
    )""",
    """CREATE TABLE speech_asset_objects (
        asset_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        object_key VARCHAR(256) COLLATE "C" NOT NULL,
        storage_version_id VARCHAR(1024) COLLATE "C" NOT NULL,
        bytes BIGINT NOT NULL CHECK (bytes > 0),
        sha256 VARCHAR(71) COLLATE "C" NOT NULL,
        writer_id VARCHAR(128) COLLATE "C" NOT NULL,
        verified_at_us BIGINT NOT NULL CHECK (verified_at_us >= 0),
        UNIQUE (asset_id, storage_version_id),
        FOREIGN KEY (asset_id, object_key, bytes, sha256)
            REFERENCES speech_assets (asset_id, object_key, bytes, sha256)
    )""",
    """CREATE TABLE speech_asset_references (
        reference_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        asset_id VARCHAR(128) COLLATE "C" NOT NULL REFERENCES speech_assets (asset_id),
        consumer_kind VARCHAR(32) NOT NULL,
        consumer_id VARCHAR(128) COLLATE "C" NOT NULL,
        consumer_revision VARCHAR(19) COLLATE "C" NOT NULL,
        created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
        released_at_us BIGINT CHECK (released_at_us >= created_at_us),
        UNIQUE (asset_id, consumer_kind, consumer_id, consumer_revision)
    )""",
    """CREATE INDEX idx_asset_references_live
       ON speech_asset_references (asset_id, released_at_us)""",
    """CREATE TABLE speech_content_deletions (
        ledger_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        asset_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE,
        sha256 VARCHAR(71) COLLATE "C" NOT NULL,
        storage_version_id VARCHAR(1024) COLLATE "C",
        reason VARCHAR(32) NOT NULL CHECK (reason = 'retention_expired'),
        deletion_state VARCHAR(16) NOT NULL CHECK
            (deletion_state IN ('queued','blocked','complete')),
        blocked_reason VARCHAR(64),
        created_at_us BIGINT NOT NULL CHECK (created_at_us >= 0),
        completed_at_us BIGINT CHECK (completed_at_us >= created_at_us),
        FOREIGN KEY (asset_id, sha256) REFERENCES speech_assets (asset_id, sha256),
        FOREIGN KEY (asset_id, storage_version_id)
            REFERENCES speech_asset_objects (asset_id, storage_version_id),
        CHECK ((deletion_state = 'complete') = (completed_at_us IS NOT NULL))
    )""",
    """CREATE TABLE speech_asset_events (
        event_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        asset_id VARCHAR(128) COLLATE "C" NOT NULL REFERENCES speech_assets (asset_id),
        event_kind VARCHAR(32) NOT NULL CHECK
            (event_kind IN
                ('created','rejected','write_started','published','expired','object_deleted')),
        asset_version BIGINT NOT NULL CHECK (asset_version >= 1),
        occurred_at_us BIGINT NOT NULL CHECK (occurred_at_us >= 0),
        UNIQUE (asset_id, event_kind)
    )""",
)


def asset_runtime_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    grants = [f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"']
    tables = (
        "speech_workspaces",
        "speech_asset_storage_authority",
        "speech_assets",
        "speech_asset_commands",
        "speech_asset_objects",
        "speech_asset_references",
        "speech_content_deletions",
        "speech_asset_events",
    )
    for table in tables:
        grants.append(f'GRANT SELECT ON "{schema}"."{table}" TO "{role}"')
        if table not in {"speech_workspaces", "speech_asset_references"}:
            grants.append(f'GRANT INSERT ON "{schema}"."{table}" TO "{role}"')
    grants.extend(
        (
            f'GRANT UPDATE (metadata_revision) ON "{schema}".speech_workspaces TO "{role}"',
            f"GRANT UPDATE (asset_state, upload_phase, writer_id, error_code, version, "
            f"reconcile_polled_at_us, expiry_polled_at_us) "
            f'ON "{schema}".speech_assets TO "{role}"',
            f"GRANT UPDATE (storage_version_id, deletion_state, blocked_reason, completed_at_us) "
            f'ON "{schema}".speech_content_deletions TO "{role}"',
        )
    )
    return tuple(grants)
