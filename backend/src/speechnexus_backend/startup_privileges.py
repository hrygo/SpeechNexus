"""Startup authority decisions shared by independent runtime database roles.

A table-level grant and a column-level grant are different authorities: a
column-only grant leaves ``has_table_privilege`` false, so a table predicate can
never stand in for a column decision. Every rejection names the authority and
the relation, and says how an operator repairs the grant.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection

_TABLE_SURFACE = "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'"
_COLUMN_SURFACE = "'SELECT,INSERT,UPDATE,REFERENCES'"


def current_role(connection: SpeechConnection) -> str:
    """Resolve the effective role of a startup connection without changing it."""
    row = connection.execute("SELECT CURRENT_USER AS name").fetchone()
    if row is None:
        raise ValueError("Runtime database identity is unavailable")
    return str(row["name"])


def _role_argument(role: str | None) -> tuple[str, tuple[object, ...]]:
    if role is None:
        return "CURRENT_USER", ()
    return "%s::name", (role,)


def require_schema_usage(
    connection: SpeechConnection, role: str | None, schema: str, *, subject: str
) -> None:
    clause, prefix = _role_argument(role)
    row = connection.execute(
        f"SELECT pg_catalog.has_schema_privilege({clause}, %s, 'USAGE') AS held",
        (*prefix, schema),
    ).fetchone()
    if row is None or not row["held"]:
        raise ValueError(
            f"{subject} lacks USAGE on schema {schema}; grant USAGE on that schema "
            "or correct the configured schema"
        )


def refuse_schema_create(
    connection: SpeechConnection, role: str | None, schema: str, *, subject: str
) -> None:
    clause, prefix = _role_argument(role)
    row = connection.execute(
        f"SELECT pg_catalog.has_schema_privilege({clause}, %s, 'CREATE') AS held",
        (*prefix, schema),
    ).fetchone()
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
    clause, prefix = _role_argument(role)
    for privilege in privileges:
        row = connection.execute(
            f"SELECT pg_catalog.has_table_privilege({clause}, %s, %s) AS held",
            (*prefix, qualified, privilege),
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
    clause, prefix = _role_argument(role)
    for privilege in privileges:
        row = connection.execute(
            f"SELECT pg_catalog.has_any_column_privilege({clause}, %s, %s) AS held",
            (*prefix, qualified, privilege),
        ).fetchone()
        if row is None or row["held"]:
            raise ValueError(
                f"{subject} must not hold column {privilege} on {table}; "
                "revoke that column grant if the role does not need it"
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
    clause, prefix = _role_argument(role)
    row = connection.execute(
        "SELECT c.relname AS name FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
        "AND c.relname <> ALL(%s::text[]) AND ("
        f"pg_catalog.has_table_privilege({clause}, c.oid, {_TABLE_SURFACE}) OR "
        f"pg_catalog.has_any_column_privilege({clause}, c.oid, {_COLUMN_SURFACE})) "
        "ORDER BY c.relname LIMIT 1",
        (schema, list(declared), *prefix, *prefix),
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
    "refuse_table_privileges",
    "require_schema_usage",
]
