"""Persisted metadata, pinned generation input and metadata replay receipts."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    VOICE_DRAFT_INSERT_COLUMNS,
    VOICE_DRAFT_REVISION_INSERT_COLUMNS,
    VOICE_METADATA_COMMAND_INSERT_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    voice_draft_publication_reader_grants,
)

VOICE_DRAFT_DDL = (
    """CREATE TABLE speech_voice_drafts (
        draft_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        generation_job_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE
            REFERENCES speech_jobs(job_id) ON DELETE RESTRICT,
        kind VARCHAR(16) NOT NULL CHECK (kind IN ('design', 'clone')),
        status VARCHAR(16) NOT NULL CHECK (status IN
            ('draft', 'generating', 'candidate', 'validating', 'validated',
             'published', 'failed', 'preview_only')),
        head_revision VARCHAR(19) NOT NULL CHECK (head_revision ~ '^[1-9][0-9]{0,18}$'),
        metadata_revision BIGINT NOT NULL CHECK (metadata_revision >= 1),
        display_name VARCHAR(128) NOT NULL CHECK (length(display_name) BETWEEN 1 AND 128),
        input_json TEXT NOT NULL CHECK (OCTET_LENGTH(input_json) BETWEEN 2 AND 65536),
        binding_json TEXT NOT NULL CHECK (OCTET_LENGTH(binding_json) BETWEEN 2 AND 1024),
        created_at_us BIGINT NOT NULL CHECK (created_at_us BETWEEN 0 AND 253402300799999999),
        updated_at_us BIGINT NOT NULL CHECK (updated_at_us >= created_at_us)
    )""",
    """CREATE TABLE speech_voice_metadata_commands (
        draft_id VARCHAR(128) COLLATE "C" NOT NULL
            REFERENCES speech_voice_drafts(draft_id) ON DELETE RESTRICT,
        key_digest VARCHAR(71) COLLATE "C" NOT NULL,
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        result_json TEXT NOT NULL CHECK (OCTET_LENGTH(result_json) BETWEEN 2 AND 8192),
        PRIMARY KEY (draft_id, key_digest)
    )""",
)

VOICE_DRAFT_REVISION_DDL = (
    """CREATE TABLE speech_voice_draft_revisions (
        draft_id VARCHAR(128) COLLATE "C" NOT NULL
            REFERENCES speech_voice_drafts(draft_id) ON DELETE RESTRICT,
        revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (revision ~ '^[1-9][0-9]{0,18}$'),
        kind VARCHAR(16) NOT NULL CHECK (kind IN ('design', 'clone')),
        generation_job_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE
            REFERENCES speech_jobs(job_id) ON DELETE RESTRICT,
        input_json TEXT NOT NULL CHECK (OCTET_LENGTH(input_json) BETWEEN 2 AND 65536),
        binding_json TEXT NOT NULL CHECK (OCTET_LENGTH(binding_json) BETWEEN 2 AND 1024),
        created_at_us BIGINT NOT NULL CHECK (created_at_us BETWEEN 0 AND 253402300799999999),
        PRIMARY KEY (draft_id, revision)
    )""",
)

VOICE_DRAFT_REVISION_BACKFILL = """INSERT INTO speech_voice_draft_revisions
        (draft_id, revision, kind, generation_job_id, input_json, binding_json, created_at_us)
    SELECT draft_id, head_revision, kind, generation_job_id, input_json, binding_json,
           created_at_us
    FROM speech_voice_drafts"""


def voice_draft_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT SELECT ON "{schema}".speech_voice_drafts TO "{role}"',
        f"GRANT INSERT ({', '.join(VOICE_DRAFT_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_voice_drafts TO "{role}"',
        f"GRANT UPDATE (display_name, metadata_revision, updated_at_us, "
        f"head_revision, generation_job_id) "
        f'ON "{schema}".speech_voice_drafts TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_draft_revisions TO "{role}"',
        f"GRANT INSERT ({', '.join(VOICE_DRAFT_REVISION_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_voice_draft_revisions TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_metadata_commands TO "{role}"',
        f"GRANT INSERT ({', '.join(VOICE_METADATA_COMMAND_INSERT_COLUMNS)}) "
        f'ON "{schema}".speech_voice_metadata_commands TO "{role}"',
        # The same identity reads the draft back, and a published draft's revision
        # must carry the facts that only the owner-privileged view exposes.
        *voice_draft_publication_reader_grants(schema, role),
    )


def voice_validation_worker_grants(schema: str, role: str) -> tuple[str, ...]:
    """The validation fact reader's narrow additions to the dispatch worker.

    The worker replays the frozen draft revision and the ready reference Asset;
    it does not own draft writes and does not need the publication role's
    catalogue or Artifact-object privileges.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_voice_draft_revisions, '
        f'"{schema}".speech_assets TO "{role}"',
    )
