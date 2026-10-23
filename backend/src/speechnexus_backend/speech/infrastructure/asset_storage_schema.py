"""Frozen 0004 storage-namespace declaration; a fact, never mutable topology."""

ASSET_STORAGE_DDL = (
    """CREATE TABLE speech_asset_storage_authority (
        binding_id SMALLINT PRIMARY KEY CHECK (binding_id = 1),
        namespace_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (namespace_digest ~ '^sha256:[0-9a-f]{64}$'),
        recorded_at_us BIGINT NOT NULL CHECK (recorded_at_us >= 0)
    )""",
)
