"""The workbench migration must be an explicit step with named outcomes.

``speechnexus-workbench-migrate`` is the only place the workbench namespace DDL
runs, so its two jobs are to refuse contradictory inputs *before* connecting and
to print ``OK:``/``FAIL:`` per step instead of relying on a container that exits
0. These tests drive ``findings``, ``plan`` and ``run`` with a fake connection and
``main`` with an injected executor, so no database is needed.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

import psycopg
import pytest

from speechnexus_backend.workbench import migrate
from speechnexus_backend.workbench.infrastructure.runtime_schema import SCHEMA_REVISION

OWNER = "postgresql://speechnexus:owner-secret@postgres:5432/speechnexus"
RUNTIME = "postgresql://sn_workbench_runtime:runtime-secret@postgres:5432/speechnexus"
CLEANUP = "postgresql://sn_workbench_cleanup:cleanup-secret@postgres:5432/speechnexus"

ENV = {
    "SPEECHNEXUS_MIGRATION_DATABASE_URL": OWNER,
    "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": "workbench",
    "SPEECHNEXUS_WORKBENCH_DATABASE_URL": RUNTIME,
    "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": CLEANUP,
}


class FakeConnection:
    """Answers the four catalog questions ``run`` asks; records every statement."""

    def __init__(
        self,
        *,
        roles: dict[str, bool] | None = None,
        schema_exists: bool = False,
        revision: int | None = None,
        granted: bool = True,
    ) -> None:
        # ``roles`` maps an existing role to whether it is a cluster-wide authority.
        self.roles = (
            {"sn_workbench_runtime": False, "sn_workbench_cleanup": False}
            if roles is None
            else roles
        )
        self.schema_exists = schema_exists
        self.revision = revision
        self.granted = granted
        self.statements: list[str] = []

    def execute(self, query: Any, params: Any = None) -> Any:
        text = query if isinstance(query, str) else query.as_string(None)
        self.statements.append(text)
        if "pg_catalog.pg_roles" in text:
            role = params[0]
            if role not in self.roles:
                return _Result(None)
            return _Result((self.roles[role],))
        if "pg_catalog.pg_namespace" in text:
            return _Result((1,) if self.schema_exists else None)
        if "runtime_schema_revision" in text and text.lstrip().upper().startswith("SELECT"):
            if self.revision is None:
                return _Result(None)
            return _Result((self.revision,))
        if "has_table_privilege" in text:
            return _Result((self.granted,))
        return _Result(None)

    def close(self) -> None:
        """The driver adapter closes the wrapped connection on exit."""
        return

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *error: object) -> None:
        return None


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeConnection]:
    holder: dict[str, FakeConnection] = {}

    def connect(dsn: str, **_kwargs: Any) -> FakeConnection:
        assert dsn == OWNER, "the migration must connect as the owner only"
        return holder["connection"]

    monkeypatch.setattr(psycopg, "connect", connect)
    holder["connection"] = FakeConnection()
    yield holder["connection"]


def _run(value: migrate.Plan) -> list[str]:
    out = io.StringIO()
    migrate.run(value, out)
    return out.getvalue().splitlines()


def test_a_blank_input_is_named_before_connecting() -> None:
    for key, expected in (
        ("SPEECHNEXUS_MIGRATION_DATABASE_URL", "created by the owner identity"),
        ("SPEECHNEXUS_WORKBENCH_DATABASE_URL", "the workbench runtime role"),
        ("SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL", "deletion ledger replay"),
    ):
        env = {name: value for name, value in ENV.items() if name != key}
        problems = migrate.findings(env)
        assert any(key in problem and expected in problem for problem in problems), problems


def test_the_owner_and_the_two_roles_must_be_three_identities() -> None:
    for key in ("SPEECHNEXUS_WORKBENCH_DATABASE_URL", "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL"):
        problems = migrate.findings({**ENV, key: OWNER})
        assert any("names the owner identity" in problem for problem in problems), problems
    shared = migrate.findings({**ENV, "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": RUNTIME})
    assert any("name the same role sn_workbench_runtime" in p for p in shared), shared
    assert migrate.findings(ENV) == []


def test_the_schema_must_be_an_explicit_non_speech_identifier() -> None:
    for schema in ("speech", "public", "pg_temp", "Bad", "-bad-name", "1bad"):
        problems = migrate.findings({**ENV, "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": schema})
        assert any("SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA" in p for p in problems), schema
    # A blank schema falls back to the documented default, not to `speech`.
    assert migrate.findings({**ENV, "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": " "}) == []


def test_a_dsn_without_a_login_name_is_named() -> None:
    problems = migrate.findings({**ENV, "SPEECHNEXUS_WORKBENCH_DATABASE_URL": "postgresql:///db"})
    assert any("not a postgresql:// DSN naming a login role" in p for p in problems), problems


def test_the_plan_derives_both_login_names_from_their_dsns() -> None:
    value = migrate.plan(ENV)
    assert value.runtime_role == "sn_workbench_runtime"
    assert value.cleanup_role == "sn_workbench_cleanup"
    assert value.schema == "workbench"
    assert value.owner_dsn == OWNER
    with pytest.raises(ValueError):
        migrate.plan({**ENV, "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": "speech"})


def test_a_fresh_namespace_is_bootstrapped_and_both_roles_granted(
    connection: FakeConnection,
) -> None:
    lines = _run(migrate.plan(ENV))
    joined = "\n".join(connection.statements)
    assert "CREATE SCHEMA" in joined.upper()
    assert f"revision {SCHEMA_REVISION}" in lines[0]
    assert lines[0].startswith("OK: bootstrapped workbench schema workbench")
    assert "granted the runtime surface to sn_workbench_runtime" in lines[1]
    assert "cleanup surface to sn_workbench_cleanup" in lines[1]
    assert lines[2].startswith("OK: workbench migration verified")


def test_an_existing_namespace_is_verified_and_regranted(
    connection: FakeConnection,
) -> None:
    connection.schema_exists = True
    connection.revision = SCHEMA_REVISION
    lines = _run(migrate.plan(ENV))
    joined = "\n".join(connection.statements)
    assert "CREATE SCHEMA" not in joined.upper(), "an existing namespace must not be recreated"
    assert lines[0].startswith("OK: workbench schema workbench already at revision")
    assert "GRANT" in joined.upper(), "the declarations are replayed and are idempotent"


def test_a_stale_revision_fails_instead_of_editing_the_namespace(
    connection: FakeConnection,
) -> None:
    connection.schema_exists = True
    connection.revision = SCHEMA_REVISION - 1
    with pytest.raises(ValueError, match="fresh-only"):
        _run(migrate.plan(ENV))


def test_a_missing_role_is_named_before_any_grant(connection: FakeConnection) -> None:
    connection.roles = {}
    with pytest.raises(ValueError) as caught:
        _run(migrate.plan(ENV))
    message = str(caught.value)
    assert "sn_workbench_runtime does not exist" in message
    assert "runbook §6" in message


def test_a_cluster_wide_role_is_refused(connection: FakeConnection) -> None:
    connection.roles = {"sn_workbench_runtime": False, "sn_workbench_cleanup": True}
    with pytest.raises(ValueError, match="cluster-wide authority"):
        _run(migrate.plan(ENV))


def test_a_grant_that_did_not_land_fails_the_step(connection: FakeConnection) -> None:
    connection.granted = False
    with pytest.raises(ValueError, match="still lacks"):
        _run(migrate.plan(ENV))


def test_main_names_each_blank_input_and_exits_non_zero() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = migrate.main(
        {k: v for k, v in ENV.items() if k != "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL"},
        out=out,
        err=err,
        execute=_must_not_connect,
    )
    assert code == 1
    assert "FAIL: SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL is blank" in err.getvalue()
    assert out.getvalue() == ""


def _must_not_connect(value: migrate.Plan, stream: TextIO) -> None:
    pytest.fail("must not connect")


def test_main_reports_a_named_refusal_from_the_run() -> None:
    def execute(value: migrate.Plan, stream: TextIO) -> None:
        raise ValueError("role sn_workbench_runtime does not exist")

    out, err = io.StringIO(), io.StringIO()
    assert migrate.main(ENV, out=out, err=err, execute=execute) == 1
    assert "FAIL: role sn_workbench_runtime does not exist" in err.getvalue()


def test_main_reports_a_driver_failure_by_type_only() -> None:
    def execute(value: migrate.Plan, stream: TextIO) -> None:
        raise psycopg.OperationalError(f"could not connect: {OWNER}")

    out, err = io.StringIO(), io.StringIO()
    assert migrate.main(ENV, out=out, err=err, execute=execute) == 1
    message = err.getvalue()
    assert "OperationalError" in message
    assert "owner-secret" not in message and "postgresql://" not in message
    assert out.getvalue() == ""


def test_main_returns_zero_after_a_clean_run() -> None:
    out, err = io.StringIO(), io.StringIO()

    def execute(value: migrate.Plan, stream: TextIO) -> None:
        print("OK: workbench migration verified", file=stream)

    assert migrate.main(ENV, out=out, err=err, execute=execute) == 0
    assert out.getvalue().startswith("OK:")
    assert err.getvalue() == ""


class QmarkBlindConnection(FakeConnection):
    """A driver that only understands its own placeholders, like psycopg does.

    This is what the migration met on the new coordination database: the module's
    statements are written to the portable ``?`` contract, and psycopg answers such
    a statement with ``ProgrammingError: the query has 0 placeholders but 1
    parameters were passed``. Reproducing it here is what makes the adapter wrap a
    checked fact instead of a comment.
    """

    def execute(self, query: Any, params: Any = None) -> Any:
        text = query if isinstance(query, str) else query.as_string(None)
        if params is not None and "?" in text:
            raise psycopg.ProgrammingError(
                "the query has 0 placeholders but 1 parameters were passed"
            )
        return super().execute(query, params)


def test_the_owner_connection_speaks_the_drivers_placeholder_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vendor placeholder never reaches the driver; the adapter is the only seam.

    Removing ``PortableConnection`` from ``run`` reproduces the production failure
    (``ProgrammingError``) here, so the wrap cannot be refactored away silently.
    """
    holder = QmarkBlindConnection()
    monkeypatch.setattr(psycopg, "connect", lambda dsn, **_: holder)
    out = io.StringIO()

    migrate.run(migrate.plan(ENV), out)

    assert holder.statements, "the migration must have executed its statements"
    assert any("%s" in statement for statement in holder.statements), (
        "the owner connection did not translate the portable placeholders"
    )
