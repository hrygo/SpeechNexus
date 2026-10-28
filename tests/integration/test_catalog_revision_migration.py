"""Catalogue version migration keeps history monotonic and refuses hidden loss."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command as migration
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"


def scalar_int(
    connection: psycopg.Connection[tuple[object, ...]],
    query: str,
    parameters: tuple[object, ...] = (),
) -> int:
    row = connection.execute(query, parameters).fetchone()
    assert row is not None
    value = row[0]
    assert isinstance(value, int)
    return value


def test_legacy_publication_count_is_seeded_and_retirement_history_blocks_downgrade() -> None:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.skip("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_rev_" + uuid.uuid4().hex[:12]
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
            migration.upgrade(config, "0021_publication_evidence")
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        for index in (1, 2, 3):
            admin.execute(
                "INSERT INTO speech_publications "
                "(kind, object_id, revision, revision_id, published_at_us) "
                "VALUES ('binding', %s, '1', %s, %s)",
                (f"legacy-binding-{index}", f"legacy-rev-{index}", 1_700_000_000_000_000 + index),
            )
        legacy_revision = scalar_int(admin, "SELECT COUNT(*) + 1 FROM speech_publications")
        assert legacy_revision == 4

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "0022_catalog_revision_state")
        seeded_revision = scalar_int(
            admin, "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE"
        )
        assert seeded_revision == legacy_revision == 4

        admin.execute(
            "INSERT INTO speech_publications "
            "(kind, object_id, revision, revision_id, published_at_us) "
            "VALUES ('binding', 'post-migration-binding', '1', 'post-migration-rev', "
            "1700000000000010)"
        )
        assert (
            scalar_int(admin, "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE")
            == 5
        )
        assert scalar_int(admin, "SELECT COUNT(*) + 1 FROM speech_publications") == 5

        # A reversible migration preserves the same legacy-visible value when no
        # retirement has hidden a row. Re-upgrading seeds that same value again.
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.downgrade(config, "0021_publication_evidence")
        state_relation = admin.execute(
            "SELECT to_regclass(%s)", (f'"{schema}".speech_catalog_state',)
        ).fetchone()
        assert state_relation is not None and state_relation[0] is None
        assert scalar_int(admin, "SELECT COUNT(*) + 1 FROM speech_publications") == 5
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            migration.upgrade(config, "0022_catalog_revision_state")
        assert (
            scalar_int(admin, "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE")
            == 5
        )

        admin.execute(
            "INSERT INTO speech_publication_retirements "
            "(kind, object_id, revision, revision_id, retired_at_us) "
            "VALUES ('binding', 'legacy-binding-1', '1', 'legacy-rev-1', 1700000000000020)"
        )
        assert (
            scalar_int(admin, "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE")
            == 6
        )
        with (
            pytest.raises(RuntimeError, match="retirements require preservation"),
            engine.connect() as connection,
        ):
            config.attributes["connection"] = connection
            migration.downgrade(config, "0021_publication_evidence")
        retired_revision = scalar_int(
            admin, "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE"
        )
        assert retired_revision == 6
        print(
            f"catalog_revision_seed_before={legacy_revision} "
            f"catalog_revision_seed_after={seeded_revision} "
            f"catalog_revision_after_reupgrade=5 "
            f"catalog_revision_after_retirement={retired_revision}"
        )
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()
