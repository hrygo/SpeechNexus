"""Measured-capability evidence, immutable voice revisions and publication receipts.

The evidence table is append-only: the recorder identity may INSERT and SELECT it
but holds no UPDATE or DELETE, so a correction is a new validation Job and a new
evidence row. Publication reads that evidence and writes the immutable revision,
the publication/pilot rows and its idempotency receipt in one transaction. Object
storage is never touched here; proof that the revision's material exists comes
from ``speech_asset_objects``.

The public catalogue reads ``speech_published_voice_revisions``. The view now
accepts a custom draft document as well as a builtin qualification document and
keeps the fail-closed rule: a selected publication whose document is missing
still raises the catalogue's 503 instead of silently dropping one row.
"""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

EVIDENCE_TABLE = "speech_voice_capability_evidence"
REVISION_TABLE = "speech_voice_revisions"
INTENT_TABLE = "speech_voice_publication_intents"
READER_VIEW = "speech_published_voice_revisions"

_EVIDENCE_ID = r"'^vce-[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'"
_IDENTIFIER = r"'^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'"
_REVISION = r"'^[1-9][0-9]{0,18}$'"
_DIGEST = r"'^sha256:[0-9a-f]{64}$'"
_MAX_TIMESTAMP = "253402300799999999"


EVIDENCE_DDL = (
    f"""CREATE TABLE {EVIDENCE_TABLE} (
        evidence_id VARCHAR(132) COLLATE "C" PRIMARY KEY
            CHECK (evidence_id ~ {_EVIDENCE_ID}),
        draft_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (draft_id ~ {_IDENTIFIER}),
        draft_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (draft_revision ~ {_REVISION}),
        validation_job_id VARCHAR(128) COLLATE "C" NOT NULL UNIQUE
            REFERENCES speech_jobs (job_id) ON DELETE RESTRICT,
        validation_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (validation_id ~ {_IDENTIFIER}),
        binding_revision_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (binding_revision_id ~ {_IDENTIFIER}),
        report_artifact_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (report_artifact_id ~ {_IDENTIFIER}),
        report_sha256 VARCHAR(71) COLLATE "C" NOT NULL CHECK (report_sha256 ~ {_DIGEST}),
        report_receipt_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (report_receipt_id ~ {_IDENTIFIER}),
        evidence_kind VARCHAR(32) COLLATE "C" NOT NULL
            CHECK (evidence_kind IN ('validation_run')),
        measured_by VARCHAR(256) NOT NULL
            CHECK (length(measured_by) BETWEEN 1 AND 256),
        measured_at_us BIGINT NOT NULL
            CHECK (measured_at_us BETWEEN 0 AND {_MAX_TIMESTAMP}),
        capabilities JSONB NOT NULL,
        coverage JSONB NOT NULL,
        material JSONB,
        evidence_digest VARCHAR(71) COLLATE "C" NOT NULL CHECK (evidence_digest ~ {_DIGEST}),
        CHECK (evidence_id = 'vce-' || validation_job_id),
        FOREIGN KEY (draft_id, draft_revision)
            REFERENCES speech_voice_draft_revisions (draft_id, revision) ON DELETE RESTRICT
    )""",
    f"""CREATE INDEX {EVIDENCE_TABLE}_draft_revision
    ON {EVIDENCE_TABLE} (draft_id, draft_revision)""",
)

REVISION_DDL = (
    f"""CREATE TABLE {REVISION_TABLE} (
        kind VARCHAR(16) COLLATE "C" NOT NULL DEFAULT 'voice'
            CHECK (kind = 'voice'),
        voice_id VARCHAR(128) COLLATE "C" NOT NULL CHECK (voice_id ~ {_IDENTIFIER}),
        revision VARCHAR(19) COLLATE "C" NOT NULL CHECK (revision ~ {_REVISION}),
        revision_id VARCHAR(128) COLLATE "C" NOT NULL CHECK (revision_id ~ {_IDENTIFIER}),
        source_type VARCHAR(16) COLLATE "C" NOT NULL
            CHECK (source_type IN ('designed', 'cloned')),
        document JSONB NOT NULL,
        material JSONB,
        evidence_id VARCHAR(132) COLLATE "C" NOT NULL
            REFERENCES {EVIDENCE_TABLE} (evidence_id) ON DELETE RESTRICT,
        draft_id VARCHAR(128) COLLATE "C" NOT NULL CHECK (draft_id ~ {_IDENTIFIER}),
        draft_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (draft_revision ~ {_REVISION}),
        published_at_us BIGINT NOT NULL
            CHECK (published_at_us BETWEEN 0 AND {_MAX_TIMESTAMP}),
        PRIMARY KEY (voice_id, revision),
        UNIQUE (revision_id),
        FOREIGN KEY (kind, voice_id, revision, revision_id)
            REFERENCES speech_publications (kind, object_id, revision, revision_id)
            ON DELETE RESTRICT,
        FOREIGN KEY (draft_id, draft_revision)
            REFERENCES speech_voice_draft_revisions (draft_id, revision) ON DELETE RESTRICT
    )""",
    f"""CREATE UNIQUE INDEX {REVISION_TABLE}_draft_revision
    ON {REVISION_TABLE} (draft_id, draft_revision) WHERE draft_id IS NOT NULL""",
)

