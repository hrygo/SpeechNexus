"""Frozen 0007 private command storage; no admission or retry authorization.

Business and admission-envelope digests have different meanings. Never backfill
either complete command from a digest, a mutable preset, or an old Job row.
"""

ADMISSION_COMMAND_DDL: tuple[str, ...] = (
    """CREATE TABLE admission_operations (
    operation_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
    workspace_id VARCHAR(128) COLLATE "C" NOT NULL,
    request_digest VARCHAR(71) COLLATE "C" NOT NULL,
    request_document TEXT,
    initial_attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    current_attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    recorded_at_us BIGINT NOT NULL CHECK (recorded_at_us BETWEEN 1 AND 9007199254740991),
    command_redacted_at_us BIGINT,
    command_deletion_job_id VARCHAR(128) COLLATE "C",
    command_deletion_evidence_digest VARCHAR(71) COLLATE "C",
    version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT admission_operation_command_shape CHECK (
        (request_document IS NOT NULL AND CHAR_LENGTH(request_document) > 0
            AND command_redacted_at_us IS NULL AND command_deletion_job_id IS NULL
            AND command_deletion_evidence_digest IS NULL)
        OR (request_document IS NULL AND command_redacted_at_us IS NOT NULL
            AND command_redacted_at_us BETWEEN recorded_at_us AND 9007199254740991
            AND command_deletion_job_id IS NOT NULL
            AND CHAR_LENGTH(command_deletion_job_id) > 0
            AND command_deletion_evidence_digest IS NOT NULL)
    )
)""",
    """CREATE TABLE admission_requests (
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    request_digest VARCHAR(71) COLLATE "C" NOT NULL,
    request_document TEXT NOT NULL CHECK (CHAR_LENGTH(request_document) > 0),
    recorded_at_us BIGINT NOT NULL CHECK (recorded_at_us BETWEEN 1 AND 9007199254740991),
    PRIMARY KEY (operation_id, attempt_id),
    FOREIGN KEY (operation_id) REFERENCES admission_operations (operation_id),
    FOREIGN KEY (operation_id, attempt_id)
        REFERENCES admission_attempts (operation_id, attempt_id)
)""",
)
