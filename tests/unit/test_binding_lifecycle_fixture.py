"""Fixture fault injection only: no PostgreSQL connection or migration is executed."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command as migration

from tests.integration import test_binding_lifecycle_pg as lifecycle


class SetupError(RuntimeError):
    pass


class Resources:
    def __init__(self, failure: str) -> None:
        self.failure = failure
        self.schemas: set[str] = set()
        self.roles: set[str] = set()
        self.admin_closed = False
        self.engine_disposed = False
        self.pool_closed = False
        self.pool_created = False
        self.granted = False

    def fail(self, stage: str) -> None:
        if self.failure == stage:
            raise SetupError(stage)

    def execute(self, query: str) -> None:
        if query.startswith("CREATE SCHEMA"):
            self.fail("schema")
            if "fixture_schema" in self.schemas:
                raise SetupError("schema_collision")
            self.schemas.add("fixture_schema")
        elif query.startswith("CREATE ROLE"):
            self.fail("role")
            if "fixture_role" in self.roles:
                raise SetupError("role_collision")
            self.roles.add("fixture_role")
        elif query.startswith("GRANT"):
            self.fail("grant")
            self.granted = True
        elif query.startswith("DROP OWNED"):
            self.fail("cleanup")
        elif query.startswith("DROP SCHEMA"):
            self.schemas.clear()
        elif query.startswith("DROP ROLE"):
            # Schema grants would keep a real PG role alive after DROP OWNED failed.
            assert not (self.granted and self.schemas)
            self.roles.clear()
        else:
            assert query.startswith("SET search_path")


def install(monkeypatch: pytest.MonkeyPatch, failure: str) -> Resources:
    state = Resources(failure)

    class Engine:
        @contextmanager
        def connect(self) -> Iterator[object]:
            yield object()

        def dispose(self) -> None:
            state.engine_disposed = True

    class Admin:
        execute = staticmethod(state.execute)

        def close(self) -> None:
            state.admin_closed = True

    class Pool:
        def open(self) -> None:
            state.fail("open")

        def close(self) -> None:
            state.pool_closed = True

    def connect(*args: Any, **kwargs: Any) -> Admin:
        state.fail("connect")
        return Admin()

    def upgrade(*args: Any) -> None:
        # Model a migration that has already made partial schema changes.
        assert state.schemas == {"fixture_schema"}
        state.fail("migration")

    def pool(*args: Any, **kwargs: Any) -> Pool:
        state.fail("pool")
        state.pool_created = True
        return Pool()

    monkeypatch.setenv(lifecycle.VARIABLE, "postgresql://synthetic.invalid/never-connect")
    monkeypatch.setattr(lifecycle, "create_engine", lambda *a, **kw: Engine())
    monkeypatch.setattr(lifecycle, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setattr(migration, "upgrade", upgrade)
    monkeypatch.setattr(lifecycle, "speech_pool", pool)
    return state


@pytest.mark.parametrize(
    "failure", ["connect", "schema", "migration", "role", "grant", "pool", "open"]
)
def test_initialization_failure_releases_only_acquired_resources(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    state = install(monkeypatch, failure)
    fixture = lifecycle.sandbox._get_wrapped_function()()
    with pytest.raises(SetupError, match=failure):
        next(fixture)
    assert state.schemas == set()
    assert state.roles == set()
    assert state.engine_disposed
    assert state.admin_closed is (failure != "connect")
    assert state.pool_closed is state.pool_created


def test_role_collision_does_not_drop_the_existing_role(monkeypatch: pytest.MonkeyPatch) -> None:
    state = install(monkeypatch, "role_collision")
    state.roles.add("fixture_role")
    fixture = lifecycle.sandbox._get_wrapped_function()()
    with pytest.raises(SetupError, match="role_collision"):
        next(fixture)
    assert state.roles == {"fixture_role"}
    assert state.schemas == set()
    assert state.admin_closed and state.engine_disposed


def test_schema_collision_does_not_migrate_or_drop_existing_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = install(monkeypatch, "schema_collision")
    state.schemas.add("fixture_schema")
    fixture = lifecycle.sandbox._get_wrapped_function()()
    with pytest.raises(SetupError, match="schema_collision"):
        next(fixture)
    assert state.schemas == {"fixture_schema"}
    assert state.roles == set()
    assert state.admin_closed and state.engine_disposed


@pytest.mark.parametrize("failure", ["body", "cleanup", "none"])
def test_teardown_attempts_all_cleanup_even_when_one_action_fails(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    state = install(monkeypatch, failure)
    fixture = lifecycle.sandbox._get_wrapped_function()()
    next(fixture)
    if failure == "body":
        with pytest.raises(SetupError, match="body"):
            fixture.throw(SetupError("body"))
    elif failure == "cleanup":
        with pytest.raises(SetupError, match="cleanup"):
            next(fixture)
    else:
        with pytest.raises(StopIteration):
            next(fixture)
    assert state.schemas == set()
    assert state.roles == set()
    assert state.admin_closed and state.engine_disposed and state.pool_closed
