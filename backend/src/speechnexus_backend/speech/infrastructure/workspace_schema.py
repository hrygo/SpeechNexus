"""Accepted workspace commands: replay evidence, outside the asset runtime grant.

``speech_workspaces`` stays the frozen 0003 authority. This ledger adds only the
identity needed to resolve a retried ``Idempotency-Key`` before any mutation:
the key digest is the primary key, so a duplicate is refused by the database
rather than after a second insert. The raw key is never stored; the recorded
digest identifies it without keeping the client's value.

The row deliberately carries no foreign key to ``speech_workspaces``. It is
evidence of an accepted intent, not a reason a later authorised deletion of the
group could be blocked; a replay whose recorded group is gone is reported as
unavailable instead of recreating or fabricating it.
"""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

WORKSPACE_TABLE = "speech_workspaces"
COMMAND_TABLE = "speech_workspace_commands"

WORKSPACE_COMMAND_DDL = (
    """CREATE TABLE speech_workspace_commands (
        key_digest VARCHAR(71) COLLATE "C" PRIMARY KEY,
        operation VARCHAR(8) NOT NULL CHECK (operation IN ('create','update')),
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL,
        intent_digest VARCHAR(71) COLLATE "C" NOT NULL,
        result_metadata_revision BIGINT NOT NULL CHECK (result_metadata_revision >= 1),
        request_id VARCHAR(128) COLLATE "C" NOT NULL,
        recorded_at_us BIGINT NOT NULL CHECK (recorded_at_us >= 0)
    )""",
)

# Column-level only. A whole-table grant would silently widen when a later
# revision adds a column, so the writer declares every column it may insert or
# update and startup compares the granted set exactly.
WORKSPACE_INSERT_COLUMNS = (
    "workspace_id",
    "display_name",
    "workspace_state",
    "created_at_us",
)
WORKSPACE_UPDATE_COLUMNS = ("display_name", "workspace_state", "metadata_revision")
COMMAND_INSERT_COLUMNS = (
    "key_digest",
    "operation",
    "workspace_id",
    "intent_digest",
    "result_metadata_revision",
    "request_id",
    "recorded_at_us",
)


def workspace_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provisioning declaration only. No DELETE, TRUNCATE, DDL or owner authority."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    insert = ", ".join(WORKSPACE_INSERT_COLUMNS)
    update = ", ".join(WORKSPACE_UPDATE_COLUMNS)
    command_insert = ", ".join(COMMAND_INSERT_COLUMNS)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".{WORKSPACE_TABLE} TO "{role}"',
        f'GRANT INSERT ({insert}) ON "{schema}".{WORKSPACE_TABLE} TO "{role}"',
        f'GRANT UPDATE ({update}) ON "{schema}".{WORKSPACE_TABLE} TO "{role}"',
        f'GRANT SELECT ON "{schema}".{COMMAND_TABLE} TO "{role}"',
        f'GRANT INSERT ({command_insert}) ON "{schema}".{COMMAND_TABLE} TO "{role}"',
    )
