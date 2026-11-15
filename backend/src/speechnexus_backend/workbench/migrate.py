"""One-shot owner migration for the independent workbench schema.

The workbench has its own PostgreSQL namespace and no Alembic branch: the speech
migration chain owns ``speech`` and the workbench fragments
(:mod:`speechnexus_backend.workbench.infrastructure.runtime_schema`) are executed
exactly once by the *owner*, on a fresh namespace, inside one transaction. This
module is that step for the reference deployment (``workbench-migrate``), and it
is the only place the workbench DDL runs.

It is not a runtime migration runner and it never repairs drift:

- a fresh namespace is bootstrapped and both non-owner roles are granted their
  declared surface in the same transaction, so a half-created schema cannot be
  left behind;
- an existing namespace is *verified* (the recorded revision must equal this
  build's) and the two grant declarations are replayed, which is idempotent;
- the owner DSN must be a different identity from both runtime roles, and both
  roles must be fresh non-owner logins, otherwise the step fails naming the
  violated condition instead of granting a wider authority.

Every outcome is an explicit line: ``OK:`` for a step that is now true and
``FAIL:`` (plus exit code 1) for one that is not. ``docker compose`` treats a
completed container as success, so "nothing printed an error" is not the
evidence this step offers.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TextIO, cast

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from speechnexus_backend.workbench.infrastructure.runtime_schema import (
    SCHEMA_REVISION,
    bootstrap_sql,
    cleanup_grants_sql,
    runtime_grants_sql,
)
from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier

OWNER_DSN_ENV = "SPEECHNEXUS_MIGRATION_DATABASE_URL"
SCHEMA_ENV = "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA"
RUNTIME_DSN_ENV = "SPEECHNEXUS_WORKBENCH_DATABASE_URL"
CLEANUP_DSN_ENV = "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL"
DEFAULT_SCHEMA = "workbench"
# The publishable tables the runtime grants give to the runtime role, and the
# erasing authority the cleanup role needs. Verified after the grants so a role
# that was created without its declaration fails here, not at first request.
RUNTIME_PROBE = ("projects", "SELECT")
CLEANUP_PROBE = ("projects", "DELETE")


@dataclass(frozen=True)
class Plan:
    """The validated inputs of one migration run."""

    owner_dsn: str
    schema: str
    runtime_role: str
    cleanup_role: str


def _role_of(dsn: str) -> str | None:
    """Read the login name out of a DSN without echoing the DSN itself."""
    try:
        value = conninfo_to_dict(dsn).get("user")
    except psycopg.Error:
        return None
    return value if isinstance(value, str) and value else None


def findings(env: Mapping[str, str]) -> list[str]:
    """Name every missing or contradictory input before the owner connects."""
    owner = env.get(OWNER_DSN_ENV, "").strip()
    runtime = env.get(RUNTIME_DSN_ENV, "").strip()
    cleanup = env.get(CLEANUP_DSN_ENV, "").strip()
    problems: list[str] = []
    if not owner:
        problems.append(
            f"{OWNER_DSN_ENV} is blank: the workbench schema is created by the owner "
            "identity, never by a runtime role"
        )
    if not runtime:
        problems.append(
            f"{RUNTIME_DSN_ENV} is blank: the workbench runtime role the migration "
            "grants to is unknown"
        )
    if not cleanup:
        problems.append(
            f"{CLEANUP_DSN_ENV} is blank: the deletion ledger replay needs its own "
            "narrow cleanup role, so the migration cannot grant it"
        )
    if problems:
        return problems
    owner_role, runtime_role, cleanup_role = (
        _role_of(owner),
        _role_of(runtime),
        _role_of(cleanup),
    )
    for role, key in (
        (owner_role, OWNER_DSN_ENV),
        (runtime_role, RUNTIME_DSN_ENV),
        (cleanup_role, CLEANUP_DSN_ENV),
    ):
        if role is None:
            problems.append(f"{key} is not a postgresql:// DSN naming a login role")
    if owner_role is not None and runtime_role is not None and owner_role == runtime_role:
        problems.append(
            f"{RUNTIME_DSN_ENV} names the owner identity {owner_role}; the workbench "
            "runtime must be a dedicated non-owner role or the startup gate refuses it"
        )
    if owner_role is not None and cleanup_role is not None and owner_role == cleanup_role:
        problems.append(
            f"{CLEANUP_DSN_ENV} names the owner identity {owner_role}; the cleanup "
            "identity must be a separate non-owner role"
        )
    if runtime_role is not None and cleanup_role is not None and runtime_role == cleanup_role:
        problems.append(
            f"{RUNTIME_DSN_ENV} and {CLEANUP_DSN_ENV} name the same role "
            f"{runtime_role}; the serving identity must never hold the erasing "
            "authority the cleanup role needs"
        )
    schema = env.get(SCHEMA_ENV, DEFAULT_SCHEMA).strip() or DEFAULT_SCHEMA
    try:
        schema_identifier(schema)
    except ValueError:
        problems.append(
            f"{SCHEMA_ENV} must be an explicit lowercase identifier that is not "
            "`public`, `information_schema` or a pg_ reserved name"
        )
    if schema == "speech":
        # The workbench never shares the speech product schema: granting there would
        # hand a workbench role authority over the base tables, and the workbench
        # fragments would try to create tables next to the speech chain.
        problems.append(
            f"{SCHEMA_ENV} is `speech`: the workbench owns a separate namespace "
            "because it never shares or writes the speech product schema"
        )
    return problems


def plan(env: Mapping[str, str]) -> Plan:
    """Build the validated plan; a named problem is a failure, never a default."""
    problems = findings(env)
    if problems:
        raise ValueError("; ".join(problems))
    runtime_role = _role_of(env[RUNTIME_DSN_ENV].strip())
    cleanup_role = _role_of(env[CLEANUP_DSN_ENV].strip())
    assert runtime_role is not None and cleanup_role is not None
    return Plan(
        owner_dsn=env[OWNER_DSN_ENV].strip(),
        schema=env.get(SCHEMA_ENV, DEFAULT_SCHEMA).strip() or DEFAULT_SCHEMA,
        runtime_role=runtime_role,
        cleanup_role=cleanup_role,
    )


def _role_problem(connection: psycopg.Connection[Any], role: str) -> str | None:
    """A migrated role must exist and must not be a cluster-wide authority."""
    row = connection.execute(
        "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls AS unsafe "
        "FROM pg_catalog.pg_roles WHERE rolname=%s",
        (role,),
    ).fetchone()
    if row is None:
        return (
            f"role {role} does not exist; create the workbench runtime and cleanup "
            "roles in the runbook §6 step before migrating"
        )
    if row[0]:
        return f"role {role} is a cluster-wide authority; use a fresh non-owner login"
    return None


def _recorded_revision(connection: psycopg.Connection[Any], schema: str) -> int | None:
    row = connection.execute(
        sql.SQL("SELECT revision FROM {}.runtime_schema_revision").format(sql.Identifier(schema))
    ).fetchone()
    return None if row is None else int(row[0])


def _granted(
    connection: psycopg.Connection[Any], role: str, schema: str, table: str, privilege: str
) -> bool:
    row = connection.execute(
        "SELECT pg_catalog.has_table_privilege(%s, %s, %s)",
        (role, f"{schema}.{table}", privilege),
    ).fetchone()
    return bool(row is not None and row[0])


def run(value: Plan, out: TextIO) -> None:
    """Bootstrap a fresh namespace or verify an existing one, then grant both roles."""
    with (
        psycopg.connect(value.owner_dsn, connect_timeout=5) as connection,
        connection.transaction(),
    ):
        # The owner may not be one of the roles it grants to: that would make
        # the runtime role an owner and the startup gate refuses it, but the
        # contradiction is cheaper to name here.
        for role in (value.runtime_role, value.cleanup_role):
            problem = _role_problem(connection, role)
            if problem is not None:
                raise ValueError(problem)
        namespace = connection.execute(
            "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s", (value.schema,)
        ).fetchone()
        if namespace is None:
            connection.execute(bootstrap_sql(value.schema))
            print(
                f"OK: bootstrapped workbench schema {value.schema} at revision {SCHEMA_REVISION}",
                file=out,
            )
        else:
            revision = _recorded_revision(connection, value.schema)
            if revision != SCHEMA_REVISION:
                raise ValueError(
                    f"schema {value.schema} records revision {revision} but this "
                    f"build expects {SCHEMA_REVISION}; the workbench fragments are "
                    "fresh-only, so migrate a new namespace instead of editing it"
                )
            print(
                f"OK: workbench schema {value.schema} already at revision {SCHEMA_REVISION}",
                file=out,
            )
        # Both declarations are idempotent and are the only place the runtime
        # and cleanup surface is written; nothing here is hand-written SQL.
        for statement in runtime_grants_sql(value.schema, value.runtime_role):
            connection.execute(cast("sql.Composed", statement))
        for statement in cleanup_grants_sql(value.schema, value.cleanup_role):
            connection.execute(cast("sql.Composed", statement))
        print(
            f"OK: granted the runtime surface to {value.runtime_role} and the "
            f"cleanup surface to {value.cleanup_role}",
            file=out,
        )
        for role, (table, privilege) in (
            (value.runtime_role, RUNTIME_PROBE),
            (value.cleanup_role, CLEANUP_PROBE),
        ):
            if not _granted(connection, role, value.schema, table, privilege):
                raise ValueError(
                    f"role {role} still lacks {privilege} on {value.schema}.{table} "
                    "after the grant declaration ran"
                )
        print(
            "OK: workbench migration verified (namespace, revision, runtime and cleanup surface)",
            file=out,
        )


def main(
    env: Mapping[str, str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    execute: Callable[[Plan, TextIO], None] = run,
) -> int:
    """Run one migration; return the process exit code instead of raising."""
    environment = os.environ if env is None else env
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    problems = findings(environment)
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=stderr)
        return 1
    value = plan(environment)
    try:
        execute(value, stdout)
    except ValueError as error:
        print(f"FAIL: {error}", file=stderr)
        return 1
    except psycopg.Error as error:
        # Driver text can carry a connection string or an object name; name the
        # failing condition type and the inputs an operator must check instead.
        print(
            "FAIL: the owner connection could not complete the workbench migration "
            f"({type(error).__name__}); check {OWNER_DSN_ENV} and the roles "
            f"{value.runtime_role}/{value.cleanup_role}",
            file=stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - console-script entry
    raise SystemExit(main())


__all__ = ["Plan", "findings", "main", "plan", "run"]
