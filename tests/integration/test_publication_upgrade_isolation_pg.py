"""Protect lifecycle backfills from transaction snapshots older than table locks."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_binding_disable_migration_pg import seed_disable

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
@pytest.mark.parametrize(
    ("base", "target", "locked_table", "new_table"),
    [
        (
            "0022_catalog_revision_state",
            "0023_publication_lifecycle",
            "speech_publications",
            "speech_publication_heads",
        ),
        (
            "0024_binding_publish_intents",
            "0025_binding_disable_targets",
            "speech_publication_retirements",
            "speech_binding_disable_intents",
        ),
    ],
)
def test_upgrade_rejects_old_snapshot_before_locks_or_ddl(
    isolation: str, base: str, target: str, locked_table: str, new_table: str
) -> None:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_upgrade_isolation_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    admin = psycopg.connect(url, autocommit=True)
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, base)
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        if target == "0023_publication_lifecycle":
            admin.execute(
                "INSERT INTO speech_publications "
                "(kind, object_id, revision, revision_id, published_at_us) "
                "VALUES ('binding','binding-test','1','revision-test',1)"
            )
        else:
            admin.execute("DROP TABLE speech_binding_disable_intents")
            seed_disable(admin)
        before = admin.execute("SELECT * FROM speech_publications").fetchall()
        with psycopg.connect(url) as writer:
            writer.execute(f'SET search_path TO "{schema}", pg_catalog')
            writer.execute(f"LOCK TABLE {locked_table} IN ROW EXCLUSIVE MODE")
            with engine.connect().execution_options(isolation_level=isolation) as connection:
                # A wrong implementation trying to lock first fails by lock timeout,
                # rather than hanging behind the independent writer.
                connection.exec_driver_sql("SET lock_timeout = '500ms'")
                connection.commit()
                config.attributes["connection"] = connection
                with pytest.raises(RuntimeError, match="upgrade requires READ COMMITTED"):
                    command.upgrade(config, target)
        assert admin.execute("SELECT version_num FROM alembic_version").fetchone() == (base,)
        assert admin.execute("SELECT to_regclass(%s)", (new_table,)).fetchone() == (None,)
        assert admin.execute("SELECT * FROM speech_publications").fetchall() == before
        if target == "0025_binding_disable_targets":
            assert admin.execute(
                "SELECT count(*) FROM speech_publication_lifecycle_events "
                "WHERE action='model_binding.disable'"
            ).fetchone() == (1,)
            assert admin.execute(
                "SELECT count(*) FROM speech_publication_retirements WHERE mode='disable'"
            ).fetchone() == (1,)
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()