INTENT_DDL = (
    f"""CREATE TABLE {INTENT_TABLE} (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY CHECK (key_digest ~ {_DIGEST}),
        request_digest VARCHAR(71) COLLATE "C" NOT NULL CHECK (request_digest ~ {_DIGEST}),
        intent JSONB NOT NULL,
        draft_id VARCHAR(128) COLLATE "C" NOT NULL CHECK (draft_id ~ {_IDENTIFIER}),
        draft_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (draft_revision ~ {_REVISION}),
        voice_id VARCHAR(128) COLLATE "C" NOT NULL CHECK (voice_id ~ {_IDENTIFIER}),
        voice_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (voice_revision ~ {_REVISION}),
        result JSONB NOT NULL,
        FOREIGN KEY (voice_id, voice_revision)
            REFERENCES {REVISION_TABLE} (voice_id, revision) ON DELETE RESTRICT
    )""",
)

# The pre-0047 shape, kept so downgrade restores exactly the view it replaced.
LEGACY_READER_VIEW_DDL = f"""CREATE VIEW {READER_VIEW}
WITH (security_barrier = true, security_invoker = false) AS
SELECT p.object_id AS voice_id, p.revision, p.revision_id, p.published_at_us,
       q.document,
       EXISTS (SELECT 1 FROM speech_publication_heads h
               WHERE h.kind=p.kind AND h.object_id=p.object_id
                 AND h.revision=p.revision AND h.revision_id=p.revision_id) AS selected,
       r.mode AS retirement_mode, r.reason AS retirement_reason, r.retired_at_us
FROM speech_publications p
LEFT JOIN speech_builtin_voice_qualifications q
  ON q.voice_id=p.object_id AND q.revision=p.revision AND q.revision_id=p.revision_id
LEFT JOIN speech_publication_retirements r
  ON r.kind=p.kind AND r.object_id=p.object_id
 AND r.revision=p.revision AND r.revision_id=p.revision_id
WHERE p.kind='voice'"""

# A custom draft document and a builtin qualification document are mutually
# exclusive sources for one publication identity. Either may be missing, which
# the reader treats as an unavailable document (503), never as an empty row.
READER_VIEW_DDL = f"""CREATE OR REPLACE VIEW {READER_VIEW}
WITH (security_barrier = true, security_invoker = false) AS
SELECT p.object_id AS voice_id, p.revision, p.revision_id, p.published_at_us,
       COALESCE(v.document, q.document) AS document,
       EXISTS (SELECT 1 FROM speech_publication_heads h
               WHERE h.kind=p.kind AND h.object_id=p.object_id
                 AND h.revision=p.revision AND h.revision_id=p.revision_id) AS selected,
       r.mode AS retirement_mode, r.reason AS retirement_reason, r.retired_at_us
FROM speech_publications p
LEFT JOIN speech_builtin_voice_qualifications q
  ON q.voice_id=p.object_id AND q.revision=p.revision AND q.revision_id=p.revision_id
LEFT JOIN {REVISION_TABLE} v
  ON v.voice_id=p.object_id AND v.revision=p.revision AND v.revision_id=p.revision_id
LEFT JOIN speech_publication_retirements r
  ON r.kind=p.kind AND r.object_id=p.object_id
 AND r.revision=p.revision AND r.revision_id=p.revision_id
WHERE p.kind='voice'"""


def voice_evidence_recorder_grants(schema: str, role: str) -> tuple[str, ...]:
    """The independent measurement recorder: append evidence, never rewrite it.

    No UPDATE or DELETE is granted. The recorder reads the Job, its immutable
    result, the committed report artifact and the draft revision it measured, and
    it may INSERT evidence rows only.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_jobs, "{schema}".speech_artifacts, '
        f'"{schema}".speech_voice_drafts, "{schema}".speech_voice_draft_revisions, '
        f'"{schema}".speech_binding_revisions, "{schema}".speech_assets, '
        f'"{schema}".speech_asset_objects TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{EVIDENCE_TABLE} TO "{role}"',
    )


def voice_publication_grants(schema: str, role: str) -> tuple[str, ...]:
    """The publisher identity: narrow reads plus the exact writes a publish needs.

    Evidence is SELECT-only: publication never rewrites the measurement that
    qualifies it. Publication rows are INSERT-only, the selected head and the
    catalogue version are the only mutable rows, and the draft's ``status`` is
    the only draft column it may change.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{EVIDENCE_TABLE}, "{schema}".speech_jobs, '
        f'"{schema}".speech_artifacts, "{schema}".speech_assets, '
        f'"{schema}".speech_asset_objects, "{schema}".speech_binding_revisions, '
        f'"{schema}".speech_publications, "{schema}".speech_publication_heads, '
        f'"{schema}".speech_catalog_state, "{schema}".speech_voice_drafts, '
        f'"{schema}".speech_voice_draft_revisions TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_publications, '
        f'"{schema}".{REVISION_TABLE}, "{schema}".{INTENT_TABLE} TO "{role}"',
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".speech_publication_heads TO "{role}"',
        f'GRANT UPDATE (revision, generated_at_us) ON "{schema}".speech_catalog_state TO "{role}"',
        f'GRANT UPDATE (status) ON "{schema}".speech_voice_drafts TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_asset_references TO "{role}"',
    )
