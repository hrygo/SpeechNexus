"""One nullable, bounded private receipt on B's existing execution record."""

EXECUTION_RESULT_DDL = (
    "ALTER TABLE submit_intents ADD COLUMN staging_result_document TEXT",
    """ALTER TABLE submit_intents ADD CONSTRAINT execution_result_shape CHECK (
        staging_result_document IS NULL OR (
            CHAR_LENGTH(staging_result_document) BETWEEN 1 AND 16384
            AND staging_output_object_key IS NOT NULL
        )
    )""",
)
