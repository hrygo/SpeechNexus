"""Real PG residue checks after injected setup failures; explicitly isolated only."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from alembic import command as migration
from tests.integration import test_binding_lifecycle_pg as lifecycle

pytestmark = pytest.mark.integration


class InjectedSetupError(RuntimeError):
    pass


@pytest.mark.parametrize("stage", ["migration", "role", "grant", "pool"])
def test_failed_setup_leaves_no_schema_or_role(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    if not os.environ.get(lifecycle.VARIABLE):
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not cleanup evidence")
    url = os.environ[lifecycle.VARIABLE]
    connect = psycopg.connect
    upgrade = migration.upgrade
    names: dict[str, str] = {}

    class Admin:
        def __init__(self) -> None:
            self.connection = connect(url, autocommit=True)

        def execute(self, query: str) -> Any:
            if query.startswith("CREATE SCHEMA"):
                names["schema"] = query.split('"')[1]
            if query.startswith("CREATE ROLE"):
                names["role"] = query.split('"')[1]
                if stage == "role":
                    raise InjectedSetupError(stage)
            if stage == "grant" and query.startswith("GRANT"):
                raise InjectedSetupError(stage)
            return self.connection.execute(query)

        def close(self) -> None:
            self.connection.close()

    def migrate(*args: Any, **kwargs: Any) -> None:
        upgrade(*args, **kwargs)
        if stage == "migration":
            # Fail after actual DDL committed, not before acquiring resources.
            raise InjectedSetupError(stage)

    def pool(*args: Any, **kwargs: Any) -> Any:
        raise InjectedSetupError("pool")

    with connect(url, autocommit=True) as observer:
        # Intercept only the fixture's admin connection. Alembic/SQLAlchemy must
        # keep the real driver's connect function for actual migrations.
        monkeypatch.setattr(lifecycle, "psycopg", SimpleNamespace(connect=lambda *a, **kw: Admin()))
        monkeypatch.setattr(migration, "upgrade", migrate)
        if stage == "pool":
            monkeypatch.setattr(lifecycle, "speech_pool", pool)
        fixture = lifecycle.sandbox._get_wrapped_function()()
        with pytest.raises(InjectedSetupError, match=stage):
            next(fixture)
        assert "schema" in names
        assert observer.execute("SELECT to_regnamespace(%s)", (names["schema"],)).fetchone() == (
            None,
        )
        if "role" in names:
            assert (
                observer.execute(
                    "SELECT rolname FROM pg_roles WHERE rolname=%s", (names["role"],)
                ).fetchone()
                is None
            )
