"""Refuse a Job runtime whose database never ran the migration profile.

The writer and admission guards read ``pg_catalog`` for the relations their role may
touch. On a database that never ran the migration profile every one of those
relations is absent, so the guard answered "the writer lacks SELECT on
``speech_workspaces``" -- a grant repair for what is really a missing migration, and
the first operator step it suggested was the wrong one (#920 item 3).

Migration presence is therefore decided first, on its own query, and the refusal is
this module's own type carrying the shared ``schema_not_migrated`` text. The check
is deliberately about the whole required surface rather than one table: a database
missing any of them cannot serve the Job surface, and naming the first absent
relation lets the log carry the fact while the public answer stays free of internal
identifiers.
"""

from __future__ import annotations

from collections.abc import Sequence

from speechnexus_backend.speech.domain.job_prerequisites import SCHEMA_NOT_MIGRATED
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection

# The relation whose absence is checked first: the migration ledger itself, so a
# database that never ran any revision is reported before the relations it would
# have created.
MIGRATION_LEDGER = "alembic_version"


class JobSchemaNotMigratedError(RuntimeError):
    """A required Job relation is absent: the migration profile has not been applied.

    The message is the shared operator text (the migration command and the restart
    step). ``missing`` carries the absent relations for the log and the tests, so the
    public refusal stays identical to every other surface that answers the same
    prerequisite.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        super().__init__(SCHEMA_NOT_MIGRATED.detail)


def require_migrated_schema(
    connection: SpeechConnection,
    schema: str,
    relations: Sequence[str],
) -> None:
    """Refuse before the role guards when any required relation is absent."""
    wanted = tuple(dict.fromkeys((MIGRATION_LEDGER, *relations)))
    rows = connection.execute(
        "SELECT r.relname AS name FROM pg_catalog.pg_class AS r "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = r.relnamespace "
        "WHERE n.nspname = %s AND r.relname = ANY(%s)",
        (schema, list(wanted)),
    ).fetchall()
    present = {str(row["name"]) for row in rows}
    missing = tuple(name for name in wanted if name not in present)
    if missing:
        raise JobSchemaNotMigratedError(missing)
