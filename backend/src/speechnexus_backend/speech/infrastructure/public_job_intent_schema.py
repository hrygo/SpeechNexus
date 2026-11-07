"""Authoritative canonical intent of one accepted public Job.

Migration ``0046_public_job_intents`` separates the public business intent from
the private admission command body. Until the admission transaction takes over
``admission_operations.request_document``, the create path writes both copies in
one transaction; reads use this table only, so a missing or tampered intent is a
refusal rather than a value reconstructed from a mutable default.

The row is append-only: the writer role holds ``SELECT`` and ``INSERT`` and never
``UPDATE`` or ``DELETE``.
"""

INTENTS = "speech_job_intents"
JOBS = "speech_jobs"

PUBLIC_JOB_INTENT_DDL = (
    f"""CREATE TABLE {INTENTS} (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES {JOBS} (job_id) ON DELETE RESTRICT,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (request_digest ~ '^sha256:[0-9a-f]{{64}}$'),
        request_document TEXT NOT NULL
            CHECK (OCTET_LENGTH(request_document) BETWEEN 2 AND 262144),
        recorded_at_us BIGINT NOT NULL
            CHECK (recorded_at_us BETWEEN 1 AND 253402300799999999)
    )""",
)

PUBLIC_JOB_INTENT_DROP = (f"DROP TABLE {INTENTS}",)
