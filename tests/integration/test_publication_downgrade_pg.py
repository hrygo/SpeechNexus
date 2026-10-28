"""Historical publication visibility and downgrade lock protection on PostgreSQL."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
HistoricalSchema = tuple[str, str, Engine, Config, psycopg.Connection[tuple[object, ...]]]


@pytest.fixture
def historical_schema() -> Iterator[HistoricalSchema]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_head_down_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    admin = psycopg.connect(url, autocommit=True)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0023_publication_lifecycle")
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        yield url, schema, engine, config, admin
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def downgrade(engine: Engine, config: Config, *, isolation: str = "READ COMMITTED") -> None:
    with engine.connect().execution_options(isolation_level=isolation) as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0022_catalog_revision_state")


def test_later_lower_revision_refuses_downgrade_and_preserves_history(
    historical_schema: HistoricalSchema,
) -> None:
    _, _, engine, config, admin = historical_schema
    with admin.cursor(row_factory=dict_row) as cursor:
        assert append_publication(
            cursor, Publication("voice", "voice-test", 10, "voice-rev-10", 100)
        )
        assert append_publication(cursor, Publication("voice", "voice-test", 2, "voice-rev-2", 200))
    before = admin.execute(
        "SELECT * FROM speech_publications ORDER BY revision::numeric"
    ).fetchall()
    heads = admin.execute("SELECT * FROM speech_publication_heads").fetchall()
    assert admin.execute("SELECT count(*) FROM speech_publication_lifecycle_events").fetchone() == (
        0,
    )
    with pytest.raises(RuntimeError, match="head visibility requires preservation"):
        downgrade(engine, config)
    assert (
        admin.execute("SELECT * FROM speech_publications ORDER BY revision::numeric").fetchall()
        == before
    )
    assert admin.execute("SELECT * FROM speech_publication_heads").fetchall() == heads
    assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0023_publication_lifecycle",
    )


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_old_snapshot_refused_before_ddl(
    historical_schema: HistoricalSchema, isolation: str
) -> None:
    _, _, engine, config, admin = historical_schema
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        downgrade(engine, config, isolation=isolation)
    assert admin.execute(
        "SELECT to_regclass('speech_publication_heads') IS NOT NULL"
    ).fetchone() == (True,)


def test_downgrade_waits_for_committed_writer_then_refuses(
    historical_schema: HistoricalSchema,
) -> None:
    url, schema, engine, config, admin = historical_schema
    with admin.cursor(row_factory=dict_row) as cursor:
        assert append_publication(
            cursor, Publication("voice", "voice-test", 10, "voice-rev-10", 100)
        )
    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(url, row_factory=dict_row) as writer:
            writer.execute(f'SET search_path TO "{schema}", pg_catalog')
            with writer.cursor() as cursor:
                assert append_publication(
                    cursor, Publication("voice", "voice-test", 2, "voice-rev-2", 200)
                )
            pid_row = writer.execute("SELECT pg_backend_pid() AS pid").fetchone()
            assert pid_row is not None
            pid = pid_row["pid"]
            future = executor.submit(downgrade, engine, config)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waiting = admin.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE %s=ANY(pg_blocking_pids(pid)))",
                    (pid,),
                ).fetchone()
                assert waiting is not None
                if waiting[0]:
                    break
                assert not future.done()
                time.sleep(0.01)
            else:
                pytest.fail("downgrade did not wait for the publication writer")
        with pytest.raises(RuntimeError, match="head visibility requires preservation"):
            future.result(timeout=10)
    assert admin.execute("SELECT revision FROM speech_publication_heads").fetchone() == ("2",)
