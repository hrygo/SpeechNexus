"""Isolated PG and least-privilege reader fixture for production gateway tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command as migrations
from alembic.config import Config
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_query_grants,
    gateway_query_target_grants,
)
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation_fixture
from tests.integration.test_production_jobs import JobDatabase

_ROOT = Path(__file__).resolve().parents[2]
_PASSWORD = "synthetic_reader_foundation_test"
_LO_FUNCTIONS = (
    "lo_create",
    "lo_creat",
    "lo_from_bytea",
    "lo_import",
    "lo_export",
    "lo_unlink",
    "lo_put",
    "lo_truncate",
    "lo_truncate64",
    "lowrite",
)


@contextmanager
def isolated_reader_database(root_url: str) -> Iterator[str]:
    """Modify ACLs only in a freshly created, uniquely named temporary database."""
    name = "sn_reader_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(root_url, autocommit=True) as root:
        root.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        url = make_conninfo(root_url, dbname=name)
        try:
            with psycopg.connect(url, autocommit=True, row_factory=dict_row) as admin:
                admin.execute(
                    sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(name)
                    )
                )
                signatures = admin.execute(
                    "SELECT p.oid::pg_catalog.regprocedure::text AS signature "
                    "FROM pg_catalog.pg_proc p "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='pg_catalog' AND p.proname=ANY(%s)",
                    (list(_LO_FUNCTIONS),),
                ).fetchall()
                if not signatures:
                    raise RuntimeError("missing PostgreSQL large-object authority functions")
                for row in signatures:
                    # Only signatures returned from this new database's pg_catalog.
                    admin.execute(
                        sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC").format(
                            sql.SQL(row["signature"])
                        )
                    )
            yield url
        finally:
            root.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@dataclass(frozen=True)
class ReaderFoundation:
    foundation: Foundation
    query_database_url: SecretStr = field(repr=False)


@pytest.fixture
def reader_foundation() -> Iterator[ReaderFoundation]:
    """Production schema, normal writers, and a separate read-only login in a fresh DB."""
    root_url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not root_url:
        pytest.skip("An explicitly configured isolated PostgreSQL server is required")
    with isolated_reader_database(root_url) as url:
        schema = "sn_reader_foundation_" + uuid.uuid4().hex[:12]
        role = "sn_reader_login_" + uuid.uuid4().hex[:12]
        with ExitStack() as cleanup:
            engine = create_engine(
                "postgresql+psycopg://",
                creator=lambda: psycopg.connect(url),
                poolclass=NullPool,
                hide_parameters=True,
            )
            cleanup.callback(engine.dispose)
            admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            cleanup.callback(admin.close)
            config = Config(str(_ROOT / "alembic.ini"))
            config.attributes["schema"] = schema
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                migrations.upgrade(config, "head")
            admin.execute(
                sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema))
            )
            # This bounded fixture uses the foundation's schema/grants/seed only;
            # its Job transaction port is never used by these gateway tests.
            db = JobDatabase("postgresql", url, schema, "", admin, cast(Any, None), None)
            foundation_generator = cast(Any, foundation_fixture).__wrapped__(db)
            cleanup.callback(foundation_generator.close)
            foundation = next(foundation_generator)
            for statement in gateway_query_target_grants(schema, foundation.gateway_role):
                admin.execute(statement)
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(_PASSWORD)
                )
            )
            cleanup.callback(admin.execute, sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            cleanup.callback(
                admin.execute, sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
            )
            for statement in gateway_query_grants(schema, role):
                admin.execute(statement)
            yield ReaderFoundation(
                foundation,
                SecretStr(make_conninfo(url, user=role, password=_PASSWORD)),
            )
