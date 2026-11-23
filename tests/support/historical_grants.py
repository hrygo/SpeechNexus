"""Apply the declared writer grants to a schema that may predate some of their columns.

Provisioning grants describe the **current** schema: ``job_runtime_grants`` names
every column the writer writes today, including ``bucket_no`` (0064). A migration
test that stops below 0064 still has to give its non-owner role the ACL that
deployment held, but the declared statement is not valid SQL there — PostgreSQL
rejects a column-level ``GRANT`` naming a missing column with
``UndefinedColumn`` and the message names the table, not the grant:

    column "bucket_no" of relation "coordination_jobs" does not exist

The historical ACL is the declared column surface intersected with the columns
the revision actually has. For 0064's addition that is the exact six-column
``INSERT`` a pre-0064 deployment held. Table-level statements are applied
verbatim; only the parenthesised column list of a column-level grant is narrowed.

This is a test-harness rule, not a production compatibility branch: the writer
itself is only supported against schema >= 0064, so nothing in ``backend/`` gets
to choose an INSERT shape. Tests that need to seed an older schema do it with
direct SQL (``tests.support.legacy_jobs``) and reproduce the ACL that revision
had, instead of driving today's writer against yesterday's tables.

A declaration that names a column the head schema also lacks would be narrowed
here too. That drift is caught by ``tests/integration/test_role_grant_coverage_pg.py``,
which applies the same declarations to the real head schema without this filter.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

#: ``GRANT <privilege> (a, b) ON "schema"."table" TO ...`` — the repository renders
#: tables both bare and double-quoted, so the table name is matched either way.
_COLUMN_GRANT = re.compile(r'^(GRANT \w+ \()([^()]*)(\) ON "[^"]+"\."?([a-z0-9_]+)"? TO )')
Query = Callable[[str, tuple[object, ...]], Any]


def apply_declared_grants(query: Query, statements: Iterable[str], *, schema: str) -> None:
    """Execute ``statements`` with column-level grants narrowed to existing columns.

    ``query`` takes one statement plus parameters and returns its fetched rows
    (empty for DDL/DCL), matching ``JobDatabase.query``.
    """
    for statement in statements:
        match = _COLUMN_GRANT.match(statement)
        if match is None:
            query(statement, ())
            continue
        table = match.group(4)
        existing = {
            row["column_name"]
            for row in query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
        }
        columns = [column.strip() for column in match.group(2).split(",")]
        kept = [column for column in columns if column in existing]
        if kept == columns:
            query(statement, ())
            continue
        if not kept:
            # No declared column exists at this revision: the grant surface for
            # this relation postdates it entirely, so there is nothing to grant.
            continue
        query(statement[: match.start(2)] + ", ".join(kept) + statement[match.end(2) :], ())
