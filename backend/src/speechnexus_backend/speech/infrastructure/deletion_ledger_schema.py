"""Frozen 0059 deletion-execution ledger; one row is one confirmed deletion.

The plan (0058) is a preview and authorizes nothing. This row is the acceptance:
it names the plan, digest, target, the exact deleted/retained scope and the
confirmation's idempotency identity, and it is the durable answer a replay
returns. ``(workspace_id, idempotency_key_digest)`` is unique, so one key cannot
commit two deletions; ``(workspace_id, plan_id)`` is unique because an issued plan
is a single-use credential.

The asset half reuses ``speech_content_deletions`` as the object-level ledger and
pins it through ``content_ledger_id``: this table records the deletion, that one
records which object version was purged. ``reason`` gains ``confirmed_deletion``
so a confirmed deletion is distinguishable from retention expiry in the same row.
"""

DELETION_LEDGER = "speech_deletion_ledger"

DELETION_LEDGER_INSERT_COLUMNS = (
    "id",
    "workspace_id",
    "plan_id",
    "plan_revision",
    "plan_digest",
    "target_owner",
    "target_kind",
    "target_id",
    "target_revision",
    "idempotency_key_digest",
    "intent_digest",
    "deleted",
    "retained",
    "status",
    "cleanup_state",
    "content_ledger_id",
    "created_at",
    "updated_at",
    "completed_at",
)

_CONFIRMED_REASON = """ALTER TABLE speech_content_deletions
    DROP CONSTRAINT speech_content_deletions_reason_check"""
_CONFIRMED_REASON_ADD = """ALTER TABLE speech_content_deletions
    ADD CONSTRAINT speech_content_deletions_reason_check
    CHECK (reason IN ('retention_expired', 'confirmed_deletion'))"""

DELETION_LEDGER_DDL = (
    _CONFIRMED_REASON,
    _CONFIRMED_REASON_ADD,
    """CREATE TABLE speech_deletion_ledger (
        id VARCHAR(128) COLLATE "C" PRIMARY KEY
            CHECK (id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'),
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL
            REFERENCES speech_workspaces (workspace_id),
        plan_id VARCHAR(128) COLLATE "C" NOT NULL
            REFERENCES speech_deletion_plans (id),
        plan_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (plan_revision ~ '^[1-9][0-9]{0,18}$'),
        plan_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
        target_owner VARCHAR(16) NOT NULL CHECK (target_owner = 'speech'),
        target_kind VARCHAR(32) NOT NULL
            CHECK (target_kind IN ('asset','operation_content')),
        target_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (target_id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'),
        target_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (target_revision ~ '^[1-9][0-9]{0,18}$'),
        idempotency_key_digest VARCHAR(64) COLLATE "C" NOT NULL
            CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
        intent_digest VARCHAR(64) COLLATE "C" NOT NULL
            CHECK (intent_digest ~ '^[0-9a-f]{64}$'),
        -- The exact public scope this row authorized, as
        -- [[owner, kind, id, revision], ...]; never a re-derived current set.
        deleted JSONB NOT NULL CHECK (
            jsonb_typeof(deleted) = 'array' AND jsonb_array_length(deleted) >= 1),
        retained JSONB NOT NULL CHECK (jsonb_typeof(retained) = 'array'),
        -- Slice B produces queued (content still awaiting the object purge or a
        -- resume) and succeeded (purge confirmed). blocked/failed need the stop
        -- coordination and read route of the later slices.
        status VARCHAR(16) NOT NULL CHECK (status IN ('queued', 'succeeded')),
        cleanup_state VARCHAR(16) NOT NULL CHECK (cleanup_state IN ('pending', 'complete')),
        content_ledger_id VARCHAR(128) COLLATE "C"
            REFERENCES speech_content_deletions (ledger_id),
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL CHECK (updated_at >= created_at),
        completed_at TIMESTAMPTZ,
        CHECK ((status = 'succeeded') = (completed_at IS NOT NULL)),
        CHECK ((status = 'succeeded') = (cleanup_state = 'complete')),
        UNIQUE (workspace_id, idempotency_key_digest),
        -- An issued plan is a single-use credential: a second key cannot replay
        -- it as if it were a fresh deletion.
        UNIQUE (workspace_id, plan_id)
    )""",
    """CREATE INDEX speech_deletion_ledger_target
       ON speech_deletion_ledger (workspace_id, target_kind, target_id)""",
)

# The exact column surfaces the Job writer needs on top of 0058. Declared once so
# the grant function, the route's own privilege probe and the deployment preflight
# cannot drift from the SQL the executor runs.
DELETION_LEDGER_UPDATE_COLUMNS = ("status", "cleanup_state", "updated_at", "completed_at")
CONTENT_DELETION_INSERT_COLUMNS = (
    "ledger_id",
    "asset_id",
    "sha256",
    "storage_version_id",
    "reason",
    "deletion_state",
    "blocked_reason",
    "created_at_us",
)
CONTENT_DELETION_UPDATE_COLUMNS = (
    "storage_version_id",
    "deletion_state",
    "blocked_reason",
    "completed_at_us",
)
ASSET_EVENT_INSERT_COLUMNS = (
    "event_id",
    "asset_id",
    "event_kind",
    "asset_version",
    "occurred_at_us",
)
# The asset state machine's own columns. They are the same writes the retention
# sweep performs; the deletion route takes the same row lock and the same steps.
ASSET_UPDATE_COLUMNS = ("asset_state", "upload_phase", "writer_id", "error_code", "version")
# The legacy private command copy is tombstoned instead of deleted: 0007's
# command-shape CHECK requires this exact quartet plus a version bump.
LEGACY_INTENT_UPDATE_COLUMNS = (
    "request_document",
    "command_redacted_at_us",
    "command_deletion_job_id",
    "command_deletion_evidence_digest",
    "version",
)
# The one table this writer may DELETE from: the authoritative public Job intent
# row of the confirmed operation (#540). Nothing else in the declared surface.
JOB_INTENT_DELETE_TABLES = ("speech_job_intents",)
