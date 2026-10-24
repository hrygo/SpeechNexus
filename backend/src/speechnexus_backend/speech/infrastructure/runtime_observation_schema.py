"""Frozen 0006 private provenance for gateway-qualified runtime readiness."""

RUNTIME_OBSERVATION_DDL: tuple[str, ...] = (
    """ALTER TABLE runtime_instance_intents
        ADD COLUMN readiness_document TEXT,
        ADD CONSTRAINT runtime_readiness_has_observation CHECK (
            readiness_document IS NULL OR (action = 'prepare'
                AND recorded_at_us IS NOT NULL AND observation_document IS NOT NULL)
        )""",
)
