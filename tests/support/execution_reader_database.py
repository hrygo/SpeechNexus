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
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.jobs_postgres import (
    PostgresJobTransactions,
    job_pool,
)
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation_fixture
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as shared_database_fixture

_ROOT = Path(__file__).resolve().parents[2]
_PASSWORD = "synthetic_reader_foundation_test"
_JOB_PASSWORD = "synthetic_job_test"
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


@contextmanager
def _transactional_job_database(url: str) -> Iterator[JobDatabase]:
    """A full production Job port inside a disposable reader-compatible database."""
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_jobs_" + suffix, "sn_jobs_role_" + suffix
    with ExitStack() as cleanup:
        engine = create_engine(
            "postgresql+psycopg://",
            creator=lambda: psycopg.connect(url),
            poolclass=NullPool,
            hide_parameters=True,
        )
        cleanup.callback(engine.dispose)
        config = Config(str(_ROOT / "alembic.ini"))
        config.attributes["schema"] = schema
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migrations.upgrade(config, "head")
        admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        cleanup.callback(admin.close)
        admin.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(_JOB_PASSWORD)
            )
        )
        cleanup.callback(admin.execute, sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        cleanup.callback(admin.execute, sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        for grant in (*job_runtime_grants(schema, role), *job_dispatch_grants(schema, role)):
            admin.execute(grant)
        pool = job_pool(make_conninfo(url, user=role, password=_JOB_PASSWORD), schema=schema)
        pool.open()
        cleanup.callback(pool.close)
        yield JobDatabase(
            "postgresql", url, schema, role, admin, PostgresJobTransactions(pool), pool
        )


@pytest.fixture(params=["postgresql", "mysql"])
def reader_aware_database(request: pytest.FixtureRequest) -> Iterator[JobDatabase]:
    """Use a private PG database only for tests that request the real reader login."""
    if "reader_query_database_url" not in request.fixturenames:
        yield from cast(Any, shared_database_fixture).__wrapped__(request)
        return

    # The native composition root is PostgreSQL-only. Other tests still delegate
    # to the unmodified dual-database Job fixture and its configuration gate.
    root_url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not root_url:
        pytest.skip("An explicitly configured isolated PostgreSQL server is required")
    if request.param != "postgresql":
        pytest.fail("The production execution reader requires PostgreSQL")
    with (
        isolated_reader_database(root_url) as url,
        _transactional_job_database(url) as database,
    ):
        yield database


@pytest.fixture
def reader_query_database_url(foundation: Foundation) -> Iterator[SecretStr]:
    """A separate restricted reader and the writer's target-identification grant."""
    database = foundation.database
    role = "sn_reader_login_" + uuid.uuid4().hex[:12]
    admin = database.admin
    for statement in gateway_query_target_grants(database.schema, foundation.gateway_role):
        admin.execute(statement)
    admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(_PASSWORD)
        )
    )
    try:
        for statement in gateway_query_grants(database.schema, role):
            admin.execute(statement)
        yield SecretStr(make_conninfo(database.url, user=role, password=_PASSWORD))
    finally:
        admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
