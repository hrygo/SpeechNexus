"""Startup authority decisions shared by independent runtime database roles.

A table-level grant and a column-level grant are different authorities: a
column-only grant leaves ``has_table_privilege`` false, so a table predicate can
never stand in for a column decision. Every rejection names the authority and
the relation, and says how an operator repairs the grant.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection
from speechnexus_portable_sql.contract import ListMarker

# 身份分支写成两条完整语句:CURRENT_USER 是关键字,具名角色是参数,
# 拼进同一句就得在运行期决定文本。判据 test_startup_privilege_statements 成对核对。
_SCHEMA_USAGE_CURRENT = "SELECT pg_catalog.has_schema_privilege(CURRENT_USER, ?, 'USAGE') AS held"
_SCHEMA_USAGE_NAMED = "SELECT pg_catalog.has_schema_privilege(CAST(? AS name), ?, 'USAGE') AS held"
_SCHEMA_CREATE_CURRENT = "SELECT pg_catalog.has_schema_privilege(CURRENT_USER, ?, 'CREATE') AS held"
_SCHEMA_CREATE_NAMED = (
    "SELECT pg_catalog.has_schema_privilege(CAST(? AS name), ?, 'CREATE') AS held"
)
_TABLE_PRIVILEGE_CURRENT = "SELECT pg_catalog.has_table_privilege(CURRENT_USER, ?, ?) AS held"
_TABLE_PRIVILEGE_NAMED = "SELECT pg_catalog.has_table_privilege(CAST(? AS name), ?, ?) AS held"
_COLUMN_PRIVILEGE_CURRENT = "SELECT pg_catalog.has_any_column_privilege(CURRENT_USER, ?, ?) AS held"
_COLUMN_PRIVILEGE_NAMED = (
    "SELECT pg_catalog.has_any_column_privilege(CAST(? AS name), ?, ?) AS held"
)
_SEQUENCE_STRAY_CURRENT = (
    "SELECT c.relname AS name FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname=? AND c.relkind='S' "
    "AND c.relname NOT IN (?) "
    "AND pg_catalog.has_sequence_privilege("
    "CURRENT_USER, c.oid, 'USAGE,SELECT,UPDATE') "
    "ORDER BY c.relname"
)
_SEQUENCE_STRAY_NAMED = (
    "SELECT c.relname AS name FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname=? AND c.relkind='S' "
    "AND c.relname NOT IN (?) "
    "AND pg_catalog.has_sequence_privilege("
    "CAST(? AS name), c.oid, 'USAGE,SELECT,UPDATE') "
    "ORDER BY c.relname"
)


_STRAY_TABLE_CURRENT = (
    "SELECT c.relname AS name FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname=? AND c.relkind IN ('r','p','v','m','f') "
    "AND c.relname NOT IN (?) AND ("
    "pg_catalog.has_table_privilege("
    "CURRENT_USER, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
    "OR pg_catalog.has_any_column_privilege("
    "CURRENT_USER, c.oid, 'SELECT,INSERT,UPDATE,REFERENCES')) "
    "ORDER BY c.relname"
)
_STRAY_TABLE_NAMED = (
    "SELECT c.relname AS name FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname=? AND c.relkind IN ('r','p','v','m','f') "
    "AND c.relname NOT IN (?) AND ("
    "pg_catalog.has_table_privilege("
    "CAST(? AS name), c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
    "OR pg_catalog.has_any_column_privilege("
    "CAST(? AS name), c.oid, 'SELECT,INSERT,UPDATE,REFERENCES')) "
    "ORDER BY c.relname"
)


def current_role(connection: SpeechConnection) -> str:
    """Resolve the effective role of a startup connection without changing it."""
    row = connection.execute("SELECT CURRENT_USER AS name").fetchone()
    if row is None:
        raise ValueError("Runtime database identity is unavailable")
    return str(row["name"])


def require_schema_usage(
    connection: SpeechConnection, role: str | None, schema: str, *, subject: str
) -> None:
    if role is None:
        row = connection.execute(_SCHEMA_USAGE_CURRENT, (schema,)).fetchone()
    else:
        row = connection.execute(_SCHEMA_USAGE_NAMED, (role, schema)).fetchone()
    if row is None or not row["held"]:
        raise ValueError(
            f"{subject} lacks USAGE on schema {schema}; grant USAGE on that schema "
            "or correct the configured schema"
        )


def refuse_schema_create(
    connection: SpeechConnection, role: str | None, schema: str, *, subject: str
) -> None:
    if role is None:
        row = connection.execute(_SCHEMA_CREATE_CURRENT, (schema,)).fetchone()
    else:
        row = connection.execute(_SCHEMA_CREATE_NAMED, (role, schema)).fetchone()
    if row is None or row["held"]:
        raise ValueError(
            f"{subject} must not hold CREATE on schema {schema}; revoke the schema CREATE grant"
        )


def refuse_table_privileges(
    connection: SpeechConnection,
    role: str | None,
    qualified: str,
    table: str,
    privileges: tuple[str, ...],
    *,
    subject: str,
    repair: str | None = None,
) -> None:
    """A table grant covers every row, so name the exact authority it confers."""
    for privilege in privileges:
        if role is None:
            row = connection.execute(_TABLE_PRIVILEGE_CURRENT, (qualified, privilege)).fetchone()
        else:
            row = connection.execute(
                _TABLE_PRIVILEGE_NAMED, (role, qualified, privilege)
            ).fetchone()
        if row is None or row["held"]:
            raise ValueError(
                f"{subject} must not hold {privilege} on {table}; "
                + (repair or "revoke that table grant if the role does not need it")
            )


def refuse_column_privileges(
    connection: SpeechConnection,
    role: str | None,
    qualified: str,
    table: str,
    privileges: tuple[str, ...],
    *,
    subject: str,
) -> None:
    """Column grants are invisible to ``has_table_privilege``; compare them directly."""
    for privilege in privileges:
        if role is None:
            row = connection.execute(_COLUMN_PRIVILEGE_CURRENT, (qualified, privilege)).fetchone()
        else:
            row = connection.execute(
                _COLUMN_PRIVILEGE_NAMED, (role, qualified, privilege)
            ).fetchone()
        if row is None or row["held"]:
            raise ValueError(
                f"{subject} must not hold column {privilege} on {table}; "
                "revoke that column grant if the role does not need it"
            )


def refuse_sequence_privileges(
    connection: SpeechConnection,
    role: str | None,
    schema: str,
    declared: tuple[str, ...],
    *,
    subject: str,
    declared_hint: str,
) -> None:
    """Sequences carry their own ACL and are invisible to every table check.

    ``relkind='S'`` is outside the table/view/materialized-view enumeration, so
    an extra sequence grant never shows up in the column-by-column equality: the
    role holds ``USAGE``/``SELECT``/``UPDATE`` on a counter nobody declared.
    ``has_table_privilege`` cannot stand in for this check because sequence and
    table privilege letters mean different authorities, and there is no column
    ACL on a sequence.
    """
    if role is None:
        row = connection.execute(_SEQUENCE_STRAY_CURRENT, (schema, ListMarker(declared))).fetchone()
    else:
        row = connection.execute(
            _SEQUENCE_STRAY_NAMED, (schema, ListMarker(declared), role)
        ).fetchone()
    if row is not None:
        raise ValueError(
            f"{subject} must not hold sequence privileges on {row['name']}; "
            f"declare it in {declared_hint} only if the transaction needs it, "
            "or revoke the stray sequence grant"
        )


def refuse_foreign_relation_privileges(
    connection: SpeechConnection,
    role: str | None,
    schema: str,
    declared: tuple[str, ...],
    *,
    subject: str,
    declared_hint: str,
) -> None:
    """The role's authority is exactly its declared tables; name any stray grant."""
    if role is None:
        row = connection.execute(_STRAY_TABLE_CURRENT, (schema, ListMarker(declared))).fetchone()
    else:
        row = connection.execute(
            _STRAY_TABLE_NAMED, (schema, ListMarker(declared), role, role)
        ).fetchone()
    if row is not None:
        raise ValueError(
            f"{subject} must not hold privileges on undeclared table {row['name']}; "
            f"add it to {declared_hint} with only its declared privileges, "
            "or revoke the stray grant"
        )


__all__ = [
    "current_role",
    "refuse_column_privileges",
    "refuse_foreign_relation_privileges",
    "refuse_schema_create",
    "refuse_sequence_privileges",
    "refuse_table_privileges",
    "require_schema_usage",
]
