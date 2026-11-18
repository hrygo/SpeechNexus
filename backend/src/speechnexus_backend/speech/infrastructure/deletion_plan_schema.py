"""Frozen 0058 deletion-plan storage; plans pin content, they never delete it.

One row is one immutable preview. It carries the exact target revision and
content digest read under lock, the normalized dependency list the public plan
was hashed over, and a private owner snapshot the execution slice rechecks
atomically. Idempotency is a database invariant: one
``(workspace_id, idempotency_key_digest)`` row, so two concurrent requests with
one key cannot commit two plans.
"""

DELETION_PLANS = "speech_deletion_plans"

DELETION_PLAN_INSERT_COLUMNS = (
    "id",
    "workspace_id",
    "target_owner",
    "target_kind",
    "target_id",
    "target_revision",
    "target_digest",
    "mode",
    "plan_revision",
    "plan_digest",
    "idempotency_key_digest",
    "intent_digest",
    "created_at",
    "expires_at",
    "dependencies",
    "content_snapshot",
)

DELETION_PLAN_DDL = (
    """CREATE TABLE speech_deletion_plans (
        id VARCHAR(128) COLLATE "C" PRIMARY KEY
            CHECK (id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'),
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL
            REFERENCES speech_workspaces (workspace_id),
        target_owner VARCHAR(16) NOT NULL CHECK (target_owner = 'speech'),
        target_kind VARCHAR(32) NOT NULL
            CHECK (target_kind IN ('asset','artifact','voice_revision','operation_content')),
        target_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (target_id ~ '^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$'),
        target_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (target_revision ~ '^[1-9][0-9]{0,18}$'),
        -- The content identity read under lock; execution rechecks it instead of
        -- trusting a summary or a current default.
        target_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (target_digest ~ '^sha256:[0-9a-f]{64}$'),
        mode VARCHAR(16) NOT NULL CHECK (mode IN ('ordinary','cascade')),
        plan_revision VARCHAR(19) COLLATE "C" NOT NULL
            CHECK (plan_revision ~ '^[1-9][0-9]{0,18}$'),
        plan_digest VARCHAR(71) COLLATE "C" NOT NULL
            CHECK (plan_digest ~ '^sha256:[0-9a-f]{64}$'),
        idempotency_key_digest VARCHAR(64) COLLATE "C" NOT NULL
            CHECK (idempotency_key_digest ~ '^[0-9a-f]{64}$'),
        intent_digest VARCHAR(64) COLLATE "C" NOT NULL
            CHECK (intent_digest ~ '^[0-9a-f]{64}$'),
        created_at TIMESTAMPTZ NOT NULL,
        -- The window is the plan's, not the client's: no row may extend it.
        expires_at TIMESTAMPTZ NOT NULL CHECK (
            expires_at = created_at + interval '300 seconds'),
        dependencies JSONB NOT NULL CHECK (jsonb_typeof(dependencies) = 'array'),
        content_snapshot JSONB NOT NULL
            CHECK (jsonb_typeof(content_snapshot) = 'object'),
        UNIQUE (workspace_id, idempotency_key_digest)
    )""",
    """CREATE INDEX speech_deletion_plans_target
       ON speech_deletion_plans (workspace_id, target_kind, target_id)""",
)
