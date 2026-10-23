"""Frozen 0005 extensions for runtime records and typed durable dispatch targets."""

RUNTIME_INTENT_DDL: tuple[str, ...] = (
    """ALTER TABLE runtime_instance_intents
        ADD COLUMN recorded_at_us BIGINT,
        ADD COLUMN updated_at_us BIGINT,
        ADD COLUMN observation_document TEXT,
        ADD CONSTRAINT runtime_intent_record_times CHECK (
            (recorded_at_us IS NULL AND updated_at_us IS NULL AND observation_document IS NULL)
            OR (recorded_at_us IS NOT NULL AND updated_at_us IS NOT NULL
                AND recorded_at_us BETWEEN 1 AND 9007199254740991
                AND updated_at_us BETWEEN recorded_at_us AND 9007199254740991)
        )""",
    """ALTER TABLE coordination_outbox
        ADD COLUMN event_kind VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'job',
        ADD COLUMN target_node_id VARCHAR(128) COLLATE "C",
        ADD COLUMN intent_id VARCHAR(128) COLLATE "C"
    """,
    "ALTER TABLE coordination_outbox ALTER COLUMN job_id DROP NOT NULL",
    """ALTER TABLE coordination_outbox
        ADD CONSTRAINT outbox_target_shape CHECK (
            (event_kind = 'job' AND job_id IS NOT NULL
                AND target_node_id IS NULL AND intent_id IS NULL)
            OR (event_kind = 'runtime' AND job_id IS NULL
                AND target_node_id IS NOT NULL AND intent_id IS NOT NULL)
        ),
        ADD CONSTRAINT outbox_runtime_target FOREIGN KEY (target_node_id, intent_id)
            REFERENCES runtime_instance_intents (target_node_id, intent_id)""",
    """CREATE INDEX idx_outbox_pending_kind
        ON coordination_outbox (event_kind, delivered_at_us, event_id)""",
)
